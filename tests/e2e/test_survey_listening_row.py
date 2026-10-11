"""Receiver location remains visible through live updates and table interaction."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize('surface', ['spectrum', 'waterfall'])
def test_clicked_frequency_stays_marked_until_receiver_retunes(live_app, page: Page, surface):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view = document.createElement('survey-view');
      document.body.append(view); view.mode = 'listen';
      view.showMlControls = false;
      view.connected = true; view.canControl = true;
      view.status = {active:true, generation:1,
        span:{center_hz:100e6,sample_rate_hz:1e6,vfo_hz:99700000},
        dimensions:[], resolution_hz:1953.125,
        labels:[{id:'a',frequency_hz:99900000,snr_db:20,active_now:true},
                {id:'b',frequency_hz:100100000,snr_db:15,active_now:true}]};
      view.frame = {...view.status.span, bins:Array(512).fill(-100)};
      // Model the confirmed receiver status returned after a successful tune.
      view.addEventListener('tune-frequency', event => {
        view.status = {...view.status, span:{...view.status.span,vfo_hz:event.detail.hz}};
      });
    }""")
    view = page.locator('survey-view')
    canvas = view.locator('canvas.survey-canvas')
    bounds = canvas.bounding_box()
    assert bounds
    canvas.click(position={'x': bounds['width'] * .4,
                           'y': bounds['height'] * (.30 if surface == 'spectrum' else .85)})
    row_a = view.locator('tr[data-label-id="a"]')
    row_b = view.locator('tr[data-label-id="b"]')
    expect(row_a).to_have_attribute('aria-current', 'true')
    expect(row_a.get_by_label('Listening', exact=True)).to_be_visible()

    # Inspecting something else must not move the receiver's listening mark.
    view.evaluate("el => {el._selectLabel('b')}")
    expect(row_a).to_have_attribute('aria-current', 'true')
    row_a.hover()
    view.evaluate("""async el => {
      for (let i=0; i<12; i++) {
        el.status = {...el.status, labels:el.status.labels.map(l => ({...l,snr_db:10+i}))};
        await el.updateComplete;
      }
    }""")
    page.wait_for_timeout(1700)  # Exceeds the previous transient flash duration.
    expect(row_a).to_have_attribute('aria-current', 'true')
    assert row_a.evaluate('el => getComputedStyle(el).animationName') == 'none'
    assert row_a.evaluate('el => getComputedStyle(el).boxShadow') != 'none'

    # Even while pointer/focus freezes measurements, a real retune must move it.
    view.evaluate("el => {el.status={...el.status,span:{...el.status.span,vfo_hz:100100000}}}")
    expect(row_b).to_have_attribute('aria-current', 'true')
    expect(row_a).to_have_attribute('aria-current', 'false')
    expect(row_b.get_by_label('Listening', exact=True)).to_be_visible()

    page.mouse.move(0, 0)
    view.evaluate("el => {el.status={...el.status,labels:[]}}")
    expect(view.locator('tbody tr[data-label-id]')).to_have_count(2)
    expect(row_b).to_have_attribute('aria-current', 'true')
    expect(row_b).to_have_class('gone selected')
    # Quiet rows retain their order and the selected receiver location.
    assert view.locator('tbody tr[data-label-id]').evaluate_all(
        'rows => rows.map(row => row.dataset.labelId)'
    ) == ['a', 'b']
