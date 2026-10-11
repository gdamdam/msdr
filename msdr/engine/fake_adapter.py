"""In-memory RadioAdapter for engine tests -- no network, no real event loop hop.

`set` applies asynchronously after `delay_s` (optionally dropping the echo or
failing outright) so restore logic -- timeouts, echo-less confirmation,
outside interference -- can be exercised deterministically and fast.
"""

import asyncio
import random
import time

from msdr.radio.protocol import Event, PropertyChanged, RadioAdapter, SpectrumFrame

_SPECTRUM_BINS = 512
_SPECTRUM_FPS = 20
_SPECTRUM_FLOOR = 80
_SPECTRUM_DC_BIN = 256
_SPECTRUM_DC_LEVEL = 200


def _bin_for_hz(hz: int, *, center: int, sample_rate: int) -> int | None:
    """Inverse of bin i -> center - sample_rate/2 + i * sample_rate/512 (FFT order)."""
    if sample_rate <= 0:
        return None
    bin_hz = sample_rate / _SPECTRUM_BINS
    idx = round((hz - center + sample_rate / 2) / bin_hz)
    if 0 <= idx < _SPECTRUM_BINS:
        return idx
    return None


def _seed_state() -> dict[str, object]:
    return {
        "device_vfo_frequency": 145_800_000,
        "device_center_frequency": 145_800_000,
        "device_sample_rate": 2_000_000,
        "demodulator": "NFM",
        "filter_bandwidth": 12_500,
        "lna_state": 3,
        "active_antenna": "Antenna B",
        "squelch_enable": False,
        "squelch_threshold": -100.0,
        "agc_enable": True,
        "agc_threshold": -30.0,
        "noise_reduction_enable": False,
        "noise_reduction_strength": 0,
        "can_control": True,
        "started": True,
        "api_version": "1.0.3",
        "valid_antennas": "Antenna A,Antenna B,Antenna C",
        "signal_power": -85.0,
        "signal_snr": 12.0,
        "overload": False,
    }


class FakeAdapter(RadioAdapter):
    def __init__(self) -> None:
        self.state: dict[str, object] = _seed_state()
        self.profiles: dict[str, dict[str, object]] = {}
        self.drop_echo: set[str] = set()
        self.fail_props: set[str] = set()
        self.timeout_get: set[str] = set()
        self.delay_s: float = 0.0
        self.connected = True
        self.calls: list[tuple] = []
        # {hz: level} synthetic carriers on top of the noise floor / DC spike.
        self.spectrum_peaks: dict[int, int] = {}
        self._spectrum_on = False
        self._spectrum_task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def get(self, prop: str) -> object:
        self.calls.append(("get", prop))
        if not self.connected:
            raise ConnectionError("not connected")
        if prop in self.timeout_get:
            raise TimeoutError("no response")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.state.get(prop)

    async def set(self, prop: str, value: object) -> None:
        self.calls.append(("set", prop, value))
        if not self.connected:
            raise ConnectionError("not connected")
        self._spawn(self._apply_set(prop, value))

    async def _apply_set(self, prop: str, value: object) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if prop in self.fail_props:
            return  # value not stored, nothing emitted
        self.state[prop] = value
        if prop in self.drop_echo:
            return
        self._emit(PropertyChanged(prop, value, "primary", time.monotonic()))

    async def apply_profile(self, name: str) -> None:
        self.calls.append(("apply_profile", name))
        if not self.connected:
            raise ConnectionError("not connected")
        profile = self.profiles.get(name)
        if not profile:
            return
        for prop, value in profile.items():
            self.state[prop] = value
            self._emit(PropertyChanged(prop, value, "primary", time.monotonic()))

    async def set_spectrum_stream(self, on: bool) -> None:
        self.calls.append(("set_spectrum_stream", on))
        if not self.connected:
            raise ConnectionError("not connected")
        if on == self._spectrum_on:
            return
        self._spectrum_on = on
        if on:
            self._spectrum_task = asyncio.create_task(self._spectrum_loop())
        elif self._spectrum_task is not None:
            self._spectrum_task.cancel()
            self._spectrum_task = None

    def emit_spectrum(self) -> None:
        """One synthetic frame from the current spectrum_peaks + device
        center/sample_rate state -- for tests that want a single frame
        without waiting on the ~20fps timer."""
        center = int(self.state.get("device_center_frequency", 0))
        sample_rate = int(self.state.get("device_sample_rate", 0))
        bins = [
            max(0, min(255, _SPECTRUM_FLOOR + random.randint(-3, 3))) for _ in range(_SPECTRUM_BINS)
        ]
        bins[_SPECTRUM_DC_BIN] = _SPECTRUM_DC_LEVEL
        for hz, level in self.spectrum_peaks.items():
            idx = _bin_for_hz(hz, center=center, sample_rate=sample_rate)
            if idx is not None:
                bins[idx] = max(0, min(255, level))
        self._emit(SpectrumFrame(bytes(bins), time.monotonic()))

    async def _spectrum_loop(self) -> None:
        try:
            while self.connected and self._spectrum_on:
                self.emit_spectrum()
                await asyncio.sleep(1 / _SPECTRUM_FPS)
        except asyncio.CancelledError:
            raise

    async def external_change(self, prop: str, value: object) -> None:
        """Simulate the user changing a value from SDRconnect's own GUI."""
        self.state[prop] = value
        self._emit(PropertyChanged(prop, value, "primary", time.monotonic()))

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _emit(self, event: Event) -> None:
        for q in self._subscribers:
            q.put_nowait(event)
