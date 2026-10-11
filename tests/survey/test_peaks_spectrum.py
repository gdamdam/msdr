"""SpectrumPeakTracker: peak detection over the floor, id stability across
frames (bin-range overlap matching, +-1.5-bin centroid fallback for narrow
peaks), width-scaled gap merging, ttl drop, and (combined with
classify.activity_class) period recovery from synthetic on/off bursts."""

import random

import pytest

from msdr.survey.classify import activity_class
from msdr.survey.peaks_spectrum import SpectrumPeakTracker

_BINS = 512
_BACKGROUND = 50
_PEAK_LEVEL = 90
_FLOOR = 50
_FPS = 23  # measured spectrum-stream rate (survey.py's _MEASURED_FPS)
_DT = 1.0 / _FPS


def _frame(
    peaks: dict[int, int] | None = None, background: int = _BACKGROUND, bins: int = _BINS
) -> bytes:
    arr = bytearray([background] * bins)
    for bin_idx, level in (peaks or {}).items():
        arr[bin_idx] = level
    return bytes(arr)


def test_two_peaks_tracked_independently():
    tracker = SpectrumPeakTracker()
    peaks = tracker.update(_frame({100: _PEAK_LEVEL, 300: _PEAK_LEVEL}), at=0.0, floor=_FLOOR)

    assert len(peaks) == 2
    centroids = sorted(p.centroid_bin for p in peaks)
    assert centroids[0] == 100
    assert centroids[1] == 300
    for p in peaks:
        assert p.level == _PEAK_LEVEL
        assert p.snr_units == _PEAK_LEVEL - _FLOOR
        assert p.active_now is True


def test_drift_within_one_bin_keeps_id():
    tracker = SpectrumPeakTracker()
    first = tracker.update(_frame({150: _PEAK_LEVEL}), at=0.0, floor=_FLOOR)
    assert len(first) == 1
    track_id = first[0].id

    second = tracker.update(_frame({151: _PEAK_LEVEL}), at=_DT, floor=_FLOOR)
    assert len(second) == 1
    assert second[0].id == track_id
    assert second[0].centroid_bin == 151


def test_gap_tolerance_merges_one_bin_gap_into_single_peak():
    tracker = SpectrumPeakTracker()
    # bin 201 stays at background -- only 200 and 202 are individually active,
    # but a 1-bin gap between active runs must still merge into one peak.
    peaks = tracker.update(_frame({200: _PEAK_LEVEL, 202: _PEAK_LEVEL}), at=0.0, floor=_FLOOR)

    assert len(peaks) == 1
    assert peaks[0].bin_lo == 200
    assert peaks[0].bin_hi == 202


def test_ttl_drops_stale_track():
    tracker = SpectrumPeakTracker(ttl_s=1.0)
    background = _frame()

    present = tracker.update(_frame({250: _PEAK_LEVEL}), at=0.0, floor=_FLOOR)
    assert len(present) == 1

    still_within_ttl = tracker.update(background, at=0.5, floor=_FLOOR)
    assert len(still_within_ttl) == 1
    assert still_within_ttl[0].active_now is False

    dropped = tracker.update(background, at=2.0, floor=_FLOOR)
    assert dropped == []


def test_periodic_on_off_recovers_period_within_one_frame():
    # Burst on for 2 frames, off for 18 (period = 20 frames), repeated 7 times
    # -- enough repeats (>=3) for classify.activity_class to spot the period
    # from the gaps between run starts, and enough elapsed time (>5s) to clear
    # the "new" window so periodicity, not freshness, decides the kind.
    tracker = SpectrumPeakTracker(history_s=60.0)
    on_frame = _frame({320: _PEAK_LEVEL})
    off_frame = _frame()
    period_frames = 20
    on_frames = 2

    at = 0.0
    latest: list = []
    for _cycle in range(7):
        for _ in range(on_frames):
            latest = tracker.update(on_frame, at=at, floor=_FLOOR)
            at += _DT
        for _ in range(period_frames - on_frames):
            latest = tracker.update(off_frame, at=at, floor=_FLOOR)
            at += _DT

    assert len(latest) == 1
    track = latest[0]
    assert len(track.on_runs) >= 3

    result = activity_class(track, now=at, history_s=tracker.history_s)
    expected_period_s = period_frames * _DT
    assert result["period_s"] is not None
    assert abs(result["period_s"] - expected_period_s) <= _DT * 1.5
    assert result["kind"] == "periodic"


def test_wideband_modulated_signal_stays_one_steady_track():
    # A continuous FM-broadcast-like carrier: 12 bins wide, each bin's level
    # jitters +-30% frame to frame and a random tilt shifts the weighted
    # centroid back and forth -- large enough single-frame centroid jumps to
    # break the old +-1.5-bin nearest-centroid match, but the active bin
    # range itself never changes, so overlap matching should hold it as one
    # track for the whole run.
    rng = random.Random(12345)
    tracker = SpectrumPeakTracker(history_s=60.0)
    width = 12
    lo = 200
    base = 150
    frames = int(5.0 * _FPS) + 1  # a bit over 5s of frames

    at = 0.0
    latest: list = []
    for _ in range(frames):
        tilt = rng.uniform(-1.0, 1.0)
        peaks = {}
        for i in range(width):
            offset = (i - (width - 1) / 2.0) / ((width - 1) / 2.0)  # -1..1
            skew = 1.0 + 0.4 * tilt * offset
            jitter = 1.0 + rng.uniform(-0.3, 0.3)
            level = max(_FLOOR + 30, min(250, base * skew * jitter))
            peaks[lo + i] = int(level)
        latest = tracker.update(_frame(peaks), at=at, floor=_FLOOR)
        at += _DT

    assert len(latest) == 1
    track = latest[0]
    result = activity_class(track, now=at, history_s=tracker.history_s)
    assert result["kind"] == "steady"
    assert result["duty"] >= 0.95


def test_merged_overlapping_tracks_duty_does_not_exceed_one():
    # Two tracks (100-104 and 114-118), both continuously matched (never
    # closed) for 5 frames -- each is a genuine "always on" span. Then a
    # frame bridges the gap into one contiguous run: the tracker keeps one
    # id and closes the other, handing its whole active history to
    # _dedupe_tracks as a closed on_run that overlaps the survivor's still
    # -open run. That overlapping time must not be counted twice.
    tracker = SpectrumPeakTracker(history_s=60.0)
    separate_peaks = {b: _PEAK_LEVEL for b in range(100, 105)}
    separate_peaks.update({b: _PEAK_LEVEL for b in range(114, 119)})
    bridged_peaks = {b: _PEAK_LEVEL for b in range(100, 119)}

    at = 0.0
    latest: list = []
    for _ in range(5):
        latest = tracker.update(_frame(separate_peaks), at=at, floor=_FLOOR)
        at += _DT
    assert len(latest) == 2

    for _ in range(6):
        latest = tracker.update(_frame(bridged_peaks), at=at, floor=_FLOOR)
        at += _DT
    assert len(latest) == 1  # bridging merges the two tracks into one

    result = activity_class(latest[0], now=at, history_s=tracker.history_s)
    assert result["duty"] <= 1.0
    assert result["duty"] == pytest.approx(1.0)


def test_two_wideband_peaks_20_bins_apart_stay_separate_tracks():
    tracker = SpectrumPeakTracker()
    width = 12
    lo_a = 100
    lo_b = lo_a + width + 20  # far enough apart that gap merging/dedupe can't join them

    at = 0.0
    latest: list = []
    for _ in range(10):
        peaks = {lo_a + i: _PEAK_LEVEL for i in range(width)}
        peaks.update({lo_b + i: _PEAK_LEVEL for i in range(width)})
        latest = tracker.update(_frame(peaks), at=at, floor=_FLOOR)
        at += _DT

    assert len(latest) == 2
    assert sorted(p.bin_lo for p in latest) == [lo_a, lo_b]


def test_pinned_tracks_outlive_the_ttl():
    from tests.survey.test_survey import _peak_frame

    tracker = SpectrumPeakTracker(threshold_units=20, ttl_s=1.0)
    tracker.update(_peak_frame(bin_idx=300), at=0.0, floor=50)
    tracker.update(_peak_frame(bin_idx=200), at=0.1, floor=50)
    kept, dropped = (t for t in tracker.update(bytes([50] * 512), at=0.2, floor=50))
    tracker.pinned = {kept.id}
    # Both unseen for > ttl_s: only the pinned one survives...
    ids = {t.id for t in tracker.update(bytes([50] * 512), at=5.0, floor=50)}
    assert ids == {kept.id}
    # ...until its own cap, when set.
    tracker.pinned_ttl_s = 10.0
    assert tracker.update(bytes([50] * 512), at=20.0, floor=50) == []


def test_split_wide_region_retains_two_active_narrow_carriers():
    tracker = SpectrumPeakTracker()
    wide = {bin_idx: 100 for bin_idx in range(100, 121)}
    assert len(tracker.update(_frame(wide), at=0.0, floor=_FLOOR)) == 1
    split = {103: 170, 117: 200}
    peaks = tracker.update(_frame(split), at=_DT, floor=_FLOOR)
    assert len(peaks) == 2
    assert {p.peak_bin for p in peaks if p.active_now} == {103, 117}
    peaks = tracker.update(_frame(split), at=2 * _DT, floor=_FLOOR)
    assert len(peaks) == 2
    assert {p.peak_bin for p in peaks if p.active_now} == {103, 117}


def test_peak_bin_tracks_strongest_current_trace_bin():
    tracker = SpectrumPeakTracker()
    broad = {bin_idx: 100 for bin_idx in range(100, 121)}
    broad[119] = 220
    peak = tracker.update(_frame(broad), at=0.0, floor=_FLOOR)[0]
    assert peak.peak_bin == 119
    assert peak.centroid_bin != peak.peak_bin
    updated = {bin_idx: 100 for bin_idx in range(100, 121)}
    updated[101] = 230
    peak = tracker.update(_frame(updated), at=_DT, floor=_FLOOR)[0]
    assert peak.peak_bin == 101


def test_wide_region_cannot_bridge_dc_and_anchor_on_excluded_spike():
    tracker = SpectrumPeakTracker()
    bins = {index: 110 for index in range(230, 282)}
    bins[256] = 255
    peaks = tracker.update(_frame(bins), at=0.0, floor=_FLOOR)
    assert len(peaks) == 2
    assert all(not 254 <= peak.peak_bin <= 258 for peak in peaks)
    assert all(peak.level == 110 for peak in peaks)
