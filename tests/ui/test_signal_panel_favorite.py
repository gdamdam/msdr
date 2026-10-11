# ruff: noqa: E501 -- embedded JavaScript source lines
"""signal-panel.js's FAV button: saves the frequency when nothing is saved
yet, adds the signal to Favorites only when it isn't one, then opens it in the
editor drawer via `select-signal`."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-panel.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fav_saves_favorites_and_opens_editor():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[^;]*;\n/gm, '')
  .replace('export class SignalPanel', 'class SignalPanel');

const memory = { id: 'sig-1', name: 'UNKNOWN 17.269000 MHz', frequency_hz: 17269000 };
const calls = [];
const api = {
  createSignal: async (body) => { calls.push(['create', body.frequency_hz]); return memory; },
  addListItems: async (list, ids) => { calls.push(['fav', list, ids]); },
  getSignalDetail: async () => ({ frequency_hz: 17269000, memory, evidence: [] }),
};
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; } }
// Arrays/objects built inside the vm realm fail deepStrictEqual's prototype check.
const same = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b));
const SignalPanel = vm.runInNewContext(src + '\nSignalPanel', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout, CustomEvent, Date,
});

function panel(detail, favorites) {
  const view = Object.create(SignalPanel.prototype);
  Object.assign(view, { open: true, target: { frequency_hz: 17269000 }, _detail: detail,
    favorites, jurisdiction: 'US', _notesDraft: '' });
  view.events = [];
  view.dispatchEvent = (ev) => view.events.push(ev);
  return view;
}

// Nothing saved: SAVE creates the signal, it becomes a favorite, the editor opens.
let view = panel({ frequency_hz: 17269000, memory: null, evidence: [] }, []);
await view._favoriteAndEdit();
same(calls, [['create', 17269000], ['fav', 'favorites', ['sig-1']]]);
const types = view.events.map((e) => e.type);
assert.ok(types.includes('favorites-changed'));
const select = view.events.find((e) => e.type === 'select-signal');
same(select.detail, { kind: 'signal', item: memory });

// Already a favorite: no API calls, just open the editor.
calls.length = 0;
view = panel({ frequency_hz: 17269000, memory, evidence: [] }, [{ id: 'sig-1' }]);
await view._favoriteAndEdit();
same(calls, []);
same(view.events.map((e) => e.type), ['select-signal']);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
