"""Report output retains repeat observations and honest span coverage."""
import json
import subprocess
from pathlib import Path


def test_band_scan_report_summary_timeline_coverage_and_safe_csv():
    module_path = Path(__file__).resolve().parents[2] / "msdr/ui/band-scan-report-data.js"
    module = json.dumps(module_path.as_uri())
    script = f"""
import assert from 'node:assert/strict';
import {{ buildBandScanReport, bandScanFrequenciesCsv, signalKind }} from {module};
const detail = {{config:{{start_hz:100000000,end_hz:106000000,span_hz:2000000,
  centers_hz:[101000000,103000000,105000000]}},payload:{{
  spans:[{{center_hz:101000000,state:'covered'}},
    {{center_hz:103000000,state:'uncovered',reason:'no fresh frames'}}],
  results:[{{frequency_hz:100500000,span_centers_hz:[101000000],observations:[
    {{observed_at:10,snr_db:9,catalog:{{name:'Reference'}}}},
    {{observed_at:20,snr_db:18,rds:{{ps:'Station'}},activity:{{kind:'bursty'}},
      catalog:[{{name:'Marine channel'}}],bandplan:{{name:'Marine VHF'}},width_hz:12000}},
  ]}},{{frequency_hz:103500000,observations:[{{observed_at:15,snr_db:6,family:{{name:'=formula'}}}}]}}],
}}}};
const report=buildBandScanReport(detail);
assert.equal(report.frequencies.length,2);
assert.equal(signalKind([{{confirmed:{{name:'AIS'}}}}]),'decoded');
assert.equal(signalKind([{{voice:{{kind:'voice'}}}}]),'voice');
assert.equal(signalKind([{{voice:{{kind:'data'}}}}]),'data');
assert.equal(signalKind([{{family:{{name:'CW-like'}}}}]),'clue');
assert.equal(signalKind([{{catalog:{{name:'AIS'}}}}]),'measured');
assert.equal(report.frequencies[0].signal_kind,'decoded');
assert.equal(report.frequencies[0].candidate,'Station');
assert.equal(report.frequencies[0].peak_snr_db,18);
assert.equal(report.frequencies[0].activity,'bursty');
assert.deepEqual(report.frequencies[0].catalog_matches,['Reference','Marine channel']);
assert.equal(report.frequencies[0].band_context,'Marine VHF');
assert.deepEqual(report.frequencies[0].evidence,
  ['Measured','RDS','Catalog reference','Band allocation']);
assert.deepEqual(report.timeline.map(row=>row.observed_at),[20,15,10]);
assert.deepEqual(report.coverage.map(row=>row.state),['covered','uncovered','unvisited']);
assert.equal(report.coverage[2].start_hz,104000000);
const compact=buildBandScanReport({{config:detail.config,payload:{{spans:detail.payload.spans,
  results:[{{frequency_hz:100500000,observation_count:918,span_count:3,snr_max_db:29.5,
    first_seen:2,last_seen:900,span_centers_hz:[101000000],observations:[
      {{observed_at:900,snr_db:29.5,recognized:{{name:'Representative clue'}}}}]}}]}}}});
assert.equal(compact.frequencies[0].count,918);
assert.equal(compact.frequencies[0].span_count,3);
assert.equal(compact.frequencies[0].peak_snr_db,29.5);
assert.equal(compact.frequencies[0].first_seen,2);
assert.equal(compact.frequencies[0].last_seen,900);
assert.equal(compact.timeline.length,1,
  'compact report retains only representatives until observation pages load');
const csv=bandScanFrequenciesCsv(report.frequencies);
assert.match(csv,/'=formula/);
assert.match(csv,/100500000/);
assert.match(csv,/band_allocation/);
assert.match(csv,/Marine VHF/);
assert.equal(csv.trim().split('\\r\\n').length,3);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
