"""Audio import: probe + decode any format via ffmpeg, convert to the game's format, loudness matching.

Target format of EA Trax streams: PS-ADPCM, stereo, 32000 Hz (decoded quality ~ 16-bit, 32 kHz band limit
16 kHz). Inputs are resampled with soxr (high precision) or, if the ffmpeg build lacks it, ffmpeg's own resampler with
a long filter; mono is duplicated to both channels. Loudness is
matched to the median integrated loudness (EBU R128 / LUFS) of the original songs with ffmpeg's two-pass
loudnorm in linear mode (pure gain) plus a -1 dBTP ceiling.
"""
import json
import os
import subprocess

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.dirname(HERE)
TARGET_RATE = 32000
TARGET_CHANNELS = 2
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ffmpeg_exe(name="ffmpeg"):
    # ffmpeg next to the tool (ffmpeg/, ffmpeg/bin/), next to its parent folder, then PATH.
    cands = []
    for base in (HERE, TOOLS):
        for sub in ("ffmpeg", os.path.join("ffmpeg", "bin")):
            cands += [os.path.join(base, sub, name + ".exe"), os.path.join(base, sub, name)]
    for cand in cands:
        if os.path.exists(cand):
            return cand
    return name


def _run(args, input_bytes=None):
    p = subprocess.run(args, input=input_bytes, capture_output=True, creationflags=_CREATE_NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError("%s failed: %s" % (os.path.basename(args[0]), p.stderr.decode("utf-8", "replace")[-800:]))
    return p


class AudioInfo:
    def __init__(self, path):
        self.path = path
        p = _run([ffmpeg_exe("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                  "-select_streams", "a:0", path])
        j = json.loads(p.stdout.decode("utf-8", "replace"))
        if not j.get("streams"):
            raise ValueError("no audio stream in " + path)
        s = j["streams"][0]
        fmt = j.get("format", {})
        self.codec = s.get("codec_name", "?")
        self.sample_rate = int(s.get("sample_rate", 0) or 0)
        self.channels = int(s.get("channels", 0) or 0)
        bits = s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0
        self.bits = int(bits) if str(bits).isdigit() else 0
        self.sample_fmt = s.get("sample_fmt", "")
        self.bit_rate = int(s.get("bit_rate") or fmt.get("bit_rate") or 0)
        self.duration = float(s.get("duration") or fmt.get("duration") or 0.0)
        tags = {}
        for src in (fmt.get("tags") or {}, s.get("tags") or {}):
            for k, v in src.items():
                tags[k.lower()] = v
        self.title = tags.get("title", "")
        self.artist = tags.get("artist", "") or tags.get("album_artist", "")
        self.album = tags.get("album", "")
        self.lossy = self.codec in ("mp3", "aac", "vorbis", "opus", "wmav2", "wmav1", "mp2", "ac3", "eac3")

    def describe(self):
        bits = ("%d-bit" % self.bits) if self.bits else (self.sample_fmt or "?")
        rate = "%.1f kHz" % (self.sample_rate / 1000.0) if self.sample_rate else "? kHz"
        ch = {1: "mono", 2: "stereo"}.get(self.channels, "%d ch" % self.channels)
        br = (", %d kbps" % (self.bit_rate // 1000)) if self.lossy and self.bit_rate else ""
        return "%s %s %s %s%s, %d:%02d" % (self.codec.upper(), rate, bits, ch, br,
                                           int(self.duration) // 60, int(self.duration) % 60)

    def quality_notes(self):
        """List of (level, text): level 'info' or 'warn'."""
        notes = []
        if self.sample_rate > TARGET_RATE:
            notes.append(("info", "Downsampled %d -> 32000 Hz (the game streams at 32 kHz)." % self.sample_rate))
        elif self.sample_rate < TARGET_RATE:
            notes.append(("warn", "Upsampled %d -> 32000 Hz: quality cannot improve beyond the source."
                          % self.sample_rate))
        if self.channels == 1:
            notes.append(("info", "Mono source: copied to both channels."))
        elif self.channels > 2:
            notes.append(("info", "%d channels downmixed to stereo." % self.channels))
        if self.bits and self.bits < 16 and not self.lossy:
            notes.append(("warn", "%d-bit source: quality cannot improve." % self.bits))
        if self.lossy and self.bit_rate and self.bit_rate < 128000:
            notes.append(("warn", "Low bitrate %d kbps source: artifacts will remain." % (self.bit_rate // 1000)))
        if self.duration > 12 * 60:
            notes.append(("warn", "Very long song (%d min): uses %.0f MB of disc space." %
                          (self.duration // 60, self.duration * 36.6 / 1024)))
        if self.duration and self.duration < 20:
            notes.append(("warn", "Very short song (< 20 s)."))
        notes.append(("info", "Encoded as PS-ADPCM stereo 32 kHz (about 4:1 vs 16-bit PCM)."))
        return notes


_RESAMPLER = None


def resampler():
    """ffmpeg resampler options: soxr (high precision) when this ffmpeg build has it, otherwise ffmpeg's own
    resampler with a long filter (the "essentials" builds that setup.bat downloads come without soxr)."""
    global _RESAMPLER
    if _RESAMPLER is None:
        try:
            p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-buildconf"], capture_output=True,
                               creationflags=_CREATE_NO_WINDOW)
            has_soxr = b"enable-libsoxr" in p.stdout + p.stderr
        except OSError:
            has_soxr = False
        _RESAMPLER = ("resampler=soxr:precision=28" if has_soxr else
                      "resampler=swr:filter_size=256:phase_shift=10:cutoff=0.97:linear_interp=1")
    return _RESAMPLER


def decode(path, rate=TARGET_RATE, filters=None):
    """Decode to float32 (n, 2) at `rate` (soxr if available, else ffmpeg's high-quality resampler)."""
    af = "aresample=%d:%s" % (rate, resampler())
    if filters:
        af = filters + "," + af
    p = _run([ffmpeg_exe(), "-v", "error", "-i", path, "-vn", "-af", af, "-ac", "2", "-f", "f32le", "-"])
    return np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, 2)


def measure_loudness(pcm, rate):
    """Integrated loudness (LUFS) and true-ish peak (dBFS, sample peak of 4x oversampled signal)."""
    import pyloudnorm
    meter = pyloudnorm.Meter(rate)
    x = pcm.astype(np.float64)
    if x.dtype.kind == "i" or np.abs(x).max() > 2.0:
        x = x / 32768.0
    lufs = meter.integrated_loudness(x)
    return float(lufs)


def loudnorm_filter(path, target_lufs, true_peak=-1.0):
    """Two-pass ffmpeg loudnorm (linear = constant gain when possible)."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path, "-vn", "-af",
                        "loudnorm=I=%.1f:TP=%.1f:LRA=20:print_format=json" % (target_lufs, true_peak),
                        "-f", "null", "-"], capture_output=True, creationflags=_CREATE_NO_WINDOW)
    err = p.stderr.decode("utf-8", "replace")
    j = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    return ("loudnorm=I=%.1f:TP=%.1f:LRA=20:measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:"
            "offset=%s:linear=true" % (target_lufs, true_peak, j["input_i"], j["input_tp"], j["input_lra"],
                                       j["input_thresh"], j["target_offset"])), float(j["input_i"])


def prepare(path, target_lufs=None, normalize=True):
    """Decode + (optionally) loudness-match a song -> int16 (n, 2) at 32 kHz, measured input loudness."""
    filt = None
    in_lufs = None
    if normalize and target_lufs is not None:
        filt, in_lufs = loudnorm_filter(path, target_lufs)
    x = decode(path, filters=filt)
    pcm = np.clip(np.round(x * 32767.0), -32768, 32767).astype(np.int16)
    return pcm, in_lufs
