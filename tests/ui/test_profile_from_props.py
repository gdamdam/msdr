"""profile-summary.js profileFromProps: wire prop names -> ProfileSettings fields."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/profile-summary.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_profile_from_props_maps_wire_names_and_keeps_missing_null():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^export function /gm, 'function ');
const { profileFromProps } = vm.runInNewContext(src + '\n({ profileFromProps })', {});
const out = profileFromProps({
  active_antenna: 'Antenna B', lna_state: 0, device_sample_rate: 2000000,
  agc_enable: false, agc_threshold: -60, noise_reduction_enable: true,
  noise_reduction_strength: 3, squelch_enable: true, squelch_threshold: -80,
  device_vfo_frequency: 7100000,
});
assert.equal(out.antenna, 'Antenna B');
assert.equal(out.lna_state, 0);
assert.equal(out.sample_rate_hz, 2000000);
assert.equal(out.audio_agc_enabled, false);
assert.equal(out.audio_agc_threshold, -60);
assert.equal(out.noise_reduction_enabled, true);
assert.equal(out.noise_reduction_strength, 3);
assert.equal(out.squelch_enabled, true);
assert.equal(out.squelch_threshold, -80);
assert.equal('vfo_hz' in out, false);
const empty = profileFromProps(undefined);
assert.ok(Object.values(empty).every((v) => v === null));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
