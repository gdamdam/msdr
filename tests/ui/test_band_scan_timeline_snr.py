"""Timeline tab SNR cell renders the Mean sub-label instead of leaking a
TemplateResult into plain text ("[object Object]")."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_timeline_snr_cell_renders_mean_sublabel():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/band-scan-report.js"
    data_module = Path(__file__).resolve().parents[2] / "msdr/ui/band-scan-report-data.js"
    script = f"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {{ activityText, signalKind }} from {str(data_module.as_uri())!r};

const sourceText = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\\n/gm, '').replace('export class BandScanReport', 'class BandScanReport');

// Mimics real lit: html() returns a template-result object with no custom
// toString. Embedding one inside a plain (untagged) template literal
// stringifies to "[object Object]" -- the bug this test guards against.
const html = (strings, ...values) => ({{_$tr: true, strings, values}});
const svg = html;
function renderTemplate(node) {{
  if (node == null || node === false || node === '') return '';
  if (Array.isArray(node)) return node.map(renderTemplate).join('');
  if (typeof node === 'object' && node._$tr) {{
    return node.strings.reduce((out, s, i) =>
      out + (i ? renderTemplate(node.values[i - 1]) : '') + s, '');
  }}
  return String(node);
}}

const {{BandScanReport}} = vm.runInNewContext(sourceText + '\\n({{BandScanReport}})', {{
  LitElement: class {{}}, customElements: {{define() {{}}}},
  html, svg, api: {{}}, buildBandScanReport: () => null, bandScanFrequenciesCsv: () => '',
  activityText, signalKind,
}});

const view = Object.create(BandScanReport.prototype);
view.observationsLoading = false;
view.selectedFrequency = null;
view.page = 0;
const rows = [{{observed_at: 10, frequency_hz: 100500000, span_center_hz: 101000000,
  snr_min_db: 10, snr_max_db: 16, snr_avg_db: 13.5, activity: 'tone'}}];
view.observationPage = rows;
view.observationTotal = rows.length;

const out = renderTemplate(view._timeline(rows));
assert.ok(!out.includes('[object Object]'), `snr cell leaked a TemplateResult: ${{out}}`);
assert.match(out, /10\\.0.16\\.0 dB/);
assert.match(out, /Mean 13\\.5 dB/);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
