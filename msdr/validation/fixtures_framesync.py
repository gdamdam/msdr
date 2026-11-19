"""Synthetic FSK streams for the frame-sync search (msdr.survey.framesync).

These carry a system's sync pattern at its frame period with *random* symbols
in between: enough to exercise the sync search, not a valid frame of any
system -- no decoder would accept them.
"""

from __future__ import annotations

import numpy as np

RATE_HZ = 48_000


def framed_levels(
    sync: np.ndarray,
    period: int,
    frames: int,
    *,
    seed: int = 0,
    two_level: bool = False,
    offset: int = 7,
) -> np.ndarray:
    """`frames` frames of `period` random symbols, `sync` written into each
    at `offset`. Four-level payload (+-1, +-3) unless `two_level` (+-3 only)."""
    rng = np.random.default_rng(seed)
    alphabet = np.array([-3, 3] if two_level else [-3, -1, 1, 3], dtype=np.int8)
    out = rng.choice(alphabet, size=frames * period)
    scale = 3 if two_level else 1  # two-level sync patterns are given as +-1
    for frame in range(frames):
        start = frame * period + offset
        out[start : start + len(sync)] = sync * scale
    return out


def random_levels(count: int, *, seed: int = 0, two_level: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    alphabet = np.array([-3, 3] if two_level else [-3, -1, 1, 3], dtype=np.int8)
    return rng.choice(alphabet, size=count)


def fsk_iq(
    levels: np.ndarray,
    *,
    baud: int,
    rate: int = RATE_HZ,
    deviation_hz: float = 1_944.0,
    offset_hz: float = 0.0,
    amplitude: float = 0.5,
) -> np.ndarray:
    """Frequency-modulate symbol `levels` (+-3 = +-`deviation_hz`), each
    symbol's edges rounded by a Hann pulse so it is not an ideal square."""
    sps = rate // baud
    if sps * baud != rate:
        raise ValueError("rate must be a whole multiple of baud")
    square = np.repeat(levels.astype(np.float64), sps)
    pulse = np.hanning(sps + 2)[1:-1]
    shaped = np.convolve(square, pulse / pulse.sum(), mode="same")
    freq = shaped * (deviation_hz / 3.0) + offset_hz
    phase = 2 * np.pi * np.cumsum(freq) / rate
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)
