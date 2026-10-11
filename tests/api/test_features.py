"""Feature flags: Settings.features gates routes and live behaviour."""

import asyncio

from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from tests.api.test_app import running_app


async def test_state_payload_includes_features_all_on_by_default():
    async with running_app() as (app, client, adapter):
        state = (await client.get("/api/state")).json()
        features = state["features"]
        assert features["passes"] is True
        assert features["ident"] is True
        assert features["catalog"] is True
        assert features["journal_passive"] is True
        assert features["tracking"] is True


async def test_patch_features_passes_off_gates_route_and_reflects_in_state():
    async with running_app() as (app, client, adapter):
        patch_resp = await client.patch("/api/settings", json={"features": {"passes": False}})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["features"]["passes"] is False
        # Other flags untouched by the partial patch.
        assert patch_resp.json()["features"]["ident"] is True

        resp = await client.get("/api/passes")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: passes"

        state = (await client.get("/api/state")).json()
        assert state["features"]["passes"] is False


async def test_patch_features_ident_off_gates_identify_routes():
    async with running_app() as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"ident": False}})

        resp = await client.get("/api/identify", params={"hz": 162_550_000})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: ident"


async def test_patch_features_sky_off_gates_sky_routes():
    async with running_app() as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"sky": False}})

        resp = await client.get("/api/sky")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: sky"


async def test_patch_features_scans_off_gates_scans_route():
    async with running_app() as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"scans": False}})

        resp = await client.get("/api/scans")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: scans"


async def test_patch_features_nearby_off_empties_nearby_and_tuned_signal():
    async with running_app() as (app, client, adapter):
        create_resp = await client.post(
            "/api/signals", json={"name": "ISS voice", "frequency_hz": 145_800_000}
        )
        assert create_resp.status_code == 200

        state = (await client.get("/api/state")).json()
        assert state["nearby"]  # nearby is on by default -- sanity check

        await client.patch("/api/settings", json={"features": {"nearby": False}})
        state = (await client.get("/api/state")).json()
        assert state["nearby"] == []
        assert state["tuned_signal_id"] is None


async def test_patch_features_tracking_off_refuses_start_tracking():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]
        await client.patch(f"/api/signals/{signal_id}", json={"satellite": "ISS"})
        await client.patch("/api/settings", json={"features": {"tracking": False}})

        resp = await client.post(f"/api/track/{signal_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: tracking"


async def test_patch_features_tracking_off_stops_active_tracking():
    async with running_app() as (app, client, adapter):
        signal_id = (await client.post("/api/capture")).json()["id"]
        await client.patch(f"/api/signals/{signal_id}", json={"satellite": "ISS"})

        start_resp = await client.post(f"/api/track/{signal_id}")
        assert start_resp.status_code == 200
        assert (await client.get("/api/state")).json()["tracking"] is not None

        await client.patch("/api/settings", json={"features": {"tracking": False}})
        assert (await client.get("/api/state")).json()["tracking"] is None


async def test_journal_passive_off_at_startup_never_starts_dwell():
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    settings.features.journal_passive = False
    async with running_app(settings=settings) as (app, client, adapter):
        assert app.state.service._dwell_running is False


async def test_journal_passive_toggle_stops_and_restarts_dwell():
    async with running_app() as (app, client, adapter):
        service = app.state.service
        assert service._dwell_running is True

        resp = await client.patch("/api/settings", json={"features": {"journal_passive": False}})
        assert resp.status_code == 200
        assert service._dwell_running is False

        resp = await client.patch("/api/settings", json={"features": {"journal_passive": True}})
        assert resp.status_code == 200
        assert service._dwell_running is True


async def test_telemetry_flag_off_stops_the_s_meter_publishes():
    adapter = FakeAdapter()
    async with running_app(adapter) as (app, client, _):
        await client.patch("/api/settings", json={"features": {"telemetry": False}})
        service = app.state.service
        q = service.broadcaster.subscribe()

        await adapter.external_change("signal_power", -70.0)
        await asyncio.sleep(0.05)
        assert [m for m in _drain(q) if m["type"] == "telemetry"] == []

        await client.patch("/api/settings", json={"features": {"telemetry": True}})
        await adapter.external_change("signal_power", -60.0)
        await asyncio.sleep(0.05)
        assert [m for m in _drain(q) if m["type"] == "telemetry"]

        service.broadcaster.unsubscribe(q)


def _drain(q):
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    return msgs
