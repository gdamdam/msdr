"""Selected aircraft/vessel trajectories use persisted fixes and anchored cards."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_vehicle_tracks_are_distinct_and_details_follow_selected_marker(live_app, page):
    page.goto(live_app.base_url)
    page.locator(".workspace-tab", has_text="MAP").click()
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app'), ts = Date.now() / 1000;
      app.decodeEvents = [
        ...[0,1,2].map(i => ({id:`air-${i}`, ts:ts-i*30, decoder:'adsb',
          source_id:'receiver', frequency_hz:1090000000,
          fields:{icao:'A1B2C3', callsign:'FLIGHT42', latitude:37.8+i*.02,
            longitude:-122.4+i*.02, ground_speed_kt:220}})),
        ...[0,1,2].map(i => ({id:`ship-${i}`, ts:ts-i*30, decoder:'ais',
          source_id:'receiver', frequency_hz:161975000,
          fields:{mmsi:123456789, lat_deg:35+i*.02, lon_deg:-119+i*.02,
            sog_kn:13.2}})),
      ];
    }""")
    view = page.locator("discover-map")
    expect(view.locator("path.reception-track")).to_have_count(2)
    view.get_by_role("button", name="Details for FLIGHT42").click()
    card = view.locator(".vehicle-map-card")
    expect(card).to_contain_text("FLIGHT42")
    expect(card).to_contain_text("Observed trajectory · 3 position fixes")
    scroll = card
    before = scroll.evaluate("el => { el.scrollTop = el.scrollHeight; return el.scrollTop; }")
    assert before > 0
    view.evaluate("el => { el._tick++; return el.updateComplete; }")
    assert scroll.evaluate("el => el.scrollTop") >= before - 2
    card_box = card.bounding_box()
    frame_box = view.locator(".discover-map-frame").bounding_box()
    assert card_box and frame_box
    assert card_box["y"] >= frame_box["y"] - 1
    assert card_box["y"] + card_box["height"] <= frame_box["y"] + frame_box["height"] + 1
    expect(view.locator("path.selected-vehicle-track")).to_have_count(1)
    expect(view.locator("path.selected-vehicle-track")).to_have_attribute("stroke", "#69b6ff")
    view.get_by_role("button", name="Details for 123456789").click()
    expect(card).to_contain_text("123456789")
    expect(view.locator("path.selected-vehicle-track")).to_have_attribute("stroke", "#365bd2")
    expect(card).to_contain_text("Observed trajectory · 3 position fixes")


def test_band_scan_span_initially_matches_receiver_rate(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")
    response = page.request.post(f"{live_app.base_url}/api/survey/span", data={
        "sample_rate_hz": 6_000_000, "center_hz": 145_800_000, "keep_vfo": True,
    })
    assert response.ok, response.text()
    page.reload()
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    survey = page.locator("survey-view").first
    survey.locator("details.survey-band-scan summary").click()
    span = survey.get_by_role("textbox", name="Band scan span", exact=True)
    expect(span).to_have_value("6 MHz")
    expect(span).to_have_attribute("readonly", "")
    response = page.request.post(f"{live_app.base_url}/api/survey/span", data={
        "sample_rate_hz": 4_000_000, "center_hz": 145_800_000, "keep_vfo": True,
    })
    assert response.ok, response.text()
    expect(span).to_have_value("4 MHz")
    preset = survey.get_by_role("combobox", name="Listening time preset")
    listening = survey.get_by_role("spinbutton", name="Seconds listening per band-scan step")
    expect(listening).to_have_value("10")
    preset.select_option("30")
    expect(listening).to_have_value("30")
    expect(listening).to_have_attribute("readonly", "")
    preset.select_option("custom")
    expect(listening).not_to_have_attribute("readonly", "")
    survey.get_by_role("button", name="Longer listening time").click()
    expect(listening).to_have_value("31")
    listening.fill("45")
    expect(preset).to_have_value("custom")


def test_saved_vehicle_history_survives_live_event_window(live_app, page):
    import time
    from types import SimpleNamespace

    async def seed():
        now = time.time()
        for i in range(3):
            live_app.service.decode_events.add(SimpleNamespace(
                ts=now - i * 30, decoder="ais", kind="position", text="vessel fix",
                frequency_hz=161_975_000, source_id="receiver",
                fields={"mmsi": 987654321, "lat_deg": 36 + i * .02,
                        "lon_deg": -120 + i * .02},
            ))
    live_app.run(seed())
    page.goto(live_app.base_url)
    page.locator(".workspace-tab", has_text="MAP").click()
    view = page.locator("discover-map")
    expect(view.locator("path.reception-track")).to_have_count(1)
    page.evaluate("document.querySelector('msdr-app').decodeEvents = []")
    expect(view.locator("path.reception-track")).to_have_count(1)
    view.get_by_role("button", name="Details for 987654321").click()
    expect(view.locator(".vehicle-map-card")).to_contain_text("3 position fixes")
    page.get_by_role("button", name="Clear map").click()
    expect(view.locator(".geo-summary")).to_contain_text("0 positioned / 0 rows")
    expect(view.locator("path.reception-track")).to_have_count(0)
    expect(view.locator(".vehicle-map-card")).to_have_count(0)
    page.reload()
    page.locator(".workspace-tab", has_text="MAP").click()
    view = page.locator("discover-map")
    expect(view.locator(".geo-summary")).to_contain_text("0 positioned / 0 rows")
    async def seed_new_fix():
        live_app.service.decode_events.add(SimpleNamespace(
            ts=time.time() + 1, decoder="ais", kind="position", text="new vessel fix",
            frequency_hz=161_975_000, source_id="receiver",
            fields={"mmsi": 987654321, "lat_deg": 36.2, "lon_deg": -120.2},
        ))
    live_app.run(seed_new_fix())
    view.evaluate("el => el._loadTrackEvents()")
    expect(view.locator(".geo-summary")).to_contain_text("1 positioned / 1 rows")
    page.get_by_role("button", name="Refresh map").click()
    expect(view.locator("path.reception-track")).to_have_count(0)
    expect(view.locator(".geo-summary")).to_contain_text("1 positioned / 1 rows")


def test_active_decode_controls_stay_within_sidebar(live_app, page):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(live_app.base_url)
    deck = page.locator(".survey-first-sidebar decode-deck")
    page.evaluate("""async () => { const app = document.querySelector('msdr-app');
      app.state = {...app.state, decoder:{active:true, since:Date.now()/1000,
        frequency_hz:1090000000, decoders:[{id:'adsb',state:'running'}], band:{active:false}}};
      await app.updateComplete;
      await document.querySelector('.survey-first-sidebar decode-deck').updateComplete;
    }""")
    header = deck.locator(".scanner-deck-line").first
    expect(header.locator(".decode-identifiers")).to_have_text("ADSB")
    expect(header).not_to_contain_text("1090.000000")
    messages = header.get_by_role("button", name="All decoded messages")
    expect(messages).to_be_visible()
    label_box = header.locator(".label").bounding_box()
    messages_box = messages.bounding_box()
    assert label_box and messages_box
    assert abs(messages_box["y"] - label_box["y"]) < 10
    expect(deck.locator('button[title="Collapse"]')).to_have_count(0)
    panel_box = deck.locator(".scanner-deck").bounding_box()
    assert panel_box
    for button in header.locator("button").all():
        box = button.bounding_box()
        assert box and box["x"] + box["width"] <= panel_box["x"] + panel_box["width"] + 1
