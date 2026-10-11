"""Held receiver step tuning stays responsive and serial via Node vm."""

import shutil
import subprocess
from pathlib import Path

import pytest

STEP_TUNER_JS = Path(__file__).resolve().parents[2] / "msdr/ui/step-tuner.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_steps_tune_immediately_accumulate_and_cancel_queued_targets():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace('export class StepTuner', 'class StepTuner');
const StepTuner = vm.runInNewContext(source + '\nStepTuner', { setTimeout, clearTimeout, Date });
const tuner = new StepTuner(30);
const calls = [];
const gates = [];
let active = 0;
let peakActive = 0;
const tune = hz => {
  calls.push(hz);
  active += 1;
  peakActive = Math.max(peakActive, active);
  return new Promise(resolve => gates.push(() => { active -= 1; resolve(); }));
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

tuner.step(1000, 10, tune);
await tick();
assert.deepEqual(calls, [1010], 'first step should be sent immediately');
tuner.step(1000, 10, tune);
tuner.step(1000, 10, tune);
await tick();
assert.deepEqual(calls, [1010], 'no overlapping requests while first tune is pending');
gates.shift()();
await wait(35);
await tick();
assert.deepEqual(calls, [1010, 1030], 'accumulated target should retain both held repeats');
assert.equal(peakActive, 1, 'tune requests must stay serialized');

tuner.step(1030, 10, tune);
const settling = tuner.cancel();
tuner.step(1030, 20, tune);
gates.shift()();
await settling;
await wait(35);
assert.deepEqual(calls, [1010, 1030, 1050],
  'cancel drops old work but a new target waits for the active request');
assert.equal(peakActive, 1, 'a restarted burst must not overlap the in-flight request');
gates.shift()();
await wait(70);
tuner.step(1000, 10, tune);
await tick();
assert.equal(calls.at(-1), 1060, 'idle gaps must not rebase on stale receiver state');
gates.shift()();
await tick();
tuner.cancel();
const failures = [];
const failed = hz => { failures.push(hz); return Promise.resolve(false); };
tuner.step(2000, 10, failed);
tuner.step(2000, 10, failed);
await wait(70);
assert.equal(failures.length, 1, 'failed tune must drop queued work');
assert.equal(tuner.targetHz, null);

"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(STEP_TUNER_JS)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
