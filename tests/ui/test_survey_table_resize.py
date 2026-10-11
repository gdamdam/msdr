# ruff: noqa: E501 -- embedded JavaScript source lines
"""survey-view.js: the Signal measurements table is capped in height and
resized with the ◢ handle; the height is clamped and remembered per browser.
Same vm.runInNewContext harness as test_survey_view_clear_filters.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SURVEY = Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_table_height_is_clamped_and_persisted():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const stored = {};
const ctx = {
  LitElement: class {}, html: () => '', css: () => '', customElements: { define() {} }, console,
  localStorage: { getItem: (k) => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; } },
  window: { addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: false }) },
  document: { addEventListener() {}, removeEventListener() {} },
  requestAnimationFrame: () => 0, cancelAnimationFrame() {}, setTimeout, clearTimeout, performance,
  ResizeObserver: class { observe() {} disconnect() {} }, navigator: {}, CustomEvent: class {},
};
vm.runInNewContext(src + '\nglobalThis.SurveyView = SurveyView; globalThis.clampTableHeight = clampTableHeight;', ctx);
assert.equal(ctx.clampTableHeight(50), 160, 'floor');
assert.equal(ctx.clampTableHeight(5000), 1200, 'ceiling');
assert.equal(ctx.clampTableHeight('abc'), 360, 'garbage -> default');
assert.equal(ctx.clampTableHeight(420.6), 421);

const view = Object.create(ctx.SurveyView.prototype);
view._resizeTable(500);
assert.equal(view._tableHeight, 500);
assert.equal(stored['msdr.survey.tableHeight'], '500', 'remembered per browser');
// Drag: 500 px start, pointer moves 80 px down -> 580.
view._tableDrag = { y: 100, height: 500 };
view._moveTableResize({ clientY: 180 });
assert.equal(view._tableHeight, 580);
let prevented = 0;
view._keyTableResize({ key: 'ArrowUp', preventDefault() { prevented += 1; } });
assert.equal(view._tableHeight, 560);
view._keyTableResize({ key: 'Home', preventDefault() { prevented += 1; } });
assert.equal(view._tableHeight, 360, 'Home resets');
view._keyTableResize({ key: 'x', preventDefault() { prevented += 1; } });
assert.equal(prevented, 2, 'unrelated keys pass through');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SURVEY)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
