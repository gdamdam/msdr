"""Signal decoders API (P1): GET /api/decoders (registry), GET/POST /api/decoder
(status, start/stop), GET/DELETE /api/decoder/events (history), POST
/api/decoder/presets/install, DecoderError -> 409, and the tune_signal hook
that starts/stops memory-driven decoding. Uses a fake decoder manager (msdr.decoder
isn't this task's package -- see the design doc) rather than the real
msdr.decoder.DecoderManager, mirroring tests/api/test_recorder.py's own
`running_app` helper (create_app's `decoder=` injection point)."""

import contextlib
import os
import wave

import httpx
import pytest

import msdr.api.service as service_module
import msdr.api.services.decoders as decoders_module
import msdr.api.services.survey as survey_module
from msdr.api.app import create_app
from msdr.api.service import DecoderError
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import DecoderSettings, Settings
from msdr.storage.db import Database


class FakeDecoderManager:
    """Same surface as msdr.decoder.manager.DecoderManager (see the design
    spec's manager contract): start/stop (runtime), start_decoding/stop_decoding,
    status()."""

    def __init__(self, *, fail_start: bool = False) -> None:
        self.running = False
        self.active = False
        self.source: str | None = None
        self.signal_id: str | None = None
        self.frequency_hz: int | None = None
        self.decoders: list[str] = []
        self.last_stop_reason: str | None = None
        self.start_calls: list[tuple] = []
        self.stop_calls: list[str] = []
        self._fail_start = fail_start
        # BAND DECODE (P2) surface: start_band/stop_band + status()["band"].
        self.band_active = False
        self.band_decoders: list[str] = []
        self.band_start_calls: list[tuple] = []
        self.band_stop_calls: list[str] = []
        # decode handoff (band_pin/band_unpin): a pin adds a fixed
        # channel like start_band, an unpin removes it -- called by
        # AppService._survey_decode_start/_stop, not a route directly.
        self.band_pin_calls: list[tuple[int, list[str]]] = []
        self.band_unpin_calls: list[int] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def start_decoding(
        self, decoders: list[str], frequency_hz: int, source: str, signal_id: str | None = None
    ) -> dict:
        if self._fail_start:
            raise DecoderError("no available decoders")
        self.active = True
        self.decoders = decoders
        self.frequency_hz = frequency_hz
        self.source = source
        self.signal_id = signal_id
        self.start_calls.append((decoders, frequency_hz, source, signal_id))
        return self.status()

    async def stop_decoding(self, reason: str = "user") -> dict:
        self.stop_calls.append(reason)
        self.active = False
        self.last_stop_reason = reason
        status = self.status()
        self.source = None
        self.signal_id = None
        return status

    async def start_band(
        self,
        decoders: list[str],
        *,
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        self.band_active = True
        self.band_decoders = decoders
        self.band_start_calls.append((decoders, max_channels, threshold_db))
        return self.status()

    async def stop_band(self, reason: str = "user") -> dict:
        self.band_stop_calls.append(reason)
        self.band_active = False
        return self.status()

    async def band_pin(self, frequency_hz: int, decoders: list[str]) -> dict:
        self.band_pin_calls.append((frequency_hz, decoders))
        self.band_active = True
        self.band_decoders = decoders
        return self.status()

    async def band_unpin(self, frequency_hz: int) -> dict:
        self.band_unpin_calls.append(frequency_hz)
        return self.status()

    def status(self) -> dict:
        return {
            "active": self.active,
            "source": self.source,
            "signal_id": self.signal_id,
            "frequency_hz": self.frequency_hz,
            "since": None,
            "decoders": [{"id": d, "state": "running", "error": None} for d in self.decoders],
            "warnings": [],
            "last_stop_reason": self.last_stop_reason,
            "audio": {
                "connected": True,
                "rate_configured": 48000,
                "rate_observed": None,
                "silent": False,
            },
            "available": {"pocsag": True, "flex": True},
            "binaries": {"multimon-ng": "/usr/local/bin/multimon-ng"},
            "events_total": 0,
            "band": {
                "active": self.band_active,
                "decoders": self.band_decoders,
                "center_hz": None,
                "sample_rate_hz": None,
                "spacing_hz": None,
                "max_channels": 8,
                "threshold_db": 10.0,
                "since": None,
                "last_stop_reason": None,
                "channels": [],
                "candidates": [],
                "iq": {"connected": False, "frames": 0, "dropped": 0, "cpu_load": 0.0},
            },
        }


@contextlib.asynccontextmanager
async def running_app(
    decoder=None, adapter: FakeAdapter | None = None, settings: Settings | None = None
):
    adapter = adapter if adapter is not None else FakeAdapter()
    if settings is None:
        settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db, decoder=decoder)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, adapter


async def test_decoders_registry_route_delegates_to_describe_decoders(monkeypatch):
    # AppService.list_decoders() must call describe_decoders(bin_dirs,
    # session=<manager status or None>) and return its result as-is -- the
    # registry contents themselves are msdr.decoder's own concern (Stream A),
    # not this route's. `session` feeds the per-decoder readiness.session
    # fact, so the service must pass it rather than leave it None.
    calls = []

    def fake_describe(bin_dirs, session=None):
        calls.append(bin_dirs)
        assert session is None or isinstance(session, dict)
        return [{"id": "pocsag", "available": False}]

    monkeypatch.setattr(decoders_module, "describe_decoders", fake_describe)
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/decoders")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "pocsag", "available": False}]
        assert calls == [app.state.service.settings.decoder.bin_dirs]


async def test_decoder_status_route_is_null_when_manager_unavailable(monkeypatch):
    # msdr.decoder may not be importable in some checkouts (see the module-level
    # ImportError guard in msdr/api/service.py) -- simulate that here rather
    # than relying on whether the package happens to be installed.
    monkeypatch.setattr(decoders_module, "DecoderManager", None)
    async with running_app() as (app, client, _adapter):
        assert app.state.service.decoder is None
        resp = await client.get("/api/decoder")
        assert resp.status_code == 200
        assert resp.json() is None
        state = (await client.get("/api/state")).json()
        assert state["decoder"] is None


async def test_decoder_status_route_reports_fake_status():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.get("/api/decoder")
        assert resp.status_code == 200
        assert resp.json()["active"] is False
        state = (await client.get("/api/state")).json()
        assert state["decoder"]["active"] is False


async def test_decoder_start_uses_current_confirmed_vfo():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.post("/api/decoder/start", json={"decoders": ["pocsag"]})
        assert resp.status_code == 200
        assert resp.json()["active"] is True
        assert fake.start_calls == [(["pocsag"], 145_800_000, "manual", None)]


async def test_decoder_start_explicit_frequency_waits_for_confirmed_tune(monkeypatch):
    from msdr.engine.restore import RestoreReport

    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        service = app.state.service

        async def tune_frequency(target):
            service.confirmed.props["device_vfo_frequency"] = target
            return RestoreReport("confirmed", [], 0)

        monkeypatch.setattr(service, "tune_frequency", tune_frequency)
        resp = await client.post(
            "/api/decoder/start",
            json={"decoders": ["pocsag"], "frequency_hz": 154_700_000},
        )
        assert resp.status_code == 200, resp.text
        assert fake.start_calls == [(["pocsag"], 154_700_000, "manual", None)]


@pytest.mark.parametrize("status", ["failed", "interrupted", "confirmed"])
async def test_decoder_start_refuses_unconfirmed_explicit_tune(monkeypatch, status):
    from msdr.engine.restore import RestoreReport

    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        service = app.state.service
        target = 154_700_000

        async def tune_frequency(_target):
            return RestoreReport(status, [], 0)

        monkeypatch.setattr(service, "tune_frequency", tune_frequency)
        resp = await client.post(
            "/api/decoder/start",
            json={"decoders": ["pocsag"], "frequency_hz": target},
        )
        assert resp.status_code == 409
        assert "not confirmed" in resp.json()["detail"]
        assert fake.start_calls == []


async def test_decoder_stop_delegates_with_user_reason():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        await client.post("/api/decoder/start", json={"decoders": ["pocsag"]})
        resp = await client.post("/api/decoder/stop")
        assert resp.status_code == 200
        assert fake.stop_calls == ["user"]


async def test_decoder_error_maps_to_409():
    fake = FakeDecoderManager(fail_start=True)
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.post("/api/decoder/start", json={"decoders": ["pocsag"]})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "no available decoders"


async def test_decoder_start_without_manager_is_409(monkeypatch):
    monkeypatch.setattr(decoders_module, "DecoderManager", None)
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/decoder/start", json={"decoders": ["pocsag"]})
        assert resp.status_code == 409


async def test_decoder_events_route_lists_filters_and_clears():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.decode_events.add(
            _Event(ts=1.0, decoder="pocsag", text="alpha", fields={"address": 1})
        )
        service.decode_events.add(_Event(ts=2.0, decoder="flex", text="beta"))

        resp = await client.get("/api/decoder/events")
        assert resp.status_code == 200
        events = resp.json()
        assert [e["text"] for e in events] == ["beta", "alpha"]

        resp = await client.get("/api/decoder/events", params={"decoder": "pocsag"})
        assert [e["decoder"] for e in resp.json()] == ["pocsag"]

        resp = await client.delete("/api/decoder/events")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 2}
        assert (await client.get("/api/decoder/events")).json() == []


async def test_decoder_presets_install_route_is_idempotent():
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/decoder/presets/install")
        assert resp.status_code == 200
        assert resp.json() == {"created": 12, "skipped": 0}

        resp = await client.post("/api/decoder/presets/install")
        assert resp.json() == {"created": 0, "skipped": 12}

        signals = (await client.get("/api/signals")).json()
        pager = next(s for s in signals if s["frequency_hz"] == 152_840_000)
        assert set(pager["decoders"]) == {"pocsag", "flex"}


async def test_decoder_band_start_route_delegates_with_options():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.post(
            "/api/decoder/band/start",
            json={"decoders": ["pocsag", "flex"], "max_channels": 4, "threshold_db": 12.0},
        )
        assert resp.status_code == 200
        assert resp.json()["band"]["active"] is True
        assert fake.band_start_calls == [(["pocsag", "flex"], 4, 12.0)]


async def test_decoder_band_stop_route_delegates_with_user_reason():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        await client.post("/api/decoder/band/start", json={"decoders": ["pocsag"]})
        resp = await client.post("/api/decoder/band/stop")
        assert resp.status_code == 200
        assert resp.json()["band"]["active"] is False
        assert fake.band_stop_calls == ["user"]


async def test_decoder_band_save_creates_signal_and_is_idempotent():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        await client.post("/api/decoder/band/start", json={"decoders": ["pocsag", "flex"]})

        resp = await client.post("/api/decoder/band/save", json={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["demodulator"] == "NFM"
        assert body["filter_bandwidth_hz"] == 12_500
        assert set(body["decoders"]) == {"pocsag", "flex"}
        assert body["name"] == "152.8400 band decode"

        # A save within +-6 kHz of an already-saved channel returns the same
        # memory rather than creating a near-duplicate.
        resp2 = await client.post(
            "/api/decoder/band/save", json={"frequency_hz": 152_844_000, "name": "ignored"}
        )
        assert resp2.status_code == 200
        assert resp2.json()["id"] == body["id"]

        signals = (await client.get("/api/signals")).json()
        assert len([s for s in signals if 152_838_000 <= s["frequency_hz"] <= 152_842_000]) == 1


async def test_decoder_signals_in_span_filters_by_distance_and_decoders():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        await client.post(
            "/api/signals",
            json={"name": "In span", "frequency_hz": 152_840_000, "decoders": ["pocsag"]},
        )
        await client.post("/api/signals", json={"name": "No decoders", "frequency_hz": 152_850_000})
        await client.post(
            "/api/signals",
            json={"name": "Out of span", "frequency_hz": 200_000_000, "decoders": ["flex"]},
        )

        result = service._decoder_signals_in_span(152_800_000, 8_000_000)
        assert [(freq, decoders) for freq, _id, decoders in result] == [(152_840_000, ["pocsag"])]


async def test_tune_signal_starts_and_stops_memory_decoding():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        with_decoders = await client.post(
            "/api/signals",
            json={"name": "Pager", "frequency_hz": 152_840_000, "decoders": ["pocsag", "flex"]},
        )
        signal_id = with_decoders.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        assert fake.start_calls == [(["pocsag", "flex"], 152_840_000, "memory", signal_id)]

        # Tuning a plain memory (no decoders) while a memory-driven decode is
        # active stops it with reason "tuned elsewhere".
        plain = await client.post(
            "/api/signals", json={"name": "Plain", "frequency_hz": 146_520_000}
        )
        plain_id = plain.json()["id"]
        resp = await client.post(f"/api/tune/signal/{plain_id}")
        assert resp.status_code == 200
        assert fake.stop_calls == ["tuned elsewhere"]


async def test_meshcore_us_signal_recalls_phy_profile_after_global_settings_change():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        created = await client.post(
            "/api/signals",
            json={"name": "MeshCore", "frequency_hz": 910_525_000,
                  "decoders": ["lora"], "decoder_preset": "meshcore_us"},
        )
        assert created.status_code == 200, created.text
        signal_id = created.json()["id"]
        svc = app.state.service
        assert svc.signals.get(signal_id).decoder_preset == "meshcore_us"
        svc.settings.decoder = svc.settings.decoder.model_copy(update={
            "lora_spreading_factor": 11,
            "lora_bandwidth_hz": 250_000,
            "lora_coding_rate": 3,
            "lora_protocol": "lora",
        })

        tuned = await client.post(f"/api/tune/signal/{signal_id}")
        assert tuned.status_code == 200, tuned.text
        assert fake.start_calls == [(["lora"], 910_525_000, "memory", signal_id)]
        assert svc.settings.decoder.lora_spreading_factor == 7
        assert svc.settings.decoder.lora_bandwidth_hz == 62_500
        assert svc.settings.decoder.lora_coding_rate == 1
        assert svc.settings.decoder.lora_protocol == "meshcore"


async def test_tune_signal_decoder_failure_is_noted_not_fatal():
    fake = FakeDecoderManager(fail_start=True)
    async with running_app(fake) as (app, client, _adapter):
        created = await client.post(
            "/api/signals",
            json={"name": "Pager", "frequency_hz": 152_840_000, "decoders": ["pocsag"]},
        )
        signal_id = created.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        report = resp.json()
        assert report["status"] == "confirmed"  # the tune itself still succeeded
        assert "decoder" in report["detail"]


class _Event:
    """Duck-types msdr.decoder.backends.DecodeEvent for DecodeEventRepo.add()."""

    def __init__(self, *, ts, decoder, text, kind="text", fields=None, frequency_hz=None):
        self.ts = ts
        self.decoder = decoder
        self.text = text
        self.kind = kind
        self.fields = fields or {}
        self.frequency_hz = frequency_hz
        self.signal_id = None


def _write_wav(path, num_frames: int = 8) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(b"\x00\x00" * num_frames)


# --- DecoderSettings.calls_dir/calls_keep, calls WAV route, retention --


def test_decoder_settings_calls_defaults_and_back_compat():
    settings = DecoderSettings()
    assert settings.calls_dir is None
    assert settings.calls_keep == 500

    # A settings.json saved before has neither key -- it must still load.
    loaded = DecoderSettings.model_validate({})
    assert loaded.calls_dir is None
    assert loaded.calls_keep == 500


async def test_decoder_call_wav_route_streams_file(tmp_path, monkeypatch):
    # Survey (msdr.survey, a separate concurrent stream) isn't this test's
    # concern -- keep AppService._build_survey a no-op so it can't fail these
    # calls-route/retention tests for reasons unrelated to Stream B.
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    _write_wav(tmp_path / "call1.wav")
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(calls_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        stored = service.decode_events.add(
            _Event(
                ts=1.0,
                decoder="dmr",
                text="DMR TG 1 from 2",
                kind="call",
                fields={"wav": "call1.wav"},
            )
        )
        resp = await client.get(f"/api/decoder/calls/{stored['id']}.wav")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert len(resp.content) > 0


async def test_decoder_call_wav_route_404_for_unknown_event(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(calls_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        resp = await client.get("/api/decoder/calls/nope.wav")
        assert resp.status_code == 404


async def test_decoder_call_wav_route_refuses_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    outside = tmp_path.parent / "outside.wav"
    _write_wav(outside)
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(calls_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        stored = service.decode_events.add(
            _Event(
                ts=1.0,
                decoder="dmr",
                text="DMR TG 1 from 2",
                kind="call",
                fields={"wav": "../outside.wav"},
            )
        )
        resp = await client.get(f"/api/decoder/calls/{stored['id']}.wav")
        assert resp.status_code == 404


async def test_decoder_call_retention_trims_oldest_wav_files(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    paths = [tmp_path / f"call{i}.wav" for i in range(5)]
    for i, path in enumerate(paths):
        _write_wav(path)
        # Force distinct, increasing mtimes so trim order is deterministic.
        mtime = 1_000_000 + i
        os.utime(path, (mtime, mtime))

    settings = Settings(
        dwell_seconds=0.05,
        db_path=":memory:",
        decoder=DecoderSettings(calls_dir=str(tmp_path), calls_keep=2),
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        service._publish_decoder_message(
            {"type": "decoder_event", "event": {"kind": "call", "fields": {"wav": "call4.wav"}}}
        )
        remaining = {p.name for p in tmp_path.glob("*.wav")}
        assert remaining == {"call3.wav", "call4.wav"}


# --- DecoderSettings.fax_dir/fax_keep, images route, retention --


def _write_png(path) -> None:
    # Route/retention behaviour doesn't parse image bytes, so any content is
    # fine -- mirrors _write_wav above for the calls route.
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def test_decoder_settings_fax_defaults_and_back_compat():
    settings = DecoderSettings()
    assert settings.fax_dir is None
    assert settings.fax_keep == 200

    # A settings.json saved before this feature has neither key -- it must
    # still load.
    loaded = DecoderSettings.model_validate({})
    assert loaded.fax_dir is None
    assert loaded.fax_keep == 200


async def test_decoder_image_route_streams_file(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    _write_png(tmp_path / "fax1.png")
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(fax_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        stored = service.decode_events.add(
            _Event(ts=1.0, decoder="wefax", text="fax started", kind="image")
        )
        # image_path is a top-level column (not a fields entry) -- set it
        # directly, since DecodeEventRepo.add() only reads it off the event
        # object at insert time.
        service.db.conn.execute(
            "UPDATE decode_events SET image_path = ? WHERE id = ?", ("fax1.png", stored["id"])
        )
        service.db.conn.commit()
        resp = await client.get(f"/api/decoder/images/{stored['id']}.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert len(resp.content) > 0


async def test_decoder_image_route_404_for_unknown_event(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(fax_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        resp = await client.get("/api/decoder/images/nope.png")
        assert resp.status_code == 404


async def test_decoder_image_route_refuses_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    outside = tmp_path.parent / "outside.png"
    _write_png(outside)
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", decoder=DecoderSettings(fax_dir=str(tmp_path))
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        stored = service.decode_events.add(
            _Event(ts=1.0, decoder="wefax", text="fax started", kind="image")
        )
        service.db.conn.execute(
            "UPDATE decode_events SET image_path = ? WHERE id = ?",
            ("../outside.png", stored["id"]),
        )
        service.db.conn.commit()
        resp = await client.get(f"/api/decoder/images/{stored['id']}.png")
        assert resp.status_code == 404


async def test_decoder_fax_retention_trims_oldest_png_files(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    paths = [tmp_path / f"fax{i}.png" for i in range(5)]
    for i, path in enumerate(paths):
        _write_png(path)
        mtime = 1_000_000 + i
        os.utime(path, (mtime, mtime))

    settings = Settings(
        dwell_seconds=0.05,
        db_path=":memory:",
        decoder=DecoderSettings(fax_dir=str(tmp_path), fax_keep=2),
    )
    async with running_app(FakeDecoderManager(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        service._publish_decoder_message(
            {"type": "decoder_event", "event": {"kind": "image", "image_path": "fax4.png"}}
        )
        remaining = {p.name for p in tmp_path.glob("*.png")}
        assert remaining == {"fax3.png", "fax4.png"}


async def test_prepare_preview_apply_restore_and_stale_preview(monkeypatch):
    from msdr.engine.restore import RestoreReport

    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, adapter):
        svc = app.state.service
        calls = []

        async def apply(desired):
            calls.append(desired)
            svc.confirmed.props.update(desired)
            return RestoreReport("confirmed", [], 0)

        monkeypatch.setattr(svc, "_apply_decoder_receiver_props", apply)
        body = {"decoder": "adsb"}
        r = await client.post("/api/decoder/prepare", json=body)
        assert r.status_code == 200, r.text
        p = r.json()
        assert not calls
        assert p["desired"]["device_vfo_frequency"] == 1090000000
        fake.active = True
        r = await client.post("/api/decoder/prepare", json={**body, "token": p["token"]})
        assert r.status_code == 400
        assert not calls
        fake.active = False
        svc.confirmed.props["device_vfo_frequency"] += 1000
        r = await client.post("/api/decoder/prepare", json={**body, "token": p["token"]})
        assert r.status_code == 400
        p = (await client.post("/api/decoder/prepare", json=body)).json()
        r = await client.post("/api/decoder/prepare", json={**body, "token": p["token"]})
        assert r.status_code == 200, r.text
        assert r.json()["report"]["status"] == "confirmed"
        r = await client.post("/api/decoder/restore", json={"token": p["token"]})
        assert r.status_code == 200, r.text
        assert calls[-1] == p["before"]
        r = await client.post("/api/decoder/restore", json={"token": p["token"]})
        assert r.status_code == 400


async def test_prepare_runs_confirmed_restore_and_can_undo():
    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, adapter):
        p = (await client.post("/api/decoder/prepare", json={"decoder": "adsb"})).json()
        r = await client.post("/api/decoder/prepare", json={"decoder": "adsb", "token": p["token"]})
        assert r.status_code == 200, r.text
        assert r.json()["report"]["status"] == "confirmed", r.text
        assert app.state.service.confirmed.props["device_vfo_frequency"] == 1090000000
        r = await client.post("/api/decoder/restore", json={"token": p["token"]})
        assert r.json()["status"] == "confirmed", r.text
        restored = app.state.service.confirmed.props["device_vfo_frequency"]
        assert restored == p["before"]["device_vfo_frequency"]
        r = await client.post("/api/decoder/reapply", json={"token": p["token"]})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "confirmed"
        assert app.state.service.confirmed.props["device_vfo_frequency"] == 1090000000
        r = await client.post("/api/decoder/restore", json={"token": p["token"]})
        assert r.json()["status"] == "confirmed"


async def test_meshcore_us_prepare_applies_and_restores_receiver_and_phy_settings(monkeypatch):
    from msdr.engine.restore import RestoreReport

    fake = FakeDecoderManager()
    async with running_app(fake) as (app, client, _adapter):
        svc = app.state.service

        async def apply(desired):
            svc.confirmed.props.update(desired)
            return RestoreReport("confirmed", [], 0)

        monkeypatch.setattr(svc, "_apply_decoder_receiver_props", apply)
        original_settings = svc.settings.decoder.model_dump()
        body = {"decoder": "lora", "profile": "meshcore_us"}
        preview = (await client.post("/api/decoder/prepare", json=body)).json()
        assert preview["desired"]["device_vfo_frequency"] == 910_525_000
        assert preview["decoder_settings_desired"] == {
            "lora_spreading_factor": 7,
            "lora_bandwidth_hz": 62_500,
            "lora_coding_rate": 1,
            "lora_protocol": "meshcore",
        }
        applied = await client.post(
            "/api/decoder/prepare", json={**body, "token": preview["token"]}
        )
        assert applied.status_code == 200, applied.text
        assert svc.settings.decoder.lora_protocol == "meshcore"
        assert svc.settings.decoder.lora_spreading_factor == 7
        assert svc.settings.decoder.lora_bandwidth_hz == 62_500
        assert svc.settings.decoder.lora_coding_rate == 1
        restored = await client.post("/api/decoder/restore", json={"token": preview["token"]})
        assert restored.status_code == 200, restored.text
        assert svc.settings.decoder.model_dump() == original_settings
        assert (
            svc.confirmed.props["device_vfo_frequency"]
            == preview["before"]["device_vfo_frequency"]
        )


async def test_meshcore_preview_expires_when_lora_settings_change():
    async with running_app(FakeDecoderManager()) as (app, client, _adapter):
        svc = app.state.service
        body = {"decoder": "lora", "profile": "meshcore_us"}
        preview = (await client.post("/api/decoder/prepare", json=body)).json()
        svc.settings.decoder = svc.settings.decoder.model_copy(
            update={"lora_protocol": "meshtastic"}
        )
        applied = await client.post(
            "/api/decoder/prepare", json={**body, "token": preview["token"]}
        )
        assert applied.status_code == 400
        assert "LoRa decoder settings changed" in applied.text


async def test_navaid_ident_is_cross_checked_against_faa_catalog():
    from msdr.catalog.models import CatalogEntry

    async with running_app(FakeDecoderManager(), settings=Settings(db_path=":memory:")) as (
        app,
        _client,
        _adapter,
    ):
        service = app.state.service
        service.catalog.replace_source(
            "faa_nasr",
            [
                CatalogEntry(
                    source="faa_nasr",
                    frequency_hz=115_800_000,
                    station="SFO VOR/DME",
                    ref_type="navaid",
                    station_id="SFO",
                ),
                CatalogEntry(
                    source="faa_nasr",
                    frequency_hz=109_550_000,
                    station="I-SFO SFO RWY 28L",
                    ref_type="navaid",
                    station_id="I-SFO",
                ),
            ],
        )

        def check(decoder, freq, ident):
            msg = {
                "type": "decoder_event",
                "event": {
                    "decoder": decoder,
                    "kind": "measurement",
                    "frequency_hz": freq,
                    "fields": {"ident": ident},
                },
            }
            service._publish_decoder_message(msg)
            return msg["event"]["fields"]

        vor = check("vor", 115_800_000, "SFO")
        assert vor["ident_catalog_match"] is True and vor["ident_catalog_ids"] == ["SFO"]
        assert check("vor", 115_800_000, "OAK")["ident_catalog_match"] is False
        # NASR writes localizer idents as "I-SFO"; Morse sends "ISFO".
        assert check("ils", 109_550_000, "ISFO")["ident_catalog_match"] is True
        assert check("vor", 113_000_000, "SFO")["ident_catalog_match"] is None
        # Events without an ident, or from other decoders, are untouched.
        assert "ident_catalog_match" not in check("vor", 115_800_000, None)
        assert "ident_catalog_match" not in check("ais", 115_800_000, "SFO")
