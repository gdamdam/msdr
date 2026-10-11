"""Inspection routes over an actual replay and selected-channel receiver."""

import json
import time
import warnings
from pathlib import Path

import numpy as np

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import DecoderSettings, Features, Settings
from msdr.storage.db import Database

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

_FS = 240_000
_CENTER = 100_000_000


def _recording(root: Path) -> Path:
    directory = root / "scope-replay"
    directory.mkdir()
    seconds = 1.5
    t = np.arange(int(_FS * seconds), dtype=np.float64) / _FS
    # A 1 kHz NFM tone on a channel 45 kHz from centre.
    phase = 2 * np.pi * 45_000 * t + 2.5 * np.sin(2 * np.pi * 1_000 * t)
    z = 0.3 * np.exp(1j * phase)
    iq = (np.column_stack((z.real, z.imag)) * 32767).astype("<i2")
    stem = f"run-01_{_FS}SPS_{_CENTER}Hz"
    (directory / f"{stem}.sigmf-data").write_bytes(iq.reshape(-1).tobytes())
    (directory / f"{stem}.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "ci16_le", "core:sample_rate": _FS, "core:version": "1.0.0"},
        "captures": [{"core:sample_start": 0, "core:frequency": _CENTER}],
        "annotations": [],
    }))
    (directory / "recording.json").write_text(json.dumps({
        "start_wall": 1_700_000_000,
        "end_wall": 1_700_000_000 + seconds,
        "preroll_s": 0,
        "runs": [{
            "data": f"{stem}.sigmf-data", "meta": f"{stem}.sigmf-meta",
            "sample_rate": _FS, "center_hz": _CENTER, "samples": len(t),
            "start_wall": 1_700_000_000, "settings": {},
        }],
    }))
    return directory


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=":memory:",
        recorder_root=str(tmp_path / "recordings"),
        tle_cache_dir=str(tmp_path / "tle"),
        nextpass_cache_dir=str(tmp_path / "nextpass"),
        location_file=str(tmp_path / "location.json"),
        decoder=DecoderSettings(
            calls_dir=str(tmp_path / "decoder" / "calls"),
            fax_dir=str(tmp_path / "decoder" / "fax"),
        ),
        features=Features(
            detect=False, ident=False, nearby=False, journal_passive=False,
            passes=False, scans=False, sky=False, tracking=False, catalog=False,
            telemetry=False, scanner=False, recorder=False, decoder=False,
            survey=True, beacons=False, finds=False, ml_controls=False,
        ),
    )


def test_inspection_api_tracks_replay_clock_trigger_and_retune(tmp_path):
    recording = _recording(tmp_path)
    app = create_app(
        _settings(tmp_path), adapter=FakeAdapter(), db=Database(":memory:"),
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app) as client:
        assert client.get("/api/inspection/review").status_code == 409
        started = client.post("/api/survey/replay", json={"path": str(recording), "loop": True})
        assert started.status_code == 200
        assert client.post("/api/review/tune", json={
            "frequency_hz": _CENTER + 45_000, "demodulator": "NFM", "bandwidth_hz": 12_500,
        }).status_code == 200

        configured = client.post("/api/inspection/review/configure", json={
            "stream": "audio", "trigger_mode": "rising", "trigger_level": 0.0,
        })
        assert configured.status_code == 200
        assert client.post("/api/inspection/review/configure", json={
            "stream": "audio", "trigger_mode": "pulse", "trigger_level": 0.0,
        }).status_code == 422

        payload = None
        for _ in range(40):
            response = client.get("/api/inspection/review?window_s=0.1&points=256")
            assert response.status_code == 200
            payload = response.json()
            if payload["streams"]["audio"]["values"]:
                break
            time.sleep(0.05)
        assert payload is not None
        assert set(payload["streams"]) == {"audio", "iq_envelope", "iq_i", "iq_q"}
        audio = payload["streams"]["audio"]
        envelope = payload["streams"]["iq_envelope"]
        assert audio["sample_rate_hz"] == 48_000
        assert envelope["sample_rate_hz"] == 50_000
        assert envelope["session_id"]
        assert len(audio["time_s"]) == len(audio["min_values"]) == len(audio["max_values"])
        assert audio["trigger_status"] in {
            "triggered", "waiting_for_trigger", "waiting_for_post_trigger",
        }
        assert payload["audio_spectrum"]["frequency_hz"]
        assert payload["audio_spectrum"]["power_dbfs"]

        drift = client.post("/api/inspection/review/drift", json={
            "enabled": False, "max_offset_hz": 500, "alpha": 0.2,
            "min_level_db": -60, "kind": "carrier",
        })
        assert drift.status_code == 200
        assert drift.json()["tracking"] is True
        assert drift.json()["correction_enabled"] is False
        assert client.post("/api/inspection/review/drift", json={
            "enabled": True, "max_offset_hz": 500, "kind": "meshtastic",
        }).status_code == 422

        old_generation = audio["generation"]
        assert client.post("/api/review/seek", json={"position_s": 0.5}).status_code == 200
        updated = None
        for _ in range(40):
            updated = client.get("/api/inspection/review?window_s=0.1&points=256").json()
            if updated["streams"]["audio"]["generation"] > old_generation:
                break
            time.sleep(0.025)
        assert updated["streams"]["audio"]["generation"] > old_generation

        old_session = updated["streams"]["iq_envelope"]["session_id"]
        assert client.post("/api/review/tune", json={
            "frequency_hz": _CENTER + 46_000, "demodulator": "NFM", "bandwidth_hz": 12_500,
        }).status_code == 200
        retuned = None
        for _ in range(40):
            retuned = client.get("/api/inspection/review?window_s=0.1&points=256").json()
            if retuned["streams"]["iq_envelope"]["session_id"] != old_session:
                break
            time.sleep(0.025)
        assert retuned["streams"]["iq_envelope"]["session_id"] != old_session
        assert client.post("/api/survey/replay/stop").status_code == 200
