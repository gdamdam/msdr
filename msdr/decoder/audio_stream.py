"""Dedicated WebSocket connection carrying only SDRconnect's demodulated audio.

Structure mirrors msdr/recorder/iq_stream.py exactly (own connection, backoff
reconnect, per-connection enable re-sent after reconnect) so decoding never
competes with IQ recording or control traffic for socket buffer space.
"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass

from websockets.exceptions import ConnectionClosed

from msdr.radio import readiness

logger = logging.getLogger(__name__)

# Binary frame header for payload type 1 (audio), 2-byte LE -- see spec
# "Measured facts": int16 stereo, L=R, ~9,608 bytes/frame at ~20 frames/s.
_AUDIO_HEADER = b"\x01\x00"


@dataclass(frozen=True)
class AudioFrame:
    data: bytes  # interleaved int16 PCM samples, header already stripped
    received_at: float  # time.time() when the frame was read off the socket


class AudioStream:
    """Own connection to SDRconnect; yields AudioFrame via frames().

    enable()/frames() are the only things a caller needs to drive this, which
    keeps it trivially replaceable by a fake in tests.
    """

    def __init__(
        self,
        host: str,
        port: int,
        backoff: tuple[float, float] = (0.5, 10.0),
        startup_grace_s: float | None = None,
    ):
        self._host = host
        self._port = port
        # After attachment or an SDRconnect (re)launch, readiness also applies
        # its shared minimum before this stream can be enabled.
        self._startup_grace_s = startup_grace_s if startup_grace_s is not None else 4.0
        self._backoff_start, self._backoff_max = backoff
        self._ws = None
        self._stopped = True
        # Per-connection, like iq_stream_enable -- reset to False whenever the
        # connection drops so a reconnect re-sends it.
        self._enabled = False
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def start(self) -> None:
        self._stopped = False
        self._reader_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        # SDRconnect can mute local playback while this stream is enabled.
        # Release it explicitly before closing the socket (also on mode-change
        # decoder teardown); socket closure alone need not restore playback.
        with contextlib.suppress(TimeoutError, OSError, ConnectionClosed):
            await asyncio.wait_for(self.enable(False), timeout=1.0)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        self._ws = None

    async def enable(self, on: bool) -> None:
        self._enabled = on
        if self._ws is not None:
            await self._send(on)

    async def _run(self) -> None:
        backoff = self._backoff_start
        while not self._stopped:
            try:
                async with readiness.ready_connection(
                    self._host, self._port, self._startup_grace_s, max_size=None
                ) as ws:
                    self._ws = ws
                    backoff = self._backoff_start
                    if self._enabled:
                        await self._send(True)
                    async for message in ws:
                        self._on_message(message)
            except Exception as exc:
                logger.warning("Audio stream connection error: %s", type(exc).__name__)
            finally:
                self._ws = None
                if not self._stopped:
                    readiness.mark_down(self._host, self._port)
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes) and message[:2] == _AUDIO_HEADER:
            self._queue.put_nowait(AudioFrame(message[2:], time.time()))
        # anything else (text, other binary payload types) is ignored here

    async def _send(self, on: bool) -> None:
        if self._ws is None:
            return
        msg = {
            "event_type": "audio_stream_enable",
            "property": "",
            "value": "true" if on else "false",
        }
        try:
            await self._ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass  # reconnect loop will resend enable on the next connection

    async def frames(self):
        while True:
            yield await self._queue.get()
