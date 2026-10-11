"""Survey controls remain visible without horizontal scrolling in narrow panes."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [360, 640, 1024, 1600])
def test_survey_controls_wrap_with_all_actions_visible(live_app, page, width):
    page.set_viewport_size({"width": max(800, width + 40), "height": 1000})
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('survey-view')")
    page.evaluate("""width => {
      document.querySelector('msdr-app').remove();
      const el=document.createElement('survey-view');
      el.style.cssText=`display:block;width:${width}px;max-width:100%`;
      el.mode='discover'; el.connected=true; el.canControl=true;
      el.wfSettings={peak_threshold_db:10,marker_hold_s:15};
      el.status={active:true,since:Date.now()/1000-2500,labels:[],dimensions:[],
        stream:{fps:22.6},sample_rates_hz:[6000000],
        span:{center_hz:163500000,sample_rate_hz:6000000},presets:[],
        analyzer:{active:false,targets:[]}};
      document.body.append(el);
    }""", width)
    view=page.locator('survey-view')
    toolbar=view.locator('.survey-toolbar-scroll')
    expect(view.get_by_role('button', name='Zoom spectrum in', exact=True)).to_be_visible()
    for label in ['Survey sample rate / width', 'Survey span preset',
                  'DSP peak threshold, dB over noise floor', 'Survey center frequency (MHz)']:
        expect(view.get_by_label(label, exact=True)).to_be_visible()
    view.get_by_role('button', name='Zoom spectrum in', exact=True).click()
    expect(view.get_by_label('Pan spectrum', exact=True)).to_be_visible()
    overflow = view.evaluate("""el => {
      const selectors=['.survey-topbar','.survey-toolbar-scroll','.spectrum-toolbar'];
      return selectors.flatMap(selector=>{
        const box=el.querySelector(selector), r=box.getBoundingClientRect();
        const bad=[...box.querySelectorAll('button,input,select')].filter(control=>{
          const c=control.getBoundingClientRect();
          return c.width && (c.left<r.left-1 || c.right>r.right+1);
        });
        return box.scrollWidth>box.clientWidth+1 || bad.length ? [selector] : [];
      });
    }""")
    assert overflow == []
    expect(toolbar.get_by_role('group', name='Spectrum levels')).to_be_visible()
    if width == 1024:
        toolbar.screenshot(path='/tmp/msdr-survey-toolbar.png')
