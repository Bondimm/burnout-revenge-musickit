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
            t = time.time()
            rep = d.build_folder(items, folder, unlock_off=True)
            assert validate.validate_folder(src, folder, log=quiet)
            assert len(rep["written"]) == len(d.changed_paths)
            first = time.time() - t
            f = core.Disc(folder)
            assert f.count == d.count + 1 and f.songs[-1].title == "Folder Song" and f.unlocked
            items = f.current_list()
            items[0].title = "Edited In Folder"
            items.append(core.NewSong(FLAC, "Second Song", "MusicKit", ""))
            t = time.time()
            rep = f.build_folder(items, folder)
            assert time.time() - t < first           # no extraction the second time
            assert validate.validate_folder(src, folder, log=quiet, hash_all=False)
            r = core.Disc(folder)
            assert r.count == d.count + 2 and r.songs[0].title == "Edited In Folder" and r.unlocked
            assert sum(not s.original for s in r.songs) == 3
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
