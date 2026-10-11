# ruff: noqa: E501 -- embedded JavaScript source lines
"""library-view.js: an older, slower _load() response must not overwrite a newer one. Runs the component source in a node vm with stubs, like
test_decode_view_live_hold.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components/library-view.js"

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


def test_stale_library_response_is_dropped():
    run(r"""
const d = [deferred(), deferred()];
let n = 0;
const api = { library: () => d[n++].promise };
const V = load('LibraryView', { api, CustomEvent: class {} });
const v = Object.create(V.prototype);
Object.assign(v, { _items: [], _total: 0, _page: 0, _params: () => ({}) });
const a = v._load(), b = v._load();
d[1].resolve({ items: [{ id: 'new' }], total: 1 });
await b;
d[0].resolve({ items: [{ id: 'old' }], total: 9 });
await a;
assert.equal(v._items[0].id, 'new');
assert.equal(v._total, 1);
""")
