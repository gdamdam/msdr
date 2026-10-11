"""Current speech filtering and uncapped, peak-anchored spectrum markers."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_speech_filter_expires_and_carrier_stays_visible(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('survey-view')")
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view = document.createElement('survey-view'); document.body.append(view);
      view.status = {active:true, generation:1, dimensions:['voice'],
        span:{center_hz:156e6,sample_rate_hz:2e6}, analyzer:{active:true,targets:[]},
        labels:[
          {id:'speech',frequency_hz:156100000,active_now:true,snr_db:30,
            voice:{kind:'voice'},analyzed_at:Date.now()/1000,analysis_state:{state:'current_result'}},
          {id:'carrier',frequency_hz:156200000,active_now:true,snr_db:40,
            voice:{kind:'silence'},analyzed_at:Date.now()/1000,analysis_state:{state:'current_result'}},
          {id:'unknown',frequency_hz:156300000,active_now:true,snr_db:20},
          {id:'off',frequency_hz:156400000,active_now:false,snr_db:20,
            voice:{kind:'voice'},analyzed_at:Date.now()/1000} ]};
    }""")
    view = page.locator("survey-view")
    expect(view.locator('[data-label-id="carrier"] [data-signal-activity]')).to_have_text(
        "● Carrier only"
    )
    expect(view.locator('[data-label-id="unknown"] [data-signal-activity]')).to_contain_text(
        "speech unknown"
    )
    expect(view.locator('[data-label-id="off"] [data-signal-activity]')).to_have_text("○ No signal")
    view.get_by_label("Speech only", exact=True).check()
    expect(view.locator("tbody tr[data-label-id]")).to_have_count(1)
    expect(view.locator('[data-label-id="speech"]')).to_be_visible()
    # The timer must remove the row even with the pointer inside the table.
    view.locator('[data-label-id="speech"]').hover()
    expect(view.locator("tbody tr[data-label-id]")).to_have_count(0, timeout=6000)
    view.get_by_label("Speech only", exact=True).uncheck()
    expect(view.locator('[data-label-id="speech"] [data-signal-activity]')).to_contain_text(
        "speech unknown"
    )
    expect(view.locator('[data-label-id="carrier"]')).to_be_visible()
    view.locator(".survey-results").screenshot(path="/tmp/msdr-signal-activity.png")


def test_markers_use_all_tracks_and_anchor_to_strongest_bin(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('survey-view')")
    result = page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view=document.createElement('survey-view'); document.body.append(view);
      const markers=Array.from({length:70}, (_,i)=>({id:`p${i}`,frequency_hz:100000+i*1000,
        peak_frequency_hz:100100+i*1000,active_now:true,snr_db:70,bin_lo:i,bin_hi:i}));
      view.wfSettings={peak_threshold_db:60};
      view.status={labels:[], spectrum_markers:markers, resolution_hz:1000};
      const count=view._spectrumLabels().length;
      view.status.spectrum_markers=[{...markers[0], frequency_hz:100500,peak_frequency_hz:101000}];
      const ctx=document.createElement('canvas').getContext('2d'), anchors=[];
      ctx.fillRect=(x,y,w,h)=>{if(w===4 && h===4) anchors.push({x:x+2,y:y+2});};
      view._wfMin=-100; view._wfMax=0; view._avgSmooth=false;
      view._drawEvidenceMarkers(ctx,{center_hz:102000,sample_rate_hz:4000,
        bins:[0,204,0,0],base_db:-100,ref_level_db:0},400,{specTop:0,specH:200,dpr:1});
      view.status.peak_detection={center_exclusion:{start_hz:101500,end_hz:102500}};
      view._hoverHz=102000;view._hoverNearest=null;
      view.requestUpdate();
      return {count,anchors};
    }""")
    assert result["count"] == 70
    assert result["anchors"][0]["x"] == 100
    assert abs(result["anchors"][0]["y"] - 40) < 1
    expect(page.locator(".survey-hover-card")).to_contain_text("peak detection is disabled here")
