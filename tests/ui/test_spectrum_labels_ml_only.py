"""_spectrumLabels() restores RFML-only proposals (no tracked spectrum peak)
and the peak_threshold filter, without duplicating an already-marked signal."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_spectrum_labels_include_ml_only_and_apply_threshold():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src=fs.readFileSync(process.argv[1],'utf8').replace(/^import .*;\n/gm,'')
  .replace('export class SurveyView','class SurveyView');
const View=vm.runInNewContext(src+'\nSurveyView',{
  LitElement:class{},customElements:{define(){}},Date:{now:()=>100000},
  api:{getSurveyBandScan:()=>new Promise(()=>{})}
});
const view=Object.create(View.prototype);
view.wfSettings={marker_hold_s:15,peak_threshold_db:10};
view.status={active:true,labels:[],spectrum_markers:[
  {id:'strong',active_now:true,last_seen:1,snr_db:20,frequency_hz:1000,width_hz:100},
  {id:'weak',active_now:true,last_seen:1,snr_db:5,frequency_hz:5000,width_hz:100},
],ml_proposals:[
  // Overlaps the 'strong' marker's frequency span -- already has a marker,
  // must not be duplicated.
  {id:'ml-match',ml_only:true,frequency_hz:1000,low_hz:950,high_hz:1050,rfml_detection:{}},
  // No tracked spectrum peak anywhere near it -- must be restored.
  {id:'ml-free',ml_only:true,frequency_hz:9000,low_hz:8950,high_hz:9050,rfml_detection:{}},
]};
const ids=view._spectrumLabels().map(l=>l.id).join(',');
assert.equal(ids,'strong,ml-free',
  'weak marker below peak_threshold_db must drop, '
  + 'ml-match must dedupe against strong, ml-free must appear');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
