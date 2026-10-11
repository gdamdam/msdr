# ruff: noqa: E501 -- embedded JavaScript source lines
"""listen-workbench.js: the decode deck gets decodeEvents (CW transcript) -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/listen-workbench.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nListenWorkbench', {
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
def test_decode_events_passed_to_deck():
    _run(r"""
const C = load();
assert.ok('decodeEvents' in C.properties, 'declared');
const w = Object.create(C.prototype);
Object.assign(w, { decoderFeature: true, decodeEvents: [{ id: 1 }], _tab: 'listen', _panelHeight: 300, _content: () => '' });
const found = [];
const walk = (n) => { if (Array.isArray(n)) n.forEach(walk); else if (n?.strings) { if (n.strings.some((p) => p.includes('<decode-deck'))) found.push(n); n.values.forEach(walk); } };
walk(w.render());
const deck = found[0];
assert.ok(deck, 'deck mounted');
const i = deck.strings.findIndex((p) => p.includes('.decodeEvents='));
assert.ok(i >= 0, 'deck template binds .decodeEvents');
assert.deepEqual(deck.values[i], [{ id: 1 }]);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_watch_tab_mounts_signal_workbench_with_receiver():
    _run(r"""
const C = load();
const w = Object.create(C.prototype);
Object.assign(w, { _tab: 'watch', receiver: { connected: true }, recorderArmed: true });
const out = w._content();
assert.ok(out.strings.some((p) => p.includes('<signal-workbench')), 'workbench mounted');
assert.deepEqual(out.values, [w.receiver, true]);
""")
