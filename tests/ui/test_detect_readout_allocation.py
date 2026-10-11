# ruff: noqa: E501 -- embedded JavaScript source lines
"""detect-readout.js's allocation lookup carries a monotonically increasing
token, same contract as its existing identifyLive sequencing: a reply for a
frequency the operator has already tuned away from must never overwrite what
a newer retune already requested, even if it resolves after the newer one."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/detect-readout.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_allocation_response_is_ignored_and_retune_resets_state():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class DetectReadout', 'class DetectReadout');

const pending = [];
const api = {
  allocationLookup: (hz, jurisdiction) => new Promise(resolve => pending.push({ hz, jurisdiction, resolve })),
  identifyLive: async () => ({ candidates: [] }),
};
const DetectReadout = vm.runInNewContext(src + '\nDetectReadout', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(DetectReadout.prototype);
view.hz = 100000000; view.mode = 'NFM'; view.bw = 12500; view.enabled = true;
view.jurisdiction = 'US'; view.candidates = null; view._allocation = undefined;
view._reqSeq = 0; view._allocSeq = 0; view._timer = null;

const first = view._lookupAllocation();   // token 1, for 100.000 MHz
view.hz = 200000000;
const second = view._lookupAllocation();  // token 2, for 200.000 MHz -- newer
assert.equal(pending.length, 2);

// Out-of-order resolution: the newer request settles first...
pending[1].resolve({ status: 'ok', frequency_hz: 200000000 });
await second;
assert.deepEqual(view._allocation, { status: 'ok', frequency_hz: 200000000 });

// ...then the stale first request finally settles. It must be dropped.
pending[0].resolve({ status: 'ok', frequency_hz: 100000000 });
await first;
assert.deepEqual(
  view._allocation, { status: 'ok', frequency_hz: 200000000 },
  'a late reply for an old frequency must never overwrite the newer result',
);

// A fresh retune (_scheduleLookup) bumps the token and clears to "loading"
// (undefined) right away, synchronously -- not just once the request lands.
view.candidates = ['stale-hint'];
view.expanded = true;
view.hz = 300000000;
view._scheduleLookup();
assert.equal(view._allocation, undefined, 'scheduling a new lookup must clear the previous allocation immediately');
assert.equal(view.candidates, null);
assert.equal(view.expanded, false);
clearTimeout(view._timer);

// Disabling clears it too, and never schedules a request.
view._allocation = { status: 'ok' };
view.enabled = false;
view._scheduleLookup();
assert.equal(view._allocation, undefined);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_jurisdiction_change_alone_reschedules_allocation_lookup():
    """A jurisdiction switch (US -> ITU1) with the same `hz` used to be
    invisible to `updated()`, which only watched hz/mode/bw/enabled: the
    stale US allocation stayed on screen under the new jurisdiction. Treat
    `jurisdiction` like `hz` in the change-detection list."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class DetectReadout', 'class DetectReadout');

const pending = [];
const api = {
  allocationLookup: (hz, jurisdiction) => new Promise(resolve => pending.push({ hz, jurisdiction, resolve })),
  identifyLive: async () => ({ candidates: [] }),
};
const DetectReadout = vm.runInNewContext(src + '\nDetectReadout', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(DetectReadout.prototype);
view.hz = 100000000; view.mode = 'NFM'; view.bw = 12500; view.enabled = true;
view.jurisdiction = 'US'; view.candidates = null; view._allocation = undefined;
view._reqSeq = 0; view._allocSeq = 0; view._timer = null;

const seed = view._lookupAllocation();   // seeds a result for US
pending[0].resolve({ status: 'ok', jurisdiction: 'US' });
await seed;
assert.deepEqual(view._allocation, { status: 'ok', jurisdiction: 'US' });

// Same hz, only jurisdiction changes -- the exact lit `updated()` hook, with
// only `jurisdiction` in the changed-properties map (hz untouched).
view.jurisdiction = 'ITU1';
view.updated(new Map([['jurisdiction', 'US']]));
assert.equal(view._allocation, undefined, 'the stale US allocation must be cleared immediately');
assert.ok(view._timer, 'a new debounced lookup must be scheduled');
clearTimeout(view._timer);

view._lookupAllocation();
assert.equal(pending.length, 2);
assert.equal(pending[1].jurisdiction, 'ITU1', 'the new lookup must use the new jurisdiction');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


FORMAT = Path(__file__).resolve().parents[2] / "msdr/ui/format.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_itu_jurisdiction_shows_why_channel_hints_are_missing():
    """allocation_context() returns `supplemental: {available: false, reason}`
    for ITU jurisdictions (the band-plan hints are US-specific); the hints
    block then shows that reason instead of US hints or nothing at all."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const { supplementalHintsNotice, bandCategoryLabel } = await import(process.argv[2]);
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class DetectReadout', 'class DetectReadout');
const html = (parts, ...args) => [...parts, ...args];
const flatten = (node, out = []) => {
  if (typeof node === 'string') out.push(node);
  else if (Array.isArray(node)) for (const n of node) flatten(n, out);
  return out;
};
const DetectReadout = vm.runInNewContext(src + '\nDetectReadout', {
  LitElement: class {}, html, customElements: { define() {} },
  supplementalHintsNotice, bandCategoryLabel, api: {}, console,
});
const view = Object.create(DetectReadout.prototype);
view.allocationOnly = true;
view.candidates = [{ provider: 'bandplan', name: 'NOAA Weather Radio WX2', category: 'marine' }];

view._allocation = { status: 'ok', jurisdiction: 'ITU2', supplemental:
  { available: false, reason: 'Channel and application hints are US-specific' } };
let text = flatten(view.render()).join(' ');
assert.match(text, /Channel \/ application hints/);
assert.match(text, /Channel and application hints are US-specific/);
assert.doesNotMatch(text, /NOAA Weather Radio WX2/, 'US hints must not show under an ITU jurisdiction');

view._allocation = { status: 'ok', jurisdiction: 'US', supplemental: { available: true, reason: null } };
text = flatten(view.render()).join(' ');
assert.match(text, /NOAA Weather Radio WX2/);
assert.doesNotMatch(text, /US-specific/);

// Older responses without the block behave as before (hints shown).
view._allocation = { status: 'ok', jurisdiction: 'US' };
assert.match(flatten(view.render()).join(' '), /NOAA Weather Radio WX2/);
assert.equal(supplementalHintsNotice(null), null);
assert.equal(supplementalHintsNotice({ supplemental: { available: false } }),
  'Channel and application hints are unavailable');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE), FORMAT.as_uri()],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


def test_allocation_strip_reports_results_and_has_no_nested_button():
    """Option A of the readout rework: the readout renders a context strip
    (summary + hints) without wrapping it in the frequency-edit <button>, and
    reports every result via `allocation-change` so the parent can render the
    one-line pill itself. Nesting buttons was invalid HTML and made Details
    open the frequency editor."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class DetectReadout', 'class DetectReadout');
// Minimal lit `html` stub: serialise strings + values so markup can be inspected.
const html = (strings, ...values) => strings.map((s, i) => s + (i < values.length ? flat(values[i]) : '')).join('');
const flat = (v) => Array.isArray(v) ? v.map(flat).join('') : (v == null ? '' : String(v));
const api = { allocationLookup: async () => ({ status: 'ok', lo_hz: 1, hi_hz: 2 }), identifyLive: async () => ({ candidates: [] }) };
const DetectReadout = vm.runInNewContext(src + '\nDetectReadout', {
  LitElement: class {}, html, customElements: { define() {} }, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
  supplementalHintsNotice: () => null, bandCategoryLabel: (c) => c,
});
const view = Object.create(DetectReadout.prototype);
const events = [];
view.dispatchEvent = (ev) => { events.push(ev); return true; };
view.allocationOnly = true; view.hz = 5264000; view.enabled = true; view.jurisdiction = 'US';
view.candidates = [{ provider: 'bandplan', name: 'Oceanographic radar (47 CFR 90.103)', category: null }];
view._reqSeq = 0; view._allocSeq = 0; view._timer = null; view._allocation = undefined;

await view._lookupAllocation();
assert.equal(events.length, 1);
assert.equal(events[0].type, 'allocation-change');
assert.deepEqual(events[0].detail, { status: 'ok', lo_hz: 1, hi_hz: 2 });

const out = view.render();
assert.ok(out.includes('frequency-context-strip'), 'renders the strip');
assert.ok(out.includes('<allocation-summary'), 'renders the summary with its own actions');
assert.ok(out.includes('Oceanographic radar'), 'lists every band-plan hint');
assert.ok(!out.includes('<button'), 'the strip itself must not wrap anything in a button');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
