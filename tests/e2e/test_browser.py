"""Browser tests over a real msdr app + FakeSDRconnect (see docs/superpowers/
specs/2026-09-23-sp1-trust-feedback-design.md, section C). Skipped unless
`--e2e` is passed (see tests/e2e/conftest.py) -- Chromium can't launch inside
this repo's sandbox, so these need to run unsandboxed or by hand:

    .venv/bin/python -m pytest -q tests/e2e --e2e
"""

import json
import re

import pytest
from playwright.sync_api import Page, expect

from msdr.catalog.models import CatalogEntry
from msdr.library.models import Signal, Tag

pytestmark = pytest.mark.e2e


def _settings_patch(response) -> bool:
    return response.url.endswith("/api/settings") and response.request.method == "PATCH"


def _open_setup(page: Page) -> None:
    page.get_by_title("Setup (,)").click()
    # Confirms the drawer is open *and* GET /api/settings has loaded (the
    # form only renders once `_draft` is populated).
    expect(page.locator("setup-drawer").get_by_role("heading", name="Radio")).to_be_visible()


def _expand_catalog(page: Page) -> None:
    page.get_by_role("tab", name=re.compile("^Catalog$", re.IGNORECASE)).click()
    expect(page.locator("catalog-view input[name='catalog-query']")).to_be_visible()
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.connected")


def test_receiver_does_not_repeat_band_allocation_as_detection(live_app, page: Page):
    page.goto(live_app.base_url)
    panel = page.locator("instrument-panel")
    # The pill beside the frequency digits is the regulatory allocation
    # summary; the band-plan channel/application hint ("ISS voice
    # downlink") lives separately, in the context strip under the digits --
    # it is never shown as if it were a detection.
    expect(panel.locator(".frequency-allocation")).to_contain_text("Allocation")
    expect(panel.locator(".frequency-allocation")).to_have_attribute(
        "title", re.compile("MHz · US · AMATEUR")
    )
    hints = panel.locator(".frequency-context-strip .band-plan-hints")
    expect(hints).to_have_attribute("title", re.compile("ISS voice downlink"))
    expect(panel.locator(".detect-readout")).to_have_count(0)


def test_receiver_separates_catalog_reference_from_band_allocation(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [CatalogEntry(
            source="eibi", frequency_hz=145_800_000,
            station="Independent catalog station", mode="NFM",
        )],
    )
    page.goto(live_app.base_url)
    panel = page.locator("instrument-panel")
    expect(panel.locator(".frequency-allocation")).to_contain_text("Allocation")
    expect(panel.locator(".frequency-allocation")).to_have_attribute(
        "title", re.compile("MHz · US · AMATEUR")
    )
    hints = panel.locator(".frequency-context-strip .band-plan-hints")
    expect(hints).to_have_attribute("title", re.compile("ISS voice downlink"))
    reference = panel.locator(".detect-readout")
    expect(reference).to_contain_text("Independent catalog station")
    expect(reference).to_contain_text("catalog match · reference only")
    expect(reference).not_to_contain_text("ISS voice downlink")


def test_listen_beacons_tune_sends_cw_and_500hz_bandwidth(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.state?.can_control")

    page.get_by_role("tab", name="Suggestions").click()
    page.locator("listen-view").get_by_role("button", name="BEACONS", exact=True).click()
    rows = page.locator("listen-view .row")
    expect(rows).to_have_count(5)

    rows.first.locator("button.tune").click()

    assert live_app.run(live_app.fake.wait_for_set("demodulator", timeout=3.0)) == "CW"
    assert live_app.run(live_app.fake.wait_for_set("filter_bandwidth", timeout=3.0)) == 500


def test_beacon_monitor_keeps_detailed_tables_collapsed_until_requested(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      document.body.append(document.createElement('beacons-view'));
    }""")
    monitor = page.locator('beacons-view')
    expect(monitor.get_by_role('button', name='START NCDXF')).to_be_visible()
    details = monitor.locator('.beacon-detail')
    expect(details).to_have_count(2)
    expect(details.first.locator('table')).not_to_be_visible()
    details.first.locator('summary').click()
    expect(details.first.locator('table')).to_be_visible()


def test_catalog_star_creates_memory_that_restores_usb(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(
                source="eibi",
                frequency_hz=7_050_000,
                station="USB Test Station",
                mode="USB",
                bandwidth_hz=2700,
            )
        ],
    )

    page.goto(live_app.base_url)
    _expand_catalog(page)

    catalog_row = page.locator("catalog-view .row", has_text="USB Test Station")
    expect(catalog_row).to_be_visible()
    catalog_row.get_by_title("Save as signal").click()

    page.get_by_role("tab", name=re.compile("^Listen$", re.IGNORECASE)).click()
    memory_row = page.locator("recent-list .row", has_text="USB Test Station")
    expect(memory_row).to_be_visible()
    memory_row.get_by_title("Tune").click()

    assert live_app.run(live_app.fake.wait_for_set("demodulator", timeout=3.0)) == "USB"


def test_catalog_band_filters_show_only_matching_entries(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(
                source="eibi", frequency_hz=990_000, station="Test AM Station", mode="AM"
            ),
            CatalogEntry(
                source="eibi", frequency_hz=7_050_000, station="Test HF Station", mode="AM"
            ),
            CatalogEntry(
                source="eibi", frequency_hz=146_520_000, station="Test VHF Station", mode="NFM"
            ),
        ],
    )

    page.goto(live_app.base_url)
    _expand_catalog(page)

    for name in ("Test AM Station", "Test HF Station", "Test VHF Station"):
        expect(page.locator("catalog-view").get_by_text(name)).to_be_visible()

    page.locator("catalog-view select[aria-label='Catalog band']").select_option("lw_mw")
    expect(page.locator("catalog-view").get_by_text("Test AM Station")).to_be_visible()
    expect(page.locator("catalog-view").get_by_text("Test HF Station")).to_have_count(0)
    expect(page.locator("catalog-view").get_by_text("Test VHF Station")).to_have_count(0)

    page.locator("catalog-view select[aria-label='Catalog band']").select_option("vhf")
    expect(page.locator("catalog-view").get_by_text("Test VHF Station")).to_be_visible()
    expect(page.locator("catalog-view").get_by_text("Test AM Station")).to_have_count(0)
    expect(page.locator("catalog-view").get_by_text("Test HF Station")).to_have_count(0)


def test_catalog_frequency_range_and_band(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(source="eibi", frequency_hz=7_050_000, station="Lower HF"),
            CatalogEntry(source="eibi", frequency_hz=7_060_000, station="Upper HF"),
            CatalogEntry(source="eibi", frequency_hz=146_520_000, station="VHF signal"),
        ],
    )
    page.goto(live_app.base_url)
    _expand_catalog(page)
    catalog = page.locator("catalog-view")
    low = catalog.get_by_role("textbox", name="Minimum frequency MHz")
    high = catalog.get_by_role("textbox", name="Maximum frequency MHz")
    low.fill("7.05")
    high.fill("7.05")
    expect(catalog.get_by_text("Lower HF")).to_be_visible()
    expect(catalog.get_by_text("Upper HF")).to_have_count(0)
    expect(catalog.get_by_text("VHF signal")).to_have_count(0)

    high.fill("8")
    expect(catalog.get_by_text("Upper HF")).to_be_visible()
    low.fill("9")
    expect(catalog.get_by_role("alert")).to_contain_text("Minimum frequency")
    expect(catalog.locator(".catalog-entry")).to_have_count(0)

    low.fill("7")
    expect(catalog.get_by_text("Lower HF")).to_be_visible()
    catalog.get_by_role("combobox", name="Catalog band").select_option("vhf")
    expect(catalog.locator(".catalog-entry")).to_have_count(0)
    high.fill("")
    expect(catalog.get_by_text("VHF signal")).to_be_visible()


def test_catalog_language_dropdown_and_source_details(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "aoki",
        [
            CatalogEntry(
                source="aoki", frequency_hz=7_050_000, station="English entry", language="English"
            ),
            CatalogEntry(
                source="aoki", frequency_hz=7_060_000, station="French entry", language="French"
            ),
        ],
    )
    page.goto(live_app.base_url)
    _expand_catalog(page)
    catalog = page.locator("catalog-view")
    language = catalog.get_by_role("combobox", name="Catalog language")
    expect(language.get_by_role("option", name="English (1)", exact=True)).to_have_count(1)
    language.select_option(label="English (1)")
    expect(catalog.get_by_text("English entry")).to_be_visible()
    expect(catalog.get_by_text("French entry")).to_have_count(0)
    source = catalog.locator(".catalog-source-filters .catalog-source-hint", has_text="Aoki")
    source.locator("button").hover()
    expect(source.get_by_role("tooltip")).to_contain_text("Aoki (JSEA)")
    row_source = catalog.locator(".catalog-entry-source").first
    row_source.hover()
    expect(row_source.get_by_role("tooltip")).to_contain_text("Aoki (JSEA)")


def test_catalog_source_scope_paging_and_source_colors(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(
                source="eibi",
                frequency_hz=7_050_000 + i * 1_000,
                station=f"EiBi {i:03}",
            )
            for i in range(205)
        ],
    )
    live_app.service.catalog.replace_source(
        "aoki",
        [
            CatalogEntry(source="aoki", frequency_hz=8_000_000 + i * 1_000, station=f"Aoki {i:02}")
            for i in range(3)
        ],
    )
    live_app.service.catalog.replace_source(
        "ndxc",
        [
            CatalogEntry(source="ndxc", frequency_hz=9_000_000 + i * 1_000, station=f"NDXC {i:02}")
            for i in range(4)
        ],
    )

    page.goto(live_app.base_url)
    _expand_catalog(page)
    catalog = page.locator("catalog-view")
    catalog_tab = page.get_by_role("tab", name="CATALOG")
    assert catalog_tab.evaluate("el => getComputedStyle(el).color") == "rgb(120, 191, 255)"
    assert catalog.locator(".panel").evaluate(
        "el => getComputedStyle(el).borderLeftColor"
    ) == "rgb(120, 191, 255)"
    status = catalog.locator(".catalog-results-status")
    expect(status).to_contain_text("All imported sources · 1–100 of 212 matches")

    catalog.get_by_role("button", name="EiBi", exact=True).click()
    catalog.get_by_role("button", name="Next", exact=True).click()
    expect(status).to_contain_text("101–200 of 205 matches")
    catalog.get_by_role("button", name="Aoki", exact=True).click()
    expect(status).to_contain_text("1 source selected · 1–3 of 3 matches")
    expect(catalog.locator('.catalog-entry[data-source="eibi"]')).to_have_count(0)
    expect(catalog.get_by_role("button", name="Aoki", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    catalog.get_by_role("button", name="Aoki", exact=True).click()
    expect(status).to_contain_text("1 source selected · 1–3 of 3 matches")
    catalog.get_by_role("button", name="All sources", exact=True).click()
    expect(status).to_contain_text("All imported sources · 1–100 of 212 matches")
    catalog.get_by_label("Combine sources", exact=True).check()
    catalog.get_by_role("button", name="EiBi", exact=True).click()
    catalog.get_by_role("button", name="Aoki", exact=True).click()
    expect(status).to_contain_text("2 sources selected · 1–100 of 208 matches")
    expect(catalog.locator(".catalog-entry")).to_have_count(100)
    expect(catalog.get_by_text("EiBi 000")).to_be_visible()
    expect(catalog.get_by_text("Aoki 00")).to_be_visible()

    eibi_source = catalog.locator('.catalog-entry[data-source="eibi"] .catalog-entry-source')
    eibi_color = eibi_source.first.evaluate(
        "el => getComputedStyle(el).color"
    )
    aoki_source = catalog.locator('.catalog-entry[data-source="aoki"] .catalog-entry-source')
    aoki_color = aoki_source.first.evaluate(
        "el => getComputedStyle(el).color"
    )
    assert eibi_color != aoki_color

    catalog.get_by_role("button", name="Next", exact=True).click()
    expect(status).to_contain_text("2 sources selected · 101–200 of 208 matches")
    expect(catalog.get_by_text("EiBi 100")).to_be_visible()
    expect(catalog.get_by_text("Aoki 00")).to_have_count(0)
    catalog.get_by_role("button", name="Previous", exact=True).click()
    expect(status).to_contain_text("2 sources selected · 1–100 of 208 matches")

    first_eibi = catalog.locator('.catalog-entry[data-source="eibi"]', has_text="EiBi 000")
    first_eibi.get_by_title("Tune").click()
    tuned = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert tuned == 7_050_000


def test_catalog_search_tune_and_import_access(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(
                source="eibi", frequency_hz=7_050_000, station="Search Me",
                mode="USB", bandwidth_hz=2700,
            )
        ],
    )
    page.goto(live_app.base_url)
    _expand_catalog(page)
    search = page.locator("catalog-view input[name='catalog-query']")
    search.fill("Search Me")
    row = page.locator("catalog-view .row", has_text="Search Me")
    expect(row).to_be_visible()
    row.locator("button.tune").click()
    assert (
        live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
        == 7_050_000
    )
    assert live_app.run(live_app.fake.wait_for_set("demodulator", timeout=3.0)) == "USB"

    page.locator("catalog-view").get_by_role("button", name="Sources & imports").click()
    drawer = page.locator("setup-drawer")
    expect(
        drawer.get_by_role("heading", name=re.compile("^Catalog$", re.IGNORECASE))
    ).to_be_visible()
    expect(drawer.get_by_placeholder("path")).to_be_visible()
    expect(drawer.get_by_role("button", name="IMPORT LIST")).to_be_visible()
    drawer.locator(".overlay-backdrop").click(position={"x": 20, "y": 20})
    expect(drawer.locator(".overlay-backdrop")).to_have_count(0)
    page.locator("catalog-view").get_by_role("button", name="Sources & imports").click()
    expect(drawer.get_by_placeholder("path")).to_be_visible()


def test_setup_clear_location(live_app, page: Page):
    page.goto(live_app.base_url)
    _open_setup(page)
    page.locator("setup-drawer .setup-nav").get_by_role("button", name="Passes").click()

    override = page.locator("setup-drawer").get_by_role("button", name="location", exact=True)
    expect(override).to_have_text("OVERRIDE: OFF")
    override.click()
    page.locator("setup-drawer input[name='lat']").fill("40")
    page.locator("setup-drawer input[name='lon']").fill("-74")

    with page.expect_response(_settings_patch) as resp_info:
        page.locator("setup-drawer").get_by_role("button", name="Save").click()
    assert resp_info.value.json()["location"] == {
        "lat": 40.0,
        "lon": -74.0,
        "altitude_m": 0.0,
        "timezone": "UTC",
    }

    _open_setup(page)
    page.locator("setup-drawer .setup-nav").get_by_role("button", name="Passes").click()
    override = page.locator("setup-drawer").get_by_role("button", name="location", exact=True)
    expect(override).to_have_text("OVERRIDE: ON")
    override.click()

    with page.expect_response(_settings_patch) as resp_info:
        page.locator("setup-drawer").get_by_role("button", name="Save").click()
    assert resp_info.value.json()["location"] is None


def test_failed_restore_shows_inline_note_and_pinned_trail(live_app, page: Page):
    live_app.service.catalog.replace_source(
        "eibi",
        [
            CatalogEntry(
                source="eibi", frequency_hz=9_000_000, station="Fail Test Station", mode="AM"
            )
        ],
    )
    # Wait for the receiver's initial seed read (including demodulator) to
    # land *before* dropping its echo -- otherwise demodulator is never read
    # at all, confirmed state stays permanently incomplete, and the catalog
    # tune below fails outright with "not connected" instead of running a
    # restore whose one demodulator step fails.
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.complete")
    # The demodulator set (NFM -> AM) is accepted but neither echoed nor
    # answered on read-back, so that restore step fails and the restore is
    # not confirmed. Not the VFO: the fake (like SDRconnect)
    # emits a coupled VFO echo when the centre moves, which confirms it anyway.
    live_app.drop_echo("demodulator")

    _expand_catalog(page)

    row = page.locator("catalog-view .row", has_text="Fail Test Station")
    expect(row).to_be_visible()
    row.locator("button.tune").click()

    expect(page.locator("catalog-view failure-note")).to_be_visible(timeout=10000)
    trail = page.locator("#restore-trail")
    expect(trail).to_be_visible()
    expect(trail).to_have_attribute("style", re.compile("border-color"))
    dismiss = trail.get_by_title("dismiss")
    expect(dismiss).to_be_visible()

    dismiss.click()
    expect(page.locator("#restore-trail")).to_have_count(0)
    # The panel-local note has its own lifecycle -- dismissing the trail
    # doesn't touch it.
    note = page.locator("catalog-view failure-note")
    expect(note).to_be_visible()
    note.get_by_title("dismiss").click()
    expect(page.locator("catalog-view failure-note")).to_have_count(0)


def test_diagnostics_modal_only_on_demand_and_retry_calls_api(live_app, page: Page):
    page.goto(live_app.base_url)
    # Healthy link: no inline panel and no warning shortcut.
    expect(page.locator("#diag-banner")).to_have_count(0)
    expect(page.locator("button.link-problem")).to_have_count(0)

    live_app.run(live_app.fake.stop())
    # A failing check shows the shortcut, but never opens diagnostics by itself.
    expect(page.locator("button.link-problem")).to_be_visible(timeout=5000)
    expect(page.locator("#diag-banner")).to_have_count(0)
    page.locator("button.link-problem").click()
    expect(page.get_by_role("dialog", name="Diagnostics")).to_be_visible()

    live_app.run(live_app.fake.start(reuse_address=True))
    with page.expect_response(lambda r: r.url.endswith("/api/connection/retry")) as resp_info:
        page.locator("#diag-banner").get_by_role("button", name="RETRY").click()
    assert resp_info.value.status == 200

    # readiness.py enforces a >=10s settle wait before a reconnect after a
    # disconnect is reported as connected (_MIN_SETTLE_S), regardless of
    # startup_grace_s -- a safety margin for a real SDRconnect instance
    # just coming back, not something RETRY can skip.
    expect(page.locator("button.link-problem")).to_have_count(0, timeout=15000)
    page.keyboard.press("Escape")
    expect(page.locator("#diag-banner")).to_have_count(0)


def test_layout_change_survives_reload(live_app, page: Page):
    page.goto(live_app.base_url)
    _open_setup(page)
    page.locator("setup-drawer .setup-nav").get_by_role("button", name="Layout").click()

    recent_row = page.locator("setup-drawer .row", has_text="RECENT")
    expect(recent_row).to_be_visible()
    with page.expect_response(_settings_patch):
        recent_row.get_by_title("move up").click()

    page.reload()
    # S1 default LISTEN column 2 is ["nearby", "recent"] (inbox lives in
    # LIBRARY now), so moving RECENT up puts it first.
    page.wait_for_function("() => document.querySelector('msdr-app')?.settings?.workspaces?.listen")
    columns = page.locator("msdr-app").evaluate("app => app.settings.workspaces.listen.columns")
    assert any(ids[:2] == ["recent", "nearby"] for ids in columns)


def test_no_section_shell_rail(live_app, page: Page):
    # v0.8.0 "layout": the separate grip+label rail row is gone -- the grip
    # now lives inside each section's own title row (see styles.css).
    page.goto(live_app.base_url)
    expect(page.locator(".section-shell-rail")).to_have_count(0)


def test_layout_preset_survives_reload(live_app, page: Page):
    page.goto(live_app.base_url)

    # Layout presets are visible in the receiver's tools row.
    page.get_by_title("Layout presets").click()
    with page.expect_response(_settings_patch):
        page.get_by_role("button", name="LISTENING", exact=True).click()

    page.reload()
    # Listen's top workbench now owns those sections outside the layout grid;
    # verify the remembered layout model rather than hidden grid shells.
    page.wait_for_function("""() => document.querySelector('msdr-app')?.settings
      ?.workspaces?.listen?.columns?.[0]?.join(',') === 'listen,nearby'""")


def test_layout_grip_keyboard_arrow_right_moves_and_survives_reload(live_app, page: Page):
    # S1: grips render only in EDIT LAYOUT mode (SETUP → LAYOUT), remembered
    # in localStorage -- switch it on before the app loads. Listen's own
    # sections (nearby/recent/listen/log/decode) now render through
    # <listen-workbench> outside the draggable grid (app.js's
    # LISTEN_WORKBENCH_SECTIONS, since 0.31.0), so this exercises Satellites'
    # grid instead -- its default columns are ["passes"] / ["sky"] / [].
    page.add_init_script("localStorage.setItem('msdr.layoutEdit', '1')")
    page.goto(live_app.base_url)
    # _switchWorkspace (app.js) resolves the clicked workspace's layout from
    # whatever this.settings holds *right now*, and marks _workspaceChosen so
    # a GET that lands later never overrides it again (S1: the operator's own
    # choice wins over the server) -- so the initial GET /api/settings must
    # land before the tab click, or it would permanently stick with the
    # pre-fetch default layout instead of the one just saved below.
    page.wait_for_function("() => document.querySelector('msdr-app')?.settings")
    # The tab switch itself PATCHes /api/settings (active workspace) --
    # wait it out first so the expect_response below can only match the
    # grip move's own PATCH, not race a still-inflight workspace switch.
    with page.expect_response(_settings_patch):
        page.get_by_role("tab", name="Satellites").click()

    grip = page.locator('[data-section-id="passes"] .grip')
    grip.focus()
    with page.expect_response(_settings_patch):
        grip.press("ArrowRight")

    # Focus stays on the moved grip rather than being lost on re-render.
    # Checked via activeElement, not to_be_focused(): Playwright's check
    # also requires document.hasFocus(), which headless Chromium may not
    # report even though the right element holds focus.
    page.wait_for_function(
        """() => document.activeElement?.matches('[data-section-id="passes"] .grip')"""
    )

    page.reload()
    page.wait_for_function("() => document.querySelector('msdr-app')?.settings")
    page.get_by_role("tab", name="Satellites").click()
    # The first render uses the default layout until GET /api/settings lands,
    # so wait for the saved one rather than reading the columns immediately.
    first_in_col2 = page.locator(".section-shell-col").nth(1).locator(".section-shell").first
    expect(first_in_col2).to_have_attribute("data-section-id", "passes")


# --- v0.9.0 direct tuning controls ------------------------------------------


def test_frequency_readout_typed_entry_tunes(live_app, page: Page):
    page.goto(live_app.base_url)

    # Double-click enters text-edit mode; a single click steps a digit instead.
    page.locator("segment-readout .seg-readout").dblclick()
    freq_input = page.locator("segment-readout input")
    expect(freq_input).to_be_visible()
    freq_input.fill("14.100")
    freq_input.press("Enter")

    got = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert got == 14_100_000


def test_frequency_readout_invalid_entry_shows_invalid_state(live_app, page: Page):
    page.goto(live_app.base_url)

    page.locator("segment-readout .seg-readout").dblclick()
    freq_input = page.locator("segment-readout input")
    freq_input.fill("not a frequency")
    freq_input.press("Enter")

    expect(page.locator("segment-readout .seg-unit.invalid")).to_be_visible()
    # Still in edit mode -- Esc cancels back to the plain readout.
    freq_input.press("Escape")
    expect(page.locator("segment-readout input")).to_have_count(0)


def test_mode_select_usb_sends_demodulator_and_leaves_vfo_unchanged(live_app, page: Page):
    page.goto(live_app.base_url)

    before = live_app.fake.state["device_vfo_frequency"]
    page.locator("#mode-select").select_option("USB")

    assert live_app.run(live_app.fake.wait_for_set("demodulator", timeout=3.0)) == "USB"
    assert live_app.fake.state["device_vfo_frequency"] == before


def test_step_button_moves_vfo_by_mode_step(live_app, page: Page):
    page.goto(live_app.base_url)

    before = live_app.fake.state["device_vfo_frequency"]
    mode = live_app.fake.state["demodulator"]
    step_hz = {
        "AM": 5000, "SAM": 5000, "USB": 1000, "LSB": 1000, "DSB": 1000, "CW": 1000,
        "NFM": 12500, "WFM": 100000, "RAW": 1000,
    }[mode]

    page.locator("#step-up").click()

    got = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert got == before + step_hz


def test_fine_tune_button_moves_vfo_by_100_hz(live_app, page: Page):
    page.goto(live_app.base_url)
    before = live_app.fake.state["device_vfo_frequency"]

    page.locator("instrument-panel").get_by_role("button", name="Fine tune up").click()

    got = live_app.run(live_app.fake.wait_for_set("device_vfo_frequency", timeout=3.0))
    assert got == before + 100


# --- v0.10.0 "Library" -------------------------------------------------------


def test_library_open_search_and_band_filter_with_paging(live_app, page: Page):
    # 54 filler memories + 1 uniquely-named HF one: enough to exercise the
    # "1-50 of N" paging text, and a target the search/band filters narrow to.
    for i in range(54):
        live_app.service.signals.add(
            Signal(name=f"Memory {i:03d}", frequency_hz=100_000_000 + i * 1000)
        )
    live_app.service.signals.add(Signal(name="Radio Free Test", frequency_hz=7_100_000))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    expect(library).to_be_visible()
    expect(library.get_by_text("1–50 of 55")).to_be_visible()

    library.locator("input[name='library-query']").fill("Radio Free Test")
    expect(library.locator("tbody tr")).to_have_count(1)
    expect(library.locator("tbody tr", has_text="Radio Free Test")).to_be_visible()

    library.locator("input[name='library-query']").fill("")
    library.locator("select[aria-label='band']").select_option("hf")
    expect(library.locator("tbody tr")).to_have_count(1)
    expect(library.locator("tbody tr", has_text="Radio Free Test")).to_be_visible()


def test_library_select_all_matching_bulk_tag_then_undo(live_app, page: Page):
    tag = Tag(name="Testband")
    live_app.service.tags.add(tag)
    # 55 memories span two pages, so "select all N matching" (only offered
    # when results exceed one page) has to reach past the visible 50.
    for i in range(55):
        live_app.service.signals.add(Signal(name=f"Bulk {i}", frequency_hz=10_000_000 + i))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    library.locator("thead input[type='checkbox']").check()
    library.get_by_role("button", name=re.compile("select all 55 matching")).click()
    library.locator("select[aria-label='bulk tag']").select_option(label="Testband")
    library.get_by_role("button", name="+ tag", exact=True).click()

    expect(library.locator("tag-chips", has_text="Testband")).to_have_count(50)
    tagged = [s for s in live_app.service.signals.list(limit=1000) if tag.id in s.tag_ids]
    assert len(tagged) == 55

    library.get_by_role("button", name=re.compile("UNDO")).click()
    expect(library.locator("tag-chips", has_text="Testband")).to_have_count(0)


def test_library_delete_row_then_global_undo_restores(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Delete Me", frequency_hz=5_000_000))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    row = library.locator("tbody tr", has_text="Delete Me")
    expect(row).to_be_visible()
    # Just this one row (<=10) -- DELETE fires without a confirm() dialog.
    row.locator("input[type='checkbox']").check()
    library.get_by_role("button", name="DELETE", exact=True).click()
    expect(library.locator("tbody tr", has_text="Delete Me")).to_have_count(0)

    page.keyboard.press("Meta+z")
    page.keyboard.press("Control+z")
    expect(library.locator("tbody tr", has_text="Delete Me")).to_be_visible()


# --- v0.11.0 "Lists" ----------------------------------------------------------


def test_library_list_create_bulk_add_reorder_persists(live_app, page: Page):
    names = ["Evening A", "Evening B", "Evening C"]
    for i, name in enumerate(names):
        live_app.service.signals.add(Signal(name=name, frequency_hz=6_000_000 + i * 1000))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    library.get_by_role("button", name="+ LIST", exact=True).click()
    name_input = library.get_by_label("new list name")
    name_input.fill("Evening SW")
    name_input.press("Enter")

    # Creating a list auto-selects it once the POST lands -- wait for that
    # (the chip appears with its count, pressed) before going back to ALL,
    # or the late selection flips the view back to the empty list.
    expect(library.get_by_role("button", name=re.compile(r"Evening SW \(0\)"))).to_have_attribute(
        "aria-pressed", "true"
    )
    library.get_by_role("button", name="ALL", exact=True).click()
    # Wait for the ALL reload to land before ticking rows -- ticking the
    # previous render's rows races the re-render and detaches them.
    expect(library.locator("tbody tr")).to_have_count(len(names))
    # These three are the whole library here, so "select page" covers them.
    library.locator("thead input[type='checkbox']").check()
    library.locator("select[aria-label='bulk list']").select_option(label="Evening SW")
    library.get_by_role("button", name="+ list", exact=True).click()

    library.get_by_role("button", name=re.compile(r"Evening SW \(3\)")).click()
    rows = library.locator("tbody tr")
    expect(rows).to_have_count(3)

    first_name = rows.nth(0).locator("td").nth(2).inner_text()
    second_name = rows.nth(1).locator("td").nth(2).inner_text()
    rows.nth(0).get_by_role("button", name=re.compile(f"move {re.escape(first_name)} down")).click()

    expect(rows.nth(0).locator("td").nth(2)).to_have_text(second_name)
    expect(rows.nth(1).locator("td").nth(2)).to_have_text(first_name)

    page.reload()
    page.keyboard.press("l")
    library = page.locator("library-view")
    library.get_by_role("button", name=re.compile(r"Evening SW \(3\)")).click()
    rows = library.locator("tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator("td").nth(2)).to_have_text(second_name)
    expect(rows.nth(1).locator("td").nth(2)).to_have_text(first_name)


def test_library_star_row_adds_to_favorites_then_unstar(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Starred One", frequency_hz=9_000_000))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    row = library.locator("tbody tr", has_text="Starred One")
    row.get_by_role("button", name="add Starred One to favorites").click()

    library.get_by_role("button", name=re.compile(r"★ FAVORITES")).click()
    fav_row = library.locator("tbody tr", has_text="Starred One")
    expect(fav_row).to_be_visible()

    fav_row.get_by_role("button", name="remove Starred One from favorites").click()
    expect(library.locator("tbody tr", has_text="Starred One")).to_have_count(0)


def test_library_saved_search_apply_restores_filters(live_app, page: Page):
    tag = Tag(name="Numbers")
    live_app.service.tags.add(tag)
    live_app.service.signals.add(Signal(name="Buzzer", frequency_hz=5_000_000, tag_ids=[tag.id]))
    live_app.service.signals.add(Signal(name="Other", frequency_hz=8_000_000))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    library.locator("input[name='library-query']").fill("Buzzer")
    library.locator("select[aria-label='tag']").select_option(label="Numbers")

    page.once("dialog", lambda d: d.accept("Buzzer search"))
    library.get_by_role("button", name="SEARCHES ▾", exact=True).click()
    library.get_by_role("button", name="SAVE CURRENT…", exact=True).click()

    library.locator("input[name='library-query']").fill("")
    library.locator("select[aria-label='tag']").select_option(label="ALL TAGS")

    library.get_by_role("button", name="SEARCHES ▾", exact=True).click()
    library.get_by_role("button", name="Buzzer search", exact=True).click()

    expect(library.locator("input[name='library-query']")).to_have_value("Buzzer")
    expect(library.locator("select[aria-label='tag']")).to_have_value(tag.id)


def test_library_duplicates_merge_then_global_undo_restores(live_app, page: Page):
    live_app.service.signals.add(Signal(name="Dup A", frequency_hz=15_000_000, demodulator="AM"))
    live_app.service.signals.add(Signal(name="Dup B", frequency_hz=15_000_000, demodulator="AM"))

    page.goto(live_app.base_url)
    page.keyboard.press("l")

    library = page.locator("library-view")
    library.get_by_role("button", name="DUPLICATES", exact=True).click()

    group = library.locator(".dup-group", has_text="Dup A")
    expect(group).to_have_count(1)
    group.get_by_role("button", name="MERGE", exact=True).click()
    expect(library.locator(".dup-group")).to_have_count(0)

    library.get_by_role("button", name="‹ BACK", exact=True).click()
    dup_rows = library.locator("tbody tr").filter(has_text=re.compile("Dup A|Dup B"))
    expect(dup_rows).to_have_count(1)

    page.keyboard.press("Meta+z")
    page.keyboard.press("Control+z")
    expect(dup_rows).to_have_count(2)


def _enable_channel_scanner(live_app, page: Page):
    response = page.request.patch(
        f"{live_app.base_url}/api/settings", data={"features": {"scanner": True}}
    )
    assert response.ok, response.text()
    page.reload()
    page.get_by_role("button", name=re.compile(r"^Channel scan")).click()


def _open_scanner_setup(page: Page):
    """Scanner setup lives in the receiver's optional disclosure."""
    page.locator("scanner-deck").get_by_title("Scanner setup").click()
    modal = page.locator("#scanner-setup-modal")
    expect(modal).to_be_visible()
    return modal


def test_scanner_range_scan_runs_and_stops(live_app, page: Page):
    page.goto(live_app.base_url)
    _enable_channel_scanner(live_app, page)

    deck = page.locator("scanner-deck")
    modal = _open_scanner_setup(page)
    modal.get_by_role("tab", name="Range", exact=True).click()
    modal.locator("input[name='scan-start-mhz']").fill("118")
    modal.locator("input[name='scan-stop-mhz']").fill("118.3")
    modal.locator("select[aria-label='step']").select_option(label="100 kHz")
    # Above the fake receiver's static 12 dB SNR (see FakeSDRconnect) so the
    # sweep advances instead of holding on the very first channel.
    modal.locator("input[name='scan-threshold-db']").fill("40")
    modal.locator("input[name='scan-dwell-ms']").fill("50")

    modal.get_by_role("button", name="▶ START", exact=True).click()
    expect(modal).to_be_hidden()
    expect(deck.get_by_text("RUNNING", exact=True)).to_be_visible()

    position = deck.locator("span", has_text=re.compile(r"^\d+/\d+$"))
    expect(position).to_have_text(re.compile(r"^[2-9]\d*/\d+$"), timeout=5000)

    deck.get_by_role("button", name="■ STOP", exact=True).click()
    expect(deck.get_by_text("RUNNING", exact=True)).to_have_count(0)


def test_scanner_list_scan_holds_on_snr_and_logs_hit(live_app, page: Page):
    sig = Signal(name="Net Control", frequency_hz=7_200_000)
    live_app.service.signals.add(sig)
    created = live_app.service.create_list("Nets")
    live_app.service.add_signals_to_list(created["id"], [sig.id])

    page.goto(live_app.base_url)
    _enable_channel_scanner(live_app, page)

    deck = page.locator("scanner-deck")
    modal = _open_scanner_setup(page)
    modal.locator("select[aria-label='scan list']").select_option(label="Nets (1)")
    modal.locator("input[name='scan-threshold-db']").fill("20")

    modal.get_by_role("button", name="▶ START", exact=True).click()
    expect(modal).to_be_hidden()
    expect(deck.get_by_text("RUNNING", exact=True)).to_be_visible()

    live_app.run(live_app.fake.external_change("signal_snr", 25.0))

    expect(deck.get_by_text("HOLDING", exact=True)).to_be_visible(timeout=5000)
    # The hit lands in the deck's last-hit line and its ▾ hits dropdown.
    expect(deck.get_by_text(re.compile(r"^last hit "))).to_be_visible()
    deck.get_by_title("Recent hits").click()
    hit_row = deck.locator(".row", has_text="Net Control")
    expect(hit_row).to_have_count(1)


def test_scanner_spectrum_scan_holds_on_candidate_and_logs_hit(live_app, page: Page):
    live_app.fake.spectrum_peaks = {145_787_500: 160}

    page.goto(live_app.base_url)
    _enable_channel_scanner(live_app, page)

    deck = page.locator("scanner-deck")
    modal = _open_scanner_setup(page)
    modal.get_by_role("tab", name="Spectrum", exact=True).click()

    modal.get_by_role("button", name="▶ START", exact=True).click()
    expect(modal).to_be_hidden()
    expect(deck.get_by_text("RUNNING", exact=True)).to_be_visible()

    # Land on the one candidate the fake spectrum exposes, then push SNR over
    # the hold threshold the same way the list-scan test does.
    live_app.run(live_app.fake.external_change("signal_snr", 25.0))

    expect(deck.get_by_text("HOLDING", exact=True)).to_be_visible(timeout=5000)
    deck.get_by_title("Recent hits").click()
    hit_row = deck.locator(".row", has_text="145.7875")
    expect(hit_row).to_have_count(1)

    deck.get_by_role("button", name="■ STOP", exact=True).click()
    expect(deck.get_by_text("RUNNING", exact=True)).to_have_count(0)


def test_scanner_deck_stays_with_receiver_across_workspaces(live_app, page: Page):
    page.goto(live_app.base_url)
    _enable_channel_scanner(live_app, page)
    expect(page.locator("instrument-panel .panel-scanner-deck scanner-deck")).to_be_visible()
    page.keyboard.press("2")
    expect(page.locator("instrument-panel .panel-scanner-deck scanner-deck")).to_be_visible()
    expect(page.locator('[data-section-id="scan"]')).to_have_count(0)
    expect(page.locator('[data-section-id="scanner"]')).to_have_count(0)


def test_passes_ics_download_link_present(live_app, page: Page):
    # Cheap, data-independent check for the v0.13.0 calendar export button --
    # PASSES itself renders unconditionally (no collapse state), so this
    # needs no location/TLE/catalog seeding. PASSES now lives in the
    # SATELLITES workspace rather than the LISTEN grid.
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="Satellites").click()
    ics_link = page.locator("passes-view").get_by_role("link", name="↓ ICS", exact=True)
    expect(ics_link).to_have_attribute("href", "/api/passes.ics")


def test_getting_started_can_be_brought_back_after_hide(live_app, page: Page):
    page.goto(live_app.base_url)
    checklist = page.locator("#getting-started")
    expect(checklist).to_be_visible()
    checklist.get_by_role("button", name="HIDE").click()
    expect(checklist).to_have_count(0)

    page.reload()  # HIDE persists across reloads...
    expect(page.locator("#getting-started")).to_have_count(0)

    # ...until SETUP's SHOW GETTING STARTED brings it back. It's in the
    # About section, collapsed by default like every setup-drawer <details>
    # but "Radio" -- the nav button opens it (see test_layout_change_survives_reload
    # for the same pattern with Layout).
    page.get_by_title("Setup (,)").click()
    page.locator("setup-drawer .setup-nav").get_by_role("button", name="About").click()
    page.locator("setup-drawer").get_by_role("button", name="SHOW GETTING STARTED").click()
    expect(page.locator("#getting-started")).to_be_visible()


def test_sky_satellite_select_changes_display_without_tuning(live_app, page: Page, tmp_path):
    # The fixture runs no nextpass, so PASSES has nothing to offer and the
    # header <select> falls back to bound-memory satellites (v0.14.1). SKY
    # still needs a location and elements to draw a position, so seed both
    # locally (this test used to pass only by reading the developer's real
    # location and nextpass install, which made its default time-dependent).
    live_app.service.signals.add(
        Signal(name="ISS packet", frequency_hz=145_825_000, satellite="ISS")
    )
    live_app.service.signals.add(
        Signal(name="AO-73 downlink", frequency_hz=145_960_000, satellite="AO-73")
    )
    tle_dir = tmp_path / "tle"
    tle_dir.mkdir()
    for name, norad in (("ISS (ZARYA)", 25544), ("FUNCUBE-1 (AO-73)", 39444)):
        (tle_dir / f"{norad}.json").write_text(json.dumps({
            "OBJECT_NAME": name, "OBJECT_ID": "2000-000A", "EPOCH": "2024-01-01T00:00:00.000000",
            "MEAN_MOTION": 15.5, "ECCENTRICITY": 0.0001, "INCLINATION": 51.6,
            "RA_OF_ASC_NODE": 0.0, "ARG_OF_PERICENTER": 0.0, "MEAN_ANOMALY": 0.0,
            "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": "U", "NORAD_CAT_ID": norad,
            "ELEMENT_SET_NO": 999, "REV_AT_EPOCH": 1, "BSTAR": 0.0001,
            "MEAN_MOTION_DOT": 0.0, "MEAN_MOTION_DDOT": 0.0,
        }))

    page.goto(live_app.base_url)
    resp = page.request.patch(f"{live_app.base_url}/api/settings", data={
        "location": {"lat": 37.0, "lon": -122.0, "altitude_m": 0, "timezone": "UTC"},
        "tle_cache_dir": str(tle_dir),
    })
    assert resp.ok
    page.reload()
    page.get_by_role("tab", name="Satellites").click()
    sky = page.locator("sky-view")
    select = sky.locator("select[aria-label='sky satellite']")
    expect(select).to_be_visible()
    # Default follows the app's rule: soonest upcoming pass, else the first
    # bound satellite. Read which one it chose, then switch to the other.
    svg = sky.locator("svg[aria-label^='Sky position']")
    expect(svg).to_have_attribute("aria-label", re.compile("ISS|AO-73"))
    default = "AO-73" if "AO-73" in svg.get_attribute("aria-label") else "ISS"
    other = "ISS" if default == "AO-73" else "AO-73"

    before_vfo = live_app.fake.state["device_vfo_frequency"]
    before_demod = live_app.fake.state["demodulator"]

    select.select_option(other)
    expect(svg).to_have_attribute("aria-label", re.compile(other))

    page.wait_for_timeout(300)  # SKY's own selector must never reach the radio
    assert live_app.fake.state["device_vfo_frequency"] == before_vfo
    assert live_app.fake.state["demodulator"] == before_demod


def test_sky_track_disabled_shows_reason(live_app, page: Page, tmp_path):
    # Exercising TRACK's disabled-reason line needs /api/sky/ISS to resolve a
    # real position -- otherwise sky-view degrades to a plain "unavailable"
    # message and TRACK never renders at all. Neither a location nor a TLE
    # is seeded by the live_app fixture (no network there either), so this
    # seeds both locally: a location far outside ISS's ground track (its
    # orbit never exceeds +/-51.6 deg latitude, and a LEO horizon is only
    # ~2300 km, so 85 deg N can never see it, at any epoch/time) and a
    # hand-built OMM record (skyfield's GP JSON shape) for its NORAD id --
    # this is local fixture data, not a network fetch.
    live_app.service.signals.add(
        Signal(name="ISS packet", frequency_hz=145_825_000, satellite="ISS")
    )

    tle_dir = tmp_path / "tle"
    tle_dir.mkdir()
    omm = {
        "OBJECT_NAME": "ISS (ZARYA)",
        "OBJECT_ID": "1998-067A",
        "EPOCH": "2024-01-01T00:00:00.000000",
        "MEAN_MOTION": 15.5,
        "ECCENTRICITY": 0.0001,
        "INCLINATION": 51.6,
        "RA_OF_ASC_NODE": 0.0,
        "ARG_OF_PERICENTER": 0.0,
        "MEAN_ANOMALY": 0.0,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": 25544,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 1,
        "BSTAR": 0.0001,
        "MEAN_MOTION_DOT": 0.0,
        "MEAN_MOTION_DDOT": 0.0,
    }
    (tle_dir / "25544.json").write_text(json.dumps(omm))

    page.goto(live_app.base_url)
    resp = page.request.patch(
        f"{live_app.base_url}/api/settings",
        data={
            "location": {"lat": 85.0, "lon": 0.0, "altitude_m": 0, "timezone": "UTC"},
            "tle_cache_dir": str(tle_dir),
        },
    )
    assert resp.ok
    page.reload()  # picks up the now-resolvable location/TLE on a fresh load
    page.get_by_role("tab", name="Satellites").click()

    sky = page.locator("sky-view")
    track = sky.locator("button", has_text="TRACK")
    expect(track).to_be_disabled()
    expect(sky.get_by_text("below horizon", exact=False)).to_be_visible()


def test_survey_results_span_spectrum_and_compact_decode_sidebar(live_app, page: Page):
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(live_app.base_url)
    workspace = page.locator(".survey-first-workspace")
    expect(workspace).to_have_count(1)
    expect(workspace.locator("survey-view")).to_have_count(1)
    visual = workspace.locator(".survey-visual")
    results = workspace.locator(".survey-results")
    sidebar = workspace.locator(".survey-first-sidebar")
    expect(results.locator(".survey-results-title")).to_have_text("Signal measurements")
    expect(results.locator("#survey-labels-table")).to_be_visible()
    expect(sidebar.locator("decode-deck .scanner-deck-line .label").first).to_have_text("Decode")
    spectrum_box = visual.bounding_box()
    results_box = results.bounding_box()
    sidebar_box = sidebar.bounding_box()
    assert spectrum_box and results_box and sidebar_box
    assert abs(results_box["x"] - spectrum_box["x"]) < 3
    assert results_box["x"] + results_box["width"] >= sidebar_box["x"] + sidebar_box["width"] - 3
    assert results_box["y"] >= max(
        spectrum_box["y"] + spectrum_box["height"], sidebar_box["y"] + sidebar_box["height"]
    )

    expand = sidebar.locator('decode-deck button[title="Expand"]')
    if expand.count():
        expand.click()
    expect(sidebar.get_by_label("Start decoding", exact=True)).to_have_count(1)
    expect(sidebar.get_by_label("Start band decoding", exact=True)).to_have_count(1)
    expect(sidebar.get_by_title("Decode setup")).to_have_count(1)
    expect(sidebar.get_by_role("button", name="Prepare receiver", exact=True)).to_have_text("⇄")
    assert sidebar.locator(".listen-decode-controls").evaluate(
        "el => getComputedStyle(el).getPropertyValue('--context-decode').trim()"
    ) == "#a8f348"
    assert sidebar.locator(".listen-workbench").evaluate(
        "el => getComputedStyle(el).getPropertyValue('--context-listen').trim()"
    ) == "#e8d65b"
    deck_box = sidebar.locator("decode-deck .scanner-deck").bounding_box()
    header_box = sidebar.locator("decode-deck .scanner-deck-line").first.bounding_box()
    assert deck_box and header_box
    assert header_box["x"] - deck_box["x"] < 14  # controls begin at the left edge
    assert header_box["width"] > deck_box["width"] * 0.85
    page.set_viewport_size({"width": 900, "height": 900})
    compact_visual = visual.bounding_box()
    compact_sidebar = sidebar.bounding_box()
    compact_results = results.bounding_box()
    assert compact_visual and compact_sidebar and compact_results
    assert compact_results["y"] >= compact_sidebar["y"] + compact_sidebar["height"]
    assert abs(compact_results["width"] - compact_visual["width"]) < 3


def test_unverified_native_profile_remains_visible_in_restore_trail(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.complete")
    response = page.request.post(
        live_app.base_url + "/api/profiles",
        data={"name": "Native test", "native_profile_name": "does-not-exist"},
    )
    profile_id = response.json()["id"]
    applied = page.request.post(live_app.base_url + f"/api/profiles/{profile_id}/apply")
    assert applied.json()["status"] == "partial"
    trail = page.locator("#restore-trail")
    expect(trail).to_be_visible()
    expect(trail).to_contain_text("native profile selection unverified")
    expect(trail).to_contain_text("partial: does-not-exist")


def test_decode_messages_bounds_rows_and_restores_focus(live_app, page: Page):
    events = [dict(id=str(i), ts=1700000000+i, decoder="ais", kind="text",
                   text=f"Vessel {i}", frequency_hz=161975000,
                   fields={"mmsi": str(i)}, integrity="crc") for i in range(500)]
    page.route("**/api/decoder/events?*", lambda route: route.fulfill(json=events))
    page.goto(live_app.base_url)
    opener = page.get_by_title("All decoded messages").first
    opener.click()
    view = page.locator("decode-view")
    close = view.get_by_role("button", name="close messages")
    expect(close).to_be_focused()
    expect(view.locator(".list > .row")).to_have_count(50)
    view.get_by_role("button", name="Show more messages").click()
    expect(view.locator(".list > .row")).to_have_count(100)
    close.focus()
    page.keyboard.press("Shift+Tab")
    expect(view.get_by_role("button", name="Show more messages")).to_be_focused()
    page.keyboard.press("Tab")
    expect(close).to_be_focused()
    close.click()
    expect(opener).to_be_focused()
    opener.click()
    expect(view.locator(".list > .row")).to_have_count(50)
    page.keyboard.press("Escape")
    expect(view.get_by_role("dialog")).to_have_count(0)
    expect(opener).to_be_focused()


def test_setup_offers_meshcore_narrow_bandwidth(live_app, page: Page):
    page.goto(live_app.base_url)
    _open_setup(page)
    section = page.locator("#setup-decoder")
    section.locator("summary").first.click()
    bandwidth = section.locator('[name="lora-bandwidth"]')
    bandwidth.select_option("62500")
    expect(bandwidth).to_have_value("62500")
    assert page.locator("setup-drawer").evaluate("el => el._draft.loraBandwidth") == 62500


def test_meshcore_receiver_profile_previews_frequency_and_phy(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.can_control")
    page.evaluate("""() => {
        const view = document.createElement('decoder-prepare');
        view.decoder = 'lora'; view.frequencyHz = 915000000;
        document.body.prepend(view);
    }""")
    view = page.locator('body > decoder-prepare')
    view.get_by_label('LoRa receiver profile').select_option('meshcore_us')
    expect(view.get_by_label('Decoder preset frequency MHz')).to_have_value('910.525')
    view.get_by_role('button', name='Preview receiver preset').click()
    expect(view.get_by_label('Decoder profile changes')).to_contain_text('62.5 kHz')
    expect(view.get_by_label('Decoder profile changes')).to_contain_text('meshcore')
    view.get_by_label('LoRa receiver profile').select_option('meshtastic_us_longfast')
    expect(view.get_by_label('Decoder preset frequency MHz')).to_have_value('906.875')
    assert view.get_by_label('Decoder preset frequency MHz').get_attribute('readonly') is not None
    view.get_by_role('button', name='Preview receiver preset').click()
    expect(view.get_by_label('Decoder profile changes')).to_contain_text('meshtastic')
    view.get_by_label('LoRa receiver profile').select_option('')
    expect(view.get_by_label('Decoder preset frequency MHz')).to_have_value('915')
    expect(view.get_by_label('Decoder preset frequency MHz')).to_be_editable()


def test_lora_profile_selector_is_in_decoder_category(live_app, page: Page):
    page.goto(live_app.base_url)
    page.wait_for_function("document.querySelector('msdr-app')?.state?.can_control")
    page.route("**/api/settings", lambda route: route.fulfill(json={"decoder": {
        "lora_protocol": "meshcore", "lora_spreading_factor": 7,
        "lora_bandwidth_hz": 62500, "lora_coding_rate": 1}}))
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const deck = document.createElement('decode-deck');
      deck.decoders = [{id:'lora',label:'LoRa / Meshtastic / MeshCore',
        available:true,demodulators:['IQ']}];
      deck.frequencyHz = 910525000;
      deck.status = {active:true,decoders:[{id:'lora',state:'running'}]};
      document.body.append(deck);
      deck._openSetup();
    }""")
    category = page.locator('#decode-channel-list .decoder-category')
    selector = category.get_by_label('LoRa receiver profile')
    expect(selector).not_to_be_visible()
    category.locator('.lora-profile-controls > summary').click()
    expect(selector).to_be_visible()
    expect(selector).to_have_value('meshcore_us')
    expect(category.get_by_label('Configured LoRa settings')).to_contain_text(
        'MeshCore · SF7 / 62.5 kHz / 4/5'
    )
    selector.select_option('meshtastic_us_longfast')
    expect(category.get_by_label('Decoder preset frequency MHz')).to_have_value('906.875')
    expect(page.locator('decoder-prepare')).to_have_count(1)

    search = page.get_by_role('searchbox', name='Search decoders')
    search.fill('nonexistent decoder')
    expect(category).not_to_be_visible()
    expect(page.get_by_text('No matching decoders', exact=True)).to_be_visible()
    search.fill('meshcore')
    expect(category).to_be_visible()
    expect(category.locator('[data-decoder-id="lora"]')).to_be_checked()
    expect(selector).to_have_value('meshtastic_us_longfast')
    search.fill('')
    expect(category).to_be_visible()


def test_decoder_restart_remembers_favorite_session(live_app, page: Page):
    page.goto(live_app.base_url)
    result = page.evaluate("""async () => {
      document.querySelector('msdr-app').remove();
      localStorage.setItem('msdr.decode.selection', JSON.stringify(['lora']));
      const deck = document.createElement('decode-deck');
      deck.decoders = [{id:'ais',demodulators:['NFM']}, {id:'lora',demodulators:['IQ']}];
      document.body.append(deck);
      deck.status = {active:true,source:'memory',since:123,decoders:[{id:'ais'}]};
      await deck.updateComplete;
      deck.status = {active:false,decoders:[]};
      await deck.updateComplete;
      let started;
      deck.addEventListener('decode-start', e => started = e.detail.decoders);
      await deck._startTop();
      // Ongoing status updates must not erase an explicit new selection.
      deck.status = {active:true,since:456,decoders:[{id:'ais'}]};
      await deck.updateComplete;
      localStorage.setItem('msdr.decode.selection', JSON.stringify(['lora']));
      deck.status = {active:true,since:456,decoders:[{id:'ais',events:5}]};
      await deck.updateComplete;
      return {started, selected:JSON.parse(localStorage.getItem('msdr.decode.selection'))};
    }""")
    assert result == {"started": ["ais"], "selected": ["lora"]}
