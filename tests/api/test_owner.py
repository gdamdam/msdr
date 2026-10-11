"""Receiver owner (U1 plan, Stream A backend items 1-3): state_payload()["owner"]
computed from scanner/tracking/beacons (priority scanner > tracking > beacons >
manual, "also" for simultaneous controllers), POST /api/owner/stop dispatch,
and GET /api/owner/conflicts. Uses the real ScannerEngine/DopplerLoop (same as
tests/api/test_scan.py and tests/api/test_app.py's tracking tests) plus a
local FakeBeaconMonitor (mirrors tests/api/test_beacons.py's), since beacons is
owned by a concurrent stream."""

import contextlib
import tempfile
import time
from pathlib import Path

import httpx

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import _fast_doppler_settings, _start_tracking
from tests.api.test_recorder import FakeRecorder

_TINY = {"dwell_ms": 30, "settle_ms": 10, "hang_s": 0.05}
_RANGE_SOURCE = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}


class FakeBeaconMonitor:
    """Same surface as msdr.survey.beacons.BeaconMonitor -- see
    tests/api/test_beacons.py's own fake of the same name."""

    def __init__(self) -> None:
        self.active = False
        self.since: float | None = None
        self.stop_calls: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def start_session(self, mode: str, frequencies_hz: list[int] | None = None) -> dict:
        self.active = True
        self.since = 1_700_000_000.0
        return self.status()

    async def stop_session(self, reason: str = "user") -> dict:
        self.stop_calls.append(reason)
        self.active = False
        self.since = None
        return self.status()

    def status(self) -> dict:
        return {
            "active": self.active,
            "mode": "ncdxf" if self.active else None,
            "band_hz": None,
            "slot": None,
            "current": None,
            "since": self.since,
            "last_stop_reason": None,
            "cycle": 0,
            "summary": [],
            "misses": {},
        }


@contextlib.asynccontextmanager
async def running_app(*, beacons=None, recorder=None, settings: Settings | None = None):
    """Mirrors tests/api/test_app.py's running_app (tmp-isolated TLE/nextpass/
    location dirs so a real tracking test can't touch a developer's actual
    cache), plus the `beacons=` injection point tests/api/test_beacons.py adds."""
    adapter = FakeAdapter()
    if settings is None:
        settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    with tempfile.TemporaryDirectory(prefix="msdr-test-") as tmp:
        tmp_path = Path(tmp)
        explicitly_set = settings.model_fields_set
        overrides: dict = {}
        if "tle_cache_dir" not in explicitly_set:
            overrides["tle_cache_dir"] = str(tmp_path / "tle")
        if "nextpass_cache_dir" not in explicitly_set:
            overrides["nextpass_cache_dir"] = str(tmp_path / "nextpass_cache")
        if "location_file" not in explicitly_set:
            overrides["location_file"] = str(tmp_path / "location.json")
        if "decoder" not in explicitly_set:
            # _build_decoder mkdir's calls_dir/fax_dir on construction --
            # without this override they'd default under
            # Path(":memory:").parent (the repo's cwd) and leak real dirs.
            overrides["decoder"] = settings.decoder.model_copy(
                update={
                    "calls_dir": str(tmp_path / "decoder" / "calls"),
                    "fax_dir": str(tmp_path / "decoder" / "fax"),
                }
            )
        if "recorder_root" not in explicitly_set:
            overrides["recorder_root"] = str(tmp_path / "recorder")
        if overrides:
            settings = settings.model_copy(update=overrides)
        db = Database(":memory:")
        app = create_app(
            settings,
            adapter=adapter,
            db=db,
            beacons=beacons,
            recorder=recorder,
            settings_path=tmp_path / "settings.json",
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield app, client, adapter


# --- owner computation ----------------------------------------------------


async def test_owner_manual_when_nothing_active():
    async with running_app() as (app, client, _adapter):
        owner = (await client.get("/api/state")).json()["owner"]
        assert owner == {"kind": "manual", "label": "MANUAL", "since": None, "stop": None}


async def test_owner_scanner_active():
    async with running_app() as (app, client, _adapter):
        before = time.time()
        resp = await client.post("/api/scan/start", json={"source": _RANGE_SOURCE, **_TINY})
        assert resp.status_code == 200

        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "scanner"
        assert owner["label"] == "SCANNER"
        assert owner["stop"] == "/api/owner/stop"
        assert "also" not in owner
        assert owner["since"] is not None and owner["since"] >= before - 1

        await client.post("/api/scan/stop")
        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "manual"


async def test_owner_tracking_active():
    async with running_app(settings=_fast_doppler_settings()) as (app, client, _adapter):
        before = time.time()
        await _start_tracking(client, satellite="ISS")

        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "tracking"
        assert owner["label"] == "TRACKING ISS"
        assert owner["stop"] == "/api/owner/stop"
        assert owner["since"] is not None and owner["since"] >= before - 1

        await client.delete("/api/track")
        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "manual"


async def test_owner_beacons_active():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        resp = await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        assert resp.status_code == 200

        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "beacons"
        assert owner["label"] == "BEACONS"
        assert owner["since"] == 1_700_000_000.0
        assert owner["stop"] == "/api/owner/stop"


async def test_owner_priority_reports_first_and_lists_also():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        resp = await client.post("/api/scan/start", json={"source": _RANGE_SOURCE, **_TINY})
        assert resp.status_code == 200

        owner = (await client.get("/api/state")).json()["owner"]
        # Priority scanner > tracking > beacons -- both are active, scanner wins.
        assert owner["kind"] == "scanner"
        assert owner["also"] == ["beacons"]

        await client.post("/api/scan/stop")


# --- POST /api/owner/stop dispatch -----------------------------------------


async def test_owner_stop_dispatches_to_scanner():
    async with running_app() as (app, client, _adapter):
        await client.post("/api/scan/start", json={"source": _RANGE_SOURCE, **_TINY})
        resp = await client.post("/api/owner/stop")
        assert resp.status_code == 200
        assert resp.json()["kind"] == "manual"
        assert (await client.get("/api/scan")).json()["state"] == "idle"


async def test_owner_stop_dispatches_to_tracking():
    async with running_app(settings=_fast_doppler_settings()) as (app, client, _adapter):
        await _start_tracking(client, satellite="ISS")
        resp = await client.post("/api/owner/stop")
        assert resp.status_code == 200
        assert resp.json()["kind"] == "manual"
        assert app.state.service.doppler_loop.status is None


async def test_owner_stop_dispatches_to_beacons():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake) as (app, client, _adapter):
        await client.post("/api/beacons/start", json={"mode": "ncdxf"})
        resp = await client.post("/api/owner/stop")
        assert resp.status_code == 200
        assert resp.json()["kind"] == "manual"
        assert fake.active is False
        assert fake.stop_calls == ["owner_stop"]


async def test_owner_stop_is_noop_when_manual():
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/owner/stop")
        assert resp.status_code == 200
        assert resp.json()["kind"] == "manual"


# --- GET /api/owner/conflicts matrix ----------------------------------------


async def test_conflicts_empty_when_nothing_active():
    async with running_app() as (app, client, _adapter):
        actions = (
            "scan", "track", "beacons", "survey_span", "band_decode", "analyzer", "record",
        )
        for action in actions:
            resp = await client.get("/api/owner/conflicts", params={"action": action})
            assert resp.status_code == 200
            assert resp.json() == {"interrupts": [], "message": ""}


async def test_conflicts_scan_reports_tracking_and_beacons():
    fake = FakeBeaconMonitor()
    async with running_app(beacons=fake, settings=_fast_doppler_settings()) as (
        app,
        client,
        _adapter,
    ):
        await _start_tracking(client, satellite="ISS")
        await client.post("/api/beacons/start", json={"mode": "ncdxf"})

        resp = await client.get("/api/owner/conflicts", params={"action": "scan"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["interrupts"]) == {"tracking", "beacons"}
        assert body["message"]


async def test_conflicts_beacons_reports_recording():
    recorder = FakeRecorder()
    recorder.state = "recording"
    async with running_app(recorder=recorder) as (app, client, _adapter):
        resp = await client.get("/api/owner/conflicts", params={"action": "beacons"})
        assert resp.status_code == 200
        assert "recording" in resp.json()["interrupts"]


async def test_conflicts_band_decode_analyzer_record_never_interrupt():
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/scan/start", json={"source": _RANGE_SOURCE, **_TINY})
        assert resp.status_code == 200
        for action in ("band_decode", "analyzer", "record"):
            resp = await client.get("/api/owner/conflicts", params={"action": action})
            assert resp.json() == {"interrupts": [], "message": ""}
        await client.post("/api/scan/stop")


async def test_conflicts_unknown_action_is_422():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/owner/conflicts", params={"action": "bogus"})
        assert resp.status_code == 422
