"""Peak sensitivity saves without resetting history and bounds step controls."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_peak_presets_steps_and_marker_filter(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.settings?.survey")
    control = page.get_by_role("combobox", name="DSP peak threshold, dB over noise floor")
    expect(control).to_be_visible()
    control.select_option("30")
    page.wait_for_function("() => !document.querySelector('msdr-app')._surveyPeakBusy")
    expect(control).to_have_value("30")
    assert live_app.service.settings.survey.peak_threshold_db == 30
    page.get_by_role("button", name="Decrease DSP peak threshold").click()
    expect(control).to_have_value("29")
    page.get_by_role("button", name="Increase DSP peak threshold").click()
    expect(control).to_have_value("30")
    control.select_option("60")
    expect(page.get_by_role("button", name="Increase DSP peak threshold")).to_be_disabled()
    control.select_option("0")
    expect(page.get_by_role("button", name="Decrease DSP peak threshold")).to_be_disabled()
    control.select_option("20")
    expect(control).to_have_value("20")
    assert page.locator("survey-view").evaluate("""view => {
      view.status = {...view.status, active:true, stream:{connected:true},
        spectrum_markers: undefined, labels:[
        {id:'weak',frequency_hz:155e6,snr_db:10},
        {id:'strong',frequency_hz:156e6,snr_db:25}]};
      return view._spectrumLabels().map(label=>label.id);
    }""") == ["strong"]
    assert page.locator("survey-view").evaluate("view => view.status.labels.length") == 2


def test_marker_hold_and_optional_center_exclusion_persist(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.settings?.survey")
    hold = page.get_by_label("DSP marker hold", exact=True)
    expect(hold).to_have_value("15")
    hold.select_option("30")
    page.wait_for_function("() => !document.querySelector('msdr-app')._surveyPeakBusy")
    assert live_app.service.settings.survey.marker_hold_s == 30
    assert live_app.service.settings.survey.center_exclusion_enabled is False
    assert page.locator('survey-view').get_by_label('Exclude receiver center').count() == 0

    def open_survey_settings():
        page.evaluate("document.querySelector('msdr-app').setupOpen = true")
        drawer = page.locator('setup-drawer')
        drawer.locator('[data-setup-section="setup-survey"]').click()
        return drawer

    drawer = open_survey_settings()
    center = drawer.get_by_label("Exclude receiver center", exact=True)
    expect(center).not_to_be_checked()
    center.check()
    drawer.get_by_role('button', name='Save', exact=True).click()
    expect(drawer.get_by_role('dialog')).not_to_be_visible()
    assert live_app.service.settings.survey.center_exclusion_enabled is True
    page.reload()
    expect(hold).to_have_value("30")
    drawer = open_survey_settings()
    center = drawer.get_by_label("Exclude receiver center", exact=True)
    expect(center).to_be_checked()
    center.uncheck()
    drawer.get_by_role('button', name='Save', exact=True).click()
    expect(drawer.get_by_role('dialog')).not_to_be_visible()
    assert live_app.service.settings.survey.center_exclusion_enabled is False
    hold.select_option("0")
    page.wait_for_function("() => !document.querySelector('msdr-app')._surveyPeakBusy")
    assert live_app.service.settings.survey.marker_hold_s == 0
