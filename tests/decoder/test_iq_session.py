import asyncio
import time
from types import SimpleNamespace

import pytest

from msdr.decoder.backends import DecodeEvent, DecoderError, LoopEventQueue
from msdr.decoder.iq_session import IQDecoderSession, gap_fill


class Stream:
    def __init__(self):
        self.stopped = False
        self.queue = asyncio.Queue()

    async def start(self):
        pass

    async def enable(self, on):
        pass

    async def stop(self):
        self.stopped = True

    async def frames(self):
        while True:
            yield await self.queue.get()


class Backend:
    error = None
    dropped_frames = 0

    def __init__(self, fail=False):
        self.fail = fail
        self.stopped = False
        self.fed = []
        self.queue = asyncio.Queue()

    async def start(self):
        if self.fail:
            raise DecoderError("plugin absent")

    def feed(self, data):
        self.fed.append(data)

    async def events(self):
        while True:
            yield await self.queue.get()

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_iq_is_not_audio_demodulated_and_events_retain_integrity():
    stream, backend, events = Stream(), Backend(), []
    session = IQDecoderSession(
        stream=stream,
        backends={"adsb": backend},
        frequency_hz=1090000000,
        signal_id="signal",
        on_event=events.append,
        on_status=lambda: None,
    )
    await session.start()
    try:
        stream.queue.put_nowait(SimpleNamespace(data=b"\x01\x00\x02\x00"))
        backend.queue.put_nowait(DecodeEvent(1, "adsb", "text", "aircraft", {}))
        # feed() runs in a worker thread now, so wait for it rather than a
        # fixed number of loop ticks.
        for _ in range(200):
            if backend.fed and events:
                break
            await asyncio.sleep(0.005)
        assert backend.fed == [b"\x01\x00\x02\x00"]
        assert events[0].source_id == "live:sdrconnect"
        assert events[0].frequency_hz == 1090000000
        assert events[0].integrity == "unknown"
        facts = session.status()["channels"][0]["decoder_status"][0]
        assert facts["events"] == 1
        assert facts["valid_frames"] == 0
        backend.dropped_frames = 2
        assert session.status()["channels"][0]["decoder_status"][0]["dropped_frames"] == 2
    finally:
        await session.stop()
    assert stream.stopped and backend.stopped


@pytest.mark.asyncio
async def test_partial_start_failure_closes_every_backend_and_stream():
    stream, first, second = Stream(), Backend(), Backend(fail=True)
    session = IQDecoderSession(
        stream=stream,
        backends={"adsb": first, "ft8": second},
        frequency_hz=0,
        signal_id=None,
        on_event=lambda event: None,
        on_status=lambda: None,
    )
    with pytest.raises(DecoderError, match="plugin absent"):
        await session.start()
    assert stream.stopped and first.stopped and second.stopped


def test_status_includes_drops_from_the_input_stream():
    from msdr.recorder.iq_stream import IQStream

    stream = IQStream("unused", 1)
    # Fill the real bounded transport queue and force one old frame out.
    for _ in range(41):
        stream._on_message(b"\x02\x00" + bytes(8))
    backend = Backend()
    backend.dropped_frames = 2
    session = IQDecoderSession(
        stream=stream, backends={"adsb": backend}, frequency_hz=1090000000,
        signal_id=None, on_event=lambda event: None, on_status=lambda: None,
    )
    status = session.status()
    assert status["channels"][0]["decoder_status"][0]["dropped_frames"] == 3
    assert status["iq"]["dropped"] == 1
    assert status["iq"]["dropped_samples"] == 2


class SlowBackend(Backend):
    """Stands in for a CPU-heavy in-process decoder (End-of-Train, VOR):
    feed() blocks, then emits through LoopEventQueue from that thread."""

    def __init__(self):
        super().__init__()
        self.queue = LoopEventQueue()

    def feed(self, data):
        time.sleep(0.3)
        self.queue.put_nowait(DecodeEvent(1, "vor", "measurement", "radial", {}))


@pytest.mark.asyncio
async def test_slow_decoder_feed_does_not_block_event_loop():
    stream, backend, events = Stream(), SlowBackend(), []
    session = IQDecoderSession(
        stream=stream,
        backends={"vor": backend},
        frequency_hz=115_800_000,
        signal_id=None,
        on_event=events.append,
        on_status=lambda: None,
    )
    await session.start()
    try:
        stream.queue.put_nowait(SimpleNamespace(data=b"\x01\x00\x02\x00"))
        await asyncio.sleep(0.01)  # let the pump hand the frame to its thread
        # The loop must keep running while feed() sleeps: on-loop feeding
        # would stall this for the full 0.3 s.
        start = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - start < 0.2
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.01)
        assert [e.text for e in events] == ["radial"]
    finally:
        await session.stop()


def _frame(data, offset, generation=1):
    return SimpleNamespace(data=data, stream_offset=offset, session_generation=generation)


def test_gap_fill_zeroes_exactly_the_samples_dropped_before_a_frame():
    assert gap_fill((1, 100), _frame(b"", 103)) == bytes(12)
    assert gap_fill((1, 100), _frame(b"", 100)) == b""  # contiguous
    assert gap_fill(None, _frame(b"", 100)) == b""  # first frame
    assert gap_fill((1, 100), _frame(b"", 103, generation=2)) == b""  # new session
    assert gap_fill((1, 100), SimpleNamespace(data=b"")) == b""  # replay/fake frame
    assert gap_fill((1, 0), _frame(b"", (1 << 20) + 1)) == b""  # too long to fill


@pytest.mark.asyncio
async def test_rs41_frame_survives_a_dropped_stream_frame():
    # A frame lost on queue overflow used to shift every later bit; zero-filling
    # the gap keeps timing so Reed-Solomon repairs the hole.
    from msdr.decoder.pydecoders.rs41 import RS41Backend
    from tests.decoder.rs41_fixtures import rs41_iq, rs41_test_frame

    raw = rs41_iq(rs41_test_frame(), leading_samples=5)
    chunk = 1024 * 4
    stream, events = Stream(), []
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    session = IQDecoderSession(
        stream=stream,
        backends={"rs41": backend},
        frequency_hz=403_000_000,
        signal_id="signal",
        on_event=events.append,
        on_status=lambda: None,
    )
    await session.start()
    try:
        chunks = [raw[i : i + chunk] for i in range(0, len(raw), chunk)]
        for n, data in enumerate(chunks):
            if n != len(chunks) // 2:  # this frame is dropped
                stream.queue.put_nowait(_frame(data, n * 1024))
        for _ in range(400):
            if events:
                break
            await asyncio.sleep(0.005)
        assert events and events[0].integrity == "passed"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_feed_before_stopping_backend():
    order = []

    class SlowBackend(Backend):
        def feed(self, data):
            order.append("feed-start")
            time.sleep(0.2)
            order.append("feed-end")

        async def stop(self):
            order.append("backend-stop")

    stream, backend = Stream(), SlowBackend()
    session = IQDecoderSession(
        stream=stream,
        backends={"adsb": backend},
        frequency_hz=1090000000,
        signal_id="signal",
        on_event=lambda e: None,
        on_status=lambda: None,
    )
    await session.start()
    stream.queue.put_nowait(SimpleNamespace(data=b"\x01\x00\x02\x00"))
    for _ in range(200):
        if "feed-start" in order:
            break
        await asyncio.sleep(0.005)
    await session.stop()
    assert order == ["feed-start", "feed-end", "backend-stop"]
