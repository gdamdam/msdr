import asyncio
import sqlite3
import threading

import pytest

from msdr.storage.db import Database
from msdr.survey.scan_storage import BandScanRepo, ScanWriterError


@pytest.mark.asyncio
async def test_failed_writer_startup_does_not_hang_flush_or_shutdown(tmp_path, monkeypatch):
    db = Database(tmp_path / "writer.db")
    db.connect()
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect

    def fail_writer(*args, **kwargs):
        if threading.current_thread().name == "msdr-scan-writer":
            entered.set()
            release.wait(2)
            raise sqlite3.OperationalError("disk unavailable")
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_writer)
    repo = BandScanRepo(db)
    assert await asyncio.to_thread(entered.wait, 1)
    flush = asyncio.create_task(repo.aflush())
    await asyncio.sleep(0.02)
    release.set()
    with pytest.raises(ScanWriterError, match="disk unavailable"):
        await asyncio.wait_for(flush, 2)
    with pytest.raises(ScanWriterError, match="disk unavailable"):
        await asyncio.wait_for(repo.aclose(), 2)
    assert repo._thread is None
    db.close()


@pytest.mark.asyncio
async def test_duplicate_snapshot_is_not_counted_as_another_measurement(tmp_path):
    db = Database(tmp_path / "dedup.db")
    db.connect()
    repo = BandScanRepo(db)
    repo.create("dedup", {})
    await repo.aappend_observations(
        "dedup",
        [
            {
                "frequency_hz": 100,
                "observed_at": now,
                "last_seen": 1,
                "snr_db": 0,
                "activity": {"kind": "quiet", "duration_s": now},
            }
            for now in range(1, 10)
        ],
    )
    await repo.aappend_observations(
        "dedup",
        [
            {
                "frequency_hz": 100,
                "observed_at": 11,
                "last_seen": 1,
                "snr_db": 0,
                "activity": {"kind": "quiet"},
                "family": {"name": "new evidence"},
            }
        ],
    )
    await repo.aflush()
    summary = repo.summary("dedup")
    assert summary["observation_count"] == 2
    assert repo.observations("dedup")["total"] == 2
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_failed_writer_counts_already_queued_writes_as_dropped(tmp_path, monkeypatch):
    db = Database(tmp_path / "writer.db")
    db.connect()
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect

    def fail_writer(*args, **kwargs):
        if threading.current_thread().name == "msdr-scan-writer":
            entered.set()
            release.wait(2)
            raise sqlite3.OperationalError("disk unavailable")
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_writer)
    repo = BandScanRepo(db)
    assert await asyncio.to_thread(entered.wait, 1)
    # Accepted before the writer failed, so the caller believes it is queued.
    repo.append_observations("scan", [{"frequency_hz": 100, "observed_at": 1, "snr_db": 0}])
    release.set()
    with pytest.raises(ScanWriterError, match="disk unavailable"):
        await asyncio.wait_for(repo.aclose(), 2)
    assert repo.dropped_writes == 1
    assert "1 queued write(s) dropped" in repo.degraded_reason
    db.close()
