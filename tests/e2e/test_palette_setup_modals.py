"""Keyboard ordering and dialog focus regressions for the top-level overlays."""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_palette_keyboard_uses_grouped_order_and_hovered_row(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("document.querySelector('msdr-app').paletteOpen = true")
    palette = page.locator("msdr-palette")
    query = palette.locator("input[name='palette-query']")
    expect(query).to_be_focused()
    palette.evaluate("""async (el) => {
      await el._loadEmpty();
      el._results = [
        {kind: "action", id: "palette-probe", title: "action", subtitle: ""},
        {kind: "signal", id: "signal-one", title: "signal one", subtitle: ""},
        {kind: "signal", id: "signal-two", title: "signal two", subtitle: ""},
        {kind: "tune_frequency", id: "tune", title: "tune", subtitle: "", frequency_hz: 145800000},
      ];
      await el.updateComplete;
      window.paletteProbe = null;
      el.addEventListener("palette-probe", () => { window.paletteProbe = "action" });
      el.addEventListener("tune-frequency", (ev) => { window.paletteProbe = ev.detail.hz });
      el.addEventListener("tune-signal", (ev) => { window.paletteProbe = ev.detail.id });
    }""")

    rows = palette.locator(".list .row")
    expect(rows).to_have_count(4)
    assert [row.inner_text().splitlines()[0] for row in rows.all()] == [
        "tune", "signal one", "signal two", "action"
    ]
    query.press("Enter")
    assert page.evaluate("window.paletteProbe") == 145800000
    expect(palette.locator('[role="dialog"]')).to_have_count(0)

    page.evaluate("document.querySelector('msdr-app').paletteOpen = true")
    expect(query).to_be_visible()
    palette.evaluate("""async (el) => {
      // Reopening re-triggers the component's own _loadEmpty() (a real
      // /api/signals/recent fetch); drain it before overriding _results, or
      // its late resolution can clobber the fixture right as the test's
      // Enter keypress lands (see test history for the flake this caused).
      await el._loadEmpty();
      el._results = [
        {kind: "action", id: "palette-probe", title: "action", subtitle: ""},
        {kind: "signal", id: "signal-one", title: "signal one", subtitle: ""},
        {kind: "signal", id: "signal-two", title: "signal two", subtitle: ""},
        {kind: "tune_frequency", id: "tune", title: "tune", subtitle: "", frequency_hz: 145800000},
      ];
      await el.updateComplete;
    }""")
    rows.nth(1).dispatch_event("mouseenter")
    assert palette.evaluate("el => el._index") == 1
    query.press("Enter")
    assert page.evaluate("window.paletteProbe") == "signal-one"


def test_palette_ignores_stale_recent_and_search_responses(live_app, page: Page):
    page.goto(live_app.base_url)
    page.evaluate("""() => {
      window.paletteRequests = [];
      window.paletteDone = {};
      const originalFetch = window.fetch.bind(window);
      const response = (data) => new Response(JSON.stringify(data), {
        status: 200, headers: {"Content-Type": "application/json"}
      });
      window.fetch = (input, options) => {
        const url = new URL(String(input), window.location.href);
        if (url.pathname === "/api/signals/recent") {
          window.paletteRequests.push("recent");
          return new Promise((resolve) => setTimeout(() => {
            window.paletteDone.recent = true;
            resolve(response([{id: "stale-recent", name: "stale recent", frequency_hz: 1000000}]));
          }, 240));
        }
        if (url.pathname === "/api/palette" && url.searchParams.get("q") === "old") {
          window.paletteRequests.push("old");
          return new Promise((resolve) => setTimeout(() => {
            window.paletteDone.old = true;
            resolve(response([{kind: "action", id: "old", title: "old result", subtitle: ""}]));
          }, 360));
        }
        if (url.pathname === "/api/palette" && url.searchParams.get("q") === "new") {
          window.paletteRequests.push("new");
          return Promise.resolve(response([
            {kind: "action", id: "new", title: "new result", subtitle: ""}
          ]));
        }
        return originalFetch(input, options);
      };
    }""")
    page.evaluate("document.querySelector('msdr-app').paletteOpen = true")
    palette = page.locator("msdr-palette")
    query = palette.locator("input[name='palette-query']")
    query.fill("old")
    page.wait_for_function("window.paletteRequests.includes('old')")
    query.fill("new")
    expect(palette.locator(".list .row")).to_contain_text("new result")
    page.wait_for_function("window.paletteDone.old && window.paletteDone.recent")
    expect(palette.locator(".list .row")).to_have_count(1)
    expect(palette.locator(".list .row")).to_contain_text("new result")


def test_setup_dialog_contains_focus_and_restores_launcher(live_app, page: Page):
    page.goto(live_app.base_url)
    launcher = page.get_by_title("Setup (,)")
    launcher.focus()
    launcher.press("Enter")
    dialog = page.locator("setup-drawer [role='dialog']")
    expect(dialog).to_be_visible()
    expect(page.locator("setup-drawer").get_by_role("heading", name="Radio")).to_be_visible()
    expect(dialog).to_have_attribute("aria-modal", "true")
    close = dialog.get_by_role("button", name="Close setup")
    expect(close).to_be_focused()
    page.get_by_role("tab", name="Listen").focus()
    expect(close).to_be_focused()

    close.press("Shift+Tab")
    expect(dialog.get_by_role("button", name="Save", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(launcher).to_be_focused()
