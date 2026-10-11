import io
import zipfile

from tests.api.test_app import running_app


async def test_backup_restore_and_evidence_download(tmp_path):
    async with running_app() as (app, client, adapter):
        app.state.service.settings.recorder_root = str(tmp_path)
        response = await client.post(
            "/api/signals", json={"name": "Saved", "frequency_hz": 100000000}
        )
        signal_id = response.json()["id"]
        evidence = await client.get(f"/api/signals/{signal_id}/evidence-bundle")
        assert evidence.status_code == 200, evidence.text
        assert zipfile.is_zipfile(io.BytesIO(evidence.content))
        backup = await client.get("/api/backup")
        assert backup.status_code == 200
        assert zipfile.is_zipfile(io.BytesIO(backup.content))
        await client.delete(f"/api/signals/{signal_id}")
        restored = await client.post("/api/backup/restore", content=backup.content)
        assert restored.status_code == 200, restored.text
        assert restored.json()["verified"] is True
        assert app.state.service.db.conn.execute(
            "SELECT name FROM signals WHERE id=?", (signal_id,)
        ).fetchone()[0] == "Saved"
        invalid = await client.post("/api/backup/restore", content=b"not a zip")
        assert invalid.status_code == 422
        assert app.state.service.db.conn.execute(
            "SELECT name FROM signals WHERE id=?", (signal_id,)
        ).fetchone()[0] == "Saved"


async def test_comparison_route_is_not_captured_as_scan_id():
    async with running_app() as (app, client, adapter):
        repo = app.state.service.band_scan.repo
        for id in ("before", "after"):
            repo.create(id, {"start_hz": 100, "end_hz": 200})
            repo.save(id, "complete", {"spans": [], "results": []})
        response = await client.get("/api/survey/band-scan/compare?before=before&after=after")
        assert response.status_code == 200, response.text
        assert response.json()["new"] == []
