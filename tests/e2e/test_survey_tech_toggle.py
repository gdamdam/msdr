"""The table's technical detail switch works independently of workspace options."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("mode", ["listen", "discover"])
def test_tech_toggle_is_visible_persistent_and_controls_table_columns(live_app, page: Page, mode):
    page.goto(live_app.base_url)
    page.evaluate("""mode => {
      document.querySelector('msdr-app').remove();
      localStorage.setItem('msdr.survey.tech', '0');
      localStorage.setItem('msdr.survey.options', '0');
      const view = document.createElement('survey-view');
      view.mode = mode;
      view.status = {active:true, generation:1,
        span:{center_hz:162.425e6,sample_rate_hz:1e6},
        dimensions:['bandwidth','modulation','symbol_rate','voice'],
        labels:[{id:'voice', frequency_hz:162425000, snr_db:25, active_now:true}]};
      document.body.append(view);
    }""", mode)
    view = page.locator('survey-view')
    button = view.get_by_role('button', name='TECH OFF', exact=True)
    expect(button).to_be_visible()
    expect(view.locator('th').filter(has_text='SNR')).to_have_count(0)
    button.click()
    expect(view.get_by_role('button', name='TECH ON', exact=True)).to_have_attribute(
        'aria-pressed', 'true'
    )
    for heading in ['SNR', 'MOD', 'BAUD', 'DUTY']:
        expect(view.get_by_role('columnheader', name=heading, exact=True)).to_be_visible()
    assert page.evaluate("localStorage.getItem('msdr.survey.tech')") == '1'

    # A fresh component, like a page reload, must keep the user's preference.
    view.evaluate("""old => {
      const next = document.createElement('survey-view');
      next.mode = old.mode; next.status = old.status;
      old.replaceWith(next);
    }""")
    expect(view.get_by_role('button', name='TECH ON', exact=True)).to_be_visible()
    view.get_by_role('button', name='TECH ON', exact=True).click()
    expect(view.get_by_role('button', name='TECH OFF', exact=True)).to_be_visible()
    expect(view.locator('th').filter(has_text='SNR')).to_have_count(0)
    assert page.evaluate("localStorage.getItem('msdr.survey.tech')") == '0'
