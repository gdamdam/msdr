import pytest

from msdr.decoder.receiver_preset import receiver_preset

PROPS = dict(device_vfo_frequency=144390000, device_center_frequency=144000000,
             device_sample_rate=2000000, demodulator="WFM", filter_bandwidth=200000,
             squelch_enable=True, noise_reduction_enable=True, lna_state=14)


def test_adsb_centers_and_expands_iq_without_touching_audio_or_antenna():
    p = receiver_preset("adsb", None, PROPS, 5)
    assert p["desired"] == dict(device_vfo_frequency=1090000000,
                                device_center_frequency=1090000000,
                                device_sample_rate=4000000, lna_state=5)
    assert p["before"]["lna_state"] == 14


def test_audio_decoder_sets_mode_filter_and_disables_audio_processing():
    p = receiver_preset("aprs", None, PROPS)
    assert p["desired"]["demodulator"] == "NFM"
    assert p["desired"]["filter_bandwidth"] == 12500
    assert p["desired"]["squelch_enable"] is False
    assert "lna_state" not in p["desired"]


def test_compatible_rate_and_sideband_preserved():
    p = receiver_preset("rtty", 14080000, {**PROPS, "demodulator": "LSB",
                                          "device_sample_rate": 6000000})
    assert p["desired"]["device_sample_rate"] == 6000000
    assert p["desired"]["demodulator"] == "LSB"


def test_meshcore_us_profile_sets_frequency_and_phy_settings_as_one_preview():
    current = {
        "lora_spreading_factor": 11,
        "lora_bandwidth_hz": 250_000,
        "lora_coding_rate": 2,
        "lora_protocol": "lora",
    }
    p = receiver_preset("lora", 144_390_000, PROPS, profile="meshcore_us",
                        decoder_settings=current)
    assert p["label"] == "MeshCore US (910.525 MHz)"
    assert p["desired"]["device_vfo_frequency"] == 910_525_000
    assert p["desired"]["device_center_frequency"] == 910_525_000
    assert p["decoder_settings_desired"] == {
        "lora_spreading_factor": 7,
        "lora_bandwidth_hz": 62_500,
        "lora_coding_rate": 1,
        "lora_protocol": "meshcore",
    }
    assert p["decoder_settings_before"] == current
    assert {c["property"] for c in p["decoder_settings_changes"]} == set(current)


def test_meshcore_us_profile_requires_lora_and_synchronized_decoder_settings():
    with pytest.raises(ValueError, match="does not match decoder"):
        receiver_preset("adsb", None, PROPS, profile="meshcore_us")
    with pytest.raises(ValueError, match="not synchronized"):
        receiver_preset("lora", None, PROPS, profile="meshcore_us")


def test_meshtastic_us_longfast_profile_matches_radio_preset():
    current = {
        "lora_spreading_factor": 7,
        "lora_bandwidth_hz": 62_500,
        "lora_coding_rate": 4,
        "lora_protocol": "meshcore",
    }
    p = receiver_preset("lora", None, PROPS, profile="meshtastic_us_longfast",
                        decoder_settings=current)
    assert p["label"] == "Meshtastic US LongFast (906.875 MHz)"
    assert p["desired"]["device_vfo_frequency"] == 906_875_000
    assert p["decoder_settings_desired"] == {
        "lora_spreading_factor": 11,
        "lora_bandwidth_hz": 250_000,
        "lora_coding_rate": 1,
        "lora_protocol": "meshtastic",
    }


@pytest.mark.parametrize("decoder,hz", [("bogus", 123), ("adsb", 144390000), ("aprs", -1)])
def test_reject_invalid_decoder_or_frequency(decoder, hz):
    with pytest.raises(ValueError):
        receiver_preset(decoder, hz, PROPS)
