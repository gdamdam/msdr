"""Survey table membership stays readable across live status churn."""

import shutil
import subprocess
from pathlib import Path

import pytest

SURVEY_VIEW_JS = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_session_rows_append_retain_quiet_and_sort_only_on_request():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export class SurveyView', 'class SurveyView');
const SurveyView = vm.runInNewContext(source + '\nSurveyView', {
  LitElement: class {}, customElements: { define() {} }, Date,
  matchesEvidenceFilter: () => true,
  html: (parts, ...values) => ({parts, values}),
});
const view = Object.create(SurveyView.prototype);
Object.assign(view, {
  _rowOrder: [], _ghosts: new Map(), _prevLabels: new Map(), _newIds: new Map(),
  _viewStatus: null, _viewSpan: null, _viewVfoHz: undefined, _orderSig: null,
  _awaitingFirstRows: true, _resortSeq: 0, _sortKey: 'snr_db', _sortOrder: 'desc',
  _activityFilter: 'all', _evidenceFilter: 'all', _snrMin: '5', _snrMax: '',
});
view._filterLabels = SurveyView.prototype._filterLabels;
view._sortLabels = SurveyView.prototype._sortLabels;
view._comparator = SurveyView.prototype._comparator;
const label = (id, hz, snr) => ({ id, frequency_hz: hz, snr_db: snr,
  active_now: true, activity: { kind: 'steady' } });
const a = label('a', 100100000, 20), b = label('b', 100200000, 10),
  c = label('c', 100300000, 8), d = label('d', 100400000, 50);
const status = labels => ({ labels, generation: 1,
  span: { center_hz: 100000000, sample_rate_hz: 1000000 }, resolution_hz: 1000 });
view.status = status([a, b, c]);
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'a,b,c');

view.status = status([{...a, snr_db: 30}, {...b, snr_db: 1}, d]);
let result = view._tableView();
assert.equal(result.rows.map(l => l.id).join(','), 'a,b,c,d');
assert.equal(result.rows[1].snr_db, 1); // live values, frozen membership
assert.ok(result.rows[2]._goneAt);
assert.equal(result.pending, 0);
assert.equal(result.gone, 1);

view._resortSeq++;
result = view._tableView();
assert.equal(result.rows.map(l => l.id).join(','), 'd,a,c');
assert.equal(result.pending, 0);
assert.equal(result.gone, 1);

// A changed filter is also an explicit request to rebuild from current rows.
view._snrMin = '';
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'd,a,c,b');

// A merge into an already accepted row must not consume the other slot.
Object.assign(view, {
  _rowOrder: [], _ghosts: new Map(), _prevLabels: new Map(), _newIds: new Map(),
  _viewStatus: null, _viewSpan: null, _orderSig: null, _awaitingFirstRows: true,
  _resortSeq: 0, _snrMin: '',
});
const x = {...label('x', 100100000, 20), width_hz: 10000};
const y = {...label('y', 100101000, 10), width_hz: 10000};
view.status = status([x, y]);
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'x,y');
view.status = status([y]);
result = view._tableView();
assert.equal(result.rows.map(l => l.id).join(','), 'x,y');
assert.ok(result.rows[0]._goneAt);
assert.equal(result.gone, 1);
view.status = status([y]);
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'x,y');
view._resortSeq++;
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'x,y');
// While hovered or keyboard-focused, even cell values and actions retain
// their existing snapshot. Leaving both resumes without receiver changes.
let renders = 0;
view._renderLiveTable = () => ({ revision: ++renders });
view._renderTable();
const original = view._tableMarkup;
view._tablePointer = true;
view.status = status([d]);
view._renderTable();
assert.equal(view._tableMarkup, original);
view._tableFocus = true;
view._tablePointer = false;
view._renderTable();
assert.equal(view._tableMarkup, original);
view._tableFocus = false;
view._renderTable();
assert.notEqual(view._tableMarkup, original);
view._tablePointer = true;
view._sortOrder = 'asc';
const previous = view._tableMarkup;
view._renderTable();
assert.notEqual(view._tableMarkup, previous); // explicit sort still works

// A retained row can fall below the saved SNR threshold while all rows
// before it still match the sorted list. This used to dereference
// sorted[i].id past the end and break the entire table render.
Object.assign(view, {
  _rowOrder: [], _ghosts: new Map(), _prevLabels: new Map(), _newIds: new Map(),
  _viewStatus: null, _viewSpan: null, _orderSig: null, _awaitingFirstRows: true,
  _sortKey: 'frequency_hz', _sortOrder: 'asc', _snrMin: '5', _snrMax: '',
});
view.status = status([a, b, c]);
assert.equal(view._tableView().rows.map(l => l.id).join(','), 'a,b,c');
view.status = status([a, b, {...c, snr_db: 1}]);
result = view._tableView();
assert.equal(result.rows.map(l => l.id).join(','), 'a,b,c');
assert.equal(result.stale, true);
view.status = status([{...a, snr_db: 1}, {...b, snr_db: 1}, {...c, snr_db: 1}]);
result = view._tableView();
assert.equal(result.rows.map(l => l.id).join(','), 'a,b,c');
assert.equal(result.stale, true);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SURVEY_VIEW_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
