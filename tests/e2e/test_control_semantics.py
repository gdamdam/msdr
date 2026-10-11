"""Control-semantics gate across the assembled UI.

Component tests run each element alone, which is how a <button> ended up
nested inside the frequency-edit <button> (0.55.1). This walks every
workspace in the real app, opens the docked inspector, the allocation
details dialog and the Setup drawer, and asserts on the live DOM:

- no interactive content inside a <button> or <a href>,
- no duplicate ids,
- every visible button or link has an accessible name,
- every open <dialog> is labelled.
"""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

_CHECK = """() => {
  const problems = [];
  const interactive = 'a[href],button,input,select,textarea,summary,[role="button"],[role="link"]';
  const label = (el) => (el.getAttribute('aria-label') || el.getAttribute('title')
    || el.textContent || '').trim().slice(0, 40);
  for (const host of document.querySelectorAll('button, a[href]')) {
    for (const inner of host.querySelectorAll(interactive)) {
      problems.push(`nested <${inner.tagName.toLowerCase()}> inside`
        + ` <${host.tagName.toLowerCase()}> "${label(host)}"`);
    }
  }
  const seen = new Map();
  for (const el of document.querySelectorAll('[id]')) seen.set(el.id, (seen.get(el.id) || 0) + 1);
  for (const [id, n] of seen) if (n > 1) problems.push(`duplicate id "${id}" x${n}`);
  for (const el of document.querySelectorAll('button, a[href]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const named = label(el) || el.getAttribute('aria-labelledby')
      || el.querySelector('img[alt], svg[aria-label], [aria-label]');
    if (!named) problems.push(
      `unnamed visible <${el.tagName.toLowerCase()}> class="${el.className}"`);
  }
  for (const d of document.querySelectorAll('dialog[open]')) {
    if (!d.getAttribute('aria-labelledby') && !d.getAttribute('aria-label')) {
      problems.push('open <dialog> without a label');
    }
  }
  return problems;
}"""


def _problems(page: Page, where: str) -> list[str]:
    return [f"[{where}] {p}" for p in page.evaluate(_CHECK)]


def test_every_workspace_has_valid_control_semantics(live_app, page: Page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(live_app.base_url)
    expect(page.locator("instrument-panel")).to_be_visible()
    workspaces = page.locator(".workspace-switch [data-workspace]").evaluate_all(
        "els => els.map(e => e.dataset.workspace)"
    )
    assert len(workspaces) >= 8, workspaces

    found: list[str] = []
    for ws in workspaces:
        page.locator(f'.workspace-switch [data-workspace="{ws}"]').click()
        page.wait_for_timeout(250)
        found += _problems(page, ws)

    # Docked inspector, the way every "select a signal" surface opens it.
    page.locator('.workspace-switch [data-workspace="listen"]').click()
    page.evaluate(
        """() => window.dispatchEvent(new CustomEvent("signal-open",
              {detail: {frequency_hz: 145787500}, bubbles: true}))"""
    )
    expect(page.locator("signal-panel")).to_be_visible()
    page.wait_for_timeout(300)
    found += _problems(page, "signal-panel")

    # Allocation details dialog (the 0.55.1 regression lived here).
    toggle = page.locator("allocation-summary .allocation-toggle").first
    if toggle.count():
        toggle.click()
        expect(page.locator("dialog.allocation-modal[open]")).to_be_visible()
        found += _problems(page, "allocation-modal")
        page.keyboard.press("Escape")

    # Setup drawer.
    page.get_by_title("Setup (,)").click()
    expect(page.locator("setup-drawer button").first).to_be_visible()
    page.wait_for_timeout(200)
    found += _problems(page, "setup-drawer")

    assert not found, "\n".join(found)
