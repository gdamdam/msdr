# ruff: noqa: E501 -- embedded JavaScript source lines
"""app.js: confirmInterrupt supersession, journal-day and state-fetch races. Runs the component source in a node vm with stubs, like
test_decode_view_live_hold.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const strip = (src) => src.replace(/^import[\s\S]*?;\n/gm, '').replace(/^export /gm, '');
const src = strip(fs.readFileSync(process.argv[1], 'utf8'));
const tick = () => new Promise((r) => setTimeout(r, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const load = (cls, extra = {}) => vm.runInNewContext(src + `\n${cls}`, {
  LitElement: class { dispatchEvent() {} }, html: () => '', css: () => '', svg: () => '', customElements: { define() {} },
  console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, Number, ...extra,
});
"""


def run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SRC)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


def test_second_confirm_interrupt_cancels_the_first():
    run(r"""
const api = { ownerConflicts: async () => ({ interrupts: ['survey'], message: 'm' }) };
const App = load('MsdrApp', { api });
const a = Object.create(App.prototype);
a._interruptConfirm = null;
const first = a.confirmInterrupt('x');
await tick();
const second = a.confirmInterrupt('y');
assert.equal(await first, false);
await tick();
a._resolveInterrupt(true);
assert.equal(await second, true);
""")


def test_stale_journal_day_is_dropped():
    run(r"""
const d = { a: deferred(), b: deferred() };
const App = load('MsdrApp', { api: { journalDay: (day) => d[day].promise } });
const v = Object.create(App.prototype);
v._journalSeq = 0; v.journal = null;
v._run = (fn) => fn();
const x = v._loadJournalDay('a'), y = v._loadJournalDay('b');
d.b.resolve('B'); await y;
d.a.resolve('A'); await x;
assert.equal(v.journal, 'B');
""")


def test_http_state_does_not_overwrite_newer_ws_push():
    run(r"""
const d = deferred();
const App = load('MsdrApp', { api: { getState: () => d.promise } });
const v = Object.create(App.prototype);
v._wsStateSeq = 0; v.state = null;
const p = v._fetchState();
v._wsStateSeq += 1; v.state = 'ws';
d.resolve('http'); await p;
assert.equal(v.state, 'ws');
// No push in between: the fetch applies.
const App2 = load('MsdrApp', { api: { getState: async () => 'http2' } });
const w = Object.create(App2.prototype); w._wsStateSeq = 0;
await w._fetchState();
assert.equal(w.state, 'http2');
""")


def test_discover_view_buttons_are_tabs():
    src = SRC.read_text()
    nav = src[src.index('class="geo-toolbar discover-views"'):][:700]
    assert 'role="tablist"' in nav and 'role="tab"' in nav and "aria-selected=" in nav and "aria-pressed" not in nav
