"""Frequency context remains tentative and launches one explicitly chosen decoder."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_survey_candidate_evidence_and_explicit_decoder_choice(live_app, page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view = document.createElement('survey-view');
      document.body.append(view);
      view.mode = 'discover';
      view.decoders = [{id: 'adsb', label: 'ADS-B / Mode S', available: true,
        demodulators: ['IQ']}];
      view.addEventListener('survey-decode', ev => window.decodeRequest = ev.detail);
      view.status = {active: true, generation: 1,
        span: {center_hz: 1090000000, sample_rate_hz: 2000000}, dimensions: [],
        analyzer: {active: false, targets: []}, labels: [{id: 'aircraft',
          frequency_hz: 1090000000, active_now: true, activity: {kind: 'bursty'},
          decoder_candidates: [{id: 'adsb', label: 'ADS-B / Mode S',
            evidence_kind: 'allocation', reason: '1090 MHz allocation',
            next_action: 'Try decoder with at least 2 MS/s IQ',
            limitations: 'Frequency does not establish a protocol or identity'}]
        }]};
    }""")
    view = page.locator("survey-view")
    # The evidence column rendered the bare evidence category text ("context")
    # until d1e9592 (0.35.0) replaced it with per-source badges; an
    # allocation-only candidate now shows the "Allocation" badge instead.
    expect(view.locator("tbody tr")).to_contain_text("Allocation")
    # The row's candidate cell sits past the fold once survey-view is bare
    # (no app scroll container around it), and survey-view's 1 Hz activity
    # timer (requestUpdate every second) nudges row layout right as a
    # pointer-driven click lands, intermittently landing the hit-test on the
    # cell instead of the button -- dispatch the click directly on the
    # button element instead of simulating a pointer click at a screen point.
    view.get_by_role("button", name="1 decoder options").dispatch_event("click")
    expect(view).to_contain_text("frequency context: 1090 MHz allocation")
    expect(view).to_contain_text("Frequency does not establish a protocol")
    view.get_by_role("button", name="TRY ADS-B / Mode S").dispatch_event("click")
    assert page.evaluate("window.decodeRequest") == {
        "frequency_hz": 1090000000,
        "decoders": ["adsb"],
    }
