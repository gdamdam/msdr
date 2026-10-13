import pytest
from pydantic import ValidationError

from msdr.engine.lna_auto import auto_lna_state, backoff_state, band_index
from msdr.settings import LnaAutoSettings, LnaBand


def test_band_change_applies_band_state_clamped_to_device_max():
    config = LnaAutoSettings()
    assert band_index(7_100_000, config) == 0
    assert band_index(1_090_000_000, config) == 3
    assert auto_lna_state(1_090_000_000, 162_000_000, config) == 0
    assert auto_lna_state(162_000_000, 1_090_000_000, config) == 8
    assert auto_lna_state(162_000_000, 1_090_000_000, config, lna_max=5) == 5
    assert auto_lna_state(1_090_000_000, 162_000_000, config, lna_min=2) == 2  # min clamp
    assert auto_lna_state(162_025_000, 161_975_000, config) is None  # same band
    assert auto_lna_state(162_000_000, None, config) == 8  # unknown current
    assert auto_lna_state(162_000_000, None, config.model_copy(update={"enabled": False})) is None


def test_backoff_steps_up_to_limit_over_baseline_and_device_max():
    config = LnaAutoSettings(max_backoff_steps=2)
    assert backoff_state(8, 8, config) == 9
    assert backoff_state(10, 8, config) is None
    assert backoff_state(5, None, config) == 6  # no baseline: count from current
    assert backoff_state(9, 8, config, lna_max=9) is None
    assert backoff_state(8, 8, config.model_copy(update={"overload_backoff": False})) is None


def test_bands_must_be_ordered_and_open_ended():
    with pytest.raises(ValidationError):
        LnaAutoSettings(bands=[LnaBand(up_to_hz=30_000_000, lna_state=1)])
    with pytest.raises(ValidationError):
        LnaAutoSettings(bands=[
            LnaBand(up_to_hz=300_000_000, lna_state=1),
            LnaBand(up_to_hz=30_000_000, lna_state=1),
            LnaBand(lna_state=0),
        ])
