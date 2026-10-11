"""Numpy-only IQ spectrograms with explicit pixel-to-signal coordinates.

Rows run forward in time (top to bottom); columns run from low to high
frequency after ``fftshift``. Pixel boxes use half-open edges ``(x0, y0,
x1, y1)``. Frequency edges lie half a FFT bin beyond bin centers. Time edges
cover the union of complete FFT windows represented by the selected rows:
the first row starts at ``y0 * hop_length`` and the last ends at
``(y1 - 1) * hop_length + n_fft`` (end-exclusive).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class SpectrogramBox:
    """Signal coordinates covered by a half-open spectrogram pixel box."""

    frequency_start_hz: float
    frequency_end_hz: float
    time_start_s: float
    time_end_s: float
    sample_start: int
    sample_end: int


@dataclass(frozen=True, slots=True)
class SpectrogramMetadata:
    """Immutable acquisition and grid metadata for an IQ spectrogram."""

    sample_rate_hz: float
    center_frequency_hz: float
    n_fft: int
    hop_length: int
    input_samples: int
    frame_count: int
    frequency_bins: int
    dynamic_range_db: float

    @property
    def bin_width_hz(self) -> float:
        return self.sample_rate_hz / self.n_fft

    @property
    def window_duration_s(self) -> float:
        return self.n_fft / self.sample_rate_hz

    def box_to_coordinates(self, box: tuple[int, int, int, int]) -> SpectrogramBox:
        """Map ``(x0, y0, x1, y1)`` pixel edges to Hz, seconds and IQ bounds."""
        if (
            not isinstance(box, tuple)
            or len(box) != 4
            or any(type(value) is not int for value in box)
        ):
            raise ValueError("box must be a tuple of four integer pixel edges")
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= self.frequency_bins):
            raise ValueError("frequency pixel edges are outside the spectrogram")
        if not (0 <= y0 < y1 <= self.frame_count):
            raise ValueError("time pixel edges are outside the spectrogram")

        half_bin = self.bin_width_hz / 2
        first_bin_center = self.center_frequency_hz - self.sample_rate_hz / 2
        start = y0 * self.hop_length
        end = (y1 - 1) * self.hop_length + self.n_fft
        return SpectrogramBox(
            frequency_start_hz=first_bin_center + x0 * self.bin_width_hz - half_bin,
            frequency_end_hz=first_bin_center + x1 * self.bin_width_hz - half_bin,
            time_start_s=start / self.sample_rate_hz,
            time_end_s=end / self.sample_rate_hz,
            sample_start=start,
            sample_end=end,
        )


@dataclass(frozen=True, slots=True)
class IQSpectrogram:
    """Read-only dBFS magnitude grid and its coordinate metadata."""

    dbfs: np.ndarray
    metadata: SpectrogramMetadata


def iq_spectrogram(
    iq: np.ndarray,
    *,
    sample_rate_hz: float,
    center_frequency_hz: float,
    n_fft: int = 256,
    hop_length: int | None = None,
    dynamic_range_db: float = 80.0,
) -> IQSpectrogram:
    """Compute a full-frame, Hann-windowed IQ spectrogram in fixed dBFS.

    FFT magnitudes are divided by the periodic Hann coherent gain (window
    sum), so a bin-centered complex tone of amplitude one reads 0 dBFS.
    Values are floored at ``-dynamic_range_db``; no per-image contrast scaling
    or frame padding is used. The FFT is centered with ``fftshift``.
    """
    data = np.asarray(iq)
    if data.ndim != 1 or not np.iscomplexobj(data):
        raise ValueError("IQ must be a one-dimensional complex array")
    if not np.isfinite(data).all():
        raise ValueError("IQ contains non-finite samples")
    if isinstance(n_fft, bool) or not isinstance(n_fft, int) or n_fft < 2 or n_fft % 2:
        raise ValueError("n_fft must be an even integer of at least 2")
    if hop_length is None:
        hop_length = n_fft // 2
    if isinstance(hop_length, bool) or not isinstance(hop_length, int) or hop_length < 1:
        raise ValueError("hop_length must be a positive integer")
    if len(data) < n_fft:
        raise ValueError("IQ is shorter than one complete FFT window")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if not math.isfinite(center_frequency_hz):
        raise ValueError("center_frequency_hz must be finite")
    if not math.isfinite(dynamic_range_db) or dynamic_range_db <= 0:
        raise ValueError("dynamic_range_db must be finite and positive")

    starts = np.arange(0, len(data) - n_fft + 1, hop_length)
    frames = np.asarray(data[starts[:, None] + np.arange(n_fft)], dtype=np.complex128)
    # Periodic Hann: the standard spectral-analysis window with denominator N.
    window = np.hanning(n_fft + 1)[:-1]
    spectrum = np.fft.fftshift(np.fft.fft(frames * window, axis=1), axes=1)
    magnitude = np.abs(spectrum) / window.sum()
    floor = 10.0 ** (-dynamic_range_db / 20.0)
    dbfs = (20.0 * np.log10(np.maximum(magnitude, floor))).astype(np.float32)
    dbfs.setflags(write=False)
    metadata = SpectrogramMetadata(
        sample_rate_hz=float(sample_rate_hz),
        center_frequency_hz=float(center_frequency_hz),
        n_fft=n_fft,
        hop_length=hop_length,
        input_samples=len(data),
        frame_count=len(starts),
        frequency_bins=n_fft,
        dynamic_range_db=float(dynamic_range_db),
    )
    return IQSpectrogram(dbfs=dbfs, metadata=metadata)
