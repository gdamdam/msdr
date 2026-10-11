# ruff: noqa: E501 -- embedded JavaScript source lines
"""receiver-loss.js: pure detection rules behind the "receiver lost" bar.
Same vm harness as test_wifi_view.py (imports stripped, `export` dropped)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/receiver-loss.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const m = vm.runInNewContext(raw + '\n({ detectLoss, isRecovered, surveyNote, shouldNotify })', { console });
const ok = { wanted: true, state: 'receiving', channel: 6, last_error: null, retry_attempt: 0 };
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_wifi_loss_only_on_transition():
    _run(r"""
assert.equal(m.detectLoss('wifi', null, { ...ok, state: 'no_device', last_error: 'gone' }), null);
const a = m.detectLoss('wifi', ok, { ...ok, state: 'no_device', last_error: 'no such device.' }, 5);
assert.equal(a.title, 'Wi-Fi adapter disconnected');
assert.equal(a.detail, 'no such device. Capture on channel 6 stopped.');
assert.equal(a.at, 5); assert.equal(a.channel, 6);
// Already lost in prev: no second alert; and not wanted: no alert.
const lost = { ...ok, state: 'failed' };
assert.equal(m.detectLoss('wifi', lost, { ...lost, last_error: 'x' }), null);
assert.equal(m.detectLoss('wifi', ok, { ...lost, wanted: false }), null);
assert.equal(m.detectLoss('wifi', { ...ok, state: 'stopped' }, lost), null);
""")


def test_wifi_early_retry_usb_error_clause():
    _run(r"""
const early = { ...ok, state: 'starting', retry_attempt: 1, last_error: 'No such device' };
assert.ok(m.detectLoss('wifi', ok, early));
// Same retry state with an unrelated error is not a disconnect.
assert.equal(m.detectLoss('wifi', ok, { ...early, last_error: 'bad channel' }), null);
// ...and the error with no retry yet isn't either.
assert.equal(m.detectLoss('wifi', ok, { ...early, retry_attempt: 0 }), null);
// After the early alert, the later "failed" push must not re-alert.
assert.equal(m.detectLoss('wifi', early, { ...early, state: 'failed' }), null);
""")


def test_wifi_recovery_and_survey_note():
    _run(r"""
assert.equal(m.isRecovered('wifi', { ...ok, state: 'receiving' }), true);
assert.equal(m.isRecovered('wifi', { ...ok, state: 'starting' }), true);
assert.equal(m.isRecovered('wifi', { ...ok, state: 'starting', last_error: 'no such device' }), false);
assert.equal(m.isRecovered('wifi', { ...ok, state: 'failed' }), false);
assert.equal(m.surveyNote({ state: 'running', current_channel: 11 }), 'The channel survey stopped at channel 11.');
assert.equal(m.surveyNote({ state: 'done' }), '');
assert.equal(m.surveyNote(null), '');
""")


def test_ble_loss_and_recovery():
    _run(r"""
const b = { wanted: true, state: 'receiving', last_error: null, retry_attempt: 0 };
const a = m.detectLoss('ble', b, { ...b, state: 'no_device', last_error: 'no usb device' });
assert.equal(a.title, 'BLE dongle disconnected');
assert.equal(a.detail, 'no usb device. Scanning stopped.');
assert.ok(m.detectLoss('ble', b, { ...b, state: 'claimed' }));
assert.equal(m.detectLoss('ble', null, { ...b, state: 'no_device' }), null);
assert.ok(m.detectLoss('ble', b, { ...b, state: 'starting', retry_attempt: 2, last_error: 'Disconnected' }));
assert.equal(m.isRecovered('ble', b), true);
assert.equal(m.isRecovered('ble', { ...b, state: 'no_device' }), false);
""")


def test_sdrconnect_loss_and_recovery():
    _run(r"""
const a = m.detectLoss('sdrconnect', { connected: true }, { connected: false });
assert.equal(a.title, 'SDRconnect connection lost');
assert.match(a.detail, /See LINK for diagnostics/);
assert.equal(m.detectLoss('sdrconnect', { connected: false }, { connected: false }), null);
assert.equal(m.detectLoss('sdrconnect', null, { connected: false }), null);
const w = m.detectLoss('sdrconnect', { connected: true }, { connected: false, wsDown: true });
assert.match(w.detail, /link to msdr dropped/);
assert.equal(m.isRecovered('sdrconnect', { connected: true }), true);
assert.equal(m.isRecovered('sdrconnect', { connected: true, wsDown: true }), false);
assert.equal(m.isRecovered('sdrconnect', { connected: false }), false);
""")


def test_should_notify_only_when_opted_in_granted_and_hidden():
    _run(r"""
assert.equal(m.shouldNotify({ pref: true, permission: 'granted', hidden: true }), true);
assert.equal(m.shouldNotify({ pref: true, permission: 'granted', hidden: false }), false);
assert.equal(m.shouldNotify({ pref: false, permission: 'granted', hidden: true }), false);
assert.equal(m.shouldNotify({ pref: true, permission: 'denied', hidden: true }), false);
""")
