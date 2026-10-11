# ruff: noqa: E501 -- embedded JavaScript source lines
"""native-audio-dsp.js: the AGC select reflects the persisted mode via ?selected -- same vm.runInNewContext harness as test_palette.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/native-audio-dsp.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const load = (extra = {}) => vm.runInNewContext(raw + '\nNativeAudioDsp', {
  LitElement: class {}, html: (strings, ...values) => ({ strings: [...strings], values }), svg: (strings, ...values) => ({ strings: [...strings], values }),
  customElements: { define() {} }, console, CustomEvent: class { constructor(t, i = {}) { this.type = t; this.detail = i.detail; } },
  ...extra,
});
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_agc_option_selected_for_persisted_mode():
    _run(r"""
const C = load({ configureNativeAudio() {} });
const v = Object.create(C.prototype);
Object.assign(v, { mode: 'review', demodulator: 'AM', settings: { agc_mode: 'fast' }, _error: '' });
const found = [];
const walk = (n) => { if (Array.isArray(n)) n.forEach(walk); else if (n?.strings) { found.push(n); n.values.forEach(walk); } };
walk(v.render());
const opts = found.filter((n) => n.strings.some((p) => p.includes('<option value=')) && n.strings.some((p) => p.includes('?selected=')));
const selected = opts.filter((n) => n.values[1] === true).map((n) => n.values[0]);
assert.deepEqual(selected, ['fast']);
const sel = found.find((n) => n.strings[0].includes('<select @change') || n.strings.some((p) => p.includes('<select ')));
assert.ok(!found.some((n) => n.strings.some((p) => /<select[^>]*\.value=/.test(p))), 'no .value on the dynamic select');
""")
