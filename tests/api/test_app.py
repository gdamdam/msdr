import asyncio
import contextlib
import json
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

import msdr.api.service as service_module
import msdr.api.services.sky as sky_module
import msdr.api.services.survey as survey_module
from msdr import __version__
from msdr.api.app import create_app
from msdr.api.service import (
    _MAX_QUEUE_SIZE,
    _TELEMETRY_MIN_INTERVAL_S,
    Broadcaster,
    next_outgoing,
)
from msdr.catalog.models import CatalogEntry
from msdr.engine.fake_adapter import FakeAdapter
from msdr.providers.nextpass import SATELLITES, Pass, PassesResult
from msdr.providers.tracking import SatPosition, SatTracker, SkyResult, SkyTrack, TleStore
from msdr.radio.protocol import ConnectionChanged, PropertyChanged
from msdr.settings import Location, Settings, SurveySettings, detect_timezone
from msdr.storage.db import Database

# This environment's starlette/anyio pairing emits two import-time
# DeprecationWarnings that -W error turns fatal, from starlette's own module
# body (not from any code of ours): it prefers an "httpx2" package for its
# test client (falling back to httpx, which is all we need), and touches the
# now-deprecated anyio.abc.BlockingPortal alias. Both are harmless for our
# purposes, so just silence the warnings for this one import rather than
# mutating third-party modules.
with warnings.catch_warnings():
    # Two different warning categories in play: anyio's is a real
    # DeprecationWarning, starlette's own is a UserWarning subclass -- ignore
    # both, scoped to just this import.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient, WebSocketDisconnect  # noqa: E402


@contextlib.asynccontextmanager
async def running_app(
    adapter: FakeAdapter | None = None, settings: Settings | None = None, survey=None
):
    adapter = adapter if adapter is not None else FakeAdapter()
    if settings is None:
        settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    # Isolate every filesystem-backed setting under a fresh temp dir, so a
    # developer's real ~/Library/Application Support/msdr (cached TLEs,
    # settings.json) or ~/.config/radio/location.json can never leak into a
    # test: a real cached TLE plus a real location makes AppService._predict_
    # passes silently pick the internal skyfield predictor instead of the
    # mocked nextpass provider tests set up (see providers/tracking.py
    # resolve_location and api/service.py._predict_passes). Only fields the
    # caller didn't already set are touched, so tests that pin their own
    # tmp_path locations (e.g. tle_cache_dir) are unaffected. db_path is
    # deliberately left alone: ":memory:" also doubles as AppService's
    # sentinel for "don't persist settings.json" (see apply_settings/
    # arm_recorder), so tests always pass it explicitly already.
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
        if "recorder_root" not in explicitly_set:
            overrides["recorder_root"] = str(tmp_path / "recorder")
        if "decoder" not in explicitly_set:
            overrides["decoder"] = settings.decoder.model_copy(
                update={
                    "calls_dir": str(tmp_path / "decoder" / "calls"),
                    "fax_dir": str(tmp_path / "decoder" / "fax"),
                }
            )
        if overrides:
            settings = settings.model_copy(update=overrides)
        db = Database(":memory:")
        app = create_app(
            settings,
            adapter=adapter,
            db=db,
            survey=survey,
            settings_path=tmp_path / "settings.json",
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield app, client, adapter


async def test_state_endpoint_after_seed():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["complete"] is True
        assert data["synced"] is True
        assert data["next_retry_s"] is None
        assert data["props"]["device_vfo_frequency"] == 145_800_000
        assert data["connected"] is True


async def test_retry_connection_route_returns_200():
    # FakeAdapter has no retry_now/retry_deadline -- the route must still
    # work via getattr (RadioAdapter protocol compatibility for adapters
    # without retry support).
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/connection/retry")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "next_retry_s": None}


async def test_capture_creates_inbox_signal_and_journal():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/capture")
        assert resp.status_code == 200
        signal = resp.json()
        assert signal["status"] == "unknown"
        assert signal["frequency_hz"] == 145_800_000

        inbox = (await client.get("/api/signals/inbox")).json()
        assert any(s["id"] == signal["id"] for s in inbox)

        journal = (await client.get("/api/journal")).json()
        entry = next(
            e for e in journal if e["kind"] == "capture" and e["signal_id"] == signal["id"]
        )
        assert entry["signal_color"] == signal["color"]
        assert entry["tag_ids"] == signal["tag_ids"]


async def test_capture_records_confirmed_antenna():
    async with running_app() as (app, client, adapter):
        assert adapter.state["active_antenna"] == "Antenna B"
        signal = (await client.post("/api/capture")).json()
        assert signal["antenna"] == "Antenna B"


async def test_tune_signal_restores_and_pushes_history():
    async with running_app() as (app, client, adapter):
        create_resp = await client.post(
            "/api/signals", json={"name": "Sat pass", "frequency_hz": 146_520_000}
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        report = resp.json()
        assert report["status"] == "confirmed"

        history = (await client.get("/api/history")).json()
        assert len(history["entries"]) == 2
        assert history["position"] == 1

        state = (await client.get("/api/state")).json()
        assert state["props"]["device_vfo_frequency"] == 146_520_000
        assert state["history"]["can_back"] is True


async def test_tune_signal_with_antenna_switches_current_antenna():
    async with running_app() as (app, client, adapter):
        assert adapter.state["active_antenna"] == "Antenna B"
        create_resp = await client.post(
            "/api/signals",
            json={"name": "Antenna A memory", "frequency_hz": 146_520_000, "antenna": "Antenna A"},
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna A"


async def test_tune_signal_without_antenna_leaves_current_antenna_unchanged():
    async with running_app() as (app, client, adapter):
        await client.post("/api/receiver/antenna", json={"antenna": "Antenna C"})
        create_resp = await client.post(
            "/api/signals", json={"name": "No antenna memory", "frequency_hz": 146_520_000}
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna C"


async def test_tune_signal_antenna_overrides_profile_antenna():
    async with running_app() as (app, client, adapter):
        profile_resp = await client.post(
            "/api/profiles", json={"name": "VHF profile", "antenna": "Antenna B"}
        )
        profile_id = profile_resp.json()["id"]
        create_resp = await client.post(
            "/api/signals",
            json={
                "name": "Antenna C memory",
                "frequency_hz": 146_520_000,
                "profile_id": profile_id,
                "antenna": "Antenna C",
            },
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna C"


async def test_antenna_lock_keeps_antenna_on_tune_but_ant_select_still_switches():
    async with running_app() as (app, client, adapter):
        assert adapter.state["active_antenna"] == "Antenna B"
        resp = await client.patch("/api/settings", json={"antenna_locked": True})
        assert resp.json()["antenna_locked"] is True
        create_resp = await client.post(
            "/api/signals",
            json={"name": "Antenna A memory", "frequency_hz": 146_520_000, "antenna": "Antenna A"},
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["device_vfo_frequency"] == 146_520_000
        assert adapter.state["active_antenna"] == "Antenna B"

        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna C"})
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna C"


async def test_lna_lock_keeps_lna_on_profile_but_lna_field_still_sets():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/receiver/lna", json={"lna_state": 5})
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["lna_state"] == 5
        resp = await client.patch("/api/settings", json={"lna_locked": True})
        assert resp.json()["lna_locked"] is True
        profile_id = (
            await client.post("/api/profiles", json={"name": "Hot LNA", "lna_state": 20})
        ).json()["id"]

        resp = await client.post(f"/api/profiles/{profile_id}/apply")
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["lna_state"] == 5

        resp = await client.post("/api/receiver/lna", json={"lna_state": 9})
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["lna_state"] == 9
        resp = await client.post("/api/receiver/lna", json={"lna_state": 60})
        assert resp.status_code == 422


async def test_signal_lna_state_applies_on_tune_unless_locked():
    async with running_app() as (app, client, adapter):
        assert adapter.state["lna_state"] == 3
        signal = (
            await client.post(
                "/api/signals",
                json={"name": "Weak beacon", "frequency_hz": 146_520_000, "lna_state": 12},
            )
        ).json()
        assert signal["lna_state"] == 12

        resp = await client.post(f"/api/tune/signal/{signal['id']}")
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["lna_state"] == 12

        await client.post("/api/receiver/lna", json={"lna_state": 4})
        await client.patch("/api/settings", json={"lna_locked": True})
        await client.post(f"/api/tune/signal/{signal['id']}")
        assert adapter.state["lna_state"] == 4

        resp = await client.patch(f"/api/signals/{signal['id']}", json={"lna_state": None})
        assert resp.json()["lna_state"] is None


async def test_auto_lna_applies_on_band_change_and_backs_off_on_overload():
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    settings.lna_auto.overload_hold_s = 0.05
    settings.lna_auto.max_backoff_steps = 2
    async with running_app(settings=settings) as (app, client, adapter):
        resp = await client.post("/api/tune/frequency", json={"hz": 1_090_000_000})
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["lna_state"] == 0
        # Same band: a hand-set LNA survives retuning.
        await client.post("/api/receiver/lna", json={"lna_state": 2})
        await client.post("/api/tune/frequency", json={"hz": 1_089_000_000})
        assert adapter.state["lna_state"] == 2

        await adapter.external_change("overload", True)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if adapter.state["lna_state"] == 4:
                break
        await asyncio.sleep(0.15)
        assert adapter.state["lna_state"] == 4  # capped at baseline + 2
        await adapter.external_change("overload", False)

        await client.patch("/api/settings", json={"lna_auto": {"enabled": False}})
        assert (await client.get("/api/settings")).json()["lna_auto"]["bands"][3]["lna_state"] == 0
        await client.post("/api/tune/frequency", json={"hz": 162_000_000})
        assert adapter.state["lna_state"] == 4


async def test_mode_lock_keeps_mode_on_tune_but_mode_select_still_switches():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/tune/frequency", json={"hz": 9_930_000, "demodulator": "SAM", "manual_mode": True}
        )
        assert resp.json()["status"] == "confirmed"
        await client.patch("/api/settings", json={"demodulator_locked": True})
        create_resp = await client.post(
            "/api/signals",
            json={"name": "AM memory", "frequency_hz": 13_845_000, "demodulator": "AM"},
        )
        signal_id = create_resp.json()["id"]

        resp = await client.post(f"/api/tune/signal/{signal_id}")
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["device_vfo_frequency"] == 13_845_000
        assert adapter.state["demodulator"] == "SAM"
        # Catalog / panel tunes send a demodulator without manual_mode: locked too.
        resp = await client.post(
            "/api/tune/frequency", json={"hz": 11_760_000, "demodulator": "AM"}
        )
        assert adapter.state["demodulator"] == "SAM"

        resp = await client.post(
            "/api/tune/frequency",
            json={"hz": 11_760_000, "demodulator": "USB", "manual_mode": True},
        )
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["demodulator"] == "USB"


async def test_back_restores_previous_state():
    async with running_app() as (app, client, adapter):
        original = (await client.get("/api/state")).json()["props"]["device_vfo_frequency"]
        tune = await client.post(
            "/api/tune/frequency", json={"hz": original + 500_000}
        )
        assert tune.json()["status"] == "confirmed"

        resp = await client.post("/api/history/back")
        assert resp.status_code == 200
        assert resp.json()["report"]["status"] == "confirmed"

        state = (await client.get("/api/state")).json()
        assert state["props"]["device_vfo_frequency"] == original


async def test_apply_profile_with_native_name_calls_apply_profile():
    async with running_app() as (app, client, adapter):
        # Registering the name in the fake's own profile table with a value
        # that differs from the seeded default gives it an event to settle
        # on, but does not prove which native profile was selected.
        adapter.profiles["VHF Sat"] = {"active_antenna": "Antenna A"}
        create_resp = await client.post(
            "/api/profiles", json={"name": "Sat profile", "native_profile_name": "VHF Sat"}
        )
        profile_id = create_resp.json()["id"]

        resp = await client.post(f"/api/profiles/{profile_id}/apply")
        assert resp.status_code == 200
        assert ("apply_profile", "VHF Sat") in adapter.calls
        assert resp.json()["status"] == "partial"
        assert resp.json()["outcomes"][0]["status"] == "unverified"


async def test_active_profile_is_only_the_applied_one_while_it_still_matches():
    async with running_app() as (app, client, adapter):
        props = (await client.get("/api/state")).json()["props"]
        created = await client.post("/api/profiles", json={
            "name": "Two-field profile",
            "antenna": props["active_antenna"],
            "lna_state": props["lna_state"],
        })
        profile_id = created.json()["id"]

        # Matching settings alone no longer label the receiver with it.
        assert (await client.get("/api/state")).json()["active_profile_id"] is None

        assert (await client.post(f"/api/profiles/{profile_id}/apply")).status_code == 200
        assert (await client.get("/api/state")).json()["active_profile_id"] == profile_id

        antennas = ("Antenna A", "Antenna B", "Antenna C")
        other = next(a for a in antennas if a != props["active_antenna"])
        resp = await client.post("/api/receiver/antenna", json={"antenna": other})
        assert resp.status_code == 200
        assert (await client.get("/api/state")).json()["active_profile_id"] is None


async def test_palette_frequency_query():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/palette", params={"q": "146.52"})
        assert resp.status_code == 200
        results = resp.json()
        assert results[0]["kind"] == "tune_frequency"
        assert results[0]["frequency_hz"] == 146_520_000


async def test_patch_identified_writes_journal():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]

        resp = await client.patch(f"/api/signals/{signal_id}", json={"status": "identified"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "identified"

        journal = (await client.get("/api/journal")).json()
        assert any(
            e["kind"] == "identified" and e["signal_id"] == signal_id for e in journal
        )


async def test_export_import_roundtrip_over_http():
    async with running_app() as (app, client, adapter):
        await client.post("/api/capture")

        doc = (await client.get("/api/export")).json()
        assert doc["format"] == "msdr/1"
        assert len(doc["signals"]) == 1

        resp = await client.post("/api/import", json=doc)
        assert resp.status_code == 200
        report = resp.json()
        assert report["skipped"].get("signals") == 1


async def test_tune_when_disconnected_returns_503():
    async with running_app() as (app, client, adapter):
        adapter.connected = False
        resp = await client.post("/api/tune/frequency", json={"hz": 146_000_000})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "SDRconnect not connected"


async def test_set_antenna_updates_confirmed_state():
    async with running_app() as (app, client, adapter):
        assert adapter.state["active_antenna"] == "Antenna B"
        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna A"})
        assert resp.status_code == 200
        report = resp.json()
        assert report["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna A"

        state = (await client.get("/api/state")).json()
        assert state["props"]["active_antenna"] == "Antenna A"
        # Not a retune: the VFO never moved for this restore.
        assert state["props"]["device_vfo_frequency"] == 145_800_000


async def test_set_antenna_invalid_value_is_422():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna Z"})
        assert resp.status_code == 422
        # Rejected before anything was sent to the radio.
        assert adapter.state["active_antenna"] == "Antenna B"


async def test_set_antenna_when_disconnected_returns_503():
    async with running_app() as (app, client, adapter):
        adapter.connected = False
        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna A"})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "SDRconnect not connected"


async def test_set_antenna_when_stopped_returns_503():
    adapter = FakeAdapter()
    adapter.state["started"] = False
    async with running_app(adapter=adapter) as (app, client, _adapter):
        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna A"})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Receiver stopped: press Play in SDRconnect"


async def test_set_antenna_409_while_a_restore_is_in_progress():
    # Mirrors tune_frequency's own RestoreRunning refusal (AppService._restore):
    # antenna goes through the same restore/executor guard.
    adapter = FakeAdapter()
    adapter.delay_s = 0.2  # keep the in-flight tune's step unconfirmed
    async with running_app(adapter) as (app, client, _adapter):
        service = app.state.service
        task = asyncio.create_task(service.tune_frequency(146_000_000))
        await asyncio.sleep(0.02)
        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna A"})
        assert resp.status_code == 409
        await task


async def test_patch_bogus_status_returns_422():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]

        resp = await client.patch(f"/api/signals/{signal_id}", json={"status": "bogus"})
        assert resp.status_code == 422

        # The rejected patch must not have been stored: /api/state (and so
        # every WS state push) still needs to be able to list signals.
        state = await client.get("/api/state")
        assert state.status_code == 200


async def test_history_states_persist_across_restart():
    # F2: history states must be persisted (not just the history table's
    # pointers to them), and the cursor must survive intact, or Back/Forward
    # restores the wrong state after a restart.
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    adapter = FakeAdapter()

    app1 = create_app(settings, adapter=adapter, db=db)
    real_close = db.close
    db.close = lambda: None  # in-memory db must survive app1's lifespan exit below
    try:
        async with app1.router.lifespan_context(app1):
            transport = httpx.ASGITransport(app=app1)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post("/api/capture")
                original = (await client.get("/api/state")).json()["props"][
                    "device_vfo_frequency"
                ]
                tune = await client.post(
                    "/api/tune/frequency", json={"hz": original + 500_000}
                )
                assert tune.json()["status"] == "confirmed"
                before = (await client.get("/api/history")).json()
    finally:
        db.close = real_close

    app2 = create_app(settings, adapter=adapter, db=db)
    async with app2.router.lifespan_context(app2):
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            after = (await client.get("/api/history")).json()

    assert after["position"] == before["position"]
    assert len(after["entries"]) == len(before["entries"])
    assert [e["vfo_hz"] for e in after["entries"]] == [e["vfo_hz"] for e in before["entries"]]


def test_ws_receives_state_and_restore_messages():
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    adapter = FakeAdapter()
    app = create_app(settings, adapter=adapter, db=db)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "state"

            resp = client.post("/api/tune/frequency", json={"hz": 146_000_000})
            assert resp.status_code == 200

            # Guaranteed to already be queued by the time the request above
            # returned: two "restore"/"step" messages (pending, then settled),
            # then "restore"/"done", "journal", "history", and a final full
            # "state".
            messages = [ws.receive_json() for _ in range(6)]
            types = [m["type"] for m in messages]
            assert types.count("restore") >= 2
            assert any(m["type"] == "restore" and m.get("phase") == "done" for m in messages)
            assert "journal" in types
            assert "history" in types


def test_ws_receives_state_after_reconnect_seed():
    # F4: a UI connected while SDRconnect was down only got the "connection"
    # message; it needs a "state" message right after the reconnect seed too.
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    adapter = FakeAdapter()
    app = create_app(settings, adapter=adapter, db=db)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "state"

            adapter.connected = False
            adapter._emit(ConnectionChanged(False, time.monotonic()))
            down = ws.receive_json()
            assert down == {"type": "connection", "connected": False}
            # mark_stale() flips synced to False without going through
            # ConfirmedState's own subscribers, so a "state" push is needed
            # here too or a connected UI never learns synced changed.
            down_state = ws.receive_json()
            assert down_state["type"] == "state"
            assert down_state["synced"] is False

            adapter.connected = True
            adapter._emit(ConnectionChanged(True, time.monotonic()))
            messages = [ws.receive_json() for _ in range(2)]
            assert messages[0] == {"type": "connection", "connected": True}
            assert messages[1]["type"] == "state"
            assert messages[1]["complete"] is True


async def test_patch_signal_color_valid_and_invalid():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]

        resp = await client.patch(f"/api/signals/{signal_id}", json={"color": "orange"})
        assert resp.status_code == 200
        assert resp.json()["color"] == "orange"

        stored = await client.get(f"/api/signals/{signal_id}")
        assert stored.json()["color"] == "orange"

        bad = await client.patch(f"/api/signals/{signal_id}", json={"color": "not-a-color"})
        assert bad.status_code == 422


async def test_patch_tag_color_and_name():
    async with running_app() as (app, client, adapter):
        tag_id = (await client.post("/api/tags", json={"name": "my-tag"})).json()["id"]

        resp = await client.patch(f"/api/tags/{tag_id}", json={"name": "renamed", "color": "gray"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "renamed"
        assert body["color"] == "gray"


async def test_patch_tag_unknown_returns_404():
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/tags/does-not-exist", json={"color": "red"})
        assert resp.status_code == 404


async def test_patch_tag_duplicate_name_returns_409():
    async with running_app() as (app, client, adapter):
        await client.post("/api/tags", json={"name": "existing"})
        other_id = (await client.post("/api/tags", json={"name": "other"})).json()["id"]

        resp = await client.patch(f"/api/tags/{other_id}", json={"name": "existing"})
        assert resp.status_code == 409


async def test_state_has_version_and_tuned_signal_id():
    async with running_app() as (app, client, adapter):
        state = (await client.get("/api/state")).json()
        assert state["version"] == __version__
        assert state["tuned_signal_id"] is None

        vfo = state["props"]["device_vfo_frequency"]
        create_resp = await client.post(
            "/api/signals", json={"name": "On freq", "frequency_hz": vfo}
        )
        signal_id = create_resp.json()["id"]

        state = (await client.get("/api/state")).json()
        assert state["tuned_signal_id"] == signal_id


async def test_tuned_signal_id_clamped_when_nearby_hz_below_1000():
    # Regression: tuned_signal_id search must use a radius of at least 1000 Hz
    # even when settings.nearby_hz is configured smaller, or it under-reports
    # (previously it just reused the "nearby" list, whose own radius could be
    # narrower than the guaranteed 1000 Hz tuning radius).
    adapter = FakeAdapter()
    adapter.state["filter_bandwidth"] = 0
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", nearby_hz=100)
    async with running_app(adapter=adapter, settings=settings) as (app, client, _adapter):
        vfo = (await client.get("/api/state")).json()["props"]["device_vfo_frequency"]
        create_resp = await client.post(
            "/api/signals", json={"name": "800 Hz off", "frequency_hz": vfo + 800}
        )
        signal_id = create_resp.json()["id"]

        state = (await client.get("/api/state")).json()
        assert state["tuned_signal_id"] == signal_id


async def test_tune_when_stopped_returns_503_and_leaves_history():
    # F7: SDRconnect reports started=false (Play not pressed) and silently
    # ignores every set_property while stopped -- msdr must refuse up front
    # instead of burning a restore's full timeout and reporting "failed".
    adapter = FakeAdapter()
    adapter.state["started"] = False
    async with running_app(adapter=adapter) as (app, client, _adapter):
        state = (await client.get("/api/state")).json()
        assert state["started"] is False

        before = (await client.get("/api/history")).json()
        resp = await client.post("/api/tune/frequency", json={"hz": 146_000_000})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Receiver stopped: press Play in SDRconnect"

        after = (await client.get("/api/history")).json()
        assert after == before

        capture_resp = await client.post("/api/capture")
        assert capture_resp.status_code == 200


def test_ws_receives_state_when_started_flips_true():
    # F7: `started` is read-only, so ConfirmedState.apply() applies it
    # silently with no StateChanged -- without the dedicated publish in
    # _consume_events, a connected UI would never learn Play was pressed.
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.state["started"] = False
    app = create_app(settings, adapter=adapter, db=db)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "state"
            assert first["started"] is False

            # Same pattern as test_ws_receives_state_after_reconnect_seed:
            # _emit() is synchronous, so it's safe to call directly from this
            # thread rather than needing the TestClient's own event loop.
            adapter.state["started"] = True
            adapter._emit(PropertyChanged("started", True, "primary", time.monotonic()))

            msg = ws.receive_json()
            assert msg["type"] == "state"
            assert msg["started"] is True


async def test_secondary_receiver_events_do_not_change_primary_state():
    async with running_app() as (app, client, adapter):
        before = (await client.get("/api/state")).json()
        assert before["props"]["device_vfo_frequency"] == 145_800_000
        assert before["started"] is True

        adapter._emit(
            PropertyChanged("device_vfo_frequency", 433_920_000, "secondary", time.monotonic())
        )
        adapter._emit(PropertyChanged("started", False, "secondary", time.monotonic()))
        await asyncio.sleep(0.02)

        after = (await client.get("/api/state")).json()
        assert after["props"]["device_vfo_frequency"] == 145_800_000
        assert after["started"] is True


async def test_post_rejected_with_foreign_origin():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/capture", headers={"Origin": "https://evil.example"}
        )
        assert resp.status_code == 403


async def test_post_allowed_with_local_origin():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/capture", headers={"Origin": "http://127.0.0.1:8765"}
        )
        assert resp.status_code == 200


def test_ws_rejects_foreign_origin():
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    adapter = FakeAdapter()
    app = create_app(settings, adapter=adapter, db=db)

    with TestClient(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            pass


async def test_identify_query_returns_wx1():
    # WX1 = 162.550 MHz (not ascending by frequency; see bandplan_us.json).
    async with running_app() as (app, client, adapter):
        resp = await client.get(
            "/api/identify", params={"hz": 162_550_000, "mode": "NFM", "bw": 12500}
        )
        assert resp.status_code == 200
        candidates = resp.json()["candidates"]
        assert "WX1" in candidates[0]["name"]


async def test_identify_signal_returns_candidate_list():
    async with running_app() as (app, client, adapter):
        # FakeAdapter's seeded VFO (145.800 MHz) is the ISS voice downlink.
        capture_resp = await client.post("/api/capture")
        signal_id = capture_resp.json()["id"]

        resp = await client.get(f"/api/signals/{signal_id}/identify")
        assert resp.status_code == 200
        assert isinstance(resp.json()["candidates"], list)


async def test_identify_signal_unknown_id_returns_404():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/signals/does-not-exist/identify")
        assert resp.status_code == 404


@pytest.mark.parametrize("value,code", [(-1, 422), (0, 200), (60, 200), (61, 422)])
async def test_patch_peak_threshold_bounds(value, code):
    async with running_app() as (_, client, _adapter):
        response = await client.patch(
            "/api/settings", json={"survey": {"peak_threshold_db": value}}
        )
        assert response.status_code == code
        if code == 200:
            assert response.json()["survey"]["peak_threshold_db"] == value


async def test_partial_survey_settings_patch_preserves_other_preferences():
    settings = Settings(db_path=":memory:", survey=SurveySettings(spectrum_fps=7, max_analyzed=2))
    async with running_app(settings=settings) as (_, client, _adapter):
        response = await client.patch("/api/settings", json={"survey": {"peak_threshold_db": 15}})
        assert response.status_code == 200
        survey = response.json()["survey"]
        assert survey["peak_threshold_db"] == 15
        assert survey["spectrum_fps"] == 7
        assert survey["max_analyzed"] == 2


async def test_patch_settings_region_eu_empties_identify():
    async with running_app() as (app, client, adapter):
        patch_resp = await client.patch("/api/settings", json={"region": "EU"})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["region"] == "EU"

        resp = await client.get("/api/identify", params={"hz": 162_550_000})
        assert resp.json()["candidates"] == []


async def test_patch_settings_passes_min_elevation_and_location_round_trip():
    async with running_app() as (app, client, adapter):
        patch_resp = await client.patch(
            "/api/settings",
            json={
                "passes_min_elevation": 35,
                "location": {
                    "lat": 37.7,
                    "lon": -122.4,
                    "altitude_m": 10,
                    "timezone": "America/Los_Angeles",
                },
            },
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["passes_min_elevation"] == 35
        assert patch_resp.json()["location"]["lat"] == 37.7

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["passes_min_elevation"] == 35
        assert get_resp.json()["location"]["timezone"] == "America/Los_Angeles"


async def test_patch_settings_explicit_null_location_clears_but_omitted_keeps():
    async with running_app(
        settings=Settings(dwell_seconds=0.05, db_path=":memory:", location=_SAN_FRANCISCO)
    ) as (app, client, adapter):
        # Omitted key: exclude_unset drops it from the patch body entirely,
        # so location must be left untouched.
        resp = await client.patch("/api/settings", json={"region": "EU"})
        assert resp.status_code == 200
        assert resp.json()["location"]["lat"] == _SAN_FRANCISCO.lat

        # Explicit null: location is nullable, so this must clear it rather
        # than being silently dropped like a null for a non-nullable field.
        resp = await client.patch("/api/settings", json={"location": None})
        assert resp.status_code == 200
        assert resp.json()["location"] is None

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["location"] is None


async def test_patch_settings_passes_days_out_of_range_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"passes_days": 30})
        assert resp.status_code == 422


async def test_get_settings_default_layout_matches_current_rendering_order():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        # S1: listen's default names only its own ids; every other id is
        # backfilled (SECTION_IDS order) into the last column.
        assert resp.json()["layout"]["columns"] == [
            ["survey", "listen", "decode"],
            ["nearby", "recent"],
            ["log", "inbox", "passes", "sky", "scans", "catalog", "beacons", "finds",
             "scan", "review", "library", "allocations"],
        ]


async def test_patch_settings_layout_round_trips():
    async with running_app() as (app, client, adapter):
        columns = [
            ["inbox"],
            ["survey", "nearby", "recent"],
            ["listen", "log", "sky", "passes", "scans", "catalog", "beacons", "finds",
             "scan", "decode", "review", "library", "allocations"],
        ]
        resp = await client.patch("/api/settings", json={"layout": {"columns": columns}})
        assert resp.status_code == 200
        assert resp.json()["layout"]["columns"] == columns

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["layout"]["columns"] == columns


async def test_patch_settings_layout_drops_retired_scanner_id():
    # "scanner" was a grid section id through v0.14.1 (SECTION_IDS then
    # included it); v0.14.2 moved it into the instrument panel, so a
    # settings.json saved by an older version still has it in layout.columns.
    # It must load and come back without it -- not 422 as an unknown id.
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={
                "layout": {
                    "columns": [
                        ["nearby"],
                        ["inbox", "recent"],
                        ["listen", "log", "passes", "sky", "scans", "catalog", "scanner"],
                    ]
                }
            },
        )
        assert resp.status_code == 200
        assert "scanner" not in resp.json()["layout"]["columns"][-1]

        get_resp = await client.get("/api/settings")
        assert "scanner" not in get_resp.json()["layout"]["columns"][-1]


async def test_patch_settings_layout_wrong_column_count_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings", json={"layout": {"columns": [["nearby"], ["inbox"]]}}
        )
        assert resp.status_code == 422


async def test_patch_settings_layout_unknown_id_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={"layout": {"columns": [["not-a-section"], [], []]}},
        )
        assert resp.status_code == 422


async def test_patch_settings_layout_duplicate_id_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={"layout": {"columns": [["nearby", "nearby"], [], []]}},
        )
        assert resp.status_code == 422


async def test_patch_settings_layout_missing_id_appended_to_last_column():
    async with running_app() as (app, client, adapter):
        # "catalog" is a known section id left out of every column here --
        # the Layout validator must backfill it (see msdr/settings.py) rather
        # than silently dropping it, so a section added later still shows up.
        resp = await client.patch(
            "/api/settings",
            json={
                "layout": {
                    "columns": [
                        ["nearby"],
                        ["inbox", "recent"],
                        ["listen", "log", "passes", "sky", "scans"],
                    ]
                }
            },
        )
        assert resp.status_code == 200
        # Missing ids are backfilled in SECTION_IDS order -- "catalog",
        # "beacons", "finds" and the S1 ids are the ones left out here --
        # which is also how an existing user's saved layout picks up a
        # section added later.
        assert resp.json()["layout"]["columns"][-1][-8:] == [
            "catalog", "beacons", "finds", "scan", "decode", "review", "library", "allocations"
        ]


async def test_patch_settings_layout_missing_survey_appended_to_last_column():
    # A layout saved before "survey" was a section id (no "survey" in any
    # column) must still load, with "survey" backfilled by the existing
    # missing-id rule rather than raising -- same rule test_..._missing_id_
    # appended_to_last_column above exercises for "catalog".
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={
                "layout": {
                    "columns": [
                        ["nearby"],
                        ["inbox", "recent"],
                        ["listen", "log", "passes", "sky", "scans", "catalog"],
                    ]
                }
            },
        )
        assert resp.status_code == 200
        assert "survey" in resp.json()["layout"]["columns"][-1]


def test_layout_default_wide_is_survey():
    assert Settings().layout.wide == ["survey"]


def test_layout_wide_rejects_unknown_id():
    with pytest.raises(ValidationError):
        Settings(layout={"wide": ["not-a-section"]})


def test_layout_wide_rejects_duplicate_id():
    with pytest.raises(ValidationError):
        Settings(layout={"wide": ["survey", "survey"]})


def test_settings_load_old_file_without_wide(tmp_path):
    # A settings.json saved before "wide" existed has no layout.wide at all
    # -- it must still load, backfilled with the field default, same as any
    # other additive field (see test_settings_load_old_file_without_analyzer_
    # fields below for the analogous survey.* case).
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"layout": {"columns": [["nearby"], ["inbox"], ["survey"]]}}))
    loaded = Settings.load(path)
    assert loaded.layout.wide == ["survey"]


async def test_patch_settings_layout_wide_round_trips_and_keeps_columns():
    async with running_app() as (app, client, adapter):
        columns = [
            ["inbox"],
            ["survey", "nearby", "recent"],
            ["listen", "log", "sky", "passes", "scans", "catalog", "beacons", "finds",
             "scan", "decode", "review", "library", "allocations"],
        ]
        resp = await client.patch("/api/settings", json={"layout": {"columns": columns}})
        assert resp.status_code == 200

        resp = await client.patch(
            "/api/settings", json={"layout": {"wide": ["survey", "finds"]}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["layout"]["wide"] == ["survey", "finds"]
        # A wide-only patch must leave columns exactly as previously set.
        assert body["layout"]["columns"] == columns

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["layout"]["wide"] == ["survey", "finds"]
        assert get_resp.json()["layout"]["columns"] == columns


async def test_patch_settings_layout_columns_only_leaves_wide_unchanged():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings", json={"layout": {"wide": ["finds"]}}
        )
        assert resp.status_code == 200

        resp = await client.patch(
            "/api/settings",
            json={"layout": {"columns": [["nearby"], ["inbox", "recent"], ["listen"]]}},
        )
        assert resp.status_code == 200
        assert resp.json()["layout"]["wide"] == ["finds"]


async def test_patch_settings_layout_wide_id_missing_from_columns_still_backfilled():
    # "finds" left out of every column here -- the existing missing-id rule
    # must still backfill it into the last column even though it's also
    # listed in "wide" (wide ids keep their slot in columns).
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={
                "layout": {
                    "wide": ["finds"],
                    "columns": [
                        ["nearby"],
                        ["inbox", "recent"],
                        ["listen", "log", "passes", "sky", "scans", "catalog", "survey", "beacons"],
                    ],
                }
            },
        )
        assert resp.status_code == 200
        assert "finds" in resp.json()["layout"]["columns"][-1]
        assert resp.json()["layout"]["wide"] == ["finds"]


# --- U2 workspaces --------------------------------------------------------


def test_settings_default_workspaces():
    settings = Settings()
    assert settings.workspace == "listen"
    assert list(settings.workspaces) == [
        "listen", "discover", "map", "band_scans", "review", "library", "satellites", "catalog",
        "allocations",
    ]
    # "layout" is a pure mirror of the active ("listen") workspace.
    assert settings.layout == settings.workspaces["listen"]
    assert settings.layout.columns[:2] == [["survey", "listen", "decode"], ["nearby", "recent"]]
    assert settings.layout.columns[2][0] == "log"
    assert settings.workspaces["discover"].columns[:2] == [["survey"], ["scan", "finds", "inbox"]]
    assert settings.workspaces["discover"].wide == ["survey", "scan", "finds"]
    assert settings.workspaces["review"].columns[0] == ["review"]
    assert settings.workspaces["review"].wide == ["review"]
    assert settings.workspaces["library"].columns[:2] == [["library"], []]
    assert settings.workspaces["library"].wide == ["library"]
    assert settings.workspaces["satellites"].columns[0] == ["passes"]
    assert settings.workspaces["catalog"].columns[0] == ["catalog"]


def test_settings_load_old_file_seeds_listen_workspace(tmp_path):
    # A settings.json saved before workspaces existed has only a top-level
    # `layout` -- "listen" must be seeded from it exactly, with the other
    # three workspaces left at their own new defaults.
    path = tmp_path / "settings.json"
    old_columns = [
        ["inbox"],
        ["survey", "nearby", "recent"],
        ["listen", "log", "sky", "passes", "scans", "catalog", "beacons", "finds"],
    ]
    path.write_text(json.dumps({"layout": {"columns": old_columns, "wide": ["survey"]}}))
    loaded = Settings.load(path)
    assert loaded.workspace == "listen"
    # Seeded as-is, plus the S1 ids backfilled into the last column.
    expected = [
        *old_columns[:2],
        [*old_columns[2], "scan", "decode", "review", "library", "allocations"],
    ]
    assert loaded.workspaces["listen"].columns == expected
    assert loaded.layout.columns == expected
    assert loaded.workspaces["discover"].columns[0] == ["survey"]


def test_settings_workspace_rejects_unknown_id():
    with pytest.raises(ValidationError):
        Settings(workspace="bogus")


def test_settings_workspaces_rejects_unknown_id():
    with pytest.raises(ValidationError):
        Settings(workspaces={"bogus": {}})


async def test_patch_settings_workspace_switches_active_workspace_and_mirror():
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"workspace": "discover"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace"] == "discover"
        assert body["layout"]["columns"] == body["workspaces"]["discover"]["columns"]

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["workspace"] == "discover"


async def test_patch_settings_workspaces_partial_merge_leaves_others_untouched():
    async with running_app() as (app, client, adapter):
        get_resp = await client.get("/api/settings")
        before = get_resp.json()["workspaces"]

        resp = await client.patch(
            "/api/settings", json={"workspaces": {"discover": {"wide": ["finds"]}}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspaces"]["discover"]["wide"] == ["finds"]
        # Untouched field of the touched workspace, and every other
        # workspace, keep their prior values.
        assert body["workspaces"]["discover"]["columns"] == before["discover"]["columns"]
        assert body["workspaces"]["listen"] == before["listen"]
        assert body["workspaces"]["satellites"] == before["satellites"]
        assert body["workspaces"]["library"] == before["library"]


async def test_patch_settings_workspace_unknown_id_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"workspace": "bogus"})
        assert resp.status_code == 422


async def test_patch_settings_workspaces_unknown_id_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings", json={"workspaces": {"bogus": {"wide": []}}}
        )
        assert resp.status_code == 422


async def test_patch_settings_legacy_layout_targets_active_workspace():
    # A pre-workspaces client only ever sends {"layout": ...} -- it must keep
    # editing the *active* workspace (not always "listen").
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"workspace": "satellites"})
        assert resp.status_code == 200

        resp = await client.patch("/api/settings", json={"layout": {"wide": ["sky"]}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["layout"]["wide"] == ["sky"]
        assert body["workspaces"]["satellites"]["wide"] == ["sky"]
        # "listen" (the other workspace) is untouched by a legacy patch made
        # while "satellites" is active.
        assert body["workspaces"]["listen"]["wide"] == ["survey"]


def test_survey_settings_analyzer_defaults():
    # S3/S4 IQ analyzer knobs (max_analyzed/analyze_dwell_s/analyze_min_snr_db)
    # -- see msdr.survey.analyzer.Analyzer.
    survey = Settings().survey
    assert survey.max_analyzed == 6
    assert survey.analyze_dwell_s == 2.0
    assert survey.analyze_min_snr_db == 8.0


def test_settings_load_old_file_without_analyzer_fields(tmp_path):
    # A settings.json saved before S3/S4 has no survey.max_analyzed/
    # analyze_dwell_s/analyze_min_snr_db -- it must still load, backfilled
    # with the new fields' defaults, same as any other additive field.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"survey": {"threshold_db": 12.0}}))
    loaded = Settings.load(path)
    assert loaded.survey.threshold_db == 12.0
    assert loaded.survey.max_analyzed == 6
    assert loaded.survey.analyze_dwell_s == 2.0
    assert loaded.survey.analyze_min_snr_db == 8.0


def test_survey_settings_waterfall_defaults():
    # SETUP's Waterfall group (wf_min_db/wf_max_db default to None -- "auto
    # from the stream", today's behaviour).
    survey = Settings().survey
    assert survey.wf_min_db is None
    assert survey.wf_max_db is None
    assert survey.wf_avg_factor == 0.5
    assert survey.wf_peak_hold is False
    assert survey.wf_palette == "sdrsharp"
    assert survey.wf_history_rows == 300
    assert survey.wf_show_grid is True


def test_survey_settings_waterfall_validates_ranges():
    with pytest.raises(ValidationError):
        SurveySettings(wf_avg_factor=0.96)
    with pytest.raises(ValidationError):
        SurveySettings(wf_avg_factor=-0.1)
    with pytest.raises(ValidationError):
        SurveySettings(wf_history_rows=49)
    with pytest.raises(ValidationError):
        SurveySettings(wf_history_rows=2001)
    with pytest.raises(ValidationError):
        SurveySettings(wf_palette="rainbow")
    with pytest.raises(ValidationError):
        SurveySettings(wf_min_db=-10, wf_max_db=-20)  # min must be < max


def test_settings_load_old_file_without_waterfall_fields(tmp_path):
    # A settings.json saved before the Waterfall group landed has none of the
    # wf_* keys -- it must still load, backfilled with their defaults.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"survey": {"threshold_db": 12.0}}))
    loaded = Settings.load(path)
    assert loaded.survey.threshold_db == 12.0
    assert loaded.survey.wf_min_db is None
    assert loaded.survey.wf_max_db is None
    assert loaded.survey.wf_avg_factor == 0.5
    assert loaded.survey.wf_peak_hold is False
    assert loaded.survey.wf_palette == "sdrsharp"
    assert loaded.survey.wf_history_rows == 300
    assert loaded.survey.wf_show_grid is True


class FakeSurvey:
    """Stand-in for msdr.survey.Survey (owned by a concurrent stream) -- just
    enough surface for AppService's survey_start/survey_stop/survey_span and
    state_payload()["survey"] wiring."""

    def __init__(self):
        self.active = False
        self.span_calls: list[tuple[int, int, bool]] = []
        self.dimensions: list[str] = ["activity", "bandwidth", "bandplan", "catalog"]
        # S5 decode handoff surface.
        self.decode_calls: list[tuple[int, list[str] | None]] = []
        self.undecode_calls: list[int] = []
        self.note_decode_event_calls: list[dict] = []
        self.identity_summary_result: dict | None = None
        # S10 beacon monitor's refusal rule reads status()["analyzer"]["active"].
        self.analyzer_active = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self) -> dict:
        return {
            "active": self.active,
            "since": None,
            "span": {"center_hz": None, "sample_rate_hz": None, "vfo_hz": None},
            "stream": {"connected": False, "frames": 0, "fps": 0.0},
            "occupancy": {
                "short": [],
                "long": [],
                "floor": 0,
                "threshold_db": 8.0,
                "windows_s": [60, 600],
            },
            "presets": [],
            "sample_rates_hz": [2_000_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000],
            # S10 beacon monitor's refusal rule reads this (AppService's
            # start_session guard) -- see tests/api/test_beacons.py.
            "analyzer": {"active": self.analyzer_active},
        }

    async def start_survey(self) -> dict:
        self.active = True
        return self.status()

    async def stop_survey(self) -> dict:
        self.active = False
        return self.status()

    async def set_span(self, sample_rate_hz: int, center_hz: int, keep_vfo: bool = True) -> dict:
        self.span_calls.append((sample_rate_hz, center_hz, keep_vfo))
        return self.status()

    def set_dimensions(self, dimensions: list[str]) -> dict:
        self.dimensions = list(dimensions)
        return self.status()

    async def decode(self, frequency_hz: int, decoders: list[str] | None) -> dict:
        self.decode_calls.append((frequency_hz, decoders))
        return self.status()

    async def undecode(self, frequency_hz: int) -> dict:
        self.undecode_calls.append(frequency_hz)
        return self.status()

    def note_decode_event(self, event: dict) -> None:
        self.note_decode_event_calls.append(event)

    def identity_summary(self, frequency_hz: int) -> dict | None:
        return self.identity_summary_result


async def test_survey_routes_package_unavailable_returns_409(monkeypatch):
    # Simulates a checkout without msdr.survey (AppService._build_survey's
    # import guard, mirroring msdr.decoder/msdr.recorder) -- survey is None,
    # so start/stop/span are refused with a 409 SurveyError.
    monkeypatch.setattr(service_module, "Survey", None)
    monkeypatch.setattr(survey_module, "Survey", None)
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/survey")
        assert resp.status_code == 200
        assert resp.json() is None

        assert (await client.post("/api/survey/start")).status_code == 409
        assert (await client.post("/api/survey/stop")).status_code == 409
        resp = await client.post(
            "/api/survey/span",
            json={"sample_rate_hz": 2_000_000, "center_hz": 152_500_000},
        )
        assert resp.status_code == 409

        resp = await client.post("/api/survey/dimensions", json={"dimensions": ["activity"]})
        assert resp.status_code == 409


async def test_survey_routes_with_fake_survey():
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, adapter):
        resp = await client.get("/api/survey")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        resp = await client.post("/api/survey/start")
        assert resp.status_code == 200
        assert resp.json()["active"] is True

        resp = await client.post(
            "/api/survey/span",
            json={"sample_rate_hz": 4_000_000, "center_hz": 930_500_000, "keep_vfo": False},
        )
        assert resp.status_code == 200
        assert fake.span_calls == [(4_000_000, 930_500_000, False)]

        resp = await client.post("/api/survey/stop")
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        state = await client.get("/api/state")
        assert state.json()["survey"]["active"] is False


async def test_survey_dimensions_route_updates_fake_survey():
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, adapter):
        resp = await client.post(
            "/api/survey/dimensions", json={"dimensions": ["activity", "bandplan"]}
        )
        assert resp.status_code == 200
        assert fake.dimensions == ["activity", "bandplan"]
        # Remembered in settings, so a restart doesn't drop the selection.
        assert app.state.service.settings.survey.dimensions == ["activity", "bandplan"]


async def test_survey_dimensions_route_accepts_iq_dimensions():
    # S3/S4: the needs_iq dimensions (modulation/symbol_rate/voice) are no
    # longer refused at this layer -- the route just passes whatever
    # set_dimensions returns/raises straight through (the real Survey does
    # the accept/reject check itself, per the plan's contract).
    fake = FakeSurvey()
    async with running_app(survey=fake) as (app, client, adapter):
        resp = await client.post(
            "/api/survey/dimensions",
            json={"dimensions": ["modulation", "symbol_rate", "voice"]},
        )
        assert resp.status_code == 200
        assert fake.dimensions == ["modulation", "symbol_rate", "voice"]


async def test_build_survey_passes_iq_stream_factory(monkeypatch):
    # AppService._build_survey wires an iq_stream_factory kwarg (S3/S4's
    # Analyzer, over its own SDRconnect connection, same pattern as
    # _build_decoder's candidate_stream) -- captured here via a stand-in
    # Survey class since the real one may not accept the kwarg yet
    # (concurrent Stream A work).
    captured: dict = {}

    class CapturingSurvey:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        def status(self) -> dict:
            return {"active": False}

    monkeypatch.setattr(service_module, "Survey", CapturingSurvey)

    monkeypatch.setattr(survey_module, "Survey", CapturingSurvey)
    async with running_app():
        assert "iq_stream_factory" in captured
        assert callable(captured["iq_stream_factory"])


async def test_identify_for_survey_returns_bandplan_and_catalog_candidates():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        service.catalog.replace_source(
            "test",
            [
                CatalogEntry(
                    source="test",
                    frequency_hz=162_550_000,
                    station="Test Station",
                    mode="NFM",
                    bandwidth_hz=12500,
                )
            ],
        )
        # WX1 = 162.550 MHz (bandplan_us.json) plus the catalog entry seeded
        # above -- both providers should contribute.
        candidates = service._identify_for_survey(162_550_000, 12_500)
        providers = {c["provider"] for c in candidates}
        assert providers == {"bandplan", "catalog"}
        for c in candidates:
            assert set(c) == {"name", "category", "confidence", "reason", "provider"}


async def test_identify_for_survey_returns_empty_when_features_off():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        service.settings.features.ident = False
        service.settings.features.catalog = False
        assert service._identify_for_survey(162_550_000, 12_500) == []


async def test_identify_for_survey_swallows_provider_exception():
    async with running_app() as (app, client, adapter):
        service = app.state.service

        def _boom(q):
            raise RuntimeError("provider exploded")

        service.identify_provider.identify = _boom
        assert service._identify_for_survey(162_550_000, 12_500) == []


def test_settings_loads_without_survey_key():
    # Backward compatibility: a settings.json saved before this feature
    # landed has no "survey" key at all (nor features.survey) -- Settings
    # must still load, filling in the SurveySettings/features.survey defaults.
    data = Settings(db_path=":memory:").model_dump()
    del data["survey"]
    del data["features"]["survey"]
    settings = Settings.model_validate(data)
    assert settings.survey.threshold_db == 8.0
    assert settings.features.survey is True


async def test_patch_signal_satellite_valid_and_invalid():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]

        resp = await client.patch(f"/api/signals/{signal_id}", json={"satellite": "ISS"})
        assert resp.status_code == 200
        assert resp.json()["satellite"] == "ISS"

        stored = await client.get(f"/api/signals/{signal_id}")
        assert stored.json()["satellite"] == "ISS"

        bad = await client.patch(f"/api/signals/{signal_id}", json={"satellite": "Sputnik-1"})
        assert bad.status_code == 422


class _FakeNextPassProvider:
    """Records passes() calls and returns a canned, error-free result."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def passes(
        self,
        satellites,
        *,
        days=2,
        min_elevation=20.0,
        horizon=10.0,
        top=50,
        location=None,
        refresh=False,
        extra_satellites=None,
    ):
        self.calls.append((tuple(satellites), days, min_elevation, horizon, top, location, refresh))
        return PassesResult(passes=[], generated_at="2026-01-01T00:00:00Z", source="fresh")


async def test_get_passes_uses_the_signals_satellite():
    async with running_app() as (app, client, adapter):
        await client.post(
            "/api/signals",
            json={"name": "Meteor pass", "frequency_hz": 137_900_000, "satellite": "M2-3"},
        )
        fake = _FakeNextPassProvider()
        app.state.service.nextpass_provider = fake

        resp = await client.get("/api/passes", params={"days": 3})
        assert resp.status_code == 200
        assert resp.json()["source"] == "fresh"
        # days is overridden by the query param; the rest come from settings defaults.
        assert fake.calls == [(("M2-3",), 3, 20.0, 10.0, 50, None, False)]


async def test_get_passes_query_overrides_min_elevation_horizon_top():
    async with running_app() as (app, client, adapter):
        fake = _FakeNextPassProvider()
        app.state.service.nextpass_provider = fake

        resp = await client.get(
            "/api/passes", params={"min_elevation": 35, "horizon": 20, "top": 5}
        )
        assert resp.status_code == 200
        assert fake.calls == [(tuple(SATELLITES), 2, 35.0, 20.0, 5, None, False)]


async def test_passes_refresh_passes_refresh_flag_through(monkeypatch, tmp_path):
    # refresh=True now also refreshes TLEs (AppService._refresh_tles) before
    # falling back to nextpass -- give it a tmp cache dir and a stubbed
    # fetch_omm so this stays fully offline.
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", tle_cache_dir=str(tmp_path / "tle")
    )
    async with running_app(settings=settings) as (app, client, adapter):
        fake = _FakeNextPassProvider()
        app.state.service.nextpass_provider = fake

        async def fake_fetch_omm(norad):
            return {"EPOCH": "2026-01-01T00:00:00"}

        monkeypatch.setattr(sky_module, "fetch_omm", fake_fetch_omm)

        resp = await client.post("/api/passes/refresh")
        assert resp.status_code == 200
        assert resp.json()["engine"] == "nextpass"
        assert fake.calls == [(tuple(SATELLITES), 2, 20.0, 10.0, 50, None, True)]


async def test_passes_refresh_also_fetches_extra_satellite_tles(monkeypatch, tmp_path):
    settings = Settings(
        dwell_seconds=0.05,
        db_path=":memory:",
        tle_cache_dir=str(tmp_path / "tle"),
        passes_extra_satellites=[{"label": "CUSTOM-1", "norad_id": 99999}],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        app.state.service.nextpass_provider = _FakeNextPassProvider()
        fetched: list[int] = []

        async def fake_fetch_omm(norad):
            fetched.append(norad)
            return {"EPOCH": "2026-01-01T00:00:00"}

        monkeypatch.setattr(sky_module, "fetch_omm", fake_fetch_omm)

        resp = await client.post("/api/passes/refresh")
        assert resp.status_code == 200
        assert 99999 in fetched
        # And the fetched OMM actually landed in the store the extra
        # satellite resolves against (AppService._extra_catalog).
        assert app.state.service.tle_store.load(99999) is not None


ISS_OMM_FOR_PASSES = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-09-22T06:30:37.496448",
    "MEAN_MOTION": 15.49224498,
    "ECCENTRICITY": 0.00047657,
    "INCLINATION": 51.6312,
    "RA_OF_ASC_NODE": 179.6046,
    "ARG_OF_PERICENTER": 167.6102,
    "MEAN_ANOMALY": 192.5004,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 58680,
    "BSTAR": 0.0001364276,
    "MEAN_MOTION_DOT": 7.132e-05,
    "MEAN_MOTION_DDOT": 0,
}
_SAN_FRANCISCO = Location(lat=37.77, lon=-122.42, altitude_m=15.0, timezone="America/Los_Angeles")


async def test_passes_uses_internal_engine_when_tles_cached_and_location_set(tmp_path):
    settings = Settings(
        dwell_seconds=0.05,
        db_path=":memory:",
        location=_SAN_FRANCISCO,
        passes_satellites=["ISS"],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        tle_dir = tmp_path / "tle"
        tle_dir.mkdir()
        (tle_dir / "25544.json").write_text(json.dumps([ISS_OMM_FOR_PASSES]))
        service.tle_store = TleStore([tle_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)

        resp = await client.get(
            "/api/passes", params={"days": 1, "min_elevation": 0, "horizon": 0}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "internal"
        assert body["source"] == "internal"
        assert body["error"] is None
        assert len(body["passes"]) >= 1
        assert body["passes"][0]["satellite"] == "ISS"


async def test_passes_falls_back_to_nextpass_when_no_tle_cached(tmp_path):
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", location=_SAN_FRANCISCO,
        passes_satellites=["ISS"],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        # Explicit, guaranteed-empty TLE store (not the host's real
        # cache dirs, which may or may not already have ISS cached).
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        service.tle_store = TleStore([empty_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)
        fake = _FakeNextPassProvider()
        app.state.service.nextpass_provider = fake

        resp = await client.get("/api/passes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "nextpass"
        assert len(fake.calls) == 1


async def test_passes_mixed_tle_coverage_reports_error_for_missing_satellite(tmp_path):
    settings = Settings(
        dwell_seconds=0.05,
        db_path=":memory:",
        location=_SAN_FRANCISCO,
        passes_satellites=["ISS", "CSS"],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        tle_dir = tmp_path / "tle"
        tle_dir.mkdir()
        (tle_dir / "25544.json").write_text(json.dumps([ISS_OMM_FOR_PASSES]))
        service.tle_store = TleStore([tle_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)

        resp = await client.get(
            "/api/passes", params={"days": 1, "min_elevation": 0, "horizon": 0}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["engine"] == "internal"
        assert body["source"] == "internal"
        assert "CSS" in body["error"]
        assert all(p["satellite"] == "ISS" for p in body["passes"])


async def test_patch_settings_extra_satellites_and_visible_only_round_trip():
    async with running_app() as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={
                "passes_extra_satellites": [{"label": "CUSTOM-1", "norad_id": 99999}],
                "passes_visible_only": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["passes_extra_satellites"] == [
            {"label": "CUSTOM-1", "norad_id": 99999}
        ]
        assert resp.json()["passes_visible_only"] is True

        get_resp = await client.get("/api/settings")
        assert get_resp.json()["passes_extra_satellites"] == [
            {"label": "CUSTOM-1", "norad_id": 99999}
        ]


async def test_get_passes_downlink_from_own_satnogs_catalog(tmp_path):
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", location=_SAN_FRANCISCO,
        passes_satellites=["ISS"],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        # Explicit, guaranteed-empty TLE store forces the nextpass fallback
        # engine (see test_passes_falls_back_to_nextpass_when_no_tle_cached)
        # -- downlink enrichment happens the same way regardless of which
        # engine served the pass (AppService._fill_downlinks).
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        service.tle_store = TleStore([empty_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)
        fake = _FakeNextPassProviderWithPass(
            Pass(
                satellite="ISS", norad=25544, rise="2026-01-01T00:00:00Z",
                peak="2026-01-01T00:05:00Z", set="2026-01-01T00:10:00Z",
                max_elevation_deg=45.0, rise_azimuth_deg=10.0, peak_azimuth_deg=90.0,
                set_azimuth_deg=170.0, range_at_peak_km=800.0, window_minutes=10.0,
            )
        )
        service.nextpass_provider = fake
        service.catalog.replace_source(
            "satnogs",
            [
                CatalogEntry(
                    source="satnogs",
                    frequency_hz=145825000,
                    station="NORAD 25544 ISS APRS digipeater",
                    mode="NFM",
                    bandwidth_hz=12500,
                    notes="satnogs Transmitter FM",
                )
            ],
        )

        resp = await client.get("/api/passes")
        assert resp.status_code == 200
        downlink = resp.json()["passes"][0]["downlink"]
        assert downlink["frequency_hz"] == 145825000
        assert downlink["mode"] == "NFM"


class _FakeNextPassProviderWithPass:
    def __init__(self, pass_: Pass):
        self._pass = pass_

    async def passes(self, satellites, **kwargs):
        return PassesResult(
            passes=[self._pass], generated_at="2026-01-01T00:00:00Z", source="fresh"
        )


async def test_passes_ics_route_returns_calendar_with_one_vevent(tmp_path):
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", location=_SAN_FRANCISCO,
        passes_satellites=["ISS"],
    )
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        service.tle_store = TleStore([empty_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)
        service.nextpass_provider = _FakeNextPassProviderWithPass(
            Pass(
                satellite="ISS", norad=25544, rise="2026-01-01T00:00:00Z",
                peak="2026-01-01T00:05:00Z", set="2026-01-01T00:10:00Z",
                max_elevation_deg=45.0, rise_azimuth_deg=10.0, peak_azimuth_deg=90.0,
                set_azimuth_deg=170.0, range_at_peak_km=800.0, window_minutes=10.0,
            )
        )

        resp = await client.get("/api/passes.ics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/calendar")
        body = resp.text
        assert body.startswith("BEGIN:VCALENDAR\r\n")
        assert body.count("BEGIN:VEVENT") == 1
        assert "SUMMARY:ISS max 45°" in body
        assert "DTSTART:20260101T000000Z" in body
        assert "DTEND:20260101T001000Z" in body
        assert body.rstrip("\r\n").endswith("END:VCALENDAR")


def _write_scan_dir(root, name: str, *, with_report: bool) -> None:
    scan_dir = root / name
    scan_dir.mkdir()
    doc = {
        "metadata": {
            "center_frequency_hz": 137900014.0,
            "sample_rate": 500000.0,
            "duration_s": 187.8,
        },
        "settings": {},
        "events": [
            {
                "id": 1,
                "kind": "transient",
                "start_s": 1.0,
                "end_s": 2.0,
                "duration_s": 1.0,
                "bandwidth_hz": 366.0,
                "contrast_db": 22.5,
                "frequency_hz": 137711415.0,
            }
        ],
    }
    (scan_dir / "events.json").write_text(json.dumps(doc))
    if with_report:
        (scan_dir / "report.html").write_text("<html></html>")


async def test_scans_routes_against_temp_root(tmp_path):
    _write_scan_dir(tmp_path, "scan1_20260101-120000-000000", with_report=True)
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))

    async with running_app(settings=settings) as (app, client, adapter):
        listing = await client.get("/api/scans")
        assert listing.status_code == 200
        body = listing.json()
        assert body["root"] == str(tmp_path)
        assert len(body["scans"]) == 1
        scan_id = body["scans"][0]["id"]

        detail = await client.get(f"/api/scans/{scan_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["scan"]["id"] == scan_id
        assert len(detail_body["events"]) == 1

        missing = await client.get("/api/scans/does-not-exist")
        assert missing.status_code == 404


async def test_scan_report_route_404_on_bad_name(tmp_path):
    _write_scan_dir(tmp_path, "scan1_20260101-120000-000000", with_report=True)
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))

    async with running_app(settings=settings) as (app, client, adapter):
        scan_id = (await client.get("/api/scans")).json()["scans"][0]["id"]

        good = await client.get(f"/api/scans/{scan_id}/report/report.html")
        assert good.status_code == 200

        bad = await client.get(f"/api/scans/{scan_id}/report/not-whitelisted.txt")
        assert bad.status_code == 404


async def test_run_scan_route_queues_job_lists_it_and_rejects_duplicate(tmp_path, monkeypatch):
    started = asyncio.Event()

    class _HangingProc:
        returncode = 0

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()  # never completes; test teardown cancels it

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    iq_path = tmp_path / "capture.iq"
    iq_path.write_bytes(b"\x00")
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))

    async with running_app(settings=settings) as (app, client, adapter):
        resp = await client.post("/api/scans/run", json={"path": str(iq_path)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        job_id = body["job_id"]

        await started.wait()

        jobs_resp = await client.get("/api/scans/jobs")
        assert jobs_resp.status_code == 200
        jobs = jobs_resp.json()["jobs"]
        assert jobs[0]["job_id"] == job_id
        assert jobs[0]["status"] == "running"
        assert jobs[0]["path"] == str(iq_path)

        dup = await client.post("/api/scans/run", json={"path": str(iq_path)})
        assert dup.status_code == 409


async def test_run_scan_route_rejects_directory_path(tmp_path):
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))
    async with running_app(settings=settings) as (app, client, adapter):
        resp = await client.post("/api/scans/run", json={"path": str(tmp_path)})
        assert resp.status_code == 400


async def test_run_scan_route_404_when_feature_disabled(tmp_path):
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))
    async with running_app(settings=settings) as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"scans": False}})
        resp = await client.post("/api/scans/run", json={"path": str(tmp_path / "x.iq")})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: scans"

        jobs_resp = await client.get("/api/scans/jobs")
        assert jobs_resp.status_code == 404


async def test_scan_clip_route_whitelist_and_traversal(tmp_path):
    _write_scan_dir(tmp_path, "scan1_20260101-120000-000000", with_report=False)
    channels = tmp_path / "scan1_20260101-120000-000000" / "channels"
    channels.mkdir()
    (channels / "event-01_8000SPS.cf32").write_bytes(b"\x00" * 8)
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))

    async with running_app(settings=settings) as (app, client, adapter):
        scan_id = (await client.get("/api/scans")).json()["scans"][0]["id"]

        good = await client.get(f"/api/scans/{scan_id}/clips/event-01_8000SPS.cf32")
        assert good.status_code == 200

        traversal = await client.get(f"/api/scans/{scan_id}/clips/../report.html")
        assert traversal.status_code == 404

        missing = await client.get(f"/api/scans/{scan_id}/clips/event-99_8000SPS.cf32")
        assert missing.status_code == 404


async def test_run_scan_route_requires_exactly_one_of_path_or_redetect(tmp_path):
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))
    async with running_app(settings=settings) as (app, client, adapter):
        neither = await client.post("/api/scans/run", json={})
        assert neither.status_code == 400

        iq_path = tmp_path / "capture.iq"
        iq_path.write_bytes(b"\x00")
        both = await client.post(
            "/api/scans/run", json={"path": str(iq_path), "redetect": "some-scan"}
        )
        assert both.status_code == 400


async def test_run_scan_route_redetect_requires_cached_spectrum(tmp_path):
    _write_scan_dir(tmp_path, "scan1_20260101-120000-000000", with_report=False)
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", iqscan_scan_root=str(tmp_path))
    async with running_app(settings=settings) as (app, client, adapter):
        resp = await client.post(
            "/api/scans/run", json={"redetect": "scan1_20260101-120000-000000"}
        )
        assert resp.status_code == 400  # no spectrum.npz cached for this scan


# --- sky / Doppler tracking --------------------------------------------

_C_KMS = 299792.458


def _range_rate_for_doppler(doppler_hz: float, f0_hz: float) -> float:
    """Inverse of tracking.py's own doppler formula, for scripting a fake tracker."""
    return -doppler_hz * _C_KMS / f0_hz


class _FakeSkyTracker:
    """Records position()/next_or_current_pass() calls and returns canned results."""

    def __init__(self):
        self.location = None
        self.calls: list[tuple] = []

    def position(self, label, at=None):
        self.calls.append(("position", label))
        if label != "ISS":
            return SkyResult(error=f"unknown satellite: {label!r}")
        pos = SatPosition(
            satellite="ISS",
            norad=25544,
            at="2026-01-01T00:00:00Z",
            azimuth_deg=180.0,
            elevation_deg=45.0,
            range_km=500.0,
            range_rate_kms=-1.0,
            visible=True,
        )
        return SkyResult(position=pos, tle_age_days=1.0)

    def next_or_current_pass(self, label, horizon_deg=0.0, hours=24.0):
        self.calls.append(("next_or_current_pass", label))
        return SkyTrack(
            satellite=label,
            rise="2026-01-01T00:00:00Z",
            peak="2026-01-01T00:05:00Z",
            set="2026-01-01T00:10:00Z",
            max_elevation_deg=45.0,
            points=[],
        )


async def test_get_sky_detail_uses_injected_tracker():
    async with running_app() as (app, client, adapter):
        fake = _FakeSkyTracker()
        app.state.service.tracker = fake

        resp = await client.get("/api/sky/ISS")
        assert resp.status_code == 200
        body = resp.json()
        assert body["position"]["satellite"] == "ISS"
        assert body["position"]["elevation_deg"] == 45.0
        assert body["track"]["max_elevation_deg"] == 45.0
        assert body["error"] is None


async def test_start_track_unknown_signal_returns_404():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/track/does-not-exist")
        assert resp.status_code == 404


async def test_start_track_without_satellite_returns_400():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]
        resp = await client.post(f"/api/track/{signal_id}")
        assert resp.status_code == 400


async def test_doppler_loop_commits_large_shift_skips_small_and_stops_on_manual_tune():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        f0 = 145_800_000
        create_resp = await client.post(
            "/api/signals",
            json={"name": "Sat pass", "frequency_hz": f0, "satellite": "ISS"},
        )
        signal_id = create_resp.json()["id"]

        # Scripted range_rate sequence: first tick shifts the target 300 Hz
        # off f0 (>= doppler_min_step_hz's default 100 -> must be committed),
        # second only 50 Hz further (< 100 -> must NOT be committed).
        rr1 = _range_rate_for_doppler(300.0, f0)
        rr2 = _range_rate_for_doppler(350.0, f0)
        scripted = iter([rr1, rr2])

        class _ScriptedTracker:
            location = None

            def position(self, label, at=None):
                pos = SatPosition(
                    satellite=label,
                    norad=25544,
                    at="2026-01-01T00:00:00Z",
                    azimuth_deg=1.0,
                    elevation_deg=45.0,
                    range_km=500.0,
                    range_rate_kms=next(scripted),
                    visible=True,
                )
                return SkyResult(position=pos)

        service.tracker = _ScriptedTracker()

        resp = await client.post(f"/api/track/{signal_id}")
        assert resp.status_code == 200
        assert resp.json()["satellite"] == "ISS"

        loop = service.doppler_loop
        await loop._tick()
        set_calls = [c for c in adapter.calls if c[0] == "set" and c[1] == "device_vfo_frequency"]
        assert set_calls == [("set", "device_vfo_frequency", f0 + 300)]

        await loop._tick()
        set_calls = [c for c in adapter.calls if c[0] == "set" and c[1] == "device_vfo_frequency"]
        assert set_calls == [("set", "device_vfo_frequency", f0 + 300)]  # unchanged: 50 Hz < step

        assert loop.signal_id == signal_id

        await adapter.external_change("device_vfo_frequency", f0 + 999)
        for _ in range(50):
            if loop.signal_id is None:
                break
            await asyncio.sleep(0.01)
        assert loop.signal_id is None

        state = (await client.get("/api/state")).json()
        assert state["tracking"] is None


class _HorizonTracker:
    """Visibility flipped by the test; every call returns the same geometry."""

    location = None

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible

    def position(self, label, at=None):
        pos = SatPosition(
            satellite=label,
            norad=25544,
            at="2026-01-01T00:00:00Z",
            azimuth_deg=1.0,
            elevation_deg=45.0 if self.visible else -5.0,
            range_km=500.0,
            range_rate_kms=0.0,
            visible=self.visible,
        )
        return SkyResult(position=pos)


def _doppler_tasks() -> list[str]:
    return sorted(
        t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("msdr-doppler")
    )


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)


def _fast_doppler_settings() -> Settings:
    return Settings(dwell_seconds=0.05, db_path=":memory:", doppler_interval_s=0.25)


async def _start_tracking(client, satellite: str = "ISS") -> str:
    signal_id = (
        await client.post(
            "/api/signals",
            json={"name": "Sat pass", "frequency_hz": 145_800_000, "satellite": satellite},
        )
    ).json()["id"]
    resp = await client.post(f"/api/track/{signal_id}")
    assert resp.status_code == 200
    return signal_id


async def test_doppler_loop_below_horizon_leaves_no_task_and_restarts_cleanly():
    """The loop used to stop itself from inside its own task: the self-cancel
    was suppressed, _run kept looping with _task=None, and the next start()
    put a second loop on the same VFO."""
    async with running_app(settings=_fast_doppler_settings()) as (app, client, adapter):
        service = app.state.service
        tracker = _HorizonTracker(visible=True)
        service.tracker = tracker
        signal_id = await _start_tracking(client)
        assert _doppler_tasks() == ["msdr-doppler-loop", "msdr-doppler-watch"]

        tracker.visible = False
        await _wait_until(lambda: service.doppler_loop.status is None)

        assert service.doppler_loop.status is None
        assert _doppler_tasks() == []
        assert (await client.get("/api/state")).json()["tracking"] is None

        tracker.visible = True
        resp = await client.post(f"/api/track/{signal_id}")
        assert resp.status_code == 200
        assert _doppler_tasks() == ["msdr-doppler-loop", "msdr-doppler-watch"]


async def test_external_vfo_change_stops_tracking_without_leaking_tasks():
    async with running_app(settings=_fast_doppler_settings()) as (app, client, adapter):
        service = app.state.service
        service.tracker = _HorizonTracker(visible=True)
        await _start_tracking(client)

        await adapter.external_change("device_vfo_frequency", 145_900_000)
        await _wait_until(lambda: service.doppler_loop.status is None)

        assert service.doppler_loop.status is None
        assert _doppler_tasks() == []


async def test_app_stop_with_active_tracking_leaves_nothing_running():
    async with running_app(settings=_fast_doppler_settings()) as (app, client, adapter):
        app.state.service.tracker = _HorizonTracker(visible=True)
        await _start_tracking(client)
        assert _doppler_tasks() == ["msdr-doppler-loop", "msdr-doppler-watch"]
    # lifespan exit ran AppService.stop()
    assert _doppler_tasks() == []


async def test_tune_stops_tracking_and_tracking_refuses_while_a_restore_runs():
    async with running_app(settings=_fast_doppler_settings()) as (app, client, adapter):
        service = app.state.service
        service.tracker = _HorizonTracker(visible=True)
        signal_id = await _start_tracking(client)

        # An explicit tune wins: the Doppler loop is stopped before the
        # restore pushes history, so the two never drive the VFO together.
        report = (await client.post("/api/tune/frequency", json={"hz": 146_000_000})).json()
        assert report["status"] == "confirmed"
        assert service.doppler_loop.status is None
        assert _doppler_tasks() == []

        # ... and the other way round: no tracking while a restore is in flight.
        adapter.delay_s = 0.2
        tune = asyncio.create_task(client.post("/api/tune/frequency", json={"hz": 147_000_000}))
        await _wait_until(lambda: service._executor.running)
        assert service._executor.running
        resp = await client.post(f"/api/track/{signal_id}")
        assert resp.status_code == 409
        adapter.delay_s = 0.0
        await tune


async def test_patch_settings_doppler_out_of_range_returns_422():
    async with running_app() as (app, client, adapter):
        assert (
            await client.patch("/api/settings", json={"doppler_interval_s": 0.01})
        ).status_code == 422
        assert (
            await client.patch("/api/settings", json={"doppler_min_step_hz": 0})
        ).status_code == 422


async def test_stop_track_returns_stopped_true():
    async with running_app() as (app, client, adapter):
        resp = await client.delete("/api/track")
        assert resp.status_code == 200
        assert resp.json() == {"stopped": True}
async def test_reconnect_with_unreadable_prop_keeps_state_stale_and_refuses():
    """A reconnect read that times out must not leave the previous
    connection's value in place looking confirmed."""
    adapter = FakeAdapter()
    async with running_app(adapter) as (app, client, _):
        assert (await client.get("/api/state")).json()["complete"] is True

        adapter.connected = False
        adapter._emit(ConnectionChanged(False, time.monotonic()))
        await asyncio.sleep(0)

        # Radio came back on antenna C, but msdr can't read that one.
        adapter.state["active_antenna"] = "Antenna C"
        adapter.timeout_get = {"active_antenna"}
        adapter.connected = True
        adapter._emit(ConnectionChanged(True, time.monotonic()))
        await asyncio.sleep(0.05)

        state = (await client.get("/api/state")).json()
        assert state["complete"] is False
        assert (await client.post("/api/capture")).status_code == 503

        # Once it can be read again, the new value lands and control returns.
        adapter.timeout_get = set()
        adapter._emit(ConnectionChanged(True, time.monotonic()))
        await asyncio.sleep(0.05)
        state = (await client.get("/api/state")).json()
        assert state["complete"] is True
        assert state["props"]["active_antenna"] == "Antenna C"
        assert (await client.post("/api/capture")).status_code == 200


async def test_signal_power_bursts_publish_throttled_telemetry():
    """Read-only props never emit StateChanged, so without a telemetry
    publish the browser S-meter freezes -- and a burst must coalesce."""
    adapter = FakeAdapter()
    async with running_app(adapter) as (app, client, _):
        service = app.state.service
        q = service.broadcaster.subscribe()

        for power in (-80.0, -79.0, -78.0, -77.0):
            await adapter.external_change("signal_power", power)
        await asyncio.sleep(0.05)

        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        telemetry = [m for m in msgs if m["type"] == "telemetry"]
        assert len(telemetry) == 1  # four changes coalesced into one publish
        assert telemetry[0]["observation"]["signal_power_db"] == -77.0

        # A change inside the throttle window waits for the next slot.
        await adapter.external_change("signal_power", -70.0)
        await asyncio.sleep(0.05)
        assert q.empty()
        await asyncio.sleep(_TELEMETRY_MIN_INTERVAL_S)
        assert q.qsize() == 1
        assert q.get_nowait()["observation"]["signal_power_db"] == -70.0

        service.broadcaster.unsubscribe(q)


async def test_broadcaster_bounds_queue_and_coalesces_telemetry():
    """Per-client queues used to be plain unbounded asyncio.Queue()s -- a slow
    browser that never drains would accumulate messages (and memory) forever.
    Telemetry is a pure snapshot, so once the queue is full the newest
    reading replaces the oldest instead of the queue growing without limit."""
    b = Broadcaster()
    q = b.subscribe()

    total = _MAX_QUEUE_SIZE + 1000
    for i in range(total):
        await b.publish({"type": "telemetry", "i": i})

    assert q.qsize() == _MAX_QUEUE_SIZE  # bounded, not `total`
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    assert len(msgs) == _MAX_QUEUE_SIZE
    assert msgs[-1]["i"] == total - 1  # newest survives
    assert q.dropped == 0  # every overflow was coalesced away, nothing lost

    b.unsubscribe(q)


async def test_broadcaster_resyncs_when_noncoalescable_overflow_is_dropped():
    """A discrete one-off event (e.g. a decoder event) has no "newest wins"
    semantics. If the full queue holds no coalescable entry to evict, the
    new arrival is dropped and counted -- but the queue is flagged so the
    client's *next* delivered message is a resync marker, ahead of whatever
    was already queued, telling the browser to reload instead of silently
    missing the event forever."""
    b = Broadcaster()
    q = b.subscribe()

    for i in range(_MAX_QUEUE_SIZE):
        await b.publish({"type": "journal", "i": i})
    assert q.qsize() == _MAX_QUEUE_SIZE

    await b.publish({"type": "decoder_event", "i": "overflow"})
    assert q.qsize() == _MAX_QUEUE_SIZE
    assert q.dropped == 1
    assert q.resync_required

    # The resync marker pre-empts delivery of the stale, already-queued
    # messages -- the client is told to reload before it resumes seeing
    # anything else.
    first = await next_outgoing(q)
    assert first == {"type": "resync", "reason": "events_dropped", "dropped": 1}
    assert not q.resync_required
    assert q.dropped == 0

    second = await next_outgoing(q)
    assert second == {"type": "journal", "i": 0}  # oldest queued entry, untouched

    b.unsubscribe(q)


async def test_broadcaster_evicts_coalescable_entry_to_save_noncoalescable_overflow():
    """If the full queue holds a coalescable snapshot entry (state, telemetry,
    ...), a non-coalescable overflow evicts the oldest of those to make room
    instead of being dropped -- a stale status snapshot is safe to lose, an
    event the client would never see again is not."""
    b = Broadcaster()
    q = b.subscribe()

    await b.publish({"type": "telemetry", "i": "snapshot"})
    for i in range(_MAX_QUEUE_SIZE - 1):
        await b.publish({"type": "journal", "i": i})
    assert q.qsize() == _MAX_QUEUE_SIZE

    await b.publish({"type": "decoder_event", "i": "saved"})
    assert q.qsize() == _MAX_QUEUE_SIZE
    assert q.dropped == 0
    assert not q.resync_required

    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    assert {"type": "telemetry", "i": "snapshot"} not in msgs  # evicted to make room
    assert msgs[-1] == {"type": "decoder_event", "i": "saved"}  # delivered, not dropped

    b.unsubscribe(q)


async def test_broadcaster_disconnects_a_persistently_full_slow_consumer(monkeypatch):
    """A client stuck full for too many consecutive publishes is marked slow
    so /ws can close its socket and let it reconnect with a clean queue."""
    monkeypatch.setattr(service_module, "_SLOW_CONSUMER_FULL_STREAK", 5)
    monkeypatch.setattr(service_module, "_SLOW_CONSUMER_FULL_SECONDS", 9999.0)
    b = Broadcaster()
    q = b.subscribe()

    for _ in range(_MAX_QUEUE_SIZE):
        await b.publish({"type": "telemetry"})
    assert not q.slow  # merely full isn't slow yet

    for _ in range(5):
        await b.publish({"type": "telemetry"})
    assert q.slow

    b.unsubscribe(q)


async def test_patch_profile_null_clears_optional_fields():
    async with running_app() as (app, client, adapter):
        created = (
            await client.post(
                "/api/profiles",
                json={"name": "HF", "antenna": "Antenna A", "native_profile_name": "SW"},
            )
        ).json()

        resp = await client.patch(
            f"/api/profiles/{created['id']}",
            json={"antenna": None, "native_profile_name": None},
        )
        assert resp.status_code == 200
        assert resp.json()["antenna"] is None
        assert resp.json()["native_profile_name"] is None

        # Omitting a field still leaves it alone (exclude_unset).
        resp = await client.patch(f"/api/profiles/{created['id']}", json={"description": "hf"})
        assert resp.json()["name"] == "HF"


async def test_palette_and_nearby_find_a_match_past_the_page_limit():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        conn = service.db.conn
        with conn:
            conn.executemany(
                "INSERT INTO signals (id, name, status, frequency_hz, heard_count, "
                "created_at, updated_at) VALUES (?, ?, 'unknown', ?, 0, ?, '')",
                [
                    (f"bulk-{i:05d}", f"Filler {i}", 100_000_000 + i * 100, f"2024-01-01T{i:05d}")
                    for i in range(1001)
                ],
            )
        needle = (
            await client.post(
                "/api/signals",
                json={"name": "Needle", "frequency_hz": 145_800_000, "demodulator": "NFM"},
            )
        ).json()

        results = (await client.get("/api/palette", params={"q": "needle"})).json()
        assert [r["id"] for r in results if r["kind"] == "signal"] == [needle["id"]]

        state = (await client.get("/api/state")).json()
        assert [s["id"] for s in state["nearby"]] == [needle["id"]]
        assert state["tuned_signal_id"] == needle["id"]


async def test_bad_input_is_422_or_409_never_500():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]

        # name is not nullable on the model: clearing it is a client error.
        resp = await client.patch(f"/api/signals/{signal_id}", json={"name": None})
        assert resp.status_code == 422
        # A negative frequency is not a valid signal.
        resp = await client.post("/api/signals", json={"name": "x", "frequency_hz": -1})
        assert resp.status_code == 422
        resp = await client.post(
            "/api/signals", json={"name": "x", "frequency_hz": 1000, "filter_bandwidth_hz": 0}
        )
        assert resp.status_code == 422
        # ensure_categories() already seeded "fav": a duplicate is a conflict.
        assert (await client.post("/api/tags", json={"name": "fav"})).status_code == 409
        # A merged patch that fails model validation is still client input.
        resp = await client.patch(f"/api/signals/{signal_id}", json={"status": None})
        assert resp.status_code == 422

        # And the happy paths still work.
        resp = await client.patch(f"/api/signals/{signal_id}", json={"name": "ok"})
        assert resp.status_code == 200
        assert (await client.post("/api/tags", json={"name": "brand new"})).status_code == 200


async def test_interrupted_tune_still_records_where_the_receiver_ended_up():
    """A tune that aborts can still have moved the radio; without a history
    entry for where it landed, Back returns None and the operator is stuck."""
    adapter = FakeAdapter()
    adapter.delay_s = 0.1  # the centre step is still in flight when we interfere
    async with running_app(adapter) as (app, client, _):
        service = app.state.service
        assert service.history.can_back() is False

        task = asyncio.create_task(service.tune_frequency(150_000_000))
        await asyncio.sleep(0.02)
        await adapter.external_change("device_vfo_frequency", 149_000_000)
        report = await task

        assert report.status == "interrupted"
        assert service.history.entries[-1].vfo_hz == 149_000_000
        assert service.history.can_back() is True


# --- timezone ---------------------------------------------------------------


async def test_state_timezone_defaults_to_auto_detected_name_and_offset():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        tz = resp.json()["timezone"]
        assert tz["auto"] is True
        assert isinstance(tz["name"], str) and tz["name"]
        assert isinstance(tz["utc_offset_minutes"], int)


async def test_patch_settings_timezone_round_trips_and_reports_offset():
    async with running_app() as (app, client, adapter):
        patch_resp = await client.patch("/api/settings", json={"timezone": "Europe/Rome"})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["timezone"] == "Europe/Rome"

        get_resp = await client.get("/api/state")
        tz = get_resp.json()["timezone"]
        # Computed via ZoneInfo directly rather than duplicating the
        # service's own offset math -- both resolve "now" against the same
        # tzdata rules, so this is exact short of an actual DST-transition
        # instant race.
        expected_offset = int(
            datetime.now(ZoneInfo("Europe/Rome")).utcoffset().total_seconds() // 60
        )
        assert tz == {
            "name": "Europe/Rome",
            "auto": False,
            "utc_offset_minutes": expected_offset,
        }


async def test_patch_settings_timezone_unknown_name_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"timezone": "Nowhere/Nope"})
        assert resp.status_code == 422


def test_detect_timezone_returns_a_string_without_etc_localtime(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    name = detect_timezone()
    assert isinstance(name, str) and name


async def test_api_timezones_contains_utc_and_europe_rome():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/timezones")
        assert resp.status_code == 200
        names = resp.json()
        assert "UTC" in names
        assert "Europe/Rome" in names
        assert names == sorted(names)


async def test_effective_location_readiness_includes_location_file(tmp_path):
    location_file = tmp_path / "location.json"
    location_file.write_text(json.dumps({"lat": 37.0, "lon": -122.0, "timezone": "UTC"}))
    settings = Settings(db_path=":memory:", location_file=str(location_file))
    async with running_app(settings=settings) as (app, client, _):
        assert app.state.service.settings.location is None
        assert (await client.get("/api/state")).json()["location_ready"] is True
        assert (await client.get("/api/sky")).json()["location"]["lat"] == 37.0


async def test_effective_location_readiness_without_location(tmp_path):
    settings = Settings(db_path=":memory:", location_file=str(tmp_path / "missing.json"))
    async with running_app(settings=settings) as (_, client, _):
        assert (await client.get("/api/state")).json()["location_ready"] is False


async def test_selected_pass_chart_uses_requested_interval():
    from datetime import datetime

    from msdr.providers.tracking import SkyTrack

    async with running_app() as (app, client, adapter):
        fake = _FakeSkyTracker()
        calls = []
        def track(label, start, end):
            calls.append((label, start, end))
            return SkyTrack(satellite=label, rise=start.isoformat(), peak=start.isoformat(),
                            set=end.isoformat(), max_elevation_deg=89, points=[])
        fake.track = track
        app.state.service.tracker = fake
        start, end = "2026-10-03T17:50:12-07:00", "2026-10-03T17:56:57-07:00"
        response = await client.get("/api/sky/ISS", params={"start": start, "end": end})
        assert response.status_code == 200
        assert response.json()["track"]["rise"] == start
        assert calls == [("ISS", datetime.fromisoformat(start), datetime.fromisoformat(end))]
        assert not any(call[0] == "next_or_current_pass" for call in fake.calls)
        for params in ({"start": start}, {"start": end, "end": start},
                       {"start": "2026-10-03T17:50:12", "end": end},
                       {"start": start, "end": "2026-10-06T17:56:57-07:00"}):
            assert (await client.get("/api/sky/ISS", params=params)).status_code == 422


async def test_visibility_annotation_loaded_with_filter_off(monkeypatch):
    async with running_app() as (app, client, adapter):
        sentinel = object()
        monkeypatch.setattr("msdr.api.services.sky.load_cached_ephemeris", lambda path: sentinel)
        app.state.service.settings.passes_visible_only = False
        assert app.state.service._load_visible_ephemeris() is sentinel


async def test_concurrent_runtime_toggles_start_each_engine_once():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        for engine, start_name, stop_name in (
            ("decoder", "_start_decoder_runtime", "_stop_decoder_runtime"),
            ("survey", "_start_survey_runtime", "_stop_survey_runtime"),
            ("beacons", "_start_beacons_runtime", "_stop_beacons_runtime"),
        ):
            await getattr(service, stop_name)()
            target = getattr(service, engine)
            starts = 0
            real_start = target.start

            async def slow_start(real_start=real_start):
                nonlocal starts
                starts += 1
                await asyncio.sleep(0.02)
                await real_start()

            target.start = slow_start
            await asyncio.gather(getattr(service, start_name)(), getattr(service, start_name)())
            assert starts == 1, f"{engine}: two concurrent starts both ran"
            assert getattr(service, f"_{engine}_running") is True
            await getattr(service, stop_name)()
            assert getattr(service, f"_{engine}_running") is False


async def test_concurrent_restores_while_doppler_stop_awaits_second_gets_restore_running():
    async with running_app() as (app, client, _adapter):
        service = app.state.service

        async def slow_stop():
            await asyncio.sleep(0.05)  # both callers pass the running check first

        service.doppler_loop.stop = slow_stop
        pushed = []
        real_push = service.history.push
        service.history.push = lambda state: (pushed.append(state), real_push(state))[1]
        results = await asyncio.gather(
            service.tune_frequency(146_000_000),
            service.tune_frequency(147_000_000),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 1 and type(errors[0]).__name__ == "RestoreRunning"
        # Pre-restore snapshot only from the winner (plus at most its post-restore one).
        assert len(pushed) <= 2
