"""ReplaySource clock: generations, run changes, seek/loop, no skipping."""

import asyncio
import json

import numpy as np
import pytest

from msdr.contracts.source import SourceDescriptor
from msdr.radio.protocol import PropertyChanged
from msdr.source.base import IQSource, SourceEvent
from msdr.source.live import LiveSource
from msdr.survey.replay import ReplaySource
from tests.source.test_timeline import KNOWN_GAP, RUNS, write_three_runs
from tests.survey.test_replay import _iq16, write_wav

TOTAL = sum(n for _, _, n, _ in RUNS)


class FakeClock:
    """Monotonic clock that jumps `step` s per read: the host looks ever
    further behind schedule, which used to trigger the catch-up skip."""

    def __init__(self, step: float = 0.0) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


async def _no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


def _source(path, *, loop=False, clock_step=0.0) -> ReplaySource:
    return ReplaySource(path, loop=loop, clock=FakeClock(clock_step), sleep=_no_sleep)


def _decode(frame) -> tuple[np.ndarray, np.ndarray]:
    iq = np.frombuffer(frame.data, dtype="<i2")
    return iq[0::2], iq[1::2]


async def _collect(stream, frames: list, *, slow: bool = False) -> None:
    async for frame in stream.frames():
        frames.append(frame)
        if slow:  # a consumer slower than the pump
            for _ in range(5):
                await asyncio.sleep(0)


async def _wait_for(pred, timeout: float = 5.0) -> None:
    async def poll():
        while not pred():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout)


def test_replay_source_satisfies_protocol(tmp_path):
    src = _source(write_three_runs(tmp_path))
    assert isinstance(src, IQSource)
    assert src.source_id == "replay:rec"
    desc = src.descriptor()
    assert isinstance(desc, SourceDescriptor)
    assert (desc.mode, desc.center_hz, desc.sample_rate_hz) == ("replay", 100_000_000, 10_000)
    assert desc.capture_time is not None


def test_seek_across_runs_updates_descriptor_and_bumps_generation(tmp_path):
    src = _source(write_three_runs(tmp_path))
    events: list[SourceEvent] = []
    src.add_listener(events.append)

    src.seek(src.timeline.elapsed_s(2, 100))
    assert (src.run_index, src.sample_index, src.generation) == (2, 100, 1)
    assert (src.center_hz, src.sample_rate_hz) == (101_000_000, 20_000)
    assert [e.kind for e in events] == ["run", "generation"]
    assert all(e.generation == 1 and e.run_index == 2 for e in events)
    assert events[-1].descriptor.center_hz == 101_000_000
    assert events[-1].discontinuity.kind == "retune"
    assert events[-1].discontinuity.at_sample == 100
    st = src.status()
    assert st["capture_start"] is None  # run 2 has no trustworthy time
    assert (st["generation"], st["run_index"], st["center_hz"]) == (1, 2, 101_000_000)

    events.clear()
    src.seek(src.timeline.elapsed_s(2, 500))  # same run: a plain seek
    assert [e.kind for e in events] == ["generation"]
    assert events[0].discontinuity.kind == "seek"
    assert src.generation == 2


async def test_plays_every_sample_across_runs_under_a_slow_consumer(tmp_path):
    # Clock jumps 1 s per read: hopelessly behind, yet nothing is skipped.
    src = _source(write_three_runs(tmp_path), clock_step=1.0)
    events: list[SourceEvent] = []
    src.add_listener(events.append)
    behind: list[float] = []
    src.add_listener(lambda e: e.kind == "end" and behind.append(src.status()["behind_s"]))
    stream = src.iq_stream()
    await stream.start()
    await stream.enable(True)
    frames: list = []
    consumer = asyncio.create_task(_collect(stream, frames, slow=True))
    await src.start()
    try:
        await _wait_for(lambda: any(e.kind == "end" for e in events))
        await _wait_for(lambda: sum(len(f.data) // 4 for f in frames) >= TOTAL)
        assert behind and behind[0] > 1.0  # reported, not skipped
    finally:
        await src.stop()
        consumer.cancel()

    assert sum(len(f.data) // 4 for f in frames) == TOTAL
    # Run boundaries: generation events before the first frame of each run.
    run_events = [e for e in events if e.kind == "run"]
    # ... and the rewind to run 0 after the end (non-loop).
    assert [e.run_index for e in run_events] == [1, 2, 0]
    assert run_events[2].discontinuity.kind == "seek"
    assert run_events[0].discontinuity.kind == "gap"
    assert run_events[0].discontinuity.gap_samples == KNOWN_GAP
    assert run_events[1].discontinuity.kind == "gap"
    assert run_events[1].discontinuity.gap_samples is None  # unknown, not invented
    gen_of_run = {0: 0, 1: run_events[0].generation, 2: run_events[1].generation}

    by_run: dict[int, list] = {0: [], 1: [], 2: []}
    for f in frames:
        i, q = _decode(f)
        run = int(q[0])
        assert (q == run).all()
        assert f.generation == gen_of_run[run]
        by_run[run].append((f.sample_index, i))
    for run, (_rate, _center, n, _sw) in enumerate(RUNS):
        idx = [s for s, _ in by_run[run]]
        assert idx == sorted(idx) and idx[0] == 0
        data = np.concatenate([i for _, i in by_run[run]])
        assert np.array_equal(data, np.arange(n) % 30_000)  # contiguous
        for s, i in by_run[run]:
            assert int(i[0]) == s % 30_000  # sample_index matches the data
    # Non-loop end: rewound and paused (old behaviour), with an "end" event.
    assert not src.playing
    assert (src.run_index, src.sample_index) == (0, 0)


async def test_interior_gaps_split_frames_and_reset_before_postgap_samples(tmp_path):
    d = write_three_runs(tmp_path)
    meta_path = sorted(d.glob("*.sigmf-meta"))[0]
    meta = json.loads(meta_path.read_text())
    # 512 is the block boundary; 750 falls inside the next 512-sample block.
    for sample, missing in ((512, 100), (750, 200)):
        meta["annotations"].append(
            {
                "core:sample_start": sample,
                "core:label": "gap",
                "core:comment": f"missing ~{missing} samples",
            }
        )
    meta_path.write_text(json.dumps(meta))

    src = _source(d, clock_step=1.0)
    events: list[SourceEvent] = []
    src.add_listener(events.append)
    stream = src.iq_stream()
    await stream.start()
    await stream.enable(True)
    frames: list = []
    consumer = asyncio.create_task(_collect(stream, frames))
    await src.start()
    try:
        await _wait_for(lambda: any(e.kind == "end" for e in events))
        await _wait_for(lambda: sum(len(f.data) // 4 for f in frames) >= TOTAL)
    finally:
        await src.stop()
        consumer.cancel()

    first = [f for f in frames if f.run_index == 0]
    assert [(f.sample_index, len(f.data) // 4, f.generation) for f in first[:3]] == [
        (0, 512, 0),
        (512, 238, 1),
        (750, 512, 2),
    ]
    assert np.array_equal(np.concatenate([_decode(f)[0] for f in first]), np.arange(3000))
    gap_events = [
        e
        for e in events
        if e.kind == "generation" and e.run_index == 0 and e.discontinuity.kind == "gap"
    ]
    assert [(e.sample_index, e.generation, e.discontinuity.gap_samples) for e in gap_events] == [
        (512, 1, 100),
        (750, 2, 200),
    ]
    assert not any(e.kind == "run" and e.run_index == 0 and e.generation in (1, 2) for e in events)

    # Seeking to or past a marker is already a generation change; replay must
    # not report the old gap again when it resumes at that stored sample.
    before_seek = src.generation
    src.seek(src.timeline.elapsed_s(0, 512))
    assert (src.run_index, src.sample_index, src.generation) == (0, 512, before_seek + 1)
    assert src._gap_cursor == 1

    resumed: list = []
    fresh_stream = src.iq_stream()
    await fresh_stream.start()
    await fresh_stream.enable(True)
    resumed_consumer = asyncio.create_task(_collect(fresh_stream, resumed))
    src.playing = True
    await src.start()
    try:
        await _wait_for(lambda: len(resumed) > 0)
    finally:
        await src.stop()
        resumed_consumer.cancel()
    assert (resumed[0].sample_index, resumed[0].generation) == (512, before_seek + 1)
    assert not any(
        e.kind == "generation"
        and e.discontinuity.kind == "gap"
        and e.sample_index == 512
        and e.generation > before_seek
        for e in events
    )


async def test_seek_and_pause_interrupt_a_long_interior_gap(tmp_path):
    d = write_three_runs(tmp_path)
    meta_path = sorted(d.glob("*.sigmf-meta"))[0]
    meta = json.loads(meta_path.read_text())
    meta["annotations"].append(
        {
            "core:sample_start": 512,
            "core:label": "gap",
            "core:comment": "missing ~1000000 samples",
        }
    )
    meta_path.write_text(json.dumps(meta))
    src = ReplaySource(d, loop=False)
    events: list[SourceEvent] = []
    src.add_listener(events.append)
    await src.start()
    try:
        await _wait_for(
            lambda: any(e.kind == "generation" and e.discontinuity.kind == "gap" for e in events)
        )
        at_gap = src.generation
        src.playing = False
        await asyncio.sleep(0.02)
        assert (src.sample_index, src.generation) == (512, at_gap)
        src.seek(src.timeline.elapsed_s(0, 1000))
        await _wait_for(lambda: src.generation > at_gap)
        assert (src.sample_index, src.generation) == (1000, at_gap + 1)
        assert not src.playing
        src.seek(0.0)
        await _wait_for(lambda: src.sample_index == 0)
        src.playing = True
        await _wait_for(
            lambda: (
                len([e for e in events if e.kind == "generation" and e.discontinuity.kind == "gap"])
                == 2
            )
        )
        await asyncio.wait_for(src.stop(), 0.5)
    finally:
        await asyncio.wait_for(src.stop(), 0.5)


async def test_loop_wrap_bumps_generation(tmp_path):
    src = _source(write_three_runs(tmp_path), loop=True)
    events: list[SourceEvent] = []
    src.add_listener(events.append)
    await src.start()
    try:
        await _wait_for(
            lambda: any(e.discontinuity and e.discontinuity.kind == "loop" for e in events)
        )
    finally:
        await src.stop()
    loop_ev = next(e for e in events if e.discontinuity and e.discontinuity.kind == "loop")
    assert loop_ev.run_index == 0 and loop_ev.sample_index == 0
    assert loop_ev.descriptor.center_hz == 100_000_000
    # Wrapping from run 2 to run 0 is a run change too.
    kinds = [e.kind for e in events if e.generation == loop_ev.generation]
    assert kinds == ["run", "generation"]
    assert src.playing


async def test_seek_while_playing_no_old_frame_after_the_new_generation(tmp_path):
    src = _source(write_three_runs(tmp_path), loop=True)
    seek_gens: list[int] = []
    src.add_listener(
        lambda e: e.kind == "generation" and "seek to" in e.discontinuity.note
        and seek_gens.append(e.generation)
    )  # fmt: skip
    stream = src.iq_stream()
    await stream.start()
    await stream.enable(True)
    frames: list = []
    consumer = asyncio.create_task(_collect(stream, frames, slow=True))
    await src.start()
    try:
        await _wait_for(lambda: len(frames) >= 3)
        old_gen = src.generation
        target = src.timeline.elapsed_s(1, 1500)
        src.seek(target)
        assert src.status()["position_s"] == pytest.approx(target)  # pending, reported
        await _wait_for(lambda: any(f.generation > old_gen for f in frames))
    finally:
        await src.stop()
        consumer.cancel()
    first_new = next(i for i, f in enumerate(frames) if f.generation > old_gen)
    assert frames[first_new].sample_index == 1500
    assert int(_decode(frames[first_new])[1][0]) == 1  # run 1's data
    # A frame of the old generation is identifiable, and none follows the new.
    assert frames[first_new - 1].generation <= old_gen < frames[first_new].generation
    assert frames[first_new].generation == seek_gens[0]
    assert all(f.generation >= frames[first_new].generation for f in frames[first_new:])


async def test_pause_keeps_the_clock_still(tmp_path):
    src = _source(write_three_runs(tmp_path), loop=True)
    events: list[SourceEvent] = []
    src.add_listener(events.append)
    await src.start()
    try:
        await _wait_for(lambda: src.sample_index > 0 or src.run_index > 0)
        src.playing = False
        await asyncio.sleep(0.02)
        held = (src.run_index, src.sample_index, src.generation)
        await asyncio.sleep(0.05)
        assert (src.run_index, src.sample_index, src.generation) == held
        assert events[-1].kind == "position"
        # Seek while paused is applied by the pump without playing on.
        src.seek(src.timeline.elapsed_s(2, 10))
        await _wait_for(lambda: src.generation > held[2])
        assert src.sample_index == 10 and not src.playing
    finally:
        await src.stop()


async def test_a_raising_listener_does_not_stop_the_pump(tmp_path):
    src = _source(write_three_runs(tmp_path))
    ok: list[SourceEvent] = []

    def bad(_event):
        raise RuntimeError("boom")

    src.add_listener(bad)
    src.add_listener(ok.append)
    await src.start()
    try:
        await _wait_for(lambda: any(e.kind == "end" for e in ok))
    finally:
        await src.stop()


async def test_wav_single_run_replays_without_capture_time(tmp_path):
    rate = 20_000
    path = tmp_path / "SDRconnect_IQ_20260925_135725_13000000HZ.wav"
    write_wav(path, _iq16(5000, rate, 1000), rate)
    src = _source(path)
    st = src.status()
    for key in (
        "path", "name", "center_hz", "sample_rate_hz", "duration_s", "position_s", "playing",
        "loop", "generation", "run_index", "runs", "capture_start", "behind_s", "source_id",
    ):  # fmt: skip
        assert key in st
    assert st["capture_start"] is None and len(st["runs"]) == 1
    assert src.descriptor().capture_time is None
    stream = src.iq_stream()
    await stream.start()
    await stream.enable(True)
    frames: list = []
    consumer = asyncio.create_task(_collect(stream, frames))
    await src.start()
    try:
        await _wait_for(lambda: sum(len(f.data) // 4 for f in frames) >= 5000)
    finally:
        await src.stop()
        consumer.cancel()
    assert sum(len(f.data) // 4 for f in frames) == 5000
    assert all(f.generation == 0 for f in frames)


def test_live_source_bumps_generation_on_centre_change():
    class Hub:
        def acquire(self):
            return asyncio.Queue()

        def release(self, q):
            pass

    live = LiveSource(adapter=None, hub=Hub(), iq_stream_factory=None,
                      center_hz=100_000_000, sample_rate_hz=2_000_000)  # fmt: skip
    events: list[SourceEvent] = []
    live.add_listener(events.append)
    assert live.descriptor().generation == 0

    live.handle_event(PropertyChanged("device_center_frequency", "100000000", "d", 0.0))
    assert events == []  # unchanged value: no bump
    live.handle_event(PropertyChanged("device_center_frequency", "145800000", "d", 0.0))
    assert live.descriptor().center_hz == 145_800_000
    assert live.generation == 1
    assert [e.kind for e in events] == ["generation"]
    assert events[0].discontinuity.kind == "retune"
    assert (events[0].run_index, events[0].sample_index) == (0, 0)
    live.handle_event(PropertyChanged("device_sample_rate", 8_000_000, "d", 0.0))
    assert live.generation == 2 and live.descriptor().sample_rate_hz == 8_000_000
