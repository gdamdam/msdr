"""Saved editing preferences must not replace the primary Survey-first workspace."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_survey_first_is_stable_in_layout_edit_mode():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/app.js").read_text()
    methods = {}
    for name in ["_usesListenWorkbench", "_surveyFirst"]:
        body = source.split(f"  {name}() {{", 1)[1].split("\n  }", 1)[0]
        methods[name] = body
    script = """
const assert = require('node:assert/strict');
const methods = METHODS;
const state = {_showPrimarySpectrum: () => true};
for (const [name, body] of Object.entries(methods)) state[name] = new Function(body);
for (const editing of [false, true]) {
  state._layoutEditMode = editing;
  for (const workspace of ['listen', 'discover']) {
    state.workspace = workspace;
    state.discoverView = 'spectrum';
    assert.equal(state._surveyFirst(), true, workspace + ' editing=' + editing);
    assert.equal(state._usesListenWorkbench(), true);
  }
  for (const view of ['inbox', 'table']) {
    state.discoverView = view;
    assert.equal(state._surveyFirst(), false);
  }
  state.workspace = 'review';
  assert.equal(state._surveyFirst(), false);
}
state.workspace = 'listen';
state._showPrimarySpectrum = () => false;
assert.equal(state._surveyFirst(), false);
""".replace("METHODS", json.dumps(methods))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
