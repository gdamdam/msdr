"""Zoom coordinates must agree with the source bins and stay inside the capture."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_zoom_frequency_coordinates():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    method = source.split("  _viewFrame() {", 1)[1].split("\n  _setZoom", 1)[0]
    script = """
const assert = require('node:assert/strict');
const view = new Function(METHOD);
const bins = Uint8Array.from({length:512}, (_,i)=>i%256);
for (const zoom of [1,2,4,8,16]) for (const pan of [0,0.25,0.5,0.75,1]) {
  const f = view.call({frame:{bins,center_hz:433e6,sample_rate_hz:6e6},_zoom:zoom,_pan:pan});
  const low = f.center_hz-f.sample_rate_hz/2;
  assert.equal(low,430e6+f._start/512*6e6);
  assert.equal(f.bins.length,512/zoom);
  assert.ok(low>=430e6 && low+f.sample_rate_hz<=436e6);
  assert.equal(f.bins[0],bins[f._start]);
}
""".replace("METHOD", json.dumps(method.rsplit("}", 1)[0]))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
