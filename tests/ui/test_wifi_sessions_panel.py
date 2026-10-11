# ruff: noqa: E501 -- embedded JavaScript source lines
"""wifi-sessions-panel.js: Remote ID decode of session B never shows session A data -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/wifi-sessions-panel.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nWifiSessionsPanel', {
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
def test_remote_id_stale_response_discarded():
    _run(r"""
const waiting = {};
const C = load({ api: { getWifiSessionRemoteId: (id) => new Promise((r) => { waiting[id] = r; }) } });
const v = Object.create(C.prototype);
Object.assign(v, { _remoteIdFor: null, _remoteIdData: null, _remoteIdSeq: 0 });
const a = v._toggleRemoteId('A');
const b = v._toggleRemoteId('B');
waiting.B({ groups: ['b'] }); await b;
waiting.A({ groups: ['a'] }); await a;
assert.equal(v._remoteIdFor, 'B');
assert.deepEqual(v._remoteIdData, { groups: ['b'] });
""")
