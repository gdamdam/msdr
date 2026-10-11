from msdr.radio.registry import RESTORABLE, decode, encode


def test_decode_bool_from_various_wire_forms():
    assert decode("squelch_enable", "true") is True
    assert decode("squelch_enable", "false") is False
    assert decode("squelch_enable", 1) is True
    assert decode("squelch_enable", "1") is True


def test_decode_int_from_string_and_float():
    assert decode("device_vfo_frequency", "145800000") == 145_800_000
    assert decode("device_vfo_frequency", 145800000.0) == 145_800_000


def test_encode_bool_true():
    assert encode("squelch_enable", True) == "true"


def test_encode_int():
    assert encode("device_sample_rate", 2_000_000) == "2000000"


def test_restorable_sorted_by_stage_and_complete():
    stages = [row.stage for row in RESTORABLE]
    assert stages == sorted(stages)
    names = {row.name for row in RESTORABLE}
    assert names == {
        "active_antenna",
        "device_sample_rate",
        "device_center_frequency",
        "device_vfo_frequency",
        "demodulator",
        "filter_bandwidth",
        "lna_state",
        "squelch_enable",
        "squelch_threshold",
        "agc_enable",
        "agc_threshold",
        "noise_reduction_enable",
        "noise_reduction_strength",
    }
    assert len(RESTORABLE) == 13


def test_unknown_prop_decode_passthrough():
    assert decode("some_future_property", "unchanged") == "unchanged"
    assert decode("some_future_property", 42) == 42
