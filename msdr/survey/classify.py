"""Spectrum-only classifiers (S2): bandwidth class from peak width and
frame resolution, activity class from a TrackedPeak's on/off history. No IQ,
no protocol knowledge -- just what the 512-bin frame can honestly support
(see the design doc's "Constraints and honesty rules": unresolved beats a
guess when the span is too coarse to tell)."""

import statistics
from itertools import pairwise

from msdr.survey.peaks_spectrum import TrackedPeak

_PERIODIC_MIN_RUNS = 3
_PERIODIC_MAX_CV = 0.35
_NEW_OBSERVED_S = 5.0
_STEADY_DUTY = 0.9
_BURSTY_DUTY = 0.5

_FLAT_TOP_MIN_BINS = 4  # need at least this many profile bins to judge shape
_FLAT_TOP_MAX_SPREAD = 3.0  # dB-equivalent units across the peak -> "flat"
_FM_BAND_LO_HZ = 87_500_000.0
_FM_BAND_HI_HZ = 108_000_000.0
_FM_BAND_MIN_WIDTH_HZ = 25_000.0  # coarse resolution often measures FM as only 30-60 kHz wide


def _is_flat_top(profile) -> bool:
    """Digital wideband signals (DAB, LTE, DVB) sit flat across the peak;
    analogue FM broadcasts are peaked/rounded. A profile shorter than
    _FLAT_TOP_MIN_BINS can't show a plateau, so it reads as not-flat."""
    values = list(profile)
    if len(values) < _FLAT_TOP_MIN_BINS:
        return False
    return (max(values) - min(values)) <= _FLAT_TOP_MAX_SPREAD


def bandwidth_class_from_occupied(
    occ_bw_hz: float | None, modulation_kind: str | None
) -> str | None:
    """BW class from the analyzer's IQ-measured occupied bandwidth (20 dB
    down, at ~1 Hz resolution) -- for labels the spectrum can't resolve: at
    wide spans a bin is wider than a whole NFM channel, so bandwidth_class is
    honestly "unresolved" for every narrow signal. Same bands as
    bandwidth_class; None when there's nothing to go on."""
    if occ_bw_hz is None:
        return None
    if modulation_kind == "carrier" or occ_bw_hz < 200.0:
        return "carrier"
    if occ_bw_hz < 4000.0:
        return "narrow"
    if occ_bw_hz <= 20000.0:
        return "am" if modulation_kind == "am" else "nfm"
    return None


def bandwidth_class(
    width_hz: float,
    resolution_hz: float,
    width_bins: int,
    center_hz: float | None = None,
    profile: list[float] | None = None,
) -> str:
    # A single bin only reads as "one narrow carrier" when the span is fine
    # enough to trust it (resolution <= 4 kHz); at coarser spans one strong
    # bin could hide anything narrower than the bin, so it falls through to
    # "unresolved" in the width_bins <= 2 check below instead of guessing.
    if width_bins == 1 and resolution_hz <= 4000:
        return "carrier"
    if width_bins <= 2:
        return "unresolved"
    if width_hz < 4000:
        return "narrow"
    if width_hz <= 20000:
        if 5000 <= width_hz <= 10000 and resolution_hz <= 2000:
            return "am"
        return "nfm"

    if profile is None:
        # No shape info supplied -- today's unconditional thresholds.
        if 100000 <= width_hz <= 300000:
            return "wfm"
        if width_hz > 25000:
            return "wide_digital"
        return "unresolved"  # 20-25 kHz: no band claims this width outright

    flat = _is_flat_top(profile)
    in_fm_band = center_hz is not None and _FM_BAND_LO_HZ <= center_hz <= _FM_BAND_HI_HZ

    # In the FM broadcast band, a peaked/rounded (not flat-topped) signal is
    # analogue FM even when coarse resolution measures it as narrow as 25 kHz.
    if in_fm_band and not flat:
        return "wfm" if width_hz >= _FM_BAND_MIN_WIDTH_HZ else "unresolved"

    if 100000 <= width_hz <= 300000:
        return "wide_digital" if flat else "wfm"
    if width_hz > 25000:
        return "wide_digital" if flat else "unresolved"
    return "unresolved"  # 20-25 kHz: no band claims this width outright


def _active_seconds(intervals: list[tuple[float, float]]) -> float:
    """Union overlapping/adjacent intervals before summing. A track merge
    can hand the surviving track a closed on_run that covers time already
    counted by its own still-open run (e.g. two tracks representing the
    same signal get merged while one was open and the other had just
    closed) -- summing them separately would double-count that overlap."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_e is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def activity_class(track: TrackedPeak, now: float, history_s: float) -> dict:
    window_start = max(track.first_seen, now - history_s)
    observed = max(now - window_start, 0.0)

    intervals = [(max(s, window_start), min(e, now)) for s, e in track.on_runs if e >= window_start]
    if track.active_now:
        intervals.append((max(track.run_start, window_start), now))
    active_time = _active_seconds(intervals)
    # Clamp as a safety net: the union above removes the known double-count
    # source, but duty is a probability and must never report over 100%.
    duty = min(1.0, active_time / observed) if observed > 0 else 0.0

    bursts = len(track.on_runs)
    run_lengths = [e - s for s, e in track.on_runs]
    burst_s = float(statistics.median(run_lengths)) if run_lengths else None

    period_s = None
    if bursts >= _PERIODIC_MIN_RUNS:
        starts = [s for s, _e in track.on_runs]
        gaps = [b - a for a, b in pairwise(starts)]
        mean_gap = statistics.fmean(gaps)
        if mean_gap > 0 and (statistics.pstdev(gaps) / mean_gap) < _PERIODIC_MAX_CV:
            period_s = float(statistics.median(gaps))

    if observed < _NEW_OBSERVED_S:
        kind = "new"
    elif duty > _STEADY_DUTY:
        kind = "steady"
    elif period_s is not None:
        kind = "periodic"
    elif duty < _BURSTY_DUTY:
        kind = "bursty"
    else:
        kind = "intermittent"

    return {"kind": kind, "duty": duty, "burst_s": burst_s, "period_s": period_s, "bursts": bursts}
