# ruff: noqa: E501 -- embedded JavaScript source lines
"""band-scan-report-data.js's band_context derives from an observation's
`allocation` (docs/allocations.md "Backend integration" compact summary --
every service, primary first, secondary marked) when present, falling back to
the older `bandplan` name only for observations/reports that predate
allocations."""
import json
import subprocess
from pathlib import Path

from tests.ui._node_env import node_env


def test_band_context_prefers_allocation_services_over_bandplan_name():
    module_path = Path(__file__).resolve().parents[2] / "msdr/ui/band-scan-report-data.js"
    module = json.dumps(module_path.as_uri())
    script = f"""
import assert from 'node:assert/strict';
import {{ buildBandScanReport }} from {module};
const detail = {{config:{{start_hz:5000000,end_hz:7500000,span_hz:2500000,
  centers_hz:[5250000,7000000]}},payload:{{spans:[],results:[
  {{frequency_hz:5264000,observations:[
    {{observed_at:10,snr_db:9,allocation:{{lo_hz:5250000,hi_hz:5275000,jurisdiction:'US',status:'ok',
      services:[{{name:'FIXED',priority:'primary'}},
        {{name:'MOBILE except aeronautical mobile',priority:'primary'}},
        {{name:'Radiolocation',priority:'secondary'}}]}}}},
  ]}},
  {{frequency_hz:7000000,observations:[
    {{observed_at:5,snr_db:3,bandplan:{{name:'Old saved hint'}}}},
  ]}},
  {{frequency_hz:7100000,observations:[
    {{observed_at:5,snr_db:2}},
  ]}},
]}}}};
const report = buildBandScanReport(detail);
assert.equal(report.frequencies[0].band_context,
  'FIXED, MOBILE except aeronautical mobile · Radiolocation (secondary)');
assert.ok(report.frequencies[0].evidence.includes('Band allocation'));
assert.equal(report.frequencies[1].band_context, 'Old saved hint',
  'falls back to the bandplan name when allocation is absent');
assert.equal(report.frequencies[2].band_context, null);
assert.ok(!report.frequencies[2].evidence.includes('Band allocation'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=15, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
