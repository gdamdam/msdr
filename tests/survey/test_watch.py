import pytest

from msdr.radio.protocol import SpectrumFrame
from msdr.survey.watch import WithinBandWatcher, plan_span_groups


def _frame(center_hz, sample_rate_hz, peaks, at=1.0):
    bins = bytearray([20] * 512)
    for frequency_hz, level in peaks.items():
        index = round(256 + (frequency_hz - center_hz) / (sample_rate_hz / 512))
        bins[index] = level
    return SpectrumFrame(bytes(bins), at)


def test_watch_reports_only_confirmed_in_span_coverage():
    watcher = WithinBandWatcher([100_000_000, 100_500_000, 102_000_000])
    status = watcher.consume(
        _frame(100_000_000, 2_000_000, {100_500_000: 120}),
        center_hz=100_000_000,
        sample_rate_hz=2_000_000,
    )
    rows = {row["frequency_hz"]: row for row in status["measurements"]}
    assert rows[100_500_000]["covered"] is True
    assert rows[100_500_000]["snr_db"] > 40
    assert rows[102_000_000]["covered"] is False
    assert rows[102_000_000]["reason"] == "outside_span"
    assert status["decode_target_hz"] == 100_500_000
    assert status["resolution_hz"] == pytest.approx(3906.25)
    assert status["decode_concurrency"] == 1


def test_priority_and_hold_select_one_watched_target():
    low, high = 100_200_000, 100_600_000
    watcher = WithinBandWatcher([low, high], hold_s=2)
    watcher.consume(_frame(100_400_000, 2_000_000, {low: 100, high: 80}, at=1),
                    center_hz=100_400_000, sample_rate_hz=2_000_000)
    assert watcher.status()["decode_target_hz"] == low
    watcher.consume(_frame(100_400_000, 2_000_000, {low: 80, high: 120}, at=2),
                    center_hz=100_400_000, sample_rate_hz=2_000_000)
    assert watcher.status()["decode_target_hz"] == low  # still held
    watcher.set_priority(high)
    watcher.consume(_frame(100_400_000, 2_000_000, {low: 80, high: 120}, at=2.1),
                    center_hz=100_400_000, sample_rate_hz=2_000_000)
    assert watcher.status()["decode_target_hz"] == high


def test_coarse_bin_resolution_warns_about_shared_watch_windows():
    watcher = WithinBandWatcher([100_000_000, 100_001_000], channel_width_hz=1000)
    status = watcher.consume(_frame(99_950_000, 10_000_000, {100_000_000: 80}),
                             center_hz=99_950_000, sample_rate_hz=10_000_000)
    rows = status["measurements"]
    assert status["resolution_hz"] == pytest.approx(19_531.25)
    assert all(row["independent"] is False for row in rows)
    assert all(row["resolution_warning"] == "channel_narrower_than_spectrum_bin" for row in rows)


def test_span_groups_are_minimal_and_validate_inputs():
    groups = plan_span_groups([100, 105, 110, 250], 20, edge_margin_fraction=0.1)
    assert [group.frequencies_hz for group in groups] == [(100, 105, 110), (250,)]
    assert all(abs(group.center_hz - freq) > 3 * 20 / 512
               for group in groups for freq in group.frequencies_hz)
    with pytest.raises(ValueError, match="sample_rate_hz"):
        plan_span_groups([100], 0)
    with pytest.raises(ValueError, match="span groups"):
        plan_span_groups([100, 105, 250], 20, edge_margin_fraction=0.1, max_groups=1)


def test_singleton_span_group_is_measurable_outside_dc_guard():
    frequency = 100_500_000
    group = plan_span_groups([frequency], 2_000_000)[0]
    assert abs(group.center_hz - frequency) > 3 * 2_000_000 / 512
    watcher = WithinBandWatcher([frequency])
    status = watcher.consume(
        _frame(group.center_hz, 2_000_000, {frequency: 100}),
        center_hz=group.center_hz,
        sample_rate_hz=2_000_000,
    )
    assert status["measurements"][0]["covered"] is True
    assert status["measurements"][0]["activity_state"] == "active"
