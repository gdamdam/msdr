"""Palette ordering, hover indexing, and async request races via Node vm."""

import shutil
import subprocess
from pathlib import Path

import pytest

PALETTE_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/palette.js"
MODAL_FOCUS_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/modal-focus.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_group_order_enter_hover_and_stale_recent_search_results():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export function', 'function')
  .replace('export class Palette', 'class Palette');
const events = [];
class CustomEventStub {
  constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
}
const Palette = vm.runInNewContext(source + '\nPalette', {
  LitElement: class {},
  html: (strings, ...values) => ({ strings: [...strings], values }),
  mhz: String, catalogOnAir() {}, catalogWindowLabel() {},
  containModalFocus() {}, restoreModalFocus() {}, trapModalTab() {},
  CustomEvent: CustomEventStub, customElements: { define() {} },
});
const palette = Object.create(Palette.prototype);
const dialog = { querySelectorAll() { return []; } };
Object.assign(palette, {
  open: true, tagsById: {}, tz: null, _query: '', _index: 0,
  querySelector(selector) { return selector.includes('role=') ? dialog : null; },
  dispatchEvent(event) { events.push(event); return true; },
  _results: [
    {kind: 'action', id: 'probe', title: 'action', subtitle: ''},
    {kind: 'signal', id: 'signal-one', title: 'signal one', subtitle: ''},
    {kind: 'tune_frequency', id: 'tune', title: 'tune', subtitle: '', frequency_hz: 145800000},
    {kind: 'signal', id: 'signal-two', title: 'signal two', subtitle: ''},
  ],
});

assert.deepEqual(
  Array.from(palette._orderedResults, result => result.title),
  ['tune', 'signal one', 'signal two', 'action'],
);
palette._onKeydown({key: 'Enter', preventDefault() {}});
assert.equal(events.find(event => event.type === 'tune-frequency')?.detail.hz, 145800000);

function templatesWithHover(root, found = []) {
  if (Array.isArray(root)) {
    for (const value of root) templatesWithHover(value, found);
  } else if (root?.strings) {
    if (root.strings.some(part => part.includes('@mouseenter='))) found.push(root);
    for (const value of root.values) templatesWithHover(value, found);
  }
  return found;
}
const rows = templatesWithHover(palette.render());
assert.equal(rows.length, 4);
const firstSignal = rows[1];
const hoverSlot = firstSignal.strings.findIndex(part => part.includes('@mouseenter='));
firstSignal.values[hoverSlot]();
assert.equal(palette._index, 1, 'hover must retain that rendered row index');

let resolveRecent;
let resolveOld;
let resolveNew;
const timers = new Map();
let nextTimerId = 0;
const PaletteWithRequests = vm.runInNewContext(source + '\nPalette', {
  LitElement: class {}, html: (...parts) => parts,
  mhz: String, catalogOnAir() {}, catalogWindowLabel() {},
  containModalFocus() {}, restoreModalFocus() {}, trapModalTab() {},
  CustomEvent: CustomEventStub, customElements: { define() {} },
  recent: () => new Promise(resolve => { resolveRecent = resolve; }),
  searchPalette: query => new Promise(resolve => {
    if (query === 'old') resolveOld = resolve;
    else if (query === 'new') resolveNew = resolve;
  }),
  setTimeout(callback) { const id = ++nextTimerId; timers.set(id, callback); return id; },
  clearTimeout(id) { timers.delete(id); },
});
const requests = Object.create(PaletteWithRequests.prototype);
Object.assign(requests, {
  open: true, _query: '', _index: 0, _results: [], _requestId: 0,
  dispatchEvent() { return true; },
});
const runTimer = () => {
  const [id, callback] = [...timers.entries()].at(-1);
  timers.delete(id);
  return callback();
};
const recentTask = requests._loadEmpty();
requests._onInput({target: {value: 'old'}});
const oldTask = runTimer();
requests._onInput({target: {value: 'new'}});
const newTask = runTimer();
resolveNew([{kind: 'action', id: 'new', title: 'new result'}]);
await newTask;
assert.equal(requests._results[0].id, 'new');
resolveRecent([{id: 'recent', name: 'stale recent', frequency_hz: 1000000}]);
await recentTask;
assert.equal(requests._results[0].id, 'new', 'stale recent response must not replace search');
resolveOld([{kind: 'action', id: 'old', title: 'old result'}]);
await oldTask;
assert.equal(requests._results[0].id, 'new', 'older search response must not replace newer search');

const focusSource = fs.readFileSync(process.argv[2], 'utf8')
  .replace(/^export function /gm, 'function ');
const focus = vm.runInNewContext(focusSource + '\n({containModalFocus, trapModalTab})', {});
let lowerFocus = 0;
let topFocus = 0;
const top = {
  focus() { topFocus += 1; }, contains() { return false; }, querySelectorAll() { return []; },
};
const lower = {
  focus() { lowerFocus += 1; }, contains() { return false; }, querySelectorAll() { return []; },
};
const outside = {};
const ownerDocument = { querySelectorAll: () => [lower, top], activeElement: outside };
lower.ownerDocument = ownerDocument;
top.ownerDocument = ownerDocument;
focus.containModalFocus({target: outside}, lower, lower);
assert.equal(lowerFocus, 0, 'background modal must not pull focus from the topmost modal');
focus.containModalFocus({target: outside}, top, top);
assert.equal(topFocus, 1);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PALETTE_JS), str(MODAL_FOCUS_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_empty_state_lists_recent_signals_then_all_actions():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export function', 'function')
  .replace('export class Palette', 'class Palette');
const emptyStateResults = vm.runInNewContext(source + '\nemptyStateResults', {
  LitElement: class {}, html() {}, customElements: { define() {} },
});
const out = emptyStateResults([{ kind: 'signal', id: 's1' }]);
assert.equal(out[0].id, 's1');
const actions = out.slice(1);
assert.ok(actions.length > 0 && actions.every((r) => r.kind === 'action' && r.id && r.title));
assert.ok(actions.some((r) => r.id === 'capture'));
assert.equal(emptyStateResults([]).length, actions.length);
"""
    subprocess.run(["node", "--input-type=module", "-e", script, str(PALETTE_JS)], check=True)
