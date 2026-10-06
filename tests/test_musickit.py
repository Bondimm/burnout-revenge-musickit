"""MusicKit tests. Run from the repository folder: .venvScriptspython -m pytest tests

Format tests use your own disc files when MUSICKIT_ISO (PAL ISO), MUSICKIT_USA_ISO (USA ISO) or MUSICKIT_DISC
(extracted PAL disc folder) are set; they only read. Without them those tests are skipped.
"""
import os
import struct
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from musickit import adpcm, core, elfpatch, iso, rws, strtable  # noqa: E402

DISC = os.environ.get("MUSICKIT_DISC", "")
ISO = os.environ.get("MUSICKIT_ISO", "")  # path to your own Burnout Revenge ISO; tests that need it are skipped otherwise
USA_ISO = os.environ.get("MUSICKIT_USA_ISO", "")


def _skip(path):
    if not os.path.exists(path):
        import pytest
        pytest.skip("missing " + path)


def test_adpcm_roundtrip_exact():
    rng = np.random.default_rng(1)
    t = np.arange(32000) / 32000.0
    x = (np.sin(2 * np.pi * 440 * t) * 12000 + rng.normal(0, 300, len(t))).astype(np.int16)
    enc = adpcm.encode(x)
    assert len(enc) == (len(x) + 27) // 28 * 16
    y = adpcm.decode(enc)[: len(x)]
    snr = 10 * np.log10((x.astype(float) ** 2).sum() / ((x - y.astype(float)) ** 2).sum())
    assert snr > 30
    # re-encoding decoded ADPCM is lossless (encoder finds the same frames)
    assert np.array_equal(adpcm.decode(adpcm.encode(y)), adpcm.decode(enc)[: len(adpcm.decode(adpcm.encode(y)))])


def test_rws_header_rebuild_identical():
    for name in ("_EATRAX0.RWS", "_EATRAX1.RWS", "MOVIES.RWS"):
        p = os.path.join(DISC, "TRACKS", name)
        _skip(p)
        with open(p, "rb") as f:
            h = rws.read_header(f)
            f.seek(0)
            assert h.build() == f.read(h.data_offset + 12)


def test_rws_segment_reencode_lossless():
    p = os.path.join(DISC, "TRACKS", "_EATRAX1.RWS")
    _skip(p)
    with open(p, "rb") as f:
        h = rws.read_header(f)
        seg = h.segments[-1]
        payload = rws.segment_payload(f, h, seg)
    pcm = rws.decode_segment(payload, seg.usable)
    pay2, us2 = rws.encode_segment(pcm)
    assert us2 == seg.usable and len(pay2) == len(payload)
    assert np.array_equal(rws.decode_segment(pay2, us2), pcm)


def test_rws_add_segment_layout():
    p = os.path.join(DISC, "TRACKS", "_EATRAX1.RWS")
    _skip(p)
    with open(p, "rb") as f:
        h = rws.read_header(f)
    end = h.data_size()
    seg = h.add_segment("41", 0x4000, 0x3F00)
    head = h.build()
    h2 = rws.RwsHeader(head)
    assert len(h2.segments) == 22 and h2.segments[-1].name == "41"
    assert h2.segments[-1].offset == end and h2.segments[-1].size == 0x4000
    assert struct.unpack_from("<I", head, 0x50)[0] == h2.data_offset + 12


def test_string_hash_and_table():
    p = os.path.join(DISC, "LANGUAGE", "STRINGS", "MAINUK.BIN")
    _skip(p)
    data = open(p, "rb").read()
    t = strtable.StringTable(data)
    assert t.build() == data
    assert t.get("EATraxArtist32") == "Bloc Party"
    assert t.get("EATraxSongTitle32") == "Helicopter"
    assert t.get("EATraxAlbum32") == "Silent Alarm"
    assert strtable.string_hash("EATraxSongTitle1") == 0xD9863099
    t.set("EATraxSongTitle42", "Test")
    t2 = strtable.StringTable(t.build())
    assert t2.get("EATraxSongTitle42") == "Test" and t2.get("EATraxArtist1") == "Yellowcard"


def test_sanitize():
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '-éü")
    assert strtable.sanitize("Café Motörhead", allowed) == "Café Motorhead"
    assert strtable.sanitize("Don\u2019t", allowed) == "Don't"


def _elf_versions():
    out = []
    p = os.path.join(DISC, "SLES_535.07")
    if os.path.exists(p):
        out.append(("PAL", open(p, "rb").read(), 0x7E83CC5B, 0x460640, 0x4A3680, 0x4F3580, 0, 0))
    if os.path.exists(USA_ISO):
        out.append(("USA", iso.IsoImage(USA_ISO).read_file("/SLUS_212.42"), 0xD224D348, 0x4604C0, 0x4A3500,
                    0x4F3300, -0x80, 0x28))
    return out


def test_elf_patch():
    vers = _elf_versions()
    if not vers:
        import pytest
        pytest.skip("no executable")
    for name, d, crc, playlist, table, profile, delta, row_delta in vers:
        assert elfpatch.crc(d) == crc
        lay = elfpatch.Layout(d)
        assert (lay.playlist, lay.table_new, lay.profile) == (playlist, table, profile), name
        assert all(v == delta for g, v in lay.delta.items() if g != "trax_row"), name
        assert lay.delta["trax_row"] == row_delta, name
        out = elfpatch.patch(d, 45)
        assert elfpatch.crc(out) == crc, name            # PCSX2 CRC kept
        e = elfpatch.Elf(out)
        assert elfpatch.read_song_count(out) == 45 and elfpatch.is_patched(out)
        assert e.r32(playlist + 0x4C) == table
        assert struct.unpack("<3I", out[e.file_offset(table + 12 * 44):][:12]) == (44, 0, 7)
        assert struct.unpack("<3I", out[e.file_offset(table + 12 * 5):][:12]) == (5, 0, 15)
        c = lay.ctx()
        for va, pal, t, _ in lay.sites():
            if t is not None:
                assert e.r32(va) == (t(c) if callable(t) else t), (name, hex(va))
        o = elfpatch.Elf(d)
        a, b = o.phdrs()[1], e.phdrs()[1]
        assert d[a[1]:a[1] + a[4]] == out[b[1]:b[1] + b[4]]   # second segment moved but unchanged
        out2 = elfpatch.extend(out, 47)
        assert elfpatch.read_song_count(out2) == 47 and elfpatch.crc(out2) == crc and len(out2) == len(out)


def test_unlock_off():
    """The switch-off unlock: two words, any order with patch/extend, PCSX2 CRC kept, idempotent."""
    vers = _elf_versions()
    if not vers:
        import pytest
        pytest.skip("no executable")
    for name, d, crc, *_ in vers:
        lay = elfpatch.Layout(d)
        assert [lay.elf.r32(va) for va, _, _, _ in lay.unlock_sites()] == [0x32020008, 0x30420001], name
        assert not elfpatch.is_unlocked(d)
        u = elfpatch.unlock(d)
        assert elfpatch.crc(u) == crc and elfpatch.is_unlocked(u), name
        assert elfpatch.unlock(u) == u                                   # idempotent
        e, eu = elfpatch.Elf(d), elfpatch.Elf(u)
        sites = {va for va, _, _, _ in lay.unlock_sites()}
        s = e.phdrs()[0]
        diff = [s[2] + i for i in range(0, s[4], 4) if e.r32(s[2] + i) != eu.r32(s[2] + i)]
        assert set(diff) == sites, name                                  # nothing else in the code changed
        pu = elfpatch.unlock(elfpatch.patch(d, 43))
        up = elfpatch.patch(u, 43)
        for x in (pu, up, elfpatch.extend(pu, 45)):
            assert elfpatch.crc(x) == crc and elfpatch.is_unlocked(x) and elfpatch.is_patched(x), name
        assert any(lo <= max(sites) < hi for lo, hi in elfpatch.touched_ranges(pu))


def test_udf_tag_crc():
    _skip(ISO)
    img = iso.IsoImage(ISO)
    for lsn in (256, img.pd_lsns[0], img.entries["/SLES_535.07"].udf_fe):
        d = img.read(lsn)
        assert bytes(iso.udf_fix_tag(d)) == d


def test_build_iso_end_to_end():
    """Full build into a temp folder (writes ~4.2 GB) - only with MUSICKIT_FULL_TEST=1."""
    if not os.environ.get("MUSICKIT_FULL_TEST"):
        import pytest
        pytest.skip("set MUSICKIT_FULL_TEST=1")
    _skip(ISO)
    from musickit import validate
    sys.path.insert(0, HERE)
    import make_test_songs
    tmp = tempfile.mkdtemp()
    make_test_songs.main(tmp)
    out = os.path.join(tmp, "test.iso")
    d = core.Disc(ISO)
    song = core.NewSong(os.path.join(tmp, "test_chords_44k_24bit.flac"), "Chord Test", "MusicKit", "Demo")
    d.build([song], out)
    assert validate.validate(ISO, out, log=lambda *a: None)
    o = core.Disc(out)
    assert not o.unlocked
    o.img.f.close()
    os.remove(out)
    # unlock only (no new songs): only the executable changes
    rep = d.build([], out, unlock_off=True)
    assert rep["unlocked"] and rep["total_songs"] == d.count
    assert validate.validate(ISO, out, log=lambda *a: None)
    o = core.Disc(out)
    assert o.unlocked and o.count == d.count and not o.patched
    o.img.f.close()
    os.remove(out)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
