"""Signal panel DECODE uses the channel decoder; Decode strip ⚗ / ◎ probes."""

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_signal_panel_decode_starts_channel_decoder_even_with_survey(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('signal-panel')")
    events = page.evaluate("""() => {
      const events = [];
      customElements.get('signal-panel').prototype._decode.call({
        _detail: {frequency_hz: 144390000}, _decodeDecoders: () => ['aprs'], surveyActive: true,
        dispatchEvent: (ev) => events.push([ev.type, ev.detail]),
      });
      return events;
    }""")
    assert events == [["decode-start", {"decoders": ["aprs"], "frequency_hz": 144390000}]]


def test_decode_strip_probe_buttons(live_app, page: Page):
    page.route("**/api/survey/test-signal", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"outcome": "matched", "frequency_hz": 144390000, "frames": 40,
                         "results": [{"id": "aprs", "label": "APRS", "state": "validated",
                                      "valid_frames": 3}]}),
    ))
    page.goto(live_app.base_url)
    deck = page.locator("listen-workbench decode-deck")
    deck.evaluate("el => { el.surveyActive = false; el.frequencyHz = 144390000; }")
    test_button = deck.get_by_role("button", name="Test signal")
    expect(test_button).to_be_disabled()
    assert "Start the survey first" in test_button.get_attribute("title")
    expect(deck.get_by_role("button", name="Optimize decode")).to_be_disabled()

    deck.evaluate("el => { el.surveyActive = true; el.frequencyHz = 144390000; }")
    expect(test_button).to_be_enabled()
    test_button.click()
    expect(deck.locator(".decode-probe-line")).to_contain_text("Test: APRS ✓ 3")
    deck.get_by_role("button", name="Dismiss result").click()
    expect(deck.locator(".decode-probe-line")).to_have_count(0)
