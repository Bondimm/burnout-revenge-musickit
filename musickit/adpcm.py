"""Sony PS-ADPCM (SPU "VAG") codec, mono channel streams of 16-byte frames (28 samples each).

Frame: byte0 = (predictor << 4) | shift, byte1 = flags, 14 bytes = 28 signed 4-bit nibbles (low nibble first).
Decoded sample = clamp16((nibble << 12 >> shift) + (h1 * f0 + h2 * f1 + 32) >> 6).
The encoder searches every predictor/shift pair per frame against the real (quantised, clamped) decoder, so the
decoder used by the PS2 SPU2 reproduces exactly what we measured.
"""
import numpy as np

try:
    from numba import njit
except ImportError:  # pragma: no cover - slow fallback
    def njit(*a, **k):
        def deco(f):
            return f
        return deco(a[0]) if a and callable(a[0]) else deco

F0 = np.array([0, 60, 115, 98, 122], dtype=np.int64)
F1 = np.array([0, 0, -52, -55, -60], dtype=np.int64)


@njit(cache=True)
def _decode(data, out, f0, f1):
    h1 = 0
    h2 = 0
    nframes = len(data) // 16
    for fr in range(nframes):
        b = fr * 16
        pred = (np.int64(data[b]) >> 4) & 0xF
        if pred > 4:
            pred = 0
        shift = np.int64(data[b]) & 0xF
        if shift > 12:
            shift = 9
        c0 = f0[pred]
        c1 = f1[pred]
        for i in range(28):
            byte = np.int64(data[b + 2 + (i >> 1)])
            nib = (byte >> 4) if (i & 1) else (byte & 0xF)
            if nib >= 8:
                nib -= 16
            s = ((nib << 12) >> shift) + ((h1 * c0 + h2 * c1 + 32) >> 6)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            out[fr * 28 + i] = s
            h2 = h1
            h1 = s


def decode(data):
    """PS-ADPCM bytes (multiple of 16) -> int16 numpy array."""
    buf = np.frombuffer(bytes(data), dtype=np.uint8)
    n = len(buf) // 16
    out = np.zeros(n * 28, dtype=np.int64)
    _decode(buf[: n * 16], out, F0, F1)
    return out.astype(np.int16)


@njit(cache=True)
def _encode(pcm, out, f0, f1, flags):
    h1 = 0
    h2 = 0
    nframes = len(pcm) // 28
    q = np.zeros(28, dtype=np.int64)
    bestq = np.zeros(28, dtype=np.int64)
    for fr in range(nframes):
        base = fr * 28
        best_err = -1.0
        best_p = 0
        best_s = 0
        best_h1 = 0
        best_h2 = 0
        for p in range(5):
            c0 = f0[p]
            c1 = f1[p]
            # smallest shift that can represent the ideal residual, then try it and a coarser one around it
            maxr = 0
            a1 = h1
            a2 = h2
            for i in range(28):
                x = pcm[base + i]
                r = x - ((a1 * c0 + a2 * c1 + 32) >> 6)
                if r < 0:
                    r = -r
                if r > maxr:
                    maxr = r
                a2 = a1
                a1 = x
            s0 = 12
            while s0 > 0 and (7 << (12 - s0)) < maxr:
                s0 -= 1
            for s in range(max(0, s0 - 1), min(12, s0 + 1) + 1):
                e1 = h1
                e2 = h2
                err = 0.0
                for i in range(28):
                    x = pcm[base + i]
                    pr = (e1 * c0 + e2 * c1 + 32) >> 6
                    r = x - pr
                    # round to nearest representable nibble
                    step = 1 << (12 - s)
                    if r >= 0:
                        n = (r + (step >> 1)) // step
                    else:
                        n = -((-r + (step >> 1)) // step)
                    if n > 7:
                        n = 7
                    elif n < -8:
                        n = -8
                    y = ((n << 12) >> s) + pr
                    if y > 32767:
                        y = 32767
                    elif y < -32768:
                        y = -32768
                    q[i] = n
                    d = float(x - y)
                    err += d * d
                    e2 = e1
                    e1 = y
                if best_err < 0 or err < best_err:
                    best_err = err
                    best_p = p
                    best_s = s
                    best_h1 = e1
                    best_h2 = e2
                    for i in range(28):
                        bestq[i] = q[i]
        o = fr * 16
        out[o] = (best_p << 4) | best_s
        out[o + 1] = flags
        for i in range(14):
            lo = bestq[2 * i] & 0xF
            hi = bestq[2 * i + 1] & 0xF
            out[o + 2 + i] = lo | (hi << 4)
        h1 = best_h1
        h2 = best_h2


def encode(pcm, flags=2):
    """int16 samples (any length) -> PS-ADPCM bytes, padded with silence to a multiple of 28 samples.
    `flags` is stored in every frame (Burnout's EA Trax streams use 0x02 everywhere)."""
    pcm = np.asarray(pcm, dtype=np.int64)
    n = (len(pcm) + 27) // 28
    padded = np.zeros(n * 28, dtype=np.int64)
    padded[: len(pcm)] = pcm
    out = np.zeros(n * 16, dtype=np.uint8)
    _encode(padded, out, F0, F1, flags)
    return out.tobytes()
