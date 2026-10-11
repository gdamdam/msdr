"""Inspector groups preserve evidence source and freshness without duplicates."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_signal_inspector_groups_evidence_by_source_and_freshness(live_app, page: Page):
    response = {
        "frequency_hz": 162430000,
        "memory": {"id": "memory-1", "name": "NOAA WX4", "notes": "local receiver",
                   "status": "identified"},
        "find": None,
        "survey_label": {
            "frequency_hz": 162430000,
            "analysis_state": {"state": "current_result", "at": 1_800_000_000},
            "analyzed_at": 1_800_000_000,
            "family": {"name": "Speech-like NFM", "confidence": 0.88},
            "last_classification": {
                "name": "Speech-like NFM", "at": 1_799_999_000,
                "source": "analyzer",
            },
        },
        "last_classification": {
            "name": "Speech-like NFM", "at": 1_799_999_000,
            "source": "analyzer",
        },
        "evidence": [
            {
                "kind": "analyzer", "title": "Speech-like NFM",
                "detail": "NFM voice estimate", "evidence_type": "iq_estimate",
                "state": "estimated", "score": 0.88, "source": "analyzer",
                "source_group": "dsp", "producer_role": "analyzer",
                "at": 1_800_000_000, "support": [],
            },
            {
                "kind": "decoded", "title": "Decoded (nfm)",
                "detail": "valid frame", "evidence_type": "decoder_output",
                "state": "decoder_output", "score": None, "source": "nfm",
                "source_group": "decoder", "producer_role": "decoder",
                "at": 1_800_000_000,
            },
            {
                "kind": "memory", "title": "NOAA WX4",
                "detail": "local receiver", "evidence_type": "operator",
                "state": "operator", "score": None, "source": "memory",
                "source_group": "memory", "producer_role": "operator",
                "at": None,
            },
            {
                # kind "bandplan" (0.53.0, 84e0f49) is excluded from the
                # evidence-group list -- it's shown in full in the header's
                # "Channel / application hints" instead (checked below).
                "kind": "bandplan", "title": "NOAA weather radio",
                "detail": "allocation only", "evidence_type": "allocation",
                "state": "context", "score": None, "source": "bandplan",
                "source_group": "context", "producer_role": "bandplan",
                "at": None,
            },
            {
                # kind "scheduled" also maps to the "context" group, and
                # (unlike bandplan) isn't filtered out of it -- this is what
                # actually populates [data-evidence-group="context"] below.
                "kind": "scheduled", "title": "NOAA WX scheduled",
                "detail": "catalog schedule match", "evidence_type": "schedule",
                "state": "context", "score": None, "source": "catalog",
                "source_group": "context", "producer_role": "catalog",
                "at": None,
            },
        ],
        "messages": [], "observations": [], "suggested_decoders": [],
        "demodulator": "NFM", "bandwidth_hz": 12500,
    }
    page.route(
        "**/api/signal-detail**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(response)
        ),
    )
    page.goto(live_app.base_url)
    # The survey-first sidebar layout (44be426, 514af13) moved each panel's
    # workspace-context accent off the outer container: the Listen section's
    # own border is reset to 0 (its colored edge lives on the inner
    # .listen-workbench-left wrapper's top border instead), while the other
    # three panels carry their pure context color on the left border (the
    # top border there is a separate, blended tint).
    listen_sel = ".listen-workbench-left"
    decoder_controls_sel = ".listen-decode-controls .scanner-deck"
    # survey-view's own .panel is flattened to display:contents in this layout
    # (its children carry the box); .survey-visual is the real rendered panel.
    survey_sel = ".primary-spectrum > survey-view > .panel .survey-visual"
    receiver_sel = "instrument-panel > #instrument-panel"
    for selector, side, color, tints in (
        # The Listen panel's inner wrapper keeps a flat, untinted background
        # by design in this layout -- only its top border carries context color.
        (listen_sel, "borderTopColor", "rgb(232, 214, 91)", False),
        (decoder_controls_sel, "borderLeftColor", "rgb(168, 243, 72)", True),
        # .survey-visual's background tint is a fixed 7% mix in this layout,
        # not driven by --evidence-panel-tint, so it doesn't change with the
        # outline/tinted setting either.
        (survey_sel, "borderLeftColor", "rgb(54, 203, 187)", False),
        (receiver_sel, "borderLeftColor", "rgb(77, 127, 196)", True),
    ):
        panel = page.locator(selector)
        expect(panel).to_be_visible()
        assert panel.evaluate(f"el => getComputedStyle(el).{side}") == color
        if tints:
            tinted_background = panel.evaluate("el => getComputedStyle(el).backgroundColor")
            page.evaluate("document.documentElement.dataset.evidencePanelStyle = 'outline'")
            # A reactive re-render elsewhere in the app can land between the
            # dataset write above and the read below; poll instead of
            # asserting once (no assertion is loosened, this still fails if
            # the background never actually changes).
            page.wait_for_function(
                "([sel, prev]) => getComputedStyle(document.querySelector(sel))"
                ".backgroundColor !== prev",
                arg=[selector, tinted_background],
            )
            page.evaluate("document.documentElement.dataset.evidencePanelStyle = 'tinted'")
    for chosen, previous in (("outline", "tinted"), ("tinted", "outline")):
        page.evaluate("document.querySelector('msdr-app').setupOpen = true")
        page.locator("setup-drawer #setup-appearance > summary").click()
        selector = page.locator('setup-drawer select[name="evidence-panel-style"]')
        expect(selector).to_have_value(previous)
        selector.select_option(chosen)
        page.locator('setup-drawer').get_by_role('button', name='Save', exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-evidence-panel-style', chosen)
        saved = page.request.get(live_app.base_url + '/api/settings').json()
        assert saved['evidence_panel_style'] == chosen
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-evidence-panel-style', 'tinted')
    page.evaluate("""() => {
      document.querySelector('msdr-app').remove();
      const panel=document.createElement('signal-panel');
      panel.target={frequency_hz:162430000}; panel.open=true; document.body.append(panel);
    }""")

    inspector = page.locator("signal-panel")
    dsp = inspector.locator('[data-evidence-group="dsp"]')
    decoder = inspector.locator('[data-evidence-group="decoder"]')
    memory = inspector.locator('[data-evidence-group="memory"]')
    context = inspector.locator('[data-evidence-group="context"]')
    expect(dsp).to_be_visible()
    expect(decoder).to_be_visible()
    expect(memory).to_be_visible()
    expect(context).to_be_visible()

    # The current DSP value is represented once even when the survey's
    # historical summary contains the same classification.
    expect(dsp.get_by_text("Speech-like NFM", exact=True)).to_have_count(1)
    expect(decoder).to_contain_text("Decoded (nfm)")
    expect(memory).to_contain_text("NOAA WX4")
    expect(context).to_contain_text("NOAA WX scheduled")
    expect(context).not_to_contain_text("NOAA weather radio")
    expect(inspector.get_by_label("Channel / application hints")).to_contain_text(
        "NOAA weather radio"
    )

    expected_colors = {
        "dsp": "rgb(69, 213, 224)",
        "decoder": "rgb(100, 221, 187)",
        "memory": "rgb(146, 175, 213)",
        "context": "rgb(239, 193, 107)",
    }
    for group, color in expected_colors.items():
        article = inspector.locator(
            f'[data-evidence-group="{group}"] article.inspector-evidence'
        ).first
        expect(article).to_be_visible()
        assert article.evaluate("el => getComputedStyle(el).borderLeftColor") == color

    card = dsp.locator("article.inspector-evidence").first
    tinted = card.evaluate("el => getComputedStyle(el).backgroundColor")
    if directory := os.environ.get("MSDR_REVIEW_OUTPUT_DIR"):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "evidence-style-c.png"), full_page=True)
    page.evaluate("document.documentElement.dataset.evidencePanelStyle = 'outline'")
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") != tinted
    assert card.evaluate("el => getComputedStyle(el).borderTopColor") == expected_colors["dsp"]
    if directory:
        page.screenshot(path=str(output / "evidence-style-b.png"), full_page=True)
    page.evaluate("document.documentElement.dataset.evidencePanelStyle = 'tinted'")
    assert card.evaluate("el => getComputedStyle(el).backgroundColor") == tinted

    # A different older DSP estimate remains available in its own collapsed
    # history disclosure while the current estimate stays the primary row.
    inspector.evaluate("""panel => {
      panel._detail={...panel._detail,last_classification:{
        name:'Data-like signal',at:1799998000,source:'analyzer'}};
    }""")
    history = inspector.locator("details.inspector-analysis-history")
    expect(history).to_be_visible()
    expect(history.locator("summary")).to_contain_text("Previous DSP classification")
    expect(dsp.get_by_text("Speech-like NFM", exact=True)).to_have_count(1)
    history.locator("summary").click()
    expect(history).to_contain_text("Data-like signal")
