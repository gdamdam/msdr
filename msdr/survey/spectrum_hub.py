"""SpectrumHub: reference-counted owner of the spectrum stream (control
connection, `spectrum_enable`). The stream is per-connection and stateful
(docs/research/2026-09-22-sdrconnect-websocket-api.md Sec7), so two
independent consumers -- the scanner's spectrum source and Survey -- can't
each call `set_spectrum_stream` without stepping on each other; this hub
enables it once for whoever needs it and fans every frame out to each
consumer's own queue, same shape as DecoderManager owning the audio stream
for its subscribers.
"""

import asyncio
import contextlib
import logging

from msdr.decoder.backends import reap_cancelled
from msdr.radio.protocol import ConnectionChanged, SpectrumFrame

logger = logging.getLogger(__name__)

# A consumer that stops draining its queue (a dead connection, a slow test)
# must not build up memory forever -- drop the oldest frame instead, same
# backpressure shape as BandDecoder's own bounded queues.
_QUEUE_MAXSIZE = 50


class SpectrumHub:
    def __init__(self, adapter) -> None:
        self._adapter = adapter
        self.connected = bool(getattr(adapter, "connected", True))

        self._queues: list[asyncio.Queue[SpectrumFrame]] = []
        self._sub_queue: asyncio.Queue | None = None
        self._run_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def consumers(self) -> int:
        return len(self._queues)

    # -- runtime: one subscription to the adapter, fanned out to consumers --

    async def start(self) -> None:
        self._sub_queue = self._adapter.subscribe()
        self._run_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._run_task is not None:
            self._run_task.cancel()
            await reap_cancelled(self._run_task)
            self._run_task = None
        if self._sub_queue is not None:
            self._adapter.unsubscribe(self._sub_queue)
            self._sub_queue = None
        for task in list(self._bg_tasks):
            task.cancel()
            await reap_cancelled(task)
        self._bg_tasks.clear()
        self._queues.clear()

    async def _run(self) -> None:
        assert self._sub_queue is not None
        try:
            while True:
                ev = await self._sub_queue.get()
                if isinstance(ev, SpectrumFrame):
                    for q in self._queues:
                        if q.qsize() >= _QUEUE_MAXSIZE:
                            with contextlib.suppress(asyncio.QueueEmpty):
                                q.get_nowait()  # drop oldest, keep the stream live
                        q.put_nowait(ev)
                elif isinstance(ev, ConnectionChanged):
                    self.connected = ev.connected
                    if ev.connected and self._queues:
                        # SDRconnect drops the stream on disconnect even though
                        # consumers are still held -- the reconnect is a fresh
                        # connection that starts with the stream off again.
                        self._spawn(self._adapter.set_spectrum_stream(True))
        except asyncio.CancelledError:
            raise

    # -- consumer API ---------------------------------------------------

    def acquire(self) -> asyncio.Queue:
        q: asyncio.Queue[SpectrumFrame] = asyncio.Queue()
        first = not self._queues
        self._queues.append(q)
        if first:
            self._spawn(self._adapter.set_spectrum_stream(True))
        return q

    def release(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)
        if not self._queues:
            self._spawn(self._adapter.set_spectrum_stream(False))

    # -- internals --------------------------------------------------------

    def _spawn(self, coro) -> None:
        # acquire()/release() are sync (plan's own signature), so the
        # enable/disable call is fire-and-forget -- mirrors FakeAdapter's own
        # _spawn for the same reason (background sets shouldn't block a
        # synchronous caller).
        async def _guarded() -> None:
            with contextlib.suppress(ConnectionError):
                await coro

        task = asyncio.create_task(_guarded())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
