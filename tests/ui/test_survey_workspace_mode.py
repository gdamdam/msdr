# ruff: noqa: E501 -- embedded JavaScript source lines
"""survey-view.js's `mode` prop (workspace-aware survey
chrome) can't be exercised through a Node runtime -- it's a LitElement
subclass that touches `document`/`customElements` at import time, which the
vendored lit.js bundle expects a real DOM for (see test_presets_js.py/
test_format_js.py for the pure-function modules that *can* run under plain
node). So this asserts on the component's source text instead, same spirit
as test_static.py's relative-import check: a static, load-bearing property of
the file rather than a runtime behavior.
"""

import re
from pathlib import Path

SURVEY_VIEW_JS = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui" / "components" / "survey-view.js"
APP_JS = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui" / "app.js"


def _source() -> str:
    return SURVEY_VIEW_JS.read_text()


def test_mode_property_declared_and_defaults_to_discover():
    src = _source()
    assert re.search(r"mode:\s*\{\s*type:\s*String\s*\}", src), "survey-view.js must declare a `mode` property"
    assert 'this.mode = "discover";' in src, "mode must default to discover so existing chrome is unchanged"


def test_app_js_passes_workspace_as_survey_view_mode():
    src = APP_JS.read_text()
    assert re.search(r"<survey-view\s*\n\s*\.mode=\$\{this\.workspace\}", src), (
        "app.js must pass `.mode=${this.workspace}` to <survey-view> so listen/discover render distinct chrome"
    )


def test_listen_mode_hides_options_toggle_and_replay_picker():
    src = _source()
    # render() must gate the OPTIONS/IDENTIFY/STATUS group (and thus the
    # analyzer-start button nested inside it, see
    # _renderOptionsToggle/_renderTableActions) behind mode !== "listen".
    render_start = src.index("  render() {")
    render_body = src[render_start:]
    assert 'const listening = this.mode === "listen";' in render_body
    assert "${this._renderSpectrumControls()}" in render_body, (
        "listen mode must still render Peak/Avg/waterfall-range controls (_renderSpectrumControls)"
    )
    assert '${!listening ? html`<div class="survey-controls">' in render_body and '"survey-group-label">Options</span>' in render_body, (
        "OPTIONS must only render on the non-listening branch"
    )
    # The ⏵ REPLAY… picker button/inline form must be conditioned on mode too.
    header_start = src.index("_renderSource() {")
    header_body = src[header_start : header_start + 3000]
    assert 'this.mode === "listen"' in header_body
    assert "⏵ REPLAY…" in header_body




def test_listen_mode_hides_decode_column():
    src = _source()
    table_start = src.index("_renderLiveTable() {")
    table_body = src[table_start : src.index("\n  }", table_start)]
    assert 'const showDecode = this.mode !== "listen";' in table_body
    assert "${col(showDecode, 100)}" in table_body
    assert '${showDecode ? html`<th>DECODE</th>` : ""}' in table_body

    row_start = src.index("_renderLabelRow(")
    row_body = src[row_start : src.index("\n  }", row_start)]
    assert 'this.mode === "listen"' in row_body, "the → DECODE cell must be conditioned on mode in _renderLabelRow"
