"""Saved receiver context and allocation follow the actual tuned frequency."""

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_saved_frequency_and_allocation_near_readout_and_inspector(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.complete")
    response = page.request.post(f"{live_app.base_url}/api/tune/frequency", data={"hz": 1089974844})
    assert response.ok
    page.wait_for_function(
        "document.querySelector('msdr-app').state.props.device_vfo_frequency === 1089974844"
    )
    page.evaluate("""() => {
      const app = document.querySelector('msdr-app');
      app.signals = [{id:'saved-air', name:'My aircraft receiver',
        frequency_hz:1089974844, demodulator:'RAW'}];
      app.favorites = [...app.signals];
    }""")
    saved = page.get_by_label('Saved frequency matches')
    expect(saved).to_contain_text('My aircraft receiver')
    expect(saved).to_contain_text('★ Favorite')
    expect(saved).to_contain_text('RAW')
    # The pill beside the digits is a labelled "Allocation" chip carrying the
    # regulatory summary in its title; the band-plan strip shows the top
    # channel/application hint, with every hint ("Aviation radionavigation
    # (DME / transponders)") in its title.
    allocation = page.locator('instrument-panel button.frequency-allocation')
    expect(allocation).to_have_attribute('title', re.compile('AERONAUTICAL RADIONAVIGATION'))
    hints = page.locator('instrument-panel .frequency-context-strip .band-plan-hints')
    expect(hints).to_have_attribute(
        'title', re.compile(re.escape('Aviation radionavigation (DME / transponders)'))
    )
    # The same offline band plan is visible immediately below the inspector frequency.
    page.evaluate("document.querySelector('msdr-app').openSignal({frequency_hz:1089974844})")
    header = page.locator('.signal-panel-header')
    header_allocation = header.get_by_label('Signal band allocation')
    expect(header_allocation).to_contain_text('AERONAUTICAL RADIONAVIGATION')
    header_hints = header.get_by_label('Channel / application hints')
    expect(header_hints).to_contain_text('Aviation radionavigation (DME / transponders)')
    response = page.request.post(
        f"{live_app.base_url}/api/tune/frequency", data={"hz": 146520000}
    )
    assert response.ok
    expect(saved).to_have_count(0)
    expect(allocation).to_have_attribute('title', re.compile('AMATEUR'))
    expect(hints).to_contain_text('2 m calling')
    expect(hints).to_have_attribute('title', re.compile('Amateur radio'))


def test_measured_signal_shows_nearby_saved_channel_without_claiming_identity(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state")
    page.evaluate("""async () => {
      const app = document.querySelector('msdr-app');
      app.signals = [{id:'adsb', name:'ADS-B / Mode S', frequency_hz:1090000000}];
      app.favorites = [...app.signals];
      // app.js binds <survey-view .status=${this.state?.survey}>, so setting
      // survey.status directly gets clobbered by the next app render; go
      // through app.state instead. active + stream.connected mark the
      // marker as live rather than a retained/stale one (see
      // survey-view.js's _spectrumLabels), which would otherwise drop it
      // once its age exceeds the marker hold; the marker must also appear
      // in spectrum_markers, which _spectrumLabels reads in preference to
      // labels when both are present.
      const measured = {id:'measured', frequency_hz:1089967000, snr_db:26.5, active_now:true};
      app.state = {...app.state, survey: {...app.state.survey, active: true,
        stream: {...app.state.survey?.stream, connected: true},
        labels: [measured], spectrum_markers: [measured]}};
      await app.updateComplete;
      const survey = document.querySelector('survey-view');
      survey._hoverHz = 1089967000;
      survey._hoverNearest = {withinRadius:true, label:measured};
    }""")
    hover = page.locator('survey-view .survey-hover-card')
    expect(hover).to_contain_text('Nearby saved ★ · ADS-B / Mode S')
    expect(hover).to_contain_text('1090.000000 MHz')
    expect(hover).to_contain_text('33.0 kHz away')
    expect(hover).to_contain_text('Unclassified signal')
    page.evaluate("document.querySelector('msdr-app').openSignal({frequency_hz:1089967000})")
    inspector = page.locator('signal-panel')
    expect(inspector.get_by_label('Nearby saved channel')).to_contain_text('ADS-B / Mode S')
    expect(inspector.get_by_label('Nearby saved channel')).to_contain_text('33.0 kHz away')

def test_receiver_context_shows_catalog_once_on_one_line(live_app, page: Page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.can_control")
    response = page.request.post(
        f"{live_app.base_url}/api/tune/frequency", data={"hz": 1090000000}
    )
    assert response.ok
    page.wait_for_function(
        "document.querySelector('msdr-app').state.props.device_vfo_frequency === 1090000000"
    )
    page.evaluate("""async () => {
      const app = document.querySelector('msdr-app');
      app.signals = [{id:'plane',name:'PLANES ADS-B',frequency_hz:1090000000}];
      app.favorites = [...app.signals];
      await app.updateComplete;
      const readout = document.querySelector(
        'instrument-panel .receiver-context-line detect-readout'
      );
      clearTimeout(readout._timer);
      readout._reqSeq++;
      readout.candidates = [{provider:'catalog',name:'ADS-B',category:'bc'}];
      await readout.updateComplete;
    }""")
    context = page.locator("instrument-panel .receiver-context-line")
    expect(context.get_by_label("Saved frequency matches")).to_contain_text("PLANES ADS-B")
    row = context.locator(".detect-readout > .row")
    expect(row).to_contain_text("catalog match · reference only")
    expect(row).not_to_contain_text("ADS-B")
    expect(row).to_have_attribute("title", "ADS-B")
    saved_y = context.locator(".tuned-saved-context").bounding_box()["y"]
    catalog_y = row.bounding_box()["y"]
    assert abs(saved_y - catalog_y) < 8
