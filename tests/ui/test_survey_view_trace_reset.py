# ruff: noqa: E501 -- embedded JavaScript source lines
"""survey-view.js: PEAK/AVG retained bins reset when span/center changes -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nSurveyView', {
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
def test_peak_and_avg_bins_reset_on_span_or_center_change():
    _run(r"""
const C = load({ performance: { now: () => 0 } });
const mk = () => {
  const v = Object.create(C.prototype);
  Object.assign(v, { _zoom: 2, _pan: 0.2, _wfAuto: false, _waterfall: true, _peakBins: new Float32Array(4), _smoothBins: new Float32Array(4),
    _positionHoverCard() {}, _ensureWfRange() {}, _updateTraceState() {}, _pushWaterfallRow() {}, _scheduleDraw() {}, _onWfSettingsChanged() {},
    querySelector: () => null });
  return v;
};
const base = { center_hz: 100e6, sample_rate_hz: 2e6, bins: [1] };
let v = mk();
v.frame = { ...base, center_hz: 101e6 };
v.updated(new Map([['frame', base]]));
assert.equal(v._peakBins, null); assert.equal(v._smoothBins, null);
v = mk();
v.frame = { ...base, sample_rate_hz: 1e6 };
v.updated(new Map([['frame', base]]));
assert.equal(v._peakBins, null); assert.equal(v._smoothBins, null);
v = mk();
v.frame = { ...base };
v.updated(new Map([['frame', base]]));
assert.ok(v._peakBins && v._smoothBins, 'same span keeps retained bins');
""")
