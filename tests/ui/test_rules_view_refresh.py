# ruff: noqa: E501 -- embedded JavaScript source lines
"""rules-view.js: _refresh drops stale responses -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/rules-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nRulesView', {
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
def test_stale_refresh_response_discarded():
    _run(r"""
const pending = [];
const mkApi = () => {
  const call = () => new Promise((r) => pending.push(r));
  return { listRules: call, listRuleEvents: call, listWifiNetworks: call, listBleAdvertisers: call, listLabels: call, getRemoteIdLive: call };
};
const C = load({ api: mkApi() });
const v = Object.create(C.prototype);
Object.assign(v, { _rules: [], _events: [], _wifi: [], _ble: [], _labels: {}, _drones: [], _error: null, _reqSeq: 0, _refreshing: false });
const first = v._refresh();
assert.equal(v._refreshing, true);
const firstCalls = pending.splice(0);
const second = v._refresh();
const secondCalls = pending.splice(0);
const body = (n) => ({ items: [n] });
secondCalls.forEach((r) => r(body('new')));
await second;
firstCalls.forEach((r) => r(body('old')));
await first;
assert.deepEqual(v._rules, ['new'], 'late older response must not overwrite');
assert.equal(v._refreshing, false);
""")
