# ruff: noqa: E501 -- embedded JavaScript source lines
"""device-history-chart.js (docs/history.md "Shared timeline (UI)"): the
pure scale/tick/gap helpers behind the min/max-band + reports-strip chart --
bucket auto-selection, column math, signal domain/scaling, run (gap)
detection for a sparse cell series, coverage-gap detection, and time ticks.
Same vm.runInNewContext harness as test_ble_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/device-history-chart.js"

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
    "BUCKET_OPTIONS, autoBucketS, bucketCount, indexByTime, xForIndex, signalDomain, "
    "yForValue, maxReports, cellColumns, cellRuns, missingCoverage, formatTickLabel, timeTicks"
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
def test_auto_bucket_s_picks_smallest_fitting_bucket():
    _run(r"""
// 6h at 60s = 360 columns exactly -- still fits the cap.
assert.equal(autoBucketS(6 * 3600, 360), 60);
// One bucket over pushes to the next size (300s).
assert.equal(autoBucketS(6 * 3600 + 60, 360), 300);
// 7 days falls all the way to the largest bucket (3600s), same as the
// server's own bucket set (docs/history.md: 60, 300, 900 or 3600 s).
assert.equal(autoBucketS(7 * 24 * 3600, 360), 3600);
assert.equal(JSON.stringify(BUCKET_OPTIONS), JSON.stringify([60, 300, 900, 3600]));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bucket_count_and_x_for_index():
    _run(r"""
assert.equal(bucketCount(0, 600, 60), 10);
assert.equal(bucketCount(0, 0, 60), 1, 'a degenerate range still has one drawable column');
assert.equal(xForIndex(0, 10), 0);
assert.equal(xForIndex(5, 10), 0.5);
assert.equal(xForIndex(9, 10), 0.9);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_signal_domain_and_y_for_value():
    _run(r"""
// cell shape: [t, reports, avg, min, max] (docs/history.md "API").
let d = signalDomain([[0, 4, -60, -65, -55], [60, 2, -58, -62, -50]]);
assert.equal(d.min, -65);
assert.equal(d.max, -50);

assert.equal(JSON.stringify(signalDomain([])), JSON.stringify({ min: -1, max: 1 }));

// A flat series (min === max) still gets a non-zero span, so it draws a
// visible line instead of collapsing to zero height.
const flat = signalDomain([[0, 1, -60, -60, -60]]);
assert.ok(flat.max > flat.min);

// Highest value draws nearest the top (smallest y, honoring padTop).
const domain = { min: 0, max: 100 };
assert.equal(yForValue(100, domain, 100, 2, 2), 2);
assert.equal(yForValue(0, domain, 100, 2, 2), 98);
assert.equal(yForValue(50, domain, 100, 0, 0), 50);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cell_columns_and_runs_break_across_gaps():
    _run(r"""
// Bucket at t=120 (index 2) is missing -- the device was silent, not that
// the receiver wasn't listening (that's missingCoverage, tested below).
const cells = [[0, 1, 0, 0, 0], [60, 1, 0, 0, 0], [180, 1, 0, 0, 0]];
const columns = cellColumns(cells, 0, 60, 4);
assert.equal(columns.length, 4);
assert.equal(columns[2].cell, null);
assert.ok(columns[0].cell && columns[1].cell && columns[3].cell);

const runs = cellRuns(columns);
assert.equal(runs.length, 2, 'the gap at index 2 splits one run into two');
assert.equal(runs[0].length, 2);
assert.equal(runs[0][0].i, 0);
assert.equal(runs[0][1].i, 1);
assert.equal(runs[1].length, 1);
assert.equal(runs[1][0].i, 3);

assert.equal(maxReports(cells), 1);
assert.equal(maxReports([]), 1, 'never zero, so a caller never divides by zero');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_missing_coverage_gap_detection():
    _run(r"""
// No coverage row for minute 60 -- the receiver wasn't listening then.
const coverage = [[0, 60], [120, 60]];
assert.equal(JSON.stringify(missingCoverage(coverage, 0, 60, 3)), JSON.stringify([1]));
assert.equal(JSON.stringify(missingCoverage([], 0, 60, 3)), JSON.stringify([0, 1, 2]), 'no coverage at all is all gaps');
// Coverage for both buckets this window actually spans (t=0, t=60) -- no gaps.
assert.equal(JSON.stringify(missingCoverage([[0, 60], [60, 60]], 0, 60, 2)), JSON.stringify([]));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_format_tick_label_and_time_ticks():
    _run(r"""
// Short range: "HH:MM" only.
assert.match(formatTickLabel(1700000000, 3600), /^\d{2}:\d{2}$/);
// Long range (>36h): date prefix too.
assert.match(formatTickLabel(1700000000, 40 * 3600), /^\w+ \d+ \d{2}:\d{2}$/);

// 10 columns at 60s, default maxTicks=6 -> step=2 -> ticks at i=0,2,4,6,8.
const ticks = timeTicks(0, 600, 60, 6);
assert.equal(ticks.length, 5);
assert.equal(ticks[0].t, 0);
assert.equal(ticks[0].frac, 0);
assert.equal(ticks[1].t, 120);
for (const tick of ticks) assert.match(tick.label, /^\d{2}:\d{2}$/);
""")
