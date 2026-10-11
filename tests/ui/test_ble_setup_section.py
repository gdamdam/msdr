# ruff: noqa: E501 -- embedded JavaScript source lines
"""ble-setup-section.js (Setup -> BLE receiver card, development notes Sniffle
backend): the two pure exported helpers -- start-disabled reasons and the
plain-language state explanations shown under the status line. Same
vm.runInNewContext harness as test_ble_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/ble-setup-section.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
"""


def _run(body):
    script = PRELUDE + body
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_start_disabled_reason_per_state_and_backend():
    _run(r"""
const { startDisabledReason } = vm.runInNewContext(raw + '\n({ startDisabledReason })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.match(startDisabledReason(null, false), /Enable BLE receiver/);
assert.equal(startDisabledReason(null, true), null);
assert.match(startDisabledReason({ state: 'dependency_missing', backend: 'hci' }, true), /pyusb/);
assert.match(startDisabledReason({ state: 'dependency_missing', backend: 'sniffle' }, true), /pyserial/);
assert.match(startDisabledReason({ state: 'no_device', backend: 'sniffle' }, true), /CP210x/);
assert.match(startDisabledReason({ state: 'firmware_mismatch' }, true), /firmware/i);
assert.match(startDisabledReason({ state: 'wrong_baud' }, true), /baud/i);
assert.equal(startDisabledReason({ state: 'receiving' }, true), 'Already running');
assert.equal(startDisabledReason({ state: 'stopped' }, true), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_state_explanation_never_flashes_firmware():
    _run(r"""
const { stateExplanation } = vm.runInNewContext(raw + '\n({ stateExplanation })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.equal(stateExplanation(null), null);
assert.equal(stateExplanation({ state: 'receiving' }), null);
const fw = stateExplanation({ state: 'firmware_mismatch' });
assert.match(fw, /flash the matching Sniffle build/);
assert.match(fw, /CP2102 vs CP2102N/);
assert.match(fw, /msdr never flashes firmware/);
assert.match(stateExplanation({ state: 'wrong_baud' }), /baud rate/);
assert.match(stateExplanation({ state: 'no_device', backend: 'sniffle' }), /CP210x serial port/);
assert.match(stateExplanation({ state: 'no_device', backend: 'hci' }), /Bluetooth-class USB/);
""")
