# ruff: noqa: E501  (embedded JavaScript)
"""Signal workbench pure helpers: parsing, coverage, disabled reasons, capture words."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-workbench.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_workbench_helpers_explain_state_in_words():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const ctx = { LitElement: class {}, html: () => '', customElements: { define() {} }, console,
  fetchJson: async () => ({}), setInterval, clearInterval };
vm.runInNewContext(src + `
globalThis.out = { parseFrequencies, spanCoverage, startBlocker, captureState, mhz, coverageText };`, ctx);
const { parseFrequencies, spanCoverage, startBlocker, captureState, mhz, coverageText } = ctx.out;

// Parsing: duplicates collapse, bad tokens are named, never throws.
assert.deepEqual([...parseFrequencies('145.5, 145.5 146').hz], [145500000, 146000000]);
assert.match(parseFrequencies('145.5, abc').error, /“abc” is not a frequency/);
assert.match(parseFrequencies('').error, /at least one/);
assert.match(parseFrequencies('0').error, /Enter positive frequencies/);

// Coverage against a 2 MHz span centred on 145.8 MHz.
const cov = spanCoverage([145500000, 147500000], 145800000, 2000000);
assert.deepEqual([...cov.inside], [145500000]);
assert.deepEqual([...cov.outside], [147500000]);
assert.equal(spanCoverage([1], null, null).known, false);

// Disabled reasons point at the fix.
const ok = { connected: true, started: true, complete: true };
const parsed = parseFrequencies('145.5');
assert.match(startBlocker({ receiver: { connected: false }, parsed, coverage: cov }), /not connected/);
assert.match(startBlocker({ receiver: { connected: true, started: false }, parsed, coverage: cov }), /Press Play/);
const outside = spanCoverage([147500000], 145800000, 2000000);
assert.match(startBlocker({ receiver: ok, parsed: parseFrequencies('147.5'), coverage: outside, cycle: false }), /span cycling/);
assert.equal(startBlocker({ receiver: ok, parsed: parseFrequencies('147.5'), coverage: outside, cycle: true }), null);
assert.equal(startBlocker({ receiver: ok, parsed, coverage: cov }), null);

// Capture states are words, not colours.
assert.equal(captureState(null).word, 'Off');
assert.match(captureState({ state: 'armed', enabled: true, event_count: 1, event_cap: 20 }).word, /Armed/);
assert.equal(captureState({ state: 'armed', enabled: true, event_count: 1, event_cap: 20 }).detail, '1 of 20 captures');
assert.match(captureState({ state: 'capped' }).word, /limit reached/);
assert.equal(mhz(145500000), '145.500');
// Coverage codes become words; resolution limits are named.
assert.equal(coverageText({ covered: false, reason: 'outside_span' }).text, 'Outside receiver span');
assert.match(coverageText({ covered: false, reason: 'dc_guard' }).text, /DC spike/);
assert.equal(coverageText({ covered: true }).text, 'Observed');
assert.match(coverageText({ covered: true, resolution_warning: 'channel_narrower_than_spectrum_bin' }).text, /narrower than one spectrum bin/);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
