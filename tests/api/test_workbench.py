"""Signal workbench HTTP wiring and spectrum-watch lifecycle."""

import asyncio

import pytest

from tests.api.test_app import running_app


@pytest.mark.asyncio
async def test_watch_status_and_stop_routes():
    frequency = 145_800_000 + 300_000
    async with running_app() as (app, client, adapter):
        adapter.spectrum_peaks[frequency] = 180
        started = await client.post(
            "/api/workbench/watch",
            json={"frequencies_hz": [frequency], "threshold_db": 10,
                  "stop_threshold_db": 6},
        )
        assert started.status_code == 200, started.text
        assert started.json()["active"] is True
        assert started.json()["watch"]["decode_concurrency"] == 1
        assert (await client.get("/api/state")).json()["owner"]["kind"] == "manual"

        for _ in range(30):
            status = (await client.get("/api/workbench")).json()
            if status["fresh"]:
                break
            await asyncio.sleep(0.05)
        assert status["fresh"] is True
        assert status["watch"]["measurements"][0]["covered"] is True

        stopped = await client.post("/api/workbench/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["active"] is False
        assert (await client.get("/api/state")).json()["owner"]["kind"] == "manual"


@pytest.mark.asyncio
async def test_watch_rejects_invalid_hysteresis_over_http():
    async with running_app() as (_app, client, _adapter):
        response = await client.post(
            "/api/workbench/watch",
            json={"frequencies_hz": [145_800_000], "threshold_db": 10,
                  "stop_threshold_db": 10},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_cycling_watch_reports_receiver_ownership_and_owner_stop():
    async with running_app() as (_app, client, adapter):
        response = await client.post(
            "/api/workbench/watch",
            json={"frequencies_hz": [145_500_000, 155_500_000],
                  "cycle_groups": True, "dwell_s": 1},
        )
        assert response.status_code == 200, response.text
        owner = (await client.get("/api/state")).json()["owner"]
        assert owner["kind"] == "watch"
        stopped = await client.post("/api/owner/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["kind"] == "manual"
        assert adapter.connected


@pytest.mark.asyncio
async def test_stop_watch_keeps_live_inspection_and_status_reports_span():
    """Stopping the watch alone must not end a live inspection started
    separately; status exposes the confirmed span for coverage previews."""
    frequency = 145_800_000 + 300_000
    async with running_app() as (app, client, adapter):
        status = (await client.get("/api/workbench")).json()
        assert status["receiver"] == {"connected": True, "started": True, "complete": True}
        assert status["center_hz"] and status["sample_rate_hz"]

        live = await client.post("/api/inspection/live/start", json={
            "frequency_hz": frequency, "demodulator": "NFM", "bandwidth_hz": 12500})
        assert live.status_code == 200, live.text
        started = await client.post("/api/workbench/watch", json={"frequencies_hz": [frequency]})
        assert started.status_code == 200, started.text

        stopped = await client.post("/api/workbench/watch/stop")
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert body["active"] is False
        assert body["live_receiver"] is not None, "live inspection must survive a watch-only stop"

        everything = (await client.post("/api/workbench/stop")).json()
        assert everything["live_receiver"] is None
