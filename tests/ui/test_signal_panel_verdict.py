# ruff: noqa: E501  (embedded JavaScript)
"""signal-panel.js signalVerdict: the docked inspector's one-line answer picks
decoder output over a DSP estimate over a saved memory, names its source and
state, and never invents a result from placeholder items."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-panel.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_verdict_prefers_decoder_then_dsp_then_memory():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[^;]*;\n/gm, '')
  .replace(/^export /gm, '');
const ctx = { LitElement: class {}, html: () => '', customElements: { define() {} }, console, Map, Set, setTimeout, clearTimeout };
vm.runInNewContext(src + `\nglobalThis.out = { signalVerdict };`, ctx);
const { signalVerdict } = ctx.out;

// Decoder output wins and carries its integrity state.
let v = signalVerdict({ decoder: [{ title: 'Decoded (dmr)', integrity: 'unknown', at: 1 }], dsp: [{ title: 'DMR / P25 / NXDN' }] });
assert.equal(v.title, 'Decoded (dmr)'); assert.equal(v.source, 'decoder'); assert.match(v.state, /unchecked/); assert.equal(v.at, 1);
v = signalVerdict({ decoder: [{ title: 'Decoded (ais)', integrity: 'passed' }] });
assert.equal(v.state, 'validated decode');
v = signalVerdict({ decoder: [{ title: 'Decoded (pocsag)', integrity: 'failed' }] });
assert.match(v.state, /failed integrity/);

// DSP estimate next; a historical one says so.
v = signalVerdict({ dsp: [{ title: 'speech-like NFM audio (tentative)', _analysisState: 'current_result' }] });
assert.equal(v.source, 'dsp'); assert.equal(v.state, 'DSP estimate');
v = signalVerdict({ dsp: [{ title: 'fm_fsk', _analysisState: 'historical_result' }] });
assert.equal(v.state, 'earlier DSP estimate');

// Placeholder items are not an identification; memory is the last resort.
v = signalVerdict({ dsp: [{ title: 'No DSP classification yet' }], memory: [{ title: 'DMR 152.878017 MHz' }] });
assert.equal(v.source, 'memory'); assert.match(v.state, /not measured/);
v = signalVerdict({ dsp: [{ title: 'No DSP classification yet' }] });
assert.equal(v.title, 'No identification yet'); assert.equal(v.source, null); assert.equal(v.state, null);
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
