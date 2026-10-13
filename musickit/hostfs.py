"""PCSX2 HostFS output: the game's files in a folder instead of a new disc image.

PCSX2's "Host Filesystem" lets a game read its files from the folder of the booted disc image ("host:"). The
Criterion games need a PCSX2 cheat for that (Nahelam's HostFS patches, see NAHELAM_URL): it makes the game load
its file system module for the host instead of the one for the disc. The kits write

- the game's files, extracted once from the user's ISO (only missing files are copied),
- the files the kit changes (each one written to a temporary file first and then renamed over the old one, so a
  file is never half-written), and
- a small boot image "<game> (HostFS).iso" in the same folder: SYSTEM.CNF + the (patched) game executable. PCSX2
  boots it like the original disc (same serial and game CRC, so per-game settings and cheats apply) and the
  folder of the image becomes the "host:" folder. It is rebuilt on every save.

The game asks for its files in mixed case ("Data/Global.txd") while the disc stores them in upper case: the
folder must be on a case-insensitive file system (Windows, macOS by default; on Linux e.g. an ext4 folder with
casefold enabled). This module itself finds files case-insensitively everywhere.

Other mods in the same folder: a kit never touches files it does not manage (another mod's replaced or added
files stay as they are). When a folder is opened as the source, a kit changes the files it manages on top of what
is there (other kits' and mods' changes stay). Saving from an ISO into a folder that already has the game rewrites
the kit's own parts from the ISO: a kit refuses when another tool changed the game executable in that folder (open
the folder as the source instead) and keeps other mods' texts in the text files.

This file is the same in every kit (MusicKit for Burnout 3 / Revenge / Dominator, ChainKit).
"""
import hashlib
import os
import re
import shutil
import struct
import time

SECTOR = 2048
TMP_SUFFIX = ".hostfs-tmp"
BOOT_SUFFIX = " (HostFS).iso"
NAHELAM_URL = "https://github.com/Nahelam/PCSX2-HostFS-Patches/tree/main/Criterion%20Games"


class HostFSError(Exception):
    pass


# ------------------------------------------------------------------------------------------- reading a folder
class FolderEntry:
    """A file of the game folder, with the attributes of iso.Entry the kits use (lsn 0: offsets are relative to
    the file itself)."""

    def __init__(self, path, host, size):
        self.path = path        # "/TRACKS/_EATRAX0.RWS" with the case found on disk
        self.host = host        # absolute path on this computer
        self.size = size
        self.lsn = 0
        self.udf_fe = None

    @property
    def sectors(self):
        return (self.size + SECTOR - 1) // SECTOR


def _key(path):
    p = path.replace("\\", "/")
    return ("/" + p.lstrip("/")).upper()


def _is_tmp(name):
    return name.endswith(TMP_SUFFIX)


_VERSION = re.compile(r";\d+$")      # "SYSTEM.CNF;1": the disc's file version number, kept by some extractors


def versioned_message(paths):
    return ("%d file name%s in this folder still end with the disc's version number (e.g. \"%s\"): the game asks "
            "for \"%s\" and cannot find them. Extract the ISO again with a tool that drops the \";1\" (7-Zip does), "
            "or rename the files, or let the kit fill an empty folder from your ISO."
            % (len(paths), "" if len(paths) == 1 else "s", paths[0].lstrip("/"), _VERSION.sub("", paths[0]).lstrip("/")))


def game_subfolder(folder, depth=2):
    """A subfolder of `folder` (up to `depth` levels down) that has the game (SYSTEM.CNF), else None."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for n in names:
        p = os.path.join(folder, n)
        if os.path.isdir(p):
            if is_game_folder(p):
                return p
            if depth > 1:
                sub = game_subfolder(p, depth - 1)
                if sub:
                    return sub
    return None


def missing_parts(img, required):
    """What an incompletely extracted game folder lacks: ["TRACKS (12 of about 253 files)", "IOP/GTFSHOST.IRX", ...].
    required = {"/FILE": 1 or "DIR": least number of files the disc has there}."""
    out = []
    for name, n in required.items():
        if name.startswith("/"):
            if _key(name) not in img.entries:
                out.append(name.lstrip("/"))
            continue
        have = sum(1 for k in img.entries if k.startswith("/" + name.upper() + "/"))
        if have < n:
            out.append("%s (%d of %d+ files)" % (name, have, n) if have else "%s (missing)" % name)
    return out          # (empty files are not a sign: some discs have them)


def incomplete_message(missing):
    return ("this folder looks incompletely extracted - missing: %s. Extract your ISO into it again, or open your ISO "
            "and save into this folder (missing files are copied from the ISO)." % ", ".join(missing[:6]) +
            (" ..." if len(missing) > 6 else ""))


def other_boot_files(folder, game):
    """Disc images at the top of `folder` other than the kit's boot image (e.g. the user's own ISO, as Nahelam's
    guide sets the folder up): PCSX2 starts the game executable from the image it boots, so those images still
    start the game without the kit's changes to the executable."""
    if not os.path.isdir(folder):
        return []
    mine = os.path.basename(boot_iso_path(folder, game)).lower()
    return sorted(n for n in os.listdir(folder) if n.lower().endswith(".iso") and n.lower() != mine
                  and os.path.isfile(os.path.join(folder, n)))


def boot_hint(folder, game):
    """One line for the save report when the folder holds other disc images (None otherwise)."""
    others = other_boot_files(folder, game)
    if not others:
        return None
    return ("Start \"%s\" in PCSX2: %s in this folder still start%s the game's original executable (the files in "
            "the folder are used either way, but the executable changes are only in \"%s\")."
            % (game + BOOT_SUFFIX, ", ".join("\"%s\"" % n for n in others), "s" if len(others) == 1 else "",
               game + BOOT_SUFFIX))


class FolderImage:
    """A game folder read like iso.IsoImage: entries {"/UPPER/PATH": FolderEntry}, read_file, open_file, span.
    Disc images at the top of the folder (the boot image, or the user's own ISO) are not game files."""

    is_folder = True
    udf = None

    def __init__(self, folder):
        if not os.path.isdir(folder):
            raise ValueError("folder not found: %s" % folder)
        self.path = os.path.abspath(folder)
        self.entries = {}
        versioned, twins = [], []
        for root, dirs, files in os.walk(self.path):
            dirs.sort()
            rel = os.path.relpath(root, self.path)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            for name in sorted(files):
                if _is_tmp(name) or (not rel and name.lower().endswith(".iso")):
                    continue
                path = "/" + (rel + "/" if rel else "") + name
                if _VERSION.search(name):
                    versioned.append(path)
                host = os.path.join(root, name)
                k = _key(path)
                if k in self.entries:
                    twins.append((self.entries[k].path, path))
                    continue
                self.entries[k] = FolderEntry(path, host, os.path.getsize(host))
        if "/SYSTEM.CNF" not in self.entries:
            sub = game_subfolder(self.path)
            if sub:
                raise ValueError("no SYSTEM.CNF in this folder, but in its subfolder \"%s\": choose that folder (the "
                                 "one with SYSTEM.CNF and the game's folders)" % os.path.relpath(sub, self.path))
            if versioned and any(_VERSION.sub("", p).upper() == "/SYSTEM.CNF" for p in versioned):
                raise ValueError(versioned_message(versioned))
            raise ValueError("no SYSTEM.CNF in this folder: not a game folder")
        if versioned:
            raise ValueError(versioned_message(versioned))
        if twins:
            raise ValueError("this folder has files whose names differ only in upper / lower case (%s): the game "
                             "would read only one of them. Keep one copy of each file (as on the disc), then try "
                             "again." % ", ".join("%s + %s" % (a.lstrip("/"), b.lstrip("/")) for a, b in twins[:3]))

    def read_file(self, path):
        with open(self.entries[_key(path)].host, "rb") as f:
            return f.read()

    def open_file(self, path):
        """(file object positioned at the file start, entry) - like iso.IsoImage.open_file (entry.lsn is 0)."""
        e = self.entries[_key(path)]
        return open(e.host, "rb"), e

    def span(self, path):
        """(file on this computer, offset of the file's first byte in it)."""
        e = self.entries[_key(path)]
        return e.host, 0

    def close(self):
        pass


def iso_span(img, path):
    """span() for an iso.IsoImage: (image file, byte offset of the file in it)."""
    e = img.entries[_key(path)]
    return img.path, e.lsn * SECTOR


def is_game_folder(folder):
    return os.path.isdir(folder) and any(_VERSION.sub("", n.upper()) == "SYSTEM.CNF" for n in os.listdir(folder))


def not_a_game_folder(folder):
    """Why a folder with other files cannot receive the game (for the kits' folder checks)."""
    sub = game_subfolder(folder)
    if sub:
        return ("the game's files are in the subfolder \"%s\" of this folder: choose that folder (the one with "
                "SYSTEM.CNF)" % os.path.relpath(sub, folder))
    return "this folder has other files: choose an empty folder or a folder with the game's files"


def boot_elf(cnf):
    """'/SLES_535.07' from the bytes of SYSTEM.CNF (None if there is no BOOT2 line)."""
    for line in cnf.decode("latin-1").splitlines():
        if line.replace(" ", "").upper().startswith("BOOT2="):
            return "/" + line.replace("/", "\\").split("\\")[-1].split(";")[0].strip().upper()
    return None


def serial_of(elf_path):
    """'SLES-53507' for '/SLES_535.07' (the serial PCSX2 shows and uses in file names)."""
    n = elf_path.strip("/").upper().replace(".", "")
    return n.replace("_", "-")


def host_path(folder, path, img=None):
    """Where `path` ("/DIR/FILE") lives in `folder`: the existing file (any case), else a new path that reuses
    existing directories whatever their case."""
    if img is not None and _key(path) in img.entries:
        return img.entries[_key(path)].host
    cur = os.path.abspath(folder)
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    for i, part in enumerate(parts):
        found = None
        if os.path.isdir(cur):
            for n in os.listdir(cur):
                if n.upper() == part.upper() and not _is_tmp(n):
                    found = n
                    break
        cur = os.path.join(cur, found or part)
    return cur


def case_sensitive(folder):
    """True if files in `folder` are case-sensitive (the game will not find its files there)."""
    probe = os.path.join(folder, "hostfs-case-probe" + TMP_SUFFIX)
    try:
        with open(probe, "wb"):
            pass
        return not os.path.exists(os.path.join(folder, "HOSTFS-CASE-PROBE" + TMP_SUFFIX.upper()))
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


CASE_WARNING = ("This folder is case-sensitive: the game asks for its files in other letter case (e.g. "
                "\"Data/Global.txd\" for DATA/GLOBAL.TXD) and PCSX2 passes the names on unchanged, so the game will "
                "not find them - renaming the files does not help. Use a case-insensitive folder: on Linux an ext4 "
                "folder with casefold (on a file system with the casefold feature: create an empty folder, run "
                "\"chattr +F <folder>\", then save into it), on macOS the default (case-insensitive) APFS.")


def case_warning(folder):
    """CASE_WARNING when `folder` (or the nearest existing folder above it, where it would be created) is
    case-sensitive, else None."""
    probe = os.path.abspath(folder)
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return CASE_WARNING if case_sensitive(probe) else None


# ------------------------------------------------------------------------------------------- writing
def locked(paths):
    """Files of `paths` another program holds open so that they cannot be replaced (Windows: PCSX2 keeps the
    game's open files and its disc image locked while the game runs). Elsewhere a running game keeps reading the
    old file and nothing can be detected: always []."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    out = []
    for p in paths:
        if not os.path.isfile(p):
            continue
        # ask for DELETE access while sharing everything: fails only if another handle refuses delete sharing
        h = k32.CreateFileW(p, 0x00010000, 0x7, None, 3, 0, None)
        if h == invalid or h is None:
            if ctypes.get_last_error() in (32, 33):     # sharing / lock violation
                out.append(p)
        else:
            k32.CloseHandle(h)
    return out


def check_unlocked(paths):
    busy = locked(paths)
    if busy:
        raise HostFSError("%s %s in use by another program - is the game running in PCSX2? Stop the game "
                          "(or close PCSX2), then save again. Nothing was changed."
                          % (", ".join(os.path.basename(p) for p in busy[:3]) + (" ..." if len(busy) > 3 else ""),
                             "is" if len(busy) == 1 else "are"))


def _write_data(f, data):
    if isinstance(data, (bytes, bytearray, memoryview)):
        f.write(data)
    elif hasattr(data, "write_to"):
        data.write_to(f)
    else:                                # (path, offset, size) of another file
        path, off, size = data
        with open(path, "rb") as src:
            src.seek(off)
            while size:
                b = src.read(min(size, 8 << 20))
                if not b:
                    raise IOError("unexpected end of " + path)
                f.write(b)
                size -= len(b)


def _size(data):
    if isinstance(data, tuple):
        return data[2]
    return len(data)


def write_files(folder, files, progress=None, extra_locked=()):
    """Write {"/DISC/PATH": bytes | iso.Parts | (path, offset, size)} into `folder`: every file goes to a
    temporary file first, then all of them are renamed over the old files. Refuses before writing anything when
    one of the files (or `extra_locked`) is in use. Returns {disc path: file on this computer}."""
    img = FolderImage(folder) if is_game_folder(folder) else None
    targets = {p: host_path(folder, p, img) for p in files}
    check_unlocked(list(targets.values()) + list(extra_locked))
    total = sum(_size(d) for d in files.values()) or 1
    done = 0
    tmps = {}
    try:
        for p, data in files.items():
            t = targets[p]
            os.makedirs(os.path.dirname(t), exist_ok=True)
            tmp = t + TMP_SUFFIX
            if progress:
                progress(done / total, "writing %s" % p.lstrip("/"))
            with open(tmp, "wb") as f:
                _write_data(f, data)
            tmps[p] = tmp
            done += _size(data)
        for p, tmp in tmps.items():
            os.replace(tmp, targets[p])
    finally:
        for tmp in tmps.values():
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    if progress:
        progress(1.0, "files written")
    return targets


def needed_bytes(img, folder):
    """Bytes extract() still has to copy into `folder` (files missing there)."""
    fimg = FolderImage(folder) if is_game_folder(folder) else None
    return sum(e.size for k, e in img.entries.items() if fimg is None or k not in fimg.entries)


def check_space(folder, nbytes):
    probe = folder
    while not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return
        probe = parent
    free = shutil.disk_usage(probe).free
    if nbytes + (64 << 20) > free:
        raise HostFSError("not enough free space in %s: %.1f GB needed, %.1f GB free" % (probe, nbytes / 1e9,
                                                                                       free / 1e9))


def _cut_off(img, e, fe):
    """True if the folder's file `fe` is the start of the disc file `e` only (an interrupted extraction)."""
    if fe.size >= e.size:
        return False
    return fe.size == 0 or same_file(img.path, e.lsn * SECTOR, fe.host, 0, fe.size)


def extract(img, folder, progress=None, skip=()):
    """Copy every file of the disc image `img` (iso.IsoImage) that is missing in `folder` (any case) into it.
    Files already there are kept as they are (another mod's files stay), except files cut off by an interrupted
    extraction (shorter than on the disc and equal to its start). `skip`: disc paths not to copy (written by the caller anyway).
    Returns the number of files copied."""
    os.makedirs(folder, exist_ok=True)
    fimg = FolderImage(folder) if is_game_folder(folder) else None
    skip = {_key(p) for p in skip}
    todo = [(k, e) for k, e in sorted(img.entries.items(), key=lambda kv: kv[1].lsn)
            if (fimg is None or k not in fimg.entries or _cut_off(img, e, fimg.entries[k])) and k not in skip]
    check_space(folder, sum(e.size for _, e in todo))
    total = sum(e.size for _, e in todo) or 1
    done = 0
    with open(img.path, "rb") as src:
        for k, e in todo:
            t = host_path(folder, e.path)
            os.makedirs(os.path.dirname(t), exist_ok=True)
            tmp = t + TMP_SUFFIX
            src.seek(e.lsn * SECTOR)
            left = e.size
            with open(tmp, "wb") as f:
                while left:
                    b = src.read(min(left, 8 << 20))
                    if not b:
                        raise IOError("unexpected end of the image at %s" % e.path)
                    f.write(b)
                    left -= len(b)
                    done += len(b)
                    if progress:
                        progress(done / total, "extracting %s" % e.path.lstrip("/"))
            os.replace(tmp, t)
    return len(todo)


def same_file(path_a, off_a, path_b, off_b, size):
    """True if `size` bytes at path_a+off_a equal those at path_b+off_b."""
    with open(path_a, "rb") as a, open(path_b, "rb") as b:
        a.seek(off_a)
        b.seek(off_b)
        left = size
        while left:
            n = min(left, 8 << 20)
            x, y = a.read(n), b.read(n)
            if x != y or not x:
                return False
            left -= n
    return True


def other_files(src_path, src_img, folder_img, managed=(), hash_all=True):
    """Files of a game folder that another mod changed or added (the kits never touch them):
    (changed: disc paths whose content differs from the disc image `src_img` at `src_path`, added: paths that are
    not on the disc). `managed`: disc paths the kit writes itself (not compared). hash_all=False compares sizes
    only."""
    managed = {_key(p) for p in managed}
    changed = []
    for k, e in sorted(src_img.entries.items()):
        o = folder_img.entries.get(k)
        if o is None or k in managed:
            continue
        if o.size != e.size or (hash_all and not same_file(src_path, e.lsn * SECTOR, o.host, 0, e.size)):
            changed.append(k)
    added = sorted(e.path for k, e in folder_img.entries.items() if k not in src_img.entries and k not in managed)
    return changed, added


def short_list(paths, n=6):
    paths = [p.lstrip("/") for p in paths]
    return ", ".join(paths[:n]) + (" and %d more" % (len(paths) - n) if len(paths) > n else "")


# ------------------------------------------------------------------------------------------- game executables
def _loaded(elf):
    """[(address, bytes)] the PS2 loads from an ELF file (the file part of every PT_LOAD segment)."""
    phoff = struct.unpack_from("<I", elf, 0x1C)[0]
    phnum = struct.unpack_from("<H", elf, 0x2C)[0]
    out = []
    for i in range(phnum):
        p_type, off, va, _, filesz = struct.unpack_from("<5I", elf, phoff + 32 * i)
        if p_type == 1 and filesz:
            out.append((va, bytes(elf[off:off + filesz])))
    return out


def elf_changes(elf_a, elf_b, skip=()):
    """Address ranges [(start, end)] where the programs loaded from the ELF files a and b differ, ignoring the
    ranges `skip` [(start, end)] (the changes a kit makes itself). An address only one of them loads counts as 0 in
    the other. Header and section table changes (not loaded) are not compared."""
    segs = (_loaded(elf_a), _loaded(elf_b))
    lo = min(va for s in segs for va, _ in s)
    hi = max(va + len(d) for s in segs for va, d in s)
    if hi - lo > (256 << 20):
        raise ValueError("unexpected program layout")
    mem = []
    for s in segs:
        m = bytearray(hi - lo)
        for va, d in s:
            m[va - lo:va - lo + len(d)] = d
        for a, b in skip:
            a, b = max(a, lo), min(b, hi)
            if a < b:
                m[a - lo:b - lo] = bytes(b - a)
        mem.append(m)
    a, b = mem
    out = []
    step = 4096
    for base in range(0, hi - lo, step):
        if a[base:base + step] == b[base:base + step]:
            continue
        for i in range(base, min(base + step, hi - lo)):
            if a[i] != b[i]:
                if out and out[-1][1] >= lo + i - 16:      # close changes count as one
                    out[-1][1] = lo + i + 1
                else:
                    out.append([lo + i, lo + i + 1])
    return [tuple(r) for r in out]


def describe_ranges(ranges, n=3):
    s = ", ".join("%#x" % a if b - a <= 4 else "%#x-%#x" % (a, b) for a, b in ranges[:n])
    return "%d place%s: %s%s" % (len(ranges), "" if len(ranges) == 1 else "s", s, " ..." if len(ranges) > n else "")


def file_sha1(path, off=0, size=None):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        f.seek(off)
        left = os.path.getsize(path) - off if size is None else size
        while left:
            b = f.read(min(left, 8 << 20))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------- boot image
def _both16(v):
    return struct.pack("<H", v) + struct.pack(">H", v)


def _both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


def _dirrec(name, lsn, size, is_dir, stamp):
    l = 33 + len(name)
    l += l % 2
    r = bytearray(l)
    r[0] = l
    r[2:10] = _both32(lsn)
    r[10:18] = _both32(size)
    r[18:25] = stamp
    r[25] = 2 if is_dir else 0
    r[28:32] = _both16(1)
    r[32] = len(name)
    r[33:33 + len(name)] = name
    return bytes(r)


def mini_iso(files, volume_id="PS2", when=None):
    """An ISO9660 image (2048-byte sectors) with `files` [(name, bytes)] in its root directory - enough for
    PCSX2 to read SYSTEM.CNF and boot the executable named there."""
    t = time.gmtime(when if when is not None else time.time())
    stamp = bytes([t.tm_year - 1900, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0])
    ldate = time.strftime("%Y%m%d%H%M%S00", t).encode() + b"\0"
    files = sorted(((n.upper().encode("ascii"), d) for n, d in files), key=lambda x: x[0])
    root_lsn, lsn, placed = 20, 21, []
    for name, data in files:
        placed.append((name, lsn, data))
        lsn += max(1, (len(data) + SECTOR - 1) // SECTOR)
    total = lsn
    recs = [_dirrec(b"\0", root_lsn, SECTOR, True, stamp), _dirrec(b"\1", root_lsn, SECTOR, True, stamp)]
    recs += [_dirrec(n + b";1", l, len(d), False, stamp) for n, l, d in placed]
    root = b"".join(recs)
    if len(root) > SECTOR:
        raise ValueError("too many files for a one-sector root directory")
    pt_l = bytes([1, 0]) + struct.pack("<I", root_lsn) + struct.pack("<H", 1) + b"\0\0"
    pt_m = bytes([1, 0]) + struct.pack(">I", root_lsn) + struct.pack(">H", 1) + b"\0\0"
    pvd = bytearray(SECTOR)
    pvd[0], pvd[1:6], pvd[6] = 1, b"CD001", 1
    pvd[8:40] = b"PLAYSTATION".ljust(32)
    pvd[40:72] = volume_id.upper().encode("ascii", "replace")[:32].ljust(32)
    pvd[80:88] = _both32(total)
    pvd[120:124], pvd[124:128], pvd[128:132] = _both16(1), _both16(1), _both16(SECTOR)
    pvd[132:140] = _both32(len(pt_l))
    struct.pack_into("<I", pvd, 140, 18)
    struct.pack_into(">I", pvd, 148, 19)
    pvd[156:190] = _dirrec(b"\0", root_lsn, SECTOR, True, stamp)
    pvd[190:813] = b" " * (813 - 190)
    pvd[813:830] = ldate
    pvd[830:847] = ldate
    pvd[847:864] = b"0" * 16 + b"\0"
    pvd[864:881] = b"0" * 16 + b"\0"
    pvd[881] = 1
    term = bytearray(SECTOR)
    term[0], term[1:6], term[6] = 255, b"CD001", 1
    out = bytearray(16 * SECTOR) + pvd + term + pt_l.ljust(SECTOR, b"\0") + pt_m.ljust(SECTOR, b"\0")
    out += root.ljust(SECTOR, b"\0")
    for name, l, data in placed:
        out += data + b"\0" * (-len(data) % SECTOR)
        if not data:
            out += b"\0" * SECTOR
    assert len(out) == total * SECTOR
    return bytes(out)


def boot_iso_path(folder, game):
    return os.path.join(folder, game + BOOT_SUFFIX)


def write_boot_iso(folder, game):
    """(Re)write "<game> (HostFS).iso" in `folder` from its SYSTEM.CNF and boot executable. Returns its path."""
    img = FolderImage(folder)
    cnf = img.read_file("/SYSTEM.CNF")
    elf_path = boot_elf(cnf)
    if not elf_path or _key(elf_path) not in img.entries:
        raise HostFSError("the boot executable named in SYSTEM.CNF is missing in %s" % folder)
    elf = img.read_file(elf_path)
    data = mini_iso([("SYSTEM.CNF", cnf), (elf_path.strip("/"), elf)], elf_path.strip("/").replace(".", ""))
    out = boot_iso_path(folder, game)
    check_unlocked([out])
    tmp = out + TMP_SUFFIX
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out)
    return out


# ------------------------------------------------------------------------------------------- instructions
def pnach_name(elf_path, crc):
    return "%s_%08X_hostfs.pnach" % (serial_of(elf_path), crc)


def instructions(game, nahelam_dir, elf_path, crc, folder=None):
    """How to play the folder in PCSX2 (numbered steps, plain text)."""
    boot = game + BOOT_SUFFIX
    return [
        "1. Download %s from Nahelam's PCSX2 HostFS patches (%s, folder \"%s\") and put it into PCSX2's "
        "\"cheats\" folder (PCSX2: Tools > Open Data Directory)." % (pnach_name(elf_path, crc), NAHELAM_URL,
                                                                    nahelam_dir),
        "2. Add the folder%s to PCSX2's game list (Settings > Game List > Add, no recursive scan): it shows "
        "\"%s\"." % (" " + folder if folder else "", boot),
        "3. Right-click it > Properties: Emulation > \"Enable Host Filesystem\" on; Cheats > \"Enable Cheats\" on and "
        "tick \"HostFS\".",
        "4. Start \"%s\". After changing files in the folder, restart the game (the boot image is rewritten on "
        "every save, so do not keep the game running while you save)." % boot,
    ]
