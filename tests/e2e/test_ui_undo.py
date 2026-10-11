"""Global undo interleaves reversible UI and Library actions."""

import time

import pytest
from playwright.sync_api import Page, expect

from msdr.catalog.models import CatalogEntry
from msdr.library.models import Signal

pytestmark = pytest.mark.e2e


def test_failed_catalog_tune_is_not_put_on_undo_stack(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [CatalogEntry(source="eibi", frequency_hz=9_000_000, station="Failed Undo", mode="AM")],
    )
    live_app.drop_echo("demodulator")
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Catalog", exact=False).click()
    catalog = page.locator("catalog-view")
    row = catalog.locator(".row", has_text="Failed Undo")
    expect(row).to_be_visible()
    row.locator("button.tune").click()
    expect(catalog.locator("failure-note")).to_be_visible(timeout=10_000)
    assert page.locator("msdr-app").evaluate(
        "app => app._undoActions.filter(item => item.kind === 'tune').length"
    ) == 0


def test_global_undo_orders_receiver_tune_before_library_delete(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Undo ordering memory", frequency_hz=5_000_000))
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.connected")
    page.keyboard.press("l")

    library = page.locator("library-view")
    row = library.locator("tbody tr", has_text="Undo ordering memory")
    expect(row).to_be_visible()
    row.locator("input[type='checkbox']").check()
    library.get_by_role("button", name="DELETE", exact=True).click()
    expect(library.locator("tbody tr", has_text="Undo ordering memory")).to_have_count(0)

    before_hz = live_app.fake.state["device_vfo_frequency"]
    tune_hz = before_hz + 125_000
    page.locator("msdr-app").evaluate(
        "(app, hz) => app._doTuneFrequency(hz, 'NFM', 12_500, 'instrument')",
        tune_hz,
    )
    assert live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0)) == tune_hz

    page.keyboard.press("Control+z")
    reverted_hz = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert reverted_hz == before_hz
    expect(library.locator("tbody tr", has_text="Undo ordering memory")).to_have_count(0)
    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")

    page.keyboard.press("Control+z")
    expect(library.locator("tbody tr", has_text="Undo ordering memory")).to_be_visible()

    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")
    page.keyboard.press("Control+Shift+z")
    expect(library.locator("tbody tr", has_text="Undo ordering memory")).to_have_count(0)
    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")
    page.keyboard.press("Control+Shift+z")
    assert live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0)) == tune_hz


def test_setup_changes_are_undoable_and_double_shortcut_is_serialized(live_app, page: Page):
    page.goto(live_app.base_url)
    page.get_by_title("Setup (,)").click()
    drawer = page.locator("setup-drawer")
    drawer.locator("#setup-survey > summary").click()
    fps = drawer.locator("input[name='survey-spectrum-fps']")
    expect(fps).to_be_visible()
    before = int(fps.input_value())
    after = before - 1 if before >= 23 else before + 1
    fps.fill(str(after))
    drawer.get_by_role("button", name="Save", exact=True).click()
    expect(drawer.locator(".overlay-backdrop")).to_have_count(0)
    assert live_app.service.settings.survey.spectrum_fps == after
    page.keyboard.press("Control+z")
    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")
    assert live_app.service.settings.survey.spectrum_fps == before
    page.keyboard.press("Control+Shift+z")
    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")
    assert live_app.service.settings.survey.spectrum_fps == after
    page.keyboard.press("Control+z")
    page.wait_for_function("() => !document.querySelector('msdr-app')._undoPending")
    assert live_app.service.settings.survey.spectrum_fps == before

    # Hold the receiver-tune request open so the second shortcut arrives
    # while the first undo is pending. Only one undo request should be sent.
    app = page.locator("msdr-app")
    before_hz = live_app.fake.state["device_vfo_frequency"]
    app.evaluate(
        """(app, beforeHz) => {
          app._undoActions = [];
          app._rememberUndo({kind:'tune', before:{
            hz:beforeHz, demodulator:'NFM', bandwidthHz:12500
          }}, Date.now());
          window.__undoTuneCalls = 0;
          const original = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = typeof input === 'string' ? input : input.url;
            if (url.endsWith('/api/tune/frequency')) {
              window.__undoTuneCalls++;
              return new Promise(resolve => setTimeout(() => resolve(original(input, init)), 250));
            }
            return original(input, init);
          };
        }""",
        before_hz,
    )
    page.keyboard.press("Control+z")
    page.keyboard.press("Control+z")
    deadline = time.monotonic() + 3
    while page.evaluate("window.__undoTuneCalls") != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert page.evaluate("window.__undoTuneCalls") == 1
    deadline = time.monotonic() + 3
    while live_app.service.settings.survey.spectrum_fps != before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert live_app.service.settings.survey.spectrum_fps == before
    assert live_app.fake.state["device_vfo_frequency"] == before_hz


def test_replay_undo_never_tunes_live_receiver(live_app, page: Page):
    page.goto(live_app.base_url)
    before_hz = live_app.fake.state["device_vfo_frequency"]
    page.locator("msdr-app").evaluate(
        """app => {
          app._undoActions = [];
          app._rememberUndo(
            {kind:'tune', before:{hz: 123456789, demodulator:'NFM', bandwidthHz:12500}},
            Date.now()
          );
          app.state = {...app.state, survey:{...app.state.survey, replay:{recording_id:'test'}}};
        }"""
    )
    requests = []
    page.on(
        "request",
        lambda req: requests.append(req.url) if "/api/tune/frequency" in req.url else None,
    )

    page.keyboard.press("Control+z")

    expect(page.locator("msdr-app")).to_contain_text("Stop replay in Review")
    assert live_app.fake.state["device_vfo_frequency"] == before_hz
    assert not any("/api/tune/frequency" in url for url in requests)
