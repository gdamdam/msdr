"""review-view.js must never paper over a damaged recording (GET
/api/recordings row with `error` set and null duration/gaps/sample_rate --
see msdr/api/services/recorder.py's _recording_error_row): the list row must
show the reason instead of a coerced 0:00 duration, and the REVIEW THIS
RECORDING button must be disabled with a title explaining why. A healthy
recording must keep behaving exactly as before."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

REVIEW_VIEW = Path(__file__).resolve().parents[2] / "msdr/ui/components/review-view.js"
RECORDING_HEALTH = Path(__file__).resolve().parents[2] / "msdr/ui/recording-health.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_damaged_row_shows_reason_and_disables_replay():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const [reviewPath, healthPath] = process.argv.slice(1);
const strip = (src) => src.replace(/^import[\s\S]*?;\n/gm, '').replace(/^export /gm, '');
const reviewSrc = strip(fs.readFileSync(reviewPath, 'utf8'));
const healthSrc = strip(fs.readFileSync(healthPath, 'utf8'));

const flat = (v) => Array.isArray(v) ? v.map(flat).join('') : (v == null ? '' : String(v));
const html = (strings, ...values) =>
  strings.map((s, i) => s + (i < values.length ? flat(values[i]) : '')).join('');

const ctx = {
  LitElement: class {},
  html,
  customElements: { define() {} },
  console,
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  // stubs for review-view.js's own format.js imports -- only mhz/sampleRate
  // are reached by the row/transport/detail paths this test exercises.
  mhz: (hz) => `${hz}Hz`,
  sampleRate: (hz) => `${hz}sps`,
  integrityBadge: () => ({ cls: '', title: '', text: '' }),
  callLineText: () => '',
  sessionFactText: () => '',
  decoderValidation: () => ({ title: '', text: '' }),
  groupDecoders: () => [],
};

vm.runInNewContext(healthSrc + '\n' + reviewSrc + '\nglobalThis.ReviewView = ReviewView;', ctx);

const view = Object.create(ctx.ReviewView.prototype);
Object.assign(view, { replay: null, _review: null, tz: null, _loop: true, _selectedPath: null });

const damaged = {
  path: '/rec/bad', name: 'bad-rec', center_hz: null, sample_rate: null,
  duration_s: null, size_bytes: null, gaps: null, runs: null,
  created: 1700000000, capture_start: null, error: 'JSONDecodeError: bad meta',
};
const healthy = {
  path: '/rec/good', name: 'good-rec', center_hz: 100000000, sample_rate: 2000000,
  duration_s: 12.5, size_bytes: 123456, gaps: 0, runs: [],
  created: 1700000000, capture_start: 1700000000, error: null,
};

// -- list row --
const damagedRow = view._row(damaged);
assert.ok(damagedRow.includes('damaged: JSONDecodeError: bad meta'),
  `damaged reason must appear in the row, got: ${damagedRow}`);
assert.ok(!damagedRow.includes('0:00'),
  `a damaged row must never show a coerced 0:00 duration, got: ${damagedRow}`);

const healthyRow = view._row(healthy);
assert.ok(!healthyRow.includes('damaged'),
  `a healthy row must not be flagged damaged, got: ${healthyRow}`);
assert.ok(healthyRow.includes('good-rec'));

// -- transport (the REVIEW THIS RECORDING button) --
const damagedTransport = view._renderTransport(damaged);
assert.ok(damagedTransport.includes('REVIEW THIS RECORDING'));
assert.ok(damagedTransport.includes('?disabled=true'),
  `the replay button must be disabled for a damaged recording, got: ${damagedTransport}`);
assert.ok(damagedTransport.includes('cannot replay: JSONDecodeError: bad meta'),
  `the disabled button must explain why via its title, got: ${damagedTransport}`);

const healthyTransport = view._renderTransport(healthy);
assert.ok(healthyTransport.includes('?disabled=false'),
  `a healthy recording must keep the replay button enabled, got: ${healthyTransport}`);

// -- detail readouts: duration/gaps must stay "unknown", never 0:00/0 --
const damagedDetail = view._renderDetail(damaged);
assert.ok(damagedDetail.includes('damaged: JSONDecodeError: bad meta'));
assert.ok(!damagedDetail.includes('0:00'),
  `the detail panel must never show a coerced 0:00 duration, got: ${damagedDetail}`);

const healthyDetail = view._renderDetail(healthy);
assert.ok(!healthyDetail.includes('damaged'));
assert.ok(healthyDetail.includes('0:13'), 'a healthy 12.5s duration still renders normally');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(REVIEW_VIEW), str(RECORDING_HEALTH)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
