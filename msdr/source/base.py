"""The source contract every IQ producer (live radio, replay) exposes.

A consumer (virtual receiver, decoders) only talks to an ``IQSource``: it
reads IQFrames from ``iq_stream()``, spectrum frames from
``acquire_spectrum()`` and learns about clock breaks (seek, loop, retune,
new run) from ``SourceEvent``s delivered to its listener. Frames carry the
source ``generation``; anything tagged with an older one is stale.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from msdr.contracts.source import Discontinuity, SourceDescriptor
from msdr.recorder.iq_stream import IQFrame

logger = logging.getLogger(__name__)

SourceEventKind = Literal["generation", "run", "end", "position"]


class IQStreamLike(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def enable(self, on: bool) -> None: ...
    def frames(self) -> AsyncIterator[IQFrame]: ...


@dataclass(frozen=True)
class SourceEvent:
    """Emitted synchronously on the event-loop thread."""

    kind: SourceEventKind
    generation: int
    sample_index: int  # position in the *current run*
    run_index: int  # 0 for live
    descriptor: SourceDescriptor  # after the change
    discontinuity: Discontinuity | None = None


SourceListener = Callable[[SourceEvent], None]


@runtime_checkable
class IQSource(Protocol):
    source_id: str

    def descriptor(self) -> SourceDescriptor: ...
    def iq_stream(self) -> IQStreamLike: ...
    def acquire_spectrum(self) -> asyncio.Queue: ...
    def release_spectrum(self, q: asyncio.Queue) -> None: ...
    def add_listener(self, cb: SourceListener) -> None: ...
    def remove_listener(self, cb: SourceListener) -> None: ...


class ListenerSet:
    """Listener registry shared by the source implementations.

    Callbacks run inline on the caller (the loop thread); one that raises is
    logged and skipped so a broken consumer can never stop the pump.
    """

    def __init__(self) -> None:
        self._listeners: list[SourceListener] = []

    def add(self, cb: SourceListener) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove(self, cb: SourceListener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def notify(self, event: SourceEvent) -> None:
        for cb in list(self._listeners):  # a callback may (un)register
            try:
                cb(event)
            except Exception:  # noqa: BLE001 -- never raise into the pump
                logger.exception("source listener failed on %s event", event.kind)
