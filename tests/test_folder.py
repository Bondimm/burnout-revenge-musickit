"""HostFS folder output with your own disc images (MUSICKIT_ISO = PAL, MUSICKIT_USA_ISO = USA; skipped without).

The quick tests copy only the files MusicKit reads (~350 MB) into pytest's temp folder. The end-to-end test
extracts the whole disc (~4 GB per ISO, one at a time) and runs only with MUSICKIT_FULL_TEST=1.
"""
import hashlib
import os
import shutil
import struct
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from musickit import core, elfpatch, hostfs, iso, validate  # noqa: E402

ISOS = [p for p in (os.environ.get("MUSICKIT_ISO", ""), os.environ.get("MUSICKIT_USA_ISO", "")) if p and os.path.exists(p)]
WAV = os.path.join(HERE, "data", "test_chords_22k_mono.wav")
FLAC = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")
quiet = lambda *a: None   # noqa: E731
needs_iso = pytest.mark.skipif(not ISOS, reason="set MUSICKIT_ISO and/or MUSICKIT_USA_ISO")


@pytest.fixture(autouse=True)
def _mini_folders_ok(request, monkeypatch):
    """The quick tests use folders with only the files the kit reads: skip the check for a complete game folder."""
    if not request.node.name.startswith(("test_folder_end_to_end", "test_gui_folder_flow")):
        monkeypatch.setattr(core, "REQUIRED", {})


class _Hash:
    def __init__(self):
        self.h = hashlib.sha1()

    def write(self, b):
        self.h.update(b)


def _digest(data):
    h = _Hash()
    hostfs._write_data(h, data)
    return h.h.hexdigest()


def _mini_folder(d, folder):
    """A game folder with only the files MusicKit reads, copied from the disc image of `d`."""
    for p in ["/SYSTEM.CNF"] + sorted(d.changed_paths):
        e = d.img.entries[p]
        host, off = core.span(d.img, p)
        target = hostfs.host_path(folder, e.path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            hostfs._write_data(f, (host, off, e.size))
    return folder


def _mtimes(folder):
    return {os.path.join(r, n): os.stat(os.path.join(r, n)).st_mtime_ns for r, _, ns in os.walk(folder) for n in ns}


def _changes(items):
    items.pop(2)                                   # remove an original song
    items.insert(0, items.pop(-1))                 # move the last song (in _EATRAX1) to the top
    items[5].title = "Folder Title"                # rename for every language
    return items


@needs_iso
@pytest.mark.parametrize("src", ISOS)
def test_folder_reads_and_saves_like_the_iso(src, tmp_path):
    d = core.Disc(src)
    folder = _mini_folder(d, str(tmp_path / "game"))
    f = core.Disc(folder)
    try:
        assert f.is_folder and not d.is_folder and f.region == d.region and f.crc == d.crc
        assert [s.title for s in f.songs] == [s.title for s in d.songs] and f.langs == d.langs
        assert f.size_plan(f.current_list()) is None
        with pytest.raises(ValueError):
            f.build_list(f.current_list(), str(tmp_path / "x.iso"))
        # the same song list gives byte-identical files from the ISO and from the folder
        reps_d, _ = d.replacements(_changes(d.current_list()), normalize=False, unlock_off=True)
        reps_f, rep = f.replacements(_changes(f.current_list()), normalize=False, unlock_off=True)
        assert sorted(reps_d) == sorted(reps_f)
        for p in reps_d:
            assert _digest(reps_d[p]) == _digest(reps_f[p]), p
        # save into the folder itself
        rep = f.build_folder(_changes(f.current_list()), folder, normalize=False, unlock_off=True)
        assert rep["folder"] == os.path.abspath(folder) and rep["total_songs"] == d.count - 1
        assert set(rep["written"]) == set(reps_f)
        r = core.Disc(folder)
        assert r.count == d.count - 1 and r.unlocked and r.crc == d.crc
        assert r.songs[0].title == d.songs[-1].title and r.songs[5].title == "Folder Title"
        assert validate.check_songs(d, r, quiet)
        for p in reps_d:
            assert hostfs.file_sha1(core.span(r.img, p)[0]) == _digest(reps_d[p]), p
        b = iso.IsoImage(rep["boot_iso"])
        try:
            assert os.path.basename(rep["boot_iso"]) == core.GAME + " (HostFS).iso"
            assert b.read_file(d.elf_path) == r.elf and b.read_file("/SYSTEM.CNF") == d.img.read_file("/SYSTEM.CNF")
        finally:
            b.f.close()
        steps = "\n".join(r.hostfs_steps())
        assert hostfs.pnach_name(d.elf_path, d.crc) in steps and core.NAHELAM_DIR in steps
        # nothing changed -> saving again rewrites the same bytes
        before = {p: hostfs.file_sha1(core.span(r.img, p)[0]) for p in reps_d}
        r.build_folder(r.current_list(), folder, normalize=False)
        r2 = core.Disc(folder)
        assert {p: hostfs.file_sha1(core.span(r2.img, p)[0]) for p in reps_d} == before
    finally:
        d.close()


@needs_iso
def test_folder_checks_and_locks(tmp_path):
    d = core.Disc(ISOS[0])
    try:
        assert d.check_folder(str(tmp_path / "new")) is None
        assert d.folder_plan(str(tmp_path / "new"))["extract_bytes"] == sum(e.size for e in d.img.entries.values())
        other = tmp_path / "other"
        other.mkdir()
        (other / "notes.txt").write_text("x")
        assert "other files" in d.check_folder(str(other))
        (tmp_path / "file.bin").write_bytes(b"x")
        assert "file" in d.check_folder(str(tmp_path / "file.bin"))
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        (wrong / "SYSTEM.CNF").write_bytes(b"BOOT2 = cdrom0:\\SLUS_999.99;1\r\n")
        assert "another game" in d.check_folder(str(wrong))
        folder = _mini_folder(d, str(tmp_path / "game"))
        assert d.check_folder(folder) is None
        plan = d.folder_plan(folder)
        assert plan["has_game"] and 0 < plan["extract_bytes"] < sum(e.size for e in d.img.entries.values())
        f = core.Disc(folder)
        assert "into itself" in f.check_folder(str(tmp_path / "new"))
        assert f.check_folder(folder) is None and f.folder_plan(folder) == {"extract_bytes": 0, "has_game": True}
        bad = elfpatch.Elf(f.elf)
        bad.d[-4:] = b"\x01\x02\x03\x04"                # another tool changed the executable (other CRC)
        with open(core.span(f.img, f.elf_path)[0], "wb") as out:
            out.write(bytes(bad.d))
        assert "CRC" in d.check_folder(folder)
        with open(core.span(f.img, f.elf_path)[0], "wb") as out:
            x = struct.unpack("<2I", b"CHAINKIT")       # the Burnout Chain mod (ChainKit) is in this folder;
            out.write(d.elf + b"CHAINKIT" + struct.pack("<I", x[0] ^ x[1]))   # (same PCSX2 CRC)
        assert "ChainKit" in d.check_folder(folder)
        with open(core.span(f.img, f.elf_path)[0], "wb") as out:
            out.write(d.elf)
        if os.name == "nt":                            # a running game keeps its files open: refused, nothing written
            rws = core.span(f.img, core.RWS_FILES[0])[0]
            t0 = os.path.getmtime(core.span(f.img, f.elf_path)[0])
            with open(rws, "rb"):
                with pytest.raises(hostfs.HostFSError):
                    f.build_folder(_changes(f.current_list()), folder, normalize=False)
            assert os.path.getmtime(core.span(f.img, f.elf_path)[0]) == t0
            assert not os.path.exists(hostfs.boot_iso_path(folder, core.GAME))
    finally:
        d.close()


@pytest.mark.skipif(not (ISOS and os.environ.get("MUSICKIT_FULL_TEST")), reason="set MUSICKIT_FULL_TEST=1 and an ISO")
def test_folder_end_to_end(tmp_path):
    """ISO -> new folder (whole disc extracted) with an added song and new audio, validated; then the folder is
    edited in place; then saving from the ISO again puts every MusicKit file back to the ISO's list."""
    for src in ISOS:
        folder = str(tmp_path / ("game " + os.path.basename(src)))
        d = core.Disc(src)
        try:
            items = d.current_list()
            items[3].audio = FLAC
            items.append(core.NewSong(WAV, "Folder Song", "MusicKit", "HostFS"))
            rep = d.build_folder(items, folder, unlock_off=True)
            assert validate.validate_folder(src, folder, log=quiet)
            assert len(rep["written"]) == len(d.changed_paths)
            f = core.Disc(folder)
            assert f.count == d.count + 1 and f.songs[-1].title == "Folder Song" and f.unlocked
            items = f.current_list()
            items[0].title = "Edited In Folder"
            items.append(core.NewSong(FLAC, "Second Song", "MusicKit", ""))
            before = _mtimes(folder)
            rep = f.build_folder(items, folder)
            after = _mtimes(folder)
            # no extraction the second time: only the written files and the boot image change
            assert len([p for p, m in before.items() if after.get(p) != m]) <= len(rep["written"]) + 1
            assert validate.validate_folder(src, folder, log=quiet, hash_all=False)
            r = core.Disc(folder)
            assert r.count == d.count + 2 and r.songs[0].title == "Edited In Folder" and r.unlocked
            assert sum(not s.original for s in r.songs) == 3
            # other mods in the folder: a replaced file, an added file, a text; a file cut off by an interrupted copy
            img = hostfs.FolderImage(folder)
            unmanaged = sorted((k for k in d.img.entries if k not in d.changed_paths and d.img.entries[k].size > 4096),
                               key=lambda k: d.img.entries[k].size)
            replaced, cut = unmanaged[0], unmanaged[1]
            hostfs.write_files(folder, {replaced: b"replaced by another mod", "/MODS/CAR.DAT": b"added",
                                        cut: d.img.read_file(cut)[:1000]})
            text_path, has_mod = _text_mod(d, folder)
            mods = {p: hostfs.file_sha1(hostfs.FolderImage(folder).entries[p].host) for p in (replaced, "/MODS/CAR.DAT")}
            lines = []
            assert validate.validate_folder(src, folder, log=lines.append, hash_all=True)
            assert any(replaced.lstrip("/") in x and x.startswith("note:") for x in lines)
            assert any("MODS/CAR.DAT" in x and x.startswith("note:") for x in lines)
            f = core.Disc(folder)                                  # saved into itself: the mods stay
            items = f.current_list()
            items[2].title = "With Mods"
            f.build_folder(items, folder)
            img = hostfs.FolderImage(folder)
            assert {p: hostfs.file_sha1(img.entries[p].host) for p in mods} == mods
            assert has_mod(img.read_file(text_path))
            # from the ISO again, a rename only: the stream files go back to the ISO's
            items = d.current_list()
            items[1].title = "Only Renamed"
            rep = d.build_folder(items, folder)
            r = core.Disc(folder)
            assert r.count == d.count and not r.unlocked and r.songs[1].title == "Only Renamed"
            for p in core.RWS_FILES:
                e = d.img.entries[p]
                assert hostfs.same_file(src, e.lsn * iso.SECTOR, core.span(r.img, p)[0], 0, e.size), p
            assert validate.validate_folder(src, folder, log=quiet)
            img = hostfs.FolderImage(folder)                       # the mods stay, the cut-off file was repaired
            assert {p: hostfs.file_sha1(img.entries[p].host) for p in mods} == mods
            assert has_mod(img.read_file(text_path))
            assert img.entries[cut].size == d.img.entries[cut].size
            assert rep.get("kept_texts") and rep["boot_hint"] is None
        finally:
            d.close()
            shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.skipif(not (ISOS and os.environ.get("MUSICKIT_FULL_TEST")), reason="set MUSICKIT_FULL_TEST=1 and an ISO")
def test_gui_folder_flow(tmp_path, monkeypatch):
    """The window's logic: ISO -> "Folder for PCSX2 HostFS" -> save -> open the folder -> change -> save into it."""
    gui = pytest.importorskip("musickit.gui")
    for var in ("APPDATA", "XDG_CONFIG_HOME", "HOME"):
        monkeypatch.setenv(var, str(tmp_path / "appdata"))
    src = ISOS[0]
    folder = str(tmp_path / "game")
    log = gui.Log(os.path.join(gui.app_dir(), "gui.log"))
    g = gui.MusicKitGui(log, {})
    g.save_settings = lambda: None

    def wait():
        t = time.time()
        while g.busy():
            assert time.time() - t < 3600
            time.sleep(0.2)
        return g.job

    def errors():
        with log.lock:
            return [line for lv, _, line in log.lines if lv == "error"]

    try:
        g.drop([src])
        assert wait().ok and g.disc is not None and not g.disc.is_folder
        assert g.folder_path == core.default_folder(src) and not g.to_folder()
        g.set_form_file(WAV)
        g.form.update(title="GUI Folder Song", artist="MusicKit", album="")
        g.add_form_song()
        g.output_type = "folder"
        g.set_folder(str(tmp_path / "busy.txt"))
        (tmp_path / "busy.txt").write_text("x")
        assert "file" in g.save_problem()
        g.set_folder(folder)
        assert g.to_folder() and g.save_problem() is None
        info = g.folder_info()
        assert info["plan"]["extract_bytes"] > 0 and not info["plan"]["has_game"] and not info["problem"]
        g.build()
        assert wait().ok, errors()[-5:]
        assert g.last_report["folder"] == os.path.abspath(folder) and os.path.isfile(g.last_report["boot_iso"])
        assert validate.validate_folder(src, folder, log=quiet, hash_all=False)
        assert g.folder_info()["plan"]["has_game"]            # read again after the save
        g.open_saved_folder()
        assert wait().ok and g.disc.is_folder and g.to_folder() and g.disc.count == core.Disc(src).count + 1
        assert not g.new_items() and not g.can_save()
        g.rename(g.items[0], "title", "Renamed In GUI")
        assert g.save_problem() is None
        if os.name == "nt":                                    # the game running: refused with a clear message
            with open(core.span(g.disc.img, core.RWS_FILES[0])[0], "rb"):
                g.build()
                assert not wait().ok and any("PCSX2" in e and "failed" in e for e in errors())
        g.build()
        assert wait().ok, errors()[-5:]
        assert g.disc.is_folder and g.disc.songs[0].title == "Renamed In GUI" and not g.can_save()
    finally:
        if g.disc is not None:
            g.disc.close()
        if log.file:
            log.file.close()
        shutil.rmtree(folder, ignore_errors=True)


def _text_mod(d, folder):
    """Another mod changes one text (not a song name) in the folder's first string file. Returns (path, check)."""
    from musickit import strtable
    p = "/LANGUAGE/STRINGS/MAIN%s.BIN" % d.langs[0]
    img = hostfs.FolderImage(folder)
    t = strtable.StringTable(img.read_file(p))
    h = next(h for h in sorted(t.entries) if h not in core.SONG_TEXT_HASHES and t.entries[h])
    t.entries[h] = "Text Mod"
    hostfs.write_files(folder, {p: t.build()})
    return p, lambda data: strtable.StringTable(data).entries.get(h) == "Text Mod"


def _elf_mod(elf):
    """Another tool's change in the executable (one code word, PCSX2 CRC kept like the kits do). Returns (elf, address)."""
    e = elfpatch.Elf(elf)
    ph = e.phdrs()[0]
    va = ph[2] + 0x200
    e.w32(va, e.r32(va) ^ 0x00010000)
    return elfpatch._fix_crc(e.d, elfpatch.crc(elf), len(e.d) > e.content_end()), va


@needs_iso
def test_other_mods_in_the_folder(tmp_path):
    """Other mods in a folder: MusicKit's own earlier save may be replaced from the ISO; another mod's text is kept
    (from the ISO and from the folder); another tool's executable change is kept from the folder and makes saving
    the ISO there refuse; the validator only notes them."""
    d = core.Disc(ISOS[0])
    try:
        folder = _mini_folder(d, str(tmp_path / "game"))
        reps, _ = d.replacements(_changes(d.current_list()), normalize=False)
        hostfs.write_files(folder, reps)                       # as an earlier MusicKit save from the ISO
        assert d.check_folder(folder) is None
        assert d.foreign_elf_changes(hostfs.FolderImage(folder).read_file(d.elf_path)) is None
        # a text mod
        p, has_mod = _text_mod(d, folder)
        assert d.check_folder(folder) is None
        assert d.other_texts(hostfs.FolderImage(folder)) == [p.split("/")[-1] + " (1 text)"]
        reps, _ = d.replacements(d.current_list()[:-1], normalize=False)
        kept = d.keep_folder_texts(folder, reps)
        assert kept == [p.split("/")[-1] + " (1 text)"] and has_mod(reps[p])
        f = core.Disc(folder)
        reps_f, _ = f.replacements(_changes(f.current_list()), normalize=False)
        assert has_mod(reps_f.get(p) or f.img.read_file(p))      # saving the folder into itself keeps it
        # another tool's change in the executable
        elf = hostfs.FolderImage(folder).read_file(d.elf_path)
        mod, va = _elf_mod(elf)
        hostfs.write_files(folder, {d.elf_path: mod})
        problem = d.check_folder(folder)
        assert "did not make" in problem and "open this folder as the source" in problem and hex(va + 2) in problem
        f = core.Disc(folder)
        reps_f, _ = f.replacements(_changes(f.current_list()), normalize=False)
        assert elfpatch.crc(reps_f[d.elf_path]) == d.crc
        new = elfpatch.Elf(reps_f[d.elf_path])
        assert new.r32(va) == elfpatch.Elf(mod).r32(va)       # kept when saving the folder into itself
        # the validator: notes, never failures
        img = hostfs.FolderImage(folder)
        small = min((k for k in d.img.entries if k not in d.changed_paths and k not in img.entries
                     and d.img.entries[k].size < 200000), key=lambda k: d.img.entries[k].size)
        hostfs.write_files(folder, {small: d.img.read_file(small) + b"mod", "/MODS/README.TXT": b"x"})
        lines = []
        n = validate.folder_notes(d, d.path, d.img, hostfs.FolderImage(folder), lines.append)
        text = "\n".join(lines)
        assert n == 4 and "FAIL" not in text and small.lstrip("/") in text and "MODS/README.TXT" in text
        assert hex(va + 2) in text and "(1 text)" in text
    finally:
        d.close()


@needs_iso
def test_incomplete_folder(tmp_path, monkeypatch):
    d = core.Disc(ISOS[0])
    try:
        folder = _mini_folder(d, str(tmp_path / "game"))
    finally:
        d.close()
    monkeypatch.setattr(core, "REQUIRED", {"OVID": 5, "/IOP/GTFSHOST.IRX": 1})
    with pytest.raises(ValueError, match="incompletely extracted") as e:
        core.Disc(folder)
    assert "OVID (missing)" in str(e.value) and "IOP/GTFSHOST.IRX" in str(e.value)


@needs_iso
def test_folders_made_by_other_tools(tmp_path):
    """Lower-case names, the user's own ISO next to the files (Nahelam's guide), the game one level down, version
    numbers in names."""
    d = core.Disc(ISOS[0])
    try:
        folder = _mini_folder(d, str(tmp_path / "game"))
        for root, dirs, files in os.walk(folder, topdown=False):         # lower-case names, as some extractors do
            for n in files + dirs:
                os.rename(os.path.join(root, n), os.path.join(root, n.lower()))
        with open(os.path.join(folder, "My Game (Europe).iso"), "wb") as f:
            f.write(b"\0" * 4096)
        f = core.Disc(folder)
        items = f.current_list()
        items[0].title = "Lower Case Folder"
        rep = f.build_folder(items, folder, normalize=False)
        assert core.Disc(folder).songs[0].title == "Lower Case Folder"
        names = [n for _, ds, fs in os.walk(folder) for n in ds + fs]
        assert not [n for n in names if n != n.lower() and not n.endswith(".iso")]      # no second copies
        assert "My Game (Europe).iso" in rep["boot_hint"] and core.GAME + " (HostFS).iso" in rep["boot_hint"]
        assert "subfolder" in d.check_folder(str(tmp_path))
        with pytest.raises(ValueError, match="subfolder"):
            core.Disc(str(tmp_path))
        os.rename(os.path.join(folder, "system.cnf"), os.path.join(folder, "system.cnf;1"))
        with pytest.raises(ValueError, match="version number"):
            core.Disc(folder)
        assert "version number" in d.check_folder(folder)
    finally:
        d.close()


def _hole_opened_by_another_tool(elf):
    """The executable as ChainKit leaves it when it runs first: the .sndata hole opened like MusicKit does (segment 1
    extended over it), its code + data at the top of the hole, the original song table and code."""
    lay0 = elfpatch.Layout(elf)
    e = elfpatch.Elf(elfpatch.patch(elf, elfpatch.ORIGINAL_SONGS))
    orig = elfpatch.Elf(elf)
    for va, _, t, _ in lay0.sites():
        if t is not None:
            e.w32(va, orig.r32(va))
    o = e.file_offset(lay0.table_new)
    e.d[o:o + 12 * elfpatch.MAX_SONGS] = bytes(12 * elfpatch.MAX_SONGS)
    e.w32(lay0.playlist + 0x4C, lay0.table_old)
    e.w32(lay0.hole_end - 0x100, 0xC0DEC0DE)                 # the other tool's data at the top of the hole
    return elfpatch._fix_crc(e.d, elfpatch.crc(elf), True), lay0.hole_end - 0x100


@needs_iso
@pytest.mark.parametrize("src", ISOS)
def test_songs_added_after_chainkit(src):
    """MusicKit after ChainKit (ISO or folder): the hole is already open, MusicKit moves its table into its own part."""
    d = core.Disc(src)
    try:
        other, mark = _hole_opened_by_another_tool(d.elf)
        assert elfpatch.is_patched(other) and not elfpatch.has_song_table(other)
        assert elfpatch.read_song_count(other) == elfpatch.ORIGINAL_SONGS
        out = elfpatch.set_table(other, [7] * 45)
        assert elfpatch.has_song_table(out) and elfpatch.read_song_count(out) == 45 and elfpatch.crc(out) == d.crc
        assert elfpatch.Elf(out).r32(mark) == 0xC0DEC0DE
        skip = elfpatch.own_ranges(other) + elfpatch.own_ranges(out)
        assert hostfs.elf_changes(other, out, skip) == []          # nothing else changed
        # the same table as MusicKit first, then the other tool: only the other tool's word differs
        first = elfpatch.set_table(d.elf, [7] * 45)
        assert hostfs.elf_changes(first, out) == [(mark, mark + 4)]
        busy = elfpatch.Elf(other)
        busy.w32(elfpatch.Layout(other).table_new + 8, 1)          # MusicKit's space used by something else
        with pytest.raises(ValueError, match="used by another mod"):
            elfpatch.set_table(bytes(busy.d), [7] * 45)
    finally:
        d.close()
