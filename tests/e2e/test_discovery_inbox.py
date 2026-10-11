"""Discovery queues share one destination, including saved legacy layouts."""
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_discovery_inbox_owns_both_queues(live_app, page: Page):
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="DISCOVER", exact=True).click()
    expect(page.locator("survey-view")).to_have_count(1)
    expect(page.locator("finds-view")).to_have_count(0)
    page.get_by_role("button", name="Discovery inbox", exact=True).click()
    expect(page.locator("finds-view")).to_have_count(1)
    expect(page.locator("inbox-list")).to_have_count(1)
    expect(page.locator("survey-view")).to_have_count(0)
    expect(page.locator("scan-deck")).to_have_count(0)
    page.get_by_role("button", name="Open Library", exact=True).click()
    expect(page.locator("library-view")).to_have_count(1)
    expect(page.locator("inbox-list")).to_have_count(0)
    page.get_by_role("button", name="Discovery inbox ·", exact=False).click()
    expect(page.locator("finds-view")).to_have_count(1)
    expect(page.locator("inbox-list")).to_have_count(1)
    page.get_by_role("button", name="Saved signals / decodes", exact=True).click()
    expect(page.locator("discover-map")).to_have_count(1)
    expect(page.locator("finds-view")).to_have_count(0)
    expect(page.locator("inbox-list")).to_have_count(0)
