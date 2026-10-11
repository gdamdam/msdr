import asyncio
import contextlib
import gc
import json
import warnings
from collections import namedtuple
from pathlib import Path

import pytest

from msdr.recorder import Recorder, RecorderConfig, RecorderError
from tests.recorder.conftest import FakeClock, make_frame_bytes

_DiskUsage = namedtuple("_DiskUsage", "total used free")

# Recorders that reached "armed" during a test: an armed-but-never-disarmed
# Recorder keeps its reader/writer tasks (and any open segment file) alive via
# a reference cycle, so it only gets reclaimed whenever the cyclic GC happens
# to run -- surfacing as a ResourceWarning on an unrelated, later test. The
# autouse fixture below stops every recorder this registers so none carry
# over.
_ARMED_RECORDERS: list[Recorder] = []


@pytest.fixture(autouse=True)
async def _stop_armed_recorders():
    yield
    while _ARMED_RECORDERS:
        rec = _ARMED_RECORDERS.pop()
        with contextlib.suppress(Exception):
            await rec.stop()


async def _armed(config, stream, clock=None, sample_rate=1000, center_hz=100_000_000) -> Recorder:
    kwargs = {"clock": clock} if clock is not None else {}
    rec = Recorder(config, stream=stream, **kwargs)
    await rec.start()
    rec.on_property("connected", True)
    rec.on_property("started", True)
    rec.on_property("device_sample_rate", sample_rate)
    rec.on_property("device_center_frequency", center_hz)
    await rec.arm(True)
    _ARMED_RECORDERS.append(rec)
    return rec


async def _await_finalize(rec: Recorder, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if rec.status()["state"] != "finalizing":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("finalize did not complete in time")


# -- arm() guards ---------------------------------------------------------


async def test_arm_refused_when_not_started(config, stream):
    rec = Recorder(config, stream=stream)
    await rec.start()
    rec.on_property("connected", True)
    with pytest.raises(RecorderError, match="not started"):
        await rec.arm(True)
    assert rec.status()["state"] == "off"


async def test_arm_refused_low_disk(tmp_path, stream, monkeypatch):
    from msdr.recorder import recorder as recorder_mod

    monkeypatch.setattr(
        recorder_mod.shutil, "disk_usage", lambda path: _DiskUsage(100, 90, 10 * 1e9)
    )
    config = RecorderConfig(root=tmp_path, min_free_gb=20)
    rec = Recorder(config, stream=stream)
    await rec.start()
    rec.on_property("connected", True)
    rec.on_property("started", True)
    with pytest.raises(RecorderError, match="low disk"):
        await rec.arm(True)
    assert rec.status()["state"] == "off"


# -- pre-roll ring behaviour via the recorder ------------------------------


async def test_preroll_evicts_beyond_configured_length(tmp_path, stream):
    clock = FakeClock()
    config = RecorderConfig(root=tmp_path, preroll_s=0.2, min_free_gb=0, segment_s=0.05)
    rec = await _armed(config, stream, clock=clock)

    for _ in range(10):
        stream.push(n_samples=50)  # 50 samples @ 1000 Hz == 0.05s per frame
        await asyncio.sleep(0.01)
        clock.advance(0.06)  # forces the writer to rotate (past segment_s)
    await asyncio.sleep(0.02)

    buffered_s = rec.status()["buffered_s"]
    assert 0 < buffered_s <= 0.3  # bounded near preroll_s, not growing to 10 * 0.05s


# -- record() promotion --------------------------------------------------


async def test_record_promotes_preroll_and_appends_live_frames(tmp_path, stream):
    clock = FakeClock()
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=0.05)
    rec = await _armed(config, stream, clock=clock)

    for _ in range(3):
        stream.push(n_samples=50)
        await asyncio.sleep(0.01)
        clock.advance(0.06)
    await asyncio.sleep(0.02)

    preroll_samples_before = sum(m.samples for m in rec._preroll_ring.closed_segments())
    assert preroll_samples_before > 0

    status = await rec.record()
    assert status["state"] == "recording"

    for _ in range(2):
        stream.push(n_samples=50)
        await asyncio.sleep(0.01)
        clock.advance(0.06)
    await asyncio.sleep(0.02)

    status = await rec.stop_recording()
    assert status["state"] == "finalizing"
    await _await_finalize(rec)

    recording_dir = rec.status()["last_recording"]
    assert recording_dir is not None
    from pathlib import Path

    data_files = list(Path(recording_dir).glob("*.sigmf-data"))
    assert len(data_files) == 1
    total_samples = data_files[0].stat().st_size // 4
    assert total_samples >= preroll_samples_before + 2 * 50


async def test_same_second_recordings_get_exclusive_directories(tmp_path, stream):
    clock = FakeClock()
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream, clock=clock)
    dirs = []
    for _ in range(2):
        await rec.record()
        stream.push(10)
        await asyncio.sleep(0.02)
        await rec.stop_recording()
        await _await_finalize(rec)
        dirs.append(rec.status()["last_recording"])
    assert dirs[0] != dirs[1]
    assert all(
        Path(d, "run-01_1000SPS_100000000Hz.sigmf-data").stat().st_size == 40
        for d in dirs
    )


async def test_armed_preroll_stops_writing_when_reserve_is_crossed(tmp_path, stream, monkeypatch):
    from msdr.recorder import recorder as recorder_mod

    usage = [_DiskUsage(100, 90, 10 * 1e9)]
    monkeypatch.setattr(recorder_mod.shutil, "disk_usage", lambda path: usage[0])
    config = RecorderConfig(root=tmp_path, min_free_gb=1, segment_s=10)
    rec = await _armed(config, stream)
    stream.push(10)
    await asyncio.sleep(0.02)
    written_before = rec._preroll_ring.current.samples
    usage[0] = _DiskUsage(100, 99, 0.01 * 1e9)
    stream.push(10)
    for _ in range(50):
        if rec.status()["state"] == "error":
            break
        await asyncio.sleep(0.02)
    assert rec.status()["state"] == "error"
    closed = rec._preroll_ring.closed_segments()
    assert sum(m.samples for m in closed) == written_before


async def test_finalization_checks_merge_reserve_and_preserves_segments(
    tmp_path, stream, monkeypatch
):
    from msdr.recorder import recorder as recorder_mod

    config = RecorderConfig(root=tmp_path, min_free_gb=1, segment_s=10)
    rec = await _armed(config, stream)
    await rec.record()
    stream.push(100)
    for _ in range(50):
        if rec._recording_ring.current and rec._recording_ring.current.samples:
            break
        await asyncio.sleep(0.01)
    recording_dir = rec._recording_dir
    monkeypatch.setattr(
        recorder_mod.shutil, "disk_usage",
        lambda path: _DiskUsage(100, 99, 0.5 * 1e9),
    )
    segments_dir = recording_dir / "segments"
    await rec.stop_recording()
    await _await_finalize(rec)
    assert "low disk to merge" in rec.status()["error"]
    assert list(segments_dir.glob("*.cs16"))
    assert not (recording_dir / "recording.json").exists()


async def test_interrupted_recording_can_be_repaired_and_verified(tmp_path, stream):
    from msdr.recorder.ring import SegmentRing

    config = RecorderConfig(root=tmp_path, min_free_gb=0)
    rec = await _armed(config, stream)
    directory = tmp_path / "recordings" / "orphaned"
    ring = SegmentRing(directory / "segments")
    ring.open_segment(1000, 101_000_000, {}, now=1.0)
    ring.write(bytes(400))
    ring.close_segment()
    assert rec.recoverable_recordings() == [
        {"name": "orphaned", "segments": 1, "samples": 100, "bytes": 400}
    ]
    repaired = await rec.repair_recording("orphaned")
    assert repaired["verified"] is True
    assert not rec.recoverable_recordings()
    assert (directory / "recording.json").exists()


async def test_recovery_finds_iq_segment_without_sidecar(tmp_path, stream):
    from msdr.recorder.ring import SegmentRing

    rec = await _armed(RecorderConfig(root=tmp_path, min_free_gb=0), stream)
    directory = tmp_path / "recordings" / "missing-sidecar"
    ring = SegmentRing(directory / "segments")
    segment = ring.open_segment(1000, 102_000_000, {}, now=10.0)
    ring.write(bytes(80))
    ring.close_segment()
    segment.meta_path.unlink()

    candidate = rec.recoverable_recordings()[0]
    assert candidate["name"] == "missing-sidecar"
    assert candidate["samples"] == 20
    assert rec.recoverable_recordings()[0]["segments"] == 1
    assert segment.meta_path.exists() is False  # listing is read-only

    result = await rec.repair_recording("missing-sidecar")
    assert result["verified"] is True


async def test_recovery_finds_segments_when_recording_index_is_corrupt(tmp_path, stream):
    from msdr.recorder.ring import SegmentRing

    rec = await _armed(RecorderConfig(root=tmp_path, min_free_gb=0), stream)
    directory = tmp_path / "recordings" / "bad-index"
    ring = SegmentRing(directory / "segments")
    ring.open_segment(1000, 103_000_000, {}, now=20.0)
    ring.write(bytes(80))
    ring.close_segment()
    (directory / "recording.json").write_text("{")

    assert rec.recoverable_recordings()[0]["name"] == "bad-index"
    assert (directory / "recording.json").read_text() == "{"
    assert (await rec.repair_recording("bad-index"))["verified"] is True


# -- run splitting / annotations ------------------------------------------


async def test_center_change_splits_into_two_runs(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream, sample_rate=1000, center_hz=100_000_000)
    await rec.record()

    stream.push(50)
    await asyncio.sleep(0.02)

    rec.on_property("device_center_frequency", 200_000_000)
    stream.push(50)
    await asyncio.sleep(0.02)

    await rec.stop_recording()
    await _await_finalize(rec)

    from pathlib import Path

    recording_dir = Path(rec.status()["last_recording"])
    data_files = sorted(p.name for p in recording_dir.glob("*.sigmf-data"))
    assert len(data_files) == 2
    assert "100000000Hz" in data_files[0]
    assert "200000000Hz" in data_files[1]


async def test_lna_change_becomes_annotation_not_split(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream)
    await rec.record()

    stream.push(50)
    await asyncio.sleep(0.02)
    rec.on_property("lna_state", 5)
    stream.push(50)
    await asyncio.sleep(0.02)

    await rec.stop_recording()
    await _await_finalize(rec)

    from pathlib import Path

    recording_dir = Path(rec.status()["last_recording"])
    data_files = list(recording_dir.glob("*.sigmf-data"))
    assert len(data_files) == 1  # no split -- gain changes stay within one run

    meta = json.loads(next(recording_dir.glob("*.sigmf-meta")).read_text())
    labels = [a["core:label"] for a in meta["annotations"]]
    assert "lna_state" in labels
    assert "REC" in labels


# -- finalize / sigmf output ------------------------------------------


async def test_finalize_sigmf_meta_matches_data(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream, sample_rate=1000, center_hz=100_000_000)
    await rec.record()  # no preroll yet -- recording starts clean

    pushed = b""
    for _ in range(4):
        data = make_frame_bytes(50)
        stream.push_raw(data)
        pushed += data
        await asyncio.sleep(0.01)

    await rec.stop_recording()
    await _await_finalize(rec)

    from pathlib import Path

    recording_dir = Path(rec.status()["last_recording"])
    data_path = next(recording_dir.glob("*.sigmf-data"))
    meta_path = next(recording_dir.glob("*.sigmf-meta"))
    meta = json.loads(meta_path.read_text())

    raw = data_path.read_bytes()
    assert raw == pushed  # bytes equal the concatenated frames, in order
    sample_count = len(raw) // 4
    assert sample_count == 200  # 4 frames * 50 samples

    assert meta["global"]["core:datatype"] == "ci16_le"
    assert meta["global"]["core:sample_rate"] == 1000
    assert meta["global"]["core:version"] == "1.0.0"
    assert meta["captures"][0]["core:frequency"] == 100_000_000
    assert meta["captures"][0]["core:sample_start"] == 0


# -- backpressure ------------------------------------------------


async def test_queue_overflow_records_a_gap(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream)

    # Pre-load far beyond the bounded internal queue (~40) before the reader
    # task ever runs, so many frames are dropped in a single burst.
    stream.push_burst(count=500, n_samples=50)
    await asyncio.sleep(0.3)

    segs = rec._preroll_ring.closed_segments()
    gaps = list(rec._preroll_ring.current.gaps) if rec._preroll_ring.current else []
    for m in segs:
        gaps.extend(m.gaps)
    assert len(gaps) > 0


async def test_status_tracks_frames_and_data_ok(tmp_path, stream):
    clock = FakeClock()
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream, clock=clock)

    status = rec.status()
    assert status["frames"] == 0
    assert status["data_ok"] is False  # armed, but no frame has arrived yet

    stream.push(50, received_at=clock.t)
    await asyncio.sleep(0.02)
    status = rec.status()
    assert status["frames"] == 1
    assert status["data_ok"] is True

    clock.advance(3.0)  # past the 2s data_ok window, with no new frame
    assert rec.status()["data_ok"] is False


async def test_dropped_count_increments_on_queue_overflow(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream)

    stream.push_burst(count=500, n_samples=50)
    await asyncio.sleep(0.3)

    status = rec.status()
    assert status["frames"] > 0
    assert status["dropped"] > 0


async def test_finalize_progress_reports_fraction_of_segments_done(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    statuses: list[dict] = []

    async def on_status(status: dict) -> None:
        statuses.append(status)

    rec = Recorder(config, stream=stream, on_status=on_status)
    await rec.start()
    rec.on_property("connected", True)
    rec.on_property("started", True)
    rec.on_property("device_sample_rate", 1000)
    rec.on_property("device_center_frequency", 100_000_000)
    await rec.arm(True)
    _ARMED_RECORDERS.append(rec)
    await rec.record()

    stream.push(50)
    await asyncio.sleep(0.02)
    rec.on_property("device_center_frequency", 200_000_000)  # splits into a 2nd run
    stream.push(50)
    await asyncio.sleep(0.02)

    assert rec.status()["finalize_progress"] is None  # not finalizing yet
    await rec.stop_recording()
    await _await_finalize(rec)
    await asyncio.sleep(0.05)  # let any still-pending on_status tasks flush

    assert rec.status()["finalize_progress"] is None  # cleared once finalize completes
    progress_values = [
        s["finalize_progress"]
        for s in statuses
        if s["state"] == "finalizing" and s["finalize_progress"] is not None
    ]
    assert 0.5 in progress_values
    assert 1.0 in progress_values


async def test_upstream_stall_records_a_gap(tmp_path, stream):
    # Frames have no sequence numbers: a 2 s silence at 1000 S/s with no
    # frames lost locally must still be flagged from the wall-clock deficit.
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream)
    t = 1_000.0
    for i in range(5):
        stream.push(100, received_at=t + i * 0.1)
    stream.push(100, received_at=t + 0.5 + 2.0)
    await asyncio.sleep(0.2)
    gaps = [g for m in rec._preroll_ring.closed_segments() for g in m.gaps]
    gaps += list(rec._preroll_ring.current.gaps)
    assert len(gaps) == 1 and 1500 <= gaps[0]["missing_estimate"] <= 2100


async def test_start_does_no_disk_io_and_arm_reports_missing_folder(tmp_path, stream):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # a file where the root's parent should be -> mkdir fails
    rec = Recorder(RecorderConfig(root=blocker / "msdr", min_free_gb=0), stream=stream)
    await rec.start()  # an unmounted drive must not break app startup
    rec.on_property("connected", True)
    rec.on_property("started", True)
    with pytest.raises(RecorderError, match="recording folder unavailable"):
        await rec.arm(True)


# -- auto-disarm ------------------------------------------------


async def test_radio_stop_auto_disarms(config, stream):
    rec = await _armed(config, stream)
    assert rec.status()["state"] == "armed"

    rec.on_property("started", False)
    for _ in range(50):
        if rec.status()["state"] == "off":
            break
        await asyncio.sleep(0.02)

    status = rec.status()
    assert status["state"] == "off"
    assert status["error"] == "radio stopped"


async def test_radio_stop_during_recording_finalizes_and_disarms(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=10)
    rec = await _armed(config, stream)
    await rec.record()

    stream.push(50)
    await asyncio.sleep(0.02)

    rec.on_property("started", False)
    for _ in range(150):
        if rec.status()["state"] == "off":
            break
        await asyncio.sleep(0.02)

    status = rec.status()
    assert status["state"] == "off"
    assert status["error"] == "radio stopped"
    assert stream.enabled is False
    assert stream.started is False

    from pathlib import Path

    recording_dir = Path(status["last_recording"])
    assert list(recording_dir.glob("*.sigmf-data"))
    assert list(recording_dir.glob("*.sigmf-meta"))


# -- disarm/stop close the active segment ---------------------------


async def test_disarm_closes_active_segment_file_handle(config, stream):
    rec = await _armed(config, stream)
    stream.push(10)
    await asyncio.sleep(0.02)
    fh = rec._preroll_ring._fh
    assert fh is not None and not fh.closed

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await rec.arm(False)
        del rec
        gc.collect()

    assert fh.closed
    assert not any(issubclass(w.category, ResourceWarning) for w in caught)


# -- writer failure --------------------------------------------------


async def test_writer_failure_enters_error_state_and_preserves_segments(tmp_path, stream):
    clock = FakeClock()
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, segment_s=0.05)
    rec = await _armed(config, stream, clock=clock)

    stream.push(n_samples=50)
    await asyncio.sleep(0.01)
    clock.advance(0.06)  # rotate so a segment actually closes
    stream.push(n_samples=50)
    await asyncio.sleep(0.02)

    segments_before = rec._preroll_ring.closed_segments()
    assert len(segments_before) > 0
    paths_before = [m.path for m in segments_before]

    def _boom(data):
        raise OSError("disk full")

    rec._preroll_ring.write = _boom
    stream.push(n_samples=50)

    for _ in range(100):
        if rec.status()["state"] == "error":
            break
        await asyncio.sleep(0.02)

    status = rec.status()
    assert status["state"] == "error"
    assert "disk full" in status["error"]
    assert stream.enabled is False
    assert stream.started is False
    assert all(p.exists() for p in paths_before)


async def test_reader_failure_enters_error_state(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0)
    rec = await _armed(config, stream)

    stream.push_raw(None)  # len(None) raises inside the reader loop

    for _ in range(100):
        if rec.status()["state"] == "error":
            break
        await asyncio.sleep(0.02)

    status = rec.status()
    assert status["state"] == "error"
    assert status["error"] == "IQ stream failed"
    assert stream.enabled is False
    assert stream.started is False


async def test_concurrent_arms_start_one_reader_and_writer(tmp_path, stream):
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0)
    rec = Recorder(config, stream=stream)
    await rec.start()
    rec.on_property("connected", True)
    rec.on_property("started", True)
    rec.on_property("device_sample_rate", 1000)
    rec.on_property("device_center_frequency", 100_000_000)
    _ARMED_RECORDERS.append(rec)

    starts = 0
    real_start = stream.start

    async def slow_start():
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)
        await real_start()

    stream.start = slow_start
    await asyncio.gather(rec.arm(True), rec.arm(True))

    assert rec.status()["state"] == "armed"
    assert starts == 1, "the second arm must see the first one's armed state, not race it"


async def test_recording_duration_uses_monotonic_clock(tmp_path, stream):
    clock = FakeClock()
    mono = FakeClock(start=1000.0)
    config = RecorderConfig(root=tmp_path, preroll_s=10, min_free_gb=0, max_recording_s=5)
    rec = Recorder(config, stream=stream, clock=clock, monotonic=mono)
    await rec.start()
    rec.on_property("connected", True)
    rec.on_property("started", True)
    rec.on_property("device_sample_rate", 1000)
    rec.on_property("device_center_frequency", 100_000_000)
    await rec.arm(True)
    _ARMED_RECORDERS.append(rec)
    await rec.record()

    clock.advance(-3600)  # NTP step backwards: wall clock is now an hour earlier
    mono.advance(2.0)
    status = rec.status()
    assert status["recording_s"] == pytest.approx(2.0)

    clock.advance(7200)  # forward step must not trip the limit early
    await rec._check_recording_limits()
    assert rec.status()["state"] == "recording"
    mono.advance(3.5)
    assert rec.status()["recording_s"] == pytest.approx(5.5)


async def test_auto_disarm_serializes_with_concurrent_arm(config, stream):
    rec = await _armed(config, stream)
    await rec._enter_error("stream died")
    assert rec.status()["state"] == "error"

    release = asyncio.Event()
    real_stop = stream.stop

    async def slow_stop():
        await release.wait()
        await real_stop()

    stream.stop = slow_stop
    rec.on_property("connected", False)  # auto-disarm parks inside slow_stop
    await asyncio.sleep(0.02)
    rec.on_property("connected", True)
    arm = asyncio.create_task(rec.arm(True))
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.wait_for(arm, 2)
    await asyncio.sleep(0.02)

    # arm must run after the in-flight disarm, not interleave with it: the
    # unserialized version ends "off" with the arm's reader/writer orphaned.
    assert rec.status()["state"] == "armed"
    assert rec._reader_task is not None and not rec._reader_task.done()
    assert rec._writer_task is not None and not rec._writer_task.done()


async def test_low_disk_in_writer_disarms_and_keeps_error(tmp_path, stream, monkeypatch):
    from msdr.recorder import recorder as recorder_mod

    config = RecorderConfig(root=tmp_path, min_free_gb=1, segment_s=10)
    rec = await _armed(config, stream)
    await rec.record()
    stream.push(100)
    await asyncio.sleep(0.05)
    monkeypatch.setattr(
        recorder_mod.shutil, "disk_usage", lambda path: _DiskUsage(100, 99, 0.5 * 1e9)
    )
    stream.push(100)
    for _ in range(100):
        if rec.status()["state"] == "off":
            break
        await asyncio.sleep(0.02)
    status = rec.status()
    assert status["state"] == "off"  # not "armed" with a dead writer
    assert status["error"] == "low disk"  # finalize must not have cleared it
