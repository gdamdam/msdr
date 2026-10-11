# ruff: noqa: E501 -- embedded JavaScript source lines
"""S1 review finding P2: app.js saved `msdr.preset.visible` (from a
preset-start event, see presets.js) but nothing read it back, so the strip's
MORE state and survey-view's OPTIONS/TECH state never actually matched a
preset's plan. Fixed by pushing the flags into the already-mounted
<instrument-panel>/<survey-view> instances via a new `applyPresetVisible`
method on each, called from app.js's _onPresetStart.

Same constraint as test_survey_workspace_mode.py: these are LitElement
subclasses that need a real DOM (the vendored lit.js touches `document` at
import time), so this is a static assertion over source text rather than a
Node-runtime import -- see that file's docstring for the full rationale.
"""

import re
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui"
APP_JS = UI_DIR / "app.js"
INSTRUMENT_PANEL_JS = UI_DIR / "components" / "instrument-panel.js"
SURVEY_VIEW_JS = UI_DIR / "components" / "survey-view.js"


def test_preset_start_still_saves_the_visibility_flags():
    src = APP_JS.read_text()
    assert 'writeLocal("msdr.preset.visible", JSON.stringify(detail.visible));' in src


def test_preset_start_pushes_visibility_into_mounted_survey_view():
    src = APP_JS.read_text()
    onstart = src[src.index("async _onPresetStart(detail) {") :]
    onstart = onstart[: onstart.index("\n  }\n") + 5]
    assert re.search(r'querySelector\("survey-view"\)\?\.applyPresetVisible\(detail\.visible\)', onstart), (
        "_onPresetStart must apply the preset's visibility to the mounted survey-view, not just localStorage"
    )


def test_instrument_panel_keeps_context_visible():
    src = INSTRUMENT_PANEL_JS.read_text()
    assert 'class="settings-strip receiver-inline-context"' in src
    assert 'class="step-profile"' in src
    assert 'class="strip-tools panel"' in src
    assert "strip-more" not in src
    # Layout B: two separate locks (antenna; mode + bandwidth) and one step widget.
    assert '"antenna-lock", "antenna")' in src
    assert '"mode-lock", "mode", "mode and bandwidth")' in src
    assert 'class="step-row step-group mono"' in src
    for step_id in ("step-down", "step-size", "step-up"):
        assert f'id="{step_id}"' in src


def test_survey_view_reads_preset_visible_more_and_tech_flags():
    src = SURVEY_VIEW_JS.read_text()
    method = src[src.index("applyPresetVisible(visible) {") :]
    method = method[: method.index("\n  }") + 4]
    assert "visible?.more" in method and "this._optionsOpen = visible.more;" in method
    assert "visible?.tech" in method and "this._techOn = visible.tech;" in method
    assert "saveOptionsOpen(this._optionsOpen);" in method
    assert "saveTechOn(this._techOn);" in method


def test_allocation_pill_opens_band_popover_instead_of_editing():
    src = INSTRUMENT_PANEL_JS.read_text()
    pill = src[src.index("  _allocationPill(tuneDisabled) {") :]
    pill = pill[: pill.index("\n  }\n") + 5]
    # The readout's own pencil is the only way to start editing.
    assert "startEdit" not in pill
    assert "<band-popover" in pill and "_bandPopoverOpen = true" in pill
    assert "this._bandAnchor = ev.currentTarget" in pill
    assert 'aria-haspopup="dialog"' in pill
