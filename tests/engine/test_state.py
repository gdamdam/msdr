import pytest

from msdr.engine.state import ProfileSettings, ReceiverState

WIRE_PROPS = {
    "device_vfo_frequency": 145_800_000,
    "device_center_frequency": 145_800_000,
    "device_sample_rate": 2_000_000,
    "demodulator": "NFM",
    "filter_bandwidth": 12_500,
    "lna_state": 3,
    "active_antenna": "Antenna B",
    "squelch_enable": False,
    "squelch_threshold": -100.0,
    "agc_enable": True,
    "agc_threshold": -30.0,
    "noise_reduction_enable": False,
    "noise_reduction_strength": 0,
}


def test_from_props_builds_from_full_wire_dict():
    state = ReceiverState.from_props(WIRE_PROPS, source="capture", api_version="1.0.3")
    assert state.vfo_hz == 145_800_000
    assert state.center_hz == 145_800_000
    assert state.antenna == "Antenna B"
    assert state.squelch_enabled is False
    assert state.source == "capture"
    assert state.sdrconnect_api_version == "1.0.3"


def test_from_props_missing_prop_raises_naming_it():
    partial = dict(WIRE_PROPS)
    del partial["lna_state"]
    with pytest.raises(ValueError, match="lna_state"):
        ReceiverState.from_props(partial, source="capture")


def test_to_props_round_trips():
    state = ReceiverState.from_props(WIRE_PROPS, source="capture")
    assert state.to_props() == WIRE_PROPS


def test_key_ignores_gain_type_fields():
    a = ReceiverState.from_props(WIRE_PROPS, source="capture")
    tweaked = dict(WIRE_PROPS, lna_state=7, squelch_enable=True, agc_threshold=-20.0)
    b = ReceiverState.from_props(tweaked, source="capture")
    assert a.key() == b.key()


def test_key_differs_on_dwell_identity_fields():
    a = ReceiverState.from_props(WIRE_PROPS, source="capture")
    retuned = dict(WIRE_PROPS, device_vfo_frequency=146_000_000)
    b = ReceiverState.from_props(retuned, source="capture")
    assert a.key() != b.key()


def test_profile_settings_to_props_excludes_none_and_native_name():
    settings = ProfileSettings(native_profile_name="VHF", antenna="Antenna A", lna_state=2)
    assert settings.to_props() == {"active_antenna": "Antenna A", "lna_state": 2}
