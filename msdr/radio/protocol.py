"""Radio adapter contract: events and the interface real/fake backends implement."""

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PropertyChanged:
    property: str
    value: object
    device: str
    at: float


@dataclass(frozen=True)
class ConnectionChanged:
    connected: bool
    at: float


@dataclass(frozen=True)
class SpectrumFrame:
    """One spectrum-stream frame: 512 uint8 bins (payload type 3), most recent
    last. Bin i <-> center - sample_rate/2 + i * sample_rate/512 (FFT order: bin
    256 is the centre; measured against an FFT of the IQ stream 2026-09-26) --
    see docs/research/2026-09-22-sdrconnect-websocket-api.md Sec12."""

    bins: bytes
    at: float


Event = PropertyChanged | ConnectionChanged | SpectrumFrame


class RadioAdapter(Protocol):
    connected: bool

    async def get(self, prop: str) -> object:
        """Return the decoded current value. Raises TimeoutError or ConnectionError."""
        ...

    async def set(self, prop: str, value: object) -> None:
        """Fire-and-forget send. Raises ConnectionError if disconnected."""
        ...

    async def apply_profile(self, name: str) -> None: ...

    async def set_spectrum_stream(self, on: bool) -> None:
        """Enable/disable the binary spectrum stream on this connection
        (idempotent). Frames arrive as SpectrumFrame events via subscribe()."""
        ...

    def subscribe(self) -> asyncio.Queue[Event]:
        """Each subscriber gets its own queue."""
        ...

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None: ...
