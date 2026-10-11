# ruff: noqa: E501 -- embedded JavaScript source lines
"""theme.js: stored-theme validation and data-theme application. Same
vm.runInNewContext harness as test_wifi_view.py, with stubbed localStorage and
document.documentElement."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/theme.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
function load() {
  const store = new Map();
  const attrs = new Map();
  const ctx = {
    localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
    document: { documentElement: {
      setAttribute: (k, v) => attrs.set(k, v),
      removeAttribute: (k) => attrs.delete(k),
    } },
  };
  const mod = vm.runInNewContext(raw + '\n({ THEMES, readTheme, applyTheme, nextTheme, themeLabel })', ctx);
  return { mod, store, attrs };
}
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_read_theme_default_roundtrip_and_unknown():
    _run(r"""
let { mod, store } = load();
assert.equal(mod.readTheme(), 'default');
store.set('msdr.theme', 'midnight');
assert.equal(mod.readTheme(), 'midnight');
store.set('msdr.theme', 'nope');
assert.equal(mod.readTheme(), 'default');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_apply_theme_sets_and_removes_attribute_and_persists():
    _run(r"""
let { mod, store, attrs } = load();
mod.applyTheme('blueprint');
assert.equal(attrs.get('data-theme'), 'blueprint');
assert.equal(store.get('msdr.theme'), 'blueprint');
assert.equal(mod.readTheme(), 'blueprint');
mod.applyTheme('default');
assert.equal(attrs.has('data-theme'), false);
assert.equal(mod.readTheme(), 'default');
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_next_theme_cycles_in_order_and_daylight_is_gone():
    """The ◐ strip button walks THEMES in order and wraps; an unknown id
    restarts at the first entry. Daylight was dropped in 0.74.1."""
    _run(r"""
const { mod } = load();
const ids = mod.THEMES.map((t) => t.id);
assert.equal(JSON.stringify(ids), JSON.stringify(['default', 'midnight', 'blueprint', 'avionics', 'monochrome']));
assert.equal(mod.nextTheme('default'), 'midnight');
assert.equal(mod.nextTheme('monochrome'), 'default', 'wraps');
assert.equal(mod.nextTheme('daylight'), 'default', 'unknown restarts');
assert.equal(mod.themeLabel('avionics'), 'Avionics');
assert.equal(mod.themeLabel('nope'), 'Default (instrument)');
""")
