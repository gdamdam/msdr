"""The Discover map and table share decoder evidence and selection."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


EVENTS = [
    {
        "id": "plane", "ts": 1750000010, "decoder": "adsb",
        "text": "<img src=x onerror=alert(1)>",
        "fields": {"report": {"aircraftState": [{
            "icao": "abc123", "callsign": "TEST42", "positionValid": True,
            "latitude": 37.78, "longitude": -122.42,
        }]}}, "integrity": "passed", "source_id": "test",
    },
    {
        "id": "plane-no-position", "ts": 1750000009, "decoder": "adsb",
        "text": "position unavailable",
        "fields": {"report": {"aircraftState": [{
            "icao": "def456", "callsign": "NOFIX",
        }]}}, "integrity": "passed", "source_id": "test",
    },
    {
        "id": "sonde", "ts": 1750000008, "decoder": "rs41", "text": "radiosonde",
        "fields": {"serial": "RS-TEST", "lat_deg": 38.0, "lon_deg": -121.0, "altitude_m": 1200},
        "integrity": "passed", "source_id": "test",
    },
    {
        "id": "aprs", "ts": 1750000007, "decoder": "aprs", "text": "APRS beacon",
        "fields": {"from": "N0CALL", "lat": 36.0, "lon": -120.0},
        "integrity": "passed", "source_id": "test",
    },
    {
        "id": "dsc", "ts": 1750000006, "decoder": "dsc", "text": "MAYDAY test",
        "fields": {"mmsi": "123456789", "lat": 35.0, "lon": -119.0},
        "integrity": "passed", "source_id": "test",
    },
    {
        "id": "failed", "ts": 1750000005, "decoder": "adsb", "text": "failed fix",
        "fields": {"report": {"aircraftState": [{
            "icao": "bad001", "callsign": "BADFIX", "latitude": 34.0, "longitude": -118.0,
        }]}},
        "integrity": "failed", "source_id": "test",
    },
]


def _seed(page: Page, live_app) -> None:
    # OSM requests are blocked so the browser test is deterministic and offline.
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Map").click()
    page.wait_for_function("document.querySelector('msdr-app')?.settings?.workspace === 'map'")
    expect(page.locator("discover-map .discover-map-canvas")).to_be_visible()
    page.evaluate("""events => {
      const app = document.querySelector('msdr-app');
      app.decodeEvents = events;
      app.signals = [{id: 'station-1', name: 'Harbor station', latitude: 37.7,
        longitude: -122.5, frequency_hz: 7000000}];
    }""", EVENTS)
    expect(page.locator("discover-map .geo-summary")).to_contain_text("5 positioned / 7 rows")


def test_discover_map_table_linked_selection_filters_integrity_and_tune(live_app, page: Page):
    page.set_viewport_size({"width": 1280, "height": 900})
    _seed(page, live_app)
    map_view = page.locator("discover-map")
    # Unknown direction still shows the vehicle type, explicitly marked unknown.
    expect(map_view.locator(".leaflet-overlay-pane path.leaflet-interactive")).to_have_count(3)
    unknown = map_view.locator(".reception-direction-marker.direction-unknown")
    expect(unknown).to_have_count(2)
    expect(unknown.first.locator(".type-symbol")).to_contain_text("✈️")
    expect(unknown.last.locator(".type-symbol")).to_contain_text("🛥️")
    expect(unknown.locator(".direction-unknown-badge")).to_have_count(2)
    expect(map_view.locator(".aircraft-map-label")).to_contain_text("TEST42 · stale")
    assert page.locator("img[src^='x']").count() == 0

    # Selecting a map marker opens evidence details, then the Table view retains selection.
    map_view.locator(".reception-direction-marker").first.click()
    expect(map_view.get_by_label("Selected map evidence")).to_be_visible()
    page.screenshot(path="/tmp/msdr-map.png", full_page=True)
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Saved signals / decodes", exact=True).click()
    expect(map_view.locator("tr.selected")).to_have_count(1)
    expect(map_view.get_by_label("Selected map evidence")).to_contain_text("TEST42")

    table = map_view.locator(".geo-table")
    expect(table.locator("tbody tr")).to_have_count(7)
    expect(table.get_by_text("NOFIX")).to_be_visible()
    expect(table.get_by_text("No usable position").first).to_be_visible()
    expect(table.get_by_text("BADFIX")).to_be_visible()
    table.get_by_role("button", name="NOFIX").click()
    expect(map_view.get_by_label("Selected map evidence")).to_contain_text("No usable position")
    page.get_by_role("tab", name="Map").click()
    expect(map_view.get_by_label("Selected map evidence")).to_contain_text("NOFIX")

    layer = map_view.get_by_role("combobox", name="Map layer")
    layer.select_option("maritime")
    expect(map_view.locator(".geo-summary")).to_contain_text("1 positioned / 1 rows")
    layer.select_option("")
    search = map_view.get_by_role("textbox", name="Search map and table")
    search.fill("RS-TEST")
    expect(map_view.locator(".geo-summary")).to_contain_text("1 positioned / 1 rows")
    search.fill("")

    # Exercise the actual bubbling tune event used by the app's tune handler.
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app');
      app.state = {...app.state, can_control: true};
      window.__tuneEvents = [];
      app.addEventListener('tune-signal', e => window.__tuneEvents.push(e.detail));
    }""")
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Saved signals / decodes", exact=True).click()
    map_view.get_by_role("button", name="Harbor station").click()
    map_view.locator("tr.selected").get_by_role("button", name="Tune").click()
    assert page.evaluate("window.__tuneEvents.length") == 1
    assert page.evaluate("window.__tuneEvents[0].id") == "station-1"


def test_clear_map_keeps_old_points_hidden_through_refresh_without_erasing_table(
    live_app, page: Page
):
    page.set_viewport_size({"width": 1280, "height": 900})
    _seed(page, live_app)
    map_view = page.locator("discover-map")
    page.get_by_role("button", name="Clear map").click()
    expect(map_view.locator(".geo-summary")).to_contain_text("0 positioned / 0 rows")
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Saved signals / decodes", exact=True).click()
    expect(map_view.locator(".geo-table tbody tr")).to_have_count(7)
    page.route("**/api/decoder/events*", lambda route: route.fulfill(json=EVENTS))
    page.route("**/api/signals", lambda route: route.fulfill(json=[{
        "id": "station-1", "name": "Harbor station", "latitude": 37.7,
        "longitude": -122.5, "frequency_hz": 7000000,
    }]))
    page.get_by_role("tab", name="Map").click()
    expect(map_view.locator(".geo-summary")).to_contain_text("0 positioned / 0 rows")
    page.get_by_role("button", name="Refresh map").click()
    expect(map_view.locator(".geo-summary")).to_contain_text("0 positioned / 0 rows")
    page.reload()
    page.get_by_role("tab", name="Map").click()
    expect(page.locator("discover-map .geo-summary")).to_contain_text("0 positioned / 0 rows")


def test_fresh_positions_glow_and_course_rotates_aircraft_and_vessel(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app');
      const ts = Date.now() / 1000;
      app.decodeEvents = [
        {id:'plane-fresh', ts, decoder:'adsb', fields:{icao:'ABC123', latitude:37.8,
          longitude:-122.4, track_deg:270}, integrity:'unavailable'},
        {id:'boat-fresh', ts, decoder:'ais', fields:{mmsi:123456789, lat_deg:37.7,
          lon_deg:-122.3, cog_deg:180}, integrity:'passed'},
      ];
    }""")
    markers = page.locator("discover-map .reception-direction-marker.fresh")
    expect(markers).to_have_count(2)
    aircraft = markers.filter(has=page.locator("svg")).first
    vessel = markers.last
    expect(aircraft.locator("svg path")).to_have_count(1)
    expect(vessel.locator("svg path")).to_have_count(2)
    assert "rotate(270deg)" in aircraft.locator(".direction-symbol").get_attribute("style")
    assert "rotate(180deg)" in vessel.locator(".direction-symbol").get_attribute("style")
    assert aircraft.evaluate("el => el.style.getPropertyValue('--fresh-age')").startswith("-")


def test_vessel_without_reported_course_faces_recent_track(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app');
      const ts = Date.now() / 1000;
      app.decodeEvents = [
        {id:'boat-west', ts:ts - 10, decoder:'ais', source_id:'rx',
          fields:{mmsi:123456789, lat_deg:0, lon_deg:0}, integrity:'passed'},
        {id:'boat-east', ts, decoder:'ais', source_id:'rx',
          fields:{mmsi:123456789, lat_deg:0, lon_deg:1}, integrity:'passed'},
      ];
    }""")
    vessel = page.locator("discover-map .reception-direction-marker.maritime")
    expect(vessel).to_have_count(1)
    assert "rotate(90deg)" in vessel.locator(".direction-symbol").get_attribute("style")
    expect(vessel).to_have_attribute("title", "Vessel: 123456789 · course from track 90°")


@pytest.mark.parametrize("width", [1280, 800, 390])
def test_discover_map_responsive_without_document_overflow(live_app, page: Page, width: int):
    page.set_viewport_size({"width": width, "height": 900})
    _seed(page, live_app)
    expect(page.locator("discover-map .discover-map-canvas")).to_be_visible()
    page.get_by_role("tab", name="Discover").click()
    page.get_by_role("button", name="Saved signals / decodes", exact=True).click()
    expect(page.locator("discover-map .geo-table")).to_be_visible()
    dimensions = page.evaluate("""() => ({
      body: document.body.scrollWidth,
      viewport: document.documentElement.clientWidth,
      table: document.querySelector('discover-map .geo-table-scroll').scrollWidth,
    })""")
    assert dimensions["body"] <= dimensions["viewport"], dimensions


def test_map_corner_resize_updates_map_and_can_reset(live_app, page: Page):
    page.set_viewport_size({"width": 1280, "height": 900})
    _seed(page, live_app)
    frame = page.locator('.discover-map-frame')
    handle = page.get_by_role('button', name='Resize map', exact=True)
    before = frame.bounding_box()
    grip = handle.bounding_box()
    x, y = grip['x'] + grip['width'] / 2, grip['y'] + grip['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 180, y + 80, steps=5)
    page.mouse.up()
    after = frame.bounding_box()
    assert after['width'] == pytest.approx(before['width'] - 180, abs=2)
    assert after['height'] == pytest.approx(before['height'] + 80, abs=2)
    page.wait_for_function("""() => {
      const map = document.querySelector('discover-map');
      const canvas = map.querySelector('.discover-map-canvas');
      return map._map.getSize().x === canvas.clientWidth &&
        map._map.getSize().y === canvas.clientHeight;
    }""")
    handle.focus()
    page.keyboard.press('ArrowUp')
    assert frame.bounding_box()['height'] == pytest.approx(after['height'] - 20, abs=2)
    page.keyboard.press('Home')
    assert frame.bounding_box()['width'] == pytest.approx(before['width'], abs=2)
    assert frame.bounding_box()['height'] == pytest.approx(before['height'], abs=2)


def test_vehicle_label_hover_details_and_external_lookups(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app'), ts = Date.now() / 1000;
      app.decodeEvents = [
        {id:'air-detail', ts, decoder:'adsb', frequency_hz:1090000000, source_id:'receiver',
          fields:{icao:'ABC123', callsign:'TEST42', registration:'N123AB', latitude:37.8,
            longitude:-122.4, altitude_ft:10000, ground_speed_kt:250, track_deg:270}},
        {id:'boat-detail', ts, decoder:'ais', frequency_hz:161975000, source_id:'receiver',
          fields:{mmsi:123456789, lat_deg:35, lon_deg:-119, sog_kn:12.3, cog_deg:180}},
      ];
    }""")
    aircraft = page.get_by_role('button', name='Details for TEST42', exact=True)
    expect(aircraft).to_have_count(1)
    aircraft.hover()
    hover = page.locator('.vehicle-hover-card')
    expect(hover).to_contain_text('ABC123')
    expect(hover).to_contain_text('250.0 kn')
    expect(hover).to_contain_text('1090.000000 MHz')
    assert hover.evaluate("el => el.getBoundingClientRect().width") >= 280
    assert min(hover.locator('dd').evaluate_all(
        "cells => cells.map(cell => cell.getBoundingClientRect().width)"
    )) >= 100
    aircraft.click()
    details = page.get_by_label('Selected map evidence')
    page.wait_for_function(
        "() => document.querySelector('.geo-detail')?.getBoundingClientRect().top < innerHeight"
    )
    expect(details).to_contain_text('Registration')
    expect(details).to_contain_text('N123AB')
    links = details.locator('.vehicle-lookup-link')
    expect(links).to_have_count(2)
    expect(links.first).to_contain_text('FlightAware')
    expect(links.first).to_have_attribute('href', 'https://www.flightaware.com/live/flight/TEST42')
    expect(links.first).to_have_attribute('target', '_blank')
    expect(links.first).to_have_attribute('rel', 'noopener noreferrer')
    expect(links.last).to_have_attribute('href', 'https://www.flightradar24.com/data')
    boat = page.get_by_role('button', name='Details for 123456789', exact=True)
    boat.hover()
    expect(hover).to_contain_text('12.3 kn')
    expect(hover).to_contain_text('161.975000 MHz')
    expect(hover.get_by_role('link', name='VesselFinder')).to_have_attribute('href',
        'https://www.vesselfinder.com/vessels/details/123456789')
    assert hover.evaluate("el => el.getBoundingClientRect().width") >= 280
    assert min(hover.locator('dd').evaluate_all(
        "cells => cells.map(cell => cell.getBoundingClientRect().width)"
    )) >= 100
    boat.click()
    page.wait_for_function(
        "() => document.querySelector('.geo-detail')?.getBoundingClientRect().top < innerHeight"
    )
    expect(details).to_contain_text('MMSI')
    expect(links.first).to_have_attribute('href', 'https://www.vesselfinder.com/vessels/details/123456789')
    expect(links.last).to_contain_text('MarineTraffic')
    expect(links.last).to_have_attribute('href', 'https://www.marinetraffic.com/en/ais/home')
    expect(details).to_contain_text('Search MMSI 123456789')
    # Rolling event retention must not erase the message being read.
    page.evaluate("document.querySelector('msdr-app').decodeEvents = []")
    expect(details).to_contain_text('123456789')
    expect(details).to_contain_text('12.3 kn')
    page.get_by_role('button', name='Close map details').click()
    expect(details).to_have_count(0)


def test_vehicle_hover_survives_map_redraw_and_has_leave_grace(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app'), ts = Date.now() / 1000;
      app.decodeEvents = [{id:'air-hover', ts, decoder:'adsb', frequency_hz:1090000000,
        fields:{icao:'A05ED9', callsign:'TEST42', latitude:37.8, longitude:-122.4}}];
    }""")
    hover = page.locator('.vehicle-hover-card')
    page.evaluate("""() => document.querySelector('[aria-label="Details for TEST42"]')
      .dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}))""")
    page.wait_for_timeout(200)
    expect(hover).to_have_count(0)
    expect(hover).to_be_visible(timeout=2000)
    label = page.get_by_role('button', name='Details for TEST42', exact=True)
    label.hover()
    expect(hover).to_contain_text('A05ED9')
    page.evaluate("""async () => {
      const map = document.querySelector('discover-map');
      window.hoverCardBeforeRedraw = document.querySelector('.vehicle-hover-card');
      map._tick++;
      await map.updateComplete;
    }""")
    assert page.evaluate(
        "document.querySelector('.vehicle-hover-card') === window.hoverCardBeforeRedraw"
    )
    page.mouse.move(0, 0)
    page.wait_for_timeout(500)
    expect(hover).to_be_visible()
    expect(hover).to_have_count(0, timeout=4000)


def test_aircraft_hover_flightaware_link_is_clickable(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app'), ts = Date.now() / 1000;
      app.decodeEvents = [{id:'air-search', ts, decoder:'adsb', frequency_hz:1090000000,
        fields:{icao:'A05ED9', callsign:'TEST42', latitude:37.8, longitude:-122.4}}];
    }""")
    label = page.get_by_role('button', name='Details for TEST42', exact=True)
    label.hover()
    hover = page.locator('.vehicle-hover-card')
    link = hover.get_by_role('link', name='FlightAware')
    expect(link).to_have_attribute('href', 'https://www.flightaware.com/live/flight/TEST42')
    expect(link).to_have_attribute('target', '_blank')
    expect(link).to_have_attribute('rel', 'noopener noreferrer')
    page.route('https://www.flightaware.com/**',
               lambda route: route.fulfill(body='FlightAware test'))
    link.hover()
    page.wait_for_timeout(350)
    expect(hover).to_be_visible()
    with page.context.expect_page() as popup_info:
        link.click()
    assert popup_info.value.url.startswith('https://www.flightaware.com/live/flight/TEST42')
    popup_info.value.close()


def test_vehicle_hover_stays_inside_map_at_each_corner_and_after_resize(live_app, page: Page):
    _seed(page, live_app)
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app'), ts = Date.now() / 1000;
      app.decodeEvents = [{id:'air-edge', ts, decoder:'adsb', frequency_hz:1090000000,
        fields:{icao:'A05ED9', callsign:'EDGE42', latitude:37.8, longitude:-122.4}}];
    }""")
    page.get_by_role('button', name='Details for EDGE42', exact=True).wait_for()
    measurements = page.evaluate("""async () => {
      const view = document.querySelector('discover-map'), map = view._map;
      const row = view.rows.find(r => r.label === 'EDGE42');
      const frame = view.querySelector('.discover-map-frame');
      const outcomes = [];
      const check = (x, y) => {
        const current = map.latLngToContainerPoint([row.lat, row.lon]);
        map.panBy([current.x - x, current.y - y], {animate:false});
        view._showVehicleHover(row);
        view._positionVehicleHover();
        const card = view.querySelector('.vehicle-hover-card').getBoundingClientRect();
        const bounds = view.querySelector('.discover-map-canvas').getBoundingClientRect();
        outcomes.push({left:card.left-bounds.left, top:card.top-bounds.top,
          right:bounds.right-card.right, bottom:bounds.bottom-card.bottom,
          width:card.width, mapWidth:bounds.width});
      };
      let size = map.getSize();
      for (const [x,y] of [[12,12],[size.x-12,12],[12,size.y-12],[size.x-12,size.y-12]])
        check(x,y);
      frame.style.width = '240px';
      map.invalidateSize({pan:false});
      size = map.getSize();
      check(size.x-12, 12);
      return outcomes;
    }""")
    assert len(measurements) == 5
    for bounds in measurements:
        assert bounds['left'] >= 6, bounds
        assert bounds['top'] >= 6, bounds
        assert bounds['right'] >= 6, bounds
        assert bounds['bottom'] >= 6, bounds
        assert bounds['width'] <= bounds['mapWidth'] - 12, bounds
