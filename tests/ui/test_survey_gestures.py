"""Exercise spectrum gesture math and click timing with the real component methods."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _method(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0].rsplit("}", 1)[0] + "}"


def test_wheel_zoom_stays_anchored_and_drag_pan_stays_in_capture():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    zoom = "  _zoomAt(" + _method(source, "  _zoomAt(", "\n  _onCanvasWheel")
    view = "  _viewFrame() {" + _method(source, "  _viewFrame() {", "\n  _setZoom")
    pan_move = (
        "  _onCanvasPointerMoveGesture("
        + _method(source, "  _onCanvasPointerMoveGesture(", "\n  _onCanvasPointerUp")
    )
    methods_json = json.dumps("{" + view + ",\n" + zoom + ",\n" + pan_move + "}")
    script = f"""
const assert = require('node:assert/strict');
const methods = new Function('return (' + {methods_json} + ')')();
const bins = new Uint8Array(512);
const canvas = {{ getBoundingClientRect: () => ({{left: 10, top: 0, width: 800, height: 300}}) }};
const viewState = {{ frame: {{bins, center_hz: 100e6, sample_rate_hz: 8e6}}, _zoom: 1, _pan: .5,
  _onCanvasPointerLeave() {{}}, _scheduleDraw() {{}} }};
Object.assign(viewState, methods);
const before = viewState._viewFrame();
const pointerHz = before.center_hz - before.sample_rate_hz/2 + .3*before.sample_rate_hz;
viewState._zoomAt(250, canvas, 2);
const after = viewState._viewFrame();
const anchoredHz = after.center_hz - after.sample_rate_hz/2 + .3*after.sample_rate_hz;
    assert.ok(Math.abs(anchoredHz-pointerHz) <= after.sample_rate_hz/512);
    assert.equal(viewState._zoom, 2);

viewState._zoom = 4;
viewState._pan = 0;
const edgeBefore = viewState._viewFrame();
const edgeHz = edgeBefore.center_hz - edgeBefore.sample_rate_hz/2 + .3*edgeBefore.sample_rate_hz;
viewState._zoomAt(250, canvas, 1.5);
const edgeAfter = viewState._viewFrame();
const edgeAnchoredHz = edgeAfter.center_hz - edgeAfter.sample_rate_hz/2
  + .3*edgeAfter.sample_rate_hz;
assert.ok(Math.abs(edgeAnchoredHz-edgeHz) <= edgeAfter.sample_rate_hz/512,
  'zoom at a clamped left edge must use the visible start bin');

const panState = {{frame: viewState.frame, _zoom: 4, _pan: .5,
  _canvasGesture: {{pointerId: 7, kind: 'pan', startX: 400, startPan: .5, startZoom: 4,
    canvas, moved: false}}, _onCanvasPointerLeave() {{}}, _scheduleDraw() {{}}}};
Object.assign(panState, methods);
panState._onCanvasPointerMoveGesture({{pointerId: 7, clientX: 800}});
assert.ok(panState._pan >= .125 && panState._pan <= .875);
assert.ok(panState._pan < .5);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_canvas_click_is_immediate_and_shift_click_is_inspection_only():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    click = (
        "  _queueCanvasClick("
        + _method(source, "  _queueCanvasClick(", "\n  _onCanvasDoubleClick")
    )
    double = (
        "  _onCanvasDoubleClick("
        + _method(source, "  _onCanvasDoubleClick(", "\n  _onCanvasClick")
    )
    methods_json = json.dumps("{" + click + ",\n" + double + "}")
    script = f"""
const assert = require('node:assert/strict');
const methods = new Function('return (' + {methods_json} + ')')();
const calls = [];
const state = Object.assign({{_suppressCanvasClick: null,
  _markerLabelAtPoint: () => null,
  _onCanvasClick: value => calls.push(value),
}}, methods);
const canvas = {{}};
state._queueCanvasClick({{currentTarget: canvas, clientX: 40, clientY: 20,
  shiftKey: false, altKey: false}});
assert.equal(calls.length, 1, 'first click should be handled synchronously');
state._queueCanvasClick({{currentTarget: canvas, clientX: 40, clientY: 20,
  shiftKey: false, altKey: false, detail: 2}});
assert.equal(calls.length, 1, 'second click of a double-click should not tune again');
state._queueCanvasClick({{currentTarget: canvas, clientX: 40, clientY: 20,
  shiftKey: true, altKey: false}});
assert.equal(calls.length, 2);
assert.equal(calls[1].shiftKey, true);
state._onCanvasDoubleClick({{currentTarget: canvas, clientX: 40, clientY: 20}});
assert.equal(calls.length, 3);
assert.equal(calls[2].shiftKey, true,
  'double-click inspection should reuse the tune-free inspection path');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_marker_drag_commits_once_and_cancel_does_not_tune():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    up = (
        "  _onCanvasPointerUp("
        + _method(source, "  _onCanvasPointerUp(", "\n  _onCanvasPointerCancel")
    )
    cancel = (
        "  _onCanvasPointerCancel("
        + _method(source, "  _onCanvasPointerCancel(", "\n  _cancelCanvasGesture")
    )
    cleanup = (
        "  _cancelCanvasGesture("
        + _method(source, "  _cancelCanvasGesture(", "\n  _onCanvasKeyDown")
    )
    methods_json = json.dumps("{" + up + ",\n" + cancel + ",\n" + cleanup + "}")
    script = f"""
const assert = require('node:assert/strict');
global.CustomEvent = class {{
  constructor(type, init) {{ this.type = type; this.detail = init.detail; }}
}};
const methods = new Function('return (' + {methods_json} + ')')();
const tuned = [];
const state = Object.assign({{_canvasGesture: {{pointerId: 3, kind: 'marker', moved: true}},
  _dragPreviewHz: 145800123, connected: true, canControl: true,
  status: {{replay: false}}, _snapOn: false,
  _nearestLabel: () => null, dispatchEvent: event => tuned.push(event.detail.hz),
  _scheduleDraw() {{}}}}, methods);
state._onCanvasPointerUp({{pointerId: 3, clientX: 340, clientY: 120}});
assert.deepEqual(tuned, [145800123]);
assert.equal(state._canvasGesture, null);

state._canvasGesture = {{pointerId: 4, kind: 'marker', moved: true}};
state._dragPreviewHz = 433900000;
state._onCanvasPointerCancel({{pointerId: 4}});
assert.deepEqual(tuned, [145800123], 'cancel must not issue a tune');
    assert.equal(state._dragPreviewHz, null);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_click_tuning_respects_control_replay_snap_alt_and_shift():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    click = "  _onCanvasClick(" + _method(source, "  _onCanvasClick(", "\n  _selectLabel")
    click_json = json.dumps("{" + click + "}")
    script = f"""
const assert = require('node:assert/strict');
global.CustomEvent = class {{
  constructor(type, init) {{ this.type = type; this.detail = init.detail; }}
}};
const click = new Function('return (' + {click_json} + ')')()._onCanvasClick;
const calls = [];
const canvas = {{getBoundingClientRect: () => ({{left: 0, width: 100}})}};
function make(overrides = {{}}) {{
  return Object.assign({{connected: true, canControl: true, status: {{replay: false}},
    _viewFrame: () => ({{center_hz: 1000, sample_rate_hz: 100, _start: 0, _count: 512}}),
    _markerLabelAtPoint: () => null,
    _nearestLabel: () => ({{label: {{id: 'near', frequency_hz: 1015}}, withinRadius: true}}),
    _selectLabel() {{}}, _openSignal: label => calls.push(['inspect', label.id]),
    _snapOn: true, dispatchEvent: event => calls.push(['tune', event.detail.hz])}}, overrides);
}}
const base = make();
click.call(base, {{currentTarget: canvas, clientX: 50, clientY: 0,
  altKey: false, shiftKey: false}});
assert.deepEqual(calls.pop(), ['tune', 1015]);
click.call(base, {{currentTarget: canvas, clientX: 50, clientY: 0, altKey: true, shiftKey: false}});
assert.deepEqual(calls.pop(), ['tune', 1000], 'Alt must bypass snap');
click.call(base, {{currentTarget: canvas, clientX: 50, clientY: 0, altKey: false, shiftKey: true}});
assert.deepEqual(calls.pop(), ['inspect', 'near']);
for (const denied of [{{canControl: false}}, {{connected: false}}, {{status: {{replay: true}}}}]) {{
  click.call(make(denied), {{currentTarget: canvas, clientX: 50, clientY: 0,
    altKey: false, shiftKey: false}});
  assert.equal(calls.length, 0, 'read-only, disconnected, and replay clicks must not tune');
}}
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_drag_release_suppresses_click_and_shift_drag_inspects():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    up = (
        "  _onCanvasPointerUp("
        + _method(source, "  _onCanvasPointerUp(", "\n  _onCanvasPointerCancel")
    )
    click = (
        "  _queueCanvasClick("
        + _method(source, "  _queueCanvasClick(", "\n  _onCanvasDoubleClick")
    )
    methods_json = json.dumps("{" + up + ",\n" + click + "}")
    script = f"""
const assert = require('node:assert/strict');
global.CustomEvent = class {{ constructor(type, init) {{ this.detail = init.detail; }} }};
const methods = new Function('return (' + {methods_json} + ')')();
const calls = [];
const canvas = {{}};
const state = Object.assign({{_canvasGesture: {{pointerId: 5, kind: 'marker',
  moved: true, shiftKey: false}},
  _dragPreviewHz: 433900000, connected: true, canControl: true,
  status: {{replay: false}}, _snapOn: false,
  _nearestLabel: () => null, _markerLabelAtPoint: () => null,
  _onCanvasClick: () => calls.push('click'),
  dispatchEvent: () => calls.push('tune'), _scheduleDraw() {{}}}}, methods);
state._onCanvasPointerUp({{pointerId: 5, clientX: 80, clientY: 40}});
state._queueCanvasClick({{currentTarget: canvas, clientX: 80, clientY: 40, detail: 1}});
assert.deepEqual(calls, ['tune'], 'a drag release must not fall through into click-to-tune');

state._canvasGesture = {{pointerId: 6, kind: 'marker', moved: true, shiftKey: true}};
state._dragPreviewHz = 433900000;
state._nearestLabel = () => ({{withinRadius: true, label: {{id: 'inspect'}}}});
state._openSignal = label => calls.push(label.id);
state._onCanvasPointerUp({{pointerId: 6, clientX: 80, clientY: 40}});
assert.deepEqual(calls, ['tune', 'inspect'], 'Shift-drag inspects without another tune');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_canvas_shift_arrows_reach_global_step_handler():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    source = (Path(__file__).resolve().parents[2] / "msdr/ui/components/survey-view.js").read_text()
    body = _method(source, "  _onCanvasKeyDown(ev) {", "\n  _startWaterfallResize")
    body = body.rsplit("}", 1)[0]
    script = """
const assert = require('node:assert/strict');
const handler = new Function('ev', BODY);
const canvas = {};
let prevented = false;
const state = {_zoom: 2, _pan: .5, frame: {bins: new Uint8Array(512)}, _scheduleDraw() {}};
handler.call(state, {key: 'ArrowRight', shiftKey: true, target: canvas,
  currentTarget: canvas, preventDefault() {prevented = true;}});
assert.equal(prevented, false);
assert.equal(state._pan, .5);
handler.call(state, {key: 'ArrowRight', target: canvas,
  currentTarget: canvas, preventDefault() {prevented = true;}});
assert.equal(prevented, true);
assert.ok(state._pan > .5);
""".replace("BODY", json.dumps(body))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
