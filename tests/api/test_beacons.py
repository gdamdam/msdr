"""Beacon monitor API (S10, msdr/api/**, Stream B): GET /api/beacons, POST
/api/beacons/start|stop, GET/DELETE /api/beacons/hits, BeaconError -> 409, the
refusal rule (survey analyzer / band decode / recorder recording), the
decoder_event -> BeaconMonitor.note_decode_event fan-in, and
state_payload()["beacons"]. Uses FakeBeaconMonitor (this module) plus
FakeSurvey (tests/api/test_app.py), FakeDecoderManager (tests/api/test_decoder.py)
and FakeRecorder (tests/api/test_recorder.py) rather than the real
msdr.survey.beacons.BeaconMonitor/msdr.survey.Survey/msdr.decoder.DecoderManager/
msdr.recorder.Recorder -- all owned by a concurrent stream or out of this
task's scope."""

import contextlib

import httpx

import msdr.api.services.beacons as beacons_module
from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import FakeSurvey
from tests.api.test_decoder import FakeDecoderManager
from tests.api.test_recorder import FakeRecorder


class FakeBeaconMonitor:
    """Same surface as msdr.survey.beacons.BeaconMonitor (see the plan's
    engine contract): runtime start/stop, start_session/stop_session,
    note_decode_event, status()."""

    def __init__(self) -> None:
        self.running = False
        self.active = False
        self.mode: str | None = None
        self.start_calls: list[tuple[str, list[int] | None]] = []
        self.stop_calls: list[str] = []
        self.note_decode_event_calls: list[dict] = []

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def start_session(self, mode: str, frequencies_hz: list[int] | None = None) -> dict:
        self.active = True
        self.mode = mode
        self.start_calls.append((mode, frequencies_hz))
        return self.status()

    async def stop_session(self, reason: str = "user") -> dict:
        self.stop_calls.append(reason)
        self.active = False
        return self.status()

    def note_decode_event(self, event: dict) -> None:
        self.note_decode_event_calls.append(event)

    def status(self) -> dict:
        return {
            "active": self.active,
            "mode": self.mode,
            "band_hz": None,
            "slot": None,
            "current": None,
            "since": None,
            "last_stop_reason": None,
            "cycle": 0,
            "summary": [],
            "misses": {},
        }


@contextlib.asynccontextmanager
async def running_app(*, beacons=None, survey=None, decoder=None, recorder=None):
    adapter = FakeAdapter()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(
        settings,
        adapter=adapter,
        db=db,
        beacons=beacons,
        survey=survey,
        decoder=decoder,
        recorder=recorder,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, adapter


# --- routes: status/start/stop delegate to the monitor -----------------------


async def test_get_beacons_returns_status():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        resp = await client.get("/api/beacons")
        assert resp.status_code == 200
        assert resp.json()["active"] is False


async def test_get_beacons_returns_none_when_unavailable(monkeypatch):
    # msdr.survey.beacons may not be importable in some checkouts (see the
    # module-level ImportError guard in msdr/api/service.py) -- simulate that
    # here rather than relying on whether the package happens to be built yet.
    monkeypatch.setattr(beacons_module, "BeaconMonitor", None)
    async with running_app() as (app, client, _adapter):
        assert app.state.service.beacons is None
        resp = await client.get("/api/beacons")
        assert resp.status_code == 200
        assert resp.json() is None


async def test_beacons_start_route_delegates_ncdxf():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 200
        assert fake.start_calls == [("ncdxf", None)]
        assert resp.json()["active"] is True


async def test_beacons_start_route_passes_frequencies_for_ndb():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        resp = await client.post(
            "/api/beacons/start", json={"mode": "ndb", "frequencies_hz": [340_000, 350_000]}
        )
        assert resp.status_code == 200
        assert fake.start_calls == [("ndb", [340_000, 350_000])]


async def test_beacons_stop_route_delegates():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        resp = await client.post("/api/beacons/stop")
        assert resp.status_code == 200
        assert fake.stop_calls == ["user"]
        assert resp.json()["active"] is False


async def test_beacons_start_returns_409_when_unavailable(monkeypatch):
    monkeypatch.setattr(beacons_module, "BeaconMonitor", None)
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 409


async def test_beacons_stop_returns_409_when_unavailable(monkeypatch):
    monkeypatch.setattr(beacons_module, "BeaconMonitor", None)
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/beacons/stop")
        assert resp.status_code == 409


# --- refusal rule: retunes every 3 min, refused while the VFO is spoken for --


async def test_beacons_start_refused_when_survey_analyzer_active():
    fake_survey = FakeSurvey()
    fake_survey.analyzer_active = True
    async with running_app(beacons=FakeBeaconMonitor(), survey=fake_survey) as (
        app,
        client,
        _adapter,
    ):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 409


async def test_beacons_start_refused_when_band_decode_active():
    fake_decoder = FakeDecoderManager()
    fake_decoder.band_active = True
    async with running_app(beacons=FakeBeaconMonitor(), decoder=fake_decoder) as (
        app,
        client,
        _adapter,
    ):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 409


async def test_beacons_start_refused_when_recorder_recording():
    fake_recorder = FakeRecorder()
    fake_recorder.state = "recording"
    async with running_app(beacons=FakeBeaconMonitor(), recorder=fake_recorder) as (
        app,
        client,
        _adapter,
    ):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 409


async def test_beacons_start_allowed_when_nothing_else_active():
    fake_survey = FakeSurvey()
    fake_decoder = FakeDecoderManager()
    fake_recorder = FakeRecorder()
    async with running_app(
        beacons=FakeBeaconMonitor(),
        survey=fake_survey,
        decoder=fake_decoder,
        recorder=fake_recorder,
    ) as (app, client, _adapter):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 200


# --- hits: list/clear go through BeaconHitRepo -------------------------------


async def test_get_beacon_hits_route_lists_seeded_hits():
    async with running_app(beacons=FakeBeaconMonitor()) as (app, client, _adapter):
        service = app.state.service
        service.beacon_hits.add(
            {"ts": 1.0, "beacon": "4U1UN", "frequency_hz": 14_100_000, "mode": "ncdxf"}
        )
        service.beacon_hits.add(
            {"ts": 2.0, "beacon": "VE8AT", "frequency_hz": 14_100_000, "mode": "ncdxf"}
        )
        resp = await client.get("/api/beacons/hits")
        assert resp.status_code == 200
        assert [h["beacon"] for h in resp.json()] == ["VE8AT", "4U1UN"]

        filtered = await client.get("/api/beacons/hits", params={"beacon": "4U1UN"})
        assert [h["beacon"] for h in filtered.json()] == ["4U1UN"]


async def test_delete_beacon_hits_route_clears():
    async with running_app(beacons=FakeBeaconMonitor()) as (app, client, _adapter):
        service = app.state.service
        service.beacon_hits.add(
            {"ts": 1.0, "beacon": "4U1UN", "frequency_hz": 14_100_000, "mode": "ncdxf"}
        )
        resp = await client.delete("/api/beacons/hits")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 1}
        assert (await client.get("/api/beacons/hits")).json() == []


# --- state_payload()["beacons"] ----------------------------------------------


async def test_state_includes_beacons_status():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        assert resp.json()["beacons"]["active"] is False


async def test_state_beacons_is_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(beacons_module, "BeaconMonitor", None)
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/state")
        assert resp.json()["beacons"] is None


# --- decoder_event fan-in -----------------------------------------------------


async def test_decoder_event_fan_in_reaches_beacons_note_decode_event():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, _client, _adapter):
        service = app.state.service
        event = {"frequency_hz": 14_100_000, "decoder": "cw", "text": "4U1UN 4U1UN"}
        service._publish_decoder_message({"type": "decoder_event", "event": event})
        assert fake.note_decode_event_calls == [event]


async def test_decoder_event_fan_in_never_raises_when_beacons_note_fails():
    class RaisingBeacons(FakeBeaconMonitor):
        def note_decode_event(self, event: dict) -> None:
            raise RuntimeError("boom")

    async with running_app(beacons=RaisingBeacons()) as (app, _client, _adapter):
        service = app.state.service
        service._publish_decoder_message(
            {"type": "decoder_event", "event": {"frequency_hz": 1, "decoder": "cw", "text": "y"}}
        )


async def test_replay_decode_does_not_confirm_a_live_beacon():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, _client, _adapter):
        for provenance in ({"analysis_run_id": "analysis-1"}, {"source_id": "replay:recording"}):
            app.state.service._publish_decoder_message(
                {
                    "type": "decoder_event",
                    "event": {
                        "frequency_hz": 14_100_000,
                        "decoder": "cw",
                        "text": "4U1UN",
                        **provenance,
                    },
                }
            )
        assert fake.note_decode_event_calls == []


async def test_other_decoder_messages_do_not_reach_beacons_note_decode_event():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, _client, _adapter):
        service = app.state.service
        service._publish_decoder_message({"type": "decoder", "active": True})
        assert fake.note_decode_event_calls == []
