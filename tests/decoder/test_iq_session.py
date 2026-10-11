import asyncio
from types import SimpleNamespace

import pytest

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.iq_session import IQDecoderSession


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
        for _ in range(5):
            await asyncio.sleep(0)
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
