import concurrent.futures as cf
import json
from concurrent.futures.process import BrokenProcessPool

import pytest

from msdr.storage.db import Database
from msdr.survey.scan_report_reader import ScanReportReader
from msdr.survey.scan_storage import BandScanRepo


@pytest.mark.asyncio
async def test_isolated_legacy_reader_preserves_all_history_and_invalidates_cache(tmp_path):
    db = Database(tmp_path / "scans.db")
    db.connect()
    original = {
        "spans": [{"center_hz": 100, "state": "covered"}],
        "results": [
            {
                "frequency_hz": 100,
                "observations": [
                    {"observed_at": 1, "snr_db": None, "family": {"name": "early"}},
                    {"observed_at": 3, "snr_db": 10},
                    {"observed_at": 5, "snr_db": 2},
                ],
            },
            {"frequency_hz": 200, "observations": [{"observed_at": 4, "snr_db": 0}]},
        ],
    }
    raw = json.dumps(original, indent=2)
    db.conn.execute(
        "INSERT INTO survey_band_scans(id,config_json,state,payload_json,started_at,updated_at) "
        "VALUES('legacy','{}','stopped',?,1,1)",
        (raw,),
    )
    db.conn.commit()
    reader = ScanReportReader(db.path)
    try:
        summary = await reader.read("summary", "legacy")
        assert summary["compact"] and summary["legacy"]
        assert summary["observation_count"] == 4
        assert summary["payload"]["results"][0]["peak_snr_db"] == 10
        assert "early" in summary["payload"]["results"][0]["identifications"]
        page = await reader.read("observations", "legacy", offset=1, limit=2)
        assert [o["observed_at"] for o in page["observations"]] == [4, 3]
        filtered = await reader.read("observations", "legacy", offset=0, limit=25, frequency_hz=200)
        assert filtered["total"] == 1
        assert filtered["observations"][0]["snr_db"] == 0
        thresholded = await reader.read(
            "observations", "legacy", offset=0, limit=2, min_peak_snr_db=5,
        )
        assert thresholded["total"] == 3
        assert len(thresholded["observations"]) == 2
        assert all(o["frequency_hz"] == 100 for o in thresholded["observations"])
        exported = await reader.read("export", "legacy")
        assert raw.encode() in exported
        assert json.loads(exported)["payload"] == original
        assert (await reader.read("get", "legacy"))["payload"] == original
        original["results"][0]["observations"].append({"observed_at": 6, "snr_db": 20})
        db.conn.execute(
            "UPDATE survey_band_scans SET payload_json=?,updated_at=2 WHERE id='legacy'",
            (json.dumps(original),),
        )
        db.conn.commit()
        assert (await reader.read("summary", "legacy"))["observation_count"] == 5
        assert (await reader.read("observations", "legacy", offset=0, limit=1))["observations"][0][
            "observed_at"
        ] == 6
        assert await reader.read("summary", "missing") is None
    finally:
        await reader.aclose()
        db.close()


@pytest.mark.asyncio
async def test_legacy_identity_matches_between_repo_and_reader(tmp_path):
    # BandScanRepo._summary and ScanReportReader._legacy_summary must agree on
    # identity extraction for legacy payloads; an RDS-only sample is the case
    # the reader's own (now removed) duplicate logic used to miss.
    db = Database(tmp_path / "legacy-identity.db")
    db.connect()
    payload = {
        "spans": [],
        "results": [
            {
                "frequency_hz": 100,
                "observations": [{"observed_at": 1, "snr_db": 5, "rds": {"ps": "KROQ"}}],
            }
        ],
    }
    db.conn.execute(
        "INSERT INTO survey_band_scans(id,config_json,state,payload_json,started_at,updated_at) "
        "VALUES('legacy-id','{}','stopped',?,1,1)",
        (json.dumps(payload),),
    )
    db.conn.commit()

    repo = BandScanRepo(db, start_writer=False)
    from_repo = repo.summary("legacy-id")
    # Call the worker function directly rather than through ScanReportReader's
    # ProcessPoolExecutor: it is the same code the pool invokes in the spawned
    # worker, and this sandbox cannot create real OS semaphores for a pool.
    from msdr.survey.scan_report_reader import _read

    from_reader = _read(db.path, "summary", "legacy-id", {})

    identifications = from_repo["payload"]["results"][0]["identifications"]
    assert identifications == ["KROQ"]
    assert from_reader["payload"]["results"][0]["identifications"] == identifications
    db.close()


@pytest.mark.asyncio
async def test_read_recreates_pool_after_broken_process_pool(tmp_path, monkeypatch):
    db = Database(tmp_path / "broken-pool.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO survey_band_scans(id,config_json,state,payload_json,started_at,updated_at) "
        "VALUES('s1','{}','stopped','{}',1,1)"
    )
    db.conn.commit()
    reader = ScanReportReader(db.path)

    class FakePool:
        instances = []

        def __init__(self, *args, **kwargs):
            FakePool.instances.append(self)

        def submit(self, fn, *args):
            future = cf.Future()
            if len(FakePool.instances) == 1:
                future.set_exception(BrokenProcessPool("worker died"))
            else:
                try:
                    future.set_result(fn(*args))
                except Exception as exc:  # pragma: no cover - surfaced via the future
                    future.set_exception(exc)
            return future

        def shutdown(self, wait=True, cancel_futures=False):
            pass

    monkeypatch.setattr("msdr.survey.scan_report_reader.ProcessPoolExecutor", FakePool)
    try:
        summary = await reader.read("summary", "s1")
        assert summary["id"] == "s1"
        assert len(FakePool.instances) == 2  # the broken pool was replaced, not reused
    finally:
        await reader.aclose()
        db.close()
