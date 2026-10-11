# ruff: noqa: E501 -- embedded JavaScript source lines
"""audio-player.js lifecycle: socket drop tears audio down; PLAY is single-flight and stop-aware. Runs the component source in a node vm with stubs, like
test_decode_view_live_hold.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components/audio-player.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const strip = (src) => src.replace(/^import[\s\S]*?;\n/gm, '').replace(/^export /gm, '');
const src = strip(fs.readFileSync(process.argv[1], 'utf8'));
const tick = () => new Promise((r) => setTimeout(r, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const load = (cls, extra = {}) => vm.runInNewContext(src + `\n${cls}`, {
  LitElement: class { dispatchEvent() {} }, html: () => '', css: () => '', svg: () => '', customElements: { define() {} },
  console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, Number, ...extra,
});
"""


def run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SRC)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


SETUP = r"""
let ctxs = [], socks = [];
class Ctx {
  constructor() { this.closed = false; this.audioWorklet = null; ctxs.push(this); }
  createGain() { return { gain: {}, connect() {} }; }
  createScriptProcessor() { return { connect() {}, disconnect() {} }; }
  suspend() { return Promise.resolve(); }
  close() { this.closed = true; return Promise.resolve(); }
  get destination() { return {}; }
}
class WS { constructor() { this.l = {}; socks.push(this); } addEventListener(t, f) { this.l[t] = f; } close() {} send() {} }
const AudioPlayer = load('AudioPlayer', { window: { AudioContext: Ctx }, WebSocket: WS, location: { protocol: 'http:', host: 'h' } });
const mk = () => { const p = Object.create(AudioPlayer.prototype);
  Object.assign(p, { endpoint: '/ws/audio', active: true, rate: 48000, volume: 1, muted: false, _playing: false, _starting: false, _playGen: 0, _bufferMs: 0, _pendingSamples: 0, _fallbackChunks: [], _fallbackOffset: 0 });
  return p; };
"""


def test_socket_close_tears_down_context_so_next_play_does_not_leak():
    run(SETUP + r"""
const p = mk();
await p._play();
assert.equal(p._playing, true);
socks[0].l.close();
assert.equal(p._playing, false);
assert.equal(ctxs[0].closed, true);
assert.equal(p._ctx, null);
await p._play();
assert.equal(ctxs.length, 2);
assert.equal(ctxs[1].closed, false);
""")


def test_double_play_is_single_flight():
    run(SETUP + r"""
const p = mk();
await Promise.all([p._play(), p._play()]);
assert.equal(ctxs.length, 1);
assert.equal(socks.length, 1);
""")


def test_stop_during_start_aborts_without_socket():
    run(SETUP + r"""
const p = mk();
const d = deferred();
p._setupNode = () => d.promise;
const started = p._play();
p._stop();
d.resolve();
await started;
assert.equal(socks.length, 0);
assert.equal(p._playing, false);
assert.equal(p._starting, false);
assert.equal(ctxs[0].closed, true);
""")
