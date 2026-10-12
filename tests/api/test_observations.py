"""GET/PATCH /api/observations, POST .../claims (msdr/api/**).
Routes are thin wrappers over msdr.observations; these
tests exercise the wiring: 404s, the PATCH link/unlink note, an operator
claim's state, restart persistence, and the legacy-import-once guard.
"""

import contextlib
import tempfile
from pathlib import Path

import httpx

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.library.models import Signal
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import running_app

_HIT = {
    "ts": 100.0,
    "duration_s": 1.0,
    "frequency_hz": 152_840_000,
    "peak_snr_db": 10.0,
}


@contextlib.asynccontextmanager
async def _running_app_with_db_path(db_path: Path, *, adapter=None):
    """Like tests/api/test_app.py's running_app(), but against a real
    on-disk db file -- running_app always uses ":memory:", which can't
    survive a close+reopen. Needed here for restart persistence and the
    legacy-import-once guard across two separate AppService lifespans on the
    same file."""
    adapter = adapter if adapter is not None else FakeAdapter()
    with tempfile.TemporaryDirectory(prefix="msdr-obs-test-") as tmp:
        tmp_path = Path(tmp)
        settings = Settings(
            dwell_seconds=0.05,
            db_path=str(db_path),
            tle_cache_dir=str(tmp_path / "tle"),
            nextpass_cache_dir=str(tmp_path / "nextpass_cache"),
            location_file=str(tmp_path / "location.json"),
            recorder_root=str(tmp_path / "recorder"),
        )
        db = Database(str(db_path))
        app = create_app(
            settings, adapter=adapter, db=db, settings_path=tmp_path / "settings.json"
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield app, client, adapter


# --- 404s --------------------------------------------------------------------


async def test_get_observation_404_for_unknown_id():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/observations/missing")
        assert resp.status_code == 404


async def test_patch_observation_404_for_unknown_id():
    async with running_app() as (app, client, _adapter):
        resp = await client.patch("/api/observations/missing", json={"signal_id": None})
        assert resp.status_code == 404


async def test_post_claim_404_for_unknown_observation():
    async with running_app() as (app, client, _adapter):
        resp = await client.post(
            "/api/observations/missing/claims",
            json={"subject": "station", "value": "KROQ", "rationale": "heard it"},
        )
        assert resp.status_code == 404


# --- schema validation ---------------------------------------------------


async def test_post_claim_422_for_bad_subject():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")
        resp = await client.post(
            f"/api/observations/{obs.id}/claims",
            json={"subject": "not-a-real-subject", "value": "x", "rationale": "y"},
        )
        assert resp.status_code == 422


# --- routes ----------------------------------------------------------------


async def test_get_list_and_get_by_id_round_trip():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")

        resp = await client.get("/api/observations", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == obs.id
        assert body[0]["evidence_state"] == "none"

        resp = await client.get(f"/api/observations/{obs.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == obs.id
        assert resp.json()["reported_hz"] == 152_840_000


async def test_patch_links_and_unlinks_memory():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(name="Test", frequency_hz=152_840_000)
        service.signals.add(signal)
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")

        resp = await client.patch(f"/api/observations/{obs.id}", json={"signal_id": signal.id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["signal_id"] == signal.id
        assert body["note"] == "link suggests, does not prove identity"

        resp = await client.patch(f"/api/observations/{obs.id}", json={"signal_id": None})
        assert resp.status_code == 200
        assert resp.json()["signal_id"] is None
        assert resp.json()["note"] == "link suggests, does not prove identity"


async def test_operator_claim_appears_with_state_operator():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")

        resp = await client.post(
            f"/api/observations/{obs.id}/claims",
            json={"subject": "station", "value": "KROQ", "rationale": "heard the callsign"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["evidence_state"] == "operator"
        assert body["identity_established"] is True
        claim = next(c for c in body["claims"] if c["producer"] == "operator")
        assert claim["subject"] == "station"
        assert claim["value"] == "KROQ"
        assert claim["evidence_type"] == "operator"
        assert claim["integrity"] == "not_applicable"
        assert claim["support"] == ["heard the callsign"]
        assert claim["historical"] is False


async def test_operator_claim_supersedes_previous_claim():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")

        first = await client.post(
            f"/api/observations/{obs.id}/claims",
            json={"subject": "station", "value": "KROQ", "rationale": "initial guess"},
        )
        first_claim_id = next(
            c["claim_id"] for c in first.json()["claims"] if c["producer"] == "operator"
        )

        resp = await client.post(
            f"/api/observations/{obs.id}/claims",
            json={
                "subject": "station",
                "value": "KOIT",
                "rationale": "corrected callsign",
                "supersedes": first_claim_id,
            },
        )
        assert resp.status_code == 200
        by_id = {c["claim_id"]: c for c in resp.json()["claims"]}
        assert by_id[first_claim_id]["historical"] is True
        assert by_id[first_claim_id]["superseded_by"] is not None
        newest = by_id[by_id[first_claim_id]["superseded_by"]]
        assert newest["value"] == "KOIT"
        assert newest["historical"] is False


# --- restart persistence + legacy import (real on-disk db) ------------------


async def test_restart_persistence_observation_survives_app_restart(tmp_path):
    db_path = tmp_path / "msdr.db"
    async with _running_app_with_db_path(db_path) as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        obs = service.obs_recorder.on_scanner_hit(_HIT, "find-1")
        obs_id = obs.id

    # First app closed (lifespan exit -> service.stop() -> db.close()).
    async with _running_app_with_db_path(db_path) as (app2, client2, _adapter2):
        resp = await client2.get("/api/observations", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        assert obs_id in [o["id"] for o in resp.json()]


async def test_legacy_import_runs_once_second_startup_imports_nothing(tmp_path):
    db_path = tmp_path / "msdr.db"
    seed_db = Database(str(db_path))
    seed_db.connect()
    seed_db.conn.execute(
        "INSERT INTO finds (id, first_ts, last_ts, seen_count, frequency_hz, status) "
        "VALUES ('legacy-find-1', 1.0, 2.0, 1, 152840000, 'new')"
    )
    seed_db.conn.commit()
    seed_db.close()

    async with _running_app_with_db_path(db_path) as (app, client, _adapter):
        resp = await client.get("/api/observations", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["legacy"] is True

    # Second startup: the settings key is already set, so import_legacy is a
    # no-op -- still exactly one observation, never doubled.
    async with _running_app_with_db_path(db_path) as (app2, client2, _adapter2):
        resp = await client2.get("/api/observations", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        assert len(resp.json()) == 1
