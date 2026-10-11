# ruff: noqa: E501 -- embedded JavaScript source lines
"""signal-panel.js `wide`: the receiver strip's ⓘ opens the panel as a
full-width "Current frequency" section under the receiver (app.js
toggleFrequencyDetails) rather than the docked/overlay signal detail. It
follows the VFO, so a retarget must keep the previous detail on screen while
the new one loads (only an "updating…" cue), and a null target (receiver
disconnected) must say so instead of rendering nothing."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-panel.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wide_mode_renders_full_width_section_and_follows_target():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[^;]*;\n/gm, '')
  .replace('export class SignalPanel', 'class SignalPanel');

// html`` stub that flattens to a string so class names can be asserted on.
const flat = (v) => Array.isArray(v) ? v.map(flat).join('') : (v == null ? '' : String(v));
const html = (strings, ...vals) => strings.map((s, i) => s + flat(vals[i])).join('');

let resolveNext = null;
const api = {
  getSignalDetail: (params) => new Promise((resolve) => {
    resolveNext = () => resolve({ frequency_hz: params.frequency_hz, memory: null, evidence: [], messages: [], observations: [] });
  }),
};
const SignalPanel = vm.runInNewContext(src + '\nSignalPanel', {
  LitElement: class {}, html, customElements: { define() {} },
  api, console, setTimeout, clearTimeout, Map, Set, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  // helpers signal-panel imports from ../format.js etc. are stripped with the
  // imports; _body is not exercised here, only render()'s wide branch.
});

const view = Object.create(SignalPanel.prototype);
view.open = true; view.wide = true; view.target = null; view._detail = null; view._loading = false;
view._content = () => 'CONTENT';
view._wideTitle = SignalPanel.prototype._wideTitle;

// No VFO: the section still renders and says the receiver is disconnected.
let out = view.render();
assert.match(out, /signal-panel wide/);
assert.doesNotMatch(out, /overlay-backdrop/, 'wide mode must not be a modal sheet');
assert.doesNotMatch(out, /CONTENT/);
assert.match(out, /Receiver disconnected/);
assert.match(out, /Current frequency/);

// With a target the content renders inside the same wide section.
view.target = { frequency_hz: 433920000 };
out = view.render();
assert.match(out, /signal-panel wide/);
assert.match(out, /CONTENT/);

// Following a retune: the old detail stays while the new one loads.
view._detail = { frequency_hz: 433920000 };
view.target = { frequency_hz: 446006250 };
const pending = view._load();
assert.equal(view._loading, true);
assert.equal(view._detail.frequency_hz, 433920000, 'previous detail must stay on screen during the refetch');
assert.match(view._wideTitle(), /updating…/);
resolveNext();
await pending;
assert.equal(view._detail.frequency_hz, 446006250);
assert.equal(view._loading, false);
assert.doesNotMatch(view._wideTitle(), /updating…/);
assert.match(view._wideTitle(), /following radio/);
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_detail_response_is_ignored_and_escape_is_marked_handled():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[^;]*;\n/gm, '')
  .replace('export class SignalPanel', 'class SignalPanel');
const resolvers = [];
const api = { getSignalDetail: (p) => new Promise((res) => resolvers.push(() => res({ frequency_hz: p.frequency_hz, memory: null }))) };
const SignalPanel = vm.runInNewContext(src + '\nSignalPanel', {
  LitElement: class {}, html: () => '', customElements: { define() {} }, api, console, setTimeout, clearTimeout, Map, Set, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
});
const view = Object.create(SignalPanel.prototype);
view.open = true; view._detail = null;
view.target = { frequency_hz: 1 };
const first = view._load();
view.target = { frequency_hz: 2 };
const second = view._load();
resolvers[1](); await second;
assert.equal(view._detail.frequency_hz, 2);
assert.equal(view._loading, false);
resolvers[0](); await first;
assert.equal(view._detail.frequency_hz, 2, 'a late reply for the earlier target must not overwrite the newer detail');

// Same target refetched (live decode event / jurisdiction switch): the
// earlier request's late reply must not overwrite the newer one either.
const api2 = { getSignalDetail: (p) => new Promise((res) => resolvers.push((v) => res({ frequency_hz: p.frequency_hz, memory: { notes: v } }))) };
Object.assign(api, api2);
view.target = { frequency_hz: 3 };
const a = view._load(); const b = view._load();
resolvers[3]('new'); await b;
assert.equal(view._notesDraft, 'new'); assert.equal(view._loading, false);
resolvers[2]('old'); await a;
assert.equal(view._notesDraft, 'new', 'a late reply for the same target must not overwrite the newer detail');

let closed = 0, prevented = false, stopped = false;
view.dispatchEvent = (ev) => { if (ev.type === 'close') closed++; };
view._onKeydown({ key: 'Escape', preventDefault: () => (prevented = true), stopPropagation: () => (stopped = true) });
assert.equal(closed, 1); assert.ok(prevented && stopped, 'Escape must be marked handled so app.js does not close a second thing');
console.log('ok');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", str(SOURCE)],
        capture_output=True, text=True, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
