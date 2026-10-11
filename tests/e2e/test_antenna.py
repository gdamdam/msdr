"""Receiver panel's ANT select (instrument-panel.js): changing it posts to
POST /api/receiver/antenna and only shows the switch as confirmed once the
fake echoes it back in props.active_antenna (same RestoreReport contract as
the tune routes) -- never the requested value.

NOTE: as of writing, POST /api/receiver/antenna does not exist yet (backend
work in progress in parallel). This test is expected to fail until that
lands; left in place per instructions for the parent to run afterward.
"""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_antenna_select_switches_b_to_a_and_shows_confirmed(live_app, page: Page):
    page.goto(live_app.base_url)

    select = page.locator("select[aria-label='Antenna']")
    expect(select).to_be_visible()
    assert live_app.fake.state["active_antenna"] == "Antenna B"
    expect(select).to_have_value("Antenna B")

    select.select_option("Antenna A")

    assert live_app.run(live_app.fake.wait_for_set("active_antenna", timeout=3.0)) == "Antenna A"
    assert live_app.fake.state["active_antenna"] == "Antenna A"
    expect(select).to_have_value("Antenna A")
    expect(select).to_be_enabled()
    expect(select.locator("option:checked")).to_have_text("A")
