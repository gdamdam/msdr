"""CW output takes display precedence without promoting catalog identities."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_cw_report_and_chip_preserve_evidence_separation():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"
    saved_frequency = Path(__file__).resolve().parents[2] / "msdr/ui/saved-frequency.js"
    # The hover card now goes through signalActivity() (0.52.2 carrier /
    # speech separation), so that module is inlined like saved-frequency.js.
    signal_activity = Path(__file__).resolve().parents[2] / "msdr/ui/signal-activity.js"
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync(process.argv[1], 'utf8')
 .replace(/^import .*;\n/gm, '').replace('export class SurveyView', 'class SurveyView');
const savedFrequency = fs.readFileSync(process.argv[2], 'utf8').replace(/^export /gm, '');
const signalActivity = fs.readFileSync(process.argv[3], 'utf8').replace(/^export /gm, '');
const html = (parts, ...args) => parts.map((p,i) => p + (args[i] ?? '')).join('');
// activityCell() went away in 0.52.2 when carrier presence and speech
// activity were separated; the CW word now comes from hasCwOutput(), which
// every cell/chip/report site consults first.
const {SurveyView, candidateName, hasCwOutput} = vm.runInNewContext(
 [source, savedFrequency, signalActivity, '({SurveyView, candidateName, hasCwOutput})'].join('\n'),
 {LitElement:class {}, customElements:{define(){}}, html, Date, Map});
const label = {frequency_hz:432304000, snr_db:20,
 confirmed:{decoder:'cw', sample:'K6ER CM87SU'},
 family:{name:'speech-like NFM audio',next_action:'listen NFM'},
 voice:{kind:'voice'}, bandplan:{name:'70 cm (420–450 MHz)'},
 catalog:[{name:'NORAD 61049'}]};
assert.equal(candidateName(label), 'CW · decoder output');
assert.equal(hasCwOutput(label), true);
const failed = {...label, confirmed:{decoder:'cw', integrity:'failed'}};
assert.equal(hasCwOutput(failed), false);
const view = Object.create(SurveyView.prototype);
view._hoverHz = label.frequency_hz;
view._hoverNearest = {withinRadius:true,label};
view._renderEvidenceBadges = () => '';
const chip = view._chipText(label);
assert.ok(chip.includes('CW'));
assert.ok(!chip.includes('speech'));
const report = view._renderHoverCard();
assert.ok(report.indexOf('BAND PLAN') < report.indexOf('432.304'));
assert.ok(report.includes('color:#f2c94c'));
assert.ok(report.includes('allocation only'));
assert.ok(report.includes('CW · decoder output'));
assert.ok(!report.includes('NORAD 61049'));
assert.ok(report.includes('Shift-click for details'));
assert.ok(!report.includes('speech-like NFM'));
assert.ok(report.includes('K6ER CM87SU'));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source), str(saved_frequency),
         str(signal_activity)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
