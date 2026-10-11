from msdr.survey.decoder_candidates import decoder_candidates


def _ids(candidates):
    return {item["id"] for item in candidates}


def test_shared_fsk_rate_suggests_compatible_protocols_without_identifying_them():
    candidates = decoder_candidates(150_000_000, {"kind": "fm_fsk"}, 4800)
    assert _ids(candidates) == {"dstar", "ysf", "m17", "rs41"}
    assert {item["evidence_kind"] for item in candidates} == {"iq_estimate"}
    assert all("Measured FM/FSK" in item["reason"] for item in candidates)
    assert all(item["limitations"] for item in candidates)


def test_other_symbol_rates_produce_only_the_matching_measured_hints():
    assert _ids(decoder_candidates(150_000_000, {"kind": "fm_fsk"}, 2400)) == {"dpmr"}
    assert _ids(decoder_candidates(150_000_000, {"kind": "fm_fsk"}, 1200)) == {"endoftrain"}
    assert _ids(decoder_candidates(7_000_000, {"kind": "fm_fsk"}, 100)) == {"dsc"}


def test_allocation_contexts_are_explicitly_not_detection():
    cases = (
        (1_090_000_000, "adsb"),
        (113_000_000, "vor"),
        (110_000_000, "ils"),
        (331_000_000, "ils"),
        (14_074_000, "ft8"),
    )
    for frequency_hz, expected_id in cases:
        candidate = next(
            item for item in decoder_candidates(frequency_hz) if item["id"] == expected_id
        )
        assert candidate["evidence_kind"] == "allocation"
        assert candidate["limitations"]


def test_frequency_only_never_suggests_digital_voice_or_rs41_as_detected():
    assert decoder_candidates(155_000_000) == []
    for item in decoder_candidates(155_000_000, {"kind": "fm_fsk"}, 4800):
        assert item["id"] in {"dstar", "ysf", "m17", "rs41"}
        assert item["evidence_kind"] == "iq_estimate"


def test_ft8_frequency_context_is_not_a_detected_signal_and_outside_tone_range_is_empty():
    ft8 = next(item for item in decoder_candidates(14_075_000) if item["id"] == "ft8")
    assert ft8["evidence_kind"] == "allocation"
    assert "not been detected" in ft8["limitations"]
    assert "ft8" not in _ids(decoder_candidates(14_080_000))


def test_measured_am_plus_navigation_allocation_is_only_a_weak_estimate():
    candidates = decoder_candidates(110_000_000, {"kind": "am"}, None)
    assert _ids(candidates) == {"vor", "ils"}
    assert {item["evidence_kind"] for item in candidates} == {"iq_estimate"}
    assert all(
        "not unique" in item["limitations"] or "not identify" in item["limitations"]
        for item in candidates
    )
