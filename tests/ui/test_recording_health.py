# ruff: noqa: E501 -- embedded JavaScript source lines
"""recordingHealth/formatUnknown (msdr/ui/recording-health.js): the shared
damaged-recording rule used by both recordings-view.js and review-view.js.
No JS test runner in this repo -- run it for real via
`node --input-type=module`, skipping if node isn't installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

RECORDING_HEALTH = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "recording-health.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_SCRIPT = """
import {{ recordingHealth, formatUnknown }} from {module_url};
import assert from 'node:assert/strict';

const base = {{
  path: '/rec/x', name: 'x', center_hz: 100000000, sample_rate: 2000000,
  duration_s: 12.5, size_bytes: 123, gaps: 0, created: 1700000000,
  capture_start: 1700000000, error: null,
}};

// Backend-flagged damage (recorder.py's _recording_error_row) wins over
// everything else and surfaces its own reason verbatim.
{{
  const row = {{ ...base, error: 'JSONDecodeError: bad meta', sample_rate: null, duration_s: null }};
  const h = recordingHealth(row);
  assert.equal(h.damaged, true);
  assert.equal(h.reason, 'JSONDecodeError: bad meta');
  assert.equal(h.canReplay, false);
  assert.equal(h.canAnalyze, false);
}}

// No backend error, but a field replay needs is missing -- still damaged.
{{
  const h = recordingHealth({{ ...base, sample_rate: null }});
  assert.equal(h.damaged, true);
  assert.equal(h.reason, 'sample rate unknown');
}}
{{
  const h = recordingHealth({{ ...base, duration_s: null }});
  assert.equal(h.damaged, true);
  assert.equal(h.reason, 'duration unknown');
}}
{{
  const h = recordingHealth({{ ...base, path: null }});
  assert.equal(h.damaged, true);
  assert.equal(h.reason, 'no data path');
}}

// A fully healthy row is never damaged.
{{
  const h = recordingHealth(base);
  assert.equal(h.damaged, false);
  assert.equal(h.reason, null);
  assert.equal(h.canReplay, true);
  assert.equal(h.canAnalyze, true);
}}

// formatUnknown: null/undefined never gets coerced into the formatter's own
// zero-ish default -- but a real 0 is not "unknown".
assert.equal(formatUnknown(null, (s) => `${{s}}s`), 'unknown');
assert.equal(formatUnknown(undefined, (s) => `${{s}}s`), 'unknown');
assert.equal(formatUnknown(0, (s) => `${{s}}s`), '0s');
assert.equal(formatUnknown(12.5, (s) => `${{s}}s`), '12.5s');
"""


def test_recording_health_rules():
    module_url = __import__("json").dumps(RECORDING_HEALTH.as_uri())
    script = _SCRIPT.format(module_url=module_url)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=20, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
