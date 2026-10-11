"""Signal inspection UI over a replayed synthetic carrier (see conftest)."""

import json
from pathlib import Path

import numpy as np
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

_FS = 240_000
_CENTER = 100_000_000


def _write_scope_recording(root: Path) -> Path:
    directory = root / "scope-e2e"
    directory.mkdir()
    seconds = 2.0
    t = np.arange(int(_FS * seconds), dtype=np.float64) / _FS
    phase = 2 * np.pi * 45_000 * t + 2.5 * np.sin(2 * np.pi * 1_000 * t)
    z = 0.3 * np.exp(1j * phase)
    stem = f"run-01_{_FS}SPS_{_CENTER}Hz"
    iq = (np.column_stack((z.real, z.imag)) * 32767).astype("<i2").reshape(-1)
    (directory / f"{stem}.sigmf-data").write_bytes(iq.tobytes())
    (directory / f"{stem}.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "ci16_le", "core:sample_rate": _FS, "core:version": "1.0.0"},
        "captures": [{"core:sample_start": 0, "core:frequency": _CENTER}],
        "annotations": [],
    }))
    (directory / "recording.json").write_text(json.dumps({
        "start_wall": 1_700_000_000, "end_wall": 1_700_000_002,
        "preroll_s": 0, "runs": [{
            "data": f"{stem}.sigmf-data", "meta": f"{stem}.sigmf-meta",
            "sample_rate": _FS, "center_hz": _CENTER, "samples": len(t),
            "start_wall": 1_700_000_000, "settings": {},
        }],
    }))
    return directory


def test_scope_draws_trigger_freeze_cursors_and_refreshes_after_seek(
    live_app, page: Page, tmp_path
):
    recording = _write_scope_recording(tmp_path)
    base = live_app.base_url
    assert page.request.post(
        f"{base}/api/survey/replay", data={"path": str(recording), "loop": True}
    ).status == 200
    assert page.request.post(f"{base}/api/review/tune", data={
        "frequency_hz": _CENTER + 45_000, "demodulator": "NFM", "bandwidth_hz": 12_500,
    }).status == 200

    page.goto(base)
    page.wait_for_function("() => customElements.get('scope-panel')")
    page.evaluate("""() => {
      document.querySelector('msdr-app')?.remove();
      const panel = document.createElement('scope-panel');
      panel.mode = 'review'; panel.active = true;
      document.body.append(panel);
    }""")
    panel = page.locator("scope-panel")
    panel.locator("details.scope-panel > summary").click()
    plot = panel.locator(".scope-plot")
    expect(plot).to_be_visible(timeout=5000)
    expect(plot.locator("path").nth(1)).not_to_have_attribute("d", "")

    with page.expect_response("**/api/inspection/review/configure") as configured:
        panel.get_by_label("Scope trigger", exact=True).select_option("rising")
    assert configured.value.status == 200
    expect(panel.locator(".scope-panel")).to_contain_text("trigger")
    carrier = panel.locator("details").nth(1)
    carrier.locator("summary").click()
    with page.expect_response("**/api/inspection/review/drift") as drift_response:
        carrier.get_by_role("button", name="Measure carrier offset").click()
    assert drift_response.value.status == 200
    expect(panel.locator(".scope-panel")).to_contain_text("Measurement only", timeout=3000)
    expect(panel.locator(".scope-panel")).not_to_contain_text('{"offset_hz"')
    with page.expect_response("**/api/inspection/review/drift") as reconfigured:
        carrier.get_by_label("Maximum carrier correction").fill("450")
        carrier.get_by_label("Maximum carrier correction").press("Tab")
    assert reconfigured.value.status == 200

    panel.get_by_role("button", name="Freeze scope").click()
    frozen_path = plot.locator("path").nth(1).get_attribute("d")
    page.wait_for_timeout(650)
    assert plot.locator("path").nth(1).get_attribute("d") == frozen_path

    set_cursor = "el => { el.value = VALUE; el.dispatchEvent(new Event('input', {bubbles:true})); }"
    panel.get_by_label("Scope cursor A").evaluate(set_cursor.replace("VALUE", "'10'"))
    panel.get_by_label("Scope cursor B").evaluate(set_cursor.replace("VALUE", "'90'"))
    expect(panel.get_by_text("Δt", exact=False)).to_be_visible()
    expect(panel.get_by_role("button", name="Resume scope")).to_be_visible()

    old = page.request.get(f"{base}/api/inspection/review?window_s=0.1&points=256").json()
    old_generation = old["streams"]["audio"]["generation"]
    assert page.request.post(f"{base}/api/review/seek", data={"position_s": 0.8}).status == 200
    with page.expect_response("**/api/inspection/review?window_s=0.1&points=512"):
        panel.get_by_role("button", name="Resume scope").click()
    new = None
    for _ in range(40):
        new = page.request.get(f"{base}/api/inspection/review?window_s=0.1&points=256").json()
        if new["streams"]["audio"]["generation"] > old_generation:
            break
        page.wait_for_timeout(25)
    assert new["streams"]["audio"]["generation"] > old_generation
    expect(panel).to_contain_text(f"generation {new['streams']['audio']['generation']}")

    panel.get_by_label("Scope view").select_option("audio_spectrum")
    expect(panel.locator(".scope-plot")).to_have_attribute(
        "aria-label", "audio_spectrum oscilloscope trace"
    )
    panel.get_by_role("button", name="Freeze scope").click()
    frozen_spectrum = panel.locator(".scope-plot path").nth(1).get_attribute("d")
    page.wait_for_timeout(350)
    assert panel.locator(".scope-plot path").nth(1).get_attribute("d") == frozen_spectrum

    page.request.post(f"{base}/api/survey/replay/stop")
