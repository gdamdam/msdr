# ruff: noqa: E501 -- embedded JavaScript source lines
"""setup-drawer.js's CATALOG sources section (docs/reference-catalogs.md
"API and UI"): the new `kind` ("static"/"refreshable"/"live") renders as a
word plus a one-line explanation, and `coverage` shows alongside it, on every
_catalogSourceRow -- without losing the row's existing label/description/
last-refresh/entry-count/error content."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SETUP_DRAWER_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/setup-drawer.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_catalog_source_kind_info_words_and_explanations():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '')
  .replace(/^export (class|function|const) /gm, '$1 ');
const context = { LitElement: class {}, html: () => {}, customElements: { define() {} }, console };
vm.createContext(context);
vm.runInContext(source + "\nthis.__exports = { catalogSourceKindInfo };", context);
const { catalogSourceKindInfo } = context.__exports;

const live = catalogSourceKindInfo('live');
assert.equal(live.text, 'live');
assert.match(live.title, /not a transmission indicator/);

const refreshable = catalogSourceKindInfo('refreshable');
assert.equal(refreshable.text, 'refreshable');
assert.match(refreshable.title, /downloaded on refresh/i);

const staticKind = catalogSourceKindInfo('static');
assert.equal(staticKind.text, 'static');
assert.match(staticKind.title, /dated bundled snapshot/i);

// Missing/unrecognized kind degrades instead of breaking the row.
assert.equal(catalogSourceKindInfo(undefined), null);
assert.equal(catalogSourceKindInfo(null), null);
assert.equal(catalogSourceKindInfo('mystery').text, 'mystery');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SETUP_DRAWER_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_catalog_source_row_renders_kind_coverage_and_existing_fields():
    """_catalogSourceRow still shows label/description/fetched line/entry
    count/error, and additionally the kind word+explanation and coverage --
    for both a never-fetched reference source and a fetched one with an
    error, so neither new field crowds out the old content."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '')
  .replace(/^export (class|function|const) /gm, '$1 ');
const html = (parts, ...args) => ({ parts, args });
const flatten = (node, out = []) => {
  if (node == null) return out;
  if (typeof node === 'string') { out.push(node); return out; }
  if (Array.isArray(node)) { for (const n of node) flatten(n, out); return out; }
  if (node.parts) { flatten([...node.parts], out); flatten(node.args, out); return out; }
  return out;
};
const context = vm.runInNewContext(source + '\nSetupDrawer', {
  LitElement: class {}, html, customElements: { define() {} },
  containModalFocus() {}, restoreModalFocus() {}, trapModalTab() {},
  resolveFeatures: (f) => f ?? {}, resolveWorkspaceLayout: () => ({ columns: [[], [], []], wide: [] }),
  sectionVisible: () => true, SECTION_LABELS: {}, LAYOUT_PRESETS: {}, WORKSPACE_LABELS: {},
  WORKSPACE_SECTIONS: {}, DEFAULT_WORKSPACE_COLUMNS: {}, utcOffsetLabel: () => '', readinessLabels: () => ({}),
  readout: (hz) => hz == null ? '--' : String(hz), api: {}, console, Date, Number, Math, Set,
});
const view = Object.create(context.prototype);
Object.assign(view, { _sources: [], _sourceErrors: {}, _orig: { catalog_sources: {} } });
const draft = { catalogSources: {} };

const neverFetched = view._catalogSourceRow(
  { source: 'ioos_hfradar', label: 'IOOS HFRNet radar sites', description: 'Coastal HF radar site list',
    kind: 'refreshable', coverage: 'US coastal sites', static: false, has_default_url: true },
  draft,
);
let text = flatten(neverFetched).join(' ');
assert.match(text, /IOOS HFRNet radar sites/, 'existing label must still render');
assert.match(text, /Coastal HF radar site list/, 'existing description must still render');
assert.match(text, /refreshable/);
assert.match(text, /downloaded on refresh/i, 'the one-line explanation must render alongside the word');
assert.match(text, /US coastal sites/, 'coverage must render');
assert.match(text, /Not fetched yet/, 'existing not-fetched fallback must still render');

view._sources = [{ source: 'nist_time', entry_count: 12, fetched_at: '2026-10-01', stale: false }];
view._sourceErrors = { nist_time: 'network unreachable' };
const errored = view._catalogSourceRow(
  { source: 'nist_time', label: 'NIST time stations', description: 'WWV/WWVH/WWVB', kind: 'static',
    coverage: 'WWV, WWVH, WWVB', static: true },
  draft,
);
text = flatten(errored).join(' ');
assert.match(text, /static/);
assert.match(text, /dated bundled snapshot/i);
assert.match(text, /WWV, WWVH, WWVB/);
assert.match(text, /network unreachable/, 'existing error rendering must still work');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SETUP_DRAWER_JS)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
