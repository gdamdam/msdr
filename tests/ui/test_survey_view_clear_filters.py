"""Empty-state actions: survey table "clear filters" and recent-list "clear
search" reset the view's filter state instead of dead-ending the user."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SURVEY = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"
RECENT = Path(__file__).resolve().parents[2] / "msdr/ui/components/recent-list.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_clear_filters_resets_activity_evidence_and_snr():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const stored = {};
const ctx = {
  LitElement: class {}, html: () => '', css: () => '', customElements: { define() {} }, console,
  localStorage: { getItem: (k) => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; } },
  window: { addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false }) },
  document: { addEventListener() {}, removeEventListener() {} },
  requestAnimationFrame: () => 0, cancelAnimationFrame() {}, setTimeout, clearTimeout, performance,
  ResizeObserver: class { observe() {} disconnect() {} }, navigator: {}, CustomEvent: class {},
};
vm.runInNewContext(src + '\nglobalThis.SurveyView = SurveyView;', ctx);
const view = Object.create(ctx.SurveyView.prototype);
view._activityFilter = 'speech'; view._evidenceFilter = 'decoded'; view._snrMin = 10;
  view._snrMax = 40;
view._clearFilters();
assert.equal(view._activityFilter, 'all');
assert.equal(view._evidenceFilter, 'all');
assert.deepEqual([view._snrMin, view._snrMax], ['', '']);
assert.equal(stored['msdr.survey.snrFilter'], JSON.stringify({ min: '', max: '' }),
  'cleared SNR range must be persisted like any other SNR change');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SURVEY)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_recent_list_offers_clear_search_only_when_a_query_is_set():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const html = (strings, ...values) => strings.map((s,
  i) => s + (i < values.length ? flat(values[i]) : '')).join('');
const flat = (v) => Array.isArray(v) ? v.map(flat).join('') : (v == null ? '' : String(v));
const ctx = { LitElement: class {}, html, css: () => '', customElements: { define() {} }, console,
  CustomEvent: class {}, localStorage: { getItem: () => null, setItem() {} }, setTimeout,
    clearTimeout,
  // stubs for recent-list.js's own imports (stripped above)
  mhz: (...a) => (typeof a[0] === 'number' ? String(a[0]) : '') };
vm.runInNewContext(src + '\nglobalThis.RecentList = RecentList;', ctx);
const view = Object.create(ctx.RecentList.prototype);
Object.assign(view, { signals: [{ id: 1, name: 'alpha', frequency_hz: 1000000,
  demodulator: 'AM' }], scope: 'all', tagsById: {}, identFeature: false, failure: null,
  emptyMessage: '', _query: 'zzz', _sort: 'default', _favoriteIds: new Set() });
let out = view.render();
assert.ok(out.includes('clear search'), 'a set query offers "clear search"');
view._query = '';
out = view.render();
assert.ok(!out.includes('clear search'), 'no query, no clear action');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(RECENT)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
