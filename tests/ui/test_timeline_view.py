# ruff: noqa: E501 -- embedded JavaScript source lines
"""timeline-view.js (Discover's Timeline tab, docs/history.md "Shared
timeline (UI)"): filter-state helpers (range -> hours, receivers/hide-class
toggling, GET /api/history/timeline param building, the client-side
hide/search re-filter), the empty-state message, and that the grid populates
_devices from a stubbed api.getTimeline -- same vm.runInNewContext harness as
test_ble_view.py. Also covers device-label-editor.js's rpa/nrpa refusal,
since timeline-view's row detail embeds that editor."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "msdr/ui/components/timeline-view.js"
CHART_SOURCE = ROOT / "msdr/ui/components/device-history-chart.js"
LABEL_SOURCE = ROOT / "msdr/ui/components/device-label-editor.js"

# timeline-view.js imports device-history-chart.js for real (bucketCount,
# missingCoverage, xForIndex, autoBucketS) -- concatenate both stripped
# sources so those names are actually defined, instead of stubbing them.
PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
function strip(path) {
  return fs.readFileSync(path, 'utf8')
    .replace(/^import .*;\n/gm, '')
    .replace(/^export (function|class|const) /gm, '$1 ');
}
const raw = strip(process.argv[1]) + '\n' + strip(process.argv[2]);
"""


def _run(body):
    script = PRELUDE + body
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CHART_SOURCE), str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hours_for_range_and_toggle_in_array():
    _run(r"""
const { hoursForRange, toggleInArray, RANGE_OPTIONS } = vm.runInNewContext(
  raw + '\n({ hoursForRange, toggleInArray, RANGE_OPTIONS })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);

assert.equal(hoursForRange('1h'), 1);
assert.equal(hoursForRange('6h'), 6);
assert.equal(hoursForRange('24h'), 24);
assert.equal(hoursForRange('7d'), 168);
assert.equal(hoursForRange('bogus'), 6, 'an unrecognized range falls back to the API default');
assert.equal(RANGE_OPTIONS.length, 4);

assert.equal(JSON.stringify(toggleInArray(['wifi', 'ble'], 'wifi')), JSON.stringify(['ble']));
assert.equal(JSON.stringify(toggleInArray(['ble'], 'wifi')), JSON.stringify(['ble', 'wifi']));
assert.equal(JSON.stringify(toggleInArray([], 'wifi')), JSON.stringify(['wifi']));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_build_timeline_params():
    _run(r"""
const { buildTimelineParams } = vm.runInNewContext(raw + '\n({ buildTimelineParams })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});

// Both receivers checked (the default) -> omitted, so the server's own
// "default both" applies; `hide` carries the default ["ignore"].
let p = buildTimelineParams({ range: '6h', receivers: ['wifi', 'ble'], includePrivate: false, hideClasses: ['ignore'], q: '' });
assert.equal(JSON.stringify(p), JSON.stringify({ hours: 6, hide: ['ignore'], limit: 500 }));

// One receiver, private included, a search term, no hidden classes.
p = buildTimelineParams({ range: '1h', receivers: ['ble'], includePrivate: true, hideClasses: [], q: 'foo' });
assert.equal(JSON.stringify(p), JSON.stringify({ hours: 1, receivers: ['ble'], include_private: true, q: 'foo', limit: 500 }));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_apply_client_filters_hide_classes_and_search():
    _run(r"""
const { applyClientFilters } = vm.runInNewContext(raw + '\n({ applyClientFilters })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});

const devices = [
  { ident: 'a', name: 'Foo', label: { class: 'ignore', name: 'Hidden' } },
  { ident: 'b', name: 'Bar', label: { class: 'known', name: 'Visible' } },
  { ident: 'c', name: 'Baz', label: null },
];

let out = applyClientFilters(devices, { hideClasses: ['ignore'], q: '' });
assert.equal(JSON.stringify(out.map(d => d.ident)), JSON.stringify(['b', 'c']), 'the ignore-class device is dropped');

out = applyClientFilters(devices, { hideClasses: [], q: 'bar' });
assert.equal(JSON.stringify(out.map(d => d.ident)), JSON.stringify(['b']), 'search matches the device name');

out = applyClientFilters(devices, { hideClasses: [], q: 'Visible' });
assert.equal(JSON.stringify(out.map(d => d.ident)), JSON.stringify(['b']), 'search also matches the label name');

out = applyClientFilters(devices, { hideClasses: [], q: '' });
assert.equal(out.length, 3, 'no filters -> nothing dropped');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_empty_state_for():
    _run(r"""
const { emptyStateFor } = vm.runInNewContext(raw + '\n({ emptyStateFor })', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console,
});

assert.equal(emptyStateFor(0, true).setup, false);
assert.match(emptyStateFor(0, true).text, /loading/i);
assert.equal(emptyStateFor(5, false), null, 'rows present -> render the grid, not the empty state');
const empty = emptyStateFor(0, false);
assert.equal(empty.setup, true);
assert.match(empty.text, /receiver runs/);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_refresh_rows_and_visible_devices_from_stubbed_fetch():
    """_refreshRows() must populate _devices/_deviceTotal from a stubbed
    api.getTimeline (same pattern as ble-view.js's own _refreshRows test),
    and _visibleDevices() must then apply the receivers + hide/search
    filters on top of whatever the server sent back."""
    _run(r"""
const api = {
  getTimeline: async (params) => {
    assert.equal(params.hours, 6);
    return {
      devices: [
        { receiver: 'wifi', ident: 'aa:aa:aa:aa:aa:aa', name: 'Router', label: null, cells: [] },
        { receiver: 'ble', ident: 'bb:bb:bb:bb:bb:bb', name: 'Sensor', label: { class: 'ignore', name: 'Old' }, cells: [] },
      ],
      device_total: 2, truncated: false, start: 0, end: 600, bucket_s: 60,
      coverage: { wifi: [], ble: [] },
    };
  },
};
const TimelineView = vm.runInNewContext(raw + '\nTimelineView', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});

const view = Object.create(TimelineView.prototype);
view._reqSeq = 0;
view._range = '6h'; view._receivers = ['wifi', 'ble']; view._includePrivate = false;
view._hideClasses = ['ignore']; view._q = '';
view._devices = []; view._deviceTotal = 0; view._truncated = false;
view._start = 0; view._end = 0; view._bucketS = 60; view._coverage = { wifi: [], ble: [] };
view._loading = true; view._error = null;

await view._refreshRows();
assert.equal(view._devices.length, 2);
assert.equal(view._deviceTotal, 2);
assert.equal(view._error, null);
assert.equal(view._loading, false);

// The hide-ignored default (['ignore']) drops the BLE sensor row client-side too.
const visible = view._visibleDevices();
assert.equal(JSON.stringify(visible.map(d => d.ident)), JSON.stringify(['aa:aa:aa:aa:aa:aa']));

// Unchecking both receivers must show nothing, even though the server
// response (already fetched) still has both rows -- there's no "neither"
// value for the request's `receivers` param, so this is enforced here.
view._receivers = [];
assert.equal(view._visibleDevices().length, 0);
""")


# device-label-editor.js: the rpa/nrpa refusal timeline-view's row detail
# relies on (docs/history.md "Labels").
LABEL_PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_compare_preset_and_param_building_and_epoch_roundtrip():
    _run(r"""
const { lastTenVsPreviousPreset, buildCompareParams, epochToLocalInputValue, localInputValueToEpoch } =
  vm.runInNewContext(raw + '\n({ lastTenVsPreviousPreset, buildCompareParams, epochToLocalInputValue, localInputValueToEpoch })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console });

// "Last 10 min vs previous 10 min": the two windows are back-to-back and
// non-overlapping, B ending now.
const now = 1_790_000_000;
const w = lastTenVsPreviousPreset(now);
assert.equal(w.bEnd, now);
assert.equal(w.bStart, now - 600);
assert.equal(w.aEnd, w.bStart);
assert.equal(w.aStart, now - 1200);

// Both receivers (the default) -> omitted, same contract as buildTimelineParams.
let p = buildCompareParams({ aStart: 1, aEnd: 2, bStart: 3, bEnd: 4, receivers: ['wifi', 'ble'], includePrivate: false });
assert.equal(JSON.stringify(p), JSON.stringify({ a_start: 1, a_end: 2, b_start: 3, b_end: 4 }));
p = buildCompareParams({ aStart: 1, aEnd: 2, bStart: 3, bEnd: 4, receivers: ['ble'], includePrivate: true });
assert.equal(JSON.stringify(p), JSON.stringify({ a_start: 1, a_end: 2, b_start: 3, b_end: 4, receivers: ['ble'], include_private: true }));

// epoch <-> datetime-local roundtrip (minute precision; datetime-local has no seconds field).
const epoch = 1_700_000_000;
const roundTripped = localInputValueToEpoch(epochToLocalInputValue(epoch));
assert.ok(Math.abs(roundTripped - epoch) < 60, 'roundtrip must stay within one minute');
assert.equal(epochToLocalInputValue(null), '');
assert.equal(localInputValueToEpoch(''), null);
assert.equal(localInputValueToEpoch('not-a-date'), null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_label_editor_refuses_rotating_ble_addresses():
    script = LABEL_PRELUDE + r"""
const { rotatingBlocker } = vm.runInNewContext(raw + '\n({ rotatingBlocker })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
});

assert.match(rotatingBlocker('ble', 'rpa'), /rotates/);
assert.match(rotatingBlocker('ble', 'nrpa'), /rotates/);
assert.equal(rotatingBlocker('ble', 'public'), null, 'a stable BLE address can be labelled');
assert.equal(rotatingBlocker('ble', 'static'), null);
assert.equal(rotatingBlocker('wifi', 'bssid'), null, 'Wi-Fi BSSIDs never rotate the same way');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(LABEL_SOURCE)],
                            capture_output=True, text=True, timeout=30, env=node_env())
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_axis_ticks_and_cell_at():
    """axisTicks spans the range in n labelled fractions (date only past one
    day); cellAt maps pointer -> row/column with the same bucket maths as
    _drawGrid and flags columns missing from the receiver's coverage."""
    _run(r"""
const { axisTicks, cellAt } = vm.runInNewContext(
  raw + '\n({ axisTicks, cellAt })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);

const t0 = new Date(2026, 0, 5, 10, 0, 0).getTime() / 1000;
const short = axisTicks(t0, t0 + 3600, 4);
assert.equal(JSON.stringify(short.map((t) => t.x)), '[0,0.25,0.5,0.75]');
assert.equal(short.map((t) => t.label).join(','), '10:00,10:15,10:30,10:45');
const long = axisTicks(t0, t0 + 3 * 86400, 3);
assert.equal(long.length, 3);
assert.ok(/\d\d:\d\d$/.test(long[0].label) && long[0].label.length > 5, 'multi-day labels carry the date');

const rows = [{ receiver: 'ble', ident: 'aa:bb', signal_kind: 'dbm', label: { name: 'Tag' }, cells: [[t0 + 60, 7, -61.4]] }];
const gaps = { ble: [3], wifi: [] };
const hit = cellAt(rows, gaps, 0.05, 5, t0, 60, 10);
assert.equal(hit.name, 'Tag');
assert.equal(hit.reports, 0, 'column 0 has no cell');
const hit1 = cellAt(rows, gaps, 0.15, 5, t0, 60, 10);
assert.equal(hit1.reports, 7);
assert.equal(hit1.signal, -61.4);
assert.equal(hit1.signalKind, 'dbm');
assert.equal(hit.signal, null);
assert.equal(hit1.t1 - hit1.t0, 60);
assert.equal(cellAt(rows, gaps, 0.5, 18 * 3, t0, 60, 10), null, 'below the last row');
const gap = cellAt(rows, gaps, 0.35, 5, t0, 60, 10);
assert.equal(gap.notListening, true);
assert.equal(hit1.notListening, false);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_clear_history_asks_first_then_wipes_and_reloads():
    """The Clear history button first asks inline (_confirmClear); on yes it
    calls api.clearHistory, drops the selection and reloads the rows."""
    _run(r"""
const calls = [];
const api = {
  clearHistory: async () => calls.push('clear'),
  getTimeline: async () => { calls.push('timeline'); return { devices: [], total: 0, start: 0, end: 0, bucket_s: 60, coverage: {} }; },
};
const TimelineView = vm.runInNewContext(raw + '\nTimelineView', {
  LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  api, console, setTimeout, clearTimeout,
});
const view = Object.create(TimelineView.prototype);
view._reqSeq = 0; view._range = '6h'; view._receivers = ['wifi', 'ble']; view._includePrivate = false;
view._hideClasses = ['ignore']; view._q = ''; view._devices = []; view._deviceTotal = 0;
view._start = 0; view._end = 0; view._bucketS = 60; view._coverage = {}; view._error = null;
view._selected = { receiver: 'ble', ident: 'aa' }; view._detail = { x: 1 }; view._compareResult = { y: 1 };

view._confirmClear = true;
view._confirmClear = false; // "Keep"
assert.equal(JSON.stringify(calls), '[]', 'declined confirm does nothing');
assert.ok(view._selected, 'selection kept when declined');

view._confirmClear = true; // first click
await view._clearHistory(); // "Yes, clear"
assert.equal(view._confirmClear, false);
assert.equal(JSON.stringify(calls), '["clear","timeline"]');
assert.equal(view._selected, null);
assert.equal(view._detail, null);
assert.equal(view._compareResult, null);
assert.equal(view._clearing, false);
assert.equal(view._error, null);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_signal_colour_mode_scales_per_receiver():
    """Colour = signal: darkness is the cell's mean signal within its own
    receiver's min..max (never one scale across Wi-Fi codes and BLE dBm); a
    heard cell without a signal value stays faint instead of disappearing;
    the reports mode is unchanged."""
    _run(r"""
const { signalDomains, cellAlpha, signalText } = vm.runInNewContext(
  raw + '\n({ signalDomains, cellAlpha, signalText })',
  { LitElement: class {}, html: () => {}, svg: () => {}, customElements: { define() {} }, console },
);
const devices = [
  { receiver: 'ble', cells: [[0, 3, -90], [60, 5, -50], [120, 1, null]] },
  { receiver: 'ble', cells: [[0, 9, -70]] },
  { receiver: 'wifi', cells: [[0, 2, 20], [60, 2, 40]] },
];
const dom = signalDomains(devices);
assert.equal(JSON.stringify(dom.ble), '{"min":-90,"max":-50}');
assert.equal(JSON.stringify(dom.wifi), '{"min":20,"max":40}');
assert.equal(signalDomains([{ receiver: 'ble', cells: [[0, 1, null]] }]).ble, null);
assert.equal(signalDomains([]).wifi, null);

assert.equal(cellAlpha([0, 3, -90], 'signal', 9, dom.ble), 0.25, 'weakest -> faint');
assert.equal(cellAlpha([60, 5, -50], 'signal', 9, dom.ble), 1, 'strongest -> solid');
assert.equal(cellAlpha([0, 9, -70], 'signal', 9, dom.ble).toFixed(3), '0.625');
assert.equal(cellAlpha([120, 1, null], 'signal', 9, dom.ble), 0.15, 'heard, no value: faint, not gone');
assert.equal(cellAlpha([0, 2, 30], 'signal', 2, null), 0.15, 'receiver without any signal values');
assert.equal(cellAlpha([0, 2, 30], 'signal', 2, { min: 30, max: 30 }), 1, 'flat domain never divides by zero');
assert.equal(cellAlpha([0, 9, -70], 'reports', 9, dom.ble), 1);
assert.equal(cellAlpha([0, 3, -70], 'reports', 9, dom.ble), 0.5);

assert.equal(signalText(-61.4, 'dbm'), '-61 dBm');
assert.equal(signalText(31.2, 'raw'), 'signal code 31 (uncalibrated)');
assert.equal(signalText(null, 'dbm'), null);
""")
