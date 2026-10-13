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


def test_shuffle_list_room():
    """The shuffle order list (one byte per song) must not run into other music manager fields, the song table or
    the part of the .sndata hole ChainKit uses."""
    assert elfpatch.manager_overlap(elfpatch.MANAGER_SHUFFLE, 51) == []
    # the list kept in the manager is too small for more songs: from 52 songs on it overwrites the shuffle position,
    # then the shuffle song count, the stream file name and the playlist copy
    assert elfpatch.manager_overlap(elfpatch.MANAGER_SHUFFLE, 52) == ["shuffle position"]
    assert len(elfpatch.manager_overlap(elfpatch.MANAGER_SHUFFLE, elfpatch.MAX_SONGS)) == 4
    # moved list: after the song table and ChainKit's arrow texture copy (table + 0x480 .. + 0x7B8), before ChainKit's
    # data (hole end - 0x3680 = table + 0x980), room for every song
    assert elfpatch.MAX_SONGS * 12 <= 0x480 and 0x7B8 <= elfpatch.SHUFFLE_OFFSET
    assert elfpatch.SHUFFLE_OFFSET + elfpatch.SHUFFLE_SIZE <= 0x980
    assert elfpatch.MAX_SONGS <= min(elfpatch.SHUFFLE_SIZE, 256)


def test_shuffle_list_moved():
    vers = _elf_versions()
    if not vers:
        import pytest
        pytest.skip("no executable")
    for name, d, crc, playlist, table, *_ in vers:
        assert elfpatch.shuffle_list(d) == ("manager", elfpatch.MANAGER_SHUFFLE)
        out = elfpatch.set_table(d, [7] * elfpatch.MAX_SONGS)
        lay = elfpatch.Layout(out)
        assert elfpatch.shuffle_list(out) == ("hole", table + elfpatch.SHUFFLE_OFFSET), name
        assert lay.hole_start <= table + 12 * 100 <= lay.shuffle and lay.shuffle + elfpatch.SHUFFLE_SIZE <= lay.hole_end
        # no list access through the manager pointer is left in the shuffle functions (byte load / store at +0x0D)
        for g in ("shuffle_fill", "shuffle_next"):
            a, b = elfpatch.PAL_GROUPS[g]
            words = [lay.elf.r32(va + lay.delta[g]) for va in range(a, b, 4)]
            assert not [w for w in words if w >> 26 in (0x20, 0x24, 0x28) and w & 0xFFFF == 0x0D], (name, g)
        # an executable patched by an older MusicKit version (list still in the manager) is caught and upgraded
        e, o = elfpatch.Elf(out), elfpatch.Elf(d)
        for va, pal, _, _ in lay.sites():
            if pal in elfpatch.SHUFFLE_SITES:
                e.w32(va, o.r32(va))
        old = elfpatch._fix_crc(e.d, crc, appended=True)
        assert elfpatch.shuffle_list(old) == ("manager", elfpatch.MANAGER_SHUFFLE)
        assert elfpatch.manager_overlap(elfpatch.MANAGER_SHUFFLE, elfpatch.read_song_count(old))
        up = elfpatch.set_table(old, [7] * elfpatch.MAX_SONGS)
        assert elfpatch.shuffle_list(up) == ("hole", lay.shuffle) and elfpatch.crc(up) == crc and up == out, name


# Addresses written by public PCSX2 patches for Burnout Revenge (pcsx2_patches: widescreen, 60 FPS, ...; Nahelam's
# HostFS patches) and by ChainKit (the Burnout Chain mod: hooks, its data and code at the top of the .sndata hole,
# its arrow texture copy), as [start, end) ranges per PCSX2 CRC. MusicKit must not write any of them.
_HOSTFS = {0x7E83CC5B: [(0x38A478, 0x38A534), (0x44D230, 0x44D234), (0x44D6AC, 0x44D6BC)],
           0xD224D348: [(0x38A2D0, 0x38A38C), (0x44D0B0, 0x44D0B4), (0x44D52C, 0x44D53C)]}
_CHAINKIT = {
    0x7E83CC5B: [(0x4A3B00, 0x4A3E38), (0x4A4000, 0x4A7680)] + [(a, a + 4) for a in (
        0x114A9C, 0x11A2DC, 0x15DCE4, 0x15DCF4, 0x15DD0C, 0x15E3B8, 0x15E3BC, 0x15F09C, 0x1617B4, 0x1617B8, 0x16D358,
        0x16FCFC, 0x16FD0C, 0x16FD1C, 0x16FD24, 0x16FDD8, 0x16FDE0, 0x16FDE4, 0x16FDE8, 0x16FED4, 0x16FEE8, 0x16FEEC,
        0x16FEF4, 0x1DC924, 0x1DC944, 0x204E4C, 0x204E98, 0x2A3A98, 0x2A3A9C, 0x2A3E0C, 0x2A3E3C, 0x2A3EC4, 0x2A3EE8,
        0x2A3EEC, 0x2A3F50, 0x2C8908, 0x2C890C)],
    0xD224D348: [(0x4A3980, 0x4A3CB8), (0x4A3E80, 0x4A7500)] + [(a, a + 4) for a in (
        0x114B54, 0x11A394, 0x15DD8C, 0x15DD9C, 0x15DDB4, 0x15E460, 0x15E464, 0x15F144, 0x16185C, 0x161860, 0x16D400,
        0x16FDA4, 0x16FDB4, 0x16FDC4, 0x16FDCC, 0x16FE80, 0x16FE88, 0x16FE8C, 0x16FE90, 0x16FF7C, 0x16FF90, 0x16FF94,
        0x16FF9C, 0x1DC8A4, 0x1DC8C4, 0x204DCC, 0x204E18, 0x2A3980, 0x2A3984, 0x2A3CF4, 0x2A3D24, 0x2A3DAC, 0x2A3DD0,
        0x2A3DD4, 0x2A3E38, 0x2C87F0, 0x2C87F4)],
}


def _cheat_ranges():
    from musickit import validate
    out = {}
    for c, cheats in validate.PNACH.items():
        out[c] = [(a, a + 4) for name, addrs in cheats.items() if "HostFS" not in name for a in addrs]
        out[c] += _HOSTFS[c]
    return out


def _overlaps(a, b):
    return [(hex(lo), hex(hi)) for lo, hi in a for x, y in b if lo < y and x < hi]


def test_no_overlap_with_pcsx2_patches_and_chainkit_static():
    """PAL executable, from the address lists alone (no disc needed): every code word MusicKit can write, the
    playlist words, the song table and the shuffle list."""
    crc, playlist, table = 0x7E83CC5B, 0x460640, 0x4A3680
    ours = [(pal, pal + 4) for pal, _, t, _ in elfpatch.CODE_PATCHES if t is not None]
    ours += [(pal, pal + 4) for pal, _, _, _ in elfpatch.UNLOCK_PATCHES]
    ours += [(playlist + 4, playlist + 8), (playlist + 0x4C, playlist + 0x50),
             (table, table + elfpatch.MAX_SONGS * 12), (table + elfpatch.SHUFFLE_OFFSET, table + elfpatch.SHUFFLE_OFFSET + elfpatch.SHUFFLE_SIZE)]
    assert _overlaps(ours, _cheat_ranges()[crc]) == []
    assert _overlaps(ours, _CHAINKIT[crc]) == []


def test_no_overlap_with_pcsx2_patches_and_chainkit():
    vers = _elf_versions()
    if not vers:
        import pytest
        pytest.skip("no executable")
    cheats = _cheat_ranges()
    for name, d, crc, playlist, table, *_ in vers:
        out = elfpatch.unlock(elfpatch.set_table(d, [7] * elfpatch.MAX_SONGS))
        lay = elfpatch.Layout(out)
        assert _overlaps(elfpatch.touched_ranges(out), cheats[crc]) == [], name
        # with ChainKit: every code word MusicKit changes, the song table and the shuffle list
        ours = [(va, va + 4) for va, _, t, _ in lay.sites() if t is not None] + \
               [(va, va + 4) for va, _, _, _ in lay.unlock_sites()] + \
               [(table, table + elfpatch.MAX_SONGS * 12), (lay.shuffle, lay.shuffle + elfpatch.SHUFFLE_SIZE)]
        assert _overlaps(ours, _CHAINKIT[crc]) == [], name


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
    # ChainKit's arrow texture in GLOBAL.TXD is accepted, any other changed file is not
    tex = o.img.entries["/DATA/GLOBAL.TXD"]
    other = next(e for k, e in sorted(o.img.entries.items())
                 if k not in d.changed_paths and k not in validate.OTHER_KITS and e.size)
    o.img.f.close()

    def flip(e):
        with open(out, "r+b") as f:
            f.seek(e.lsn * 2048 + e.size // 2)
            b = f.read(1)
            f.seek(-1, 1)
            f.write(bytes([b[0] ^ 0xFF]))
    flip(tex)
    assert validate.validate(ISO, out, log=lambda *a: None)
    flip(other)
    assert not validate.validate(ISO, out, log=lambda *a: None)
    os.remove(out)


# ---------------------------------------------------------------- song management (replace/edit/remove/move)
def _isos():
    out = [p for p in (ISO, USA_ISO) if p and os.path.exists(p)]
    if not out:
        import pytest
        pytest.skip("set MUSICKIT_ISO and/or MUSICKIT_USA_ISO")
    return out


def _ffmpeg():
    from musickit import audio
    try:
        audio.AudioInfo(os.path.join(HERE, "data", "test_chords_22k_mono.wav"))
    except Exception:
        import pytest
        pytest.skip("ffmpeg not found")


def _check_reps(d, items, reps):
    """Song list consistency of the files build_list writes for `items`: executable table + count + CRC, the
    strings of every language and the stream segment of every position."""
    total = len(items)
    elf = reps[d.elf_path]
    assert elfpatch.crc(elf) == d.crc and elfpatch.read_song_count(elf) == total
    table = elfpatch.read_table(elf)
    for k, it in enumerate(items):
        assert table[k][:2] == (k, 0)
        assert table[k][2] == (7 if isinstance(it, core.NewSong) else d.songs[it.src].flags)
    for l in d.langs:
        t = strtable.StringTable(reps["/LANGUAGE/STRINGS/MAIN%s.BIN" % l])
        for k, it in enumerate(items):
            for f, sid in (("title", "EATraxSongTitle%d"), ("artist", "EATraxArtist%d"), ("album", "EATraxAlbum%d")):
                want = it.text(l, f) if isinstance(it, core.NewSong) else it.text(d, l, f)
                assert t.get(sid % (k + 1)) == (want or (" " if f != "title" else "Untitled")), (l, k, f)
    for ri, path in enumerate(core.RWS_FILES):
        lo, hi = (0, min(core.SPLIT, total)) if ri == 0 else (core.SPLIT, total)
        if path not in reps:          # unchanged file: same songs at the same place, or not used at all
            assert hi <= lo or all(isinstance(items[k], core.SongRef) and items[k].src == k and not items[k].audio
                                   for k in range(lo, hi))
            continue
        parts = reps[path].parts
        h = rws.RwsHeader(parts[0])
        assert len(h.segments) == hi - lo and len(parts) == 1 + hi - lo
        for j, seg in enumerate(h.segments):
            it = items[lo + j]
            assert seg.name == "%02d" % (lo + j)
            assert seg.offset == sum(s.size for s in h.segments[:j])
            if isinstance(it, core.SongRef) and not it.audio:
                s = d.songs[it.src]
                src_seg = d.headers[s.rws_index].segments[s.segment]
                e = d.img.entries[core.RWS_FILES[s.rws_index]]
                assert parts[1 + j] == (d.path, e.lsn * iso.SECTOR + d.headers[s.rws_index].segment_file_offset(src_seg),
                                        src_seg.size)
                assert seg.usable == src_seg.usable and seg.uuid[8:] == src_seg.uuid[8:]
            else:
                assert seg.uuid.startswith(core.MARK) and len(parts[1 + j]) == seg.size


def test_remove_move_edit_consistency():
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        assert d.is_unchanged(items) and d.save_warning(items) is None
        items.pop(2)                                   # remove an original song in _EATRAX0
        items.insert(0, items.pop(-1))                 # move the last song (in _EATRAX1) to the top
        items[24], items[25] = items[25], items[24]    # swap two songs in _EATRAX1
        items[5].title = "Edited Title"                # rename for every language
        items[5].names = {d.langs[-1]: {"artist": "Edited Artist"}}
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert rep["total_songs"] == 40 and rep["removed"] == 1
        assert d.save_shift(items)[:3] == [1, 2, 3] and d.save_warning(items)
        t = strtable.StringTable(reps["/LANGUAGE/STRINGS/MAIN%s.BIN" % d.langs[-1]])
        assert t.get("EATraxSongTitle6") == "Edited Title" and t.get("EATraxArtist6") == "Edited Artist"
        assert t.get("EATraxSongTitle41") == d.songs[40].names[d.langs[-1]]["title"]   # original ids stay


def test_edit_only_keeps_streams_and_saves():
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        items[0].names = {l: {"title": "Neu %s" % l} for l in d.langs}
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert not any(k in reps for k in core.RWS_FILES)   # streams untouched
        assert d.save_shift(items) == [] and d.save_warning(items) is None


def test_minimum_one_song_and_limits():
    import pytest
    for p in _isos():
        d = core.Disc(p)
        items = [core.SongRef(30)]
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert core.RWS_FILES[1] not in reps and len(d.save_shift(items)) == 41
        with pytest.raises(ValueError):
            d.replacements([], normalize=False)
        with pytest.raises(ValueError):
            d.replacements([core.SongRef(0)] * (core.MAX_SONGS + 1), normalize=False)
        assert core.MAX_SONGS == elfpatch.MAX_SONGS == 95
        with pytest.raises(ValueError):
            elfpatch.set_table(d.elf, [7] * 96)


def test_replace_audio_keeps_slot():
    _ffmpeg()
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        items[4].audio = os.path.join(HERE, "data", "test_chords_22k_mono.wav")      # in _EATRAX0
        items[30].audio = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")   # in _EATRAX1
        items.append(core.NewSong(os.path.join(HERE, "data", "test_chords_22k_mono.wav"), "New", "MusicKit", ""))
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert d.save_shift(items) == []
        assert [s["index"] for s in rep["songs"]] == [4, 30, 41] and rep["songs"][0]["replaced"]
        h = rws.RwsHeader(reps[core.RWS_FILES[0]].parts[0])
        pcm = rws.decode_segment(reps[core.RWS_FILES[0]].parts[5], h.segments[4].usable)
        assert abs(len(pcm) / 32000.0 - 20.0) < 0.1


def test_unlock_with_song_list():
    """unlock_off with an unchanged list changes only the executable; with a changed list it rides along."""
    for p in _isos():
        d = core.Disc(p)
        reps, rep = d.replacements(d.current_list(), normalize=False, unlock_off=True)
        assert list(reps) == [d.elf_path] and rep["unlocked"] and rep["total_songs"] == d.count
        elf = reps[d.elf_path]
        assert elfpatch.is_unlocked(elf) and elfpatch.crc(elf) == d.crc and not elfpatch.is_patched(elf)
        assert elfpatch.read_table(elf) == elfpatch.read_table(d.elf)
        items = d.current_list()
        items.pop(3)
        items.insert(0, items.pop(30))
        items[7].title = "Edited"
        reps, rep = d.replacements(items, normalize=False, unlock_off=True)
        _check_reps(d, items, reps)
        elf = reps[d.elf_path]
        assert rep["unlocked"] and elfpatch.is_unlocked(elf) and elfpatch.is_patched(elf)
        again = elfpatch.set_table(elf, [7] * 50)          # a later song change keeps the unlock
        assert elfpatch.is_unlocked(again) and elfpatch.crc(again) == d.crc
        reps, rep = d.replacements(items, normalize=False)
        assert not rep["unlocked"] and not elfpatch.is_unlocked(reps[d.elf_path])


def test_size_plan_and_dvd_capacity():
    """Projected image size from the planned layout (no encoding) and the single-layer DVD check."""
    import numpy as np
    for p in _isos():
        d = core.Disc(p)
        src = d.img.volume_sectors * iso.SECTOR
        same = d.size_plan(d.current_list())
        assert same["bytes"] == src and same["fits"] and "fits on a single-layer DVD" in same["text"] and "more minutes" in same["text"]
        pcm = (np.sin(np.arange(32000 * 3) / 7.0) * 8000).astype(np.int16)
        payload, usable = rws.encode_segment(np.stack([pcm, pcm], 1), d.headers[0].block_size)
        assert d.encoded_size(3.0) == (len(payload), usable)
        items = d.current_list() + [core.NewSong("fake%d.flac" % k, "Song %d" % k, "Band", "") for k in range(10)]
        small = d.size_plan(items, estimate={k: 200.0 for k in range(d.count, len(items))})
        audio_bytes = 10 * d.encoded_size(200.0)[0]
        rws1 = d.img.entries[core.RWS_FILES[1]].size     # the grown _EATRAX1.RWS is written after the volume end
        assert small["fits"] and audio_bytes + rws1 <= small["bytes"] - src <= audio_bytes + rws1 + (8 << 20)
        big = d.size_plan(items, estimate={k: 3000.0 for k in range(d.count, len(items))})   # 10 x 50 min
        assert not big["fits"] and big["bytes"] > core.DVD5_SECTORS * iso.SECTOR
        assert "larger than a single-layer DVD (4.7 GB)" in big["text"] and "%.2f GB" % (big["bytes"] / 1e9) in big["text"]
        fewer = d.current_list()
        del fewer[35:]                     # shorter stream file: rewritten in place (only the executable may move)
        assert d.size_plan(fewer)["bytes"] - src <= d.img.entries[d.elf_path].size + (1 << 20)
        moved = d.current_list()
        del moved[5:15]                    # songs move from _EATRAX1 into _EATRAX0: that file grows and is appended
        assert d.size_plan(moved)["bytes"] > src
    assert "about 0 more minutes" in core.capacity_text(core.DVD5_SECTORS * iso.SECTOR)


def test_set_table_shrink_and_grow():
    vers = _elf_versions()
    if not vers:
        import pytest
        pytest.skip("no executable")
    for name, d, crc, playlist, table, profile, delta, _ in vers:
        for n in (1, 20, 40, 41, 60, elfpatch.MAX_SONGS):
            flags = [(i % 7) + 1 for i in range(n)]
            out = elfpatch.set_table(d, flags)
            assert elfpatch.crc(out) == crc and elfpatch.read_song_count(out) == n, (name, n)
            assert [t[2] for t in elfpatch.read_table(out)] == flags
            again = elfpatch.set_table(out, flags[:max(1, n // 2)] + [7])     # re-patch a patched executable
            assert elfpatch.crc(again) == crc and len(again) == len(out)
            e = elfpatch.Elf(again)
            for i in range(41):    # entries 0..40 stay valid for the memory card profile loader
                assert struct.unpack("<3I", again[e.file_offset(table + 12 * i):][:12])[:2] == (i, 0)


def test_manage_end_to_end():
    """Remove an original, replace one, rename one, add one, reopen the output and rename again (writes 2 images
    of ~4 GB per ISO, one at a time) - only with MUSICKIT_FULL_TEST=1."""
    if not os.environ.get("MUSICKIT_FULL_TEST"):
        import pytest
        pytest.skip("set MUSICKIT_FULL_TEST=1")
    _ffmpeg()
    from musickit import validate
    flac = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")
    wav = os.path.join(HERE, "data", "test_chords_22k_mono.wav")
    quiet = lambda *a: None   # noqa: E731
    for p in _isos():
        tmp = tempfile.mkdtemp()
        out1, out2 = os.path.join(tmp, "step1.iso"), os.path.join(tmp, "step2.iso")
        d = core.Disc(p)
        names = [s.title for s in d.songs]
        items = d.current_list()
        items.pop(1)                                   # remove original #2
        items[8].audio = flac                          # replace (now #9, was #10)
        items[3].title = "Renamed Once"                # rename (now #4, was #5)
        items.append(core.NewSong(wav, "Added Song", "MusicKit", "Demo"))
        plan = d.size_plan(items, unlock_off=True)
        rep = d.build_list(items, out1, unlock_off=True)   # song changes + "allow switching any song OFF"
        assert rep["unlocked"]
        assert abs(os.path.getsize(out1) - plan["bytes"]) <= 64 << 10, (os.path.getsize(out1), plan["bytes"])
        assert validate.validate(p, out1, log=quiet)
        r = core.Disc(out1)                            # read back the modified image
        assert r.count == 41 and r.patched and r.crc == d.crc and r.unlocked
        assert [s.title for s in r.songs[:3]] == [names[0], names[2], names[3]]
        assert r.songs[3].title == "Renamed Once" and r.songs[40].title == "Added Song"
        assert [s.original for s in r.songs] == [True] * 8 + [False] + [True] * 31 + [False]
        assert abs(r.songs[8].duration - 30.0) < 0.1
        items2 = r.current_list()                      # edit again + move + add on the modified image
        items2[3].title = "Renamed Twice"
        items2.insert(0, items2.pop(40))
        items2.append(core.NewSong(flac, "Added Later", "MusicKit", ""))
        r.build_list(items2, out2)
        assert validate.validate(out1, out2, log=quiet)
        r2 = core.Disc(out2)
        assert r2.count == 42 and r2.crc == d.crc and r2.unlocked     # the unlock stays on later edits
        assert [s.title for s in r2.songs[:2]] == ["Added Song", names[0]]
        assert r2.songs[4].title == "Renamed Twice" and r2.songs[41].title == "Added Later"
        assert sum(not s.original for s in r2.songs) == 3
        r.img.f.close()
        r2.img.f.close()
        os.remove(out1)
        os.remove(out2)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
