"""Pass selection must drive both charts without tuning the radio."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_pass_selection_charts_and_responsive_layout(live_app, page):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)

    def make_pass(dt, elevation):
        return dict(
            satellite="ISS",
            rise=dt.isoformat(),
            peak=(dt + timedelta(minutes=3)).isoformat(),
            set=(dt + timedelta(minutes=6)).isoformat(),
            max_elevation_deg=elevation,
            rise_azimuth_deg=310,
            peak_azimuth_deg=40,
            set_azimuth_deg=130,
            visible=True,
            geometry="Excellent",
            downlink=dict(frequency_hz=145825000, mode="NFM"),
        )

    first, later = make_pass(start, 54), make_pass(start + timedelta(hours=6), 89)
    requests = []

    def sky(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requests.append(query)
        p = later if query.get("start") == [later["rise"]] else first
        dt = datetime.fromisoformat(p["rise"])
        points = [
            dict(
                at=(dt + timedelta(minutes=i)).isoformat(),
                azimuth_deg=310 - i * 30,
                elevation_deg=p["max_elevation_deg"] * [0.1, 0.4, 0.7, 1, 0.7, 0.4, 0.1][i],
            )
            for i in range(7)
        ]
        route.fulfill(json=dict(track={**p, "points": points}, position=None, error=None))

    page.route(
        "**/api/passes",
        lambda r: r.fulfill(
            json=dict(passes=[later, first], timezone="America/Los_Angeles", engine="internal")
        ),
    )
    page.route("**/api/sky/ISS?*", sky)
    page.goto(live_app.base_url)
    response = page.request.patch(
        f"{live_app.base_url}/api/settings",
        data={
            "workspace": "satellites",
            "features": {"sky": True, "passes": True},
            "location": {
                "lat": 37.756,
                "lon": -122.401,
                "altitude_m": 50,
                "timezone": "America/Los_Angeles",
            },
        },
    )
    assert response.ok
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.reload()
    rows = page.locator(".satellite-pass")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("54°")
    before = live_app.fake.state["device_vfo_frequency"]
    rows.nth(1).dblclick()
    expect(rows.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".satellite-elevation")).to_contain_text("89°")
    assert requests[-1]["start"] == [later["rise"]]
    assert requests[-1]["end"] == [later["set"]]
    assert live_app.fake.state["device_vfo_frequency"] == before
    expect(page.locator(".satellite-charts")).to_be_focused()
    left = page.locator("passes-view").bounding_box()
    right = page.locator("sky-view").bounding_box()
    assert right["x"] > left["x"] + left["width"]
    page.locator(".satellite-workspace").screenshot(path="/tmp/msdr-satellite-desktop.png")
    rows.first.press("Enter")
    expect(page.locator(".satellite-elevation")).to_contain_text("54°")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator(".satellite-workspace").screenshot(path="/tmp/msdr-satellite-mobile.png")

    page.locator(".satellite-elevation").scroll_into_view_if_needed()
    expect(page.locator(".satellite-elevation")).to_be_in_viewport()
    page.screenshot(path="/tmp/msdr-satellite-mobile-chart.png")
    days = page.get_by_label("Prediction days")
    days.select_option("7")
    expect(days).to_be_enabled()
    expect(days).to_have_value("7")
    settings = page.request.get(f"{live_app.base_url}/api/settings").json()
    assert settings["passes_days"] == 7
    iss = page.get_by_role("group", name="Satellites to predict").get_by_role(
        "button", name="ISS", exact=True
    )
    iss.click()
    expect(iss).to_be_enabled()
    expect(iss).to_have_attribute("aria-pressed", "false")
    settings = page.request.get(f"{live_app.base_url}/api/settings").json()
    assert "ISS" not in settings["passes_satellites"]
    page.reload()
    expect(page.get_by_label("Prediction days")).to_have_value("7")
    expect(iss).to_have_attribute("aria-pressed", "false")
