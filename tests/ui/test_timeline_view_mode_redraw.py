# ruff: noqa: E501 -- embedded JavaScript source lines
"""timeline-view.js: grid redraws when returning from Compare -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/timeline-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nTimelineView', {
  LitElement: class {}, html: (strings, ...values) => ({ strings: [...strings], values }), svg: (strings, ...values) => ({ strings: [...strings], values }),
  customElements: { define() {} }, console, CustomEvent: class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } },
  ...extra,
});
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_redraw_when_switching_back_to_timeline():
    _run(r"""
const C = load({ bucketCount() {}, indexByTime() {}, missingCoverage() {}, xForIndex() {}, autoBucketS() {} });
const v = Object.create(C.prototype);
let draws = 0;
Object.assign(v, { _mode: 'timeline', _drawGrid() { draws += 1; } });
v.updated(new Map([['_mode', 'compare']]));
assert.equal(draws, 1, 'back in timeline mode: redraw');
v._mode = 'compare';
v.updated(new Map([['_mode', 'timeline']]));
assert.equal(draws, 1, 'entering compare does not draw the grid');
v.updated(new Map([['_other', 1]]));
assert.equal(draws, 1);
v.updated(new Map([['_devices', []]]));
assert.equal(draws, 2, 'devices change still redraws');
""")
