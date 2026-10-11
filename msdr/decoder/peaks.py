"""Auto peak detection over IQ blocks for BAND DECODE (spec P2).

PSD-based: find bins that stand out from the noise floor by threshold_db,
merge nearby bins into one peak per emitter, and only report a peak once it
has been seen in >=2 of the last `history` update() calls -- a transient blip
(adjacent-channel splatter, a burst) shouldn't spawn a channel on its own.
"""

import collections
from dataclasses import dataclass

import numpy as np

# LO leakage sits at DC on this device family (spec "Measured facts"); same
# idea as iqscan's --dc-exclude.
_DC_EXCLUDE_HZ = 3000.0

# 4-term Blackman-Harris coefficients: ~92 dB sidelobes, vs. Hann's ~31 dB.
# A strong signal's own window leakage otherwise clears floor+threshold_db
# tens of kHz away and gets reported back as a second, spurious "peak".
_BH_COEFFS = (0.35875, 0.48829, 0.14128, 0.01168)


def _blackman_harris(n: int) -> np.ndarray:
    a0, a1, a2, a3 = _BH_COEFFS
    x = np.arange(n)
    return (
        a0
        - a1 * np.cos(2 * np.pi * x / (n - 1))
        + a2 * np.cos(4 * np.pi * x / (n - 1))
        - a3 * np.cos(6 * np.pi * x / (n - 1))
    )


@dataclass(frozen=True)
class Peak:
    frequency_hz: float
    snr_db: float
    power_db: float
    width_hz: float


class _Track:
    """One persistent candidate: EMA'd snr + a rolling seen/not-seen window."""

    __slots__ = ("frequency_hz", "power_db", "width_hz", "snr_db", "hits")

    def __init__(
        self, frequency_hz: float, power_db: float, width_hz: float, snr_db: float, history: int
    ):
        self.frequency_hz = frequency_hz
        self.power_db = power_db
        self.width_hz = width_hz
        self.snr_db = snr_db
        self.hits: collections.deque[bool] = collections.deque([True], maxlen=history)


class PeakDetector:
    def __init__(
        self,
        fs: int,
        center_hz: float,
        nfft: int = 65536,
        threshold_db: float = 10.0,
        merge_hz: float = 10000,
        history: int = 10,
    ):
        self.fs = fs
        self.center_hz = center_hz
        self.nfft = nfft
        self.threshold_db = threshold_db
        self.merge_hz = merge_hz
        self.history = history
        self._tracks: list[_Track] = []

    def retune(self, fs: int, center_hz: float) -> None:
        self.fs = fs
        self.center_hz = center_hz
        self._tracks = []

    def update(self, x: np.ndarray) -> list[Peak]:
        n = min(self.nfft, x.size)
        if n > 1:
            block = np.asarray(x[:n], dtype=np.complex64)
            window = _blackman_harris(n).astype(np.float32)
            spectrum = np.fft.fftshift(np.fft.fft(block * window))
            freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / self.fs)) + self.center_hz
            power_db = 20 * np.log10(np.abs(spectrum) + 1e-20)

            floor_db = float(np.median(power_db))
            excluded = np.abs(freqs - self.center_hz) < _DC_EXCLUDE_HZ
            candidate = (power_db > floor_db + self.threshold_db) & ~excluded

            detections = self._merge(freqs, power_db, candidate, floor_db)
            self._match(detections)
        return self._confirmed()

    def _merge(
        self, freqs: np.ndarray, power_db: np.ndarray, candidate: np.ndarray, floor_db: float
    ) -> list["Peak"]:
        idx = np.nonzero(candidate)[0]
        if idx.size == 0:
            return []
        bin_hz = float(freqs[1] - freqs[0])
        gap = max(1, int(round(self.merge_hz / bin_hz)))
        groups = np.split(idx, np.where(np.diff(idx) > gap)[0] + 1)

        peaks = []
        for group in groups:
            weights = np.power(10.0, power_db[group] / 20.0)
            centroid = float(np.average(freqs[group], weights=weights))
            peak_power = float(power_db[group].max())
            width = float(freqs[group].max() - freqs[group].min()) + bin_hz
            peaks.append(Peak(centroid, peak_power - floor_db, peak_power, width))
        return peaks

    def _match(self, detections: list["Peak"]) -> None:
        matched: set[int] = set()
        for detection in detections:
            candidates = [
                t
                for t in self._tracks
                if id(t) not in matched
                and abs(t.frequency_hz - detection.frequency_hz) <= self.merge_hz / 2
            ]
            track = min(
                candidates, key=lambda t: abs(t.frequency_hz - detection.frequency_hz), default=None
            )
            if track is None:
                self._tracks.append(
                    _Track(
                        detection.frequency_hz,
                        detection.power_db,
                        detection.width_hz,
                        detection.snr_db,
                        self.history,
                    )
                )
                continue
            track.frequency_hz = detection.frequency_hz
            track.power_db = detection.power_db
            track.width_hz = detection.width_hz
            track.snr_db = 0.3 * detection.snr_db + 0.7 * track.snr_db
            track.hits.append(True)
            matched.add(id(track))

        for track in self._tracks:
            if id(track) not in matched:
                track.hits.append(False)
        # Bound the tracked set: drop anything not seen at all in the window.
        self._tracks = [t for t in self._tracks if any(t.hits)]

    def _confirmed(self) -> list[Peak]:
        result = [
            Peak(t.frequency_hz, t.snr_db, t.power_db, t.width_hz)
            for t in self._tracks
            if sum(t.hits) >= 2
        ]
        result.sort(key=lambda p: p.snr_db, reverse=True)
        return result
