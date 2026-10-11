import asyncio
import time
from dataclasses import dataclass, field

import pytest

from msdr.radio.protocol import ConnectionChanged, SpectrumFrame


class FakeAdapter:
    """RadioAdapter double for survey tests: dict-backed get/set, records
    every set_spectrum_stream() call, and lets a test push SpectrumFrame /
    ConnectionChanged events directly onto the one subscription SpectrumHub
    holds -- no socket, no background frame generator (unlike
    msdr/engine/fake_adapter.py, which the scanner tests own)."""

    def __init__(self, state: dict[str, object] | None = None):
        self.state = dict(state or {})
        self.connected = True
        self.sets: list[tuple[str, object]] = []
        self.spectrum_calls: list[bool] = []
        self._subscribers: list[asyncio.Queue] = []

    async def get(self, prop: str) -> object:
        return self.state.get(prop)

    async def set(self, prop: str, value: object) -> None:
        self.state[prop] = value
        self.sets.append((prop, value))

    async def set_spectrum_stream(self, on: bool) -> None:
        self.spectrum_calls.append(on)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def push_frame(self, bins: bytes, at: float | None = None) -> None:
        # The real adapter stamps frames with time.monotonic(); mirror it.
        ev = SpectrumFrame(bins, at if at is not None else time.monotonic())
        for q in self._subscribers:
            q.put_nowait(ev)

    def push_connection(self, connected: bool) -> None:
        self.connected = connected
        ev = ConnectionChanged(connected, time.time())
        for q in self._subscribers:
            q.put_nowait(ev)


class FakeConfirmed:
    """ConfirmedState double: subscribe()/unsubscribe() plus a test-only
    emit(), same shape as tests/decoder/conftest.py's own -- Survey only
    reads .props and follows StateChanged, not the full ConfirmedState."""

    def __init__(self, props: dict[str, object] | None = None):
        self.props: dict[str, object] = dict(props or {})
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, sc) -> None:
        for q in self._subs:
            q.put_nowait(sc)


@dataclass
class FakeSurveySettings:
    """SurveySettings double (Stream B owns the real pydantic model)."""

    threshold_db: float = 8.0
    occupancy_windows_s: tuple[int, int] = (60, 600)
    spectrum_fps: int = 10
    presets: list = field(default_factory=list)
    peak_threshold_db: float = 10.0
    center_exclusion_enabled: bool = False
    marker_hold_s: float = 15.0
    label_ttl_s: int = 120
    dimensions: list = field(
        default_factory=lambda: ["activity", "bandwidth", "bandplan", "catalog"]
    )


_INITIAL_CENTER_HZ = 153_000_000
_INITIAL_SAMPLE_RATE_HZ = 8_000_000
_INITIAL_VFO_HZ = 153_000_000


@pytest.fixture
def adapter():
    return FakeAdapter(
        {
            "device_center_frequency": _INITIAL_CENTER_HZ,
            "device_sample_rate": _INITIAL_SAMPLE_RATE_HZ,
            "device_vfo_frequency": _INITIAL_VFO_HZ,
            "spectrum_ref_level": -20,
            "spectrum_base": -145,
        }
    )


@pytest.fixture
def confirmed():
    return FakeConfirmed(
        {
            "device_center_frequency": _INITIAL_CENTER_HZ,
            "device_sample_rate": _INITIAL_SAMPLE_RATE_HZ,
            "device_vfo_frequency": _INITIAL_VFO_HZ,
        }
    )


@pytest.fixture
def settings():
    return FakeSurveySettings()


@pytest.fixture
def published():
    return []
