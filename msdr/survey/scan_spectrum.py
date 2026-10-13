"""Resolution-aware stitched spectrum and bounded representative time slices."""

from __future__ import annotations

import math


def _merge(intervals):
    merged = []
    for low, high in sorted(intervals):
        if merged and low <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    return merged


class SpectrumCapture:
    def __init__(
        self,
        start_hz: int,
        end_hz: int,
        *,
        native_bin_width_hz=None,
        width: int | None = None,
        max_slices: int = 256,
    ):
        self.start_hz, self.end_hz = start_hz, end_hz
        native = native_bin_width_hz or (end_hz - start_hz) / (width or 512)
        self.width = max(1, min(8192, width or math.ceil((end_hz - start_hz) / native)))
        self.max_slices = max(4, max_slices)
        self.bin_width_hz = (end_hz - start_hz) / self.width
        self.native_bin_width_hz = native
        self.sums = [0.0] * self.width
        self.maxima = [None] * self.width
        self.counts = [0] * self.width
        # Distance of each bin from the centre of the slice its average came
        # from, as a fraction of the half-span. Overlapping slices hand a bin to
        # the one that saw it nearer its centre: a slice's edges sit on the
        # receiver filter's roll-off and would drag the stitched level down.
        self._owner_distance = [math.inf] * self.width
        self.first_at = [None] * self.width
        self.last_at = [None] * self.width
        self.slices = []
        self.frames = 0
        self.intervals = []
        self._slice_stride = 1
        self._slice_count = 0
        self._last_slice_at = None
        self._latest_slice = None
        self._has_center_exclusion = False

    def add(self, spectrum: dict) -> list[list[float]]:
        bins = spectrum.get("bins")
        if bins is None or not len(bins):
            return []
        center, span, at = (spectrum.get(k) for k in ("center_hz", "span_hz", "at"))
        if (
            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (center, span, at))
            or span <= 0
        ):
            return []
        n = len(bins)
        exclusion = max(-1, int(spectrum.get("center_exclusion_bins", -1)))
        self._has_center_exclusion |= exclusion >= 0
        step = span / n
        first = center - span / 2
        values = []
        intervals = []
        for index, value in enumerate(bins):
            # SpectrumFrame's frequency convention is i * span / N, without
            # a half-bin offset. Its cell occupies [frequency, frequency+step).
            hz = first + index * step
            low, high = max(self.start_hz, hz), min(self.end_hz, hz + step)
            usable = (
                abs(index - n // 2) > exclusion
                and isinstance(value, (int, float))
                and math.isfinite(value)
            )
            values.append(float(value) if usable else None)
            if not usable or high <= low:
                continue
            intervals.append([low, high])
            # A coarse overview bin may contain several source bins; preserve
            # their max and average, never fabricate extra native resolution.
            position = min(self.width - 1, int((low - self.start_hz) / self.bin_width_hz))
            middle = self.start_hz + (position + 0.5) * self.bin_width_hz
            distance = abs(middle - center) / (span / 2)
            owner = self._owner_distance[position]
            if distance < owner - 1e-9:
                self._owner_distance[position] = distance
                self.sums[position], self.counts[position] = 0.0, 0
                owner = distance
            if distance <= owner + 1e-9:
                self.sums[position] += float(value)
                self.counts[position] += 1
            # The maximum stays a peak hold across every slice, edges included.
            previous = self.maxima[position]
            self.maxima[position] = (
                float(value) if previous is None else max(float(value), previous)
            )
            if self.first_at[position] is None:
                self.first_at[position] = at
            self.last_at[position] = at
        intervals = _merge(intervals)
        if not intervals:
            return []
        self.intervals = _merge(self.intervals + intervals)
        self.frames += 1
        slice_ = {
            "at": at,
            "center_hz": center,
            "span_hz": span,
            "first_bin_hz": first,
            "bin_width_hz": step,
            "values_db": values,
            "center_exclusion_bins": exclusion,
        }
        self._latest_slice = slice_
        # Sample at most once per second, then halve temporal density as the
        # bounded history fills. Unlike a ring buffer this retains early bands.
        if self._last_slice_at is None or at - self._last_slice_at >= 1:
            self._last_slice_at = at
            index = self._slice_count
            self._slice_count += 1
            if index % self._slice_stride == 0:
                if len(self.slices) >= self.max_slices - 1:
                    self.slices = self.slices[::2]
                    self._slice_stride *= 2
                if index % self._slice_stride == 0:
                    self.slices.append(slice_)
        return intervals

    def export(self):
        bins = [
            {
                "frequency_hz": self.start_hz + i * self.bin_width_hz,
                "typical_db": self.sums[i] / count,
                "max_db": self.maxima[i],
                "sample_count": count,
                "first_at": self.first_at[i],
                "last_at": self.last_at[i],
            }
            for i, count in enumerate(self.counts)
            if count
        ]
        holes = []
        hole_reason = ("uncaptured or center-excluded spectrum" if self._has_center_exclusion
                       else "uncaptured spectrum")
        cursor = self.start_hz
        for low, high in self.intervals:
            if low > cursor:
                holes.append(
                    {
                        "start_hz": cursor,
                        "end_hz": low,
                        "reason": hole_reason,
                    }
                )
            cursor = max(cursor, high)
        if cursor < self.end_hz:
            holes.append(
                {
                    "start_hz": cursor,
                    "end_hz": self.end_hz,
                    "reason": "uncaptured or center-excluded spectrum",
                }
            )
        slices = list(self.slices)
        if self._latest_slice is not None and (
            not slices or slices[-1]["at"] != self._latest_slice["at"]
        ):
            slices.append(self._latest_slice)
        return {
            "available": bool(bins),
            "start_hz": self.start_hz,
            "end_hz": self.end_hz,
            "bin_width_hz": self.bin_width_hz,
            "native_bin_width_hz": self.native_bin_width_hz,
            "bins": bins,
            "slices": slices,
            "holes": holes,
            "captured_frames": self.frames,
            "units": "relative_db",
            "typical_statistic": "mean_display_db",
            "time_history_sampled": True,
            "slice_interval_s": self._slice_stride,
            "time_slice_candidates": self._slice_count,
        }
