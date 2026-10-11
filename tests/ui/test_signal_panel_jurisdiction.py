# ruff: noqa: E501 -- embedded JavaScript source lines
"""signal-panel.js's allocation evidence comes from the server's
/api/signal-detail, which reads settings.allocation_jurisdiction at request
time (msdr/api/services/allocations.py:allocation_context ->
_allocation_jurisdiction). The panel caches `_detail` and only used to
refetch on `target`/`latestDecodeEvent` changes, so switching jurisdiction
while a signal is open left the previous jurisdiction's allocation evidence
on screen. `jurisdiction` must trigger a refetch too."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-panel.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_jurisdiction_change_refetches_signal_detail():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
// signal-panel.js has a multi-line `import {...} from "../format.js"` block,
// unlike the single-line imports elsewhere -- strip across newlines too.
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[^;]*;\n/gm, '')
  .replace('export class SignalPanel', 'class SignalPanel');

let calls = [];
const api = {
  getSignalDetail: async (params) => {
    calls.push(params);
    return { frequency_hz: params.frequency_hz, memory: {}, evidence: [] };
  },
};
const SignalPanel = vm.runInNewContext(src + '\nSignalPanel', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(SignalPanel.prototype);
view.open = true;
view.target = { frequency_hz: 100000000 };
view._detail = null;
view.jurisdiction = 'US';

await view._load();
assert.equal(calls.length, 1, 'initial load must fetch signal-detail once');

// Same lit `updated()` hook the component implements, with only
// `jurisdiction` in the changed-properties map (target untouched).
view.jurisdiction = 'ITU1';
view.updated(new Map([['jurisdiction', 'US']]));
await new Promise((r) => setTimeout(r, 0));
assert.equal(calls.length, 2, 'a jurisdiction change with the panel open must refetch signal-detail');

// No target open -- a jurisdiction change must not fetch anything.
view.target = null;
calls = [];
view.jurisdiction = 'US';
view.updated(new Map([['jurisdiction', 'ITU1']]));
await new Promise((r) => setTimeout(r, 0));
assert.equal(calls.length, 0, 'no open target means nothing to refetch');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
