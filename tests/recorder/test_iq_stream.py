import asyncio

import pytest

from msdr.recorder.iq_stream import IQStream


@pytest.mark.asyncio
async def test_stream_queue_is_bounded_and_keeps_latest_frames():
    stream = IQStream("127.0.0.1", 5454)
    for n in range(50):
        stream._on_message(b"\x02\x00" + bytes([n]))
    assert stream.backlog_frames == 40
    assert stream.dropped_count == 10
    frame = await stream._queue.get()
    assert frame.data == bytes([10])


@pytest.mark.asyncio
async def test_start_clears_old_frames_and_tags_new_session_generation(monkeypatch):
    stream = IQStream("127.0.0.1", 5454)

    async def parked_run():
        await asyncio.Event().wait()

    monkeypatch.setattr(stream, "_run", parked_run)
    stream._on_message(b"\x02\x00old")
    await stream.start()
    generation = stream.generation
    stream._on_message(b"\x02\x00new")
    frame = await stream._queue.get()
    assert frame.data == b"new"
    assert frame.generation == 0
    assert frame.session_generation == generation
    assert stream.backlog_frames == 0
    await stream.stop()
