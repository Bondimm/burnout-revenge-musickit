"""Tests that need no disc image (run on every OS in CI): codecs, containers, text tables, ffmpeg import with the
generated test songs in tests/data, the resampler fallback, platform helpers (ffmpeg lookup, settings folder,
audio preview player) and the GUI logic without a window.
"""
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from musickit import adpcm, audio, core, iso, rws, strtable  # noqa: E402

FLAC = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")
WAV = os.path.join(HERE, "data", "test_chords_22k_mono.wav")


def _need_ffmpeg():
    exe = audio.ffmpeg_exe("ffprobe")
    if not (os.path.isfile(exe) or shutil.which(exe)):
        pytest.skip("ffmpeg not found")


def _snr(ref, out):
    ref = ref.astype(np.float64)
    err = ref - out[:len(ref)].astype(np.float64)
    return 10 * np.log10((ref ** 2).sum() / max((err ** 2).sum(), 1e-9))


def test_numba_available():
    import numba  # noqa: F401  (requirements.txt installs it on every supported OS; without it encoding is slow)


def test_rws_segment_roundtrip():
    t = np.arange(32000 * 2) / 32000.0
    left = (np.sin(2 * np.pi * 440 * t) * 9000).astype(np.int16)
    right = (np.sin(2 * np.pi * 660 * t) * 6000).astype(np.int16)
    payload, usable = rws.encode_segment(np.stack([left, right], 1), block=0x2000)
    assert len(payload) % 0x2000 == 0 and usable % 32 == 0 and usable <= len(payload)
    pcm = rws.decode_segment(payload, usable, block=0x2000)
    assert pcm.shape[1] == 2 and pcm.shape[0] >= len(left)
    assert _snr(left, pcm[:, 0]) > 25 and _snr(right, pcm[:, 1]) > 25
    chans = rws.deinterleave(payload, usable)
    assert rws.interleave(chans) == (payload, usable)
    assert np.array_equal(adpcm.decode(chans[0])[:len(left)], pcm[:len(left), 0])


def test_string_table_roundtrip():
    if hasattr(strtable, "IndexedStringTable"):          # Burnout 3: strings addressed by number
        t0 = strtable.IndexedStringTable.__new__(strtable.IndexedStringTable)
        t0.magic, t0.type_id, t0.strings = strtable.MAGIC, 0xB93622FA, ["Title", "Artist ü", ""]
        t = strtable.IndexedStringTable(t0.build())
        assert t.strings == ["Title", "Artist ü", ""] and len(t0.build()) == t0.size()
        k = t.append("New song")
        t.set(0, "Renamed")
        again = strtable.IndexedStringTable(t.build())
        assert again.get(k) == "New song" and again.get(0) == "Renamed" and again.get(99) is None
    else:                                                # Revenge / Dominator: strings addressed by id hash
        import struct
        empty = struct.pack("<4I", 0x586A0308, 0x766BF591, 0, 0x10)
        t = strtable.StringTable(empty)
        for k in range(1, 8):
            t.set("EATraxSongTitle%d" % k, "Song %d é" % k)
        t.set("EATraxArtist1", "Band")
        data = t.build()
        again = strtable.StringTable(data)
        assert again.get("EATraxSongTitle5") == "Song 5 é" and again.get("EATraxArtist1") == "Band"
        assert [strtable._signed(h) for h in again.order] == sorted(strtable._signed(h) for h in again.order)
        again.delete("EATraxArtist1")
        assert strtable.StringTable(again.build()).get("EATraxArtist1") is None
    assert strtable.sanitize("Café – “Best”…", set("Cafe -\"Best.")) == 'Cafe - "Best"...'


def test_capacity_and_output_name():
    assert "about 0 more minutes" in core.capacity_text(core.DVD5_SECTORS * iso.SECTOR)
    src = os.path.join("some folder", "My Game.iso")
    assert core.default_output(src) == os.path.join("some folder", "My Game (MusicKit).iso")
    assert core.default_output(core.default_output(src)) == core.default_output(src)


def test_ffmpeg_lookup(tmp_path, monkeypatch):
    """ffmpeg/bin/ffmpeg(.exe) next to the kit wins; Windows names only on Windows."""
    monkeypatch.setattr(audio, "HERE", str(tmp_path / "kit"))
    monkeypatch.setattr(audio, "TOOLS", str(tmp_path))
    bindir = tmp_path / "kit" / "ffmpeg" / "bin"
    bindir.mkdir(parents=True)
    other = bindir / ("ffprobe" if os.name == "nt" else "ffprobe.exe")   # the other OS's name: ignored
    other.write_bytes(b"x")
    exe = bindir / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    assert audio.ffmpeg_exe("ffprobe") == str(exe)
    exe.unlink()
    assert audio.ffmpeg_exe("ffprobe") != str(other)


def test_audio_import_and_resampler_fallback(monkeypatch):
    _need_ffmpeg()
    info = audio.AudioInfo(FLAC)
    assert (info.codec, info.sample_rate, info.channels) == ("flac", 44100, 2) and info.duration > 1
    mono = audio.AudioInfo(WAV)
    assert mono.channels == 1 and any("Mono" in t for _, t in mono.quality_notes())
    pcm = audio.decode(WAV)
    assert pcm.shape[1] == 2 and abs(len(pcm) / 32000.0 - mono.duration) < 0.1
    assert np.array_equal(pcm[:, 0], pcm[:, 1])
    # an ffmpeg without soxr (or none at all while probing) -> ffmpeg's own resampler, which must work too
    monkeypatch.setattr(audio, "_RESAMPLER", None)
    real = audio.ffmpeg_exe
    monkeypatch.setattr(audio, "ffmpeg_exe", lambda name="ffmpeg": os.path.join(HERE, "no-such-ffmpeg"))
    assert audio.resampler().startswith("resampler=swr")
    monkeypatch.setattr(audio, "ffmpeg_exe", real)
    x = audio.decode(FLAC)
    assert abs(len(x) / 32000.0 - info.duration) < 0.1 and 0.05 < np.abs(x).max() <= 1.0


def test_prepare_loudness_and_encode():
    _need_ffmpeg()
    pcm, in_lufs = audio.prepare(FLAC, target_lufs=-20.0)
    assert pcm.dtype == np.int16 and pcm.shape[1] == 2 and in_lufs is not None
    assert abs(audio.measure_loudness(pcm, 32000) - -20.0) < 1.5
    payload, usable = rws.encode_segment(pcm[:32000 * 3])
    out = rws.decode_segment(payload, usable)
    assert _snr(pcm[:32000 * 3, 0], out[:, 0]) > 15


def test_player_stops(monkeypatch):
    """The non-Windows preview player runs a stoppable background process (afplay on macOS)."""
    if sys.platform == "darwin":
        assert audio.Player.command("x.wav") == ["afplay", "x.wav"]
    monkeypatch.setattr(audio.os, "name", "posix")    # exercise that code path on every OS
    monkeypatch.setattr(audio.Player, "command",
                        staticmethod(lambda path: [sys.executable, "-c", "import time; time.sleep(60)"]))
    p = audio.Player()
    p.play("x.wav")
    first = p.proc
    assert first.poll() is None
    p.play("y.wav")                                     # a new preview stops the previous one
    assert first.poll() is not None and p.proc.poll() is None
    second = p.proc
    p.stop()
    assert second.poll() is not None and p.proc is None
    p.stop()


def test_settings_folder(tmp_path, monkeypatch):
    gui = pytest.importorskip("musickit.gui")
    for var in ("APPDATA", "XDG_CONFIG_HOME", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    d = gui.app_dir()
    assert os.path.isdir(d)
    if os.name == "nt":
        assert os.path.dirname(d) == str(tmp_path)
    elif sys.platform == "darwin":
        assert os.path.dirname(d) == os.path.join(str(tmp_path), "Library", "Application Support")
    else:
        assert os.path.dirname(d) == str(tmp_path)


def test_gui_logic_without_disc(tmp_path, monkeypatch):
    """The window's logic without a window and without a disc: drop files, errors, Save button reason."""
    gui = pytest.importorskip("musickit.gui")
    for var in ("APPDATA", "XDG_CONFIG_HOME", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    log = gui.Log(os.path.join(gui.app_dir(), "gui.log"))
    g = gui.MusicKitGui(log, {})
    g.save_settings = lambda: None
    assert g.disc is None and g.save_problem() == "select the ISO first" and not g.can_save()
    g.drop([str(tmp_path / "missing.iso")])
    assert g.disc is None and g.disc_error
    _need_ffmpeg()
    g.drop([WAV])
    assert g.form["path"] == WAV and g.form_info is not None and g.form_error is None
    assert g.form["title"] == "test_chords_22k_mono"
    g.stop()
