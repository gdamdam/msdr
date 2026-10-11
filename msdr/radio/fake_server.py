"""In-process fake SDRconnect server: same wire protocol, with quirks for testing.

Speaks the same JSON-over-WebSocket envelope as a real SDRconnect instance so
SDRconnectAdapter can be exercised without the real app. The quirk knobs exist
because the real server is a little sloppy (bare numbers instead of strings,
no response to some gets) and the adapter has to cope with that.
"""

import asyncio
import contextlib
import json
import random
from collections import defaultdict

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from msdr.radio.registry import decode, encode

_SPECTRUM_HEADER = b"\x03\x00"
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


def _synthesize_spectrum_bins(*, center: int, sample_rate: int, peaks: dict[int, int]) -> bytes:
    bins = [
        max(0, min(255, _SPECTRUM_FLOOR + random.randint(-3, 3))) for _ in range(_SPECTRUM_BINS)
    ]
    bins[_SPECTRUM_DC_BIN] = _SPECTRUM_DC_LEVEL
    for hz, level in peaks.items():
        idx = _bin_for_hz(hz, center=center, sample_rate=sample_rate)
        if idx is not None:
            bins[idx] = max(0, min(255, level))
    return bytes(bins)


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


class FakeSDRconnect:
    def __init__(self, port: int = 0):
        self._port = port
        self.state: dict[str, object] = _seed_state()
        self.profiles: dict[str, dict[str, object]] = {}
        # {hz: level} synthetic carriers added to the spectrum stream on top
        # of the noise floor and the DC spike -- see _synthesize_spectrum_bins.
        self.spectrum_peaks: dict[int, int] = {}
        # Quirks.
        self.numeric_values = False
        self.echo_unchanged = True
        self.drop_echo: set[str] = set()
        self.drop_get: set[str] = set()
        self.delay_s = 0.0
        self._server = None
        self._clients: set = set()
        # Per-connection: the stream ends when the socket closes (spec Sec7).
        self._spectrum_tasks: dict[object, asyncio.Task] = {}
        # Per-property queues of values seen in set_property/apply_device_profile,
        # consumed by wait_for_set. A queue (not a single future) means a set that
        # arrives before wait_for_set is called is still observed, not lost to a race.
        self._sets: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        # S3 replay contract: total set_property/apply_device_profile *messages*
        # dispatched (one per message, not per property inside a profile) --
        # the zero-live-writes gate for replay tuning/listening/decoding.
        self._set_count = 0

    @property
    def port(self) -> int:
        return self._port

    def set_count(self) -> int:
        """Total set_property + apply_device_profile events seen (S3 contract):
        tests assert this stays 0 while only a replay is tuned/listened to."""
        return self._set_count

    async def start(self, *, reuse_address: bool = False) -> int:
        # reuse_address=False (SO_REUSEADDR off) by default: if the port is
        # already bound (e.g. real SDRconnect on 5454) this fails loudly
        # instead of silently sharing the address, which is how the fake once
        # shadowed a running SDRconnect instance. A caller that is itself
        # restarting on a port it just released (its own connections briefly
        # lingering in TIME_WAIT, not a conflict with another process) may
        # pass reuse_address=True to skip that wait.
        self._server = await serve(
            self._handle, "127.0.0.1", self._port, reuse_address=reuse_address
        )
        self._port = self._server.sockets[0].getsockname()[1]
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def external_change(self, prop: str, value: object) -> None:
        """Simulate the user changing a value from SDRconnect's own GUI."""
        self.state[prop] = value
        await self._broadcast_property_changed(prop, value)

    async def wait_for_set(self, prop: str, timeout: float = 1.0) -> object:
        return await asyncio.wait_for(self._sets[prop].get(), timeout)

    async def _handle(self, ws) -> None:
        self._clients.add(ws)
        try:
            async for message in ws:
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                if isinstance(message, bytes):
                    continue
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(ws, msg)
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            await self._stop_spectrum_stream(ws)

    async def _dispatch(self, ws, msg: dict) -> None:
        event_type = msg.get("event_type")
        prop = msg.get("property")
        if event_type == "get_property":
            await self._handle_get(ws, prop)
        elif event_type == "set_property":
            self._set_count += 1
            await self._handle_set(prop, msg.get("value"))
        elif event_type == "apply_device_profile":
            self._set_count += 1
            await self._handle_apply_profile(msg.get("value"))
        elif event_type == "spectrum_enable":
            # Not a property (spec Sec7): set_property/get_property on it are
            # silently ignored on the real server, so this is its own branch.
            if msg.get("value") == "true":
                await self._start_spectrum_stream(ws)
            else:
                await self._stop_spectrum_stream(ws)

    async def _handle_get(self, ws, prop: str) -> None:
        if prop in self.drop_get:
            return
        await ws.send(
            json.dumps(
                {
                    "event_type": "get_property_response",
                    "property": prop,
                    "value": self._wire_value(prop),
                }
            )
        )

    async def _handle_set(self, prop: str, raw_value: object) -> None:
        value = decode(prop, raw_value)
        changed = self.state.get(prop) != value
        self.state[prop] = value
        self._sets[prop].put_nowait(value)
        if prop in self.drop_echo:
            return
        if not changed and not self.echo_unchanged:
            return
        await self._broadcast_property_changed(prop, value)

    async def _handle_apply_profile(self, name: str) -> None:
        profile = self.profiles.get(name)
        if profile is None:
            return
        for prop, value in profile.items():
            self.state[prop] = value
            self._sets[prop].put_nowait(value)
            await self._broadcast_property_changed(prop, value)

    def _wire_value(self, prop: str) -> object:
        value = self.state.get(prop)
        if self.numeric_values:
            return value
        return encode(prop, value)

    async def _start_spectrum_stream(self, ws) -> None:
        if ws in self._spectrum_tasks:
            return  # idempotent, mirrors the adapter's own idempotent enable
        self._spectrum_tasks[ws] = asyncio.create_task(self._spectrum_loop(ws))

    async def _stop_spectrum_stream(self, ws) -> None:
        task = self._spectrum_tasks.pop(ws, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _spectrum_loop(self, ws) -> None:
        interval = 1 / _SPECTRUM_FPS
        try:
            while True:
                center = int(self.state.get("device_center_frequency", 0))
                sample_rate = int(self.state.get("device_sample_rate", 0))
                bins = _synthesize_spectrum_bins(
                    center=center, sample_rate=sample_rate, peaks=self.spectrum_peaks
                )
                with contextlib.suppress(ConnectionClosed):
                    await ws.send(_SPECTRUM_HEADER + bins)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _broadcast_property_changed(self, prop: str, value: object) -> None:
        msg = json.dumps(
            {
                "event_type": "property_changed",
                "property": prop,
                "value": self._wire_value(prop),
                "device": "primary",
            }
        )
        for client in list(self._clients):
            with contextlib.suppress(ConnectionClosed):
                await client.send(msg)
