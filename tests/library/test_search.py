from msdr.engine.state import ReceiverState
from msdr.library.models import ReceiverProfile, Signal
from msdr.library.search import match_heard, nearby, rank


def _state(**overrides) -> ReceiverState:
    defaults = dict(
        vfo_hz=1_000_010,
        center_hz=1_000_000,
        sample_rate_hz=2_000_000,
        demodulator="FM",
        filter_bandwidth_hz=2_000,
        lna_state=0,
        squelch_enabled=False,
        squelch_threshold=0.0,
        audio_agc_enabled=False,
        audio_agc_threshold=0.0,
        noise_reduction_enabled=False,
        noise_reduction_strength=0,
        antenna="ANT1",
        captured_at=0.0,
        source="observed",
    )
    defaults.update(overrides)
    return ReceiverState(**defaults)


def test_rank_frequency_query_yields_tune_row_then_nearby_signal():
    nearby_signal = Signal(
        name="Nearby", frequency_hz=137_905_000, color="cyan", tag_ids=["t1"]
    )
    results = rank("137.9", [nearby_signal], [], {}, nearby_hz=10_000)
    assert results[0].kind == "tune_frequency"
    assert results[0].frequency_hz == 137_900_000
    assert len(results) == 2
    assert results[1].kind == "signal"
    assert results[1].id == nearby_signal.id
    assert results[1].color == "cyan"
    assert results[1].tag_ids == ["t1"]


def test_rank_fuzzy_prefix_boost_breaks_ties():
    meteor = Signal(name="Meteor M2-4", frequency_hz=100_000_000, color="green", tag_ids=["t2"])
    meteorology = Signal(name="Some meteorology", frequency_hz=100_000_000)
    results = rank("met", [meteorology, meteor], [], {}, nearby_hz=1_000)
    assert [r.title for r in results] == ["Meteor M2-4", "Some meteorology"]
    assert results[0].color == "green"
    assert results[0].tag_ids == ["t2"]


def test_rank_profile_uses_profile_title_prefix():
    profile = ReceiverProfile(name="VHF Sat")
    results = rank("vhf", [], [profile], {}, nearby_hz=1_000)
    assert results[0].title == "Profile: VHF Sat"


def test_nearby_uses_bandwidth_over_nearby_hz_when_larger():
    signal = Signal(name="X", frequency_hz=1_008_000)
    assert nearby([signal], 1_000_000, bandwidth_hz=20_000, nearby_hz=1_000) == [signal]
    assert nearby([signal], 1_000_000, bandwidth_hz=None, nearby_hz=1_000) == []


def test_match_heard_prefers_same_demodulator_over_nearest():
    closer_wrong_demod = Signal(name="A", frequency_hz=1_000_000, demodulator="AM")
    farther_right_demod = Signal(name="B", frequency_hz=1_000_050, demodulator="FM")
    state = _state()  # vfo_hz=1_000_010, demodulator="FM"
    result = match_heard([closer_wrong_demod, farther_right_demod], state, nearby_hz=100)
    assert result is farther_right_demod


def test_unknown_name_formats_frequency():
    assert Signal.unknown_name(152_347_500) == "UNKNOWN 152.347500 MHz"


def test_receiver_profile_matches_ignores_none_fields():
    profile = ReceiverProfile(name="P", antenna="ANT1")
    assert profile.matches({"active_antenna": "ANT1", "device_vfo_frequency": 999}) is True
    assert profile.matches({"active_antenna": "ANT2"}) is False

    empty_profile = ReceiverProfile(name="Empty")
    assert empty_profile.matches({"active_antenna": "ANT1"}) is False
