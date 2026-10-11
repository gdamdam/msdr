"""Preview, apply and keyboard undo through the real receiver preparation API."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_decoder_prepare_preview_apply_and_undo(live_app, page):
    page.route("**/api/decoders", lambda r: r.fulfill(json=[{
        "id": "adsb", "label": "ADS-B / Mode S", "available": True,
        "demodulators": ["IQ"], "default_bandwidth_hz": 2400000,
    }]))
    page.goto(live_app.base_url)
    page.get_by_role("button", name="Prepare receiver", exact=True).click()
    page.locator('#decode-channel-list input[data-decoder-id="adsb"]').check()
    panel = page.get_by_role("region", name="Prepare decoder receiver")
    # section has an accessible label and therefore a region role.
    panel.get_by_role("button", name="Preview receiver preset").click()
    expect(panel).to_contain_text("1090 MHz")
    panel.get_by_role("button", name="Apply receiver preset").click()
    expect(panel).to_contain_text("Receiver ready")
    page.keyboard.press("Escape")
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#status-line")).to_contain_text("Receiver settings restored")


def test_decoder_selection_removes_incompatible_path(live_app, page):
    page.route("**/api/decoders", lambda r: r.fulfill(json=[
        {"id": "adsb", "label": "ADS-B", "available": True, "demodulators": ["IQ"]},
        {"id": "ais", "label": "AIS", "available": True, "demodulators": ["NFM"]},
        {"id": "aprs", "label": "APRS", "available": True, "demodulators": ["NFM"]},
    ]))
    page.goto(live_app.base_url)
    page.get_by_role("button", name="Prepare receiver", exact=True).click()
    selectors = {key: page.locator(f'#decode-channel-list input[data-decoder-id="{key}"]')
                 for key in ("adsb", "ais", "aprs")}
    selectors["adsb"].check()
    selectors["ais"].check()
    expect(selectors["adsb"]).not_to_be_checked()
    expect(page.get_by_text("Deselected ADS-B:", exact=False)).to_be_visible()
    selectors["aprs"].check()
    expect(selectors["ais"]).to_be_checked()
    selectors["adsb"].check()
    expect(selectors["ais"]).not_to_be_checked()
    expect(selectors["aprs"]).not_to_be_checked()


def test_prepare_indicator_only_when_selected_decoder_needs_receiver_changes(live_app, page):
    page.goto(live_app.base_url)
    deck = page.locator("listen-workbench decode-deck")
    needed = deck.evaluate("""el => {
      localStorage.setItem('msdr.decode.selection', JSON.stringify(['adsb']));
      el.connected = true; el.started = true; el.canControl = true;
      el.status = {active:false, band:{active:false}};
      el.decoders = [{id:'adsb', available:true, demodulators:['IQ'],
        default_bandwidth_hz:2400000}];
      el.frequencyHz = 162400000;
      el.receiverProps = {device_vfo_frequency:162400000, device_center_frequency:162400000,
        device_sample_rate:6000000};
      return el._preparationNeeded;
    }""")
    assert needed is True
    expect(deck.locator(".decode-prepare-button")).to_have_class(
        "decode-prepare-button needs-attention"
    )
    deck.evaluate("""el => {
      el.frequencyHz = 1090000000;
      el.receiverProps = {device_vfo_frequency:1090000000, device_center_frequency:1090000000,
        device_sample_rate:6000000};
    }""")
    expect(deck.locator(".decode-prepare-button")).to_have_class("decode-prepare-button")
