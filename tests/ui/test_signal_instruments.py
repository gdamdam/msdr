# ruff: noqa: E501  (embedded JavaScript)
"""ⓘ panel Instruments helpers: suggested tuning coerced to what live
inspection accepts, and whether the live receiver is on the shown frequency."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-instruments.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_tuning_and_relation():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const ctx = { LitElement: class {}, html: () => '', customElements: { define() {} }, console,
  fetchJson: async () => ({}), setInterval, clearInterval };
vm.runInNewContext(src + `
globalThis.out = { liveTuning, liveRelation };`, ctx);
const { liveTuning, liveRelation } = ctx.out;

// Suggested tuning passes through, case-insensitively.
assert.deepEqual({ ...liveTuning('nfm', 12500) }, { demodulator: 'NFM', bandwidth: 12500 });
// Unknown/missing mode -> NFM; missing or out-of-range width -> the mode default.
assert.deepEqual({ ...liveTuning('DMR', 0) }, { demodulator: 'NFM', bandwidth: 12500 });
assert.deepEqual({ ...liveTuning(null, null) }, { demodulator: 'NFM', bandwidth: 12500 });
assert.deepEqual({ ...liveTuning('USB', 999999) }, { demodulator: 'USB', bandwidth: 2700 });
assert.deepEqual({ ...liveTuning('WFM', undefined) }, { demodulator: 'WFM', bandwidth: 200000 });

// Relation to the frequency on screen.
assert.equal(liveRelation(null, 145500000), 'off');
assert.equal(liveRelation({ frequency_hz: 145500000 }, 145500000), 'here');
assert.equal(liveRelation({ frequency_hz: 145500001 }, 145500000), 'here');
assert.equal(liveRelation({ frequency_hz: 145525000 }, 145500000), 'elsewhere');
console.log('ok');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", str(SOURCE)],
        capture_output=True,
        text=True,
        env=node_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
