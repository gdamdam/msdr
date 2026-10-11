# ruff: noqa: E501 -- embedded JavaScript source lines
"""wifi-view.js (Discover's Wi-Fi tab, docs/wifi.md "UI" contract): row
rendering helpers, stable BSSID-keyed selection across a refresh, the
sequence-token guard against an out-of-order network-list response, sparkline
gap breaks, and the per-receiver-state empty/error messages. Same
vm.runInNewContext harness as test_detect_readout_allocation.py: imports are
stripped and `export` is dropped so the source can run as a plain script with
stubbed LitElement/html/customElements/api globals."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/wifi-view.js"

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
def test_ssid_channel_and_signal_cell_helpers():
    _run(r"""
const mod = vm.runInNewContext(raw + '\n({ ssidLabel, channelMarkers, signalCellText })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

// Hidden SSID: either the explicit flag or an empty string. (Compared
// field-by-field, not via deepEqual on the whole object -- these plain
// objects are constructed inside the vm sandbox's realm, so a strict
// deepEqual against an outer-realm object literal would fail on prototype
// identity alone even when every field matches.)
let r = mod.ssidLabel({ ssid_hidden: true, ssid: '' });
assert.equal(r.text, 'hidden network'); assert.equal(r.hidden, true);
r = mod.ssidLabel({ ssid_hidden: false, ssid: '' });
assert.equal(r.text, 'hidden network'); assert.equal(r.hidden, true);
r = mod.ssidLabel({ ssid_hidden: false, ssid: 'CafeWifi' });
assert.equal(r.text, 'CafeWifi'); assert.equal(r.hidden, false);

// configured-channel marker and mismatch marker, independently togglable.
assert.equal(mod.channelMarkers({ channel_source: 'ds_ie', channel_mismatch: false }).length, 0);
let marks = mod.channelMarkers({ channel_source: 'configured', channel_mismatch: false });
assert.equal(marks.length, 1);
assert.equal(marks[0].text, 'configured');
marks = mod.channelMarkers({ channel_source: 'ds_ie', channel_mismatch: true });
assert.equal(marks.length, 1);
assert.equal(marks[0].text, 'mismatch');
marks = mod.channelMarkers({ channel_source: 'configured', channel_mismatch: true });
assert.equal(marks.length, 2);

// Signal: raw PHY code vs dBm vs not reported at all.
assert.equal(mod.signalCellText(null), null);
assert.equal(mod.signalCellText({ kind: 'raw', latest: 47 }).text, 'raw 47');
assert.match(mod.signalCellText({ kind: 'raw', latest: 47 }).title, /[Uu]ncalibrated/);
assert.equal(mod.signalCellText({ kind: 'dbm', latest: -62 }).text, '-62 dBm');
assert.equal(mod.signalCellText({ kind: 'dbm', latest: -62 }).title, null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sparkline_breaks_on_a_gap_over_ten_seconds():
    _run(r"""
const { sparklineSegments } = vm.runInNewContext(raw + '\n({ sparklineSegments })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

// Continuous samples (<=10s apart): one segment covering every point.
let segs = sparklineSegments([[0, 10], [5, 12], [10, 9]], 100, 20, 10);
assert.equal(segs.length, 1);
assert.equal(segs[0].split(' ').length, 3);

// A >10s gap between two samples must break the line into two segments.
segs = sparklineSegments([[0, 10], [5, 12], [30, 9], [35, 11]], 100, 20, 10);
assert.equal(segs.length, 2, 'a 25s gap must start a new segment');
assert.equal(segs[0].split(' ').length, 2);
assert.equal(segs[1].split(' ').length, 2);

// Fewer than 2 usable points: nothing to draw.
assert.equal(sparklineSegments([], 100, 20, 10).length, 0);
assert.equal(sparklineSegments([[0, 10]], 100, 20, 10).length, 0);
assert.equal(sparklineSegments([[0, null], [5, 12]], 100, 20, 10).length, 0);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_empty_state_messages_per_receiver_state():
    _run(r"""
const { emptyStateFor } = vm.runInNewContext(raw + '\n({ emptyStateFor })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.match(emptyStateFor({ state: 'unavailable' }, 0, false).text, /not installed|unavailable/i);
assert.equal(emptyStateFor({ state: 'unavailable' }, 0, false).setup, true);
assert.match(emptyStateFor({ state: 'dependency_missing' }, 0, false).text, /dependency/i);
assert.match(emptyStateFor({ state: 'no_device' }, 0, false).text, /adapter/i);
assert.match(emptyStateFor({ state: 'stopped' }, 0, false).text, /stopped/i);
assert.equal(emptyStateFor({ state: 'stopped' }, 0, false).setup, false);  // Start is in the view now
assert.equal(emptyStateFor({ state: 'stopped' }, 4, false), null, 'stop keeps the networks heard so far on screen');
assert.match(emptyStateFor({ state: 'receiving' }, 0, false).text, /no networks observed/i);
assert.equal(emptyStateFor({ state: 'receiving' }, 0, false).setup, false);
assert.equal(emptyStateFor({ state: 'receiving' }, 3, false), null, 'rows present -> render the table, not an empty state');
const failed = emptyStateFor({ state: 'failed', last_error: 'usb_error: timeout' }, 0, false);
assert.match(failed.text, /usb_error: timeout/);

// Viewing a saved session never talks about the live receiver's state.
assert.equal(emptyStateFor({ state: 'stopped' }, 2, true), null);
assert.doesNotMatch(emptyStateFor({ state: 'stopped' }, 0, true).text, /stopped/i);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_selection_is_keyed_by_bssid_and_survives_a_count_only_refresh():
    """Selecting a row and then receiving a refreshed row set (same BSSIDs,
    same order, only beacon/probe counts changed) must leave the selection
    in place -- the operator didn't navigate away, only the numbers ticked."""
    _run(r"""
const api = {
  listWifiNetworks: async () => ({
    items: [
      { bssid: 'aa:aa:aa:aa:aa:aa', ssid: 'One', beacons: 100 },
      { bssid: 'bb:bb:bb:bb:bb:bb', ssid: 'Two', beacons: 50 },
    ],
    total: 2, configured_channel: 6, since: 1700000000,
  }),
  getWifiNetwork: async (bssid) => ({ bssid }),
};
const WifiView = vm.runInNewContext(raw + '\nWifiView', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(WifiView.prototype);
view.source = null; view._reqSeq = 0; view._detailSeq = 0;
view._q = ''; view._channelFilter = ''; view._offset = 0;
view._rows = []; view._total = 0; view._selectedBssid = null; view._detail = null;
view._frameHex = {}; view._error = null; view._loading = true;
view.dispatchEvent = () => true;

await view._refreshRows();
assert.equal(view._rows.length, 2);
view._select('bb:bb:bb:bb:bb:bb');
assert.equal(view._selectedBssid, 'bb:bb:bb:bb:bb:bb');

// A second refresh with updated counts but the same rows/order.
api.listWifiNetworks = async () => ({
  items: [
    { bssid: 'aa:aa:aa:aa:aa:aa', ssid: 'One', beacons: 101 },
    { bssid: 'bb:bb:bb:bb:bb:bb', ssid: 'Two', beacons: 52 },
  ],
  total: 2, configured_channel: 6, since: 1700000000,
});
await view._refreshRows();
assert.equal(view._selectedBssid, 'bb:bb:bb:bb:bb:bb', 'selection must survive a count-only refresh');
assert.equal(view._rows[1].beacons, 52, 'the refreshed counts must still land');
assert.equal(view._rows[0].bssid, 'aa:aa:aa:aa:aa:aa', 'server order is kept, not re-sorted');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_network_list_response_is_dropped():
    """A slow response for a filter/page the operator already changed away
    from must never overwrite what a newer request already fetched."""
    _run(r"""
const pending = [];
const api = {
  listWifiNetworks: (params) => new Promise(resolve => pending.push({ params, resolve })),
  getWifiNetwork: async (bssid) => ({ bssid }),
};
const WifiView = vm.runInNewContext(raw + '\nWifiView', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(WifiView.prototype);
view.source = null; view._reqSeq = 0; view._detailSeq = 0;
view._q = ''; view._channelFilter = ''; view._offset = 0;
view._rows = []; view._total = 0; view._selectedBssid = null; view._detail = null;
view._frameHex = {}; view._error = null; view._loading = true;

const first = view._refreshRows();   // sequence token 1
view._channelFilter = '6';
const second = view._refreshRows();  // sequence token 2 -- newer
assert.equal(pending.length, 2);

// Out-of-order resolution: the newer request settles first...
pending[1].resolve({ items: [{ bssid: 'new', ssid: 'Newer' }], total: 1, configured_channel: 6, since: 1 });
await second;
assert.equal(view._rows[0].bssid, 'new');

// ...then the stale first request finally settles. It must be dropped.
pending[0].resolve({ items: [{ bssid: 'old', ssid: 'Older' }], total: 1, configured_channel: 1, since: 1 });
await first;
assert.equal(view._rows[0].bssid, 'new', 'a late reply for an old filter must never overwrite the newer result');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_merge_and_sort_header_cycle():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const ctx = { LitElement: class {}, html: () => '', customElements: { define() {} }, console, api: {} };
vm.runInNewContext(src + '\nglobalThis.out = { mergeLiveFrames, liveFrameText, nextSort };', ctx);
const { mergeLiveFrames, liveFrameText, nextSort } = ctx.out;
let st = mergeLiveFrames([], [{ seq: 1, name: 'beacon' }, { seq: 2, name: 'ACK' }], 0, 3);
assert.equal(JSON.stringify(st.frames.map(f => f.seq)), JSON.stringify([2, 1]));            // newest first
st = mergeLiveFrames(st.frames, [{ seq: 2 }, { seq: 3 }, { seq: 4 }], st.lastSeq, 3);
assert.equal(JSON.stringify(st.frames.map(f => f.seq)), JSON.stringify([4, 3, 2]));         // dedup + bounded
assert.equal(st.lastSeq, 4);
assert.equal(liveFrameText({ name: 'beacon', ssid: 'Cafe', channel: 6, signal: { kind: 'raw', value: 46 }, length: 230 }),
  'beacon · “Cafe” · ch 6 · raw 46 · 230 B');
assert.match(liveFrameText({ name: 'ACK', source: null, length: 10, bad_fcs: true }), /bad checksum/);
let s = { sort: 'first_seen', order: 'asc' };
s = nextSort(s, 'packets'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'packets', order: 'desc' }));
s = nextSort(s, 'packets'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'packets', order: 'asc' }));
s = nextSort(s, 'ssid'); assert.equal(JSON.stringify({ sort: s.sort, order: s.order }), JSON.stringify({ sort: 'ssid', order: 'asc' }));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(SOURCE)],
                            capture_output=True, text=True, timeout=30, env=node_env())
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_survey_preset_expansion_and_channel_parsing():
    _run(r"""
const mod = vm.runInNewContext(raw + '\n({ SURVEY_PRESETS, presetChannels, parseSurveyChannels })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

// 2.4 GHz 1-13.
assert.equal(JSON.stringify(mod.presetChannels('24_all')), JSON.stringify(Array.from({ length: 13 }, (_, i) => i + 1)));
// 2.4 GHz 1/6/11.
assert.equal(JSON.stringify(mod.presetChannels('24_1_6_11')), JSON.stringify([1, 6, 11]));
// 5 GHz is non-empty and entirely above 14.
assert.ok(mod.presetChannels('5ghz').length > 0);
assert.ok(mod.presetChannels('5ghz').every((c) => c > 14));
// DFS (52-144) is out of the default 5 GHz and All presets; the "incl. DFS" ones have it.
const dfs = (c) => c >= 52 && c <= 144;
assert.ok(!mod.presetChannels('5ghz').some(dfs) && !mod.presetChannels('all').some(dfs));
assert.ok(mod.presetChannels('5ghz_dfs').some(dfs) && mod.presetChannels('all_dfs').some(dfs));
assert.equal(mod.presetChannels('all').length, 13 + 9);
assert.equal(mod.presetChannels('all_dfs').length, 13 + 25);
// "all" is the union of 2.4 and 5 GHz.
assert.equal(mod.presetChannels('all').length, mod.presetChannels('24_all').length + mod.presetChannels('5ghz').length);
// Unknown/empty preset id -> no channels (custom entry).
assert.equal(mod.presetChannels('').length, 0);
assert.equal(mod.presetChannels('nope').length, 0);

// Free-text channel parsing: whitespace, stray commas and non-numeric junk
// are dropped; duplicates are collapsed.
assert.equal(JSON.stringify(mod.parseSurveyChannels('1, 6,11')), JSON.stringify([1, 6, 11]));
assert.equal(JSON.stringify(mod.parseSurveyChannels('1,,6,')), JSON.stringify([1, 6]));
assert.equal(JSON.stringify(mod.parseSurveyChannels('6,6,6')), JSON.stringify([6]));
assert.equal(JSON.stringify(mod.parseSurveyChannels('')), JSON.stringify([]));
assert.equal(JSON.stringify(mod.parseSurveyChannels('a,b')), JSON.stringify([]));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_survey_results_sort_and_next_sort():
    _run(r"""
const mod = vm.runInNewContext(raw + '\n({ sortSurveyResults, nextSurveySort, surveyFrameRate, surveyStrongest, surveyBand })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.equal(mod.surveyBand(6), '2.4 GHz');
assert.equal(mod.surveyBand(149), '5 GHz');
assert.equal(mod.surveyFrameRate({ frames: 20, listened_s: 10 }), 2);
assert.equal(mod.surveyFrameRate({ frames: 20, listened_s: 0 }), 0, 'never NaN/Infinity for an unvisited channel');
assert.equal(mod.surveyStrongest({ top: [{ bssid: 'a', signal: -40 }, { bssid: 'b', signal: -70 }] }).bssid, 'a');
assert.equal(mod.surveyStrongest({ top: [] }), null);

const results = [
  { channel: 11, frames: 100, listened_s: 10, networks: 2, top: [{ signal: -50 }] },
  { channel: 1, frames: 50, listened_s: 10, networks: 5, top: [{ signal: -30 }] },
  { channel: 6, frames: 10, listened_s: 10, networks: 0, top: [] },
];

// Default/explicit channel sort, ascending.
let sorted = mod.sortSurveyResults(results, { sort: 'channel', order: 'asc' });
assert.equal(JSON.stringify(sorted.map((r) => r.channel)), JSON.stringify([1, 6, 11]));

// Networks, descending -- the channel with no networks (null-ish "strongest"
// doesn't apply here, networks is always a number) still sorts normally.
sorted = mod.sortSurveyResults(results, { sort: 'networks', order: 'desc' });
assert.equal(JSON.stringify(sorted.map((r) => r.channel)), JSON.stringify([1, 11, 6]));

// "strongest" sorts by top[0].signal; a channel with no networks yet (null)
// sorts last in both directions.
sorted = mod.sortSurveyResults(results, { sort: 'strongest', order: 'desc' });
assert.equal(sorted[sorted.length - 1].channel, 6, 'no networks yet sorts last');

// nextSort: a new column starts at its natural direction; the same column flips.
let s = { sort: 'channel', order: 'asc' };
s = mod.nextSurveySort(s, 'frames');
assert.equal(JSON.stringify(s), JSON.stringify({ sort: 'frames', order: 'desc' }));
s = mod.nextSurveySort(s, 'frames');
assert.equal(JSON.stringify(s), JSON.stringify({ sort: 'frames', order: 'asc' }));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_visible_rows_hides_ignored_labels_by_default():
    """visibleRows() (docs/history.md: tables show the label and a 'hide
    ignored' filter) drops rows whose label class is `ignore` when
    hideIgnored is on, keyed by bssid -- same contract as ble-view.js's
    visibleRows (keyed by addr), client-side only so paging/`total` are
    untouched."""
    _run(r"""
const { visibleRows } = vm.runInNewContext(raw + '\n({ visibleRows })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

const rows = [
  { bssid: 'aa:aa:aa:aa:aa:aa', ssid: 'Mine' },
  { bssid: 'bb:bb:bb:bb:bb:bb', ssid: 'Noisy' },
  { bssid: 'cc:cc:cc:cc:cc:cc', ssid: 'Unlabelled' },
];
const labels = new Map([
  ['aa:aa:aa:aa:aa:aa', { name: 'Home AP', class: 'mine' }],
  ['bb:bb:bb:bb:bb:bb', { name: 'Neighbor', class: 'ignore' }],
]);

assert.equal(visibleRows(rows, labels, false).length, 3, 'hideIgnored off -> nothing filtered');
const hidden = visibleRows(rows, labels, true);
assert.equal(hidden.length, 2, 'the ignore-class row must be dropped');
assert.ok(!hidden.some((r) => r.bssid === 'bb:bb:bb:bb:bb:bb'));
assert.ok(hidden.some((r) => r.bssid === 'cc:cc:cc:cc:cc:cc'), 'an unlabelled row is never hidden');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_refresh_labels_populates_map_and_label_changed_refetches():
    """_refreshLabels() must populate _labels from a stubbed
    api.listLabels('wifi'), keyed by ident (bssid); _onLabelChanged() (the
    device-label-editor's event) must re-run it -- same pattern as
    ble-view.js's _refreshLabels/_onLabelChanged."""
    _run(r"""
let calls = 0;
const api = {
  listLabels: async (receiver) => {
    assert.equal(receiver, 'wifi');
    calls += 1;
    return { items: calls === 1
      ? [{ ident: 'aa:aa:aa:aa:aa:aa', name: 'Home AP', class: 'mine' }]
      : [{ ident: 'aa:aa:aa:aa:aa:aa', name: 'Home AP', class: 'known' }] };
  },
};
const WifiView = vm.runInNewContext(raw + '\nWifiView', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(WifiView.prototype);
view._labels = new Map(); view._error = null;

await view._refreshLabels();
assert.equal(view._labels.size, 1);
assert.equal(view._labels.get('aa:aa:aa:aa:aa:aa').class, 'mine');

// _onLabelChanged (the device-label-editor's event handler) just re-runs
// _refreshLabels, fire-and-forget -- same as ble-view.js's _onLabelChanged.
let refreshed = 0;
view._refreshLabels = () => { refreshed += 1; };
view._onLabelChanged();
assert.equal(refreshed, 1, 'label-changed must trigger a refresh');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_select_skips_history_fetch_when_viewing_a_saved_session():
    """docs/history.md: history is live-only. Selecting a row while
    this.source is a recorded session ("session:...") must still fetch the
    label-bearing detail, but must not call getDeviceHistory -- the History
    section is hidden for a session, the Label section stays."""
    _run(r"""
let historyCalls = 0;
const api = {
  getWifiNetwork: async (bssid) => ({ bssid }),
  getDeviceHistory: async () => { historyCalls += 1; return { cells: [] }; },
};
const WifiView = vm.runInNewContext(raw + '\nWifiView', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
  autoBucketS: () => 60, // the real import is stripped by PRELUDE; stub it
  wifiChannelMHz: () => null,
});

const view = Object.create(WifiView.prototype);
view.source = 'session:abc123';
view._detailSeq = 0; view._selectedBssid = null; view._detail = null;
view._frameHex = {}; view._detailHistory = null;

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

view._select('aa:aa:aa:aa:aa:aa');
await flush();
assert.equal(view._selectedBssid, 'aa:aa:aa:aa:aa:aa');
assert.equal(historyCalls, 0, 'history must not be fetched while viewing a saved session');
assert.equal(view._detail.bssid, 'aa:aa:aa:aa:aa:aa', 'the label-bearing detail is still fetched');

// Live (no source): the same selection must fetch history too.
view.source = null;
view._selectedBssid = null; view._detail = null;
view._select('aa:aa:aa:aa:aa:aa');
await flush();
assert.equal(historyCalls, 1, 'history must be fetched when not viewing a saved session');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_survey_progress_text():
    _run(r"""
const mod = vm.runInNewContext(raw + '\n({ surveyProgressText, surveyEstimateText })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.equal(mod.surveyProgressText(null), '');
assert.equal(mod.surveyProgressText({ state: 'idle' }), '');

// Listening: channel, countdown, then elapsed of the run's estimate.
const now = 1_791_000_004;
const running = { state: 'running', phase: 'listening', round: 1, rounds: 1, current_channel: 44,
  channel_ends_at: now + 6, started_at: now - 35, estimated_total_s: 60 };
assert.equal(mod.surveyProgressText(running, now), 'round 1 of 1 · listening on channel 44 · 6 s left · 0:35 of ~1:00');

// Switching: the helper restarts on the next channel -- no stale "0 s left".
const switching = { ...running, phase: 'switching', current_channel: 11, channel_ends_at: null };
assert.equal(mod.surveyProgressText(switching, now), 'round 1 of 1 · switching to channel 11… · 0:35 of ~1:00');

// rounds: 0 means "until stopped" -- no "of N", no estimate.
const untilStopped = { state: 'running', phase: 'listening', round: 3, rounds: 0, current_channel: 11,
  channel_ends_at: now + 2, started_at: now - 125, estimated_total_s: null };
assert.equal(mod.surveyProgressText(untilStopped, now), 'round 3 · listening on channel 11 · 2 s left · 2:05 elapsed');

// Never a negative countdown once the deadline has just passed.
const atDeadline = { state: 'running', phase: 'listening', round: 1, rounds: 1, current_channel: 6, channel_ends_at: now - 1 };
assert.equal(mod.surveyProgressText(atDeadline, now), 'round 1 of 1 · listening on channel 6 · 0 s left');

assert.match(mod.surveyProgressText({ state: 'stopping' }), /^Stopping/);
assert.equal(mod.surveyProgressText({ state: 'done' }), 'Survey complete.');
assert.equal(mod.surveyProgressText({ state: 'stopped' }), 'Survey stopped.');
assert.match(mod.surveyProgressText({ state: 'failed', error: 'channel 6: helper is no_device' }), /no_device/);
assert.equal(mod.surveyProgressText({ state: 'failed', error: null }), 'Survey failed.');

// Before Start: (dwell + ~5 s restart) per channel per round.
assert.equal(mod.surveyEstimateText(3, 10, 1), 'about 0:45 for 3 channels');
assert.equal(mod.surveyEstimateText(13, 10, 2), 'about 6:30 for 13 channels × 2 rounds');
assert.equal(mod.surveyEstimateText(3, 10, 0), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_vendor_text():
    _run(r"""
const { vendorText } = vm.runInNewContext(raw + '\n({ vendorText })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});
assert.equal(vendorText({ vendor: 'Wyze Labs Inc', local_admin: false }), 'Wyze Labs Inc');
assert.equal(vendorText({ vendor: null, local_admin: true }), 'locally administered');
assert.equal(vendorText({ vendor: null, local_admin: false }), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hidden_name_label_and_grouping():
    _run(r"""
const { ssidLabel, apGroupKey, groupNetworks } = vm.runInNewContext(
  raw + '\n({ ssidLabel, apGroupKey, groupNetworks })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);
assert.equal(ssidLabel({ ssid_hidden: true, ssid: null, revealed_ssids: [] }).text, 'hidden network');
assert.equal(ssidLabel({ ssid_hidden: true, ssid: null, revealed_ssids: ['deOuro_mesh'] }).text,
  'hidden (seen as deOuro_mesh)');

// Locally-administered bit and last digit ignored; other octets must match.
assert.equal(apGroupKey('a8:42:a1:10:20:31'), apGroupKey('aa:42:a1:10:20:35'));
assert.notEqual(apGroupKey('a8:42:a1:10:20:31'), apGroupKey('a8:42:a1:10:21:31'));

const rows = [
  { bssid: 'a8:42:a1:10:20:31', ssid: 'deOuro_1', ssid_hidden: false, channel: 1, vendor: 'TP-Link' },
  { bssid: 'aa:42:a1:10:20:35', ssid: null, ssid_hidden: true, revealed_ssids: [], channel: 1, vendor: null },
  { bssid: '10:00:00:00:00:01', ssid: 'deOuro_1', ssid_hidden: false, channel: 11, vendor: 'Other' },
];
const byAp = groupNetworks(rows, 'ap');
assert.equal(byAp.length, 2);
assert.equal(byAp[0].title, 'TP-Link · deOuro_1');
assert.equal(byAp[0].detail, '2 BSSIDs · channel 1 · probably one access point');
const byName = groupNetworks(rows, 'name');
assert.equal(byName.map((g) => g.title).join('|'), 'deOuro_1|Hidden, name not seen');
assert.equal(byName[0].detail, '2 BSSIDs · channels 1, 11');
assert.equal(groupNetworks(rows, ''), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_signal_bar():
    _run(r"""
const { signalBarPct, rawSignalRange, signalCellText, isRare } = vm.runInNewContext(
  raw + '\n({ signalBarPct, rawSignalRange, signalCellText, isRare })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);
// dBm: the BLE scale, -100..-30, clamped; the median wins over the latest.
assert.equal(signalBarPct({ kind: 'dbm', latest: -90, median: -65 }), 50);
assert.equal(signalBarPct({ kind: 'dbm', latest: -20 }), 100);
assert.equal(signalCellText({ kind: 'raw', latest: 52, median: 30, samples: 40 }).text, 'raw 30');
// Raw: relative to rows heard often enough; a rare row doesn't set the scale.
const rows = [
  { beacons: 500, signal: { kind: 'raw', latest: 18, median: 18 } },
  { beacons: 500, signal: { kind: 'raw', latest: 40, median: 52 } },
  { beacons: 3, signal: { kind: 'raw', latest: 90, median: 90 } },
  { beacons: 500, signal: null },
];
assert.ok(isRare(rows[2]) && !isRare(rows[0]));
const range = rawSignalRange(rows);
assert.equal(JSON.stringify(range), '[18,52]');
assert.equal(signalBarPct({ kind: 'raw', median: 35 }, range), 50);
assert.equal(signalBarPct({ kind: 'raw', latest: 35 }, null), null);
assert.equal(signalBarPct({ kind: 'raw', latest: 30 }, [30, 30]), 100);
assert.equal(signalBarPct(null), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_network_summary_parts_and_survey_verdict():
    _run(r"""
const { networkSummaryParts, surveyVerdict, summaryBlocks } = vm.runInNewContext(raw + '\n({ networkSummaryParts, surveyVerdict, summaryBlocks })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});
assert.equal(networkSummaryParts({ total: 0 }), null);
const parts = networkSummaryParts({ window_s: 60, total: 5, access_points: 3, hidden: 1, local_admin: 1,
  bands: [{ label: '2.4 GHz', count: 4 }], security: [{ label: 'Open', count: 1 }], makers: [] });
assert.equal(parts.lead, '5 networks heard in the last minute');
assert.equal(parts.groups.map((g) => g.label).join('|'), 'probable access points|by band|by security|hidden');
assert.equal(parts.groups[0].count, 3);
assert.equal(parts.groups[1].subgroups[0].label, '2.4 GHz');
assert.equal(parts.rows.map((r) => `${r.kind}:${r.label}`).join('|'), 'total:Networks heard (last minute)|'
  + 'total:Probable access points|total:Hidden|head:Band|item:2.4 GHz|head:Security|item:Open');
assert.equal(parts.rows[6].warn, true);
assert.equal(parts.rows[4].warn, false);
assert.equal(parts.rows[4].share, 0.8);
const blocks = summaryBlocks(parts);
assert.equal(JSON.stringify(blocks.map((b) => [b.title, b.rows.length])), JSON.stringify([['Totals', 3], ['Band', 1], ['Security', 1]]));
assert.equal(summaryBlocks(null).length, 0);

assert.equal(surveyVerdict([{ channel: 1, listened_s: 0, frames: 0, networks: 0 }]), null);
const results = [
  { channel: 1, listened_s: 10, frames: 4000, networks: 8, top: [{ ds_channel: 1 }, { ds_channel: 3 }] },
  { channel: 6, listened_s: 10, frames: 1000, networks: 12, top: [] },
  { channel: 11, listened_s: 10, frames: 1000, networks: 2, top: [] },
  { channel: 36, listened_s: 10, frames: 50, networks: 1, top: [] },
];
const tiles = Object.fromEntries(surveyVerdict(results).map((t) => [t.label, t.value]));
assert.equal(tiles['busiest channel'], 1);
assert.equal(tiles['most networks'], 6);
assert.equal(tiles['least congested 2.4 GHz'], 11); // ties on rate broken by fewer networks
assert.equal(tiles['least congested 5 GHz'], undefined); // one channel can't be "least"
assert.equal(tiles['overlap'], 1);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_survey_done_with_failed_channels_and_status_sort():
    _run(r"""
const { surveyProgressText, sortSurveyResults } = vm.runInNewContext(raw + '\n({ surveyProgressText, sortSurveyResults })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});
assert.equal(surveyProgressText({ state: 'done', failed_channels: 0 }), 'Survey complete.');
assert.match(surveyProgressText({ state: 'done', failed_channels: 2 }), /^Survey complete; 2 channels could not be started/);
const rows = [
  { channel: 1, status: 'done' }, { channel: 6, status: 'failed' }, { channel: 11, status: 'pending' }, { channel: 36, status: 'listening' },
];
assert.equal(sortSurveyResults(rows, { sort: 'status', order: 'asc' }).map((r) => r.channel).join(','), '36,6,1,11');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_overlap_load_verdict_and_findings_from_a_real_survey():
    _run(r"""
const { overlapLoad, surveyVerdict, surveyFindings, findingsText } = vm.runInNewContext(
  raw + '\n({ overlapLoad, surveyVerdict, surveyFindings, findingsText })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console, Date },
);
// Frames/s from an all-channels (no DFS) survey; channel 4 decodes almost
// nothing but sits between busy 1, 6 and 7.
const rates = { 1: 32.4, 2: 11.8, 3: 14.1, 4: 2.3, 5: 30.7, 6: 92.2, 7: 135.9, 8: 53.8, 9: 62.2, 10: 52.0, 11: 130.9, 12: 33.1, 13: 54.0,
  36: 3146.8, 40: 2429.6, 44: 2410.2, 48: 2468.5, 149: 92.4, 153: 127.1, 157: 92.7, 161: 62.3, 165: 14.4 };
const nets = { 1: 7, 4: 1, 7: 7, 36: 3, 40: 0, 44: 2, 48: 1, 149: 0, 153: 3, 165: 3 };
const top = { 36: [{ ssid: 'zzzz', signal: 88, ds_channel: 36 }], 5: [{ ssid: 'zzzz', signal: 40, ds_channel: 7 }], 7: [{ ssid: 'zzzz', signal: 56, ds_channel: 7 }],
  8: [{ ssid: 'zzzz', signal: 18, ds_channel: 7 }], 10: [{ ssid: 'zzzz', signal: 56, ds_channel: 7 }], 12: [{ ssid: null, signal: 42, ds_channel: 12 }] };
const results = Object.keys(rates).map((c) => ({ channel: +c, listened_s: 10, frames: rates[c] * 10, networks: nets[c] ?? 2, top: top[c] ?? [] }));

const loads = overlapLoad(results);
assert.equal(Math.round(loads.get(1)), 91);   // 1-5
assert.equal(Math.round(loads.get(4)), 373);  // 1-8
assert.equal(Math.round(loads.get(36)), 3147); // 5 GHz: own rate

const tiles = Object.fromEntries(surveyVerdict(results).map((t) => [t.label, t]));
assert.equal(tiles['least congested 2.4 GHz'].value, 1, 'picked among 1/6/11 by overlap load, not channel 4');
assert.match(tiles['least congested 2.4 GHz'].detail, /91 frames\/s incl\. overlap.*among 1\/6\/11/);
assert.equal(tiles['least congested 5 GHz'].value, 165);
assert.equal(tiles['busiest channel'].value, 36);

const findings = surveyFindings(results);
const text = findingsText(findings, { started_at: 1791600000 });
assert.match(text, /Pick: channel 1, the lowest overlap load/);
assert.match(text, /Channel 4 looks quiet \(2\.3 frames\/s\) but 373 frames\/s from channels 1, 5, 6, 7, 8 share its frequencies/);
assert.match(text, /Channels 36-48 all carry 2410-3147 frames\/s with only 3 networks: that is data traffic on one 80 MHz-wide channel, not beacons, with its primary on 36 \(zzzz\)/);
assert.match(text, /zzzz is the strongest network on 5 channels/);
assert.match(text, /hidden network .* strongest on channel 12/);
assert.match(text, /Pick: channel 165/);
assert.ok(text.startsWith('msdr Wi-Fi channel survey ·'));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_survey_column_has_help_text():
    _run(r"""
const { SURVEY_COLUMNS } = vm.runInNewContext(raw + '\n({ SURVEY_COLUMNS })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});
assert.ok(SURVEY_COLUMNS.length >= 9);
assert.ok(SURVEY_COLUMNS.every((c) => typeof c.help === 'string' && c.help.length > 20));
assert.match(SURVEY_COLUMNS.find((c) => c.key === 'load').help, /Use this, not Frames\/s/);
""")
