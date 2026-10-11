# ruff: noqa: E501 -- embedded JavaScript source lines
"""palette.js: Enter with a pending typed query waits for fresh results -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/palette.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nPalette', {
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
def test_enter_waits_for_fresh_results_not_stale_first():
    _run(r"""
const searches = [];
const C = load({ mhz: String, catalogOnAir() {}, catalogWindowLabel() {}, containModalFocus() {}, restoreModalFocus() {}, trapModalTab() {},
  recent: async () => [], clearTimeout, setTimeout,
  searchPalette: async (q) => { searches.push(q); return [{ kind: 'tune_frequency', frequency_hz: 162550000, title: q }]; } });
const events = [];
const p = Object.create(C.prototype);
Object.assign(p, { open: true, _query: '', _index: 0, _requestId: 0, _debounce: null, _searching: false,
  _results: [{ kind: 'signal', id: 'stale', title: 'old recent' }],
  querySelector: () => ({ querySelectorAll: () => [] }), dispatchEvent(e) { events.push(e); return true; } });
p._onInput({ target: { value: '162.55' } });
p._onKeydown({ key: 'Enter', preventDefault() {}, });
await tick(); await tick();
assert.deepEqual(searches, ['162.55']);
assert.equal(events.filter((e) => e.type === 'tune-signal').length, 0, 'stale recent signal not tuned');
assert.equal(events.find((e) => e.type === 'tune-frequency')?.detail.hz, 162550000);
clearTimeout(p._debounce);
// settled results: Enter is synchronous again
events.length = 0; p._searching = false;
p._onKeydown({ key: 'Enter', preventDefault() {} });
assert.ok(events.some((e) => e.type === 'tune-frequency'));
""")
