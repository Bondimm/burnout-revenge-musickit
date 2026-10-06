"""Validate a MusicKit output ISO against its source: file systems parse, untouched files are byte-identical
and keep their LSNs, the song list/names/streams of the new image are consistent."""
import hashlib
import struct

from . import core, iso

from . import elfpatch

# PCSX2 patches (resources/patches.zip) for the supported versions, by game CRC: written EE addresses.
PNACH = {
    0x7E83CC5B: {"Widescreen 16:9 (Dread and Arapapa)": [0x3A64A8, 0x1BFEB10]},
    0xD224D348: {
        "Widescreen 16:9/21:9 (SuperType1/remco)": [0x1398C0, 0x1398C8, 0x16BCA8, 0x16BCAC, 0x1C02438, 0x1C02448,
                                                     0x1C02398, 0x1C02410, 0x1BFE698, 0x10DA78, 0x167844, 0x1C02108,
                                                     0x32677C],
        "Disable Motion Blur / Bloom": [0x1BFE7BA, 0x1BFE7B8],
        "60 FPS Menus / Crashes": [0x1125F4, 0x1125EC, 0x104B9C],
        "Progressive Scan / MPH to KPH / extras (Nehalem)": [0x19778C, 0x1765E8, 0x1767DC, 0x210FA8, 0x2B5334],
    },
}


def _hash_file(img, e):
    h = hashlib.sha1()
    img.f.seek(e.lsn * iso.SECTOR)
    left = e.size
    while left:
        b = img.f.read(min(left, 8 << 20))
        h.update(b)
        left -= len(b)
    return h.hexdigest()


def validate(src_path, out_path, log=print, hash_all=True):
    ok = True
    src = iso.IsoImage(src_path)
    out = iso.IsoImage(out_path)
    s0 = core.Disc(src_path)
    CHANGED = s0.changed_paths
    if not out.udf:
        log("FAIL: UDF bridge not readable: %s" % getattr(out, "udf_error", "?"))
        ok = False
    # pycdlib cross-check (independent ISO9660/UDF parser)
    try:
        import pycdlib
        p = pycdlib.PyCdlib()
        p.open(out_path)
        n = 0
        for root, dirs, files in p.walk(iso_path="/"):
            n += len(files)
        nu = 0
        if p.has_udf():
            for root, dirs, files in p.walk(udf_path="/"):
                nu += len(files)
        p.close()
        log("pycdlib: %d ISO9660 files, %d UDF files" % (n, nu))
        if n != len(src.entries) or (nu and nu != len(src.entries)):
            log("FAIL: file count differs from source (%d)" % len(src.entries))
            ok = False
    except ImportError:
        log("pycdlib not installed, skipped")
    except Exception as exc:
        log("FAIL: pycdlib could not parse the image: %s" % exc)
        ok = False
    # UDF and ISO9660 agree
    for k, e in out.entries.items():
        if e.udf_fe is None:
            continue
        d = out.read(e.udf_fe)
        ext = out._fe_extents(d)
        size = struct.unpack_from("<Q", d, 56)[0]
        if size != e.size or ext[0][0] + out.part_start != e.lsn:
            log("FAIL: UDF/ISO9660 mismatch for %s" % k)
            ok = False
        if bytes(iso.udf_fix_tag(d)) != d:
            log("FAIL: bad UDF descriptor CRC for %s" % k)
            ok = False
    same = moved = 0
    for k, e in src.entries.items():
        o = out.entries.get(k)
        if o is None:
            log("FAIL: %s missing" % k)
            ok = False
            continue
        if k in CHANGED:
            continue
        if o.lsn != e.lsn or o.size != e.size:
            log("FAIL: %s moved (%d -> %d)" % (k, e.lsn, o.lsn))
            moved += 1
            ok = False
        elif hash_all and _hash_file(src, e) != _hash_file(out, o):
            log("FAIL: %s content differs" % k)
            ok = False
        else:
            same += 1
    log("untouched files identical at original LSNs: %d/%d%s" % (same, len(src.entries) - len(CHANGED),
                                                                 "" if hash_all else " (LSN/size only)"))
    for k in sorted(CHANGED):
        log("changed %-34s lsn %8d -> %8d  size %10d -> %10d" % (k, src.entries[k].lsn, out.entries[k].lsn,
                                                                 src.entries[k].size, out.entries[k].size))
    d = core.Disc(out_path)
    log("version: %s" % d.region)
    log("songs: %d -> %d" % (s0.count, d.count))
    log("switch any song OFF: %s" % ("yes" if d.unlocked else "no (original behaviour)"))
    c_src, c_out = elfpatch.crc(s0.elf), elfpatch.crc(d.elf)
    log("PCSX2 game CRC: %08X -> %08X %s" % (c_src, c_out, "(unchanged)" if c_src == c_out else "CHANGED"))
    if c_src != c_out:
        ok = False
    ranges = elfpatch.touched_ranges(d.elf)
    for name, addrs in PNACH.get(c_src, {}).items():
        clash = [a for a in addrs if any(lo <= a < hi for lo, hi in ranges)]
        log("pnach '%s': %s" % (name, "no overlap" if not clash else "OVERLAP at " + ", ".join(map(hex, clash))))
        if clash:
            ok = False
    log("note: the modified disc can never match the redump MD5 (expected for any modified image)")
    for s in d.songs[s0.count:]:
        pcm = d.decode_song(s.index)
        log("  #%d %s / %s / %s  %.1f s  peak %d" % (s.index + 1, s.artist, s.title, s.album, len(pcm) / 32000.0,
                                                    int(abs(pcm.astype(int)).max())))
    for s, o in zip(d.songs, s0.songs):
        if (s.title, s.artist, s.album, s.usable) != (o.title, o.artist, o.album, o.usable):
            log("FAIL: original song %d changed" % (s.index + 1))
            ok = False
    log("RESULT: %s" % ("OK" if ok else "FAILED"))
    return ok
