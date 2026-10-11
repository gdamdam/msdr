"""Decoder category/help and Discover output strip are user-visible flows."""

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_discover_latest_output_and_deck_messages_button_opens_messages(live_app, page: Page):
    page.goto(live_app.base_url)
    # Inject only after the app's own startup loads (and any reconnect resync,
    # which reloads decoder events) have landed, or they overwrite the fixture.
    page.wait_for_function("""async () => {
      const state = await fetch('/api/state').then(response => response.json());
      return state.connected && state.started && state.complete;
    }""")
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
          const app = document.querySelector('msdr-app');
          app.decodeEvents = [{
            id: 'event-1', ts: 1750000000, decoder: 'pocsag', kind: 'text',
            text: 'capcode 123456: Test message', fields: {capcode: '123456'},
            integrity: 'unknown', source_id: 'live:test'
          }];
        }"""
    )
    listen_decode = page.locator("listen-workbench decode-deck")
    expect(listen_decode).to_contain_text("Test message")
    expect(listen_decode).to_contain_text("POCSAG")
    expect(page.locator("instrument-panel #decode-button")).to_have_count(0)
    listen_decode.get_by_role("button", name="All decoded messages").click()
    expect(page.locator("decode-view .overlay-backdrop")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("decode-view .overlay-backdrop")).to_have_count(0)
    page.get_by_role("tab", name="Discover").click()
    # Last decoded output lives in the Listen deck and DECODE chip only.
    expect(page.locator("decode-output-strip")).to_have_count(0)


def test_decoder_setup_groups_protocols_and_shows_explanatory_hover(live_app, page: Page):
    page.goto(live_app.base_url)
    deck = page.locator("listen-workbench decode-deck")
    deck.evaluate("el => el._openSetup()")
    dialog = page.locator("#decode-setup-modal")
    expect(dialog).to_be_visible()
    expect(dialog.locator(".decoder-category-title").first).to_be_visible()
    pocsag = dialog.locator("label.decoder-option", has_text="POCSAG")
    if pocsag.count():
        expect(pocsag).to_have_attribute("title", re.compile("paging decoder"))


def test_band_adsb_has_working_single_channel_shortcut(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.decoders?.length > 0")
    deck = page.locator("listen-workbench decode-deck")
    deck.evaluate("""el => {
      el.decoders = [{id:'adsb',label:'ADS-B / Mode S',demodulators:['IQ'],available:true,
        readiness:{usable:true,installation:{state:'present'},validation:{state:'untested'},session:{state:'idle'}}}];
      document.querySelector('msdr-app').decoders = el.decoders;
      el._openSetup();
    }""")
    dialog = page.locator("#decode-setup-modal")
    expect(dialog.get_by_role("heading", name="Single-channel decode")).to_be_visible()
    experimental = dialog.locator('#decode-channel-list section[aria-label="Experimental"]')
    expect(experimental.locator('input[data-decoder-id="adsb"]')).to_have_count(1)
    single = dialog.locator('#decode-channel-list input[data-decoder-id="adsb"]')
    single.check()
    single.uncheck()
    # Band decode lives in its own tab; the shortcut jumps back to Single-channel.
    dialog.get_by_role("tab", name="Band decode").click()
    expect(dialog.locator("#decode-channel-list")).to_have_count(0)
    expect(dialog.locator('#decode-band-list section[aria-label="Experimental"]')).to_contain_text(
        "ADS-B / Mode S"
    )
    expect(
        dialog.locator("#decode-band-list label", has_text="ADS-B / Mode S").locator("input")
    ).to_be_disabled()
    dialog.get_by_role("button", name="Use ADS-B / Mode S in single-channel decode").click()
    expect(dialog.get_by_role("tab", name="Single-channel decode")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(dialog.locator('#decode-channel-list input[data-decoder-id="adsb"]')).to_be_checked()
    expect(dialog.get_by_role("status")).to_contain_text("1090 MHz")
    assert deck.evaluate("el => Array.from(el._selected)") == ["adsb"]
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
