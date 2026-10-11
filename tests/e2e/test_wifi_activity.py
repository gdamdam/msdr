"""Wi-Fi Discover -> Activity tab in a real browser (docs/wifi.md "Wi-Fi
activity"). Rows are seeded directly into the history database rather than
waiting on real capture -- viewing this tab only reads history, it never
starts or stops reception, so there is nothing to drive through the fake
helper here (contrast tests/e2e/test_wifi.py, which does start capture)."""

import time

import pytest
from playwright.sync_api import expect

from msdr.wifi.activity import ActivityCell

pytestmark = pytest.mark.e2e

def test_activity_tab_renders_seeded_heatmap(live_app, page, tmp_path):
    screenshot = tmp_path / "wifi-activity.png"
    now = time.time()

    async def seed():
        store = live_app.service.history_store
        cells = {}
        for i in range(120):  # the last two minutes, channel 6 every second
            second = int(now) - i
            cells[(second, 6)] = ActivityCell(
                packets=5 + (i % 7), listened_s=1.0, sig_sum=-70.0, sig_n=1
            )
            if i % 3 == 0:  # channel 11 only occasionally listened -- sparser column
                cells[(second, 11)] = ActivityCell(packets=1, listened_s=1.0)
        store.write_activity(cells)

    live_app.run(seed())

    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Wi-Fi").click()
    wifi_view = page.locator("wifi-view")
    expect(wifi_view).to_be_visible()

    wifi_view.get_by_role("tab", name="Activity").click()
    chart = wifi_view.locator("wifi-activity-chart")
    expect(chart).to_be_visible()
    expect(chart.locator("svg")).to_be_visible(timeout=10000)
    expect(chart.locator("rect.wifi-activity-cell").first).to_be_visible(timeout=10000)
    expect(wifi_view.locator(".wifi-activity-sentence")).to_contain_text("not measured RF spectrum")
    expect(wifi_view.locator(".wifi-activity-key")).to_contain_text("hatched")

    page.screenshot(path=str(screenshot))
