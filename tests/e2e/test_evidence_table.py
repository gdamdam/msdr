# ruff: noqa: E501 -- embedded JavaScript test data
"""The compact survey Evidence header remains both filterable and sortable."""

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_signals_in_view_is_collapsible_and_keeps_live_count(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const el = document.createElement('survey-view'); document.body.append(el);
      el.mode = 'listen';
      el.status = {active:true, generation:1,
        span:{center_hz:100000000,sample_rate_hz:1000000},
        dimensions:[], analyzer:{active:false,targets:[]}, labels:[
          {id:'a',frequency_hz:100100000,active_now:true,activity:{kind:'steady'}}]};
    }""")
    details = page.locator("survey-view .survey-signal-shortcuts")
    expect(details).to_have_attribute("aria-label", "Signals in view")
    expect(details.locator("summary")).to_contain_text("Signals in view (1)")
    expect(details.locator("summary")).not_to_contain_text("MHz")
    expect(details.locator(".survey-badge-lane")).not_to_be_visible()

    details.locator("summary").click()
    expect(details.locator(".survey-badge-lane")).to_be_visible()
    expect(details.locator(".survey-badge-lane")).to_contain_text("MHz · ● active / ○ quiet · ⓘ details")
    # <details> dispatches `toggle` on a later task, so the localStorage write
    # trails the synchronous DOM change; poll rather than read immediately.
    page.wait_for_function("() => localStorage.getItem('msdr.survey.shortcuts') === '1'")
    page.evaluate("""() => {
      const el = document.querySelector('survey-view');
      el.status = {...el.status, labels:[...el.status.labels,
        {id:'b',frequency_hz:100200000,active_now:false,activity:{kind:'quiet'}}]};
    }""")
    expect(details.locator("summary")).to_contain_text("Signals in view (2)")
    expect(details).to_have_attribute("open", "")
    details.locator("summary").click()
    expect(details.locator(".survey-badge-lane")).not_to_be_visible()
    page.wait_for_function("() => localStorage.getItem('msdr.survey.shortcuts') === '0'")


def test_signals_in_view_remembers_open_state_across_mounts(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("() => localStorage.setItem('msdr.survey.shortcuts', '1')")
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const el = document.createElement('survey-view'); document.body.append(el);
      el.mode = 'listen';
      el.status = {active:true, generation:1,
        span:{center_hz:100000000,sample_rate_hz:1000000},
        dimensions:[], analyzer:{active:false,targets:[]}, labels:[]};
    }""")
    details = page.locator("survey-view .survey-signal-shortcuts")
    expect(details).to_have_attribute("open", "")


def test_evidence_filter_is_in_header_and_does_not_trigger_sort(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate(
        """() => {
          // Use the real registered component without the live app above it;
          // the app's websocket status refresh otherwise replaces test rows.
          document.querySelector("msdr-app").remove();
          const el = document.createElement("survey-view");
          document.body.append(el);
          el.mode = "listen";
          el._optionsOpen = false;
          el.status = {
            active: true,
            generation: 1,
            span: {center_hz: 102000000, sample_rate_hz: 1000000},
            resolution_hz: 1953.125,
            dimensions: [],
            analyzer: {active: false, targets: []},
            labels: [
              {id: "none", frequency_hz: 100000000, active_now: true, activity: {kind: "steady"}},
              {id: "context", frequency_hz: 101000000, active_now: true, activity: {kind: "steady"}, bandplan: {name: "Allocation"}},
              {id: "estimate", frequency_hz: 102000000, active_now: true, activity: {kind: "steady"}, family: {name: "Candidate"}},
              {id: "output", frequency_hz: 103000000, active_now: true, activity: {kind: "steady"}, confirmed: {decoder: "aprs", integrity: "failed"}},
              {id: "validated", frequency_hz: 104000000, active_now: true, activity: {kind: "steady"}, confirmed: {decoder: "aprs", integrity: "passed"}},
            ],
          };
        }"""
    )
    view = page.locator("survey-view")
    evidence_filter = view.get_by_role("combobox", name="Filter survey rows by evidence")
    expect(evidence_filter).to_be_visible()
    expect(view.locator("tbody tr")).to_have_count(5)

    evidence_filter.select_option("validated")
    expect(view.locator("tbody tr")).to_have_count(1)
    expect(view.locator("tbody tr").first).to_have_attribute("data-label-id", "validated")
    assert view.locator("th").filter(has_text="EVIDENCE").get_attribute("aria-sort") == "none"
    assert view.evaluate("(el) => el._sortKey") == "frequency_hz"

    evidence_filter.select_option("all")
    header = view.locator("th").filter(has_text="EVIDENCE")
    header.click(position={"x": 12, "y": 12})
    expect(header).to_have_attribute("aria-sort", "descending")
    assert view.locator("tbody tr").evaluate_all(
        '(rows) => rows.map((row) => row.dataset.labelId)'
    ) == ["validated", "output", "estimate", "context", "none"]

    header.click(position={"x": 12, "y": 12})
    expect(header).to_have_attribute("aria-sort", "ascending")
    assert view.locator("tbody tr").evaluate_all(
        '(rows) => rows.map((row) => row.dataset.labelId)'
    ) == ["none", "context", "estimate", "output", "validated"]


def test_source_colors_match_spectrum_and_checked_output(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const el = document.createElement('survey-view'); document.body.append(el);
      el.mode = 'listen';
      el.status = {active:true, generation:1, span:{center_hz:160e6,sample_rate_hz:6e6},
        dimensions:[], analyzer:{active:false,targets:[]}, labels:[
          {id:'mixed',frequency_hz:160e6,snr_db:12,active_now:true,activity:{kind:'steady'},
           bandplan:{name:'Allocation'},catalog:[{name:'Scheduled'}],
           confirmed:{decoder:'dsc',integrity:'failed'}}]};
    }""")
    row = page.locator('tr[data-label-id="mixed"]')
    expect(row.locator('.survey-evidence-badge')).to_have_count(4)
    expect(row.locator('[data-evidence-source="decoded"]')).to_contain_text('⚠ Decoded')
    expect(page.locator('.survey-badge-lane')).not_to_contain_text('✓')
    result = page.locator('survey-view').evaluate("""el => {
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d'); const drawn=[];
      ctx.fillText = function(text,x,y) { drawn.push({text,x,color:this.fillStyle}); };
      el._drawEvidenceMarkers(ctx,el.status.span,600,{specTop:0,specH:150,dpr:1});
      const badges=[...el.querySelectorAll('tbody .survey-evidence-badge')];
      return {drawn, colors:badges.map(b=>b.style.getPropertyValue('--evidence-color'))};
    }""")
    assert [d['text'] for d in result['drawn']] == ['DSP', 'A', 'S', 'D']
    assert [d['color'] for d in result['drawn']] == result['colors']
    assert len({d['x'] for d in result['drawn']}) == 1
    assert all(abs(d['x'] - 300) < 14 for d in result['drawn'])
    page.get_by_text('Evidence colors / spectrum markers', exact=True).click()
    expect(page.locator('.survey-evidence-legend')).to_contain_text('do not confirm reception')


def test_analysis_freshness_is_explicit_on_survey_and_signal_inspector(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const el = document.createElement('survey-view'); document.body.append(el);
      el.mode = 'discover';
      el.status = {active:true, generation:1, span:{center_hz:101.5e6,sample_rate_hz:1e6},
        dimensions:['modulation','voice'],
        analyzer:{active:true,targets:[],candidate_frequencies_hz:[101e6]}, labels:[
          {id:'new',frequency_hz:100e6,active_now:true,analysis_state:{state:'not_analyzed'}},
          {id:'waiting',frequency_hz:101e6,active_now:true,
            analysis_state:{state:'waiting',previous_name:'Data-like signal'}},
          {id:'current',frequency_hz:102e6,active_now:true,family:{name:'Speech-like NFM'},
            analyzed_at:Date.now()/1000,analysis_state:{state:'current_result',refresh_pending:true}},
          {id:'historical',frequency_hz:103e6,active_now:true,
            last_classification:{name:'Data-like signal',at:Date.now()/1000-600},
            analysis_state:{state:'historical_result',name:'Data-like signal',at:Date.now()/1000-600}}
        ]};
    }""")
    view = page.locator('survey-view')
    for signal_id, state, text in [
        ('new', 'not_analyzed', 'Not analyzed'),
        ('waiting', 'waiting', 'Waiting'),
        ('current', 'current_result', 'Current result'),
        ('historical', 'historical_result', 'Historical result'),
    ]:
        freshness = view.locator(f'tr[data-label-id="{signal_id}"] .survey-analysis-state')
        expect(freshness).to_have_attribute('data-analysis-state', state)
        expect(freshness).to_contain_text(text)
    expect(view.locator('tr[data-label-id="current"] .survey-analysis-state')).to_contain_text('Refresh queued')

    response = {
        'frequency_hz': 101e6, 'memory': None, 'find': None,
        'survey_label': {'frequency_hz':101e6,
          'analysis_state':{'state':'waiting','previous_name':'Data-like signal'},
          'last_classification':{'name':'Data-like signal','at':1}},
        'last_classification':{'name':'Data-like signal','at':1},
        'evidence':[], 'messages':[], 'observations':[], 'suggested_decoders':[],
        'demodulator':None, 'bandwidth_hz':None,
    }
    page.route('**/api/signal-detail**', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(response)
    ))
    page.evaluate("""() => {
      const panel = document.createElement('signal-panel');
      panel.target={frequency_hz:101e6}; panel.open=true; document.body.append(panel);
    }""")
    inspector = page.locator('signal-panel')
    status = inspector.locator('[data-analysis-state="waiting"]').filter(has_text='DSP analysis')
    expect(status).to_contain_text('DSP analysis · Waiting')
    history = inspector.locator('.inspector-analysis-history')
    expect(history).to_contain_text('Previous DSP classification')
    history.locator('summary').click()
    expect(history).to_contain_text('Data-like signal')


def test_shifted_evidence_marker_uses_underlying_frequency_anchor(live_app, page: Page):
    page.goto(live_app.base_url)
    result = page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const view = document.createElement('survey-view'); document.body.append(view);
      view.mode='listen'; view._snapOn=false; view.connected=true; view.canControl=true;
      // Peaks at/under the default 10 dB threshold are hidden unless
      // "retained" (see survey-view.js's _spectrumLabels); disable that
      // filter so both markers stay visible like the rest of this test
      // assumes.
      view.wfSettings = {peak_threshold_db: 0};
      const labels=[
        {id:'left',frequency_hz:100e6,snr_db:10,active_now:true},
        {id:'right',frequency_hz:100015000,snr_db:12,active_now:true}
      ];
      view.status={active:true,span:{center_hz:100e6,sample_rate_hz:1e6},labels};
      view._layout={w:1000,dpr:1,specH:100,specTop:0};
      const canvas={getBoundingClientRect:()=>({left:0,top:0,width:1000,height:200})};
      const tuned=[]; const opened=[];
      view.addEventListener('tune-frequency', ev=>tuned.push(ev.detail.hz));
      view.addEventListener('signal-open', ev=>opened.push(ev.detail.frequency_hz));
      // The right signal's tag is displaced from its x=515 anchor to x=528.
      const marker=view._markerLabelAtPoint(528,12,canvas);
      view._queueCanvasClick({currentTarget:canvas,clientX:528,clientY:12,detail:1,
        shiftKey:false,altKey:false});
      // Remove the live label before the delayed click fires; it must use the
      // signal captured at pointer time instead of remapping shifted x=528.
      view.status={...view.status,labels:[]};
      return new Promise(resolve=>setTimeout(()=>{
        view.status={...view.status,labels};
        view._layout={w:1000,dpr:1,specH:100,specTop:0};
        view._onCanvasDoubleClick({currentTarget:canvas,clientX:528,clientY:12});
        resolve({marker:marker?.frequency_hz,tuned,opened,
          raw:view._markerLabelAtPoint(528,100,canvas)});
      },400));
    }""")
    assert result['marker'] == 100015000
    assert result['tuned'] == [100015000]
    assert result['opened'] == [100015000]


def test_spectrum_levels_are_before_snap(live_app, page: Page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_app.base_url)
    toolbar = page.locator(".spectrum-toolbar")
    # Levels now live in the Display popover, which follows the Snap/trace
    # controls in the Detect row; open it to reach the min/max sliders.
    toolbar.get_by_role("button", name="Display ▾").click()
    assert toolbar.locator("input[type=range]").count() >= 2
    assert toolbar.evaluate("""el => {
      const min = el.querySelector('input[type=range]');
      const snap = [...el.querySelectorAll('button')].find(b => b.textContent.trim() === 'Snap');
      return !!(snap.compareDocumentPosition(min) & Node.DOCUMENT_POSITION_FOLLOWING);
    }""")


def test_setup_recording_folder_sends_partial_patch(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate('''async () => {
      document.querySelector("msdr-app").remove();
      const el = document.createElement("setup-drawer");
      document.body.append(el);
      el.open = true;
      await el.updateComplete;
      await el._load();
    }''')
    captured = []

    def settings_route(route):
        if route.request.method != "PATCH":
            route.continue_()
            return
        captured.append(route.request.post_data_json)
        route.fulfill(json={"recorder_root": "/tmp/msdr-recordings"})

    page.route("**/api/settings", settings_route)
    drawer = page.locator("setup-drawer")
    # Setup sections are a jump-to accordion (see setup-drawer.js's
    # _jumpToSection); open "Recordings" before reaching its field.
    drawer.get_by_role("button", name="Recordings", exact=True).click()
    drawer.get_by_label("Recording output folder", exact=True).fill("/tmp/msdr-recordings")
    drawer.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("document.querySelector('setup-drawer')._orig.recorder_root === '/tmp/msdr-recordings'")
    assert captured == [{"recorder_root": "/tmp/msdr-recordings"}]


def test_survey_center_field_uses_mhz_even_above_1000(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate('''() => {
      document.querySelector("msdr-app").remove();
      const el = document.createElement("survey-view");
      document.body.append(el);
      el.connected = true;
      el.status = {active: true, dimensions: [], labels: [],
        sample_rates_hz: [6000000, 12000000],
        span: {center_hz: 1092400000, sample_rate_hz: 6000000, vfo_hz: 1090000000}};
      window.appliedSpan = null;
      el.addEventListener('survey-span', e => window.appliedSpan = e.detail);
    }''')
    view = page.locator("survey-view")
    field = view.get_by_role("textbox", name="Survey center frequency (MHz)")
    for value in ("1090", "1090.000000", "1090000 kHz", "1090000000 Hz"):
        field.fill(value)
        field.press("Enter")
        assert page.evaluate("window.appliedSpan.center_hz") == 1090000000
    field.fill("not a frequency")
    field.press("Enter")
    assert page.evaluate("window.appliedSpan.center_hz") == 1090000000
    field.fill("160.000")
    field.press("Enter")
    assert page.evaluate("window.appliedSpan.center_hz") == 160000000
    view.get_by_role("combobox", name="Survey sample rate / width").select_option("12000000")
    assert page.evaluate("window.appliedSpan") == {
        "sample_rate_hz": 12000000, "center_hz": 160000000, "keep_vfo": True,
    }


def test_survey_step_buttons_move_half_sampled_span(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const el = document.createElement('survey-view'); document.body.append(el);
      el.connected = true;
      el.status = {active:true, span:{center_hz:162000000,sample_rate_hz:6000000},
        sample_rates_hz:[6000000], labels:[], dimensions:[]};
      window.spanChanges = [];
      el.addEventListener('survey-span', event => window.spanChanges.push(event.detail));
    }""")
    view = page.locator('survey-view')
    left = view.get_by_role('button', name='Step survey left by half the sampled span')
    right = view.get_by_role('button', name='Step survey right by half the sampled span')
    step_group = view.get_by_role('group', name='Shift survey span')
    expect(step_group).to_have_attribute('aria-label', 'Shift survey span')
    # Buttons show only the arrow glyph (see survey-layout.css's wrapping
    # toolbar fix); the step amount lives in their title and the shared
    # step-size readout instead.
    expect(left).to_have_attribute('title', 'Move survey center left by 3.000 MHz (50% overlap)')
    expect(right).to_have_attribute('title', 'Move survey center right by 3.000 MHz (50% overlap)')
    expect(step_group.locator('.survey-step-size')).to_contain_text('Center · step 3.000 MHz')
    center = step_group.get_by_role('textbox', name='Survey center frequency (MHz)')
    expect(center).to_have_value('162.000000')
    expect(view.get_by_role('textbox', name='Survey center frequency (MHz)')).to_have_count(1)
    expect(left).to_be_enabled()
    expect(right).to_be_enabled()
    left.click()
    right.click()
    right.click()
    assert page.evaluate('window.spanChanges') == [
        {'sample_rate_hz': 6000000, 'center_hz': 159000000, 'keep_vfo': True},
        {'sample_rate_hz': 6000000, 'center_hz': 162000000, 'keep_vfo': True},
        {'sample_rate_hz': 6000000, 'center_hz': 165000000, 'keep_vfo': True},
    ]
    expect(center).to_have_value('165.000000')
    center.fill('166.250000')
    center.press('Enter')
    assert page.evaluate('window.spanChanges.at(-1)') == {
        'sample_rate_hz': 6000000, 'center_hz': 166250000, 'keep_vfo': True,
    }
    expect(view.get_by_role('textbox', name='Survey center frequency (MHz)')).to_have_count(1)
    view.evaluate('el => { el.status = {...el.status, span:{...el.status.span, center_hz:165000000}}; }')
    expect(center).to_have_value('165.000000')
    view.evaluate('el => { el.connected = false; }')
    expect(left).to_be_disabled()
    expect(right).to_be_disabled()
