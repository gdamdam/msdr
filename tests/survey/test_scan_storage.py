"""Durability, compaction and bounded capture regression tests."""

import asyncio
import time

import pytest

from msdr.storage.db import Database
from msdr.survey.bandscan import BandScan
from msdr.survey.scan_storage import BandScanRepo


@pytest.mark.asyncio
async def test_append_compacts_unchanged_samples_but_keeps_evidence_changes(tmp_path):
    db = Database(tmp_path / "scan.db")
    db.connect()
    repo = BandScanRepo(db)
    repo.create("s1", {"start_hz": 100, "end_hz": 200, "centers_hz": [150]})
    span = {
        "center_hz": 150,
        "start_hz": 100,
        "end_hz": 200,
        "state": "covered",
        "visited": True,
        "spectrum_captured": True,
        "dsp_completed": False,
        "usable_intervals_hz": [[100, 200]],
    }
    repo.checkpoint("s1", "running", {"span_count": 1, "covered_count": 1}, span, 0)
    started = time.time()
    samples = [
        {
            "frequency_hz": 150,
            "span_center_hz": 150,
            "observed_at": started + n / 100,
            "last_seen": started + n / 100,
            "snr_db": n % 20 - 10,
            "activity": {"kind": "steady", "duty": n / 1000},
            "family": {"name": "FM"},
        }
        for n in range(1000)
    ]
    for group in range(0, 1000, 50):
        await repo.aappend_observations("s1", samples[group : group + 50])
    changed = {**samples[-1], "observed_at": started + 11, "family": {"name": "FSK"}}
    await repo.aappend_observations("s1", [changed])
    repo.checkpoint(
        "s1",
        "complete",
        {"span_count": 1, "covered_count": 1, "result_count": 1, "coverage_complete": True},
        span,
        0,
    )
    await repo.aflush()
    summary = await repo.asummary("s1")
    assert summary["observation_count"] == 1001
    row = summary["payload"]["results"][0]
    assert row["observation_count"] == 1001
    assert row["snr_min_db"] == -10
    assert row["snr_max_db"] == 9
    assert set(row["identifications"]) == {"FM", "FSK"}
    page = await repo.aobservations("s1", limit=10, frequency_hz=150)
    assert page["total"] == 2
    assert sum(o["sample_count"] for o in page["observations"]) == 1001
    assert page["observations"][0]["family"]["name"] == "FSK"
    assert page["observations"][1]["sample_count"] == 1000
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_background_writer_preserves_events_under_queue_pressure(tmp_path):
    db = Database(tmp_path / "pressure.db")
    db.connect()
    repo = BandScanRepo(db, queue_size=1)
    repo.create("s2", {"start_hz": 100, "end_hz": 300, "centers_hz": [150, 250]})
    original = repo._apply

    def slow(conn, kind, args):
        time.sleep(0.003)
        original(conn, kind, args)

    repo._apply = slow
    for index in range(2):
        span = {
            "center_hz": 150 + 100 * index,
            "start_hz": 100 + 100 * index,
            "end_hz": 200 + 100 * index,
            "state": "covered",
            "visited": True,
            "spectrum_captured": True,
            "usable_intervals_hz": [[100 + 100 * index, 200 + 100 * index]],
        }
        for _ in range(20):
            repo.checkpoint(
                "s2", "running", {"span_count": index + 1, "covered_count": index + 1}, span, index
            )
        await repo.aappend_observations(
            "s2",
            [
                {
                    "frequency_hz": 150 + 100 * index,
                    "span_center_hz": 150 + 100 * index,
                    "observed_at": time.time(),
                    "snr_db": index,
                    "family": {"name": f"kind-{index}"},
                }
            ],
        )
        await repo.aflush()  # terminal span persisted before the next starts
    repo.checkpoint(
        "s2", "complete", {"span_count": 2, "covered_count": 2, "result_count": 2}, span, 1
    )
    await repo.aflush()
    summary = repo.summary("s2")
    assert len(summary["payload"]["spans"]) == 2
    assert summary["observation_count"] == 2
    assert summary["state"] == "complete"
    assert repo.degraded_reason is not None
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_recovery_marks_incomplete_capture_explicitly(tmp_path):
    path = tmp_path / "crash.db"
    db = Database(path)
    db.connect()
    repo = BandScanRepo(db)
    repo.create("s3", {"start_hz": 100, "end_hz": 300, "centers_hz": [150, 250]})
    repo.checkpoint(
        "s3",
        "running",
        {"span_count": 1, "coverage_complete": False},
        {
            "center_hz": 150,
            "start_hz": 100,
            "end_hz": 200,
            "state": "dwelling",
            "visited": True,
            "spectrum_captured": False,
        },
        0,
    )
    await repo.aflush()
    await repo.aclose()
    db.close()
    db = Database(path)
    db.connect()
    repo = BandScanRepo(db)
    repo.interrupt_unfinished()
    summary = repo.summary("s3")
    assert summary["state"] == "interrupted"
    assert summary["payload"]["coverage_complete"] is False
    assert summary["payload"]["spans"][0]["state"] == "uncovered"
    assert "restarted" in summary["payload"]["spans"][0]["reason"]
    assert repo.latest()[0]["uncovered_count"] == 1
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_capture_marks_visited_spectrum_dsp_and_holes_separately(tmp_path):
    db = Database(tmp_path / "capture.db")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    sequence = [0]

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        return True

    def snapshot():
        sequence[0] += 1
        center, span, _ = confirmed[0]
        return {
            "frame_seq": sequence[0],
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": span,
            "generation": 1,
            "usable_spectrum": sequence[0],
            "dsp_completed": 0,
            "dsp_targets": 1,
            "dsp_analyzed": 0,
            "dsp_pending": 1,
            "spectrum": {
                "bins": [-70.0] * 512,
                "center_hz": center,
                "span_hz": span,
                "at": time.time(),
                "center_exclusion_bins": 2,
            },
        }

    scan = BandScan(repo, tune, lambda: confirmed[0], lambda: [], snapshot=snapshot)
    started = scan.start(100_000_000, 102_000_000, 2_000_000, 0.15, 0)
    await scan._task
    summary = await repo.asummary(started["current"]["id"])
    span = summary["payload"]["spans"][0]
    assert span["state"] == "covered"
    assert span["visited"] and span["spectrum_captured"]
    assert span["dsp_completed"] is False and span["dsp_pending"] == 1
    assert len(span["usable_intervals_hz"]) == 2  # DC exclusion remains a hole
    assert summary["payload"]["coverage_complete"] is False
    spectrum = await repo.aspectrum(started["current"]["id"])
    assert spectrum["available"] and spectrum["holes"]
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_normal_stop_flushes_findings_before_returning(tmp_path):
    db = Database(tmp_path / "normal-stop.db")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    frame = [0]
    observed = asyncio.Event()

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        return True

    def snapshot():
        frame[0] += 1
        center, span, _ = confirmed[0]
        return {
            "frame_seq": frame[0],
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": span,
        }

    def labels():
        observed.set()
        return [
            {
                "frequency_hz": 101_000_000,
                "last_seen": time.time(),
                "snr_db": 12,
                "family": {"name": "FM"},
            }
        ]

    scan = BandScan(repo, tune, lambda: confirmed[0], labels, snapshot=snapshot)
    started = scan.start(100_000_000, 102_000_000, 2_000_000, 20, 0)
    await asyncio.wait_for(observed.wait(), 2)
    stopped = await scan.stop()
    saved = await repo.asummary(started["current"]["id"])
    assert stopped["state"] == saved["state"] == "stopped"
    assert saved["observation_count"] >= 1
    assert saved["payload"]["spans"][0]["state"] == "uncovered"
    assert saved["payload"]["coverage_complete"] is False
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_long_incremental_capture_keeps_event_loop_responsive(tmp_path):
    db = Database(tmp_path / "long.db")
    db.connect()
    repo = BandScanRepo(db)
    repo.create("long", {"start_hz": 100, "end_hz": 300, "centers_hz": [150]})
    ticks = []
    done = asyncio.Event()

    async def heartbeat():
        while not done.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    task = asyncio.create_task(heartbeat())
    started = time.time()
    for batch in range(100):
        await repo.aappend_observations(
            "long",
            [
                {
                    "frequency_hz": 100 + (i % 100),
                    "span_center_hz": 150,
                    "observed_at": started + (batch * 100 + i) / 1000,
                    "last_seen": started + (batch * 100 + i) / 1000,
                    "snr_db": (i % 30) - 15,
                    "family": {"name": "FM"},
                }
                for i in range(100)
            ],
        )
    await repo.aflush()
    done.set()
    await task
    summary = await repo.asummary("long")
    assert summary["observation_count"] == 10_000
    assert len(ticks) >= 5
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.2
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_same_span_generation_reset_cannot_count_as_covered(tmp_path):
    db = Database(tmp_path / "generation.db")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    snapshots = [0]

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        return True

    def snapshot():
        snapshots[0] += 1
        center, span, _ = confirmed[0]
        return {
            "frame_seq": snapshots[0],
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": span,
            "generation": 1 if snapshots[0] == 1 else 2,
            "usable_spectrum": snapshots[0],
            "dsp_completed": 0,
            "spectrum": {
                "bins": [-50.0] * 512,
                "center_hz": center,
                "span_hz": span,
                "at": time.time(),
            },
        }

    scan = BandScan(repo, tune, lambda: confirmed[0], lambda: [], snapshot=snapshot)
    started = scan.start(100_000_000, 102_000_000, 2_000_000, 0.01, 0)
    await scan._task
    summary = await repo.asummary(started["current"]["id"])
    span = summary["payload"]["spans"][0]
    assert span["visited"]
    assert span["state"] == "uncovered"
    assert "generation changed" in span["reason"]
    assert not span["spectrum_captured"]
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_first_fresh_frame_labels_survive_short_dwell(tmp_path):
    db = Database(tmp_path / "first-frame.db")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    calls = [0]

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        return True

    def snapshot():
        calls[0] += 1
        center, span, _ = confirmed[0]
        return {
            "frame_seq": 0 if calls[0] == 1 else 1,
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": span,
        }

    def labels():
        return [
            {
                "frequency_hz": 101_000_000,
                "last_seen": time.time(),
                "snr_db": 7,
                "family": {"name": "FM"},
            }
        ]

    scan = BandScan(repo, tune, lambda: confirmed[0], labels, snapshot=snapshot)
    started = scan.start(100_000_000, 102_000_000, 2_000_000, 0.01, 0)
    await scan._task
    summary = await repo.asummary(started["current"]["id"])
    assert summary["payload"]["spans"][0]["state"] == "covered"
    assert summary["payload"]["spans"][0]["fresh_frames"] == 1
    assert summary["observation_count"] == 1
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_degraded_reason_resets_when_a_new_scan_starts(tmp_path):
    db = Database(tmp_path / "degrade-reset.db")
    db.connect()
    repo = BandScanRepo(db, queue_size=1)
    repo.create("a", {"start_hz": 100, "end_hz": 200, "centers_hz": [150]})
    original = repo._apply

    def slow(conn, kind, args):
        time.sleep(0.003)
        original(conn, kind, args)

    repo._apply = slow
    span = {
        "center_hz": 150,
        "start_hz": 100,
        "end_hz": 200,
        "state": "covered",
        "visited": True,
        "spectrum_captured": True,
        "usable_intervals_hz": [[100, 200]],
    }
    for _ in range(20):
        repo.checkpoint("a", "running", {"span_count": 1, "covered_count": 1}, span, 0)
    await repo.aflush()
    assert repo.degraded_reason is not None  # scan A degraded under queue pressure

    repo._apply = original
    repo.create("b", {"start_hz": 200, "end_hz": 300, "centers_hz": [250]})
    assert repo.degraded_reason is None  # a fresh scan must not inherit A's degradation
    await repo.aclose()
    db.close()


@pytest.mark.asyncio
async def test_checkpoint_skips_unchanged_spectrum_payload(tmp_path):
    db = Database(tmp_path / "spectrum-skip.db")
    db.connect()
    repo = BandScanRepo(db)
    repo.create("sx", {"start_hz": 100, "end_hz": 200, "centers_hz": [150]})
    received = []
    original = repo._apply

    def spy(conn, kind, args):
        if kind == "checkpoint":
            received.append(args[-1])
        original(conn, kind, args)

    repo._apply = spy
    spectrum = {"available": True, "captured_frames": 3}
    repo.checkpoint("sx", "running", {"span_count": 1}, None, None, spectrum)
    await repo.aflush()
    repo.checkpoint("sx", "running", {"span_count": 1}, None, None, dict(spectrum))
    await repo.aflush()
    assert received[0] is not None
    assert received[1] is None  # same captured_frames: writer gets no spectrum

    spectrum_next = {"available": True, "captured_frames": 4}
    repo.checkpoint("sx", "running", {"span_count": 1}, None, None, spectrum_next)
    await repo.aflush()
    assert received[2] is not None  # a new frame count is always persisted

    await repo.aclose()
    db.close()
