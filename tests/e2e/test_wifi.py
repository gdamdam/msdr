"""Wi-Fi Discover tab, Setup card and lifecycle in a real browser (docs/
wifi.md), driven only by tests/wifi/fake_helper.py via settings.wifi.
helper_command -- never the real adapter or msdr-wifi-capture binary."""

import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

FAKE_HELPER = Path(__file__).resolve().parents[1] / "wifi" / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]


def test_wifi_enable_start_discover_select_stop(live_app, page):
    async def enable():
        await live_app.service.apply_settings(
            {"wifi": {"enabled": True, "helper_command": HELPER_COMMAND}}
        )

    live_app.run(enable())

    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Wi-Fi").click()

    wifi_view = page.locator("wifi-view")
    expect(wifi_view).to_be_visible()
    # Enabled (via PATCH above) but not yet receiving.
    expect(wifi_view.locator("p.empty")).to_contain_text("stopped")

    # Start, Stop and Clear live in the Wi-Fi view itself.
    wifi_view.get_by_role("button", name="Start", exact=True).click()

    rows = wifi_view.locator(".wifi-table tbody tr")
    expect(rows.first).to_be_visible(timeout=10000)
    first_bssid = rows.first.locator("td.mono").inner_text()

    rows.first.click()
    detail = wifi_view.locator("aside.wifi-detail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text(first_bssid)
    # Details fetched from the API, not just echoing the row -- IEs present.
    expect(detail.locator("details.wifi-ies > summary")).to_be_visible()

    # Clear empties the list while capture keeps running; rows come back.
    wifi_view.get_by_role("button", name="Clear list").click()
    expect(detail).to_have_count(0)
    expect(rows.first).to_be_visible(timeout=10000)

    wifi_view.get_by_role("button", name="Stop", exact=True).click()
    expect(wifi_view.locator(".wifi-controls")).to_contain_text("stopped", timeout=8000)
    expect(wifi_view.get_by_role("button", name="Start", exact=True)).to_be_visible()


def _synthetic_capture() -> bytes:
    """A small radiotap pcap: beacons from two networks on channel 6 with a
    varying antenna signal, enough for a busiest-network row and a signal trend."""
    from msdr.wifi import pcap
    from tests.wifi.fixtures import build_beacon_frame, build_radiotap

    out = [pcap.write_header(linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP)]
    for i in range(40):
        busy = i % 4 != 0
        rt = build_radiotap(channel_mhz=2437, antsignal=-48 - (i % 9))
        frame = build_beacon_frame(
            bssid="aa:bb:cc:dd:ee:01" if busy else "aa:bb:cc:dd:ee:02",
            ssid=b"TestNetA" if busy else b"TestNetB",
            ds_channel=6,
            frame_timestamp=i * 102_400,
        )
        out.append(pcap.write_record(1_700_000_000.0 + i * 0.25, rt + frame))
    return b"".join(out)


def test_imported_session_details_load_and_draw_the_signal_trend(live_app, page):
    """Inspecting an imported capture: details must be fetched from that session
    (they used to hit the live table and stay on "loading…"), and the signal
    sparkline must be real SVG (it was built with the html tag and never drawn)."""
    async def enable():
        await live_app.service.apply_settings({"wifi": {"enabled": True}})

    live_app.run(enable())
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(live_app.base_url)
    resp = page.request.post(
        f"{live_app.base_url}/api/wifi/import?name=fixture.pcap",
        data=_synthetic_capture(),
        headers={"Content-Type": "application/vnd.tcpdump.pcap", "Origin": live_app.base_url},
    )
    assert resp.ok, resp.text()
    session_id = resp.json().get("id") or resp.json()["session"]["id"]
    page.evaluate("id => document.querySelector('msdr-app')._openWifiInspect(id)", session_id)

    view = page.locator("wifi-view")
    view.locator("th button", has_text="Packets").click()  # busiest network first
    rows = view.locator(".wifi-table tbody tr")
    expect(rows.first).to_be_visible(timeout=10000)
    rows.first.click()
    detail = view.locator("aside.wifi-detail")
    expect(detail.locator("h4", has_text="Reception")).to_be_visible(timeout=5000)
    expect(detail).not_to_contain_text("loading")
    drawn = page.evaluate(
        "() => [...document.querySelectorAll('aside.wifi-detail polyline')]"
        ".map(p => p instanceof SVGElement)"
    )
    assert drawn and all(drawn), drawn
