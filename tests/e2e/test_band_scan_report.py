"""Band-scan report navigation and decoder recovery in a real browser."""

import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_band_scan_report_and_inspector_actions(live_app, page):
    async def seed():
        repo = live_app.service.band_scan.repo
        config = {
            "start_hz": 136000000,
            "end_hz": 138000000,
            "span_hz": 2000000,
            "dwell_s": 3,
            "overlap": 0.25,
            "centers_hz": [137000000],
        }
        repo.create("report-test", config)
        observations = [
            {
                "observed_at": 100 + i,
                "snr_db": 17.5 + (i == 27),
                "activity": {"kind": "bursty"},
                "catalog": [{"name": "Catalog entry"}],
                "bandplan": {"name": "Amateur 2m"},
                "family": {"name": "Test signal"},
            }
            for i in range(28)
        ]
        repo.save(
            "report-test",
            "complete",
            {
                "spans": [
                    {
                        "center_hz": 137000000,
                        "start_hz": 136000000,
                        "end_hz": 138000000,
                        "state": "covered",
                        "fresh_frames": 4,
                    }
                ],
                "results": [
                    {
                        "frequency_hz": 137123000,
                        "span_centers_hz": [137000000],
                        "observations": observations,
                    }
                ],
            },
        )

    live_app.run(seed())
    page.route(
        "**/api/survey/band-scan/report-test/spectrum",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "report-test",
                    "available": True,
                    "start_hz": 136000000,
                    "end_hz": 138000000,
                    "bin_width_hz": 200000,
                    "bins": [
                        {
                            "frequency_hz": 136000000 + i * 200000,
                            "typical_db": -92 + i,
                            "max_db": -87 + i,
                            "sample_count": 3,
                            "first_at": 100,
                            "last_at": 130,
                        }
                        for i in range(10)
                    ],
                    "holes": [
                        {
                            "start_hz": 136800000,
                            "end_hz": 137000000,
                            "reason": "Uncaptured or center-excluded",
                        }
                    ],
                    "slices": [
                        {
                            "at": 100,
                            "center_hz": 137000000,
                            "span_hz": 2000000,
                            "first_bin_hz": 136000000,
                            "bin_width_hz": 100000,
                            "center_exclusion_bins": 2,
                            "values_db": [
                                -90,
                                -88,
                                -87,
                                -85,
                                -86,
                                -88,
                                -90,
                                -91,
                                -94,
                                -96,
                                None,
                                None,
                                None,
                                -91,
                                -89,
                                -87,
                                -84,
                                -86,
                                -88,
                                -90,
                            ],
                        }
                    ],
                    "time_history_sampled": True,
                    "slice_interval_s": 1,
                }
            ),
        ),
    )
    page.goto(live_app.base_url)
    tabs = page.locator(".workspace-tab")
    names = tabs.all_text_contents()
    assert names.index("MAP") < names.index("BAND SCANS") < names.index("REVIEW")
    page.locator(".workspace-tab", has_text="DISCOVER").click()
    survey = page.locator("survey-view").first
    survey.evaluate("""el => { el._bandScan = {current:{id:'report-test',state:'complete',
      completed_count:1,total:1,covered_count:1,uncovered_count:0,result_count:1}}; }""")
    survey.locator("details.survey-band-scan summary").click()
    survey.get_by_role("button", name="View report").click()
    expect(page.locator(".workspace-tab.active")).to_have_text("BAND SCANS")
    report = page.locator("band-scan-report")
    expect(report).to_contain_text("137.123000")
    expect(report).to_contain_text("Test signal")
    expect(report.locator(".band-report-frequency-table tbody tr").first).to_have_class(
        "band-report-row-clue"
    )
    expect(report.locator(".band-report-frequency-table tbody tr").first).to_contain_text(
        "Analysis clue"
    )
    assert (
        report.locator(".band-report-frequency-table tbody tr").first.evaluate(
            "el => getComputedStyle(el.firstElementChild).borderLeftColor"
        )
        == "rgb(197, 161, 235)"
    )
    expect(report).to_contain_text("Catalog context · Catalog entry")
    expect(report).to_contain_text("Band allocation · Amateur 2m reference only")
    expect(report).not_to_contain_text("[object Object]")
    min_snr = report.get_by_role("spinbutton", name="Minimum peak SNR (dB)")
    min_snr.fill("19")
    expect(report).to_contain_text("0 frequencies shown · 1 total")
    min_snr.fill("")
    expect(report).to_contain_text("1 frequencies shown · 1 total")
    delete_calls = []
    page.route(
        "**/api/survey/band-scan/report-test",
        lambda route: (
            delete_calls.append(route.request.method) if route.request.method == "DELETE" else None,
            route.continue_(),
        )[1],
    )
    page.route(
        "**/api/survey/band-scan",
        lambda route: (
            delete_calls.append(route.request.method) if route.request.method == "DELETE" else None,
            route.continue_(),
        )[1],
    )
    page.on("dialog", lambda dialog: dialog.dismiss())
    report.get_by_role("button", name="Delete report").click()
    report.get_by_role("button", name="Delete all reports").click()
    assert "DELETE" not in delete_calls, (
        "canceling delete confirmation must leave reports untouched"
    )
    expect(report.locator(".band-report-frequency-table")).to_contain_text("Test signal")
    with page.expect_download() as csv_download:
        report.get_by_role("button", name="Export frequencies CSV").click()
    assert csv_download.value.suggested_filename.endswith(".csv")
    with page.expect_download() as json_download:
        report.get_by_role("button", name="Export full JSON").click()
    assert json_download.value.suggested_filename.endswith(".json")
    report.get_by_role("tab", name="Timeline").click()
    expect(report).to_contain_text("bursty")
    expect(report.locator(".band-report-table tbody tr")).to_have_count(25)
    expect(report.locator(".band-report-pager")).to_contain_text("28 rows")
    report.get_by_role("button", name="Next").click()
    expect(report.locator(".band-report-table tbody tr")).to_have_count(3)
    expect(report.locator(".band-report-pager")).to_contain_text("page 2 of 2")
    report.get_by_role("tab", name="Coverage").click()
    expect(report).to_contain_text("covered")
    expect(report).to_contain_text("Not recorded")
    report.get_by_role("tab", name="Spectrum").click()
    expect(report.locator(".band-report-spectrum-chart svg")).to_be_visible()
    assert report.locator(".band-report-spectrum-chart rect").first.evaluate(
        "el => el.namespaceURI === 'http://www.w3.org/2000/svg' && el.getBBox().width > 0"
    )
    assert (
        report.locator(".band-report-spectrum-chart text")
        .nth(1)
        .evaluate("el => el.namespaceURI === 'http://www.w3.org/2000/svg'")
    )
    expect(report.locator(".band-report-spectrum-marker")).to_have_count(1)
    expect(report).to_contain_text("relative dB")
    expect(report).to_contain_text("gaps include unsampled time")
    marker = report.locator(".band-report-spectrum-marker")
    marker.hover()
    details = report.locator("#band-report-finding-details")
    expect(details).to_be_visible()
    expect(details).to_contain_text("Test signal")
    expect(details).to_contain_text("Source bin")
    details.hover()
    expect(details).to_be_visible()
    report.locator(".band-report-spectrum-chart").screenshot(
        path="/tmp/msdr-band-scan-report-finding-details.png"
    )
    marker.focus()
    expect(marker).to_have_attribute("aria-describedby", "band-report-finding-details")
    expect(details).to_be_visible()
    before_zoom = report.locator(".band-report-spectrum-controls span").inner_text()
    report.get_by_role("button", name="Zoom in").click()
    after_zoom = report.locator(".band-report-spectrum-controls span").inner_text()
    assert after_zoom != before_zoom
    report.locator(".band-report-spectrum-chart").scroll_into_view_if_needed()
    page.screenshot(path="/tmp/msdr-band-scan-report-spectrum.png", full_page=False)
    report.locator(".band-report-spectrum-chart").screenshot(
        path="/tmp/msdr-band-scan-report-spectrum-chart.png"
    )
    marker.click()
    panel = page.locator("signal-panel")
    expect(panel.locator(".signal-panel-quick-actions")).to_contain_text("TUNE")
    report.get_by_role("tab", name="Frequencies").click()
    report.get_by_role("button", name="Inspect").click()
    panel = page.locator("signal-panel")
    expect(panel.locator(".signal-panel-quick-actions")).to_contain_text("TUNE")
    expect(panel.locator(".signal-panel-quick-actions")).to_contain_text("DECODE")
    expect(panel.locator(".signal-panel-quick-actions")).to_contain_text("SAVE")


def test_slow_summary_is_not_restarted_by_refresh_and_stale_selection_is_ignored(live_app, page):
    async def seed():
        repo = live_app.service.band_scan.repo
        config = {
            "start_hz": 136000000,
            "end_hz": 138000000,
            "span_hz": 2000000,
            "dwell_s": 3,
            "overlap": 0.25,
            "centers_hz": [137000000],
        }
        for scan_id, hz in (("slow-a", 137100000), ("slow-b", 137200000)):
            repo.create(scan_id, config)
            repo.save(
                scan_id,
                "complete",
                {
                    "spans": [],
                    "results": [
                        {
                            "frequency_hz": hz,
                            "observations": [
                                {"observed_at": 100, "snr_db": 8, "family": {"name": scan_id}}
                            ],
                        }
                    ],
                },
            )

    live_app.run(seed())
    page.add_init_script("""(() => {
      const fetch0 = window.fetch.bind(window);
      window._reportSummaryCalls = [];
      window.fetch = async (input, init) => {
        const url = String(input);
        if (url.includes('/api/survey/band-scan/') && url.includes('/summary')) {
          window._reportSummaryCalls.push(url);
          const delay = window._reportSummaryCalls.length === 1 ? 4500 : 350;
          await new Promise(resolve => setTimeout(resolve, delay));
        }
        return fetch0(input, init);
      };
    })();""")
    page.goto(live_app.base_url)
    page.locator(".workspace-tab", has_text="BAND SCANS").click()
    report = page.locator("band-scan-report")
    picker = report.locator(".band-report-picker select")
    expect(picker).to_be_visible()
    selected = picker.input_value()
    expect(report.locator(".band-report-header")).to_be_visible()
    page.wait_for_timeout(3200)
    calls_for_selected = page.evaluate(
        "id => window._reportSummaryCalls.filter(url => "
        "url.endsWith('/' + id + '/summary')).length",
        selected,
    )
    assert calls_for_selected == 1, (
        "the periodic history refresh must leave one pending summary request alone"
    )
    options = picker.locator("option").evaluate_all("nodes => nodes.map(option => option.value)")
    other = next(value for value in options if value != selected)
    picker.select_option(other)
    expect(report.locator(".band-report-frequency-table")).to_contain_text(other, timeout=5000)
    assert page.evaluate(
        "id => window._reportSummaryCalls.some(url => url.endsWith('/' + id + '/summary'))", other
    )
    page.wait_for_timeout(1100)
    assert picker.input_value() == other
    expect(report.locator(".band-report-frequency-table")).to_contain_text(other)


def test_legacy_scan_shows_findings_strip_without_fabricating_spectrum(live_app, page):
    async def seed():
        repo = live_app.service.band_scan.repo
        config = {
            "start_hz": 136000000,
            "end_hz": 138000000,
            "span_hz": 2000000,
            "dwell_s": 3,
            "overlap": 0.25,
            "centers_hz": [137000000],
        }
        repo.create("legacy-report", config)
        repo.save(
            "legacy-report",
            "complete",
            {
                "spans": [],
                "results": [
                    {
                        "frequency_hz": 137123000,
                        "observations": [
                            {"observed_at": 100, "snr_db": 12, "family": {"name": "legacy finding"}}
                        ],
                    }
                ],
            },
        )

    live_app.run(seed())
    page.goto(live_app.base_url)
    page.locator(".workspace-tab", has_text="BAND SCANS").click()
    report = page.locator("band-scan-report")
    report.locator(".band-report-picker select").select_option("legacy-report")
    report.get_by_role("tab", name="Spectrum").click()
    expect(report).to_contain_text("No measured spectrum is available")
    expect(report.locator(".band-report-finding-strip")).to_be_visible()
    marker = report.locator(".band-report-finding-marker")
    expect(marker).to_be_visible()
    marker.hover()
    details = report.locator("#band-report-finding-details")
    expect(details).to_be_visible()
    expect(details).to_contain_text("legacy finding")
    expect(details).to_contain_text("No measured spectrum bin retained")
    expect(report.locator(".band-report-finding-strip")).to_have_class(
        "band-report-finding-strip has-details"
    )
    marker.focus()
    expect(marker).to_have_attribute("aria-describedby", "band-report-finding-details")
    marker.press("Escape")
    expect(details).to_have_count(0)
    expect(report.locator(".band-report-spectrum-chart")).to_have_count(0)
    report.locator(".band-report-finding-marker").click()
    expect(page.locator("signal-panel .signal-panel-quick-actions")).to_contain_text("TUNE")


def test_decoder_rate_error_has_specific_setup_action(live_app, page):
    page.route(
        "**/api/decoders",
        lambda route: route.fulfill(
            json=[
                {
                    "id": "adsb",
                    "label": "ADS-B / Mode S",
                    "available": True,
                    "demodulators": ["IQ"],
                    "default_bandwidth_hz": 2400000,
                }
            ]
        ),
    )
    page.goto(live_app.base_url)
    deck = page.locator("decode-deck").first
    deck.evaluate("""el => { el.failure = {subject: 'decode', status: 'error',
      detail: 'ADS-B / Mode S: needs IQ rate ≥ 2400000 samples/s'}; }""")
    deck.get_by_role("button", name="Set up ADS-B / Mode S").click()
    expect(page.get_by_role("region", name="Prepare decoder receiver")).to_be_visible()
    expect(page.locator('#decode-channel-list input[data-decoder-id="adsb"]')).to_be_checked()
