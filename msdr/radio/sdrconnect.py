"""WebSocket adapter to a live SDRconnect instance (GUI or headless)."""

import asyncio
import contextlib
import json
import logging
import time

from websockets.exceptions import ConnectionClosed

from msdr.radio import readiness
from msdr.radio.protocol import ConnectionChanged, Event, PropertyChanged, SpectrumFrame
from msdr.radio.registry import REGISTRY, decode, encode

logger = logging.getLogger(__name__)

_UNKNOWN_PROP_TIMEOUT_S = 1.5
# Binary frame header for payload type 3 (spectrum), 2-byte LE.
_SPECTRUM_HEADER = b"\x03\x00"


class SDRconnectAdapter:
    def __init__(
        self,
        host: str,
        port: int,
        backoff: tuple[float, float] = (0.5, 10.0),
        startup_grace_s: float = 3.0,
    ):
        self._host = host
        self._port = port
        # After attachment or an SDRconnect (re)launch: wait at least the
        # shared readiness minimum before reporting connected -- the
        # service reads state and enables spectrum right on ConnectionChanged.
        self._startup_grace_s = startup_grace_s
        self._backoff_start, self._backoff_max = backoff
        self.connected = False
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._stopped = False
        self._subscribers: list[asyncio.Queue[Event]] = []
        # Pending get() futures keyed by property name, so a second concurrent
        # get on the same prop awaits the same in-flight request.
        self._pending: dict[str, asyncio.Future] = {}
        # Recreated each backoff wait so a stale retry_now() from while
        # connected can't be mistaken for one during the next wait.
        self._retry_event = asyncio.Event()
        # monotonic() deadline of the next reconnect attempt; None whenever
        # not waiting out a backoff (connected, or mid-attempt).
        self.retry_deadline: float | None = None
        # Not a property (spectrum_enable/get_property are silently ignored
        # by SDRconnect for it) -- tracked here so set_spectrum_stream is
        # idempotent instead of re-sending the command every call.
        self._spectrum_on = False

    async def start(self) -> None:
        self._stopped = False
        self._reader_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        self.connected = False
        self._ws = None

    async def _run(self) -> None:
        backoff = self._backoff_start
        while not self._stopped:
            try:
                async with readiness.ready_connection(
                    self._host, self._port, self._startup_grace_s
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = self._backoff_start
                    self._emit(ConnectionChanged(True, time.monotonic()))
                    async for message in ws:
                        self._on_message(message)
            except Exception as exc:
                # Anything the connect-and-read loop can raise -- a dropped
                # connection (ConnectionClosed/OSError), but also a wrong port
                # serving plain HTTP or a WSS/ACL rejection from SDRconnect
                # 1.0.10 (InvalidHandshake/InvalidStatus/InvalidMessage) --
                # must not kill this task, or there's no ConnectionChanged(False)
                # and no reconnect ever again (F5). Fall through to backoff.
                logger.warning("SDRconnect connection error: %s", type(exc).__name__)
            finally:
                self._ws = None
                if not self._stopped:
                    readiness.mark_down(self._host, self._port)
                if self.connected:
                    self.connected = False
                    # The stream is per connection -- it's already gone; don't
                    # skip re-sending spectrum_enable on the next connection.
                    self._spectrum_on = False
                    # A deliberate stop() is not a "disconnect" -- only report
                    # ConnectionChanged(False) for connections lost unexpectedly.
                    if not self._stopped:
                        self._emit(ConnectionChanged(False, time.monotonic()))
                    self._fail_pending(ConnectionError("disconnected"))
            if self._stopped:
                break
            self.retry_deadline = time.monotonic() + backoff
            self._retry_event = asyncio.Event()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._retry_event.wait(), backoff)
            woke_early = self._retry_event.is_set()
            self.retry_deadline = None
            backoff = self._backoff_start if woke_early else min(backoff * 2, self._backoff_max)

    def retry_now(self) -> None:
        """Wake a backoff wait immediately and reset it to retry right away."""
        self._retry_event.set()

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            if message[:2] == _SPECTRUM_HEADER:
                self._emit(SpectrumFrame(message[2:], time.monotonic()))
            return  # other payload types (IQ/audio) are never enabled; ignore
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        event_type = msg.get("event_type")
        prop = msg.get("property")
        if event_type == "property_changed":
            value = decode(prop, msg.get("value"))
            device = msg.get("device") or "primary"
            self._emit(PropertyChanged(prop, value, device, time.monotonic()))
        elif event_type == "get_property_response":
            # The control adapter's requests address the primary receiver.
            # A secondary-tuner response has the same property name but is
            # not a valid answer to that pending read.
            if (msg.get("device") or "primary") != "primary":
                return
            fut = self._pending.get(prop)
            if fut is not None and not fut.done():
                fut.set_result(decode(prop, msg.get("value")))

    def _emit(self, event: Event) -> None:
        for q in self._subscribers:
            q.put_nowait(event)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def get(self, prop: str) -> object:
        if not self.connected:
            raise ConnectionError("not connected")
        fut = self._pending.get(prop)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            fut.add_done_callback(lambda f, p=prop: self._pending.pop(p, None))
            self._pending[prop] = fut
            await self._send({"event_type": "get_property", "property": prop, "value": ""})
        spec = REGISTRY.get(prop)
        timeout = spec.timeout_s if spec is not None else _UNKNOWN_PROP_TIMEOUT_S
        # Shield so a timeout on this call doesn't cancel a future another
        # concurrent get() on the same property is also awaiting.
        # asyncio.timeout, not wait_for: on Python 3.11 wait_for swallows the
        # *caller's* cancellation when fut completes at the same moment, so a
        # shutdown that cancels the event consumer mid-_seed() was lost and
        # the consumer waited forever (CPython gh-86296, fixed in 3.12).
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(fut)
        except TimeoutError:
            # The shield protects fut from the timeout's cancellation, so a lost
            # response would otherwise leave it in _pending forever -- every
            # later get() on this prop would reuse it and never resend the
            # request. Clear it (only if it's still ours) so the next get()
            # issues a fresh get_property.
            if self._pending.get(prop) is fut:
                self._pending.pop(prop, None)
            raise

    async def set(self, prop: str, value: object) -> None:
        await self._send(
            {"event_type": "set_property", "property": prop, "value": encode(prop, value)}
        )

    async def apply_profile(self, name: str) -> None:
        await self._send({"event_type": "apply_device_profile", "property": "", "value": name})

    async def set_spectrum_stream(self, on: bool) -> None:
        # A dedicated command, not a property (spec Sec7) -- "true"/"false" as
        # strings, matching how the real GUI sends it.
        if on == self._spectrum_on:
            return
        await self._send(
            {"event_type": "spectrum_enable", "property": "", "value": "true" if on else "false"}
        )
        self._spectrum_on = on

    async def _send(self, msg: dict) -> None:
        if not self.connected or self._ws is None:
            raise ConnectionError("not connected")
        try:
            await self._ws.send(json.dumps(msg))
        except ConnectionClosed as e:
            # send() can race a close that the reader task hasn't observed yet;
            # translate to the protocol's ConnectionError so callers only ever
            # need to catch one exception type.
            raise ConnectionError("connection closed") from e

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)
