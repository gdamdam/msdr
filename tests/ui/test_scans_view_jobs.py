# ruff: noqa: E501 -- embedded JavaScript source lines
"""scans-view.js: the first job poll seeds state without firing completions. Runs the component source in a node vm with stubs, like
test_decode_view_live_hold.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components/scans-view.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const strip = (src) => src.replace(/^import[\s\S]*?;\n/gm, '').replace(/^export /gm, '');
const src = strip(fs.readFileSync(process.argv[1], 'utf8'));
const tick = () => new Promise((r) => setTimeout(r, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const load = (cls, extra = {}) => vm.runInNewContext(src + `\n${cls}`, {
  LitElement: class { dispatchEvent() {} }, html: () => '', css: () => '', svg: () => '', customElements: { define() {} },
  console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, Number, ...extra,
});
"""


def run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SRC)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


def test_first_poll_does_not_fire_completion_for_already_done_jobs():
    run(r"""
let jobs = [{ job_id: 'a', status: 'done', scan_id: 7 }];
const V = load('ScansView', { scanJobs: async () => ({ jobs }), CustomEvent: class { constructor(type) { this.type = type; } } });
const v = Object.create(V.prototype);
const events = [];
Object.assign(v, { _jobs: [], _jobsSeeded: false, _pendingExpand: null, dispatchEvent: (e) => events.push(e.type) });
await v._pollJobs();
assert.deepEqual(events, []);
assert.equal(v._pendingExpand, null);
// A job that flips to done after being seen running does fire.
jobs = [{ job_id: 'b', status: 'running' }];
await v._pollJobs();
jobs = [{ job_id: 'b', status: 'done', scan_id: 8 }];
await v._pollJobs();
assert.deepEqual(events, ['scan-completed']);
assert.equal(v._pendingExpand, 8);
clearTimeout(v._jobsTimer);
""")
