"""RenderWare Audio stream (.RWS) container as used by Burnout Revenge's EA Trax files.

Layout (little endian), see FORMAT notes in README.md:
  0x0000 chunk 0x080d {size, version}         whole file
  0x000c chunk 0x080e {size, version}         header, payload at 0x18
  0x0018 base (0x50) | file name | segments (0x20 each) | usable sizes (u32 per segment*layer) |
         segment uuids (16 each) | segment names | layer info (0x28 each) | layer config (0x30 each) |
         layer uuids (16 each) | layer names | garbage up to the header chunk end
  data   chunk 0x080f {size, version}, segments back to back (each padded to a multiple of the block size)

Strings are NUL terminated and padded to the next 16 bytes. EA Trax: 1 layer, PS-ADPCM stereo 32 kHz,
block 0x2000 = 0x1000 left + 0x1000 right.
"""
import struct
import uuid

from . import adpcm

CODEC_PSADPCM = 0xD9EA9798


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _rws_string(b, o):
    e = b.index(b"\0", o)
    size = (e - o) // 16 * 16 + 16
    return b[o:e].decode("latin-1"), b[o:o + size]


def _pad_string(s, template=b""):
    raw = s.encode("latin-1") + b"\0"
    size = (len(raw) + 15) // 16 * 16
    return raw + b"\0" * (size - len(raw))


class Segment:
    def __init__(self, info, usable, uid, name, raw_name):
        self.info = bytearray(info)   # 0x20 bytes
        self.usable = usable          # payload bytes without padding (all channels)
        self.uuid = bytes(uid)
        self.name = name
        self.raw_name = raw_name

    @property
    def size(self):
        return _u32(self.info, 0x18)

    @property
    def offset(self):
        return _u32(self.info, 0x1C)


class RwsHeader:
    """Parsed header of an RWS file (data is not loaded; use read_segment with an open file)."""

    def __init__(self, blob):
        b = bytes(blob)
        if _u32(b, 0) != 0x80D or _u32(b, 0x0C) != 0x80E:
            raise ValueError("not an RWS audio file")
        self.version = b[8:12]
        self.header_size = _u32(b, 0x10)
        self.data_offset = 0x18 + self.header_size          # position of the 0x080f chunk
        self.data_chunk = b[self.data_offset:self.data_offset + 12] if len(b) >= self.data_offset + 12 else None
        o = 0x18
        self.base = bytearray(b[o:o + 0x50])
        nseg = _u32(b, o + 0x20)
        nlay = _u32(b, o + 0x28)
        o += 0x50
        self.name, raw = _rws_string(b, o)
        self.raw_name = raw
        o += len(raw)
        infos = []
        for _ in range(nseg):
            infos.append(b[o:o + 0x20])
            o += 0x20
        usable = [_u32(b, o + 4 * i) for i in range(nseg * nlay)]
        o += 4 * nseg * nlay
        uids = []
        for _ in range(nseg):
            uids.append(b[o:o + 16])
            o += 16
        names = []
        for _ in range(nseg):
            n, raw = _rws_string(b, o)
            names.append((n, raw))
            o += len(raw)
        if nlay != 1:
            raise ValueError("only single-layer RWS streams are supported (got %d layers)" % nlay)
        self.segments = [Segment(infos[i], usable[i], uids[i], names[i][0], names[i][1]) for i in range(nseg)]
        self.layer_info = bytearray(b[o:o + 0x28])
        o += 0x28
        self.layer_cfg = bytearray(b[o:o + 0x30])
        o += 0x30
        self.layer_uuid = b[o:o + 16]
        o += 16
        self.layer_name, self.layer_raw_name = _rws_string(b, o)
        o += len(self.layer_raw_name)
        self.header_end = o
        self.tail = b[o:self.data_offset]   # garbage/padding (kept for byte-identical rebuilds)

    # --- properties of the (single) layer
    @property
    def sample_rate(self):
        return _u32(self.layer_cfg, 0)

    @property
    def channels(self):
        return self.layer_cfg[0x0D]

    @property
    def codec(self):
        return _u32(self.layer_cfg, 0x1C)

    @property
    def block_size(self):
        return _u32(self.layer_info, 0x20)

    def data_size(self):
        if not self.segments:
            return 0
        last = self.segments[-1]
        return last.offset + last.size

    def segment_file_offset(self, seg):
        return self.data_offset + 12 + seg.offset

    # --- building
    def build(self, min_header_size=None):
        """Serialise header bytes (file chunk + header chunk + data chunk header) for the current segments."""
        body = bytearray()
        base = bytearray(self.base)
        struct.pack_into("<I", base, 0x20, len(self.segments))
        body += base + self.raw_name
        for s in self.segments:
            body += s.info
        for s in self.segments:
            body += struct.pack("<I", s.usable)
        for s in self.segments:
            body += s.uuid
        for s in self.segments:
            body += s.raw_name
        body += self.layer_info + self.layer_cfg + self.layer_uuid + self.layer_raw_name
        end = 0x18 + len(body)
        # header chunk size: keep the original if it still fits, else grow so data starts on a 0x800 boundary
        hs = self.header_size
        if min_header_size:
            hs = max(hs, min_header_size)
        if end + 12 > 0x18 + hs:
            data_at = (end + 12 + 0x7FF) // 0x800 * 0x800
            hs = data_at - 12 - 0x18
        tail = self.tail if 0x18 + len(body) + len(self.tail) == 0x18 + hs else b""
        body += tail
        body += b"\0" * (hs - len(body))
        # base fields: +0x00 used header bytes (end - 0x2c, as in the originals), +0x38 data payload offset
        struct.pack_into("<I", body, 0x00, end - 0x2C)
        struct.pack_into("<I", body, 0x38, 0x24 + hs)  # payload start
        data_size = self.data_size()
        out = bytearray()
        total = 0x0C + 0x0C + hs + 0x0C + data_size
        out += struct.pack("<II", 0x80D, total - 0x0C) + self.version
        out += struct.pack("<II", 0x80E, hs) + self.version
        out += body
        out += struct.pack("<II", 0x80F, data_size) + self.version
        self.header_size = hs
        self.data_offset = 0x18 + hs
        return bytes(out)

    def add_segment(self, name, payload_size, usable):
        """Append a segment descriptor (payload_size already padded to the block size)."""
        tmpl = self.segments[-1]
        info = bytearray(tmpl.info)
        struct.pack_into("<I", info, 0x18, payload_size)
        struct.pack_into("<I", info, 0x1C, self.data_size())
        seg = Segment(info, usable, uuid.uuid4().bytes, name, _pad_string(name))
        self.segments.append(seg)
        return seg


def read_header(f):
    f.seek(0)
    head = f.read(0x20)
    hs = _u32(head, 0x10)
    f.seek(0)
    return RwsHeader(f.read(0x18 + hs + 12))


def segment_payload(f, hdr, seg):
    f.seek(hdr.segment_file_offset(seg))
    return f.read(seg.size)


def deinterleave(payload, usable, block=0x2000, channels=2):
    """Split block-interleaved PS-ADPCM into per-channel byte strings (trimmed to `usable`)."""
    half = block // channels
    chans = [bytearray() for _ in range(channels)]
    for o in range(0, len(payload), block):
        for c in range(channels):
            chans[c] += payload[o + c * half:o + (c + 1) * half]
    per = usable // channels
    return [bytes(c[:per]) for c in chans]


def interleave(chans, block=0x2000):
    """Per-channel PS-ADPCM byte strings -> block-interleaved payload padded to the block size, usable size."""
    channels = len(chans)
    half = block // channels
    n = max(len(c) for c in chans)
    nblocks = (n + half - 1) // half
    out = bytearray()
    for i in range(nblocks):
        for c in chans:
            part = c[i * half:(i + 1) * half]
            out += part + b"\0" * (half - len(part))
    return bytes(out), n * channels


def decode_segment(payload, usable, block=0x2000, channels=2):
    import numpy as np
    chans = deinterleave(payload, usable, block, channels)
    pcm = [adpcm.decode(c) for c in chans]
    n = min(len(p) for p in pcm)
    return np.stack([p[:n] for p in pcm], axis=1)


def encode_segment(pcm, block=0x2000):
    """pcm: (n, channels) int16 -> (payload, usable)."""
    chans = []
    for c in range(pcm.shape[1]):
        chans.append(adpcm.encode(pcm[:, c], flags=2))  # originals carry flag 0x02 on every frame
    return interleave(chans, block)
