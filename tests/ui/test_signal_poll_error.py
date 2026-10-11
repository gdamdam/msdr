# ruff: noqa: E501 -- embedded JavaScript source lines
"""signal-workbench / signal-instruments / ble-setup-section: a good poll clears only a poll failure -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/signal-workbench.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nSignalWorkbench', {
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
def test_signal_workbench_poll_error_cleared():
    _run(r"""
let fail = true;
const C = load({ fetchJson: async () => { if (fail) throw new Error('Failed to fetch'); return { tuned_hz: 0 }; } });
const v = Object.create(C.prototype);
Object.assign(v, { error: '', isConnected: true, _fetching: false, _open: false, _wasRunning: false, frequencies: 'x', busy: false, pending: '' });
await v._refresh();
assert.equal(v.error, 'Failed to fetch');
fail = false;
await v._refresh();
assert.equal(v.error, '', 'next good poll clears it');
v.error = 'Start failed: x'; v._pollFailed = false;
await v._refresh();
assert.equal(v.error, 'Start failed: x', 'action error is kept');
""")


SOURCE_SIGNALINSTRUMENTS = SOURCE.parent / "signal-instruments.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_signal_instruments_poll_error_cleared():
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE.replace("raw + '\\nSignalWorkbench'", "raw + '\\nSignalInstruments'") + r"""
let fail = true;
const C = load({ fetchJson: async () => { if (fail) throw new Error('Failed to fetch'); return { live_receiver: null }; } });
const v = Object.create(C.prototype);
Object.assign(v, { _error: '', isConnected: true, _fetching: false, _live: null });
await v._refresh();
assert.equal(v._error, 'Failed to fetch');
fail = false;
await v._refresh();
assert.equal(v._error, '');
v._error = 'Live inspection failed: x'; v._pollFailed = false;
await v._refresh();
assert.equal(v._error, 'Live inspection failed: x');
""", str(SOURCE_SIGNALINSTRUMENTS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


SOURCE_BLESETUPSECTION = SOURCE.parent / "ble-setup-section.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ble_setup_section_poll_error_cleared():
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE.replace("raw + '\\nSignalWorkbench'", "raw + '\\nBleSetupSection'") + r"""
let fail = true;
const C = load({ api: { getBleStatus: async () => { if (fail) throw new Error('Failed to fetch'); return { ok: 1 }; } } });
const v = Object.create(C.prototype);
Object.assign(v, { _error: null, _status: null });
await v._refreshStatus();
assert.equal(v._error, 'Failed to fetch');
fail = false;
await v._refreshStatus();
assert.equal(v._error, null);
v._error = 'Could not save BLE settings: x'; v._pollFailed = false;
await v._refreshStatus();
assert.equal(v._error, 'Could not save BLE settings: x');
""", str(SOURCE_BLESETUPSECTION)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
