"""Signal workbench watch controls over a real browser and live API."""

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_workbench_api_without_browser(live_app):
    with httpx.Client(base_url=live_app.base_url) as client:
        for _ in range(100):
            state = client.get("/api/state").json()
            if state["connected"] and state["started"] and state["complete"]:
                break
            time.sleep(0.05)
        response = client.post("/api/workbench/watch", json={"frequencies_hz": [145500000]})
        assert response.status_code == 200, (
            f"{response.text} state={state} connected={live_app.service.adapter.connected} "
            f"fake_clients={len(live_app.fake._clients)}"
        )
        assert client.post("/api/workbench/stop").status_code == 200


def test_signal_workbench_reports_errors_and_live_coverage(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("""async () => {
      const state = await fetch('/api/state').then(response => response.json());
      return state.connected && state.started && state.complete;
    }""")
    page.locator("#listen-tab-watch").click()
    panel = page.locator("signal-workbench")
    expect(panel).to_be_visible()
    disclosure = panel.locator("summary").nth(0)
    disclosure.click()

    frequencies = panel.get_by_label("Watch frequencies MHz")
    frequencies.fill("0")
    # Invalid input is explained beside the field and Start explains why it is off.
    expect(panel.locator("#wb-freqs-help")).to_contain_text("Enter positive frequencies")
    expect(panel.get_by_role("button", name="Start watching")).to_be_disabled()
    page.wait_for_function("""async () => {
      const state = await fetch('/api/state').then(response => response.json());
      return state.connected && state.started && state.complete;
    }""", timeout=10000)

    # Stay well clear of DC while remaining inside the fake receiver's 2 MHz span.
    frequencies.fill("145.500")
    deadline = time.monotonic() + 10
    start_button = panel.get_by_role("button", name="Start watching")
    while True:
        page.wait_for_function("""async () => {
          const state = await fetch('/api/state').then(response => response.json());
          return state.connected && state.started && state.complete;
        }""", timeout=max(1, int((deadline - time.monotonic()) * 1000)))
        with page.expect_response("**/api/workbench/watch") as started:
            panel.get_by_role("button", name="Start watching").click()
        if started.value.status == 200:
            break
        if started.value.status != 409 or time.monotonic() >= deadline:
            pytest.fail(started.value.text())
        expect(start_button).to_be_enabled(timeout=1000)
    expect(panel.get_by_role("alert")).to_have_count(0)
    expect(panel.locator(".wb-running")).to_contain_text("live spectrum", timeout=5000)
    expect(panel.locator(".wb-running")).to_contain_text("Capture")
    expect(panel.locator("tbody tr").first).to_contain_text("Observed", timeout=5000)
    expect(panel.locator("tbody tr").first).to_contain_text("quiet / unverified")

    with page.expect_response("**/api/workbench/watch/stop") as stopped:
        panel.get_by_role("button", name="Stop watching").click()
    assert stopped.value.status == 200, stopped.value.text()
    # Stopping hides the running block but keeps the panel open where the user left it.
    expect(panel.locator(".wb-running")).to_have_count(0, timeout=3000)
    expect(panel.get_by_label("Watch frequencies MHz")).to_be_visible()


def test_signal_workbench_running_state_fits_a_phone_width(live_app, page: Page):
    """Running state with per-operation stop buttons stays readable at 390 px:
    no page-level horizontal scroll (the receiver panel above used to force ~620 px)."""
    page.set_viewport_size({"width": 390, "height": 1400})
    page.goto(live_app.base_url)
    page.wait_for_function("""async () => {
      const state = await fetch('/api/state').then(response => response.json());
      return state.connected && state.started && state.complete;
    }""")
    page.locator("#listen-tab-watch").click()
    panel = page.locator("signal-workbench")
    panel.locator("summary").nth(0).click()
    panel.get_by_label("Watch frequencies MHz").fill("145.5, 152.84")
    for _ in range(20):
        with page.expect_response("**/api/workbench/watch") as started:
            panel.get_by_role("button", name="Start watching").click()
        if started.value.status == 200:
            break
        page.wait_for_timeout(300)
    running = panel.locator(".wb-running")
    expect(running).to_contain_text("live spectrum", timeout=6000)
    expect(running).to_contain_text("Outside receiver span")
    expect(running.get_by_role("button", name="Stop watching")).to_be_visible()
    assert page.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth + 1")
    panel.get_by_role("button", name="Stop watching").click()
    expect(running).to_have_count(0, timeout=3000)
