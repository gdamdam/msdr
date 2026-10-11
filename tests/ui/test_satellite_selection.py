# ruff: noqa: E501 -- embedded JavaScript
"""Exercise same-satellite selection and stale asynchronous responses."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_selected_pass_and_stale_chart_responses():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '').replace(/^export (class|function|const) /gm, '$1 ');
const requests = [];
const SkyView = vm.runInNewContext(source + '\nSkyView', {
  LitElement: class {}, html: (...args) => args, svg: (...args) => args,
  customElements: { define() {} },
  api: { getSkyFor: (...args) => new Promise(resolve => requests.push({args, resolve})) },
});
const view = new SkyView();
view.selectedPass = {satellite: 'ISS', rise: '2026-10-03T17:50:12-07:00', set: '2026-10-03T17:56:57-07:00'};
const first = view._loadSky();
assert.equal(requests[0].args[2], view.selectedPass.rise);
assert.equal(requests[0].args[3], view.selectedPass.set);
view.selectedPass = {satellite: 'ISS', rise: '2026-10-04T10:32:00-07:00', set: '2026-10-04T10:38:00-07:00'};
const second = view._loadSky();
requests[1].resolve({track: {...view.selectedPass}});
await second;
requests[0].resolve({track: 'stale today'});
await first;
assert.equal(view._sky.track.rise, view.selectedPass.rise);
const mismatch = view._loadSky();
requests[2].resolve({track: {rise: '2026-10-03T09:41:00-07:00', set: '2026-10-03T09:50:00-07:00'}});
await mismatch;
assert.equal(view._sky.track, null);
assert.match(view._sky.error, /different pass/);
let reloads = 0;
view._loadSky = () => reloads++;
view.updated();
view.selectedPass = {...view.selectedPass, rise: '2026-10-05T10:32:00-07:00'};
view.updated();
assert.equal(reloads, 2);
"""
    path = Path(__file__).resolve().parents[2] / "msdr/ui/components/sky-view.js"
    subprocess.run(
        ["node", "--input-type=module", "-e", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
