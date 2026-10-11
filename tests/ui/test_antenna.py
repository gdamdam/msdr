"""Antenna select helpers exercised in Node, matching the UI's ES module
(shared by instrument-panel.js's receiver ANT select and drawer.js's saved-
signal Antenna field)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

HELPER_JS = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "antenna.js"
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


def test_antenna_options_parses_csv_and_lets_callers_choose_their_own_fallback():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ antennaOptions }} from {module};
import assert from 'node:assert/strict';

assert.deepEqual(
  antennaOptions("Antenna A,Antenna B,Antenna C"),
  [
    {{ value: "Antenna A", letter: "A" }},
    {{ value: "Antenna B", letter: "B" }},
    {{ value: "Antenna C", letter: "C" }},
  ],
);
// Whitespace around entries is trimmed.
assert.deepEqual(antennaOptions(" Antenna A , Antenna B "), [
  {{ value: "Antenna A", letter: "A" }},
  {{ value: "Antenna B", letter: "B" }},
]);
// No fallback baked in -- empty/missing input is just [].
assert.deepEqual(antennaOptions(""), []);
assert.deepEqual(antennaOptions(null), []);
assert.deepEqual(antennaOptions(undefined), []);
// A name that doesn't match "Antenna X" still gets a usable letter (itself).
assert.deepEqual(antennaOptions("Whip"), [{{ value: "Whip", letter: "Whip" }}]);
""")


def test_antenna_display_shows_pending_marker_never_as_confirmed():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ antennaDisplay }} from {module};
import assert from 'node:assert/strict';

// Confirmed only, no pending request in flight.
assert.equal(antennaDisplay("Antenna B", null), "B");
assert.equal(antennaDisplay(null, null), "--");

// Pending shows the *requested* value with a marker, regardless of what's
// still confirmed -- the confirmed value is never upgraded to look settled
// just because a request is in flight.
assert.equal(antennaDisplay("Antenna B", "Antenna A"), "A\\u2026");
assert.equal(antennaDisplay(null, "Antenna C"), "C\\u2026");

// Once the request clears (pending back to null/undefined), it reports
// whatever is actually confirmed -- including a revert after a failure.
assert.equal(antennaDisplay("Antenna B", undefined), "B");
""")
