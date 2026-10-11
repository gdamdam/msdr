"""Delayed contextual help covers controls without taking over interactive plots."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_long_hover_help_and_existing_hover_ownership(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    tab = page.locator(".workspace-tab[data-workspace='discover']")
    tip = page.locator("#msdr-ui-help")
    tab.hover()
    expect(tip).to_be_hidden()
    expect(tip).to_be_visible(timeout=2000)
    expect(tip).to_contain_text("Survey live spectrum")
    page.locator(".workspace-tab[data-workspace='listen']").hover()
    expect(tip).to_be_hidden()
    expect(tip).to_be_visible(timeout=2000)
    expect(tip).to_contain_text("saved and nearby channels")

    # The canvas already uses pointer hover to inspect spectrum positions.
    canvas = page.locator("survey-view canvas.survey-canvas").first
    canvas.hover(position={"x": 15, "y": 15})
    expect(tip).to_be_hidden()
    page.wait_for_timeout(1050)
    expect(tip).to_be_hidden()


def test_help_is_short_delayed_and_clears_on_action(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    tip = page.locator("#msdr-ui-help")
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    action = page.locator("survey-view .survey-band-scan > summary")
    action.hover()
    page.wait_for_timeout(250)
    expect(tip).to_be_hidden()
    expect(tip).to_be_visible(timeout=1800)
    expect(tip).to_contain_text("Scan a frequency range")
    page.mouse.down()
    expect(tip).to_be_hidden()
    page.mouse.up()
    # Existing native titles remain authoritative.
    page.locator("#step-down").hover()
    page.wait_for_timeout(1050)
    expect(tip).to_be_hidden()


def test_receiver_status_indicators_explain_their_meaning(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    tip = page.locator("#msdr-ui-help")
    for selector, phrase in [
        ("#instrument-panel .lamp-cluster", "connected to SDRconnect"),
        ("#instrument-panel .lamp-cluster:nth-of-type(2)", "saved channel"),
        ("#instrument-panel .source-chip", "live SDRconnect VFO"),
    ]:
        page.locator(selector).first.hover()
        expect(tip).to_be_visible(timeout=2000)
        expect(tip).to_contain_text(phrase)


def test_receiver_activity_chip_tracks_survey_and_band_scan(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    app = page.locator("msdr-app")
    label = page.locator("#instrument-panel .lamp-cluster .label").last
    app.evaluate("""el => {
      el.state = {...el.state, owner: {kind:'manual', label:'MANUAL'},
        survey: {...el.state.survey, active:true, replay:null}};
    }""")
    expect(label).to_have_text("SURVEY")
    app.evaluate("el => { el._bandScanStatus = {state:'running'}; }")
    expect(label).to_have_text("BAND SCAN")
    app.evaluate("""el => {
      el._bandScanStatus = {state:'idle'};
      el.state = {...el.state, survey: {...el.state.survey, active:false}};
    }""")
    expect(label).to_have_text("MANUAL")
