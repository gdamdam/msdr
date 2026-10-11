"""ML evidence uses the same stable table and listening mark as DSP rows."""

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_ml_only_rows_keep_listening_mark_and_history(live_app, page: Page):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view = document.createElement('survey-view');
      document.body.append(view); view.mode = 'listen'; view.showMlControls = false;
      const detection = {score:.82,evidence:{label:'item'},stale:false,age_s:1};
      view.status = {active:true,generation:1,
        span:{center_hz:100e6,sample_rate_hz:1e6,vfo_hz:99800000},
        dimensions:[],resolution_hz:1953.125,analyzer:{active:false,targets:[]},
        labels:[{id:'tracked',frequency_hz:100100000,width_hz:12500,
          snr_db:20,active_now:true,activity:{kind:'steady'},
          rfml:{status:'ok',classification:'FM',score:.94,stale:false},
          rfml_detection:detection}],
        ml_proposals:[
          {id:'ml-matched',ml_only:true,frequency_hz:100100000,width_hz:12500,
            low_hz:100093750,high_hz:100106250,rfml_detection:detection},
          {id:'ml-only',ml_only:true,frequency_hz:99800000,width_hz:12500,
            low_hz:99793750,high_hz:99806250,rfml_detection:detection},
          {id:'ml-fragment',ml_only:true,frequency_hz:99800500,width_hz:12500,
            low_hz:99794250,high_hz:99806750,rfml_detection:detection}]};
      view.addEventListener('tune-frequency',event => {
        view.status={...view.status,span:{...view.status.span,vfo_hz:event.detail.hz}};
      });
    }""")
    view = page.locator('survey-view')
    rows = view.locator('tbody tr[data-label-id]')
    expect(rows).to_have_count(2)
    initial_order = rows.evaluate_all('rows=>rows.map(r=>r.dataset.labelId)')
    tracked = view.locator('tr[data-label-id="tracked"]')
    mlrow = view.locator('tr[data-label-id="ml-only"]')
    expect(tracked.locator('[data-evidence-source="ml"]')).to_have_count(2)
    expect(mlrow.locator('[data-evidence-source="ml"]')).to_have_count(1)
    expect(mlrow.locator('[data-evidence-source="dsp"]')).to_have_count(0)
    expect(mlrow).to_have_attribute('aria-current', 'true')
    expect(mlrow.get_by_label('Listening', exact=True)).to_be_visible()
    expect(mlrow).not_to_contain_text('● now')
    expect(mlrow).not_to_contain_text('NaN')

    # A fresh age/status update must not bypass the user's reading freeze.
    mlrow.hover()
    frozen = mlrow.inner_text()
    view.evaluate("""el => {
      el.status={...el.status,ml_proposals:el.status.ml_proposals.map(p =>
        ({...p,rfml_detection:{...p.rfml_detection,stale:true,age_s:90}}))};
    }""")
    expect(mlrow).to_have_text(frozen)
    page.mouse.move(0, 0)
    expect(mlrow.locator('[data-evidence-source="ml"]')).to_contain_text('historical')

    # A vanished proposal is retained, never shown as current RF activity.
    view.evaluate("el => {el.status={...el.status,ml_proposals:[]}}")
    expect(rows).to_have_count(2)
    expect(mlrow).to_have_attribute('aria-current', 'true')
    expect(mlrow.locator('[data-evidence-source="ml"]')).to_contain_text('historical')
    assert rows.evaluate_all('rows=>rows.map(r=>r.dataset.labelId)') == initial_order
    tracked.locator('button.tune').first.click()
    expect(tracked).to_have_attribute('aria-current', 'true')
    expect(mlrow).to_have_attribute('aria-current', 'false')
    view.evaluate("""el => {el.status={...el.status,labels:[...el.status.labels,
      {id:'new-dsp',frequency_hz:99800000,width_hz:12500,snr_db:15,active_now:true}]}}""")
    tracked.locator('button.tune').first.blur()
    page.mouse.move(0, 0)
    expect(rows).to_have_count(2)
    expect(view.locator('tr[data-label-id="new-dsp"]')).to_be_visible()
    assert rows.evaluate_all('rows=>rows.map(r=>r.dataset.labelId)') == [
        'new-dsp' if value == 'ml-only' else value for value in initial_order
    ]
    if directory := os.environ.get('MSDR_REVIEW_OUTPUT_DIR'):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'survey-evidence.png'), full_page=True)
