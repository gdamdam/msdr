# ruff: noqa: E501 -- embedded JavaScript source lines
"""Reference-catalog UI (docs/reference-catalogs.md "API and UI"): row
rendering for the four frequency cases, state words, tune gating, station-id
detection, the "Nearby" sort toggle's 400 handling, and out-of-order search
responses with the new filter params."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

CATALOG_VIEW_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/catalog-view.js"

# Strips every top-level `export` (class/function/const) so the source can run
# as a classic vm script (not an ES module) -- same transform as
# test_catalog_view_refresh.py, generalized to the new exported helpers.
_STRIP = r"""
function stripExports(src) {
  return src.replace(/^import .*;\n/gm, '').replace(/^export (class|function|const) /gm, '$1 ');
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frequency_text_covers_the_four_cases():
    script = (
        _STRIP
        + r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = stripExports(fs.readFileSync(process.argv[1], 'utf8'));
const context = {
  LitElement: class {}, html: (...parts) => parts, customElements: { define() {} },
  mhz: (hz) => hz == null ? '--' : `${(hz / 1e6).toFixed(6)} MHz`,
  relativeOrDate: (iso, at) => {
    const mins = Math.floor((at.getTime() - Date.parse(iso)) / 60000);
    return mins < 60 ? `${mins}m ago` : `${Math.floor(mins / 60)}h ago`;
  },
  console, Date, Number, Math,
};
vm.createContext(context);
vm.runInContext(source + "\nthis.__exports = { catalogFrequencyText, catalogCurrentFreqBadge, catalogStateWord, catalogTuneInfo, looksLikeStationId };", context);
const { catalogFrequencyText, catalogCurrentFreqBadge, catalogStateWord, catalogTuneInfo, looksLikeStationId } = context.__exports;

// 1. Fixed frequency (as today).
assert.equal(catalogFrequencyText({ frequency_hz: 10000000 }), '10.000000 MHz');

// 2. Operating range: frequency_hz null, both lo/hi set.
assert.equal(catalogFrequencyText({ frequency_hz: null, freq_lo_hz: 4300000, freq_hi_hz: 4600000 }), '4.3–4.6 MHz range');

// 2b. One-sided ranges still produce something sensible.
assert.equal(catalogFrequencyText({ freq_lo_hz: 13475000 }), '13.475 MHz+ range');
assert.equal(catalogFrequencyText({ freq_hi_hz: 13475000 }), 'up to 13.475 MHz range');

// 3. All null -> "frequency not published".
assert.equal(catalogFrequencyText({ frequency_hz: null, freq_lo_hz: null, freq_hi_hz: null }), 'frequency not published');
assert.equal(catalogFrequencyText({}), 'frequency not published');

// 4. current_freq_hz badge -- separate from the above, freshness + staleness.
assert.equal(catalogCurrentFreqBadge({ current_freq_hz: null }), null);
const now = new Date('2026-10-01T12:00:00Z');
const fresh = catalogCurrentFreqBadge({ current_freq_hz: 13475000, current_freq_at: '2026-10-01T10:00:00Z' }, now);
assert.equal(fresh.stale, false);
assert.match(fresh.text, /^current 13\.475000 MHz at /);
const stale = catalogCurrentFreqBadge({ current_freq_hz: 13475000, current_freq_at: '2026-09-29T10:00:00Z' }, now);
assert.equal(stale.stale, true);
// Undated current frequency can't be trusted as fresh either.
assert.equal(catalogCurrentFreqBadge({ current_freq_hz: 13475000 }, now).stale, true);

// State words: null for schedule rows (default when ref_type is missing).
assert.equal(catalogStateWord({}), null);
assert.equal(catalogStateWord({ ref_type: 'schedule' }), null);
assert.equal(catalogStateWord({ ref_type: 'station', on_air_state: 'continuous' }), 'continuous service');
assert.equal(catalogStateWord({ ref_type: 'navaid', on_air_state: 'published_active' }), 'published active');
assert.equal(catalogStateWord({ ref_type: 'radar_site', on_air_state: 'unknown' }), 'state unknown');
assert.equal(catalogStateWord({ ref_type: 'radar_site' }), 'state unknown');

// Tune gating: tunable rows get the normal title; non-tunable rows explain why.
// (Plain property checks, not deepEqual -- these objects come from a vm
// realm with a different Object prototype than this script's.)
const tunableA = catalogTuneInfo({ tunable: true });
assert.equal(tunableA.tunable, true);
assert.equal(tunableA.title, 'Tune');
const tunableB = catalogTuneInfo({ frequency_hz: 1000 });
assert.equal(tunableB.tunable, true);
assert.equal(tunableB.title, 'Tune');
const noFreq = catalogTuneInfo({ tunable: false });
assert.equal(noFreq.tunable, false);
assert.match(noFreq.title, /No published frequency/);
const rangeOnly = catalogTuneInfo({ tunable: false, freq_lo_hz: 1000, freq_hi_hz: 2000 });
assert.equal(rangeOnly.tunable, false);
assert.match(rangeOnly.title, /operating range/);

// Station-id detection: <=5 alphanumerics.
assert.equal(looksLikeStationId('SFO'), true);
assert.equal(looksLikeStationId('WWV'), true);
assert.equal(looksLikeStationId('ACI1'), true);
assert.equal(looksLikeStationId(''), false);
assert.equal(looksLikeStationId('  '), false);
assert.equal(looksLikeStationId('toolong1'), false);
assert.equal(looksLikeStationId('has space'), false);
assert.equal(looksLikeStationId('news in english'), false);
"""
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CATALOG_VIEW_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


def _harness(extra_globals=""):
    """A vm context exposing the whole CatalogView class (for instance
    methods), same construction test_catalog_view_refresh.py uses."""
    return (
        _STRIP
        + r"""
import fs from 'node:fs';
import vm from 'node:vm';
"""
        + extra_globals
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_nearby_400_shows_detail_inline_and_resets_the_toggle():
    script = _harness() + r"""
import assert from 'node:assert/strict';

const source = stripExports(fs.readFileSync(process.argv[1], 'utf8'));
let lastParams;
const api = {
  searchCatalog: async (params) => {
    lastParams = params;
    if (params.sort === 'distance') {
      throw new Error('No receiver location configured; set it in Settings → Location');
    }
    return { entries: [{ station: 'WWV' }], count: 1 };
  },
};
const context = vm.runInNewContext(source + '\nCatalogView', {
  LitElement: class {}, html: (...parts) => parts, customElements: { define() {} },
  mhz: (hz) => hz == null ? '--' : `${(hz/1e6).toFixed(6)} MHz`,
  catalogOnAir: () => true, catalogWindowLabel: () => '24h',
  relativeOrDate: () => 'just now', dateTime: () => '--',
  api, setTimeout, clearTimeout, setInterval, clearInterval, Date, Number, Math, Set,
});
const view = Object.create(context.prototype);
Object.assign(view, {
  _requestId: 0, _loading: false, _band: '', _minMhz: '', _maxMhz: '',
  _language: '', _myLangs: false, languages: [], _query: '', _onAir: false,
  _selectedSources: [], _offset: 0, _entries: [], _count: 0,
  _rangeError: null, _error: null, _network: '', _refType: '',
  _nearby: true, _nearbyError: null, _sources: [],
});
await view._search();
assert.equal(view._nearby, false, 'the toggle must reset, never assume a location');
assert.match(view._nearbyError, /No receiver location configured/, 'the server detail text must show inline');
// The retried request (without sort=distance) still runs and populates results.
assert.equal(lastParams.sort, undefined);
assert.equal(view._entries[0].station, 'WWV');
assert.equal(view._count, 1);
assert.equal(view._error, null, 'a 400 for distance sort is not a generic search failure');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CATALOG_VIEW_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_station_id_param_and_out_of_order_responses_with_new_filters():
    script = _harness() + r"""
import assert from 'node:assert/strict';

const source = stripExports(fs.readFileSync(process.argv[1], 'utf8'));
const pending = [];
const api = { searchCatalog: (params) => new Promise((resolve) => pending.push({ params, resolve })) };
const context = vm.runInNewContext(source + '\nCatalogView', {
  LitElement: class {}, html: (...parts) => parts, customElements: { define() {} },
  mhz: (hz) => hz == null ? '--' : `${(hz/1e6).toFixed(6)} MHz`,
  catalogOnAir: () => true, catalogWindowLabel: () => '24h',
  relativeOrDate: () => 'just now', dateTime: () => '--',
  api, setTimeout, clearTimeout, setInterval, clearInterval, Date, Number, Math, Set,
});
const view = Object.create(context.prototype);
Object.assign(view, {
  _requestId: 0, _loading: false, _band: '', _minMhz: '', _maxMhz: '',
  _language: '', _myLangs: false, languages: [], _query: 'WWV', _onAir: false,
  _selectedSources: [], _offset: 0, _entries: [], _count: 0,
  _rangeError: null, _error: null, _network: 'NIST', _refType: 'station',
  _nearby: false, _nearbyError: null, _sources: [],
});

const first = view._search();
assert.equal(pending[0].params.q, 'WWV');
assert.equal(pending[0].params.station_id, 'WWV', 'a short alphanumeric query also goes as station_id');
assert.equal(pending[0].params.network, 'NIST');
assert.equal(pending[0].params.ref_type, 'station');

// A newer search (e.g. the operator changed ref_type) must win even if the
// older request's response arrives later -- same token contract as
// detect-readout's allocation lookups.
view._refType = 'navaid';
const second = view._search();
assert.equal(pending.length, 2);
pending[1].resolve({ entries: [{ station: 'newer' }], count: 1 });
await second;
assert.equal(view._entries[0].station, 'newer');

pending[0].resolve({ entries: [{ station: 'stale' }], count: 1 });
await first;
assert.equal(view._entries[0].station, 'newer', 'a late reply for an older request must never overwrite the newer result');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CATALOG_VIEW_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_tune_button_absent_for_a_non_tunable_row():
    """Row rendering: a non-tunable entry gets a disabled control with an
    explanatory title instead of a working Tune button, and clicking the row
    itself must not fire a tune (unlike a tunable row)."""
    script = _harness() + r"""
import assert from 'node:assert/strict';

const source = stripExports(fs.readFileSync(process.argv[1], 'utf8'));
const html = (parts, ...args) => ({ parts, args });
const flatten = (node, out = []) => {
  if (node == null) return out;
  if (typeof node === 'string') { out.push(node); return out; }
  if (Array.isArray(node)) { for (const n of node) flatten(n, out); return out; }
  if (node.parts) { flatten([...node.parts], out); flatten(node.args, out); return out; }
  return out;
};
const context = vm.runInNewContext(source + '\nCatalogView', {
  LitElement: class {}, html, customElements: { define() {} },
  mhz: (hz) => hz == null ? '--' : `${(hz/1e6).toFixed(6)} MHz`,
  catalogOnAir: () => true, catalogWindowLabel: () => '24h',
  relativeOrDate: () => 'just now', dateTime: () => '2026-10-01',
  api: {}, Date, Number, Math, Set,
});
const view = Object.create(context.prototype);
Object.assign(view, { _now: Date.now(), tz: null, _sources: [] });

const tunableRow = flatten(view._row({ station: 'WWV', frequency_hz: 10000000, tunable: true }));
assert.ok(tunableRow.some((s) => typeof s === 'string' && s.includes('Tune')));

const notTunable = view._row({ station: 'ACI1 radar', ref_type: 'radar_site', tunable: false, frequency_hz: null });
const text = flatten(notTunable).join(' ');
assert.match(text, /disabled/, 'a non-tunable row must render a disabled control, not a working Tune button');
assert.match(text, /No published frequency to tune/, 'the disabled control explains why it is disabled');
assert.doesNotMatch(text, /\btitle="Tune"\b/, 'the disabled control must not carry the normal "Tune" title');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CATALOG_VIEW_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
