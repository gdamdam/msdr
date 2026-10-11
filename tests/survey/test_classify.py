"""bandwidth_class table cases and activity_class kind derivation
(new/steady/bursty/periodic/intermittent) against hand-built TrackedPeak
fixtures -- classify.py is pure, so no tracker/frames needed here."""

import pytest

from msdr.survey.classify import activity_class, bandwidth_class
from msdr.survey.peaks_spectrum import TrackedPeak

# (width_hz, resolution_hz, width_bins, expected)
_BANDWIDTH_CASES = [
    (1000.0, 500.0, 2, "unresolved"),  # width_bins <= 2 -> unresolved even though narrow
    (1000.0, 1000.0, 1, "carrier"),  # single bin, fine resolution -> confidently a carrier
    (1000.0, 8000.0, 1, "unresolved"),  # single bin, coarse resolution -> can't tell, unresolved
    (2000.0, 500.0, 5, "narrow"),  # < 4k
    (12000.0, 2000.0, 6, "nfm"),  # 4k-20k, not the am window
    (7000.0, 1000.0, 4, "am"),  # 5k-10k with resolution <= 2k
    (7000.0, 4000.0, 4, "nfm"),  # 5k-10k but resolution too coarse for am
    (200_000.0, 10_000.0, 20, "wfm"),  # 100k-300k
    (40_000.0, 2000.0, 20, "wide_digital"),  # > 25k
    (22_000.0, 2000.0, 12, "unresolved"),  # 20k-25k gap: no band claims this width
]

_FM_CENTER_HZ = 100_000_000.0
_ROUNDED_PROFILE_3 = [70, 90, 70]  # spread 20 -- peaked, not flat
_FLAT_PROFILE_8 = [90, 91, 90, 89, 90, 91, 90, 89]  # spread 2 -- flat top
_ROUNDED_PROFILE_8 = [60, 75, 85, 90, 90, 85, 75, 60]  # spread 30 -- peaked


@pytest.mark.parametrize("width_hz,resolution_hz,width_bins,expected", _BANDWIDTH_CASES)
def test_bandwidth_class_table(width_hz, resolution_hz, width_bins, expected):
    # Defaults unchanged: no center_hz/profile -> today's thresholds.
    assert bandwidth_class(width_hz, resolution_hz, width_bins) == expected


def test_bandwidth_class_fm_band_rounded_narrow_measurement_is_wfm():
    # Coarse resolution measures a continuous FM broadcast as only 3 bins /
    # 46,875 Hz wide -- a peaked (not flat-topped) shape in the FM band must
    # still read as wfm, not wide_digital.
    result = bandwidth_class(
        46_875.0, 15_625.0, 3, center_hz=_FM_CENTER_HZ, profile=_ROUNDED_PROFILE_3
    )
    assert result == "wfm"


def test_bandwidth_class_flat_top_outside_fm_band_is_wide_digital():
    result = bandwidth_class(
        125_000.0, 15_625.0, 8, center_hz=200_000_000.0, profile=_FLAT_PROFILE_8
    )
    assert result == "wide_digital"


def test_bandwidth_class_rounded_outside_fm_band_is_wfm():
    result = bandwidth_class(
        125_000.0, 15_625.0, 8, center_hz=200_000_000.0, profile=_ROUNDED_PROFILE_8
    )
    assert result == "wfm"


def _track(*, first_seen, last_seen, on_runs, active_now, run_start=0.0) -> TrackedPeak:
    return TrackedPeak(
        id="t0",
        bin_lo=100,
        bin_hi=100,
        centroid_bin=100.0,
        level=90,
        snr_units=40.0,
        first_seen=first_seen,
        last_seen=last_seen,
        on_runs=on_runs,
        active_now=active_now,
        run_start=run_start,
    )


def test_activity_class_new_when_barely_observed():
    track = _track(first_seen=99.0, last_seen=100.0, on_runs=[], active_now=True, run_start=99.0)
    result = activity_class(track, now=100.0, history_s=600.0)
    assert result["kind"] == "new"


def test_activity_class_steady_when_always_on():
    # Active continuously since first_seen, well past the "new" window.
    track = _track(first_seen=0.0, last_seen=100.0, on_runs=[], active_now=True, run_start=0.0)
    result = activity_class(track, now=100.0, history_s=600.0)
    assert result["kind"] == "steady"
    assert result["duty"] > 0.9


def test_activity_class_bursty_when_low_duty_no_period():
    # Short, irregularly spaced runs -> low duty, gaps too uneven for a period
    # (gaps 5s then 50s: coefficient of variation well above the 0.35 cutoff).
    on_runs = [(0.0, 1.0), (5.0, 6.0), (55.0, 56.0)]
    track = _track(first_seen=0.0, last_seen=56.0, on_runs=on_runs, active_now=False)
    result = activity_class(track, now=100.0, history_s=600.0)
    assert result["kind"] == "bursty"
    assert result["duty"] < 0.5
    assert result["period_s"] is None


def test_activity_class_duty_clamped_when_open_run_overlaps_on_runs():
    # A closed on_run that overlaps the still-open run (e.g. left over from
    # a track merge) must not be double-counted: the true activity here is
    # continuous from t=0 to now=100, so duty must be exactly 1.0, not 1.5.
    track = _track(
        first_seen=0.0, last_seen=100.0, on_runs=[(0.0, 60.0)], active_now=True, run_start=50.0
    )
    result = activity_class(track, now=100.0, history_s=600.0)
    assert result["duty"] == pytest.approx(1.0)
    assert result["duty"] <= 1.0


def test_activity_class_periodic_when_regular_gaps():
    # Runs start every 10s with low variance -> period recovered.
    on_runs = [(0.0, 1.0), (10.0, 11.0), (20.0, 21.0), (30.0, 31.0)]
    track = _track(first_seen=0.0, last_seen=31.0, on_runs=on_runs, active_now=False)
    result = activity_class(track, now=100.0, history_s=600.0)
    assert result["kind"] == "periodic"
    assert result["period_s"] == pytest.approx(10.0)
