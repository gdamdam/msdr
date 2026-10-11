"""Focused browser checks for the unified Listen panel and replay source safety."""

import pytest
from playwright.sync_api import Page, expect

from msdr.library.models import Signal

pytestmark = pytest.mark.e2e


def test_listen_workbench_tabs_keep_existing_rows_and_keyboard_navigation(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Workbench memory", frequency_hz=7_100_000))
    page.goto(live_app.base_url)

    tabs = page.get_by_role("tablist", name="Listen views")
    expect(tabs.get_by_role("tab")).to_have_count(5)
    expect(tabs.get_by_role("tab", name="Recent")).to_have_attribute("aria-selected", "true")

    recent_tab = tabs.get_by_role("tab", name="Recent")
    recent_tab.focus()
    page.keyboard.press("ArrowRight")
    favorites = tabs.get_by_role("tab", name="Favorites")
    expect(favorites).to_have_attribute("aria-selected", "true")
    expect(favorites).to_be_focused()

    recent_tab.click()
    row = page.locator("listen-workbench recent-list .row", has_text="Workbench memory")
    expect(row).to_be_visible()
    page.wait_for_function(
        "document.querySelector('msdr-app')?.state?.connected && "
        "document.querySelector('msdr-app')?.state?.complete"
    )
    row.get_by_title("Tune").click()
    frequency = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert frequency == 7_100_000


def test_replay_disables_live_strip_and_routes_inspector_tune_to_review(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Replay tune target", frequency_hz=7_200_000))
    page.goto(live_app.base_url)
    page.locator("listen-workbench").get_by_role("tab", name="Recent").click()
    page.locator("listen-workbench recent-list .row", has_text="Replay tune target").get_by_title(
        "Signal detail"
    ).click()
    expect(page.locator("signal-panel")).to_be_visible()
    # Wait for the real connection handshake to settle before injecting the
    # virtual replay state below -- otherwise a later backend-pushed "state"
    # message (which knows nothing about this client-only override) can race
    # in and wipe it before the assertions below observe it.
    page.wait_for_function(
        "document.querySelector('msdr-app')?.state?.connected && "
        "document.querySelector('msdr-app')?.state?.complete"
    )

    # The UI is deliberately placed in the backend-pushed replay state. The
    # real review endpoint may return 409, but the request must stay virtual.
    page.evaluate(
        """() => {
          const app = document.querySelector('msdr-app');
          app.state = {
            ...app.state,
            survey: {
              ...app.state.survey,
              replay: {name: 'sample.iq', path: '/tmp/sample.iq'}
            }
          };
        }"""
    )
    expect(page.locator("instrument-panel #mode-select")).to_be_disabled()
    expect(page.locator("instrument-panel #step-up")).to_be_disabled()
    expect(page.locator("signal-panel").get_by_role("button", name="DECODE")).to_be_disabled()

    requests = []
    page.on("request", lambda req: requests.append(req.url))
    page.locator("signal-panel").get_by_role("button", name="TUNE").click()
    page.wait_for_timeout(250)
    assert any(url.endswith("/api/review/tune") for url in requests)
    assert not any(url.endswith("/api/tune/frequency") for url in requests)

    page.keyboard.press("f")
    assert page.locator("instrument-panel #frequency-edit").count() == 0


def test_favorites_search_and_sort_keep_tune_actions(live_app, page: Page):
    entries = [
        Signal(name="Zulu air", frequency_hz=161_975_000, color="blue", demodulator="NFM"),
        Signal(name="Alpha beacon", frequency_hz=10_000_000, color="yellow", demodulator="AM"),
        Signal(name="Bravo aircraft", frequency_hz=1_090_000_000, color="red", demodulator="RAW"),
    ]
    for entry in entries:
        live_app.service.signals.add(entry)
    live_app.service.add_signals_to_list("favorites", [entry.id for entry in entries])
    page.goto(live_app.base_url)
    page.locator("listen-workbench").get_by_role("tab", name="Favorites").click()

    rows = page.locator("listen-workbench recent-list .row")
    expect(rows).to_have_count(3)
    sorting = page.get_by_role("combobox", name="Sort favorites")
    sorting.select_option("frequency-asc")
    expect(rows.first).to_contain_text("Alpha beacon")
    sorting.select_option("name-desc")
    expect(rows.first).to_contain_text("Zulu air")
    sorting.select_option("color")
    expect(rows.first).to_contain_text("Bravo aircraft")

    search = page.get_by_role("searchbox", name="Search favorites")
    search.fill("1090")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Bravo aircraft")
    search.fill("no such station")
    expect(rows).to_have_count(0)
    expect(page.get_by_text("No matching entries")).to_be_visible()
    search.fill("")
    page.wait_for_function(
        "document.querySelector('msdr-app')?.state?.connected && "
        "document.querySelector('msdr-app')?.state?.complete"
    )
    rows.first.get_by_title("Tune").click()
    tuned_hz = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert tuned_hz == 1_090_000_000
