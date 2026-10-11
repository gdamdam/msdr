import asyncio
import json

import pytest

from msdr.decoder.audio_stream import AudioStream


@pytest.mark.asyncio
async def test_stop_releases_audio_before_closing_reader():
    calls = []

    class Socket:
        async def send(self, message):
            calls.append(json.loads(message))

    async def reader():
        try:
            await asyncio.Future()
        finally:
            calls.append('closed')

    stream = AudioStream('unused', 0)
    stream._ws = Socket()
    stream._enabled = True
    stream._reader_task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    await stream.stop()
    assert calls == [
        {'event_type': 'audio_stream_enable', 'property': '', 'value': 'false'}, 'closed'
    ]
    assert stream._enabled is False
    assert stream.connected is False


@pytest.mark.asyncio
async def test_stop_closes_reader_even_when_disable_fails():
    class Socket:
        async def send(self, message):
            raise OSError('connection lost')

    stream = AudioStream('unused', 0)
    stream._ws = Socket()
    stream._enabled = True
    await stream.stop()
    assert stream._enabled is False
    assert stream.connected is False
