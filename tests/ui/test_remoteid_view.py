# ruff: noqa: E501 -- embedded JavaScript source lines
"""remoteid-view.js: deselecting a UAS drops its in-flight detail fetch -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/remoteid-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nRemoteIdView', {
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
def test_deselect_discards_pending_detail():
    _run(r"""
let resolve;
const C = load({ api: { getRemoteIdUas: () => new Promise((r) => { resolve = r; }) } });
const v = Object.create(C.prototype);
Object.assign(v, { _selectedKey: null, _detail: null, _detailSeq: 0 });
v._select('k1');
v._select('k1'); // deselect while the fetch is pending
resolve({ key: 'k1', sources: [] });
await tick();
assert.equal(v._selectedKey, null);
assert.equal(v._detail, null, 'late response must not re-show the pane');
assert.equal(v._renderDetail(), '');
v._detail = { key: 'k1', sources: [] };
assert.equal(v._renderDetail(), '', 'no pane without a selection');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sort_rows_orders_by_key_and_keeps_default():
    _run(r"""
const { sortRows } = vm.runInNewContext(raw + '\n({ sortRows })', { LitElement: class {}, html: () => {}, customElements: { define() {} }, console });
const rows = [
  { key: 'b', uas_id_text: 'B', last_seen: 5, sources: ['wifi'] },
  { key: 'a', uas_id_text: 'a', last_seen: null, sources: [] },
  { key: 'c', uas_id_text: 'C', last_seen: 9, sources: ['ble'], conflict: true },
];
const keys = (r) => r.map((x) => x.key).join('');
assert.equal(keys(sortRows(rows, { key: null, dir: 'asc' })), 'bac');
assert.equal(keys(sortRows(rows, { key: 'uas', dir: 'asc' })), 'abc');
assert.equal(keys(sortRows(rows, { key: 'uas', dir: 'desc' })), 'cba');
assert.equal(keys(sortRows(rows, { key: 'last_seen', dir: 'asc' })), 'bca', 'missing last');
assert.equal(keys(sortRows(rows, { key: 'last_seen', dir: 'desc' })), 'cba', 'missing last');
assert.equal(keys(sortRows(rows, { key: 'flags', dir: 'desc' })), 'cba');
""")
