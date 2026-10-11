"""CW phrases should read as a stream without joining unrelated receptions."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "cw-transcript.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_cw_transcript_groups_and_bounds():
    script = f"""
import assert from 'node:assert/strict';
import {{ cwTranscripts }} from {json.dumps(MODULE.as_uri())};
const ev = (id, ts, text, extra = {{}}) => ({{
  id, ts, text, decoder: 'cw', frequency_hz: 7000000,
  source_id: 'live:sdrconnect', generation: 1, ...extra,
}});
const events = [
  ev('c', 12, ' WORLD '), ev('a', 10, '  HELLO  '),
  ev('b', 11, '   CQ   DE  '),
  ev('other-decoder', 13, 'NO', {{decoder: 'rtty'}}),
  ev('other-frequency', 13, 'DIFFERENT', {{frequency_hz: 7100000}}),
  ev('other-generation', 14, 'NEW', {{generation: 2}}),
  ev('replay', 15, 'REPLAY', {{source_id: 'replay:file', analysis_run_id: 'r1'}}),
  ev('later-session', 400, 'AGAIN'),
];
const groups = cwTranscripts(events);
assert.equal(groups.length, 5);
assert.equal(groups[0].text, 'HELLO CQ DE WORLD');
assert.equal(groups.at(-1).text, 'AGAIN');
assert.deepEqual(cwTranscripts(events, {{since: 11, frequencyHz: 7000000}})
  .map(g => g.text), ['CQ DE WORLD', 'NEW', 'REPLAY', 'AGAIN']);
assert.deepEqual(cwTranscripts([ev('empty', 1, '  '), ev('x', 2, 'X')])
  .map(g => g.text), ['X']);
const many = Array.from({{length: 600}}, (_, i) => ev(String(i), i, 'X'.repeat(100)));
const bounded = cwTranscripts(many);
assert.equal(bounded.reduce((n, g) => n + g.text.length, 0) <= 12000, true);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
