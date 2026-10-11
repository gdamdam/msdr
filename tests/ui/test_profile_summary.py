"""profile-summary.js: only stored (non-null) settings appear, false != unset."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/profile-summary.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_profile_summary_lists_only_non_null_settings():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^export function /gm, 'function ');
const { profileSummary } = vm.runInNewContext(src + '\n({ profileSummary })', {});
assert.equal(profileSummary({ antenna: 'A', lna_state: 0, audio_agc_enabled: true }), 'Antenna A · LNA 0 · AGC on');
assert.equal(profileSummary({ antenna: 'Antenna B', lna_state: 3 }), 'Antenna B · LNA 3');
assert.equal(profileSummary({ squelch_enabled: false, sample_rate_hz: 2000000 }), '2 MS/s · Squelch off');
assert.equal(profileSummary({ antenna: null }), '');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
