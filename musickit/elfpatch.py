"""Patches the Burnout Revenge executable (PAL SLES_535.07 or USA SLUS_212.42) so the EA Trax playlist can hold
more than 41 songs.

Facts (PAL addresses; the USA build has the same code ~0x180 bytes lower and is located by signature):
- song table: 41 entries x 12 bytes {u32 song id, u32 0, u32 flags (1/2/4 = menus/races/..., 8 = "new")}
  (PAL 0x460450); playlist struct (PAL 0x460640): +0x04 song count (41), +0x4c table pointer.
- song i streams from tracks\\_EATrax0.rws segment i (i < 20) or tracks\\_EATrax1.rws segment i-20.
- the profile (PAL 0x4f3580, saved on the memory card) holds one flag byte per original song (41 bytes); code
  copies it into the table (songtable_ref, profile_load on profile load) and writes it back when a song is
  toggled (song_manager_toggle) or played to the end (song_finished). Those must stay below index 41.

The table moves into the 16 KB hole between the two PT_LOAD segments (the unused .sndata section); segment 1 is
extended over it and the rest of the file shifts by the hole size.

PCSX2 identifies games by the XOR of all 32-bit words of the boot ELF (ElfObject::GetCRC) and picks patches /
widescreen pnach files by it. A compensation word is appended after the last byte of the file (never loaded) so the
patched ELF keeps the original CRC.
"""
import struct

ORIGINAL_SONGS = 41
# Shuffle mode keeps its play order as one byte per song at music manager +0xD (filled at 0x252F20, read at
# 0x253028 in PAL). From 96 songs on that array reaches the playlist's song count at manager +0x6C, which can
# lead to an out-of-range song index in shuffle mode. (52+ songs already touch the shuffle position/count at
# +0x40/+0x44: harmless, shuffle just picks again.) So 95 songs at most.
MAX_SONGS = 95
_DETECT_MAX = 100      # images made with an older MusicKit (up to 100 songs) must still be readable
KNOWN = {0x7E83CC5B: "PAL SLES-53507", 0xD224D348: "USA SLUS-21242"}

# Signature groups: PAL function windows containing the patch sites (located in other builds by masked search).
PAL_GROUPS = {
    "songtable_ref": (0x251DF0, 0x251E88),   # profile flags -> table
    "profile_load": (0x226830, 0x2268B8),   # profile load: apply flags to every song
    "song_manager_toggle": (0x252E18, 0x252E9C),   # Song Manager toggle
    "song_finished": (0x2520E4, 0x252164),   # song finished
    "trax_row": (0x1A5858, 0x1A5A08),   # Song Manager row: set mode / show mode
}

# Optional "unlock": the game refuses OFF for a song whose "not heard yet" bit (flags & 8) is set - every original
# song starts with it and loses it only after playing to the end once. These two words drop that check.
UNLOCK_PATCHES = [
    (0x1A58CC, "trax_row", 0x0000102D, "daddu v0,zero,zero  (was andi v0,s0,8: OFF refused for unheard songs)"),
    (0x1A59FC, "trax_row", 0x24020001, "addiu v0,zero,1  (was andi v0,v0,1: row offers OFF only for heard songs)"),
]
_UNLOCK_SITES = {pal for pal, _, _, _ in UNLOCK_PATCHES}

# (PAL vaddr, group, template, meaning). Template: int word, None = keep the original word, or a callable(ctx).
CODE_PATCHES = [
    (0x251E20, "songtable_ref", lambda c: 0x3C020000 | c["tbl_hi"], "lui v0,%hi(table)"),
    (0x251E28, "songtable_ref", lambda c: 0x24420000 | c["tbl_lo"], "addiu v0,v0,%lo(table)"),
    (0x226890, "profile_load", 0x24020029, "addiu v0,zero,41  (was lw v0,4(sp) = song count)"),
    (0x252E74, "song_manager_toggle", 0x2D230029, "sltiu v1,t1,41  (was lbu v1,0x54(a2))"),
    (0x252104, "song_finished", None, "lw a2,0xc4(s0)"),
    (0x252108, "song_finished", 0x2403FFF7, "addiu v1,zero,-9"),
    (0x25210C, "song_finished", 0x8CC20008, "lw v0,8(a2)"),
    (0x252110, "song_finished", 0x00431024, "and v0,v0,v1"),
    (0x252114, "song_finished", 0xACC20008, "sw v0,8(a2)"),
    (0x252118, "song_finished", 0x8E0300D8, "lw v1,0xd8(s0)"),
    (0x25211C, "song_finished", 0x2C620029, "sltiu v0,v1,41"),
    (0x252120, "song_finished", 0x0002180A, "movz v1,zero,v0"),
    (0x252124, "song_finished", 0x00651821, "addu v1,v1,a1  (a1 = %hi(profile))"),
    (0x252128, "song_finished", lambda c: 0x90620000 | c["prof_lo"], "lbu v0,%lo(profile)(v1)"),
    (0x25212C, "song_finished", 0x0200202D, "daddu a0,s0,zero"),
    (0x252130, "song_finished", None, "andi v0,v0,0xf7"),
    (0x252134, "song_finished", None, "jal <stop stream>"),
    (0x252138, "song_finished", lambda c: 0xA0620000 | c["prof_lo"], "sb v0,%lo(profile)(v1)"),
]
PAL_PROFILE_LO_SITE = 0x252114      # addiu a1,a1,%lo(profile) in the original code
PAL_TABLE_LO_SITES = (0x251E20, 0x251E28)


def crc(data):
    """PCSX2 game CRC: XOR of every little-endian 32-bit word of the ELF file."""
    import numpy as np
    n = len(data) // 4 * 4
    return int(np.bitwise_xor.reduce(np.frombuffer(bytes(data[:n]), dtype="<u4"))) if n else 0


def hi16(a):
    return ((a + 0x8000) >> 16) & 0xFFFF


class Elf:
    def __init__(self, data):
        self.d = bytearray(data)
        if self.d[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        self.phoff = struct.unpack_from("<I", self.d, 0x1C)[0]
        self.phnum = struct.unpack_from("<H", self.d, 0x2C)[0]
        self.shoff = struct.unpack_from("<I", self.d, 0x20)[0]
        self.shnum = struct.unpack_from("<H", self.d, 0x30)[0]

    def phdrs(self):
        return [list(struct.unpack_from("<8I", self.d, self.phoff + 32 * i)) for i in range(self.phnum)]

    def set_phdr(self, i, ph):
        struct.pack_into("<8I", self.d, self.phoff + 32 * i, *ph)

    def sections(self):
        return [list(struct.unpack_from("<10I", self.d, self.shoff + 40 * i)) for i in range(self.shnum)]

    def file_offset(self, va):
        for ph in self.phdrs():
            if ph[0] == 1 and ph[2] <= va < ph[2] + ph[4]:
                return ph[1] + va - ph[2]
        raise KeyError(hex(va))

    def r32(self, va):
        return struct.unpack_from("<I", self.d, self.file_offset(va))[0]

    def w32(self, va, v):
        struct.pack_into("<I", self.d, self.file_offset(va), v)

    def content_end(self):
        end = self.shoff + self.shnum * 40
        for sh in self.sections():
            if sh[1] != 8:
                end = max(end, sh[4] + sh[5])
        for ph in self.phdrs():
            end = max(end, ph[1] + ph[4])
        return end


def _find_group(e, sig, seg):
    """sig = [[value, mask], ...]; returns the vaddrs where (word & mask) == value for the whole window."""
    import numpy as np
    base_va, off, size = seg[2], seg[1], seg[4]
    text = np.frombuffer(bytes(e.d[off:off + size - size % 4]), dtype="<u4")
    vals = np.array([v for v, m in sig], dtype=np.uint32)
    masks = np.array([m for v, m in sig], dtype=np.uint32)
    cand = np.nonzero((text & masks[0]) == vals[0])[0]
    n = len(sig)
    hits = [int(i) for i in cand if i + n <= len(text) and np.array_equal(text[i:i + n] & masks, vals)]
    return [base_va + 4 * i for i in hits]


class Layout:
    """Addresses of everything MusicKit touches, for a given executable."""

    def __init__(self, data):
        e = Elf(data)
        self.elf = e
        ph = e.phdrs()
        if len(ph) < 2 or ph[0][0] != 1 or ph[1][0] != 1:
            raise ValueError("unexpected ELF layout")
        self.seg1, self.seg2 = ph[0], ph[1]
        self.patched = self.seg1[2] + self.seg1[4] == self.seg2[2]
        self.hole_end = self.seg2[2]
        sndata = [s for s in e.sections() if s[5] == 0x4000 and self.seg2[2] - 0x4008 <= s[3] < self.seg2[2]]
        if self.patched:
            self.hole_start = sndata[0][3] - 8 if sndata else None
        else:
            self.hole_start = self.seg1[2] + self.seg1[4]
            if self.hole_end - self.hole_start < MAX_SONGS * 12 + 16:
                raise ValueError("no room for the song table in this executable")
        self.table_new = sndata[0][3] if sndata else (self.hole_start + 15) & ~15
        # code sites
        self.delta = {}
        for g, (a, b) in PAL_GROUPS.items():
            hits = _find_group(e, PAL_SIGNATURES[g], self.seg1)
            if self.patched and not hits:
                hits = _find_group(e, PATCHED_SIGNATURES[g], self.seg1)
            if len(hits) != 1:
                raise ValueError("code signature %s found %d times (unsupported executable)" % (g, len(hits)))
            self.delta[g] = hits[0] - a
        prof_site = PAL_PROFILE_LO_SITE + self.delta["song_finished"]
        w = e.r32(prof_site)
        if self.patched:
            w = e.r32(0x252128 + self.delta["song_finished"])
        self.prof_lo = w & 0xFFFF
        lui = e.r32(0x252100 + self.delta["song_finished"])
        self.profile = ((lui & 0xFFFF) << 16) + (self.prof_lo - 0x10000 if self.prof_lo & 0x8000 else self.prof_lo)
        # original table + playlist
        if not self.patched:
            hi = e.r32(0x251E20 + self.delta["songtable_ref"]) & 0xFFFF
            lo = e.r32(0x251E28 + self.delta["songtable_ref"]) & 0xFFFF
            self.table_old = (hi << 16) + (lo - 0x10000 if lo & 0x8000 else lo)
            ptr = self.table_old
        else:
            self.table_old = None
            ptr = self.table_new
        self.playlist = self._find_playlist(e, ptr)

    def _find_playlist(self, e, table):
        s = self.seg1
        d = bytes(e.d[s[1]:s[1] + s[4]])
        key = struct.pack("<I", table)
        hits = []
        i = d.find(key)
        while i >= 0:
            if i % 4 == 0 and i >= 0x4C:
                base = i - 0x4C
                z, n = struct.unpack_from("<II", d, base)
                if z == 0 and 1 <= n <= _DETECT_MAX:
                    hits.append(s[2] + base)
            i = d.find(key, i + 1)
        if len(hits) != 1:
            raise ValueError("playlist structure not found (%d candidates)" % len(hits))
        return hits[0]

    def ctx(self):
        return {"tbl_hi": hi16(self.table_new), "tbl_lo": self.table_new & 0xFFFF, "prof_lo": self.prof_lo}

    def sites(self):
        """[(vaddr, PAL vaddr, template, meaning)] for this build."""
        return [(pal + self.delta[g], pal, t, m) for pal, g, t, m in CODE_PATCHES]

    def unlock_sites(self):
        return [(pal + self.delta[g], pal, t, m) for pal, g, t, m in UNLOCK_PATCHES]


def is_unlocked(data):
    """True if the "switch off unheard songs" unlock is applied."""
    lay = Layout(data)
    return all(lay.elf.r32(va) == t for va, _, t, _ in lay.unlock_sites())


def unlock(data):
    """Let the Song Manager switch any song OFF, including original songs not heard yet (same PCSX2 CRC).
    Works on original and MusicKit-patched executables."""
    if is_unlocked(data):
        return bytes(data)
    lay = Layout(data)
    e = lay.elf
    target_crc = crc(data)
    appended = len(e.d) > e.content_end()     # a compensation word is already there
    for va, _, t, _ in lay.unlock_sites():
        e.w32(va, t)
    return _fix_crc(e.d, target_crc, appended)


def is_patched(data):
    e = Elf(data)
    ph = e.phdrs()
    return ph[0][2] + ph[0][4] == ph[1][2]


def read_song_count(data):
    lay = Layout(data)
    return lay.elf.r32(lay.playlist + 4)


def read_table(data):
    """Song table entries [(song id, 0, flags)] for the current song count (original or relocated table)."""
    lay = Layout(data)
    e = lay.elf
    n = e.r32(lay.playlist + 4)
    ptr = e.r32(lay.playlist + 0x4C)
    return [struct.unpack_from("<3I", e.d, e.file_offset(ptr + 12 * i)) for i in range(n)]


def set_table(data, flags):
    """Return a copy whose song table is entry i = (i, 0, flags[i]) for every song (1..MAX_SONGS songs, same
    PCSX2 CRC). An original executable is patched first. Songs are identified by their position only: the
    profile flag bytes 0..40 (memory card) apply to table entries 0..40, later entries keep the flags written here.
    """
    total = len(flags)
    if not 1 <= total <= MAX_SONGS:
        raise ValueError("between 1 and %d songs are supported (got %d)" % (MAX_SONGS, total))
    target_crc = crc(data)
    if not is_patched(data):
        data = patch(data, ORIGINAL_SONGS)
    lay = Layout(data)
    e = lay.elf
    old = e.r32(lay.playlist + 4)
    for i in range(max(total, old, ORIGINAL_SONGS)):
        if i < total:
            entry = (i, 0, flags[i])
        elif i < ORIGINAL_SONGS:      # the profile loader still writes flags into entries 0..40
            entry = (i, 0, 7)
        else:
            entry = (0, 0, 0)
        o = e.file_offset(lay.table_new + 12 * i)
        e.d[o:o + 12] = struct.pack("<3I", *entry)
    e.w32(lay.playlist + 4, total)
    appended = len(e.d) > e.content_end()
    return _fix_crc(e.d, target_crc, appended)


def _fix_crc(data, target, appended):
    """Make crc(data) == target via a compensation word after the ELF content (appended if needed)."""
    out = bytearray(data)
    if len(out) % 4:
        out += b"\0" * (4 - len(out) % 4)
    if not appended:
        out += b"\0\0\0\0"
    struct.pack_into("<I", out, len(out) - 4, 0)
    struct.pack_into("<I", out, len(out) - 4, crc(out) ^ target)
    assert crc(out) == target
    return bytes(out)


def patch(data, total_songs, new_flags=7):
    """Return a patched copy of an original executable holding `total_songs` table entries (same PCSX2 CRC)."""
    if total_songs > MAX_SONGS:
        raise ValueError("at most %d songs fit" % MAX_SONGS)
    lay = Layout(data)
    if lay.patched:
        raise ValueError("executable already patched")
    e = lay.elf
    target_crc = crc(data)
    if e.r32(lay.playlist + 4) != ORIGINAL_SONGS or e.r32(lay.playlist + 0x4C) != lay.table_old:
        raise ValueError("playlist data does not match an original Burnout Revenge executable")
    for i in range(ORIGINAL_SONGS):
        if struct.unpack("<3I", e.d[e.file_offset(lay.table_old + 12 * i):][:12])[:2] != (i, 0):
            raise ValueError("song table does not match")
    seg1, seg2 = lay.seg1, lay.seg2
    hole = bytearray(lay.hole_end - lay.hole_start)
    for i in range(total_songs):
        if i < ORIGINAL_SONGS:
            o0 = e.file_offset(lay.table_old + 12 * i)
            entry = e.d[o0:o0 + 12]
        else:
            entry = struct.pack("<3I", i, 0, new_flags)
        o = lay.table_new - lay.hole_start + 12 * i
        hole[o:o + 12] = entry
    seg1_end_file = seg1[1] + seg1[4]
    seg2_off = seg2[1]
    shift = (seg1_end_file + len(hole)) - seg2_off
    out = bytearray(e.d[:seg1_end_file]) + hole + e.d[seg2_off:]
    ne = Elf(out)
    ph = e.phdrs()
    s1 = list(seg1)
    s1[4] += len(hole)
    s1[5] += len(hole)
    ne.set_phdr(0, s1)
    for i in range(1, len(ph)):
        if ph[i][1] >= seg2_off:
            ph[i][1] += shift
        ne.set_phdr(i, ph[i])
    if ne.shoff >= seg2_off:
        ne.shoff += shift
        struct.pack_into("<I", ne.d, 0x20, ne.shoff)
    for i in range(ne.shnum):
        o = ne.shoff + 40 * i
        sh = list(struct.unpack_from("<10I", ne.d, o))
        if sh[5] == 0x4000 and lay.hole_start <= sh[3] < lay.hole_end:   # .sndata now inside segment 1
            sh[4] = s1[1] + sh[3] - s1[2]
        elif sh[4] >= seg2_off:
            sh[4] += shift
        struct.pack_into("<10I", ne.d, o, *sh)
    ne.w32(lay.playlist + 4, total_songs)
    ne.w32(lay.playlist + 0x4C, lay.table_new)
    c = lay.ctx()
    for va, pal, tmpl, _ in lay.sites():
        if tmpl is None:
            continue
        ne.w32(va, tmpl(c) if callable(tmpl) else tmpl)
    return _fix_crc(ne.d, target_crc, appended=False)


def extend(data, total_songs, new_flags=7):
    """Grow the song table of an executable already patched by MusicKit (keeps its PCSX2 CRC)."""
    lay = Layout(data)
    if not lay.patched:
        raise ValueError("executable is not MusicKit-patched")
    e = lay.elf
    target_crc = crc(data)
    old = e.r32(lay.playlist + 4)
    if total_songs < old:
        raise ValueError("cannot remove songs from an already patched disc; rebuild from the original ISO")
    if total_songs > MAX_SONGS:
        raise ValueError("at most %d songs fit" % MAX_SONGS)
    for i in range(old, total_songs):
        o = e.file_offset(lay.table_new + 12 * i)
        e.d[o:o + 12] = struct.pack("<3I", i, 0, new_flags)
    e.w32(lay.playlist + 4, total_songs)
    appended = len(e.d) > e.content_end()
    return _fix_crc(e.d, target_crc, appended)


def touched_ranges(data):
    """Virtual address ranges MusicKit writes (for conflict checks against PCSX2 pnach patches)."""
    lay = Layout(data)
    r = [(va, va + 4) for va, _, t, _ in lay.sites() if t is not None]
    if is_unlocked(data):
        r += [(va, va + 4) for va, _, _, _ in lay.unlock_sites()]
    r += [(lay.playlist + 4, lay.playlist + 8), (lay.playlist + 0x4C, lay.playlist + 0x50),
          (lay.hole_start, lay.hole_end)]
    return r


# Signatures of the PAL groups ([value, mask] per word), generated from SLES_535.07 by gen_signatures() into
# signatures.json: "original" for unpatched executables, "patched" to find the groups again in MusicKit output.
def _mask_of(w):
    op = w >> 26
    if op in (0x02, 0x03):                 # j / jal: targets differ between builds
        return 0xFC000000
    if op in (0x0F, 0x09, 0x0D):           # lui / addiu / ori: address halves differ
        return 0xFFFF0000
    return 0xFFFFFFFF


def gen_signatures(pal_data, path=None):
    import json
    import os
    e = Elf(pal_data)
    def sig(w, va, m=None):
        if va in _UNLOCK_SITES:            # optional patch sites: match either state
            return [0, 0]
        m = _mask_of(w) if m is None else m
        return [w & m, m]
    orig = {g: [sig(e.r32(va), va) for va in range(a, b, 4)] for g, (a, b) in PAL_GROUPS.items()}
    global PAL_SIGNATURES
    PAL_SIGNATURES = orig
    pe = Elf(patch(pal_data, ORIGINAL_SONGS + 1))
    var = {pal for pal, g, t, m in CODE_PATCHES if callable(t)}
    patched = {}
    for g, (a, b) in PAL_GROUPS.items():
        patched[g] = []
        for va in range(a, b, 4):
            w = pe.r32(va)
            patched[g].append(sig(w, va, 0xFFFF0000 if va in var else None))
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "signatures.json")
    json.dump({"original": orig, "patched": patched}, open(path, "w"))
    return path


def _load_signatures():
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signatures.json")
    if os.path.exists(p):
        j = json.load(open(p))
        return j["original"], j["patched"]
    return {}, {}


PAL_SIGNATURES, PATCHED_SIGNATURES = _load_signatures()
