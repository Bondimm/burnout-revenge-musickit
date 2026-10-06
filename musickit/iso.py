"""ISO9660 + UDF 1.02 bridge image reader/patcher for PS2 DVDs.

The output image is a byte copy of the source with a few files replaced: a file that still fits in its
original sectors is rewritten in place, otherwise it is appended after the original end of the volume.
Both file systems are patched (ISO9660 directory records, UDF file entries + descriptor CRCs), the volume
size, the UDF partition length and the trailing UDF anchor are updated. Untouched files keep their LSNs.
"""
import os
import shutil
import struct

SECTOR = 2048


def crc16_ccitt(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def udf_fix_tag(desc, location=None):
    """Recompute descriptor CRC (over CRC length bytes after the 16-byte tag) and the tag checksum."""
    desc = bytearray(desc)
    if location is not None:
        struct.pack_into("<I", desc, 12, location)
    crc_len = struct.unpack_from("<H", desc, 10)[0]
    struct.pack_into("<H", desc, 8, crc16_ccitt(desc[16:16 + crc_len]))
    desc[4] = sum(desc[i] for i in range(16) if i != 4) & 0xFF
    return desc


def _both32(v):
    return struct.pack("<I", v) + struct.pack(">I", v)


class Parts:
    """File content assembled from bytes and (path, offset, size) ranges of other files, written without loading
    it into memory (used for large .RWS files)."""

    def __init__(self, parts):
        self.parts = list(parts)

    def __len__(self):
        return sum(len(p) if isinstance(p, (bytes, bytearray)) else p[2] for p in self.parts)

    def write_to(self, out):
        for p in self.parts:
            if isinstance(p, (bytes, bytearray)):
                out.write(p)
                continue
            path, off, size = p
            with open(path, "rb") as f:
                f.seek(off)
                while size:
                    b = f.read(min(size, 8 << 20))
                    if not b:
                        raise IOError("unexpected end of " + path)
                    out.write(b)
                    size -= len(b)

    def read(self):
        import io
        b = io.BytesIO()
        self.write_to(b)
        return b.getvalue()


class Entry:
    def __init__(self, path, lsn, size, rec_lsn, rec_off):
        self.path = path
        self.lsn = lsn
        self.size = size
        self.rec_lsn = rec_lsn
        self.rec_off = rec_off
        self.udf_fe = None   # absolute LSN of the UDF file entry

    @property
    def sectors(self):
        return (self.size + SECTOR - 1) // SECTOR


class IsoImage:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        pvd = self.read(16)
        if pvd[1:6] != b"CD001" or pvd[0] != 1:
            raise ValueError("not an ISO9660 image")
        self.pvd = pvd
        self.volume_sectors = struct.unpack_from("<I", pvd, 80)[0]
        self.entries = {}
        root = pvd[156:156 + 34]
        self._walk(struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0], "")
        self.udf = None
        try:
            self._read_udf()
        except Exception as exc:  # image without UDF bridge: ISO9660 only
            self.udf = None
            self.udf_error = str(exc)

    def read(self, lsn, count=1):
        self.f.seek(lsn * SECTOR)
        return self.f.read(count * SECTOR)

    def read_file(self, path):
        e = self.entries[path.upper()]
        self.f.seek(e.lsn * SECTOR)
        return self.f.read(e.size)

    def open_file(self, path):
        """(file object positioned at the file start, size)."""
        e = self.entries[path.upper()]
        f = open(self.path, "rb")
        f.seek(e.lsn * SECTOR)
        return f, e

    # ---- ISO9660
    def _walk(self, lsn, size, prefix):
        data = self.read(lsn, (size + SECTOR - 1) // SECTOR)
        off = 0
        while off < size:
            length = data[off]
            if length == 0:
                off = (off // SECTOR + 1) * SECTOR
                continue
            rec = data[off:off + length]
            ext = struct.unpack_from("<I", rec, 2)[0]
            dsize = struct.unpack_from("<I", rec, 10)[0]
            flags = rec[25]
            nlen = rec[32]
            name = rec[33:33 + nlen]
            if name not in (b"\x00", b"\x01"):
                name = name.decode("ascii", "replace").split(";")[0]
                path = prefix + "/" + name
                if flags & 2:
                    self._walk(ext, dsize, path)
                else:
                    self.entries[path.upper()] = Entry(path, ext, dsize, lsn + off // SECTOR, off % SECTOR)
            off += length

    # ---- UDF
    def _read_udf(self):
        avdp = self.read(256)
        if struct.unpack_from("<H", avdp, 0)[0] != 2:
            raise ValueError("no UDF anchor")
        self.avdp = avdp
        mlen, mloc = struct.unpack_from("<II", avdp, 16)
        rlen, rloc = struct.unpack_from("<II", avdp, 24)
        self.vds = [(mloc, mlen // SECTOR), (rloc, rlen // SECTOR)]
        part_start = None
        fsd = None
        lvid = None
        self.pd_lsns = []
        for loc, n in self.vds:
            for lsn in range(loc, loc + n):
                d = self.read(lsn)
                t = struct.unpack_from("<H", d, 0)[0]
                if t == 5:
                    part_start = struct.unpack_from("<I", d, 188)[0]
                    self.pd_lsns.append(lsn)
                elif t == 6 and fsd is None:
                    fsd = struct.unpack_from("<I", d, 252)[0]
                    lvid = struct.unpack_from("<II", d, 432)
                elif t == 8 or t == 0:
                    break
        self.part_start = part_start
        self.lvid_lsn = lvid[1] if lvid and lvid[0] else None
        fsd_d = self.read(part_start + fsd)
        root_icb = struct.unpack_from("<I", fsd_d, 404)[0]
        self.udf = True
        self._udf_walk(root_icb, "")

    def _fe(self, block):
        d = self.read(self.part_start + block)
        if struct.unpack_from("<H", d, 0)[0] != 261:
            raise ValueError("expected UDF file entry at block %d" % block)
        return d

    def _fe_extents(self, d):
        icb_flags = struct.unpack_from("<H", d, 34)[0]
        l_ea, l_ad = struct.unpack_from("<II", d, 168)
        ads = d[176 + l_ea:176 + l_ea + l_ad]
        kind = icb_flags & 7
        out = []
        if kind == 0:
            for i in range(0, len(ads), 8):
                ln, pos = struct.unpack_from("<II", ads, i)
                out.append((pos, ln & 0x3FFFFFFF))
        elif kind == 1:
            for i in range(0, len(ads), 16):
                ln, pos = struct.unpack_from("<II", ads, i)
                out.append((pos, ln & 0x3FFFFFFF))
        else:
            raise ValueError("unsupported UDF allocation type %d" % kind)
        return out

    def _udf_walk(self, block, prefix):
        d = self._fe(block)
        size = struct.unpack_from("<Q", d, 56)[0]
        data = b""
        for pos, ln in self._fe_extents(d):
            data += self.read(self.part_start + pos, (ln + SECTOR - 1) // SECTOR)[:ln]
        data = data[:size]
        off = 0
        while off + 38 <= len(data):
            if struct.unpack_from("<H", data, off)[0] != 257:
                break
            chars = data[off + 18]
            l_fi = data[off + 19]
            icb = struct.unpack_from("<I", data, off + 24)[0]
            l_iu = struct.unpack_from("<H", data, off + 36)[0]
            ident = data[off + 38 + l_iu:off + 38 + l_iu + l_fi]
            total = (38 + l_iu + l_fi + 3) // 4 * 4
            off += total
            if chars & 8:      # parent
                continue
            if not ident:
                continue
            name = ident[1:].decode("latin-1") if ident[0] == 8 else ident[1:].decode("utf-16-be")
            path = (prefix + "/" + name).upper()
            if chars & 2:
                self._udf_walk(icb, path)
            elif path in self.entries:
                self.entries[path].udf_fe = self.part_start + icb

    # ---- writing
    def plan(self, sizes):
        """Placement of replaced files {iso path: size in bytes} -> ({path: lsn}, new volume sectors, has end
        anchor). A file that still fits stays in place, the others go after the end of the volume."""
        sizes = {(k.upper() if k.startswith("/") else "/" + k.upper()): v for k, v in sizes.items()}
        has_end_anchor = False
        if self.udf:
            last = self.read(self.volume_sectors - 1)
            has_end_anchor = struct.unpack_from("<H", last, 0)[0] == 2
        # new data starts where the end anchor was (or at the end). Replaced files that already sit after every
        # untouched file (appended by an earlier MusicKit build) are rewritten from there, so the image does not
        # grow on every rebuild.
        next_lsn = self.volume_sectors - 1 if has_end_anchor else self.volume_sectors
        fixed_end = max(e.lsn + e.sectors for k, e in self.entries.items() if k not in sizes)
        tail = [self.entries[k].lsn for k in sizes if self.entries[k].lsn >= fixed_end]
        relocate_tail = bool(tail)
        if relocate_tail:
            next_lsn = min(next_lsn, max(fixed_end, min(tail)))
        lsns = {}
        for key in sorted(sizes, key=lambda k: self.entries[k].lsn):
            e = self.entries[key]
            n = (sizes[key] + SECTOR - 1) // SECTOR
            if n <= e.sectors and not (relocate_tail and e.lsn >= fixed_end):
                lsns[key] = e.lsn
            else:
                lsns[key] = next_lsn
                next_lsn += n
        new_volume = max(next_lsn + (1 if has_end_anchor else 0), self.volume_sectors)
        return lsns, new_volume, has_end_anchor

    def build(self, out_path, replacements, progress=None):
        """Write a new image: replacements = {iso path: bytes or Parts}. Returns {path: (lsn, size)} of written files."""
        if os.path.abspath(out_path) == os.path.abspath(self.path):
            raise ValueError("refusing to overwrite the source image")
        reps = {}
        for p, data in replacements.items():
            key = p.upper() if p.startswith("/") else "/" + p.upper()
            if key not in self.entries:
                raise KeyError("file not on the disc: " + p)
            reps[key] = data
        # copy the source image
        total = os.path.getsize(self.path)
        done = 0
        with open(self.path, "rb") as src, open(out_path, "wb") as dst:
            while True:
                chunk = src.read(16 << 20)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(0.9 * done / total, "copying image")
        lsns, new_volume, has_end_anchor = self.plan({k: len(v) for k, v in reps.items()})
        placed = {}
        with open(out_path, "r+b") as out:
            for key in sorted(reps, key=lambda k: self.entries[k].lsn):
                e = self.entries[key]
                data = reps[key]
                n = (len(data) + SECTOR - 1) // SECTOR
                lsn = lsns[key]
                out.seek(lsn * SECTOR)
                if isinstance(data, Parts):
                    data.write_to(out)
                else:
                    out.write(data)
                out.write(b"\0" * (n * SECTOR - len(data)))
                placed[key] = (lsn, len(data))
                # ISO9660 directory record: extent (both endian) at +2, size at +10
                out.seek(e.rec_lsn * SECTOR + e.rec_off + 2)
                out.write(_both32(lsn) + _both32(len(data)))
                if self.udf and e.udf_fe is not None:
                    fe = bytearray(self.read(e.udf_fe))
                    self._patch_fe(fe, lsn - self.part_start, len(data))
                    out.seek(e.udf_fe * SECTOR)
                    out.write(udf_fix_tag(fe))
            # PVD volume space size
            pvd = bytearray(self.pvd)
            pvd[80:88] = _both32(new_volume)
            out.seek(16 * SECTOR)
            out.write(pvd)
            if self.udf:
                part_len = (new_volume - 1 if has_end_anchor else new_volume) - self.part_start
                for lsn in self.pd_lsns:
                    pd = bytearray(self.read(lsn))
                    struct.pack_into("<I", pd, 192, part_len)
                    out.seek(lsn * SECTOR)
                    out.write(udf_fix_tag(pd))
                if self.lvid_lsn:
                    lv = bytearray(self.read(self.lvid_lsn))
                    if struct.unpack_from("<H", lv, 0)[0] == 9:
                        npart = struct.unpack_from("<I", lv, 72)[0]
                        if npart >= 1:
                            struct.pack_into("<I", lv, 80 + 4 * npart, part_len)   # size table
                            struct.pack_into("<I", lv, 80, 0)                      # free space
                        out.seek(self.lvid_lsn * SECTOR)
                        out.write(udf_fix_tag(lv))
                if has_end_anchor:
                    out.seek((new_volume - 1) * SECTOR)
                    out.write(udf_fix_tag(self.avdp, new_volume - 1))
            out.truncate(new_volume * SECTOR)
        if progress:
            progress(1.0, "done")
        return placed

    def _patch_fe(self, fe, block, size):
        icb_flags = struct.unpack_from("<H", fe, 34)[0]
        kind = icb_flags & 7
        l_ea, l_ad = struct.unpack_from("<II", fe, 168)
        struct.pack_into("<Q", fe, 56, size)
        nblocks = (size + SECTOR - 1) // SECTOR
        struct.pack_into("<Q", fe, 64, nblocks)
        maxext = 0x3FFFF800
        exts = []
        pos, left = block, size
        while left > 0 or not exts:
            ln = min(left, maxext)
            exts.append((pos, ln))
            pos += (ln + SECTOR - 1) // SECTOR
            left -= ln
        if kind == 0:
            ads = b"".join(struct.pack("<II", ln, p) for p, ln in exts)
        elif kind == 1:
            ads = b"".join(struct.pack("<IIH6x", ln, p, 0) for p, ln in exts)
        else:
            raise ValueError("unsupported UDF allocation type")
        base = 176 + l_ea
        fe[base:base + l_ad] = b"\0" * l_ad
        fe[base:base + len(ads)] = ads
        struct.pack_into("<I", fe, 172, len(ads))
        struct.pack_into("<H", fe, 10, base + len(ads) - 16)


def write_layout(img, out_txt):
    """disc_layout.txt for the PC port: '<lsn> <size> /PATH' per file, sorted by LSN."""
    rows = sorted((e.lsn, e.size, e.path) for e in img.entries.values())
    with open(out_txt, "w") as f:
        for lsn, size, path in rows:
            f.write("%d %d %s\n" % (lsn, size, path))
