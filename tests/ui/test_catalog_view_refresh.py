"""Catalog view schedule refresh and retry behavior using component methods."""

import shutil
import subprocess
from pathlib import Path

import pytest

CATALOG_VIEW_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/catalog-view.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_on_air_tick_refreshes_the_current_page_and_search_errors_can_retry():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (class|function|const) /gm, '$1 ');
const CatalogView = vm.runInNewContext(source + '\nCatalogView', {
  LitElement: class {}, html: (...parts) => parts, customElements: { define() {} },
  mhz() {}, catalogOnAir() {}, catalogWindowLabel() {}, api: {},
});
const view = Object.create(CatalogView.prototype);
Object.assign(view, { _onAir: true, _offset: 200, _now: 0 });
let refreshOptions;
view._search = options => { refreshOptions = options; };
view._refreshClock();
assert.equal(typeof view._now, 'number');
assert.equal(refreshOptions.preserveScroll, true);
assert.equal(view._offset, 200);

let attempts = 0;
let lastOptions;
let restoredScroll;
const api = { searchCatalog: async options => {
  attempts += 1;
  lastOptions = options;
  if (attempts === 1) throw new Error('server unavailable');
  return { entries: [], count: 0 };
}};
const SearchView = vm.runInNewContext(source + '\nCatalogView', {
  LitElement: class {}, html: (...parts) => parts, customElements: { define() {} },
  mhz() {}, catalogOnAir() {}, catalogWindowLabel() {}, api,
  setTimeout, clearTimeout, setInterval, clearInterval, Date, Number, Math,
  window: { scrollY: 375, scrollX: 0, scrollTo: (_x, y) => { restoredScroll = y; } },
  document: { documentElement: { scrollTop: 375 } },
});
const search = Object.create(SearchView.prototype);
Object.assign(search, {
  _requestId: 0, _loading: false, _band: '', _minMhz: '', _maxMhz: '',
  _language: '', _myLangs: false, languages: [], _query: '', _onAir: true,
  _selectedSources: [], _offset: 100, _entries: [], _count: 0,
  _rangeError: null, _error: null,
});
await search._search();
assert.equal(search._error, 'Catalog search failed: server unavailable');
assert.equal(search._count, 0);
await search._search();
assert.equal(search._error, null);
assert.equal(search._count, 0);
assert.equal(attempts, 2);

search.updateComplete = Promise.resolve();
await search._refreshClock();
assert.equal(lastOptions.offset, 100);
assert.equal(lastOptions.on_air, 1);
assert.equal(restoredScroll, 375);
assert.equal(search._offset, 100);

// An earlier response must not replace the newly selected source.
const pending = [];
api.searchCatalog = options => new Promise(resolve => pending.push({ options, resolve }));
search._selectedSources = ['aoki'];
const oldRequest = search._search();
search._toggleSource('eibi');
assert.equal(pending[1].options.sources.join(','), 'eibi');
assert.equal(pending[1].options.offset, 0);
pending[1].resolve({ entries: [{ station: 'new source' }], count: 1 });
await Promise.resolve();
pending[0].resolve({ entries: [{ station: 'old source' }], count: 1 });
await oldRequest;
assert.equal(search._entries[0].station, 'new source');

// Invalidate immediately, including the debounce interval before the next request.
const beforeTyping = search._search();
search._onQueryInput('updated');
pending[2].resolve({ entries: [{ station: 'outdated query' }], count: 1 });
await beforeTyping;
assert.equal(search._entries[0].station, 'new source');
assert.equal(search._loading, true);
clearTimeout(search._debounce);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(CATALOG_VIEW_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
