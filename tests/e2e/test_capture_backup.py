import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_backup_health_and_scan_comparison_controls(live_app, page):
    async def seed():
        repo = live_app.app.state.service.band_scan.repo
        for id, hz in (("baseline", 137100000), ("current", 137200000)):
            repo.create(id, {"start_hz": 136000000, "end_hz": 138000000})
            repo.save(
                id,
                "complete",
                {
                    "spans": [{"start_hz": 136000000, "end_hz": 138000000, "state": "covered"}],
                    "results": [{"frequency_hz": hz, "observations": [{"snr_db": 20}]}],
                },
            )

    live_app.run(seed())
    page.goto(live_app.base_url)
    page.evaluate("document.querySelector('msdr-app').setupOpen = true")
    backup = page.locator("backup-control")
    backup.locator("summary").click()
    expect(backup.get_by_role("link", name="Download full backup")).to_have_attribute(
        "href", "/api/backup?include_media=false"
    )
    backup.get_by_label("Include referenced media").check()
    expect(backup.get_by_role("link", name="Download full backup")).to_have_attribute(
        "href", "/api/backup?include_media=true"
    )
    page.get_by_role("button", name="Close setup", exact=True).click()
    page.locator(".workspace-tab", has_text="BAND SCANS").click()
    report = page.locator("band-scan-report")
    report.locator(".band-report-picker select").select_option("current")
    report.get_by_text("Compare scanning sessions", exact=True).click()
    report.get_by_label("Comparison baseline").select_option("baseline")
    expect(report).to_contain_text("1 new · 1 disappeared")
    expect(report).to_contain_text("137.200000")


def test_capture_health_and_repair_action(live_app, page):
    import json

    pending = [{"name": "interrupted", "segments": 2, "bytes": 1024, "samples": 256}]
    page.route("**/api/recordings", lambda route: route.fulfill(json=[]))
    page.route("**/api/recorder/recovery", lambda route: route.fulfill(json=pending))

    def repair(route):
        pending.clear()
        route.fulfill(body=json.dumps({"verified": True}), content_type="application/json")

    page.route("**/api/recorder/recovery/interrupted", repair)
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      const control = document.createElement('recorder-control');
      control.status = {state: 'armed', capture_health: {iq_throughput_bps: 4000000,
        disk_write_bps: 3900000, dropped_samples: 12, backlog_s: 0.25,
        remaining_recording_s: 120}};
      document.body.appendChild(control);
    }""")
    control = page.locator("body > recorder-control")
    control.locator("summary").click()
    expect(
        control.locator(".capture-health-metrics > div").filter(has_text="IQ received")
    ).to_contain_text("4.00 MB/s")
    expect(
        control.locator(".capture-health-metrics > div").filter(has_text="Dropped samples")
    ).to_contain_text("12")
    expect(
        control.locator(".capture-health-metrics > div").filter(has_text="Remaining")
    ).to_contain_text("2:00")
    # Open the actual component independently of application navigation state.
    page.evaluate("document.querySelector('recordings-view').open = true")
    view = page.locator("recordings-view")
    expect(view).to_contain_text("Interrupted recordings")
    view.get_by_role("button", name="Repair", exact=True).click()
    expect(view.get_by_role("button", name="Repair", exact=True)).to_have_count(0)


@pytest.mark.parametrize("width", [280, 600])
def test_capture_health_wraps_with_compact_type(live_app, page, width):
    page.goto(live_app.base_url)
    page.evaluate(
        """width => {
      const host = document.createElement('div');
      host.className = 'strip-recording'; host.id = 'health-layout-test';
      host.style.cssText = `width:${width}px;max-width:100%;padding:8px;box-sizing:border-box`;
      const control = document.createElement('recorder-control');
      control.status = {state:'off',capture_health:{iq_throughput_bps:24000000,
        disk_write_bps:24000000,dropped_samples:123456789012345,backlog_s:0.125,
        remaining_recording_s:86400}};
      host.append(control); document.body.prepend(host);
    }""",
        width,
    )
    health = page.locator("#health-layout-test .capture-health")
    health.locator("summary").click()
    expect(health.locator("dd")).to_have_count(5)
    assert health.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert (
        health.locator("dd").first.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") <= 13
    )
    assert health.locator(".dim").evaluate("el => getComputedStyle(el).whiteSpace") == "normal"
    if width == 280:
        health.screenshot(path="/tmp/msdr-capture-health.png")
