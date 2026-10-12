from playwright.sync_api import Page, expect


def test_messages_window_resizes_without_closing(live_app, page: Page):
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(live_app.base_url)
    page.wait_for_function("() => document.querySelector('msdr-app')?.decoders?.length > 0")
    page.evaluate("document.querySelector('msdr-app').decodeViewOpen = true")
    panel = page.locator(".decode-evidence-panel")
    expect(panel).to_be_visible()
    before = panel.bounding_box()
    # Drag the native resize grip (bottom-right) out past the panel's edge:
    # the drag ends over the backdrop, which must not close the window.
    page.mouse.move(before["x"] + before["width"] - 3, before["y"] + before["height"] - 3)
    page.mouse.down()
    page.mouse.move(
        before["x"] + before["width"] + 120, before["y"] + before["height"] + 150, steps=8
    )
    page.mouse.up()
    expect(panel).to_be_visible()
    after = panel.bounding_box()
    assert after["width"] > before["width"] + 50
    assert after["height"] > before["height"] + 50
    # A plain click on the backdrop still closes it.
    page.mouse.click(5, 890)
    expect(panel).to_have_count(0)
