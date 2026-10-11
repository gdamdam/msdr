# ruff: noqa: E501 -- embedded JavaScript source lines
"""allocation-summary.js: compact line lists every service (primary first,
secondary marked), and every non-ok status renders its status-specific label
plus the lookup result's `message` verbatim (docs/allocations.md "Lookup
result"). createRenderRoot/lit touch `document` at import time (same
constraint as test_marker_hold.py), so this runs the source in a vm sandbox
with LitElement/html/customElements stubbed, rather than importing it as a
real ES module."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/allocation-summary.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_compact_line_lists_every_service_and_expanded_detail_renders_all_sections():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class) /gm, '$1 ');
const html = (...parts) => parts;
const { allocationStatusInfo, servicesLine, compactText, AllocationSummary } = vm.runInNewContext(
  src + '\n({ allocationStatusInfo, servicesLine, compactText, AllocationSummary })',
  { LitElement: class {}, html, customElements: { define() {} } },
);

function flatten(node, out = []) {
  if (typeof node === 'string') out.push(node);
  else if (Array.isArray(node)) for (const n of node) flatten(n, out);
  return out;
}

const result = {
  frequency_hz: 5264000, jurisdiction: 'US', status: 'ok',
  lo_hz: 5250000, hi_hz: 5275000,
  national: {
    federal: {
      services: [
        { name: 'FIXED', priority: 'primary', raw: 'FIXED' },
        { name: 'MOBILE', qualifier: 'except aeronautical mobile', priority: 'primary', raw: 'MOBILE except aeronautical mobile' },
      ],
      footnotes: ['US340'],
    },
    non_federal: {
      services: [{ name: 'Radiolocation', priority: 'secondary', raw: 'Radiolocation' }],
      footnotes: [], rule_parts: ['Maritime (80)'],
    },
    shared: false,
  },
  international: {
    region: 'ITU2', lo_hz: 5250000, hi_hz: 5275000,
    services: [{ name: 'FIXED', priority: 'primary', raw: 'FIXED' }], footnotes: [],
  },
  footnotes: [{ id: 'US340', text: 'Footnote US340 full text.', source_id: 'ecfr_2_106', kind: 'us' }],
  citations: [{ source_id: 'ntia_table', title: 'NTIA Table', url: 'https://redbook.ntia.gov/', edition: '2025', effective_date: '2025-09-10' }],
  disagreements: [{ kind: 'footnote_text', id: 'US340', sources: ['ntia_table', 'ecfr_2_106'] }],
  sources: { stale: false, statuses: { ntia_table: 'ok', ecfr_2_106: 'ok' } },
};

// Exact wording from the component's own contract.
assert.equal(
  compactText(result),
  '5.250–5.275 MHz · US · FIXED, MOBILE except aeronautical mobile · Radiolocation (secondary)',
);
assert.equal(servicesLine(result), 'FIXED, MOBILE except aeronautical mobile · Radiolocation (secondary)');
assert.equal(allocationStatusInfo(result), null, '"ok" has no status info');

const view = Object.create(AllocationSummary.prototype);
view.data = result;
view.compact = false;
view._expanded = false;
const collapsed = flatten(view.render());
assert.ok(collapsed.some(s => s.includes('FIXED, MOBILE except aeronautical mobile · Radiolocation (secondary)')),
  'compact line must include every service, not a truncated subset');

view._expanded = true;
const expanded = flatten(view.render());
const text = expanded.join(' ');
assert.match(text, /FIXED/);
assert.match(text, /MOBILE except aeronautical mobile/);
assert.match(text, /Radiolocation/);
assert.match(text, /Maritime \(80\)/, 'non-federal rule parts must render');
assert.match(text, /US340/);
assert.match(text, /Footnote US340 full text\./, 'footnote ids render with their resolved text');
assert.match(text, /NTIA Table/);
assert.match(text, /2025-09-10/, 'citation effective date must render');
assert.match(text, /International context \(ITU Region 2 table as reproduced in the US table\)/);
assert.match(text, /footnote_text/, 'disagreements must render when present');

// Federal and Non-Federal render side by side, each with only its own
// services (never merged): FIXED/MOBILE are Federal here, Radiolocation
// (secondary) Non-Federal.
assert.match(text, /Federal/);
assert.match(text, /Non-Federal/);
const federal = flatten(view._renderServiceColumn('Federal', result.national.federal)).join(' ');
const nonFederal = flatten(view._renderServiceColumn('Non-Federal', result.national.non_federal)).join(' ');
assert.match(federal, /FIXED/); assert.match(federal, /primary/);
assert.doesNotMatch(federal, /Radiolocation/);
assert.match(nonFederal, /Radiolocation/); assert.match(nonFederal, /secondary/);
assert.doesNotMatch(nonFederal, /FIXED/);
assert.match(nonFederal, /Maritime \(80\)/);

const stale = { ...result, sources: { stale: true, statuses: {} } };
view.data = stale;
view._expanded = false;
assert.ok(flatten(view.render()).some(s => s.includes('stale')), 'stale badge shown when sources.stale');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_non_ok_status_renders_its_label_and_verbatim_message():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class) /gm, '$1 ');
const html = (...parts) => parts;
const { allocationStatusInfo, AllocationSummary } = vm.runInNewContext(
  src + '\n({ allocationStatusInfo, AllocationSummary })',
  { LitElement: class {}, html, customElements: { define() {} } },
);

function flatten(node, out = []) {
  if (typeof node === 'string') out.push(node);
  else if (Array.isArray(node)) for (const n of node) flatten(n, out);
  return out;
}

const cases = [
  ['not_covered', 'Not covered by loaded data'],
  ['unsupported_jurisdiction', 'Jurisdiction not supported'],
  ['source_unavailable', 'Allocation source unavailable'],
  ['unallocated', 'Explicitly unallocated'],
];
const view = Object.create(AllocationSummary.prototype);
for (const [status, label] of cases) {
  const message = `message for ${status}`;
  const d = { status, message, frequency_hz: 1 };
  const info = allocationStatusInfo(d);
  assert.equal(info.label, label);
  assert.equal(info.message, message);

  view.data = d;
  view.compact = false;
  view._expanded = false;
  const text = flatten(view.render()).join(' ');
  assert.ok(text.includes(label), `${status}: expected label "${label}"`);
  assert.ok(text.includes(message), `${status}: message must render verbatim`);
}
assert.equal(allocationStatusInfo(null), null);
assert.equal(allocationStatusInfo({ status: 'ok' }), null);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_details_open_a_dialog_and_open_chart_bubbles_to_window():
    """Details renders a native <dialog> (sections: columns, footnotes, sources)
    instead of an inline div, and Open chart dispatches a bubbling, composed
    event -- a non-bubbling one never reached app.js's window listener."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export /gm, '');
const html = (strings, ...values) => strings.map((s, i) => s + (i < values.length ? flat(values[i]) : '')).join('');
const flat = (v) => Array.isArray(v) ? v.map(flat).join('') : (v == null ? '' : String(v));
const ctx = {
  LitElement: class {}, html, customElements: { define() {} }, console, document: { activeElement: null },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; this.bubbles = !!init?.bubbles; this.composed = !!init?.composed; } },
};
vm.runInNewContext(src + '\nglobalThis.AllocationSummary = AllocationSummary;', ctx);
const view = Object.create(ctx.AllocationSummary.prototype);
view.compact = true; view._expanded = true;
view.querySelector = () => null;
view.data = {
  frequency_hz: 26010000, lo_hz: 25670000, hi_hz: 26100000, jurisdiction: 'US', status: 'ok',
  national: { federal: { services: [], footnotes: [] },
    non_federal: { services: [{ name: 'BROADCASTING', priority: 'primary', footnotes: ['5.134'], raw: 'BROADCASTING 5.134' }], footnotes: [], rule_parts: ['International Broadcast Stations (73F)'] }, shared: false },
  international: { region: 'ITU2', services: [{ name: 'BROADCASTING', priority: 'primary', footnotes: [] }], footnotes: [] },
  footnotes: [{ id: '5.134', text: 'x'.repeat(300), source_id: 'ecfr_2_106' }],
  citations: [{ source_id: 'ntia_table', title: 'NTIA Table', url: 'https://example', edition: '2025', effective_date: null }],
  disagreements: [], sources: { stale: false, statuses: {} },
};
const out = view.render();
assert.ok(out.includes('<dialog class="overlay-backdrop allocation-modal"'), 'Details renders a native dialog');
assert.ok(out.includes('25.67 MHz–26.1 MHz'), 'modal title shows the band edges');
assert.ok(out.includes('<details class="allocation-fold">'), 'long footnotes start folded');
assert.ok(out.includes('International Broadcast Stations (73F)'), 'rule parts render');
assert.ok(out.includes('allocation-footnotes'), 'footnotes render as an id/text grid');

const events = [];
view.dispatchEvent = (ev) => { events.push(ev); return true; };
view._openChart();
assert.equal(events.length, 1);
assert.equal(events[0].type, 'open-allocation-chart');
assert.equal(events[0].detail.hz, 26010000);
assert.ok(events[0].bubbles && events[0].composed, 'event must bubble out of the light DOM to window');
assert.equal(view._expanded, false, 'opening the chart closes the modal');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
