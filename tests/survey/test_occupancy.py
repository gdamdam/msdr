"""OccupancyMap on synthetic frames: known per-bin duty cycle (+/- 2%), the
floor tracks the per-frame median, and reset() clears both windows."""

import numpy as np

from msdr.survey.occupancy import OccupancyMap

_FLOOR = 80
_THRESHOLD_UNITS = 16


def _frame(active_bins: set[int], bins: int = 512) -> bytes:
    levels = np.full(bins, _FLOOR, dtype=np.uint8)
    for b in active_bins:
        levels[b] = _FLOOR + _THRESHOLD_UNITS + 5  # comfortably over the threshold
    return levels.tobytes()


def test_duty_cycle_matches_known_fraction_per_bin():
    occ = OccupancyMap(windows_frames=(100, 100), threshold_units=_THRESHOLD_UNITS)
    for i in range(100):
        active = {20}  # bin 20: always on -> duty 1.0
        if i % 2 == 0:  # bin 10: on every other frame -> duty 0.5
            active.add(10)
        occ.update(_frame(active))

    snap = occ.snapshot()
    assert abs(snap["short"][20] - 1.0) < 0.02
    assert abs(snap["short"][10] - 0.5) < 0.02
    assert snap["short"][0] == 0.0
    assert snap["floor"] == _FLOOR


def test_short_and_long_windows_are_independent():
    # A window shorter than the burst sees a higher duty than a longer one
    # covering the same burst plus quiet frames after it.
    occ = OccupancyMap(windows_frames=(10, 20), threshold_units=_THRESHOLD_UNITS)
    for _ in range(10):
        occ.update(_frame({5}))
    for _ in range(10):
        occ.update(_frame(set()))

    snap = occ.snapshot()
    assert snap["short"][5] == 0.0  # last 10 frames: all quiet
    assert abs(snap["long"][5] - 0.5) < 0.02  # last 20 frames: half active


def test_reset_clears_both_windows_and_floor():
    occ = OccupancyMap(windows_frames=(10, 10), threshold_units=_THRESHOLD_UNITS)
    for _ in range(10):
        occ.update(_frame({5}))
    occ.reset()

    snap = occ.snapshot()
    assert all(v == 0.0 for v in snap["short"])
    assert all(v == 0.0 for v in snap["long"])
    assert snap["floor"] == 0


def test_snapshot_returns_512_floats():
    occ = OccupancyMap()
    occ.update(_frame({1, 2, 3}))
    snap = occ.snapshot()
    assert len(snap["short"]) == 512
    assert len(snap["long"]) == 512
    assert all(isinstance(v, float) for v in snap["short"])
