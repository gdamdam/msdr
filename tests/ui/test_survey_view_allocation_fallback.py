# ruff: noqa: E501 -- embedded JavaScript source lines
"""survey-view.js hover card: `label.allocation` (every service) wins, and a
label from a saved report that predates allocations still renders its
`bandplan` name -- the compatibility rule of docs/allocations.md."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

ROOT = Path(__file__).resolve().parents[2] / "msdr/ui"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_hover_card_prefers_allocation_and_falls_back_to_bandplan():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync(process.argv[1], 'utf8')
 .replace(/^import .*;\n/gm, '').replace('export class SurveyView', 'class SurveyView');
const extras = [process.argv[2], process.argv[3]].map(p => fs.readFileSync(p, 'utf8').replace(/^export /gm, '')).join('\n');
const html = (parts, ...args) => parts.map((p, i) => p + (args[i] ?? '')).join('');
const SurveyView = vm.runInNewContext(source + '\n' + extras + '\nSurveyView',
 {LitElement:class {}, customElements:{define(){}}, html, Date, Map});
const render = (label) => {
  const view = Object.create(SurveyView.prototype);
  view._hoverHz = label.frequency_hz;
  view._hoverNearest = {withinRadius:true, label};
  return view._renderHoverCard();
};
const withAllocation = render({frequency_hz:5264000, snr_db:10,
  bandplan:{name:'Old saved hint'},
  allocation:{lo_hz:5250000, hi_hz:5275000, jurisdiction:'US', status:'ok', services:[
    {name:'FIXED', priority:'primary', table:'federal'},
    {name:'MOBILE', priority:'primary', table:'federal'},
    {name:'Radiolocation', priority:'secondary', table:'non_federal'}]}});
assert.ok(withAllocation.includes('ALLOCATION · FIXED, MOBILE · Radiolocation (secondary)'));
assert.ok(!withAllocation.includes('BAND PLAN'));
const legacy = render({frequency_hz:7000000, snr_db:3, bandplan:{name:'Old saved hint'}});
assert.ok(legacy.includes('BAND PLAN · Old saved hint'));
const bare = render({frequency_hz:7100000, snr_db:2});
assert.ok(!bare.includes('BAND PLAN') && !bare.includes('ALLOCATION'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(ROOT / "components/survey-view.js"),
         str(ROOT / "saved-frequency.js"), str(ROOT / "signal-activity.js")],
        capture_output=True, text=True, timeout=20, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
