"""Discovery queues share one destination, including saved legacy layouts."""
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_discovery_inbox_owns_both_queues(live_app, page: Page):
    page.goto(live_app.base_url)
    page.get_by_role("tab", name="DISCOVER", exact=True).click()
    expect(page.locator("survey-view")).to_have_count(1)
    page.locator("survey-view .survey-ml-section > summary").click()
    expect(page.get_by_role("switch", name="Enable ML classifier")).to_have_count(1)
    expect(page.get_by_role("switch", name="Enable ML detector")).to_have_count(1)
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


# ml_available before live_app: the app must start with RF ML switched on.
def test_ml_controls_toggle_and_visibility_persist_independently(
    ml_available, live_app, page: Page
):
    page.goto(live_app.base_url)
    page.locator("survey-view .survey-ml-section > summary").click()
    classifier = page.get_by_role("switch", name="Enable ML classifier")
    detector = page.get_by_role("switch", name="Enable ML detector")
    expect(classifier).to_be_visible()
    expect(detector).to_be_visible()
    expect(classifier).not_to_be_checked()
    classifier.click()
    expect(classifier).to_be_checked()
    detector.click()
    expect(detector).to_be_checked()
    response = page.request.patch(
        live_app.base_url + "/api/settings", data={"features": {"ml_controls": False}}
    )
    assert response.ok
    page.reload()
    expect(classifier).to_have_count(0)
    expect(detector).to_have_count(0)
    settings = page.request.get(live_app.base_url + "/api/settings").json()
    assert settings["survey"]["ml"]["enabled"] is True
    assert settings["survey"]["ml"]["detector"]["enabled"] is True
    response = page.request.patch(
        live_app.base_url + "/api/settings", data={"features": {"ml_controls": True}}
    )
    assert response.ok
    page.reload()
    expect(classifier).to_be_checked()
    expect(detector).to_be_checked()
