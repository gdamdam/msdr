# ruff: noqa: E501 -- embedded JavaScript source lines
"""allocation-chart.js's pure helpers: the frequency-jump parser and the
service-name -> family -> colour mapping (components/allocation-chart.js,
docs/allocations.md's "UI" bullet). Loaded via node:vm with the imports
stripped and `export` keywords dropped (same pattern as test_marker_hold.py/
test_palette.py) -- a real ESM import would execute the module's own
`customElements.define(...)` at top level, which has no stub outside a DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components" / "allocation-chart.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_JUMP_CASES = [
    ["5.264 MHz", 5_264_000],
    ["5.264MHz", 5_264_000],
    ["146.52", 146_520_000],
    ["2.4 GHz", 2_400_000_000],
    ["2.4GHz", 2_400_000_000],
    ["2.4G", 2_400_000_000],
    ["145800 kHz", 145_800_000],
    ["145800k", 145_800_000],
    ["9000000 Hz", 9_000_000],
    ["9000000", 9_000_000],
    ["  7.2 MHz  ", 7_200_000],
    ["1,420 MHz", 1_420_000_000],
    ["", None],
    ["not a frequency", None],
    ["-5", None],
    ["0", None],
]

_FAMILY_CASES = [
    ["FIXED", "fixed"],
    ["MOBILE", "mobile"],
    # The qualifier must never be passed in -- "except aeronautical mobile"
    # would otherwise false-match AERONAUTICAL; serviceFamily only ever sees
    # the plain service name.
    ["MARITIME MOBILE", "maritime"],
    ["AERONAUTICAL MOBILE (R)", "aeronautical"],
    ["AERONAUTICAL RADIONAVIGATION", "aeronautical"],
    ["MARITIME RADIONAVIGATION", "maritime"],
    ["RADIONAVIGATION", "radionavigation"],
    ["RADIOLOCATION", "radiolocation"],
    ["BROADCASTING", "broadcasting"],
    ["BROADCASTING-SATELLITE", "satellite"],
    ["FIXED-SATELLITE", "satellite"],
    ["MOBILE-SATELLITE", "satellite"],
    ["RADIONAVIGATION-SATELLITE", "satellite"],
    ["AMATEUR", "amateur"],
    ["AMATEUR-SATELLITE", "satellite"],
    ["SPACE RESEARCH", "space_research"],
    ["SPACE OPERATION", "space_research"],
    ["RADIO ASTRONOMY", "radio_astronomy"],
    ["STANDARD FREQUENCY AND TIME SIGNAL", "standard_frequency"],
    ["STANDARD FREQUENCY AND TIME SIGNAL-SATELLITE", "satellite"],
    ["", "other"],
    ["SOME UNKNOWN SERVICE", "other"],
]

_SCRIPT = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|const|class)/gm, '$1');

// popoverPlacement lives in band-popover.js (the chart re-exports it).
const popSrc = fs.readFileSync(process.argv[2], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|const|class)/gm, '$1');
const { popoverPlacement } = vm.runInNewContext(
  popSrc + '\n;({ popoverPlacement })',
  { LitElement: class {}, customElements: { define() {} }, console }
);

const mod = vm.runInNewContext(
  src + '\n;({ parseJumpFrequency, serviceFamily, familyColor, familyGradient, SERVICE_FAMILIES, AllocationChart })',
  { LitElement: class {}, customElements: { define() {} }, console, popoverPlacement }
);
mod.popoverPlacement = popoverPlacement;

const failures = [];

for (const [input, expected] of JUMP_CASES) {
  const got = mod.parseJumpFrequency(input);
  if (got !== expected) failures.push(`parseJumpFrequency(${JSON.stringify(input)}) = ${got}, expected ${expected}`);
}

for (const [input, expected] of FAMILY_CASES) {
  const got = mod.serviceFamily(input);
  if (got !== expected) failures.push(`serviceFamily(${JSON.stringify(input)}) = ${got}, expected ${expected}`);
}

const colors = mod.SERVICE_FAMILIES.map(([key]) => mod.familyColor(key));
if (new Set(colors).size !== colors.length) failures.push('family colours are not all distinct');
if (mod.familyColor('not-a-real-family') !== mod.familyColor('other')) {
  failures.push("unknown family must fall back to other's colour");
}

if (mod.familyGradient(['fixed']) !== mod.familyColor('fixed')) {
  failures.push('single-family gradient must be the plain colour');
}
const shared = mod.familyGradient(['fixed', 'mobile']);
if (!shared.includes(mod.familyColor('fixed')) || !shared.includes(mod.familyColor('mobile'))) {
  failures.push("shared-band gradient must reference both families' colours");
}

const box = { top: 100, left: 50, right: 850, bottom: 700 };
const card = { width: 300, height: 200 };
const band = (l, r, t, b) => ({ left: l, right: r, top: t, bottom: b });
let pl = mod.popoverPlacement(band(400, 500, 150, 170), card, box);
if (pl.side !== 'below' || pl.top !== 80 || pl.left !== 250) failures.push('below placement: ' + JSON.stringify(pl));
pl = mod.popoverPlacement(band(400, 500, 600, 620), card, box);
if (pl.side !== 'above' || pl.top !== 290) failures.push('above placement: ' + JSON.stringify(pl));
pl = mod.popoverPlacement(band(50, 60, 150, 170), card, box);
if (pl.left !== 0) failures.push('left clamp: ' + JSON.stringify(pl));
pl = mod.popoverPlacement(band(840, 850, 150, 170), card, box);
if (pl.left !== 500) failures.push('right clamp: ' + JSON.stringify(pl));
// Too tall for either side: roomier side, capped to its room, never off the edge.
pl = mod.popoverPlacement(band(400, 500, 150, 170), { width: 300, height: 900 }, box);
if (pl.side !== 'below' || pl.top !== 80 || pl.maxHeight !== 520) failures.push('tall card below: ' + JSON.stringify(pl));
pl = mod.popoverPlacement(band(400, 500, 600, 620), { width: 300, height: 900 }, box);
if (pl.side !== 'above' || pl.top !== 0 || pl.maxHeight !== 490) failures.push('tall card above: ' + JSON.stringify(pl));

const chart = new mod.AllocationChart();
chart._anchor = { el: { isConnected: false } };
chart._inspecting = { hz: 1, loading: false, result: null, error: null };
chart._closeInspect();
if (chart._anchor !== null || chart._inspecting !== null) failures.push('_closeInspect must clear anchor and state');

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
"""


def test_parse_jump_frequency_and_family_colour_mapping():
    script = (
        "const JUMP_CASES = " + json.dumps(_JUMP_CASES) + ";\n"
        "const FAMILY_CASES = " + json.dumps(_FAMILY_CASES) + ";\n"
        + _SCRIPT
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(MODULE), str(MODULE.parent / "band-popover.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
