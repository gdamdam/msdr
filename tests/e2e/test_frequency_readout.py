"""Displayed MHz and digit hit targets must describe the same frequency."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    "hz, text, first_place",
    [(160_000_000, "160.000000", 100_000_000),
     (7_100_000, "7.100000", 1_000_000),
     (1_200_000_000, "1200.000000", 1_000_000_000)],
)
def test_mhz_render_and_digit_step(live_app, page, hz, text, first_place):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('segment-readout')")
    page.evaluate("""hz => {
        const el = document.createElement('segment-readout');
        el.id = 'frequency-under-test'; el.hz = hz; el.editable = true;
        el.addEventListener('tune-frequency', e => window.testTune = e.detail.hz);
        document.body.prepend(el);
    }""", hz)
    component = page.locator('#frequency-under-test')
    expect(component.locator('.seg-value')).to_have_text(text)
    expect(component.locator('.seg-unit')).to_have_text('MHz')
    bounds = component.locator('.seg-readout').bounding_box()
    page.mouse.click(bounds['x'] + bounds['width'] / len(text) / 2,
                     bounds['y'] + bounds['height'] / 4)
    page.wait_for_function('hz => window.testTune === hz', arg=hz + first_place)
    page.mouse.click(bounds['x'] + bounds['width'] * (len(text) - 0.5) / len(text),
                     bounds['y'] + bounds['height'] / 4)
    page.wait_for_function('hz => window.testTune === hz', arg=hz + 1)
    component.get_by_role('button', name='Enter frequency').click()
    field = component.locator('input')
    field.fill('160.000')
    field.press('Enter')
    page.wait_for_function('() => window.testTune === 160000000')


def test_fine_tune_tap_hold_acceleration_and_release(live_app, page):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('segment-readout')")
    page.evaluate("""() => {
        window.fineSteps = [];
        const el = document.createElement('segment-readout');
        el.id = 'fine-tune-under-test';
        el.hz = 451_100_000;
        el.editable = true;
        el.fineTune = true;
        el.addEventListener('step-frequency', e => window.fineSteps.push(e.detail));
        document.body.prepend(el);
    }""")
    up = page.locator('#fine-tune-under-test').get_by_role('button', name='Fine tune up')
    down = page.locator('#fine-tune-under-test').get_by_role('button', name='Fine tune down')
    expect(up).to_be_visible()
    up.click()
    assert page.evaluate('window.fineSteps') == [{'direction': 1, 'fixedStepHz': 100}]

    down.hover()
    page.mouse.down()
    page.wait_for_timeout(3250)
    page.mouse.up()
    steps = page.evaluate('window.fineSteps')
    assert steps[1] == {'direction': -1, 'fixedStepHz': 100}
    assert any(step == {'direction': -1, 'fixedStepHz': 500} for step in steps)
    assert any(step == {'direction': -1, 'fixedStepHz': 1000} for step in steps)
    page.wait_for_timeout(350)
    assert page.evaluate('window.fineSteps') == steps

    up.focus()
    up.press('Enter')
    assert page.evaluate('window.fineSteps')[-1] == {'direction': 1, 'fixedStepHz': 100}
