"""The live radio behind the IQSource contract (routing and status only).

A thin wrapper over what the service already runs -- the SDRconnect
adapter, the SpectrumHub and the IQStream factory -- so replay and live can
be addressed the same way. It changes nothing about how the live path works:
spectrum queues come straight from the hub, IQ streams straight from the
factory (their frames keep the default generation 0 / sample_index None).

The only state it adds is the descriptor: centre/rate as last reported by
the adapter, and a generation that bumps (Discontinuity "retune") whenever
either changes. Feed adapter events to handle_event(); start() does that
from an own adapter subscription.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable

from msdr.contracts.source import SourceDescriptor
from msdr.radio.protocol import PropertyChanged
from msdr.source.base import IQStreamLike, ListenerSet, SourceEvent, SourceListener

logger = logging.getLogger(__name__)

_CENTER_PROP = "device_center_frequency"
_RATE_PROP = "device_sample_rate"


def _as_int(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class LiveSource:
    def __init__(
        self,
        *,
        adapter,
        hub,
        iq_stream_factory: Callable[[], IQStreamLike] | None,
        center_hz: int | None = None,
        sample_rate_hz: int | None = None,
        source_id: str = "live:sdrconnect",
    ) -> None:
        self.source_id = source_id
        self._adapter = adapter
        self._hub = hub
        self._iq_factory = iq_stream_factory
        self._listeners = ListenerSet()
        self._desc: SourceDescriptor | None = None
        self._pending_center, self._pending_rate = center_hz, sample_rate_hz
        self._update_span(center_hz, sample_rate_hz, bump=False)
        self._task: asyncio.Task | None = None

    # -- IQSource -------------------------------------------------------------

    def descriptor(self) -> SourceDescriptor | None:
        """None until the adapter has reported both centre and rate."""
        return self._desc

    @property
    def generation(self) -> int:
        return self._desc.generation if self._desc is not None else 0

    def iq_stream(self) -> IQStreamLike:
        if self._iq_factory is None:
            raise RuntimeError("live IQ stream not wired up")
        return self._iq_factory()

    def acquire_spectrum(self) -> asyncio.Queue:
        return self._hub.acquire()

    def release_spectrum(self, q: asyncio.Queue) -> None:
        self._hub.release(q)

    def add_listener(self, cb: SourceListener) -> None:
        self._listeners.add(cb)

    def remove_listener(self, cb: SourceListener) -> None:
        self._listeners.remove(cb)

    def status(self) -> dict:
        d = self._desc
        return {
            "source_id": self.source_id,
            "mode": "live",
            "center_hz": d.center_hz if d else self._pending_center,
            "sample_rate_hz": d.sample_rate_hz if d else self._pending_rate,
            "generation": self.generation,
        }

    # -- adapter events ------------------------------------------------------

    def handle_event(self, event: object) -> None:
        """Track centre/rate from an adapter event; other events are ignored."""
        if not isinstance(event, PropertyChanged):
            return
        value = _as_int(event.value)
        if value is None or value <= 0:
            return
        if event.property == _CENTER_PROP:
            self._update_span(value, None, bump=True)
        elif event.property == _RATE_PROP:
            self._update_span(None, value, bump=True)

    def _update_span(self, center_hz: int | None, rate_hz: int | None, *, bump: bool) -> None:
        if center_hz is not None:
            self._pending_center = center_hz
        if rate_hz is not None:
            self._pending_rate = rate_hz
        center, rate = self._pending_center, self._pending_rate
        if not center or not rate:
            return
        if self._desc is None:
            self._desc = SourceDescriptor(
                source_id=self.source_id, mode="live", sample_rate_hz=rate, center_hz=center
            )
            return
        if (center, rate) == (self._desc.center_hz, self._desc.sample_rate_hz):
            return
        desc = self._desc.model_copy(update={"center_hz": center, "sample_rate_hz": rate})
        if bump:
            desc = desc.bump("retune")
            desc = desc.model_copy(update={"discontinuities": desc.discontinuities[-32:]})
        self._desc = desc
        if bump:
            self._listeners.notify(
                SourceEvent(
                    kind="generation",
                    generation=desc.generation,
                    sample_index=0,
                    run_index=0,
                    descriptor=desc,
                    discontinuity=desc.discontinuities[-1],
                )
            )

    async def start(self) -> None:
        """Follow the adapter's events from an own subscription."""
        if self._task is None:
            queue = self._adapter.subscribe()
            self._task = asyncio.create_task(self._follow(queue))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _follow(self, queue: asyncio.Queue) -> None:
        try:
            while True:
                self.handle_event(await queue.get())
        finally:
            self._adapter.unsubscribe(queue)
