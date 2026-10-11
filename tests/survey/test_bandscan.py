import asyncio
import time

import pytest

from msdr.storage.db import Database
from msdr.survey.bandscan import BandScan, BandScanError, BandScanRepo, make_centers


def test_make_centers_cover_range_with_configured_overlap():
    centers = make_centers(100, 510, 200, 0.5)
    assert centers == [200, 300, 400, 410]
    covered = [(c - 100, c + 100) for c in centers]
    assert covered[0][0] <= 100
    assert covered[-1][1] >= 510
    assert all(covered[i][1] >= covered[i + 1][0] for i in range(len(covered) - 1))


@pytest.mark.asyncio
async def test_failed_confirmation_is_uncovered_and_repeated_labels_aggregate():
    db = Database(":memory:")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    calls = 0
    frames = [0]

    async def tune(span, center):
        nonlocal calls
        calls += 1
        if calls == 1:
            confirmed[0] = (center, span, center)
            return True
        confirmed[0] = (center - 1, span, center)
        return True

    def get_labels():
        frames[0] += 1
        return [
            {
                "frequency_hz": 101_000_000,
                "snr_db": 12.0,
                "bw_class": "nfm",
                "last_seen": time.time(),
            }
        ]

    def snapshot():
        frames[0] += 1
        center, rate, _vfo = confirmed[0] or (None, None, None)
        return {
            "frame_seq": frames[0],
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": rate,
        }

    scan = BandScan(repo, tune, lambda: confirmed[0], get_labels, snapshot=snapshot)
    result = scan.start(100_000_000, 103_000_000, 2_000_000, 0.25, 0.5)
    await scan._task

    saved = repo.get(result["current"]["id"])
    payload = saved["payload"]
    assert saved["state"] == "complete"
    assert payload["spans"][0]["state"] == "covered"
    assert payload["spans"][1]["state"] == "uncovered"
    assert len(payload["results"]) == 1
    assert len(payload["results"][0]["span_centers_hz"]) == 1
    db.close()


@pytest.mark.asyncio
async def test_pause_resume_stop_and_history_survive_restart():
    db = Database(":memory:")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    entered = asyncio.Event()
    frames = [0]

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        entered.set()
        return True

    def snapshot():
        frames[0] += 1
        center, rate, _vfo = confirmed[0] or (None, None, None)
        return {
            "frame_seq": frames[0],
            "connected": True,
            "survey_center_hz": center,
            "survey_sample_rate_hz": rate,
        }

    scan = BandScan(repo, tune, lambda: confirmed[0], lambda: [], snapshot=snapshot)
    started = scan.start(100_000_000, 104_000_000, 2_000_000, 20, 0.25)
    await entered.wait()
    assert scan.pause()["state"] == "paused"
    assert scan.resume()["state"] == "running"
    stopped = await scan.stop()
    assert stopped["state"] == "stopped"
    saved = repo.get(started["current"]["id"])
    assert all(span["state"] != "covered" for span in saved["payload"]["spans"])

    restored = BandScan(BandScanRepo(db), tune, lambda: confirmed[0], lambda: [], snapshot=snapshot)
    history = restored.status()["history"]
    assert history[0]["id"] == started["current"]["id"]
    assert history[0]["state"] == "stopped"
    db.close()


def test_result_dedup_preserves_adjacent_channels_and_all_observations():
    db = Database(":memory:")
    db.connect()
    scan = BandScan(BandScanRepo(db), lambda *_: True, lambda: (0, 0, 0), lambda: [])
    scan.current = {
        "config": {"start_hz": 100_000_000, "end_hz": 101_000_000, "span_hz": 2_000_000},
        "results": [],
    }
    span = {
        "center_hz": 100_500_000,
        "start_hz": 99_500_000,
        "end_hz": 101_500_000,
        "capture_started_at": time.time() - 1,
    }
    for _ in range(125):
        scan._merge_labels(
            span,
            [{"frequency_hz": 100_000_000, "snr_db": 10, "last_seen": time.time()}],
            scan.current["config"],
        )
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_010_000, "snr_db": 11, "last_seen": time.time()}],
        scan.current["config"],
    )
    assert [row["frequency_hz"] for row in scan.current["results"]] == [100_000_000, 100_010_000]
    assert len(scan.current["results"][0]["observations"]) == 125
    db.close()


def test_representative_sample_keeps_zero_snr_over_weaker_negative_snr():
    db = Database(":memory:")
    db.connect()
    scan = BandScan(BandScanRepo(db), lambda *_: True, lambda: (0, 0, 0), lambda: [])
    scan.current = {
        "id": "scan-1",
        "config": {"start_hz": 100_000_000, "end_hz": 101_000_000, "span_hz": 2_000_000},
        "results": [],
    }
    span = {
        "center_hz": 100_500_000,
        "start_hz": 99_500_000,
        "end_hz": 101_500_000,
        "capture_started_at": time.time() - 1,
    }
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_000_000, "snr_db": 0.0, "last_seen": time.time()}],
        scan.current["config"],
    )
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_000_000, "snr_db": -4.0, "last_seen": time.time()}],
        scan.current["config"],
    )
    reps = scan.current["results"][0]["observations"]
    assert reps[0]["snr_db"] == 0.0
    db.close()


def test_label_within_tolerance_merges_but_outside_tolerance_is_new_result():
    db = Database(":memory:")
    db.connect()
    scan = BandScan(BandScanRepo(db), lambda *_: True, lambda: (0, 0, 0), lambda: [])
    scan.current = {
        "config": {"start_hz": 100_000_000, "end_hz": 101_000_000, "span_hz": 2_000_000},
        "results": [],
    }
    span = {
        "center_hz": 100_500_000,
        "start_hz": 99_500_000,
        "end_hz": 101_500_000,
        "capture_started_at": time.time() - 1,
    }
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_000_000, "snr_db": 10, "last_seen": time.time()}],
        scan.current["config"],
    )
    # tolerance here is span_hz / 1024 = ~1953 Hz; 1500 Hz off is within it but
    # not an exact _result_index key, so this must hit the bucketed fallback.
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_001_500, "snr_db": 11, "last_seen": time.time()}],
        scan.current["config"],
    )
    assert len(scan.current["results"]) == 1

    # 5000 Hz off is outside tolerance: a distinct result.
    scan._merge_labels(
        span,
        [{"frequency_hz": 100_005_000, "snr_db": 9, "last_seen": time.time()}],
        scan.current["config"],
    )
    assert len(scan.current["results"]) == 2
    db.close()


def test_center_count_is_bounded():
    with pytest.raises(BandScanError, match="more than 5000 spans"):
        make_centers(1, 1_000_000, 10, 0.5)


def test_stale_and_out_of_span_labels_are_not_recorded():
    db = Database(":memory:")
    db.connect()
    scan = BandScan(BandScanRepo(db), lambda *_: True, lambda: (0, 0, 0), lambda: [])
    scan.current = {
        "config": {"start_hz": 100, "end_hz": 200, "span_hz": 100},
        "results": [],
    }
    span = {
        "center_hz": 150,
        "start_hz": 100,
        "end_hz": 200,
        "capture_started_at": time.time(),
    }
    scan._merge_labels(
        span,
        [
            {"frequency_hz": 150, "last_seen": span["capture_started_at"] - 1},
            {"frequency_hz": 250, "last_seen": time.time()},
        ],
        scan.current["config"],
    )
    assert scan.current["results"] == []
    db.close()


@pytest.mark.asyncio
async def test_survey_processed_span_must_match_confirmed_receiver_span():
    db = Database(":memory:")
    db.connect()
    repo = BandScanRepo(db)
    confirmed = [None]
    frames = [0]

    async def tune(span, center):
        confirmed[0] = (center, span, center)
        return True

    def snapshot():
        frames[0] += 1
        center, rate, _vfo = confirmed[0]
        return {
            "frame_seq": frames[0],
            "connected": True,
            "survey_center_hz": center + 1,
            "survey_sample_rate_hz": rate,
        }

    scan = BandScan(repo, tune, lambda: confirmed[0], lambda: [], snapshot=snapshot)
    started = scan.start(100_000_000, 101_000_000, 2_000_000, 0.01, 0.25)
    await scan._task
    saved = repo.get(started["current"]["id"])
    span = saved["payload"]["spans"][0]
    assert span["state"] == "uncovered"
    assert "fresh frame at a different span" in span["reason"]
    db.close()
