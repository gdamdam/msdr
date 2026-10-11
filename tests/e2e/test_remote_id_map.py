"""Live Remote ID on the Map (docs/remote-id.md "Map"): seeding the service's
shared Tracker directly (same `live_app.service` pattern as
tests/e2e/test_wifi.py's apply_settings/tests/e2e/test_vehicle_tracks.py's
decode_events.add) must produce three distinct marker kinds -- drone,
operator and takeoff -- linked by a thin line, on the real GET
/api/remoteid/live -> discover-map.js polling path (no page.route stubbing).

NOTE: not run in this sandbox (no browser/Playwright available here); written
and reviewed for correctness against the live_app/Playwright conventions used
by the rest of tests/e2e, but not executed. Run with
`.venv/bin/pytest tests/e2e/test_remote_id_map.py -m e2e` on a machine with
Playwright browsers installed.
"""

import pytest
from playwright.sync_api import Page, expect

from msdr.remoteid import messages

pytestmark = pytest.mark.e2e


def _basic_id(uas_id: bytes) -> messages.BasicId:
    return messages.BasicId(
        protocol_version=2, ua_type=2, id_type=1, uas_id_bytes=uas_id, uas_id_text="E2E-SERIAL"
    )


def _location(*, lat: float, lon: float) -> messages.Location:
    return messages.Location(
        protocol_version=2, status=2, height_type=0, direction_deg=90.0,
        speed_horizontal_ms=5.0, speed_vertical_ms=0.0, latitude_deg=lat, longitude_deg=lon,
        altitude_baro_m=100.0, altitude_geo_m=110.0, height_m=50.0,
        horiz_accuracy_enum=11, vert_accuracy_enum=6, baro_accuracy_enum=6,
        speed_accuracy_enum=3, timestamp_accuracy_enum=1, timestamp_s=10.0,
    )


def _system(*, op_lat: float, op_lon: float, location_type: int) -> messages.System:
    return messages.System(
        protocol_version=2, operator_location_type=location_type, classification_type=0,
        operator_latitude_deg=op_lat, operator_longitude_deg=op_lon, area_count=1,
        area_radius_m=0, area_ceiling_m=None, area_floor_m=None, category_eu=0, class_eu=0,
        operator_altitude_geo_m=60.0, timestamp_s_since_2019=28_000_000,
    )


def _pack(*msgs) -> messages.MessagePack:
    return messages.MessagePack(protocol_version=2, message_count=len(msgs), messages=tuple(msgs))


def _seed_tracker(live_app) -> None:
    uas_id = bytes([0x7e]) + bytes(19)

    async def seed():
        tracker = live_app.service.remoteid_tracker
        tracker.ingest_wifi_pack(
            "wifi:aa:bb:cc:dd:ee:ff",
            1,
            _pack(
                _basic_id(uas_id),
                _location(lat=37.80, lon=-122.40),
                _system(op_lat=37.81, op_lon=-122.41, location_type=1),  # LIVE_GNSS -> operator
            ),
        )
        tracker.ingest_wifi_pack(
            "wifi:aa:bb:cc:dd:ee:ff",
            2,
            _pack(_system(op_lat=37.82, op_lon=-122.42, location_type=0)),  # TAKEOFF
        )

    live_app.run(seed())


def test_remote_id_map_layer_renders_three_marker_kinds(live_app, page: Page):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
    _seed_tracker(live_app)

    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Map").click()
    page.wait_for_function("document.querySelector('msdr-app')?.settings?.workspace === 'map'")
    map_view = page.locator("discover-map")
    expect(map_view.locator(".discover-map-canvas")).to_be_visible()

    # discover-map.js polls GET /api/remoteid/live every 2s (docs/remote-id.md
    # "Map"); give it time to pick up the seeded tracker state.
    expect(map_view.locator(".remoteid-marker.drone")).to_have_count(1, timeout=6000)
    expect(map_view.locator(".remoteid-marker.operator")).to_have_count(1, timeout=6000)
    expect(map_view.locator(".remoteid-marker.takeoff")).to_have_count(1, timeout=6000)

    # The three positions belong to one UAS -- linked by a thin connecting line.
    expect(map_view.locator("path.remoteid-link-line")).to_have_count(1)

    # Hover card on the drone marker carries UAS ID, type-specific fields,
    # transport and age -- never a verified/inferred claim.
    map_view.locator(".remoteid-marker.drone").hover()
    hover = map_view.locator(".remoteid-hover-card")
    expect(hover).to_contain_text("E2E-SERIAL")
    expect(hover).to_contain_text("drone")
    expect(hover).to_contain_text("Altitude")
    expect(hover).to_contain_text("Transport: wifi")
    expect(hover).to_contain_text("Reported claim, not a verified location")

    # Selecting the drone marker opens the Remote ID details pane.
    map_view.locator(".remoteid-marker.drone").click()
    details = map_view.get_by_label("Selected Remote ID evidence")
    expect(details).to_contain_text("E2E-SERIAL")
    expect(details).to_contain_text("Remote ID claim")

    # Layer filter isolates the new layer from the rest of the map.
    layer = map_view.get_by_role("combobox", name="Map layer")
    layer.select_option("remoteid")
    expect(map_view.locator(".geo-summary")).to_contain_text("3 positioned")
    layer.select_option("")


def test_remote_id_discover_tab_shows_live_uas_table(live_app, page: Page):
    _seed_tracker(live_app)
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Remote ID", exact=True).click()

    view = page.locator("remoteid-view")
    expect(view).to_be_visible()
    expect(view.locator("table.geo-table tbody tr")).to_have_count(1, timeout=6000)
    expect(view).to_contain_text("E2E-SERIAL")

    view.get_by_role("button", name="E2E-SERIAL").click()
    detail = view.locator(".remoteid-detail")
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Drone position")
    expect(detail).to_contain_text("Operator position")
    expect(detail).to_contain_text("Takeoff/reference position")
