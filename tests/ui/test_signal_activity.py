"""Current speech/carrier states require fresh timestamped DSP evidence."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / ".venv/lib/python3.13/site-packages/playwright/driver/node"


@pytest.mark.skipif(not NODE.exists() and shutil.which("node") is None, reason="node unavailable")
def test_signal_activity_freshness_speech_hold_and_filter_predicate():
    script = r"""
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const { signalActivity, isSpeechActivity } = await import(pathToFileURL(process.argv[1]));
const label = (kind, analyzed_at = 100, extra = {}) => ({
  active_now: true, analyzed_at, voice: kind ? { kind } : null, ...extra,
});

let result = signalActivity(label('voice'), 101);
assert.equal(result.state, 'speech');
assert.equal(result.speechDetected, true);
assert.equal(result.speechAt, 100);
assert.match(result.text, /tentative/);
assert.equal(isSpeechActivity(label('voice'), 102.9), true);

// The hold expires at the original DSP timestamp plus three seconds.
assert.equal(signalActivity(label('voice'), 103).state, 'carrier');
result = signalActivity(label('silence', 101), 102.9, 100);
assert.equal(result.state, 'speech'); // a fresh silence result does not erase the short hold
assert.equal(isSpeechActivity(label('silence', 101), 103, 100), false);
assert.equal(signalActivity(label('silence', 103), 103, 100).state, 'carrier_only');
assert.equal(signalActivity(label('silence'), 101).state, 'carrier_only');
assert.equal(signalActivity(label('data'), 101).state, 'carrier');
assert.match(signalActivity(label('data'), 101).text, /data detected/);

// Explicit inactivity wins. Stale, waiting, historical and missing DSP data
// stay unknown instead of being mislabeled as carrier only.
assert.equal(signalActivity({ ...label('voice'), active_now: false }, 101).state, 'no_signal');
assert.equal(signalActivity(label('silence', 69), 100).text, 'Carrier present · speech unknown');
assert.equal(signalActivity(label('silence', 100, {
  analysis_state: { state: 'historical_result' },
}), 100).text, 'Carrier present · speech unknown');
assert.equal(signalActivity(label('silence', 100, {
  analysis_state: { state: 'waiting' },
}), 100).text, 'Carrier present · speech unknown');
result = signalActivity(label(null, 100), 100);
assert.equal(result.state, 'carrier');
assert.equal(result.text, 'Carrier present · speech unknown');
assert.equal(signalActivity({ active_now: true, voice: { kind: 'silence' } }, 100).text,
  'Carrier present · speech unknown');
assert.equal(signalActivity(label('voice', 101), 100).text, 'Carrier present · speech unknown');
assert.equal(signalActivity(label('silence', 100), 100, 101).text,
  'Carrier present · speech unknown');
assert.equal(signalActivity({ voice: { kind: 'silence' } }, 100).state, 'unknown');
assert.equal(isSpeechActivity(label('silence', 69), 100, 99), false);
"""
    node = str(NODE if NODE.exists() else shutil.which("node"))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(ROOT / "msdr/ui/signal-activity.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
