"""Exercise Survey's tuned-row retention with the actual component methods."""

import shutil
import subprocess
from pathlib import Path

import pytest

SURVEY_VIEW_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_tuned_row_survives_refresh_and_moves_when_vfo_changes():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class SurveyView', 'class SurveyView');
const SurveyView = vm.runInNewContext(source + '\nSurveyView', {
  LitElement: class {}, customElements: { define() {} }, Date,
});
let now = 1_000_000;
Date.now = () => now;
const view = Object.create(SurveyView.prototype);
Object.assign(view, {
  frame: { sample_rate_hz: 512_000, bins: new Array(512) },
  _rowOrder: [], _ghosts: new Map(), _prevLabels: new Map(), _newIds: new Map(),
  _viewStatus: null, _viewSpan: null, _orderSig: null, _resortSeq: 0,
  _bsSpanInitialized: true, // band-scan draft wiring is outside this tuned-row test
  _sortKey: 'frequency_hz', _sortOrder: 'asc', _activityFilter: 'all',
  _evidenceFilter: 'all', _snrMin: '', _snrMax: '', _selectedLabelId: 'a',
});
view._filterLabels = labels => labels;
view._sortLabels = labels => labels;
view._comparator = () => (a, b) => a.frequency_hz - b.frequency_hz;
const a = { id: 'a', frequency_hz: 100_000_000, width_hz: 2_000 };
const b = { id: 'b', frequency_hz: 101_000_000, width_hz: 2_000 };
const status = (labels, vfo_hz) => ({ labels, span: { center_hz: 100_500_000,
  sample_rate_hz: 512_000, vfo_hz }, generation: 1 });

view.status = status([a], a.frequency_hz);
assert.equal(view._highlightedLabelId(view._tableView().rows), 'a');
const first = view.status;
view.status = status([], a.frequency_hz);
view.willUpdate(new Map([['status', first]]));
view._tableView(); // first empty refresh creates the quiet row
now += 61_000;
view.status = status([], a.frequency_hz);
assert.equal(view._tableView().rows.map(row => row.id).join(','), 'a');
assert.equal(view._highlightedLabelId(view._tableView().rows), 'a');

const replacement = { ...a, id: 'a2' };
const beforeRetrack = view.status;
view.status = status([replacement], a.frequency_hz);
view.willUpdate(new Map([['status', beforeRetrack]]));
assert.equal(view._tableView().rows.map(row => row.id).join(','), 'a2');
assert.equal(view._highlightedLabelId(view._tableView().rows), 'a2');

const beforeMove = view.status;
view.status = status([b], b.frequency_hz);
view.willUpdate(new Map([['status', beforeMove]]));
assert.equal(view._selectedLabelId, null);
const movedRows = view._tableView().rows;
assert.equal(view._highlightedLabelId(movedRows), 'b');
assert.equal(movedRows.filter(row => row.id === 'a').length, 0);

const beforeClear = view.status;
view.status = status([b], 102_000_000);
view.willUpdate(new Map([['status', beforeClear]]));
assert.equal(view._highlightedLabelId(view._tableView().rows), null);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SURVEY_VIEW_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
