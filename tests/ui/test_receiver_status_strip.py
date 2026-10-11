# ruff: noqa: E501 -- embedded JavaScript source lines
"""receiver-status-strip.js (development notes Phase 1 "Station status strip"): the
pure exported helpers -- lamp color/lit per state, the relative "last
packet" label, and the compact channel/PHY label. Same vm.runInNewContext
harness as test_ble_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/receiver-status-strip.js"

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
def test_lamp_for_state():
    _run(r"""
const { lampFor } = vm.runInNewContext(raw + '\n({ lampFor })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

const eq = (a, b) => assert.equal(JSON.stringify(a), JSON.stringify(b));
eq(lampFor('receiving'), { color: 'var(--ok)', lit: true });
eq(lampFor('stale'), { color: 'var(--accent)', lit: true });
eq(lampFor('starting'), { color: 'var(--accent)', lit: true });
eq(lampFor('failed'), { color: 'var(--danger)', lit: true });
eq(lampFor('firmware_mismatch'), { color: 'var(--danger)', lit: true });
eq(lampFor('wrong_baud'), { color: 'var(--danger)', lit: true });
eq(lampFor('no_device'), { color: 'var(--danger)', lit: true });
eq(lampFor('stopped'), { color: null, lit: false });
eq(lampFor(undefined), { color: null, lit: false });
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_since_label():
    _run(r"""
const { sinceLabel } = vm.runInNewContext(raw + '\n({ sinceLabel })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.equal(sinceLabel(null), null);
assert.equal(sinceLabel(100, 100.4), '<1s ago');
assert.equal(sinceLabel(100, 130), '30s ago');
assert.equal(sinceLabel(100, 220), '2m ago');
assert.equal(sinceLabel(100, 100 + 3 * 3600), '3h ago');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_channel_label_per_kind_and_backend():
    _run(r"""
const { channelLabel } = vm.runInNewContext(raw + '\n({ channelLabel })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.equal(channelLabel('wifi', { channel: 36 }), 'ch36');
assert.equal(channelLabel('wifi', {}), '');
assert.equal(channelLabel('wifi', { channel: null, configured_channel: 10 }), 'ch10', 'stopped: the channel Start would use');
assert.equal(channelLabel('wifi', { channel: 6, configured_channel: 10 }), 'ch6', 'running: the tuned channel wins');
// hci backend reports no per-report channel (docs/ble.md): nothing shown.
assert.equal(channelLabel('ble', { backend: 'hci' }), '');
assert.equal(channelLabel('ble', { backend: 'sniffle', phy: 'coded', channel: '37' }), 'CODED ch37');
assert.equal(channelLabel('ble', { backend: 'sniffle', phy: '1m', channel: 'hop' }), '1M hop');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_lamp_click_opens_its_discover_tab():
    _run(r"""
const html = (strings, ...values) => ({ strings, values });
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; this.bubbles = init?.bubbles; } }
const { ReceiverStatusStrip } = vm.runInNewContext(raw + '\n({ ReceiverStatusStrip })', {
  LitElement: class {}, html, CustomEvent, customElements: { define() {} }, console,
});
const strip = Object.create(ReceiverStatusStrip.prototype);
const sent = [];
strip.dispatchEvent = (ev) => sent.push(ev);
for (const kind of ['wifi', 'ble']) {
  const tpl = strip._lamp(kind, { enabled: true, state: 'receiving', channel: 6 });
  const i = tpl.strings.findIndex((s) => s.trimEnd().endsWith('@click='));
  tpl.values[i]();
}
assert.deepEqual(sent.map((e) => [e.type, e.detail.view, e.bubbles]),
  [['open-discover', 'wifi', true], ['open-discover', 'ble', true]]);
""")
