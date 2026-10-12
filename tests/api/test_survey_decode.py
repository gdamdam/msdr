"""Survey decode handoff (msdr/api/**, Stream B): POST /api/survey/decode|
undecode|save, the decoder_event -> Survey.note_decode_event fan-in, and the
AppService._survey_decode_start/_stop hooks Survey's own decode_start/
decode_stop kwargs will call (Stream A). Uses FakeSurvey (tests/api/test_app.py)
and FakeDecoderManager (tests/api/test_decoder.py) rather than the real
msdr.survey.Survey/msdr.decoder.DecoderManager -- both packages are owned by a
concurrent stream."""

import contextlib

import httpx
import pytest

from msdr.api.app import create_app
from msdr.api.service import SurveyError
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import FakeSurvey
from tests.api.test_decoder import FakeDecoderManager


@contextlib.asynccontextmanager
async def running_app(*, survey=None, decoder=None, adapter: FakeAdapter | None = None):
    adapter = adapter if adapter is not None else FakeAdapter()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db, survey=survey, decoder=decoder)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, adapter


# --- routes: decode/undecode/save delegate to Survey ------------------------


async def test_survey_decode_route_delegates_to_survey():
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, _adapter):
        resp = await client.post(
            "/api/survey/decode", json={"frequency_hz": 152_840_000, "decoders": ["pocsag"]}
        )
        assert resp.status_code == 200
        assert fake.decode_calls == [(152_840_000, ["pocsag"])]


async def test_survey_decode_route_defaults_decoders_to_none():
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, _adapter):
        resp = await client.post("/api/survey/decode", json={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        assert fake.decode_calls == [(152_840_000, None)]


async def test_signal_test_reads_span_without_tuning(monkeypatch):
    async def fake_test_signal(**kwargs):
        assert kwargs["requested"] == ["dmr"]
        assert kwargs["center_hz"] == 145_800_000
        return {
            "frequency_hz": kwargs["frequency_hz"],
            "outcome": "inconclusive",
            "results": [],
            "frames": 8,
        }

    monkeypatch.setattr("msdr.decoder.signal_test.test_signal", fake_test_signal)
    fake = FakeSurvey()
    fake.active = True
    async with running_app(survey=fake) as (_app, client, adapter):
        resp = await client.post(
            "/api/survey/test-signal",
            json={"frequency_hz": 145_805_000, "decoders": ["dmr"]},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "inconclusive"
        assert not any(call[0] == "set" for call in adapter.calls)


async def test_optimize_route_returns_recommendation_without_tuning(monkeypatch):
    async def fake_test_signal(**kwargs):
        assert kwargs["optimize"] is True
        return {
            "frequency_hz": kwargs["frequency_hz"],
            "outcome": "improved",
            "recommended_hz": kwargs["frequency_hz"] + 2000,
            "recommended_decoder": "dmr",
        }

    monkeypatch.setattr("msdr.decoder.signal_test.test_signal", fake_test_signal)
    fake = FakeSurvey()
    fake.active = True
    async with running_app(survey=fake) as (_app, client, adapter):
        resp = await client.post(
            "/api/survey/optimize-decode",
            json={"frequency_hz": 145_805_000, "decoders": ["dmr"]},
        )
        assert resp.status_code == 200
        assert resp.json()["recommended_hz"] == 145_807_000
        assert not any(call[0] == "set" for call in adapter.calls)


async def test_survey_undecode_route_delegates_to_survey():
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, _adapter):
        resp = await client.post("/api/survey/undecode", json={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        assert fake.undecode_calls == [152_840_000]


# --- save: reuses the P2 creation path with identity_summary() -------------


async def test_survey_save_uses_identity_summary_name_decoders_notes():
    fake = FakeSurvey()
    fake.identity_summary_result = {
        "name": "POCSAG1200 152.8400",
        "decoders": ["pocsag"],
        "notes": "confirmed: pocsag — ALPHA - Test Page",
    }
    async with running_app(survey=fake) as (app, client, _adapter):
        resp = await client.post("/api/survey/save", json={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "POCSAG1200 152.8400"
        assert body["decoders"] == ["pocsag"]
        assert body["notes"] == "confirmed: pocsag — ALPHA - Test Page"

        # Explicit `name` in the request wins over identity_summary()'s.
        signals = (await client.get("/api/signals")).json()
        assert len(signals) == 1


async def test_survey_save_is_idempotent_within_6khz():
    fake = FakeSurvey()
    fake.identity_summary_result = {"name": "Pager", "decoders": ["pocsag"], "notes": ""}
    async with running_app(survey=fake) as (app, client, _adapter):
        first = await client.post("/api/survey/save", json={"frequency_hz": 152_840_000})
        second = await client.post("/api/survey/save", json={"frequency_hz": 152_844_000})
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        signals = (await client.get("/api/signals")).json()
        assert len(signals) == 1


async def test_survey_save_explicit_name_overrides_identity_summary():
    fake = FakeSurvey()
    fake.identity_summary_result = {"name": "auto name", "decoders": [], "notes": ""}
    async with running_app(survey=fake) as (app, client, _adapter):
        resp = await client.post(
            "/api/survey/save", json={"frequency_hz": 152_840_000, "name": "My label"}
        )
        assert resp.json()["name"] == "My label"


# --- decoder_event fan-in ----------------------------------------------------


async def test_decoder_event_fan_in_reaches_survey_note_decode_event():
    fake_survey = FakeSurvey()
    async with running_app(survey=fake_survey) as (app, _client, _adapter):
        service = app.state.service
        event = {"frequency_hz": 152_840_000, "decoder": "pocsag", "text": "ALPHA - Test Page"}
        service._publish_decoder_message({"type": "decoder_event", "event": event})
        assert fake_survey.note_decode_event_calls == [event]


async def test_decoder_event_fan_in_never_raises_when_survey_note_fails():
    class RaisingSurvey(FakeSurvey):
        def note_decode_event(self, event: dict) -> None:
            raise RuntimeError("boom")

    async with running_app(survey=RaisingSurvey()) as (app, _client, _adapter):
        service = app.state.service
        # Must not raise -- publishing (broadcaster fan-out) still has to go
        # through even if the survey confirm side fails.
        service._publish_decoder_message(
            {"type": "decoder_event", "event": {"frequency_hz": 1, "decoder": "x", "text": "y"}}
        )


async def test_other_decoder_messages_do_not_reach_note_decode_event():
    fake_survey = FakeSurvey()
    async with running_app(survey=fake_survey) as (app, _client, _adapter):
        service = app.state.service
        service._publish_decoder_message({"type": "decoder", "active": True})
        assert fake_survey.note_decode_event_calls == []


@pytest.mark.parametrize("kind", ["measurement", "sync"])
async def test_measurements_and_sync_do_not_confirm_signal_identity(kind):
    from unittest.mock import Mock

    fake_survey = FakeSurvey()
    async with running_app(survey=fake_survey) as (app, _client, _adapter):
        service = app.state.service
        service.obs_recorder.on_decode_event = Mock()
        service._publish_decoder_message(
            {
                "type": "decoder_event",
                "event": {"decoder": "ils", "kind": kind, "text": "DDM 0.01"},
            }
        )
        assert fake_survey.note_decode_event_calls == []
        service.obs_recorder.on_decode_event.assert_not_called()


# --- _survey_decode_start/_stop: the hooks Survey.decode_start/decode_stop --
# --- will call once Stream A wires them into Survey.decode()/undecode() ----


async def test_survey_decode_start_calls_decoder_band_pin():
    fake_decoder = FakeDecoderManager()
    # survey=FakeSurvey(): _build_survey (real msdr.survey.Survey) doesn't
    # accept decode_start/decode_stop yet (Stream A lands them separately),
    # so every app here is built with a fake survey to avoid exercising it.
    async with running_app(survey=FakeSurvey(), decoder=fake_decoder) as (app, _client, _adapter):
        service = app.state.service
        status = await service._survey_decode_start(152_840_000, ["pocsag"])
        assert fake_decoder.band_pin_calls == [(152_840_000, ["pocsag"])]
        assert status["band"]["active"] is True


async def test_survey_raw_iq_candidates_use_original_iq_session_not_band_pin():
    decoder = FakeDecoderManager()
    async with running_app(survey=FakeSurvey(), decoder=decoder) as (app, _client, _adapter):
        status = await app.state.service._survey_decode_start(1090000000, ["adsb"])
        assert decoder.start_calls == [(["adsb"], 1090000000, "manual", None)]
        assert decoder.band_pin_calls == []
        assert status["active"]
        await app.state.service._survey_decode_stop(1090000000)
        assert decoder.stop_calls == ["survey"]
        assert decoder.band_unpin_calls == []


async def test_survey_decode_stop_calls_decoder_band_unpin():
    fake_decoder = FakeDecoderManager()
    async with running_app(survey=FakeSurvey(), decoder=fake_decoder) as (app, _client, _adapter):
        service = app.state.service
        await service._survey_decode_stop(152_840_000)
        assert fake_decoder.band_unpin_calls == [152_840_000]


async def test_survey_decode_start_raises_survey_error_without_decoder(monkeypatch):
    import msdr.api.services.decoders as decoders_module

    monkeypatch.setattr(decoders_module, "DecoderManager", None)
    async with running_app(survey=FakeSurvey()) as (app, _client, _adapter):
        service = app.state.service
        assert service.decoder is None
        try:
            await service._survey_decode_start(152_840_000, ["pocsag"])
            raise AssertionError("expected SurveyError")
        except SurveyError:
            pass
