"""Generate synthetic, copyright-free test songs (chords + a 1 kHz/1.5 kHz marker pattern for fingerprinting)."""
import os
import sys

import numpy as np
import soundfile as sf


def chord_song(seconds=30.0, rate=44100, seed=1):
    t = np.arange(int(seconds * rate)) / rate
    prog = [(261.63, 329.63, 392.00), (220.00, 261.63, 329.63), (174.61, 220.00, 261.63), (196.00, 246.94, 293.66)]
    left = np.zeros_like(t)
    right = np.zeros_like(t)
    beat = 0.5
    for k in range(int(seconds / 2)):
        f = prog[k % 4]
        seg = (t >= 2 * k) & (t < 2 * k + 2)
        tt = t[seg] - 2 * k
        env = np.minimum(1, tt * 20) * np.exp(-tt * 0.8)
        left[seg] += env * sum(np.sin(2 * np.pi * x * tt) for x in f) / 3
        right[seg] += env * sum(np.sin(2 * np.pi * x * 1.003 * tt + 0.5) for x in f) / 3
    # marker: alternating 1000/1500 Hz beeps every beat (easy to find in an audio dump)
    ph = (t % beat) < 0.12
    mk = np.where((t // beat) % 2 == 0, np.sin(2 * np.pi * 1000 * t), np.sin(2 * np.pi * 1500 * t)) * ph * 0.35
    left += mk
    right += mk
    x = np.stack([left, right], 1) * 0.5
    return x.astype(np.float32)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    sf.write(os.path.join(out_dir, "test_chords_44k_24bit.flac"), chord_song(30.0, 44100), 44100, subtype="PCM_24")
    sf.write(os.path.join(out_dir, "test_chords_22k_mono.wav"), chord_song(20.0, 22050)[:, 0], 22050, subtype="PCM_16")
    print("written to", out_dir)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "data"))
