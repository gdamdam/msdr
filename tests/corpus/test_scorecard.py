"""Corpus scorecard: deterministic expectations only -- the
scorecard itself runs a much bigger synthetic corpus (see
docs/validation/scorecard-synthetic.md), but this test asserts exactly what
the plan calls out as non-negotiable: negatives produce no decoder output
and no protocol/station claim, AIS burst decodes (or is honestly recorded as
"unavailable"), keyed carrier is never called CW, and noise is never voice.
"""

from __future__ import annotations

import numpy as np
import pytest_asyncio

from msdr.validation.scorecard import (
    SensitivityResult,
    _noisy,
    render_markdown,
    run_scorecard,
)


# The default fixture loop scope is "function"; a module-scoped async fixture
# must ask for a module loop or pytest-asyncio raises ScopeMismatch.
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def report():
    return await run_scorecard()


def _case(report, case_id: str):
    return next(c for c in report.cases if c.case_id == case_id)


async def test_negatives_produce_no_decoder_output_and_no_protocol_claim(report):
    negatives = [c for c in report.cases if c.is_negative]
    assert len(negatives) >= 5
    for case in negatives:
        counts = [
            (d.name, d.status, d.valid_frames, d.invalid_frames, d.unknown_integrity_frames)
            for d in case.decoders
        ]
        assert case.decoder_events_total() == 0, (
            f"{case.case_id}: expected zero decoder events on a negative control, got {counts}"
        )
        assert case.has_protocol_claim() is False, case.case_id


async def test_ais_burst_decodes_or_is_honestly_unavailable(report):
    case = _case(report, "gmsk9600_ais_burst")
    (ais,) = [d for d in case.decoders if d.name == "ais"]
    assert ais.status in ("available", "unavailable")
    if ais.status == "unavailable":
        # Honest, not silent: the scorecard still records the case, it just
        # can't grade it without msdr.decoder.pydecoders.ais.AisDecoder.
        assert ais.detail
    else:
        assert ais.valid_frames >= 1, ais.detail
        assert ais.expected_payload_match is True, ais.detail


async def test_keyed_carrier_is_not_cw(report):
    case = _case(report, "keyed_carrier_ook_20baud")
    (family_outcome,) = [d for d in case.detectors if d.name == "family"]
    assert "family='CW'" not in family_outcome.detail
    assert family_outcome.outcome == "correct"


async def test_noise_is_not_voice(report):
    case = _case(report, "white_noise")
    (voice_outcome,) = [d for d in case.detectors if d.name == "voice"]
    assert voice_outcome.detail != "voice"
    assert voice_outcome.outcome == "correct"


async def test_cw_case_is_the_positive_contrast_for_keyed_carrier(report):
    """Same family_for/keying_timing path as keyed_carrier, proper Morse
    timing this time -- if this ever also stopped saying "CW", the keyed-
    carrier negative control above would be meaningless (both would just be
    "never CW")."""
    case = _case(report, "cw_proper_timing")
    (family_outcome,) = [d for d in case.detectors if d.name == "family"]
    assert "family='CW'" in family_outcome.detail


async def test_markdown_renders(report):
    md = render_markdown(report)
    assert "synthetic fixtures only" in md.lower()
    assert "not a claim of field accuracy" in md.lower()
    assert "## Detectors" in md
    assert "## Decoders" in md
    for case in report.cases:
        assert case.label in md


def test_sensitivity_threshold_ignores_a_lucky_pass_below_a_failing_level():
    r = SensitivityResult("x", trials=5, passes={10: 5, 8: 5, 6: 3, 4: 5, 2: 0})
    assert r.threshold_db == 8  # 4 dB decoded every seed, but 6 dB above it did not
    assert SensitivityResult("x", trials=5, passes={10: 4, 8: 5}).threshold_db is None
    assert SensitivityResult("x", trials=5).threshold_db is None  # not run


def test_noisy_stays_under_full_scale_at_low_snr():
    # Otherwise the int16 decoders would be measuring clipping, not noise.
    iq = (0.7 * np.exp(1j * np.linspace(0, 50, 4000))).astype(np.complex64)
    out = _noisy(iq, -8.0, seed=1)
    assert max(np.abs(out.real).max(), np.abs(out.imag).max()) <= 0.9 + 1e-6


async def test_sensitivity_sweep_is_clean_at_the_top_and_rendered(report):
    assert {r.decoder for r in report.sensitivity} == {"ais", "adsb", "rs41"}
    for r in report.sensitivity:
        if not r.passes:  # honestly unavailable, same rule as the AIS case
            assert r.detail
            continue
        assert r.passes[max(r.passes)] == r.trials, r
        assert r.threshold_db is not None
    assert "## Sensitivity (synthetic AWGN sweep)" in render_markdown(report)
