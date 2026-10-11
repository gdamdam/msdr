# ruff: noqa: E501 -- embedded JavaScript source lines
"""setup-drawer.js Beacons NDB picker helpers: isNdbRow and mergeNdbFrequencies.
Same vm.runInNewContext harness as test_wifi_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/setup-drawer.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^import \{[^}]*\} from .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
"""


def _run(body):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", PRELUDE + body, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_merge_and_ndb_filter():
    _run(r"""
const mod = vm.runInNewContext(raw + '\n({ isNdbRow, mergeNdbFrequencies })', {
  LitElement: class {}, html: () => {}, customElements: { define() {} }, console,
  api: {}, document: {}, window: {},
});
const m = mod.mergeNdbFrequencies;
assert.equal(m('', 375000, true), '375000');
assert.equal(m('375000\n200000', 250000, true), '200000\n250000\n375000');   // numeric sort, not lexical
assert.equal(m('375000\n375000', 375000, true), '375000');                   // de-dupe
assert.equal(m('375000\n190000\n999', 375000, false), '999\n190000');        // manual entry kept
assert.equal(m('375000\nabc', 400000, true), '375000\n400000');              // junk dropped
assert.equal(mod.isNdbRow({ component: 'NDB', frequency_hz: 375000 }), true);
assert.equal(mod.isNdbRow({ component: 'MARINE NDB', frequency_hz: null }), true);
assert.equal(mod.isNdbRow({ component: 'VOR', frequency_hz: 113000000 }), false);
assert.equal(mod.isNdbRow({ component: 'X', frequency_hz: 300000 }), true);
""")
