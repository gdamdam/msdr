# ruff: noqa: E501 -- embedded JavaScript source lines
"""setup-drawer.js _buildPatch: a blank/non-numeric Auto LNA cell must block the
save with an inline error, not coerce to 0 (max gain) or NaN (open-ended band)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SRC = Path(__file__).resolve().parents[2] / "msdr/ui/components/setup-drawer.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_lna_auto_blank_or_non_numeric_cells_are_rejected():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '')
  .replace(/^export (class|function|const) /gm, '$1 ');
const context = { LitElement: class {}, html: () => {}, css: () => '', resolveFeatures: () => ({}), resolveWorkspaceLayout: () => ({}), customElements: { define() {} }, console };
vm.createContext(context);
vm.runInContext(source + "\nthis.__x = { draftFromSettings, SetupDrawer };", context);
const { draftFromSettings, SetupDrawer } = context.__x;
const bands = [{ up_to_hz: 30e6, lna_state: 4 }, { up_to_hz: null, lna_state: 0 }];
const build = (mut) => {
  const d = draftFromSettings({ lna_auto: { bands } });
  mut(d);
  const self = { _draft: d, _orig: { lna_auto: { bands } } };
  return [SetupDrawer.prototype._buildPatch.call(self), self._lnaAutoError];
};
let [p, e] = build(() => {});
assert.equal(e, null); assert.deepEqual(Object.keys(p), []);
[p, e] = build((d) => { d.lnaAutoBands[0].lnaState = ''; });
assert.equal(p, null); assert.match(e, /band 1.*LNA state/);
[p, e] = build((d) => { d.lnaAutoBands[0].upToMhz = 'abc'; });
assert.equal(p, null); assert.match(e, /band 1.*upper edge/);
[p, e] = build((d) => { d.lnaAutoBands[0].upToMhz = ''; });
assert.equal(p, null);
[p, e] = build((d) => { d.lnaAutoBands[0].upToMhz = '20'; });
assert.equal(p.lna_auto.bands[0].up_to_hz, 20e6);
"""
    r = subprocess.run(["node", "--input-type=module", "-e", script, str(SRC)],
                       capture_output=True, text=True, timeout=30, env=node_env())
    assert r.returncode == 0, r.stderr
