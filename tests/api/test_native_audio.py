"""Native listening preferences and bounded SDRconnect audio controls."""

import json

import pytest
from pydantic import ValidationError

from msdr.settings import NativeAudioSettings, Settings
from tests.api.test_app import running_app


def test_native_audio_defaults_migrate_old_settings_and_round_trip(tmp_path):
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({"host": "127.0.0.1", "port": 5454}))
    loaded = Settings.load(legacy)
    assert loaded.native_audio.agc_mode == "default"
    assert loaded.native_audio.manual_gain == 1
    assert loaded.native_audio.ssb_low_hz == 0
    assert loaded.native_audio.ssb_high_hz is None
    assert loaded.native_audio.cw_pitch_hz == 700
    loaded.native_audio.notch_enabled = True
    loaded.native_audio.nr_strength = 0.7
    loaded.save(legacy)
    restored = Settings.load(legacy)
    assert restored.native_audio.notch_enabled is True
    assert restored.native_audio.nr_strength == 0.7


def test_native_audio_rejects_invalid_passband_and_receiver_bounds():
    with pytest.raises(ValidationError):
        NativeAudioSettings(ssb_low_hz=1000, ssb_high_hz=1100)
    with pytest.raises(ValidationError):
        NativeAudioSettings(notch_frequency_hz=7000)
    with pytest.raises(ValidationError):
        NativeAudioSettings(blanker_threshold=2)


@pytest.mark.asyncio
async def test_sdrconnect_audio_dsp_command_is_typed_and_confirmed():
    async with running_app() as (_app, client, adapter):
        response = await client.post(
            "/api/receiver/audio-dsp", json={"property": "agc_enable", "value": False}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "confirmed"
        assert body["confirmed_value"] is False
        assert ("set", "agc_enable", False) in adapter.calls

        invalid_type = await client.post(
            "/api/receiver/audio-dsp", json={"property": "agc_enable", "value": -20}
        )
        assert invalid_type.status_code == 422
        valid_unscaled_threshold = await client.post(
            "/api/receiver/audio-dsp", json={"property": "agc_threshold", "value": 84}
        )
        assert valid_unscaled_threshold.status_code == 200
        invalid_range = await client.post(
            "/api/receiver/audio-dsp", json={"property": "noise_reduction_strength", "value": 101}
        )
        assert invalid_range.status_code == 422
        invalid_field = await client.post(
            "/api/receiver/audio-dsp", json={"property": "device_vfo_frequency", "value": 145000000}
        )
        assert invalid_field.status_code == 200
        assert invalid_field.json()["status"] == "unsupported"
        assert ("set", "device_vfo_frequency", 145000000) not in adapter.calls

        adapter.fail_props.add("squelch_enable")
        rejected = await client.post(
            "/api/receiver/audio-dsp", json={"property": "squelch_enable", "value": True}
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["confirmed_value"] is False


@pytest.mark.asyncio
async def test_native_audio_settings_patch_merges_partial_values():
    async with running_app() as (_app, client, _adapter):
        response = await client.patch("/api/settings", json={"native_audio": {"nr_enabled": True}})
        assert response.status_code == 200, response.text
        native = response.json()["native_audio"]
        assert native["nr_enabled"] is True
        assert native["agc_mode"] == "default"
        assert native["manual_gain"] == 1
        assert native["ssb_high_hz"] is None
