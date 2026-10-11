"""Evidence contract: independent evidence types, no probability, no
"confirmed" from allocation/schedule (plan §3)."""

import pytest
from pydantic import ValidationError

from msdr.contracts.evidence import (
    Claim,
    Measurement,
    claim_state,
    evidence_state,
    identity_established,
)


def _claim(evidence_type, subject="protocol", **kw):
    integrity = kw.pop("integrity", None)
    if integrity is None:
        integrity = (
            "not_applicable"
            if evidence_type in ("allocation", "schedule", "operator")
            else ("passed" if evidence_type == "validated_decode" else "unknown")
        )
    return Claim(
        subject=subject,
        value="AIS",
        evidence_type=evidence_type,
        producer="test",
        integrity=integrity,
        **kw,
    )


def test_allocation_and_schedule_alone_are_context_not_identity():
    claims = [
        _claim("allocation", subject="service"),
        _claim("schedule", subject="station"),
    ]
    assert evidence_state(claims) == "context"
    assert not identity_established(claims)


def test_iq_estimate_is_estimated_and_not_identity():
    claims = [_claim("allocation", subject="service"), _claim("iq_estimate")]
    assert evidence_state(claims) == "estimated"
    assert not identity_established(claims)


def test_decoder_output_without_integrity_is_not_validated():
    c = _claim("decoder_output", integrity="unavailable")
    assert claim_state(c) == "estimated"
    assert not identity_established([c])


def test_validated_decode_requires_passed_integrity():
    with pytest.raises(ValidationError):
        _claim("validated_decode", integrity="unknown")
    ok = _claim("validated_decode")
    assert claim_state(ok) == "validated"
    assert identity_established([ok])


def test_operator_identification_is_its_own_state():
    c = _claim("operator", subject="station")
    assert evidence_state([c]) == "operator"
    assert identity_established([c])


def test_superseded_claims_are_historical_and_ignored_by_default():
    old = _claim("validated_decode", superseded_by="newer")
    new = _claim("iq_estimate")
    assert old.historical
    assert evidence_state([old, new]) == "estimated"
    assert evidence_state([old, new], include_historical=True) == "validated"
    assert not identity_established([old, new])


def test_allocation_claim_cannot_carry_an_integrity_result():
    with pytest.raises(ValidationError):
        _claim("allocation", subject="service", integrity="passed")


def test_claim_has_no_probability_field():
    fields = set(Claim.model_fields)
    assert "probability" not in fields and "confidence" not in fields
    assert "score" in fields  # heuristic ranking score, named as such


def test_sample_interval_must_be_consistent():
    with pytest.raises(ValidationError):
        _claim("iq_estimate", sample_start=10)
    with pytest.raises(ValidationError):
        _claim("iq_estimate", sample_start=10, sample_end=5)
    c = _claim("iq_estimate", sample_start=5, sample_end=10)
    assert (c.sample_start, c.sample_end) == (5, 10)


def test_percent_measurement_needs_denominator_and_window():
    with pytest.raises(ValidationError):
        Measurement(name="duty", value=42.0, unit="%")
    m = Measurement(name="duty", value=42.0, unit="%", denominator="dwell time", window_s=30.0)
    assert m.unit == "%"


def test_dbfs_dbm_db_are_distinct_units():
    units = {Measurement(name="p", value=-30.0, unit=u).unit for u in ("dBFS", "dBm", "dB")}
    assert units == {"dBFS", "dBm", "dB"}


def test_historical_rfml_claims_never_count_as_evidence():
    # The RF ML feature was removed, but databases keep its claims.
    for producer in ("survey.rfml", "survey.rfml.detector"):
        c = _claim("iq_estimate", subject="modulation").model_copy(update={"producer": producer})
        assert claim_state(c) == "none"
        assert evidence_state([c]) == "none"
