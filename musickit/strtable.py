"""Burnout Revenge string tables (LANGUAGE/STRINGS/MAIN{UK,FR,GE}.BIN).

Header: u32 magic 0x586a0308, u32 type id 0x766bf591, u32 count, u32 index offset (0x10).
Index: count u32 offsets. Entry: u32 id hash + UTF-16LE text + NUL, padded to 4 bytes.
Entries are sorted by the hash as a signed int32 (the game binary-searches them, string lookup).
Hash (string hash): CRC-32 table, init 0xffffffff, arithmetic (sign-extending) shift, no final xor,
over the ASCII id string, e.g. "EATraxSongTitle32" (ids are 1-based song numbers).
"""
import struct
import unicodedata

_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _T.append(_c)


def string_hash(name):
    c = 0xFFFFFFFF
    for ch in name.encode("latin-1"):
        if ch >= 0x80:
            ch -= 0x100  # the game indexes with a signed char
        sc = c - (1 << 32) if c & 0x80000000 else c
        c = ((sc >> 8) & 0xFFFFFFFF) ^ _T[(ch ^ c) & 0xFF]
    return c


def _signed(h):
    return h - (1 << 32) if h & 0x80000000 else h


class StringTable:
    def __init__(self, data):
        self.magic, self.type_id, n, io = struct.unpack_from("<4I", data, 0)
        offs = struct.unpack_from("<%dI" % n, data, io)
        self.entries = {}   # hash -> text
        self.order = []
        for o in offs:
            h = struct.unpack_from("<I", data, o)[0]
            e = o + 4
            while data[e:e + 2] != b"\0\0":
                e += 2
            self.entries[h] = data[o + 4:e].decode("utf-16-le")
            self.order.append(h)

    def get(self, name):
        return self.entries.get(string_hash(name))

    def set(self, name, text):
        self.entries[string_hash(name)] = text

    def delete(self, name):
        self.entries.pop(string_hash(name), None)

    def build(self):
        keys = sorted(self.entries, key=_signed)
        n = len(keys)
        io = 0x10
        pos = io + 4 * n
        index = []
        body = bytearray()
        for k, h in enumerate(keys):
            index.append(pos + len(body))
            raw = struct.pack("<I", h) + self.entries[h].encode("utf-16-le") + b"\0\0"
            if k != n - 1:  # the originals leave the last entry unpadded
                raw += b"\0" * (-len(raw) % 4)
            body += raw
        out = struct.pack("<4I", self.magic, self.type_id, n, io) + struct.pack("<%dI" % n, *index) + body
        return bytes(out)


def charset(tables):
    """Characters used anywhere in the original tables = characters the game fonts can draw."""
    cs = set()
    for t in tables:
        for s in t.entries.values():
            cs.update(s)
    return cs


def sanitize(text, allowed, max_len=None):
    """Map text into the allowed charset (accents are stripped only when the accented glyph is missing)."""
    out = []
    for ch in text:
        if ch in allowed:
            out.append(ch)
            continue
        repl = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                "…": "..."}.get(ch)
        if repl is None:
            repl = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append("".join(c for c in repl if c in allowed) or ("?" if "?" in allowed else ""))
    s = "".join(out).strip()
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s
