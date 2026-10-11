# ruff: noqa: E501 -- embedded JavaScript source lines
"""beacons-view.js NCDXF schedule helpers (ncdxfOnAir, beaconFrequencyNow).
Same vm.runInNewContext harness as test_wifi_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/beacons-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ncdxf_schedule():
    _run(r"""
const m = vm.runInNewContext(raw + '\n({ ncdxfOnAir, beaconFrequencyNow })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});
const T = 180 * 1000 * 1000; // a multiple of 180 s
const at = (ms, off) => m.ncdxfOnAir(T + ms, off);

let r = at(0);
assert.equal(r.length, 5);
assert.equal(r[0].band_mhz, 14.1);
assert.equal(r[0].beacon, '4U1UN');
assert.equal(r[4].band_mhz, 28.2);
assert.equal(r[4].index, 14);
assert.equal(r[4].beacon, 'CS3B');
assert.equal(r[0].secondsLeft, 10);

r = at(10000);
assert.equal(r[0].beacon, 'VE8AT');
assert.equal(r[1].beacon, '4U1UN');

assert.equal(at(6000)[0].secondsLeft, 4);

// +10 s offset behaves like being 10 s further into the cycle; negative wraps.
assert.equal(at(0, 10)[0].beacon, 'VE8AT');
assert.equal(at(0, -10)[0].beacon, 'YV5B');

// 4U1UN is on 14.100 at p=0 and silent at p=60 (slot 6 -> band k would be 6).
assert.equal(m.beaconFrequencyNow(T, 0, 0), 14.1);
assert.equal(m.beaconFrequencyNow(T + 60000, 0, 0), null);
""")
