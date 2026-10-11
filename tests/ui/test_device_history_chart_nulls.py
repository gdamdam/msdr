# ruff: noqa: E501 -- embedded JavaScript source lines
"""device-history-chart.js: null min/max/avg cells break the band/line instead of plotting at 0 -- same vm.runInNewContext harness as test_palette.py."""
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
const load = (extra = {}) => vm.runInNewContext(raw + '\nDeviceHistoryChart', {
  LitElement: class {}, html: (strings, ...values) => ({ strings: [...strings], values }), svg: (strings, ...values) => ({ strings: [...strings], values }),
  customElements: { define() {} }, console, CustomEvent: class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } },
  ...extra,
});
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_null_cells_not_plotted():
    _run(r"""
const C = load({});
const v = Object.create(C.prototype);
// cell = [bucket, reports, avg, min, max]
Object.assign(v, { cells: [[0, 1, -60, -70, -50], [60, 1, null, null, null], [120, 1, -62, -72, -52]], coverage: [], bucketS: 60, start: 0, end: 180, signalKind: 'dbm', _hatchId: 'h' });
const found = [];
const walk = (n) => { if (Array.isArray(n)) n.forEach(walk); else if (n?.strings) { found.push(n); n.values.forEach(walk); } };
walk(v.render());
const polylines = found.filter((n) => n.strings[0].includes('<polyline points='));
const paths = found.filter((n) => n.strings[0].includes('<path d=') && n.strings.some((p) => p.includes('opacity="0.18"')));
for (const n of [...polylines, ...paths]) assert.ok(!String(n.values[0]).includes('NaN'));
assert.equal(polylines.length, 2, 'avg line split at the null bucket');
assert.equal(paths.length, 2, 'band split at the null bucket');
// all-null series: nothing plotted
Object.assign(v, { cells: [[0, 1, null, null, null]] });
const f2 = []; const walk2 = (n) => { if (Array.isArray(n)) n.forEach(walk2); else if (n?.strings) { f2.push(n); n.values.forEach(walk2); } };
walk2(v.render());
assert.equal(f2.filter((n) => n.strings[0].includes('<polyline points=')).length, 0);
""")
