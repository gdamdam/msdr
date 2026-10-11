# ruff: noqa: E501 -- embedded JavaScript source lines
"""finds-view.js: stale-load guard and live finds honouring the text query. Runs the component source in a node vm with stubs, like
test_decode_view_live_hold.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components/finds-view.js"

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


def test_stale_finds_response_is_dropped():
    run(r"""
const d = [deferred(), deferred()];
let n = 0;
const V = load('FindsView', { api: { listFinds: () => d[n++].promise } });
const v = Object.create(V.prototype);
Object.assign(v, { _finds: [], _counts: {}, _status: null, _query: '' });
const a = v._load(), b = v._load();
d[1].resolve({ finds: [{ id: 2 }], counts: { new: 1 } });
await b;
d[0].resolve({ finds: [{ id: 1 }], counts: { new: 5 } });
await a;
assert.equal(v._finds[0].id, 2);
assert.equal(v._counts.new, 1);
""")


def test_merge_find_respects_text_query():
    run(r"""
const V = load('FindsView');
const v = Object.create(V.prototype);
Object.assign(v, { _finds: [], _counts: {}, _status: null, _query: 'Beacon' });
v._mergeFind({ find: { id: 1, label: 'other', status: 'new' }, created: true });
assert.equal(v._finds.length, 0);
v._mergeFind({ find: { id: 2, label: 'my beacon', status: 'new' }, created: true });
assert.equal(v._finds.length, 1);
""")
