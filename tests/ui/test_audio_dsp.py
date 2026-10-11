"""Audio-DSP status helpers exercised in Node, matching the UI's ES module
(shared by instrument-panel.js's receiver Audio line -- AGC / Noise
reduction / Squelch toggles and their Levels popover fields)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

HELPER_JS = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "audio-dsp.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=node_env(),
    )
    assert result.returncode == 0, result.stderr


def test_dsp_value_matches_uses_tolerance_for_numbers_and_exact_match_otherwise():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ dspValueMatches }} from {module};
import assert from 'node:assert/strict';

// Numbers: within 0.51 counts as a match (SDRconnect echoes rounded values).
assert.equal(dspValueMatches(50, 50), true);
assert.equal(dspValueMatches(50, 50.5), true);
assert.equal(dspValueMatches(50, 50.6), false);
assert.equal(dspValueMatches(50, 49.5), true);
assert.equal(dspValueMatches(50, 48), false);

// Booleans (and anything else) need an exact match.
assert.equal(dspValueMatches(true, true), true);
assert.equal(dspValueMatches(true, false), false);
""")


def test_settle_pending_dsp_clears_only_matched_pending_entries():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ settlePendingDsp }} from {module};
import assert from 'node:assert/strict';

const statusMap = {{
  agc_enable: {{ status: "pending", value: true }},
  noise_reduction_strength: {{ status: "pending", value: 40 }},
  squelch_enable: {{ status: "unsupported", value: true, reason: "nope" }},
}};

// agc_enable's pending value is echoed back -> cleared. NR strength is not
// yet echoed (still 10) -> stays pending. squelch_enable isn't pending at
// all -> untouched.
const next = settlePendingDsp(statusMap, {{ agc_enable: true, noise_reduction_strength: 10 }});
assert.equal("agc_enable" in next, false);
assert.deepEqual(next.noise_reduction_strength, {{ status: "pending", value: 40 }});
assert.deepEqual(next.squelch_enable, statusMap.squelch_enable);

// Nothing resolved -> same reference back, so callers can skip a re-render.
const unchanged = settlePendingDsp(statusMap, {{ squelch_enable: true }});
assert.equal(unchanged, statusMap);
""")


def test_dsp_control_status_never_reports_confirmed_or_rejected():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ dspControlStatus }} from {module};
import assert from 'node:assert/strict';

assert.equal(dspControlStatus({{ status: "pending", value: true }}, false), "pending");
const unsupported = {{ status: "unsupported", reason: "nope" }};
assert.equal(dspControlStatus(unsupported, undefined), "unsupported");
assert.equal(dspControlStatus(null, undefined), "unavailable");
assert.equal(dspControlStatus(null, null), "unavailable");
// Normal operation (no local record, or a settled/cleared one) with a
// reported value is just "ready" -- never "confirmed".
assert.equal(dspControlStatus(null, true), "ready");
assert.equal(dspControlStatus(undefined, false), "ready");
""")


def test_dsp_effective_value_prefers_the_in_flight_request_while_pending():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ dspEffectiveValue }} from {module};
import assert from 'node:assert/strict';

assert.equal(dspEffectiveValue({{ status: "pending", value: true }}, false), true);
assert.equal(dspEffectiveValue(null, true), true);
assert.equal(dspEffectiveValue({{ status: "unsupported", value: true }}, false), false);
""")


def test_dsp_rejection_note_only_for_non_confirmed_non_unsupported_results():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ dspRejectionNote }} from {module};
import assert from 'node:assert/strict';

assert.equal(dspRejectionNote("AGC", {{ status: "confirmed" }}), null);
assert.equal(dspRejectionNote("AGC", {{ status: "unsupported", reason: "nope" }}), null);
assert.equal(dspRejectionNote("AGC", null), null);
assert.equal(
  dspRejectionNote("AGC", {{ status: "rejected", reason: "receiver busy" }}),
  "AGC: receiver busy",
);
assert.equal(dspRejectionNote("AGC", {{ status: "rejected" }}), "AGC wasn't accepted.");
""")
