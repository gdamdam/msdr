"""Live, within-span watch measurements and span grouping helpers.

This module reports spectrum power only. A spectrum frame can measure many
frequencies, but it does not decode them; ``decode_target_hz`` names one
priority target at a time for a caller that has an actual decoder/tune path.
Out-of-span targets remain visible as uncovered and never get invented data.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from msdr.radio.protocol import SpectrumFrame

_BINS = 512
_CENTER_BIN = 256
_DC_GUARD_BINS = 2


@dataclass(frozen=True)
class SpanGroup:
    center_hz: int
    frequencies_hz: tuple[int, ...]

    def as_dict(self) -> dict:
        return {"center_hz": self.center_hz, "frequencies_hz": list(self.frequencies_hz)}


def plan_span_groups(
    frequencies_hz: Iterable[int],
    sample_rate_hz: int,
    *,
    edge_margin_fraction: float = 0.03,
    max_groups: int | None = None,
) -> list[SpanGroup]:
    """Greedily group sorted watch frequencies into spans with usable edges.

    ``edge_margin_fraction`` is a fraction of the full sample rate reserved
    at each edge. A group's midpoint is its suggested receiver center. This
    plans coverage only; callers must confirm every retune before marking a
    group observed.
    """
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if not 0 <= edge_margin_fraction < 0.5:
        raise ValueError("edge_margin_fraction must be in [0, 0.5)")
    if max_groups is not None and max_groups <= 0:
        raise ValueError("max_groups must be positive")
    frequencies = sorted({int(freq) for freq in frequencies_hz})
    if any(freq <= 0 for freq in frequencies):
        raise ValueError("frequencies must be positive")
    bin_width = sample_rate_hz / _BINS
    dc_guard_hz = (_DC_GUARD_BINS + 1) * bin_width
    # Keep slack on either side of a midpoint group so the suggested center
    # can be nudged out of the DC guard without pushing a watched edge out of
    # the usable band.
    usable_width = sample_rate_hz * (1.0 - 2.0 * edge_margin_fraction) - 2 * dc_guard_hz
    if usable_width <= 0:
        raise ValueError("sample rate is too small for the requested margins and DC guard")
    groups: list[SpanGroup] = []
    current: list[int] = []
    for frequency in frequencies:
        if current and frequency - current[0] > usable_width:
            groups.append(_make_span_group(current, dc_guard_hz))
            current = []
        current.append(frequency)
    if current:
        groups.append(_make_span_group(current, dc_guard_hz))
    if max_groups is not None and len(groups) > max_groups:
        raise ValueError(f"frequencies require {len(groups)} span groups (max {max_groups})")
    return groups


def _make_span_group(frequencies: list[int], dc_guard_hz: float) -> SpanGroup:
    center = (frequencies[0] + frequencies[-1]) // 2
    if any(abs(freq - center) <= dc_guard_hz for freq in frequencies):
        center += math.ceil(dc_guard_hz) + 1
    return SpanGroup(center, tuple(frequencies))


class WithinBandWatcher:
    """Measure configured frequencies from confirmed spectrum frames.

    The receiver center and sample rate must be supplied from confirmed state
    for each frame. ``bytes`` and integer sequences are the radio's 0..255
    spectrum levels; conversion matches Survey's current 255-units-per-range
    mapping (default dynamic range 125 dB). A float sequence is treated as
    already converted dB values. SNR is local peak dB minus the median noise
    floor, after excluding DC and watched-signal bins.
    """

    def __init__(
        self,
        frequencies_hz: Iterable[int] = (),
        *,
        hold_s: float = 1.0,
        activity_threshold_db: float = 10.0,
        stop_threshold_db: float = 6.0,
        edge_margin_fraction: float = 0.03,
        spectrum_base_db: float = -145.0,
        dynamic_range_db: float = 125.0,
        channel_width_hz: int | None = None,
    ) -> None:
        if hold_s < 0 or not math.isfinite(hold_s):
            raise ValueError("hold_s must be finite and nonnegative")
        if not math.isfinite(activity_threshold_db) or not math.isfinite(stop_threshold_db):
            raise ValueError("activity thresholds must be finite")
        if activity_threshold_db <= stop_threshold_db:
            raise ValueError("activity_threshold_db must exceed stop_threshold_db")
        if not 0 <= edge_margin_fraction < 0.5:
            raise ValueError("edge_margin_fraction must be in [0, 0.5)")
        if dynamic_range_db <= 0 or not math.isfinite(dynamic_range_db):
            raise ValueError("dynamic_range_db must be finite and positive")
        if channel_width_hz is not None and channel_width_hz <= 0:
            raise ValueError("channel_width_hz must be positive")
        self.hold_s = hold_s
        self.activity_threshold_db = activity_threshold_db
        self.stop_threshold_db = stop_threshold_db
        self.edge_margin_fraction = edge_margin_fraction
        self.spectrum_base_db = spectrum_base_db
        self.dynamic_range_db = dynamic_range_db
        self.channel_width_hz = channel_width_hz
        self._frequencies: tuple[int, ...] = ()
        self._priority_hz: int | None = None
        self._selected_hz: int | None = None
        self._hold_until = 0.0
        self._last_frame_at: float | None = None
        self._center_hz: int | None = None
        self._sample_rate_hz: int | None = None
        self._measurements: dict[int, dict] = {}
        self.configure(frequencies_hz)

    def configure(self, frequencies_hz: Iterable[int]) -> None:
        frequencies = tuple(sorted({int(freq) for freq in frequencies_hz}))
        if any(freq <= 0 for freq in frequencies):
            raise ValueError("frequencies must be positive")
        self._frequencies = frequencies
        self._measurements = {freq: self._measurements.get(freq, {"frequency_hz": freq,
            "covered": False, "snr_db": None, "level_db": None, "last_seen": None})
            for freq in frequencies}
        if self._priority_hz not in frequencies:
            self._priority_hz = None
        if self._selected_hz not in frequencies:
            self._selected_hz = None

    def set_priority(self, frequency_hz: int | None) -> None:
        if frequency_hz is not None and int(frequency_hz) not in self._frequencies:
            raise ValueError("priority frequency must be one of the watched frequencies")
        self._priority_hz = int(frequency_hz) if frequency_hz is not None else None

    def invalidate(self, reason: str = "awaiting_fresh_frame") -> None:
        """Drop all measurements after a retune or reconnect boundary."""
        self._selected_hz = None
        self._hold_until = 0.0
        for frequency in self._frequencies:
            self._measurements[frequency] = {
                "frequency_hz": frequency,
                "covered": False,
                "active": False,
                "activity_state": "uncovered",
                "snr_db": None,
                "level_db": None,
                "last_seen": None,
                "reason": reason,
            }

    def consume(
        self,
        frame: SpectrumFrame | bytes | bytearray | Sequence[int] | Sequence[float],
        *,
        center_hz: int,
        sample_rate_hz: int,
    ) -> dict:
        """Measure every watched frequency that this actual frame covers."""
        bins, at, raw_levels = _frame_values(frame)
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if len(bins) != _BINS:
            raise ValueError(f"spectrum frame must contain {_BINS} bins")
        if not math.isfinite(at):
            raise ValueError("frame timestamp must be finite")
        center_hz, sample_rate_hz = int(center_hz), int(sample_rate_hz)
        self._last_frame_at = at
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        bin_width = sample_rate_hz / _BINS
        half_usable = sample_rate_hz * (0.5 - self.edge_margin_fraction)
        db_bins = _to_db(bins, raw_levels=raw_levels, spectrum_base_db=self.spectrum_base_db,
                         dynamic_range_db=self.dynamic_range_db)
        resolution_hz = bin_width
        target_bins: dict[int, int] = {}
        for frequency in self._frequencies:
            offset = frequency - center_hz
            index = int(round(_CENTER_BIN + offset / bin_width))
            usable = abs(offset) <= half_usable and 0 <= index < _BINS
            dc = abs(index - _CENTER_BIN) <= _DC_GUARD_BINS
            was_active = bool(self._measurements[frequency].get("active"))
            self._measurements[frequency] = {
                "frequency_hz": frequency,
                "covered": bool(usable),
                "active": was_active if usable else False,
                "activity_state": "pending" if usable else "uncovered",
                "snr_db": None,
                "level_db": None,
                "last_seen": at if usable else self._measurements[frequency].get("last_seen"),
                "reason": "dc_guard" if usable and dc else None if usable else "outside_span",
            }
            if usable and not dc:
                target_bins[frequency] = index
        overlapping = {
            freq for freq, index in target_bins.items()
            if any(other != freq and abs(index - other_index) <= 2
                   for other, other_index in target_bins.items())
        }
        too_narrow = self.channel_width_hz is not None and self.channel_width_hz < bin_width
        excluded = set(range(max(0, _CENTER_BIN - _DC_GUARD_BINS),
                             min(_BINS, _CENTER_BIN + _DC_GUARD_BINS + 1)))
        for index in target_bins.values():
            excluded.update(range(max(0, index - 2), min(_BINS, index + 3)))
        noise_values = [value for idx, value in enumerate(db_bins) if idx not in excluded]
        noise_floor = statistics.median(noise_values) if noise_values else None
        for frequency, index in target_bins.items():
            local = db_bins[max(0, index - 1):min(_BINS, index + 2)]
            level = max(local)
            self._measurements[frequency].update({
                "level_db": round(level, 2),
                "snr_db": round(level - noise_floor, 2) if noise_floor is not None else None,
                "noise_floor_db": round(noise_floor, 2) if noise_floor is not None else None,
                "resolution_hz": round(resolution_hz, 2),
                "independent": frequency not in overlapping and not too_narrow,
                "resolution_warning": (
                    "channel_narrower_than_spectrum_bin" if too_narrow else
                    "overlaps_another_watched_window" if frequency in overlapping else None
                ),
            })
            previous_active = self._measurements[frequency]["active"]
            snr = self._measurements[frequency]["snr_db"]
            resolvable = frequency not in overlapping and not too_narrow
            self._measurements[frequency]["active"] = bool(resolvable and (
                snr is not None and (
                    snr >= self.activity_threshold_db
                    or (previous_active and snr > self.stop_threshold_db)
                )
            ))
            self._measurements[frequency]["activity_state"] = (
                "unresolved" if not resolvable else
                "active" if self._measurements[frequency]["active"] else "quiet"
            )
        self._choose_target(at)
        return self.status()

    def status(self) -> dict:
        return {
            "center_hz": self._center_hz,
            "sample_rate_hz": self._sample_rate_hz,
            "resolution_hz": (
                self._sample_rate_hz / _BINS if self._sample_rate_hz is not None else None
            ),
            "last_frame_at": self._last_frame_at,
            "decode_target_hz": self._selected_hz,
            "priority_frequency_hz": self._priority_hz,
            "hold_until": self._hold_until if self._selected_hz is not None else None,
            "measurements": [dict(self._measurements[freq]) for freq in self._frequencies],
            "decode_concurrency": 1,
        }

    def _choose_target(self, at: float) -> None:
        available = {
            freq: row for freq, row in self._measurements.items()
            if row["covered"] and row["active"] and row["snr_db"] is not None
        }
        if not available:
            # A target leaving the confirmed span drops immediately; the hold
            # never claims coverage or decoding beyond the actual frame.
            self._selected_hz = None
            self._hold_until = at
            return
        if self._priority_hz in available:
            chosen = self._priority_hz
        elif self._selected_hz in available and at < self._hold_until:
            return
        else:
            chosen = max(available, key=lambda freq: (available[freq]["snr_db"], -freq))
        if chosen != self._selected_hz:
            self._selected_hz = chosen
            self._hold_until = at + self.hold_s
        elif self._priority_hz is None:
            # Renew a selection that remains the strongest when its dwell
            # expires, avoiding frame-to-frame swaps on tiny SNR variations.
            self._hold_until = at + self.hold_s


def _frame_values(frame) -> tuple[list[float], float, bool]:
    if isinstance(frame, SpectrumFrame):
        raw, at = frame.bins, frame.at
    else:
        raw, at = frame, 0.0
    if isinstance(raw, (bytes, bytearray)):
        return [float(value) for value in raw], float(at), True
    values = list(raw)
    if values and all(isinstance(value, int) and 0 <= value <= 255 for value in values):
        return [float(value) for value in values], float(at), True
    converted = [float(value) for value in values]
    if any(not math.isfinite(value) for value in converted):
        raise ValueError("spectrum values must be finite")
    return converted, float(at), False


def _to_db(
    values: list[float], *, raw_levels: bool, spectrum_base_db: float, dynamic_range_db: float
) -> list[float]:
    if raw_levels:
        scale = dynamic_range_db / 255.0
        return [spectrum_base_db + value * scale for value in values]
    return values
