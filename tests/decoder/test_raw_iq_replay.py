"""Raw recording decoding must never concatenate different sample clocks."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from msdr.contracts.source import SourceDescriptor
from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.iq_backend import build_iq_backend
from msdr.decoder.registry import DECODERS
from msdr.decoder.replay_decode import ReplayDecodeSession
from msdr.recorder.iq_stream import IQFrame
from msdr.settings import DecoderSettings


class Stream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.stopped = False
        self.dropped = 0

    async def start(self):
        pass

    async def enable(self, on):
        pass

    async def stop(self):
        self.stopped = True

    async def frames(self):
        while True:
            yield await self.queue.get()


class Source:
    source_id = "replay:test"
    run_index = 0

    def __init__(self):
        self.desc = SourceDescriptor(
            source_id=self.source_id,
            mode="replay",
            center_hz=403_000_000,
            sample_rate_hz=192_000,
        )
        self.stream = Stream()

    def descriptor(self):
        return self.desc

    def iq_stream(self):
        return self.stream


class Receiver:
    channel_generation = 0
    frequency = 403_000_000

    def __init__(self, source):
        self.source = source

    def status(self):
        return dict(
            generation=self.source.desc.generation,
            channel_generation=self.channel_generation,
            frequency_hz=self.frequency,
            demodulator="NFM",
        )


class Backend:
    error = None

    def __init__(self):
        self.queue = asyncio.Queue()
        self.inputs = []
        self.stopped = False

    async def start(self):
        pass

    def feed(self, data):
        self.inputs.append(data)

    async def stop(self):
        self.stopped = True

    async def events(self):
        while True:
            yield await self.queue.get()


@pytest.fixture
def harness(monkeypatch):
    source = Source()
    receiver = Receiver(source)
    backends, events = [], []

    def build(spec, binaries, **kwargs):
        backend = Backend()
        backend.arguments = kwargs
        backends.append(backend)
        return backend

    monkeypatch.setattr("msdr.decoder.replay_decode.build_iq_backend", build)
    session = ReplayDecodeSession(
        receiver,
        source,
        binaries={},
        settings=DecoderSettings(),
        on_event=events.append,
        on_status=lambda _: None,
    )
    return SimpleNamespace(
        source=source, receiver=receiver, session=session, backends=backends, events=events
    )


def frame(index=0, generation=0, run=0):
    return IQFrame(b"\0" * 400, 1.0, generation, index, run)


def event():
    return DecodeEvent(1.0, "rs41", "message", "test", {}, integrity="passed")


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_raw_pump_provenance_and_stops_without_live_writes(harness):
    h = harness
    await h.session.start_single(["rs41"])
    try:
        await h.source.stream.queue.put(frame(200))
        await settle()
        assert h.backends[0].inputs == [frame().data]
        await h.backends[0].queue.put(event())
        await settle()
        e = h.events[0]
        assert (e.source_id, e.generation, e.sample_index, e.run_index) == (
            "replay:test",
            0,
            200,
            0,
        )
        assert e.analysis_run_id and e.fields["source"] == "replay-iq"
        assert e.frequency_hz == h.receiver.frequency
    finally:
        await h.session.stop()
    assert h.source.stream.stopped and h.backends[0].stopped
    assert not h.session.active


@pytest.mark.parametrize("change", ["seek", "loop", "run", "retune", "recording"])
async def test_generation_transition_rejects_old_output_and_restarts(harness, change):
    h = harness
    await h.session.start_single(["rs41"])
    try:
        await h.session._feed_iq(frame())
        old = h.backends[-1]
        if change == "retune":
            h.receiver.channel_generation += 1
            h.receiver.frequency += 1000
        elif change == "recording":
            h.source.source_id = "replay:replacement"
        else:
            h.source.desc = h.source.desc.model_copy(update={"generation": 1})
            if change == "run":
                h.source.run_index = 1
                h.source.desc = h.source.desc.model_copy(update={"sample_rate_hz": 384_000})
        await old.queue.put(event())
        await settle()
        assert not h.events
        new_frame = frame(100, h.source.desc.generation, h.source.run_index)
        assert await h.session._feed_iq(new_frame)
        assert old.stopped and len(h.backends) == 2
        assert h.backends[-1].arguments["sample_rate_hz"] == h.source.desc.sample_rate_hz
        await h.backends[-1].queue.put(event())
        await settle()
        assert len(h.events) == 1
        assert h.events[0].source_id == h.source.source_id
        assert h.events[0].run_index == h.source.run_index
    finally:
        await h.session.stop()


async def test_gaps_duplicates_and_stale_blocks(harness):
    h = harness
    await h.session.start_single(["rs41"])
    try:
        await h.session._feed_iq(frame())
        assert not await h.session._feed_iq(frame())  # duplicate
        assert len(h.backends[0].inputs) == 1
        assert await h.session._feed_iq(frame(300))  # samples 100..299 missing
        assert len(h.backends) == 2 and h.backends[0].stopped
        assert not await h.session._feed_iq(frame(400, 7))
        assert len(h.backends[1].inputs) == 1
        assert await h.session._feed_iq(frame(400), dropped=1)
        assert len(h.backends) == 3
    finally:
        await h.session.stop()


async def test_stopped_session_cannot_emit_queued_output(harness):
    h = harness
    await h.session.start_single(["rs41"])
    await h.session._feed_iq(frame())
    await h.backends[0].queue.put(event())
    await h.session.stop()
    await settle()
    assert not h.events


async def test_restart_has_new_analysis_identity(harness):
    h = harness
    await h.session.start_single(["rs41"])
    first = h.session.analysis_run_id
    await h.session.start_single(["rs41"])
    assert h.session.analysis_run_id != first
    assert h.backends[0].stopped
    await h.session.stop()


def test_raw_factory_validates_capture_span_and_rate():
    spec = DECODERS["rs41"]
    for rate, freq in [(1000, 403e6), (192000, 404e6), (192000, None)]:
        with pytest.raises(DecoderError):
            build_iq_backend(spec, {}, sample_rate_hz=rate, center_hz=403e6, frequency_hz=freq)
    backend = build_iq_backend(spec, {}, sample_rate_hz=192000, center_hz=403e6, frequency_hz=403e6)
    assert backend.decoder_id == "rs41"
    with pytest.raises(DecoderError, match="unsupported"):
        build_iq_backend(
            replace(spec, backend="unknown"),
            {},
            sample_rate_hz=192000,
            center_hz=403e6,
            frequency_hz=403e6,
        )


async def test_canceled_start_reaps_started_backend(harness, monkeypatch):
    h = harness
    entered = asyncio.Event()
    gate = asyncio.Event()
    backend = Backend()

    async def blocked_start():
        entered.set()
        await gate.wait()

    backend.start = blocked_start
    monkeypatch.setattr("msdr.decoder.replay_decode.build_iq_backend", lambda *a, **kw: backend)
    task = asyncio.create_task(h.session.start_single(["rs41"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.stopped
    assert not h.session.active
    assert not h.session._pump_tasks


async def test_ft8_requires_capture_clock_and_uses_sample_offset(harness):
    from datetime import UTC, datetime

    h = harness
    h.session._binaries = {"jt9": "/fake/jt9"}
    status = await h.session.start_single(["ft8"])
    assert "capture UTC" in status["decoders"][0]["error"]
    assert not h.backends
    await h.session.stop()
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    h.source.desc = h.source.desc.model_copy(update={"capture_time": origin})
    await h.session.start_single(["ft8"])
    clocks = []
    h.backends[0].feed_at = lambda data, epoch: clocks.append(epoch)
    try:
        await h.session._feed_iq(frame(192000))
        assert clocks == [origin.timestamp() + 1]
    finally:
        await h.session.stop()


def test_ft8_window_uses_supplied_recording_time(monkeypatch):
    from msdr.decoder.standalone_ft8 import StandaloneFT8Backend

    backend = StandaloneFT8Backend(
        None,
        sample_rate_hz=48000,
        center_hz=14_074_000,
        frequency_hz=14_074_000,
    )
    monkeypatch.setattr("msdr.decoder.standalone_ft8.time.time", lambda: 999999)
    backend.feed_at(b"\0" * 40, 1500.5)
    assert backend._window_epoch == 1515


async def test_real_rs41_recording_reaches_raw_decoder(tmp_path):
    """Exercise SigMF playback, not just a fake IQ stream/backend."""
    import json

    from msdr.source.virtual_receiver import VirtualReceiver
    from msdr.survey.replay import ReplaySource
    from tests.decoder.rs41_fixtures import rs41_iq, rs41_test_frame

    rate, center = 57600, 403_000_000
    data = rs41_iq(rs41_test_frame(), leading_samples=5) * 2
    recording = tmp_path / "synthetic-rs41"
    recording.mkdir()
    (recording / "run.sigmf-data").write_bytes(data)
    (recording / "run.sigmf-meta").write_text(
        json.dumps(
            {
                "global": {
                    "core:datatype": "ci16_le",
                    "core:sample_rate": rate,
                    "core:version": "1.0.0",
                },
                "captures": [{"core:sample_start": 0, "core:frequency": center}],
                "annotations": [],
            }
        )
    )
    (recording / "recording.json").write_text(
        json.dumps(
            {
                "start_wall": 1700000000.0,
                "end_wall": 1700000002.0,
                "preroll_s": 0,
                "runs": [
                    {
                        "data": "run.sigmf-data",
                        "meta": "run.sigmf-meta",
                        "sample_rate": rate,
                        "center_hz": center,
                        "samples": len(data) // 4,
                        "start_wall": 1700000000.0,
                        "settings": {},
                    }
                ],
            }
        )
    )
    source = ReplaySource(recording, loop=False)
    receiver = VirtualReceiver(source)
    receiver.tune(center, "NFM", 12500)
    events = []
    session = ReplayDecodeSession(
        receiver,
        source,
        binaries={},
        settings=DecoderSettings(),
        on_event=events.append,
        on_status=lambda _: None,
    )
    await receiver.start()
    try:
        await session.start_single(["rs41"])
        await settle()
        await source.start()
        async with asyncio.timeout(10):
            while source.playing:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.1)
        assert events, session.status()
        assert all(e.integrity == "passed" and e.source_id == source.source_id for e in events)
        assert all(e.fields["source"] == "replay-iq" and e.analysis_run_id for e in events)
        assert all(e.sample_index is not None for e in events)
    finally:
        await session.stop()
        await source.stop()
        await receiver.stop()


async def test_stop_cleans_other_backends_when_one_cleanup_fails(harness):
    h = harness
    await h.session.start_single(["rs41", "dsc"])

    async def broken_stop():
        raise RuntimeError("cleanup failed")

    h.backends[0].stop = broken_stop
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await h.session.stop()
    assert not h.session.active
    assert h.backends[1].stopped
    assert all(single.backend is None for single in h.session._singles.values())
