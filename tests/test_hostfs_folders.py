"""HostFS game folders made by other tools, other mods in them, and letter case - with small made-up folders (no game
data, runs everywhere). On a case-sensitive file system (Linux) the case tests check what the kit does where the
game itself would not find its files.

This file is the same in every kit (MusicKit for Burnout 3 / Revenge / Dominator, ChainKit).
"""
import importlib
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
KIT = "chainkit" if os.path.isdir(os.path.join(ROOT, "chainkit")) else "musickit"
hostfs = importlib.import_module(KIT + ".hostfs")
iso = importlib.import_module(KIT + ".iso")

CNF = b"BOOT2 = cdrom0:\\SLES_123.45;1\r\nVER = 1.00\r\nVMODE = PAL\r\n"


def _make(folder, files):
    """files = {"rel/path": bytes}; returns folder."""
    for rel, data in files.items():
        p = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    return str(folder)


def _game(folder, case=str.upper, extra=None):
    names = {"SYSTEM.CNF": CNF, "SLES_123.45": b"\x7fELF" + bytes(60), "DATA/GLOBAL.TXD": b"T" * 100,
             "TRACKS/_EATRAX0.RWS": b"R" * 300, "IOP/GTFSHOST.IRX": b"I" * 10}
    files = {"/".join(case(p) for p in k.split("/")): v for k, v in names.items()}
    files.update(extra or {})
    return _make(folder, files)


def _listing(folder):
    return sorted(os.path.relpath(os.path.join(r, n), folder).replace(os.sep, "/") for r, _, ns in os.walk(folder)
                  for n in ns)


# ------------------------------------------------------------------------------------------- letter case
@pytest.mark.parametrize("case", [str.upper, str.lower, lambda s: s[:1].upper() + s[1:].lower()],
                         ids=["upper", "lower", "mixed"])
def test_any_letter_case_is_read_and_written_in_place(tmp_path, case):
    folder = _game(tmp_path / "game", case)
    img = hostfs.FolderImage(folder)
    assert img.read_file("/DATA/GLOBAL.TXD") == b"T" * 100 and img.read_file("/data/global.txd") == b"T" * 100
    assert hostfs.boot_elf(img.read_file("/SYSTEM.CNF")) == "/SLES_123.45"
    before = _listing(folder)
    hostfs.write_files(folder, {"/DATA/GLOBAL.TXD": b"new", "/TRACKS/_EATRAX0.RWS": b"rws", "/DATA/ADDED.BIN": b"a"})
    after = _listing(folder)
    # the existing files were replaced under their own names, never a second copy in other letter case
    assert [p for p in after if p.upper() != "DATA/ADDED.BIN"] == before
    added = [p for p in after if p.upper() == "DATA/ADDED.BIN"]
    assert len(added) == 1 and added[0].split("/")[0] == case("DATA")      # into the existing folder
    img = hostfs.FolderImage(folder)
    assert img.read_file("/DATA/GLOBAL.TXD") == b"new" and img.read_file("/TRACKS/_EATRAX0.RWS") == b"rws"
    boot = hostfs.write_boot_iso(folder, "Test Game")
    b = iso.IsoImage(boot)
    try:
        assert b.read_file("/SLES_123.45") == img.read_file("/SLES_123.45")
    finally:
        b.f.close()
    assert _listing(folder) == sorted(after + ["Test Game (HostFS).iso"])


def test_case_warning_where_the_game_would_not_find_its_files(tmp_path):
    sensitive = hostfs.case_sensitive(str(tmp_path))
    warn = hostfs.case_warning(str(tmp_path / "not" / "there" / "yet"))      # the nearest existing folder decides
    assert (warn is not None) == sensitive
    if sensitive:                       # Linux: PCSX2 passes the game's mixed-case names on unchanged
        assert "case-sensitive" in warn and "casefold" in warn


@pytest.mark.skipif(os.name == "nt" or sys.platform == "darwin",
                    reason="needs a case-sensitive file system (Linux)")
def test_names_differing_only_in_case_are_refused(tmp_path):
    if not hostfs.case_sensitive(str(tmp_path)):
        pytest.skip("this temporary folder is case-insensitive")
    folder = _game(tmp_path / "game", extra={"data/global.txd": b"other"})
    with pytest.raises(ValueError, match="differ only in upper / lower case"):
        hostfs.FolderImage(folder)
    folder2 = _game(tmp_path / "game2", str.lower)
    assert hostfs.case_warning(folder2) is not None and hostfs.is_game_folder(folder2)
    hostfs.write_files(folder2, {"/DATA/GLOBAL.TXD": b"x"})            # still no second copy in upper case
    assert "DATA/GLOBAL.TXD" not in _listing(folder2) and "data/global.txd" in _listing(folder2)


# ------------------------------------------------------------------------------------------- other extractors
def test_iso_version_numbers_in_names_are_explained(tmp_path):
    folder = _make(tmp_path / "game", {"SYSTEM.CNF;1": CNF, "SLES_123.45;1": b"\x7fELF", "DATA/GLOBAL.TXD;1": b"x"})
    assert hostfs.is_game_folder(folder)
    with pytest.raises(ValueError, match="version number") as e:
        hostfs.FolderImage(folder)
    assert "7-Zip" in str(e.value)
    folder = _game(tmp_path / "game2", extra={"DATA/OTHER.BIN;1": b"x"})
    with pytest.raises(ValueError, match="version number"):
        hostfs.FolderImage(folder)


def test_game_one_level_down(tmp_path):
    _game(tmp_path / "parent" / "Burnout")
    parent = str(tmp_path / "parent")
    with pytest.raises(ValueError, match="subfolder \"Burnout\""):
        hostfs.FolderImage(parent)
    assert "subfolder" in hostfs.not_a_game_folder(parent) and not hostfs.is_game_folder(parent)
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "notes.txt").write_text("x")
    assert "other files" in hostfs.not_a_game_folder(str(tmp_path / "plain"))


def test_incomplete_extraction(tmp_path):
    folder = _game(tmp_path / "game", extra={"TRACKS/EMPTY.RWS": b""})
    img = hostfs.FolderImage(folder)
    required = {"DATA": 1, "TRACKS": 3, "SOUND": 1, "/IOP/GTFSHOST.IRX": 1, "/IOP/MISSING.IRX": 1}
    missing = hostfs.missing_parts(img, required)
    assert "TRACKS (2 of 3+ files)" in missing and "SOUND (missing)" in missing and "IOP/MISSING.IRX" in missing
    assert not any(m.startswith("DATA") for m in missing)          # (an empty file is not a sign: discs have some)
    assert "incompletely extracted" in hostfs.incomplete_message(missing)
    assert hostfs.missing_parts(img, {"DATA": 1, "TRACKS": 1}) == []


def test_other_disc_images_and_files_next_to_the_game(tmp_path):
    """Nahelam's guide keeps the user's own ISO in the game folder (PCSX2 boots it, the files come from the
    folder); other tools add an ELF, notes or texture folders. They are not game files and stay untouched."""
    folder = _game(tmp_path / "game", extra={"Burnout (Europe).iso": b"ISO" * 100, "readme.txt": b"r",
                                              "textures/SLES-12345/replacements/a.png": b"png"})
    img = hostfs.FolderImage(folder)
    assert "/BURNOUT (EUROPE).ISO" not in img.entries and "/README.TXT" in img.entries
    assert hostfs.other_boot_files(folder, "Test Game") == ["Burnout (Europe).iso"]
    hostfs.write_boot_iso(folder, "Test Game")
    assert hostfs.other_boot_files(folder, "Test Game") == ["Burnout (Europe).iso"]
    hint = hostfs.boot_hint(folder, "Test Game")
    assert "Test Game (HostFS).iso" in hint and "Burnout (Europe).iso" in hint
    os.remove(os.path.join(folder, "Burnout (Europe).iso"))
    assert hostfs.boot_hint(folder, "Test Game") is None
    with open(os.path.join(folder, "readme.txt"), "rb") as f:
        assert f.read() == b"r"


# ------------------------------------------------------------------------------------------- other mods
def _disc_image(path, files):
    with open(path, "wb") as f:
        f.write(hostfs.mini_iso(sorted(files.items())))
    return iso.IsoImage(str(path))


def test_extract_keeps_mods_and_repairs_cut_off_files(tmp_path):
    files = {"SYSTEM.CNF": CNF, "SLES_123.45": b"\x7fELF" + bytes(60), "A.BIN": b"A" * 5000, "B.BIN": b"B" * 5000,
             "C.BIN": b"C" * 5000, "D.BIN": b"D" * 70}
    img = _disc_image(tmp_path / "disc.iso", files)
    try:
        folder = _make(tmp_path / "game", {"SYSTEM.CNF": CNF, "A.BIN": b"A" * 1000,    # cut off: copied again
                                           "B.BIN": b"modded", "c.bin": b"",         # a mod / an empty file
                                           "MOD/EXTRA.DAT": b"extra"})
        n = hostfs.extract(img, folder)
        assert n == 4                                   # A (cut off), C (empty), D and the ELF (missing)
        f = hostfs.FolderImage(folder)
        assert f.read_file("/A.BIN") == b"A" * 5000 and f.read_file("/C.BIN") == b"C" * 5000
        assert f.read_file("/B.BIN") == b"modded" and f.read_file("/MOD/EXTRA.DAT") == b"extra"
        assert "c.bin" in _listing(folder) and "C.BIN" not in _listing(folder)       # same name as before
        changed, added = hostfs.other_files(img.path, img, f, managed=["/D.BIN"])
        assert changed == ["/B.BIN"] and added == ["/MOD/EXTRA.DAT"]
        assert "B.BIN" in hostfs.short_list(changed)
    finally:
        img.f.close()


def _elf(segments):
    """A tiny ELF file with PT_LOAD segments [(address, bytes)]."""
    phoff, n = 0x34, len(segments)
    off = phoff + 32 * n
    head = bytearray(b"\x7fELF" + bytes(0x30))
    struct.pack_into("<I", head, 0x1C, phoff)
    struct.pack_into("<H", head, 0x2C, n)
    ph, body = b"", b""
    for va, data in segments:
        ph += struct.pack("<8I", 1, off + len(body), va, va, len(data), len(data), 7, 16)
        body += data
    return bytes(head) + ph + body


def test_executable_changes_outside_the_kits_own(tmp_path):
    a = _elf([(0x100000, bytes(range(256)) * 16), (0x200000, b"\0" * 64)])
    assert hostfs.elf_changes(a, a) == []
    b = bytearray(a)
    b[0x34 + 64 + 0x10] ^= 0xFF                       # one byte of the first segment
    b[0x34 + 64 + 0x800] ^= 0xFF
    assert hostfs.elf_changes(a, bytes(b)) == [(0x100010, 0x100011), (0x100800, 0x100801)]
    assert hostfs.elf_changes(a, bytes(b), skip=[(0x100000, 0x100100)]) == [(0x100800, 0x100801)]
    c = _elf([(0x100000, bytes(range(256)) * 16), (0x200000, b"\0" * 64), (0x300000, b"\1\2")])   # a new segment
    assert hostfs.elf_changes(a, c) == [(0x300000, 0x300002)]
    assert hostfs.elf_changes(a, c, skip=[(0x300000, 0x300010)]) == []
    assert "2 places" in hostfs.describe_ranges(hostfs.elf_changes(a, bytes(b)))


# ------------------------------------------------------------------------------------------- README
def test_readme_credits_nahelam():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        text = f.read()
    assert "https://github.com/Nahelam/PCSX2-HostFS-Patches" in text
    assert hostfs.NAHELAM_URL in text and "Nahelam" in text
    assert "_hostfs.pnach" in text
