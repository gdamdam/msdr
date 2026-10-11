"""Browser geometry check for the compact receiver and Listen workbench.

Recent and Decode now precede Survey at the user's request. The test checks
that order and the docked inspector at 1280×800 and 1440×900.
"""

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

def _rect(page: Page, selector: str) -> dict:
    return page.evaluate(
        """(sel) => { const el = document.querySelector(sel);
             if (!el) return null;
             const r = el.getBoundingClientRect();
             return {top: r.top, bottom: r.bottom, height: r.height, width: r.width}; }""",
        selector,
    )


@pytest.mark.parametrize("viewport", [(1280, 800), (1440, 900)])
def test_default_listen_workbench_and_inspector_order(
    live_app, page: Page, viewport
):
    w, h = viewport
    page.set_viewport_size({"width": w, "height": h})
    page.goto(live_app.base_url)
    expect(page.locator("instrument-panel")).to_be_visible()
    expect(page.locator("survey-view canvas").first).to_be_visible()

    # Open the docked inspector the way every "select a signal" surface does.
    page.evaluate(
        """() => window.dispatchEvent(new CustomEvent("signal-open",
              {detail: {frequency_hz: 145787500}, bubbles: true}))"""
    )
    expect(page.locator("signal-panel")).to_be_visible()
    # Measure the strip with its real content: an early read (before the
    # allocation lookup, meter and recorder render) under-reports height.
    panel = page.locator("instrument-panel")
    expect(panel.locator("button.frequency-allocation")).not_to_have_text(
        re.compile("Looking up")
    )
    expect(
        panel.locator(".frequency-context-strip allocation-summary .allocation-open-chart")
    ).to_be_visible()
    expect(panel.locator("s-meter .smeter")).to_be_visible()
    expect(panel.locator("#recorder-arm-button")).to_be_visible()
    page.wait_for_timeout(300)

    strip = _rect(page, "instrument-panel")
    tabs = _rect(page, ".workspace-switch")
    workbench = _rect(page, "listen-workbench")
    canvas = _rect(page, "survey-view canvas")
    assert strip and tabs and workbench and canvas
    assert strip["top"] >= 0, f"strip scrolled out of view (top {strip['top']:.0f})"
    # Three strip rows (status, readout, settings); band-scan fields are a disclosure.
    assert 100 <= strip["height"] <= 220, f"strip {strip['height']:.0f}px (target 100–220)"
    assert tabs["bottom"] <= h
    # The user explicitly placed Recent/Decode above the Listen survey.
    assert tabs["bottom"] <= workbench["top"] < canvas["top"]
    assert workbench["top"] < h, "Listen controls must be visible without scrolling"
    assert page.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth")
    # Inspector docked (no dimming backdrop) at >=1100 px.
    assert page.locator("signal-panel .overlay-backdrop").count() == 0
    assert page.locator("signal-panel aside.docked, signal-panel .signal-panel.docked").count() == 1


def test_receiver_scanner_uses_its_own_right_column_without_overflow(live_app, page: Page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(live_app.base_url)
    response = page.request.patch(
        f"{live_app.base_url}/api/settings", data={"features": {"scanner": True}}
    )
    assert response.ok, response.text()
    page.reload()
    panel = page.locator("instrument-panel")
    panel.get_by_role("button", name=re.compile(r"^Channel scan")).click()
    scanner = panel.locator(".receiver-operations scanner-deck")
    expect(scanner).to_be_visible()
    expect(panel.locator(".receiver-primary .strip-recording")).to_have_count(0)
    expect(panel.locator(".receiver-operations .strip-recording")).to_be_visible()
    scanner.get_by_title("Expand").click()
    expect(scanner.get_by_title("Scanner setup")).to_be_visible()
    assert panel.evaluate("""el => {
      const box = el.getBoundingClientRect();
      const ops = el.querySelector('.receiver-operations').getBoundingClientRect();
      const deck = el.querySelector('scanner-deck .scanner-deck').getBoundingClientRect();
      return ops.left > box.left && deck.right <= box.right &&
        el.scrollWidth <= el.clientWidth;
    }""")


def test_recorder_status_and_buttons_share_one_receiver_group(live_app, page: Page):
    page.goto(live_app.base_url)
    panel = page.locator("instrument-panel")
    group = panel.get_by_role("group", name="Recorder controls")
    expect(group.get_by_text("Recorder · idle")).to_be_visible()
    expect(group.locator("#recorder-arm-button")).to_be_visible()
    expect(group.locator("#recorder-rec-button")).to_be_visible()
    expect(group.get_by_role("button", name="Recordings")).to_be_visible()
    expect(panel.locator(".strip-row1 .record-chip")).to_have_count(0)


def test_channel_scanner_disclosure_is_compact(live_app, page: Page):
    page.goto(live_app.base_url)
    panel = page.locator("instrument-panel")
    disclosure = panel.locator(".channel-scanner-disclosure")
    expect(disclosure).to_be_visible()
    features = page.request.get(f"{live_app.base_url}/api/settings").json()["features"]
    assert features["scanner"] is True
    expect(disclosure.locator("scanner-deck")).to_be_hidden()
    panel.get_by_role("button", name=re.compile(r"^Channel scan")).click()
    expect(disclosure.locator("scanner-deck")).to_be_visible()


def test_discover_band_scan_settings_start_and_focus(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    quick = page.locator("survey-view details.survey-band-scan")
    quick.locator("summary").click()
    quick.get_by_label("Start MHz").fill("145.000")
    quick.get_by_label("End MHz").fill("146.000")
    # Simulate SDRconnect changing span after the forms have already mounted.
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app');
      app.state = {...app.state, props: {...app.state.props, device_sample_rate: 6000000}};
    }""")
    expect(quick.get_by_label("Band scan span")).to_have_value("6 MHz")
    quick.get_by_role("combobox", name="Listening time preset").select_option("custom")
    quick.get_by_role("button", name="Longer listening time").click()
    quick.get_by_label("Overlap %").fill("30")
    with page.expect_response("**/api/survey/band-scan/start") as started:
        quick.get_by_role("button", name="▶ SCAN RANGE").click()
    assert started.value.ok, started.value.text()
    assert started.value.request.post_data_json["span_hz"] == 6000000
    expect(page.locator(".workspace-tab", has_text="DISCOVER")).to_have_attribute(
        "aria-selected", "true"
    )
    scan = page.locator("survey-view details.survey-band-scan")
    expect(scan).to_have_attribute("open", "")
    expect(scan).to_have_class("survey-band-scan band-scan-start-glow")
    expect(scan.get_by_label("Start MHz")).to_have_value("145.000")
    expect(scan.get_by_label("End MHz")).to_have_value("146.000")
    expect(scan.get_by_label("Seconds listening per band-scan step")).to_have_value("11")
    expect(scan.get_by_label("Overlap %")).to_have_value("30")
    stop = quick.get_by_role("button", name="■ STOP", exact=True)
    expect(stop).to_be_visible()
    stop_bg = stop.evaluate("el => getComputedStyle(el).backgroundColor")
    owner_stop_bg = page.locator(".owner-stop.run-stop").evaluate(
        "el => getComputedStyle(el).backgroundColor"
    )
    assert stop_bg == owner_stop_bg
    scan.locator("summary").first.click()
    expect(scan).to_have_attribute("open", "")
    quick.get_by_role("button", name="■ STOP", exact=True).click()
    expect(quick.get_by_role("button", name="▶ SCAN RANGE", exact=True)).to_be_visible()


def test_primary_tabs_and_listen_height_grip(live_app, page: Page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_app.base_url)
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    primary = page.locator(".workspace-tab.active")
    secondary = page.locator(".discover-views button[aria-pressed=true]")
    expect(primary).to_have_css("font-weight", "700")
    expect(secondary).to_have_css("font-weight", "700")
    assert primary.evaluate("el => getComputedStyle(el).borderTopWidth") == "1px"
    assert secondary.evaluate("el => getComputedStyle(el).borderTopWidth") == "0px"
    selected_color = primary.evaluate("el => getComputedStyle(el).color")
    primary.hover()
    assert primary.evaluate("el => getComputedStyle(el).color") == selected_color
    inactive = page.locator(".workspace-tab", has_text="MAP")
    inactive_idle = inactive.evaluate("el => getComputedStyle(el).color")
    inactive.hover()
    assert inactive.evaluate("el => getComputedStyle(el).color") != inactive_idle
    assert inactive.evaluate("el => getComputedStyle(el).borderTopColor") != "rgba(0, 0, 0, 0)"
    panel = page.locator(".survey-first-sidebar .listen-workbench-left")
    grip = panel.get_by_role("button", name="Resize Listen panel")
    before = panel.bounding_box()["height"]
    box = grip.bounding_box()
    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2 + 100, steps=5)
    page.mouse.up()
    assert panel.bounding_box()["height"] >= before + 90
    page.reload()
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    sidebar = page.locator(".survey-first-sidebar .listen-workbench-left")
    assert sidebar.bounding_box()["height"] >= before + 90
