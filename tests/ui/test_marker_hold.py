"""Marker hold follows RF timestamps, independently of speech and redraws."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_marker_hold_expiry_and_reset():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src=fs.readFileSync(process.argv[1],'utf8').replace(/^import .*;\n/gm,'')
  .replace('export class SurveyView','class SurveyView');
let now=100000;
let resolveRefresh;
const View=vm.runInNewContext(src+'\nSurveyView',{
  LitElement:class{},customElements:{define(){}},Date:{now:()=>now},
  api:{getSurveyBandScan:()=>new Promise(resolve=>{resolveRefresh=resolve;})}
});
const view=Object.create(View.prototype);
view.wfSettings={marker_hold_s:15};
view.status={labels:[],spectrum_markers:[
  {id:'live',active_now:true,last_seen:1},
  {id:'held',active_now:false,last_seen:95},
  {id:'expired',active_now:false,last_seen:80},
  {id:'unknown',active_now:false}
]};
let labels=view._spectrumLabels();
assert.equal(labels.map(l=>l.id).join(','),'live,held');
assert.equal(labels[0]._markerOpacity,1);
assert.equal(labels[1]._markerRetained,true);
assert.equal(labels[1].active_now,false);
const opacity=labels[1]._markerOpacity;
now=105000;
assert.ok(view._spectrumLabels()[1]._markerOpacity<opacity);
now=110000;
assert.equal(view._spectrumLabels().map(l=>l.id).join(','),'live');
view.wfSettings.marker_hold_s=0;now=100000;
assert.equal(view._spectrumLabels().map(l=>l.id).join(','),'live');
view.wfSettings.marker_hold_s=60;
assert.equal(view._spectrumLabels().length,3);
view.status={generation:2,labels:[],spectrum_markers:[]};
assert.equal(view._spectrumLabels().length,0,'retune must not resurrect old markers');
const pending=view._refreshBandScan();
await view._bandScanAction(async()=>({state:'running'}));
resolveRefresh({state:'idle'});
await pending;
assert.equal(view._bandScan.state,'running','old poll must not undo Start');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
