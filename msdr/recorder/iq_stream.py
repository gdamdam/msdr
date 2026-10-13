"""Dedicated WebSocket connection carrying only the IQ stream.

Kept separate from SDRconnectAdapter's control connection so ~40 MB/s of IQ
data at 10 MS/s can't starve control traffic (property gets/sets, spectrum).
"""

import asyncio
import contextlib
import itertools
import json
import logging
import time
from dataclasses import dataclass

from websockets.exceptions import ConnectionClosed

from msdr.radio import readiness

_INSTANCES = itertools.count()

logger = logging.getLogger(__name__)

# Binary frame header for payload type 2 (IQ), 2-byte LE.
_IQ_HEADER = b"\x02\x00"
_QUEUE_SIZE = 40


@dataclass(frozen=True)
class IQFrame:
    data: bytes  # interleaved int16 I/Q samples, header already stripped
    received_at: float  # time.time() when the frame was read off the socket
    # Source clock (msdr.source): replay sets both; live leaves the defaults.
    # Consumers drop frames whose generation isn't theirs (seek/loop/retune).
    generation: int = 0
    sample_index: int | None = None  # first sample's index in its run
    run_index: int = 0  # replay timeline run; live streams use run 0
    session_generation: int | None = None  # live socket lifecycle; separate from source generation
    # Live only: samples received in this session before this frame, counting
    # frames later dropped on overflow, so a consumer can see where a gap is.
    stream_offset: int | None = None


class IQStream:
    """Own connection to SDRconnect; yields IQFrame via frames().

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
        # After attachment or an SDRconnect (re)launch, readiness applies its
        # shared minimum and spaces stream enables across clients.
        self._startup_grace_s = (
            startup_grace_s if startup_grace_s is not None else 5.0 + 0.5 * (next(_INSTANCES) % 4)
        )
        self._backoff_start, self._backoff_max = backoff
        self._ws = None
        self._stopped = True
        # Per-connection, like spectrum_enable on the control socket -- reset
        # to False whenever the connection drops so a reconnect re-sends it.
        self._enabled = False
        self._queue: asyncio.Queue[IQFrame] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._reader_task: asyncio.Task | None = None
        self._generation = 0
        self._dropped_count = 0
        self._dropped_samples = 0
        self._received_samples = 0

    @property
    def generation(self) -> int:
        """Live-session generation attached to every emitted frame."""
        return self._generation

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def dropped_samples(self) -> int:
        return self._dropped_samples

    @property
    def backlog_frames(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        self._clear_queue()
        self._generation += 1
        self._dropped_count = 0
        self._dropped_samples = 0
        self._received_samples = 0
        self._stopped = False
        self._reader_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._generation += 1
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        self._ws = None
        self._clear_queue()

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def enable(self, on: bool) -> None:
        self._enabled = on
        if self._ws is not None:
            await self._send(on)

    async def _run(self) -> None:
        backoff = self._backoff_start
        while not self._stopped:
            try:
                # max_size=None: frames are ~2 MB of raw IQ at 10 MS/s, far
                # past the websockets default 1 MiB message-size limit.
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
                logger.warning("IQ stream connection error: %s", type(exc).__name__)
            finally:
                self._ws = None
                if not self._stopped:
                    readiness.mark_down(self._host, self._port)
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes) and message[:2] == _IQ_HEADER:
            if self._queue.full():
                # Keep the freshest live data; an old backlog is less useful
                # than a current frame and must remain memory bounded.
                with contextlib.suppress(asyncio.QueueEmpty):
                    dropped = self._queue.get_nowait()
                    self._dropped_count += 1
                    self._dropped_samples += len(dropped.data) // 4
            data = message[2:]
            self._queue.put_nowait(
                IQFrame(
                    data,
                    time.time(),
                    session_generation=self._generation,
                    stream_offset=self._received_samples,
                )
            )
            self._received_samples += len(data) // 4
        # anything else (text, other binary payload types) is ignored here

    async def _send(self, on: bool) -> None:
        if self._ws is None:
            return
        msg = {"event_type": "iq_stream_enable", "property": "", "value": "true" if on else "false"}
        try:
            await self._ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass  # reconnect loop will resend enable on the next connection

    async def frames(self):
        while True:
            yield await self._queue.get()
