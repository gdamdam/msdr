"""OccupancyMap: per-bin fraction of frames whose level exceeds the noise
floor, over two rolling windows. Cheap enough to run at ~23 frames/s forever:
a fixed-size ring of 0/1 activity per bin plus a running per-bin sum, so
`snapshot()` is O(bins) regardless of window length -- no re-scanning the
whole window on every frame.
"""

import numpy as np

_BINS = 512
# 60s / 600s at the measured ~23 frames/s (docs/research/2026-09-22-sdrconnect-
# websocket-api.md Sec12).
_WINDOWS_FRAMES = (1_380, 13_800)
# threshold_db=8.0 (settings default) at ~0.49 dB/uint8 unit -- see survey.py's
# own conversion for the general case; this default matches settings' default.
_THRESHOLD_UNITS = 16


class OccupancyMap:
    def __init__(
        self,
        bins: int = _BINS,
        windows_frames: tuple[int, int] = _WINDOWS_FRAMES,
        threshold_units: int = _THRESHOLD_UNITS,
    ) -> None:
        self._bins = bins
        self._threshold_units = threshold_units
        self._windows_frames = windows_frames
        self._short = _Ring(bins, windows_frames[0])
        self._long = _Ring(bins, windows_frames[1])
        self._floor = 0.0

    def update(self, frame: bytes) -> None:
        levels = np.frombuffer(frame, dtype=np.uint8)
        if levels.size != self._bins:
            return  # malformed frame -- ignore rather than crash the pump
        floor = float(np.median(levels))
        self._floor = floor
        active = (levels.astype(np.int16) > floor + self._threshold_units).astype(np.uint8)
        self._short.push(active)
        self._long.push(active)

    @property
    def floor(self) -> int:
        """Current per-frame floor (uint8 units), rounded -- shared with
        SpectrumPeakTracker so both use the same noise-floor estimate."""
        return int(round(self._floor))

    def snapshot(self) -> dict:
        return {
            "short": self._short.fractions(),
            "long": self._long.fractions(),
            "floor": self.floor,
        }

    def reset(self) -> None:
        self._short.reset()
        self._long.reset()
        self._floor = 0.0


class _Ring:
    """One rolling window: a (size, bins) uint8 ring of 0/1 activity plus a
    running per-bin sum, updated incrementally (subtract the slot being
    overwritten, add the new one) instead of resumming the whole ring."""

    def __init__(self, bins: int, size: int) -> None:
        self._buffer = np.zeros((size, bins), dtype=np.uint8)
        self._sums = np.zeros(bins, dtype=np.int64)
        self._size = size
        self._pos = 0
        self._count = 0

    def push(self, active: np.ndarray) -> None:
        idx = self._pos % self._size
        self._sums += active.astype(np.int64) - self._buffer[idx].astype(np.int64)
        self._buffer[idx] = active
        self._pos += 1
        self._count = min(self._count + 1, self._size)

    def fractions(self) -> list[float]:
        denom = self._count if self._count else 1
        return (self._sums / denom).tolist()

    def reset(self) -> None:
        self._buffer[:] = 0
        self._sums[:] = 0
        self._pos = 0
        self._count = 0
