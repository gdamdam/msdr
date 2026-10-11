# ruff: noqa: E501 -- embedded JavaScript source lines
"""ble-view.js (Discover's BLE tab, docs/ble.md "UI contract"): sort-header
cycling, the live-feed merge helper, start-blocker reasons per receiver
state, and that the Advertisers table renders rows from a stubbed fetch.
Same vm.runInNewContext harness as test_wifi_view.py: imports are stripped
and `export` is dropped so the source can run as a plain script with stubbed
LitElement/html/customElements/api globals."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/ble-view.js"

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
def test_start_blocker_reasons_per_state():
    _run(r"""
const { bleStartBlocker } = vm.runInNewContext(raw + '\n({ bleStartBlocker })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});

assert.match(bleStartBlocker(null), /loading/i);
assert.match(bleStartBlocker({ state: 'dependency_missing' }), /pyusb/i);
assert.match(bleStartBlocker({ state: 'no_device' }), /dongle/i);
assert.match(bleStartBlocker({ state: 'claimed' }), /macOS/);
assert.match(bleStartBlocker({ state: 'no_le' }), /LE support/);
assert.equal(bleStartBlocker({ state: 'stopped' }), null, 'disabled-in-settings is not a blocker');
assert.equal(bleStartBlocker({ state: 'receiving' }), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_merge_and_sort_header_cycle():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const ctx = { LitElement: class {}, html: () => '', svg: () => '', customElements: { define() {} }, console, api: {} };
vm.runInNewContext(src + '\nglobalThis.out = { mergeLiveFrames, liveFrameText, nextSort };', ctx);
const { mergeLiveFrames, liveFrameText, nextSort } = ctx.out;

// Newest-first merge, deduped and bounded by `keep` -- same contract as
// wifi-view.js's mergeLiveFrames (both key off a bare `seq`).
let st = mergeLiveFrames([], [{ seq: 1, evt: 'ADV_IND' }, { seq: 2, evt: 'ADV_IND' }], 0, 3);
assert.equal(JSON.stringify(st.frames.map(f => f.seq)), JSON.stringify([2, 1]));
st = mergeLiveFrames(st.frames, [{ seq: 2 }, { seq: 3 }, { seq: 4 }], st.lastSeq, 3);
assert.equal(JSON.stringify(st.frames.map(f => f.seq)), JSON.stringify([4, 3, 2]));
assert.equal(st.lastSeq, 4);

assert.equal(
  liveFrameText({ evt: 'ADV_IND', name: 'Thingy', company: 'Nordic', rssi: -58, length: 24 }),
  'ADV_IND · “Thingy” · Nordic · -58 dBm · 24 B'
);
assert.match(liveFrameText({ evt: 'ADV_NONCONN_IND', addr: 'aa:bb:cc:dd:ee:ff', length: 12, remote_id: true }), /Remote ID/);

// Sort cycling: a new column starts at its natural direction; the active
// column flips on a second click. Every BLE_COLUMNS header is sortable now
// (docs/ble.md API table), including Identity and Services.
let s = { sort: 'first_seen', order: 'asc' };
s = nextSort(s, 'rssi'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'rssi', order: 'desc' }));
s = nextSort(s, 'rssi'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'rssi', order: 'asc' }));
s = nextSort(s, 'company'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'company', order: 'asc' }));
s = nextSort(s, 'identity'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'identity', order: 'asc' }));
s = nextSort(s, 'services'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'services', order: 'desc' }));
s = nextSort(s, 'device'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'device', order: 'asc' }));
// An unrecognized key (e.g. a column that isn't in BLE_COLUMNS) must still
// leave the current sort alone.
const unchanged = nextSort(s, 'bogus');
assert.equal(JSON.stringify(unchanged), JSON.stringify(s), 'an unrecognized key must not change the current sort');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(SOURCE)],
                            capture_output=True, text=True, timeout=30, env=node_env())
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sensor_chip_and_reading_text():
    _run(r"""
const { sensorChipText, sensorReadingText } = vm.runInNewContext(raw + '\n({ sensorChipText, sensorReadingText })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});

assert.equal(sensorChipText(null), null);
assert.equal(sensorChipText({ protocol: 'bthome', encrypted: false }), 'BTHome');
assert.equal(sensorChipText({ protocol: 'bthome', encrypted: true }), 'BTHome (encrypted)');
assert.equal(sensorChipText({ protocol: 'ruuvi_rawv2', encrypted: false }), 'Ruuvi');
assert.equal(sensorChipText({ protocol: 'unknown_proto', encrypted: false }), 'unknown_proto');

assert.equal(sensorReadingText({ value: 25.0, unit: '°C' }), '25 °C');
assert.equal(sensorReadingText({ value: true, unit: null }), 'yes');
assert.equal(sensorReadingText({ value: false, unit: null }), 'no');
assert.equal(sensorReadingText({ value: null, unit: null }), '--');
assert.equal(sensorReadingText(undefined), '--');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_advertisers_table_renders_rows_from_stubbed_fetch():
    """_refreshRows() must populate _rows/_total from a stubbed
    api.listBleAdvertisers, the same way wifi-view.js's _refreshRows does
    from api.listWifiNetworks."""
    _run(r"""
const api = {
  listBleAdvertisers: async (params) => {
    assert.equal(params.sort, 'first_seen');
    assert.equal(params.order, 'asc');
    return {
      items: [
        { addr: 'aa:aa:aa:aa:aa:aa', identity: 'public', name: 'Thingy', company: 'Nordic', service_uuids: ['180d'], rssi: -58, reports: 12, last_seen: 1700000100 },
        { addr: 'bb:bb:bb:bb:bb:bb', identity: 'rpa', name: null, company: null, service_uuids: [], rssi: -71, reports: 3, last_seen: 1700000050 },
      ],
      total: 2, since: 1700000000,
    };
  },
  getBleAdvertiser: async (addr) => ({ addr }),
};
const BleView = vm.runInNewContext(raw + '\nBleView', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(BleView.prototype);
view._reqSeq = 0; view._detailSeq = 0;
view._q = ''; view._offset = 0;
view._rows = []; view._total = 0; view._selectedAddr = null; view._detail = null;
view._error = null; view._loading = true;
view._sort = { sort: 'first_seen', order: 'asc' };

await view._refreshRows();
assert.equal(view._rows.length, 2);
assert.equal(view._total, 2);
assert.equal(view._rows[0].addr, 'aa:aa:aa:aa:aa:aa');
assert.equal(view._rows[1].identity, 'rpa');
assert.equal(view._error, null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_device_column_precedence_and_identity_chip_text():
    """Device column: a user label beats the advertised name, which beats
    the company (+" device"), which beats "Unknown device"; Identity chip
    text is the compact form, not the long docs/ble.md explanation."""
    _run(r"""
const { deviceDisplay, identityChipText, identityLabel } = vm.runInNewContext(
  raw + '\n({ deviceDisplay, identityChipText, identityLabel })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);

// Compared via JSON.stringify, not assert.deepEqual: deviceDisplay()'s
// object literal is constructed inside the vm context, a different realm
// from this script's, so deepEqual's prototype check would spuriously fail.
const row = { name: 'Thingy', company: 'Nordic' };
assert.equal(JSON.stringify(deviceDisplay(row, { name: 'Kitchen Sensor', class: 'mine' })), JSON.stringify({ text: 'Kitchen Sensor', labeled: true }));
assert.equal(JSON.stringify(deviceDisplay(row, null)), JSON.stringify({ text: 'Thingy', labeled: false }));
assert.equal(JSON.stringify(deviceDisplay({ name: null, company: 'Nordic' }, null)), JSON.stringify({ text: 'Nordic device', labeled: false }));
assert.equal(JSON.stringify(deviceDisplay({ name: null, company: null }, null)), JSON.stringify({ text: 'Unknown device', labeled: false }));

assert.equal(identityChipText('public'), 'Public');
assert.equal(identityChipText('static'), 'Static');
assert.equal(identityChipText('rpa'), 'RPA');
assert.equal(identityChipText('nrpa'), 'NRPA');
// The chip stays short; the long explanation lives in identityLabel() (the title text).
assert.match(identityLabel('rpa'), /resolvable private/);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_device_column_names_apple_continuity_and_airpods():
    _run(r"""
const { deviceDisplay, appleBatteryText } = vm.runInNewContext(
  raw + '\n({ deviceDisplay, appleBatteryText })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);
const text = (row) => deviceDisplay(row, null).text;
const types = [{ type: '0x0c', name: 'Handoff' }, { type: '0x16', name: null }];
assert.equal(text({ company: 'Apple', apple: { types, audio: null } }), 'Apple device · Handoff, type 0x16');
const audio = { model: 'AirPods Pro (2nd generation)', model_code: '0x1420', battery_left: 40,
  battery_right: 90, battery_case: null, charging_left: false, charging_right: true, charging_case: false };
assert.equal(text({ company: 'Apple', apple: { types, audio } }), 'AirPods Pro (2nd generation)');
assert.equal(text({ company: 'Apple', apple: { types, audio: { ...audio, model: null } } }), 'Apple audio 0x1420');
// An advertised name still wins.
assert.equal(text({ name: 'Mine', company: 'Apple', apple: { types, audio } }), 'Mine');
assert.equal(appleBatteryText(audio), 'L 40% · R 90% ⚡ · case --');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sniffle_states_and_dynamic_coverage():
    """firmware_mismatch/wrong_baud (development notes Sniffle backend): plain blocker
    reasons, and the coverage sentence is per-backend/PHY/channel from the
    server's `status.coverage`, never the hardcoded hci-only fallback."""
    _run(r"""
const { bleStartBlocker, emptyStateFor, coverageText, coverageShortText, liveFrameText, bleChannelMHzText } =
  vm.runInNewContext(
    raw + '\n({ bleStartBlocker, emptyStateFor, coverageText, coverageShortText, liveFrameText, bleChannelMHzText })',
    { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
  );

assert.match(bleStartBlocker({ state: 'firmware_mismatch' }), /firmware/i);
assert.match(bleStartBlocker({ state: 'wrong_baud' }), /baud/i);

const fw = emptyStateFor({ state: 'firmware_mismatch' }, 0);
assert.match(fw.text, /flash the matching Sniffle build/);
assert.equal(fw.setup, true);
const wb = emptyStateFor({ state: 'wrong_baud' }, 0);
assert.match(wb.text, /baud rate/);
assert.match(emptyStateFor({ state: 'stopped' }, 0).text, /stopped/i);
assert.equal(emptyStateFor({ state: 'stopped' }, 4), null, 'stop keeps the advertisers heard so far on screen');

// Sniffle covers exactly what it is configured for, never "all".
const sniffleStatus = { backend: 'sniffle', phy: 'coded', channel: '37', coverage: 'Coded PHY on channel 37 only; legacy and extended advertising (aux-channel chaining followed); no scan responses, no connection following' };
assert.equal(coverageText(sniffleStatus), sniffleStatus.coverage);
assert.doesNotMatch(coverageText(sniffleStatus), /\ball\b/i);
assert.match(coverageShortText(sniffleStatus), /Coded PHY/);
assert.match(coverageShortText(sniffleStatus), /channel 37/);
assert.match(coverageShortText(sniffleStatus), /channel 37 \(2402 MHz\)/);
assert.match(coverageShortText({ backend: 'sniffle', phy: '1m', channel: '39' }), /2480 MHz/);
assert.equal(bleChannelMHzText('38'), '2426 MHz');
assert.equal(bleChannelMHzText('hop'), '2402 / 2426 / 2480 MHz');
assert.equal(bleChannelMHzText(undefined), '2402 MHz');
assert.match(coverageShortText({ backend: 'sniffle', phy: '1m', channel: 'hop' }), /hop/);
assert.match(coverageShortText({ backend: 'hci' }), /Legacy advertising only/);

// Live feed line carries PHY/channel/extended when the sniffle backend
// reports them, and stays unchanged (no stray separators) when it doesn't.
assert.equal(
  liveFrameText({ evt: 'AUX_ADV_IND', addr: 'aa:bb:cc:dd:ee:ff', length: 40, phy: 'coded', channel: 6, extended: true }),
  'AUX_ADV_IND · aa:bb:cc:dd:ee:ff · 40 B · CODED ch6 · ext'
);
assert.equal(
  liveFrameText({ evt: 'ADV_IND', addr: 'aa:bb:cc:dd:ee:ff', length: 10 }),
  'ADV_IND · aa:bb:cc:dd:ee:ff · 10 B'
);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_device_summary_parts():
    _run(r"""
const { deviceSummaryParts, summaryBlocks } = vm.runInNewContext(raw + '\n({ deviceSummaryParts, summaryBlocks })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console });
assert.equal(deviceSummaryParts({ total: 0, groups: [] }), null);
assert.equal(deviceSummaryParts(null), null);
const parts = deviceSummaryParts({ window_s: 60, total: 5, groups: [
  { label: 'Apple', count: 3, subgroups: [{ label: 'iPhone / iPad / Mac nearby', count: 2 }, { label: 'AirPods / Beats', count: 1 }] },
  { label: 'Unknown', count: 2, subgroups: [] },
] });
assert.equal(parts.lead, 'About 5 devices heard in the last minute');
assert.equal(parts.groups[0].detail, 'iPhone / iPad / Mac nearby 2, AirPods / Beats 1');
assert.equal(parts.groups[1].detail, '');
assert.deepEqual(parts.rows.map((r) => [r.label, r.count, r.level]),
  [['Apple', 3, 0], ['iPhone / iPad / Mac nearby', 2, 1], ['AirPods / Beats', 1, 1], ['Unknown', 2, 0]]);
assert.equal(parts.rows[0].share, 0.6);
const blocks = summaryBlocks(parts);
assert.equal(JSON.stringify(blocks.map((b) => [b.title, b.count, b.rows.length])), JSON.stringify([['Total', 5, 0], ['Apple', 3, 2], ['Unknown', 2, 0]]));
assert.equal(blocks[1].rows[0].label, 'iPhone / iPad / Mac nearby');
assert.equal(summaryBlocks(null).length, 0);
assert.equal(deviceSummaryParts({ window_s: 300, total: 1, groups: [] }).lead, 'About 1 device heard in the last 5 minutes');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_radio_and_start_send_partial_settings_patches():
    """A stale `settings` copy (phy still "1m" after the user picked Coded)
    must not ride along with the next change: _setRadio and _start send only
    the key they change, and _setRadio restarts a running capture."""
    _run(r"""
const calls = [];
const api = {
  patchSettings: async (body) => { calls.push(['patch', body]); return body; },
  stopBle: async () => calls.push(['stop']),
  startBle: async () => calls.push(['start']),
  getBleStatus: async () => ({ state: 'receiving' }),
};
const BleView = vm.runInNewContext(raw + '\nBleView', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});
const view = Object.create(BleView.prototype);
view.settings = { enabled: false, phy: '1m', channel: '37' };  // stale: server already has phy=coded
view._status = { state: 'receiving' };
view.dispatchEvent = () => true;
view._refreshStatus = async () => {};

// JSON compare: objects from inside the vm realm are never reference-equal
const seen = () => JSON.stringify(calls);
await view._setRadio('channel', 'hop');
assert.equal(seen(), JSON.stringify([['patch', { ble: { channel: 'hop' } }], ['stop'], ['start']]));

calls.length = 0;
view._status = { state: 'stopped' };
await view._setRadio('phy', 'coded');
assert.equal(seen(), JSON.stringify([['patch', { ble: { phy: 'coded' } }]]), 'no restart when not running');

calls.length = 0;
await view._start();
assert.equal(seen(), JSON.stringify([['patch', { ble: { enabled: true } }], ['start']]));
""")
