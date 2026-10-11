# ruff: noqa: E501 -- embedded JavaScript source lines
"""wifi-activity-chart.js (docs/wifi.md "Wi-Fi activity"): the pure bucket
choice, cell normalisation, gap/hatch detection and colour-scale helpers
behind the Discover -> Wi-Fi -> Activity heatmap. Same vm.runInNewContext
harness as test_device_history_chart.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/wifi-activity-chart.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const ctx = { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console };
"""

EXPORTS = (
    "ACTIVITY_SENTENCE, ACTIVITY_COVERAGE_LEGEND, ACTIVITY_RANGE_OPTIONS, "
    "ACTIVITY_BUCKET_OPTIONS, activityAutoBucketS, channelColumns, columnSpan, "
    "timeBuckets, indexActivity, buildActivityGrid, packetRate, signalMean, "
    "hasSignalMode, signalDomain, cellOpacity, cellTooltip, accessibleSummary, "
    "wifiChannelMHz, channelLabel"
)


def _run(body, exports=EXPORTS):
    script = (
        PRELUDE
        + f"const {{ {exports} }} = vm.runInNewContext(raw + '\\n({{ {exports} }})', ctx);\n"
        + body
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_activity_auto_bucket_s_picks_smallest_fitting_bucket():
    _run(r"""
assert.equal(JSON.stringify(ACTIVITY_BUCKET_OPTIONS), JSON.stringify([1, 5, 15, 60, 300]));
// 6 minutes at 1 s = 360 buckets exactly -- still fits the cap.
assert.equal(activityAutoBucketS(360, 360), 1);
assert.equal(activityAutoBucketS(361, 360), 5);
// 24 h falls all the way to the largest bucket (300 s).
assert.equal(activityAutoBucketS(24 * 3600, 360), 300);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_channel_columns_groups_bands_with_a_gap():
    _run(r"""
const cols = channelColumns([11, 1, 36, 6]);
// 2.4 GHz ascending first, then a gap column, then 5 GHz ascending.
assert.equal(JSON.stringify(cols.map((c) => [c.channel, c.band, c.col])),
  JSON.stringify([[1, "2.4 GHz", 0], [6, "2.4 GHz", 1], [11, "2.4 GHz", 2], [36, "5 GHz", 4]]));
assert.equal(columnSpan(cols), 5);

// Single band: no gap inserted.
const only24 = channelColumns([6, 1]);
assert.equal(JSON.stringify(only24.map((c) => c.col)), JSON.stringify([0, 1]));
assert.equal(columnSpan([]), 1, 'an empty response still has a drawable width');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_time_buckets_and_index_activity():
    _run(r"""
assert.equal(JSON.stringify(timeBuckets(0, 300, 60)), JSON.stringify([0, 60, 120, 180, 240]));
assert.equal(timeBuckets(0, 0, 60).length, 1, 'a degenerate range still has one drawable row');

const idx = indexActivity([[60, 6, 10, 1, -70], [60, 11, 2, 1, null]]);
assert.deepEqual(idx.get("60|6"), [60, 6, 10, 1, -70]);
assert.equal(idx.get("60|1"), undefined);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_build_activity_grid_never_interpolates_and_is_newest_first():
    _run(r"""
const data = {
  start: 0, end: 180, bucket_s: 60, channels: [1, 6],
  rows: [[0, 1, 5, 1, null], [120, 6, 3, 1, null]],
};
const grid = buildActivityGrid(data);
// Newest time first.
assert.equal(grid[0].t, 120);
assert.equal(grid[2].t, 0);
// (t=120, channel=1) and (t=0, channel=6) were never observed -- hatched
// (cell: null), never synthesised from a neighbour.
const row120 = grid[0].cells;
assert.equal(row120.find((c) => c.channel === 1).cell, null);
assert.deepEqual(row120.find((c) => c.channel === 6).cell, [120, 6, 3, 1, null]);
const row0 = grid[2].cells;
assert.deepEqual(row0.find((c) => c.channel === 1).cell, [0, 1, 5, 1, null]);
assert.equal(row0.find((c) => c.channel === 6).cell, null);
// The middle bucket (t=60) has no row for either channel: both hatched.
const row60 = grid[1].cells;
assert.ok(row60.every((c) => c.cell === null));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_packet_rate_normalises_by_listened_seconds():
    _run(r"""
assert.equal(packetRate([0, 6, 10, 2, null]), 5);
assert.equal(packetRate([0, 6, 10, 0, null]), 0, 'never divides by zero listened seconds');
assert.equal(signalMean([0, 6, 10, 2, -70.5]), -70.5);
assert.equal(signalMean([0, 6, 10, 2, null]), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_has_signal_mode_and_signal_domain():
    _run(r"""
assert.equal(hasSignalMode([[0, 6, 1, 1, null], [0, 11, 1, 1, -60]]), true);
assert.equal(hasSignalMode([[0, 6, 1, 1, null]]), false);
assert.equal(signalDomain([[0, 6, 1, 1, null]]), null);
const d = signalDomain([[0, 6, 1, 1, -70], [0, 11, 1, 1, -50]]);
assert.equal(d.min, -70);
assert.equal(d.max, -50);
// A flat series still gets a non-zero span.
const flat = signalDomain([[0, 6, 1, 1, -60]]);
assert.ok(flat.max > flat.min);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cell_opacity_colour_scale():
    _run(r"""
// Packets mode: scaled by rate against maxRate, floored above zero so a
// listened-but-silent cell is still visibly drawn (not hatched, not blank).
const scalePackets = { maxRate: 10 };
assert.ok(cellOpacity([0, 6, 10, 1, null], "packets", scalePackets) > 0.9);
const silent = cellOpacity([0, 6, 0, 1, null], "packets", scalePackets);
assert.ok(silent > 0 && silent < 0.2, 'zero packets: low but non-zero opacity');
assert.equal(cellOpacity([0, 6, 0, 1, null], "packets", { maxRate: 0 }), silent);

// Signal mode: scaled across the domain; no signal on this cell -> floor.
const scaleSignal = { domain: { min: -90, max: -30 } };
assert.ok(cellOpacity([0, 6, 1, 1, -30], "signal", scaleSignal) > 0.9);
assert.ok(cellOpacity([0, 6, 1, 1, -90], "signal", scaleSignal) < 0.2);
assert.equal(cellOpacity([0, 6, 1, 1, null], "signal", scaleSignal),
  cellOpacity([0, 6, 0, 1, null], "packets", { maxRate: 0 }));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cell_tooltip_and_accessible_summary():
    _run(r"""
const hatched = { channel: 6, t: 0, cell: null };
assert.match(cellTooltip(hatched, 60), /not listening/);

const listened = { channel: 6, t: 0, cell: [0, 6, 10, 2, -70] };
const text = cellTooltip(listened, 60);
assert.match(text, /Channel 6/);
assert.match(text, /10 packets/);
assert.match(text, /2\.0 s observed/);
assert.match(text, /5\.0 packets\/s/);
assert.match(text, /uncalibrated/);

const summary = accessibleSummary({ start: 0, end: 3600, channels: [1, 6], max_rate: 4.2 });
assert.match(summary, /channels 1, 6/);
assert.match(summary, /4\.2 packets per second/);
assert.equal(accessibleSummary(null), "Wi-Fi activity: no channels observed in this range.");

assert.match(ACTIVITY_SENTENCE, /not measured RF spectrum/);
assert.match(ACTIVITY_SENTENCE, /channel utilisation/);
assert.match(ACTIVITY_COVERAGE_LEGEND, /hatched/);
assert.equal(ACTIVITY_RANGE_OPTIONS.length, 4);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rows_fit_plot_height_and_time_labels_are_spaced():
    # Regression: scaling a 360-row drawing to fit squeezed every column into
    # a sliver; rows now share a fixed plot height in real pixels instead.
    _run(r"""
assert.equal(rowHeight(10), 14);            // short ranges: capped, not giant
assert.ok(Math.abs(rowHeight(360) * 360 - 420) < 1e-9);
const labels = timeLabelRows(360, rowHeight(360));
assert.ok(labels.length >= 9 && labels.length <= 11);
assert.equal(labels[0], 0);
assert.equal(JSON.stringify(timeLabelRows(5, 14)), JSON.stringify([0, 3]));

// Round local times: 15 s rows at 14 px need >= 3 rows (45 s) apart -> 1 min
// step, so only the :00-second rows are labelled.
const base = new Date(2026, 0, 5, 10, 0, 0).getTime() / 1000;
const times = Array.from({ length: 12 }, (_, i) => base + 165 - i * 15); // newest first, 10:02:45 .. 10:00:00
assert.equal(JSON.stringify(niceTimeLabelRows(times, 15, 14)), JSON.stringify([3, 7, 11]));
// 1 min rows squeezed to ~1.2 px: 40 px = 34 rows -> 1 h step.
const hourly = Array.from({ length: 360 }, (_, i) => base + 359 * 60 - i * 60);
const h = niceTimeLabelRows(hourly, 60, 420 / 360);
assert.equal(h.length, 6);
assert.ok(h.every((i) => new Date(hourly[i] * 1000).getMinutes() === 0));
// Too short for two round labels: evenly spaced fallback.
assert.equal(JSON.stringify(niceTimeLabelRows([base + 15, base], 15, 14)), JSON.stringify([0]));
""", exports="rowHeight, timeLabelRows, niceTimeLabelRows")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wifi_channel_mhz_and_channel_label():
    """Column headers and tooltips name the centre frequency, not just the
    channel number (2.4 GHz: 2407 + 5*ch, 14 = 2484; 5 GHz: 5000 + 5*ch)."""
    _run(r"""
assert.equal(wifiChannelMHz(1), 2412);
assert.equal(wifiChannelMHz(6), 2437);
assert.equal(wifiChannelMHz(13), 2472);
assert.equal(wifiChannelMHz(14), 2484);
assert.equal(wifiChannelMHz(36), 5180);
assert.equal(wifiChannelMHz("165"), 5825);
assert.equal(wifiChannelMHz(null), null);
assert.equal(wifiChannelMHz(undefined), null);
assert.equal(channelLabel(6), 'Channel 6 (2437 MHz)');
assert.match(cellTooltip({ channel: 10, t: 0, cell: null }, 60), /^Channel 10 \(2457 MHz\) · .*not listening$/);
""")
