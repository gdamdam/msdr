"""Survey band-scan API wiring using only FakeAdapter and FakeSurvey."""

import contextlib

import httpx
import pytest

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import FakeSurvey


@contextlib.asynccontextmanager
async def running_app():
    adapter = FakeAdapter()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db, survey=FakeSurvey())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, adapter


@pytest.mark.asyncio
async def test_start_stop_and_saved_detail_routes():
    async with running_app() as (client, adapter):
        response = await client.post(
            "/api/survey/band-scan/start",
            json={
                "start_hz": 100_000_000,
                "end_hz": 101_000_000,
                "span_hz": 2_000_000,
                "dwell_s": 0.25,
                "overlap": 0.25,
            },
        )
        assert response.status_code == 200, response.text
        scan_id = response.json()["current"]["id"]
        assert response.json()["state"] == "running"
        status = await client.get("/api/survey/band-scan")
        assert status.json()["current"]["state"] in {"running", "complete", "failed"}, status.text
        if status.json()["current"]["state"] == "failed":
            detail = await client.get(f"/api/survey/band-scan/{scan_id}")
            pytest.fail(detail.json()["payload"].get("failure_reason", "scan failed"))

        stopped = await client.post("/api/survey/band-scan/stop")
        assert stopped.status_code == 200, stopped.text
        detail = await client.get(f"/api/survey/band-scan/{scan_id}")
        assert detail.status_code == 200
        assert detail.json()["state"] in {"stopped", "complete"}
        assert detail.json()["payload"]["spans"][0]["state"] == "uncovered"
        assert detail.json()["payload"]["spans"][0]["reason"]
        assert adapter.state["device_center_frequency"] == 100_500_000
        assert adapter.state["device_vfo_frequency"] == 100_500_000


@pytest.mark.asyncio
async def test_start_rejects_invalid_frequency_range():
    async with running_app() as (client, _adapter):
        response = await client.post(
            "/api/survey/band-scan/start",
            json={
                "start_hz": 101_000_000,
                "end_hz": 100_000_000,
                "span_hz": 2_000_000,
                "dwell_s": 1,
                "overlap": 0.25,
            },
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_compact_report_routes_and_pagination_validation():
    async with running_app() as (client, _adapter):
        response = await client.post(
            "/api/survey/band-scan/start",
            json={
                "start_hz": 100_000_000,
                "end_hz": 101_000_000,
                "span_hz": 2_000_000,
                "dwell_s": 0.25,
                "overlap": 0.25,
            },
        )
        scan_id = response.json()["current"]["id"]
        await client.post("/api/survey/band-scan/stop")
        base = f"/api/survey/band-scan/{scan_id}"
        summary = await client.get(base + "/summary")
        assert summary.status_code == 200, summary.text
        assert summary.json()["compact"] is True
        page = await client.get(base + "/observations?offset=0&limit=25")
        assert page.status_code == 200, page.text
        assert page.json()["offset"] == 0
        assert len(page.json()["observations"]) <= 25
        spectrum = await client.get(base + "/spectrum")
        assert spectrum.status_code == 200, spectrum.text
        exported = await client.get(base + "/export")
        assert exported.status_code == 200, exported.text
        assert "attachment" in exported.headers["content-disposition"]
        assert exported.json()["id"] == scan_id
        for query in ("offset=-1", "limit=0", "limit=501", "frequency_hz=-1"):
            assert (await client.get(base + "/observations?" + query)).status_code == 422
        for suffix in ("summary", "observations", "spectrum", "export"):
            assert (await client.get("/api/survey/band-scan/missing/" + suffix)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_retune_boundary_wraps_restore_including_failure(failure):
    from types import SimpleNamespace

    from msdr.api.service import AppService

    calls = []
    props = dict(FakeAdapter().state)
    confirmed = SimpleNamespace(props=props, api_version="1.0.3")
    survey = SimpleNamespace(
        begin_retune=lambda: calls.append("begin"),
        end_retune=lambda center, rate: calls.append(("end", center, rate)),
    )

    async def restore(*args, **kwargs):
        assert calls == ["begin"]
        calls.append("restore")
        confirmed.props["device_center_frequency"] = 150_000_000
        if failure:
            raise ConnectionError("lost receiver")
        return SimpleNamespace(status="confirmed")

    service = SimpleNamespace(confirmed=confirmed, survey=survey, _restore=restore)
    if failure:
        with pytest.raises(ConnectionError):
            await AppService._band_scan_tune_span(service, 2_000_000, 150_000_000)
    else:
        assert await AppService._band_scan_tune_span(service, 2_000_000, 150_000_000)
    assert calls == ["begin", "restore", ("end", 150_000_000, 2_000_000)]


@pytest.mark.asyncio
async def test_delete_one_or_all_reports_protects_active_scans():
    async with running_app() as (client, _adapter):
        repo = client._transport.app.state.service.band_scan.repo
        for scan_id in ("old-a", "old-b", "active"):
            repo.create(scan_id, {})
        repo.save("old-a", "complete", {"results": [], "spans": []})
        repo.save("old-b", "stopped", {"results": [], "spans": []})
        repo.append_observations("old-a", [{"frequency_hz": 100, "observed_at": 1, "snr_db": 10}])
        assert (await client.delete("/api/survey/band-scan/active")).status_code == 409
        assert (await client.delete("/api/survey/band-scan")).status_code == 409
        assert len(repo.latest()) == 3  # bulk deletion is atomic on refusal
        deleted = await client.delete("/api/survey/band-scan/old-a")
        assert deleted.json() == {"deleted": 1}
        assert repo.get("old-a") is None
        assert (
            repo.db.conn.execute("SELECT count(*) FROM survey_scan_observations").fetchone()[0] == 0
        )
        assert repo.db.conn.execute("SELECT count(*) FROM survey_scan_results").fetchone()[0] == 0
        repo.save("active", "stopped", {"results": [], "spans": []})
        assert (await client.delete("/api/survey/band-scan")).json() == {"deleted": 2}
        assert repo.latest() == []
        assert (await client.delete("/api/survey/band-scan/missing")).status_code == 404


@pytest.mark.asyncio
async def test_observation_snr_filter_applies_before_pagination():
    async with running_app() as (client, _adapter):
        repo = client._transport.app.state.service.band_scan.repo
        repo.create("levels", {})
        repo.append_observations(
            "levels",
            [
                {"frequency_hz": 100, "observed_at": 1, "snr_db": 15},
                {
                    "frequency_hz": 100,
                    "observed_at": 3,
                    "snr_db": 1,
                    "family": {"name": "new evidence"},
                },
                {"frequency_hz": 200, "observed_at": 2, "snr_db": 3},
            ],
        )
        response = await client.get(
            "/api/survey/band-scan/levels/observations?min_peak_snr_db=10&limit=1"
        )
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert response.json()["observations"][0]["frequency_hz"] == 100
        assert response.json()["observations"][0]["snr_db"] == 1
        assert (
            await client.get("/api/survey/band-scan/levels/observations?min_peak_snr_db=NaN")
        ).status_code == 422
