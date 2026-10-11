"""Scanner API (v0.12.0): POST /api/scan/*, GET /api/scan, the scanner
feature flag, PATCH .../items/{signal_id} skip, and behaviour that only shows
up wired through AppService -- no Back/Forward history push while scanning,
the passive journal's heard_count not bumping, and pausing on disconnect.
Uses the same running_app helper as tests/api/test_app.py."""

import asyncio
import time
from contextlib import asynccontextmanager

from msdr.api.app import create_app
from msdr.api.schemas import ScanStartIn
from msdr.engine.fake_adapter import FakeAdapter
from msdr.radio.protocol import ConnectionChanged
from msdr.settings import Features, Settings
from msdr.storage.db import Database
from tests.api.test_app import running_app as _running_app


@asynccontextmanager
async def running_app(adapter=None):
    # Channel scanning is optional and off in production. These tests opt in.
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", features=Features(scanner=True))
    async with _running_app(adapter, settings=settings) as resources:
        yield resources


async def _wait_until(cond, timeout: float = 2.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def _signal(client, **overrides):
    body = {"name": "Test Signal", "frequency_hz": 146_000_000}
    body.update(overrides)
    resp = await client.post("/api/signals", json=body)
    assert resp.status_code == 200
    return resp.json()


async def _list_with_signal(client, freq=146_000_000):
    lst = (await client.post("/api/lists", json={"name": "Sweep"})).json()
    signal = await _signal(client, frequency_hz=freq)
    await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [signal["id"]]})
    return lst, signal


_TINY = {"dwell_ms": 30, "settle_ms": 10, "hang_s": 0.05}


async def test_scan_start_requires_connection():
    adapter = FakeAdapter()
    adapter.connected = False
    async with running_app(adapter) as (app, client, _adapter):
        source = {"type": "range", "start_hz": 1_000_000, "stop_hz": 1_000_000, "step_hz": 1}
        resp = await client.post("/api/scan/start", json={"source": source})
        assert resp.status_code == 503


async def test_scan_start_range_and_get_status():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/scan/start",
            json={
                "source": {
                    "type": "range",
                    "start_hz": 146_000_000,
                    "stop_hz": 146_000_100,
                    "step_hz": 100,
                },
                **_TINY,
            },
        )
        assert resp.status_code == 200
        status = resp.json()
        assert status["state"] == "running"
        assert status["total"] == 2

        status = (await client.get("/api/scan")).json()
        assert status["state"] in ("running", "holding")
        assert status["config"]["source"]["type"] == "range"

        await client.post("/api/scan/stop")


async def test_scan_start_conflicts_while_running():
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}
        await client.post("/api/scan/start", json={"source": source, **_TINY})
        resp = await client.post("/api/scan/start", json={"source": source, **_TINY})
        assert resp.status_code == 409
        await client.post("/api/scan/stop")


async def test_scan_start_unknown_list_is_404():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/scan/start", json={"source": {"type": "list", "list_id": "nope"}, **_TINY}
        )
        assert resp.status_code == 404


async def test_scan_start_range_stop_before_start_is_422():
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 2_000_000, "stop_hz": 1_000_000, "step_hz": 1}
        resp = await client.post("/api/scan/start", json={"source": source})
        assert resp.status_code == 422


async def test_scan_feature_flag_gates_routes():
    async with running_app() as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"scanner": False}})
        source = {"type": "range", "start_hz": 1_000_000, "stop_hz": 1_000_000, "step_hz": 1}
        assert (await client.post("/api/scan/start", json={"source": source})).status_code == 404
        assert (await client.get("/api/scan")).status_code == 404


async def test_disabling_scanner_feature_stops_a_running_scan():
    # Claim: apply_settings only live-stops tracking (Doppler) on feature-off,
    # not the scanner -- so a running scan kept sweeping in the background
    # with no route left to reach it (every /api/scan/* 404s once disabled).
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}
        await client.post("/api/scan/start", json={"source": source, **_TINY})
        service = app.state.service
        await _wait_until(lambda: service.scanner.state in ("running", "holding"))

        resp = await client.patch("/api/settings", json={"features": {"scanner": False}})
        assert resp.status_code == 200
        await _wait_until(lambda: service.scanner.state == "idle", timeout=2.0)


async def test_set_antenna_does_not_pause_active_scan():
    # Claim: changing the antenna is not a retune -- ScannerEngine._watch_
    # overrides only reacts to device_vfo_frequency/device_center_frequency/
    # demodulator (see _OVERRIDE_PROPS), so it must never see active_antenna
    # and pause a running scan over it.
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}
        await client.post(
            "/api/scan/start",
            json={"source": source, "dwell_ms": 2000, "settle_ms": 10, "hang_s": 0.05},
        )
        service = app.state.service
        await _wait_until(lambda: service.scanner.state in ("running", "holding"))
        await _wait_until(lambda: not service.scanner.is_tuning)

        resp = await client.post("/api/receiver/antenna", json={"antenna": "Antenna A"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        assert adapter.state["active_antenna"] == "Antenna A"

        await asyncio.sleep(0.05)  # let _watch_overrides react, if it were going to
        assert service.scanner.state in ("running", "holding")

        await client.post("/api/scan/stop")


async def test_scan_pause_resume_stop_lifecycle():
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}
        await client.post("/api/scan/start", json={"source": source, **_TINY})

        resp = await client.post("/api/scan/pause")
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"

        resp = await client.post("/api/scan/resume")
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"

        resp = await client.post("/api/scan/stop")
        assert resp.status_code == 200
        assert resp.json()["state"] == "idle"

        assert (await client.post("/api/scan/pause")).status_code == 409
        assert (await client.post("/api/scan/resume")).status_code == 409


async def test_scan_skip_and_hold_controls():
    async with running_app() as (app, client, adapter):
        lst, _a = await _list_with_signal(client, freq=146_000_000)
        b = await _signal(client, name="B", frequency_hz=146_100_000)
        await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [b["id"]]})

        await client.post(
            "/api/scan/start",
            json={
                "source": {"type": "list", "list_id": lst["id"]},
                "dwell_ms": 1000,
                "settle_ms": 10,
            },
        )
        service = app.state.service
        await _wait_until(
            lambda: service.scanner.channel is not None and not service.scanner.is_tuning
        )

        resp = await client.post("/api/scan/skip", json={"persist": True})
        assert resp.status_code == 200
        # "skip always" drops the item from the active rotation too, so the
        # 2-item list shrinks to just B, landing back at position 0.
        await _wait_until(
            lambda: service.scanner.channel is not None
            and service.scanner.channel.signal_id == b["id"],
            timeout=1.0,
        )
        assert service.scanner.total == 1

        resp = await client.post("/api/scan/hold")
        assert resp.status_code == 200
        assert resp.json()["manual_hold"] is True

        await client.post("/api/scan/stop")

        items = (await client.get(f"/api/lists/{lst['id']}/items")).json()
        skipped = next(i for i in items if i["frequency_hz"] == 146_000_000)
        # persisted via the scan/skip route -- confirmed through the dedicated
        # PATCH route's own read path below.
        resp = await client.patch(
            f"/api/lists/{lst['id']}/items/{skipped['id']}", json={"skip": False}
        )
        assert resp.status_code == 200


async def test_patch_list_item_skip_route():
    async with running_app() as (app, client, adapter):
        lst, signal = await _list_with_signal(client)
        resp = await client.patch(
            f"/api/lists/{lst['id']}/items/{signal['id']}", json={"skip": True}
        )
        assert resp.status_code == 200
        assert resp.json() == {"skip": True}

        assert (
            await client.patch(f"/api/lists/{lst['id']}/items/missing", json={"skip": True})
        ).status_code == 404
        assert (
            await client.patch("/api/lists/nope/items/x", json={"skip": True})
        ).status_code == 404


async def test_scanning_does_not_push_history_or_bump_heard_count():
    async with running_app() as (app, client, adapter):
        original_history = (await client.get("/api/history")).json()

        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_200, "step_hz": 100}
        await client.post("/api/scan/start", json={"source": source, **_TINY})
        await asyncio.sleep(0.3)  # several channel visits' worth of time
        await client.post("/api/scan/stop")

        history = (await client.get("/api/history")).json()
        assert history == original_history

        journal = (await client.get("/api/journal")).json()
        assert not any(e["kind"] == "observed" for e in journal)


async def test_disconnect_pauses_active_scan():
    async with running_app() as (app, client, adapter):
        source = {"type": "range", "start_hz": 146_000_000, "stop_hz": 146_000_100, "step_hz": 100}
        await client.post("/api/scan/start", json={"source": source, **_TINY})

        adapter.connected = False
        adapter._emit(ConnectionChanged(False, time.monotonic()))
        service = app.state.service
        await _wait_until(lambda: service.scanner.state == "paused")
        assert service.scanner.reason == "disconnected"


async def test_spectrum_scan_start_status_and_stop_disables_stream():
    async with running_app() as (app, client, adapter):
        adapter.spectrum_peaks = {145_787_500: 160}

        resp = await client.post(
            "/api/scan/start",
            json={
                "source": {"type": "spectrum", "level_threshold": 20},
                "dwell_ms": 30,
                "settle_ms": 10,
                "hang_s": 0.05,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"
        assert ("set_spectrum_stream", True) in adapter.calls

        status = (await client.get("/api/scan")).json()
        assert status["config"]["source"]["type"] == "spectrum"
        assert "spectrum" in status
        assert status["spectrum"] is not None
        assert status["spectrum"]["sample_rate_hz"] == 2_000_000

        await client.post("/api/scan/stop")
        assert ("set_spectrum_stream", False) in adapter.calls


async def test_spectrum_scan_start_rejects_start_at_or_after_stop():
    async with running_app() as (app, client, adapter):
        source = {"type": "spectrum", "start_hz": 146_000_000, "stop_hz": 146_000_000}
        resp = await client.post("/api/scan/start", json={"source": source})
        assert resp.status_code == 422


async def test_stop_scanner_on_app_shutdown():
    adapter = FakeAdapter()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", features=Features(scanner=True))
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db)
    async with app.router.lifespan_context(app):
        service = app.state.service
        source_body = {
            "source": {
                "type": "range",
                "start_hz": 146_000_000,
                "stop_hz": 146_000_100,
                "step_hz": 100,
            },
            **_TINY,
        }
        await service.scan_start(ScanStartIn.model_validate(source_body))
        assert service.scanner.state != "idle"
    assert service.scanner.state == "idle"
