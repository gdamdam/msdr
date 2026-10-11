"""Inbox samples badge reads the summary's true observation_count instead of
the length of the (now representative-only) observations list."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_inbox_samples_badge_uses_observation_count():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/band-scan-inbox.js"
    format_module = Path(__file__).resolve().parents[2] / "msdr/ui/format.js"
    script = f"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {{ mhz }} from {str(format_module.as_uri())!r};

const sourceText = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\\n/gm, '').replace('export class BandScanInbox', 'class BandScanInbox');

// Mimics real lit: html() returns a template-result object (no custom
// toString); flattening it below is enough to inspect the rendered text.
const html = (strings, ...values) => ({{_$tr: true, strings, values}});
function renderTemplate(node) {{
  if (node == null || node === false || node === '') return '';
  if (Array.isArray(node)) return node.map(renderTemplate).join('');
  if (typeof node === 'object' && node._$tr) {{
    return node.strings.reduce((out, s, i) =>
      out + (i ? renderTemplate(node.values[i - 1]) : '') + s, '');
  }}
  return String(node);
}}

const {{BandScanInbox}} = vm.runInNewContext(sourceText + '\\n({{BandScanInbox}})', {{
  LitElement: class {{}}, customElements: {{define() {{}}}},
  html, api: {{}}, mhz,
}});

const view = Object.create(BandScanInbox.prototype);
view.error = '';
view.scans = [{{id: 's1', state: 'done', result_count: 1, covered_count: 1, total_spans: 1,
  span_count: 1, started_at: 0, config: {{start_hz: 100000000, end_hz: 101000000}}}}];
// The summary endpoint trims "observations" to a few representatives but
// still reports the true total separately as observation_count.
view.details = {{s1: {{payload: {{results: [{{frequency_hz: 100500000, observation_count: 42,
  span_centers_hz: [1, 2, 3], observations: [{{observed_at: 1, snr_db: 2, span_center_hz: 1e8}}]}}],
  spans: []}}}}}};

const out = renderTemplate(view.render());
assert.match(out, /42 samples/, `expected the true observation_count, got: ${{out}}`);
assert.ok(!out.includes('1 samples'), 'must not fall back to the truncated representatives length');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
