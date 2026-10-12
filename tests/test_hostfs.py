"""PCSX2 HostFS folder output (hostfs.py) without a disc image: boot image, folder reading with any letter case,
safe writing, extraction, refusing files in use. Runs on every OS in CI. (Same file in every kit.)
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    from musickit import hostfs, iso  # noqa: E402
except ImportError:
    from chainkit import hostfs, iso  # noqa: E402

CNF = b"BOOT2 = cdrom0:\\SLES_535.07;1\r\nVER = 1.01\r\nVMODE = PAL\r\n"
ELF = b"\x7fELF" + bytes(range(256)) * 20


def _game(folder, extra=()):
    """A small fake game folder: SYSTEM.CNF, the executable and a few files in mixed-case folders."""
    files = {"SYSTEM.CNF": CNF, "SLES_535.07": ELF, "TRACKS/_EATRAX0.RWS": b"A" * 5000,
             "Language/Strings/MAINUK.BIN": b"strings", "DATA/GLOBAL.TXD": b"txd"}
    files.update(extra)
    for rel, data in files.items():
        p = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    return files


def test_names_and_instructions():
    assert hostfs.boot_elf(CNF) == "/SLES_535.07"
    assert hostfs.boot_elf(b"BOOT2=cdrom0:\\SLUS_212.42;1\n") == "/SLUS_212.42"
    assert hostfs.boot_elf(b"VER = 1\n") is None
    assert hostfs.serial_of("/SLES_535.07") == "SLES-53507" and hostfs.serial_of("SLUS_215.96") == "SLUS-21596"
    assert hostfs.pnach_name("/SLES_535.07", 0x7E83CC5B) == "SLES-53507_7E83CC5B_hostfs.pnach"
    steps = hostfs.instructions("Burnout Revenge", "Burnout Revenge", "/SLES_535.07", 0x7E83CC5B)
    text = "\n".join(steps)
    assert len(steps) == 4 and "SLES-53507_7E83CC5B_hostfs.pnach" in text and hostfs.NAHELAM_URL in text
    assert "Burnout Revenge (HostFS).iso" in text and "Host Filesystem" in text and "HostFS" in text


def test_mini_iso_readable(tmp_path):
    data = hostfs.mini_iso([("SYSTEM.CNF", CNF), ("SLES_535.07", ELF), ("EMPTY.BIN", b"")], "SLES_53507")
    assert len(data) % hostfs.SECTOR == 0
    p = tmp_path / "boot.iso"
    p.write_bytes(data)
    img = iso.IsoImage(str(p))
    try:
        assert sorted(img.entries) == ["/EMPTY.BIN", "/SLES_535.07", "/SYSTEM.CNF"]
        assert img.read_file("/SYSTEM.CNF") == CNF and img.read_file("/SLES_535.07") == ELF
        assert img.read_file("/EMPTY.BIN") == b"" and img.udf is None
        assert img.volume_sectors * hostfs.SECTOR == len(data)
        assert img.pvd[40:50] == b"SLES_53507"
    finally:
        img.f.close()


def test_folder_image_any_case(tmp_path):
    folder = str(tmp_path / "game")
    files = _game(folder, {"Burnout Revenge (HostFS).iso": b"boot", "notes" + hostfs.TMP_SUFFIX: b"x"})
    assert hostfs.is_game_folder(folder) and not hostfs.is_game_folder(str(tmp_path))
    img = hostfs.FolderImage(folder)
    assert "/LANGUAGE/STRINGS/MAINUK.BIN" in img.entries and "/SLES_535.07" in img.entries
    assert not any(k.endswith(".ISO") or k.endswith(hostfs.TMP_SUFFIX.upper()) for k in img.entries)
    assert img.read_file("/language/strings/mainuk.bin") == b"strings"
    e = img.entries["/TRACKS/_EATRAX0.RWS"]
    assert e.path == "/TRACKS/_EATRAX0.RWS" and e.size == 5000 and e.lsn == 0
    f, e2 = img.open_file("/tracks/_eatrax0.rws")
    with f:
        f.seek(e2.lsn * iso.SECTOR + 10)
        assert f.read(3) == b"AAA"
    assert img.span("/TRACKS/_EATRAX0.RWS") == (e.host, 0)
    # a new file reuses the existing folders whatever their case
    new = hostfs.host_path(folder, "/LANGUAGE/STRINGS/MAINFR.BIN", img)
    assert new == os.path.join(folder, "Language", "Strings", "MAINFR.BIN")
    assert hostfs.host_path(folder, "/language/strings/MAINUK.BIN", img) == img.entries["/LANGUAGE/STRINGS/MAINUK.BIN"].host
    with pytest.raises(ValueError):
        hostfs.FolderImage(str(tmp_path / "missing"))
    os.makedirs(str(tmp_path / "empty"))
    with pytest.raises(ValueError):
        hostfs.FolderImage(str(tmp_path / "empty"))
    assert len(files) == 7


def test_write_files_atomic(tmp_path):
    folder = str(tmp_path / "game")
    _game(folder)
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(256)) * 4)

    class Parts:      # like iso.Parts
        def __init__(self, parts):
            self.parts = parts

        def __len__(self):
            return sum(len(p) for p in self.parts)

        def write_to(self, out):
            for p in self.parts:
                out.write(p)

    seen = []
    out = hostfs.write_files(folder, {"/TRACKS/_EATRAX0.RWS": Parts([b"new", b"rws"]),
                                      "/language/strings/MAINFR.BIN": b"fr",
                                      "/NEW/DIR/FILE.BIN": (str(src), 256, 10)},
                             progress=lambda f, m: seen.append(f))
    img = hostfs.FolderImage(folder)
    assert img.read_file("/TRACKS/_EATRAX0.RWS") == b"newrws"
    assert img.read_file("/LANGUAGE/STRINGS/MAINFR.BIN") == b"fr"
    assert os.path.dirname(img.entries["/LANGUAGE/STRINGS/MAINFR.BIN"].host).endswith(os.path.join("Language", "Strings"))
    assert img.read_file("/NEW/DIR/FILE.BIN") == bytes(range(10))
    assert set(out) == {"/TRACKS/_EATRAX0.RWS", "/language/strings/MAINFR.BIN", "/NEW/DIR/FILE.BIN"}
    assert seen[-1] == 1.0
    for root, dirs, files in os.walk(folder):
        assert not any(n.endswith(hostfs.TMP_SUFFIX) for n in files)


def test_write_refused_while_in_use(tmp_path):
    folder = str(tmp_path / "game")
    _game(folder)
    rws = os.path.join(folder, "TRACKS", "_EATRAX0.RWS")
    with open(rws, "rb"):           # like PCSX2 streaming a song (Windows: no delete sharing)
        if os.name == "nt":
            assert hostfs.locked([rws, os.path.join(folder, "SYSTEM.CNF")]) == [rws]
            with pytest.raises(hostfs.HostFSError) as exc:
                hostfs.write_files(folder, {"/SYSTEM.CNF": b"changed", "/TRACKS/_EATRAX0.RWS": b"x"})
            assert "PCSX2" in str(exc.value) and "_EATRAX0.RWS" in str(exc.value)
            assert open(os.path.join(folder, "SYSTEM.CNF"), "rb").read() == CNF   # nothing was written
        else:
            assert hostfs.locked([rws]) == []      # not detectable: the running game keeps the old file
    assert hostfs.locked([rws]) == []


def test_extract_and_boot_image(tmp_path):
    files = [("SYSTEM.CNF", CNF), ("SLES_535.07", ELF), ("README.TXT", b"r" * 3000)]
    src = tmp_path / "disc.iso"
    src.write_bytes(hostfs.mini_iso(files))
    img = iso.IsoImage(str(src))
    folder = str(tmp_path / "out" / "game")
    try:
        assert hostfs.needed_bytes(img, folder) == sum(len(d) for _, d in files)
        assert hostfs.extract(img, folder, skip=["/readme.txt"]) == 2
        assert hostfs.is_game_folder(folder) and not os.path.exists(os.path.join(folder, "README.TXT"))
        with open(os.path.join(folder, "SLES_535.07"), "wb") as f:      # a changed file is kept
            f.write(b"\x7fELF patched")
        assert hostfs.needed_bytes(img, folder) == 3000
        assert hostfs.extract(img, folder) == 1
        assert hostfs.extract(img, folder) == 0
        assert open(os.path.join(folder, "README.TXT"), "rb").read() == b"r" * 3000
        assert open(os.path.join(folder, "SLES_535.07"), "rb").read() == b"\x7fELF patched"
        boot = hostfs.write_boot_iso(folder, "Burnout Revenge")
        assert boot == os.path.join(folder, "Burnout Revenge (HostFS).iso")
        b = iso.IsoImage(boot)
        try:
            assert sorted(b.entries) == ["/SLES_535.07", "/SYSTEM.CNF"]
            assert b.read_file("/SLES_535.07") == b"\x7fELF patched"
        finally:
            b.f.close()
        assert "/BURNOUT REVENGE (HOSTFS).ISO" not in hostfs.FolderImage(folder).entries
        assert hostfs.same_file(str(src), 0, str(src), 0, 4096)
        assert hostfs.file_sha1(boot) == hostfs.file_sha1(boot, 0, os.path.getsize(boot))
    finally:
        img.f.close()


def test_boot_image_needs_executable(tmp_path):
    folder = str(tmp_path / "game")
    os.makedirs(folder)
    with open(os.path.join(folder, "SYSTEM.CNF"), "wb") as f:
        f.write(CNF)
    with pytest.raises(hostfs.HostFSError):
        hostfs.write_boot_iso(folder, "Burnout Revenge")


def test_space_check(tmp_path):
    hostfs.check_space(str(tmp_path / "not" / "yet"), 1)
    with pytest.raises(hostfs.HostFSError):
        hostfs.check_space(str(tmp_path), 1 << 62)


def test_case_sensitivity_probe(tmp_path):
    cs = hostfs.case_sensitive(str(tmp_path))
    if os.name == "nt" or sys.platform == "darwin":
        assert cs is False          # Windows / default macOS file systems
    assert not os.listdir(str(tmp_path))     # the probe file is removed again
