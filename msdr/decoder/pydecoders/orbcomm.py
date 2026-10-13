"""Native Orbcomm downlink decoder (137.2-138 MHz, 4800 bps SDPSK, ~25 kHz channels).

Signal chain follows the open-source fbieberly/ORBCOMM-receiver approach: carrier
recovery by squaring the (BPSK-like) signal, root-raised-cosine matched filter,
symbol timing by maximising the differential eye, differential decode, then a
600-bit frame search on the 24-bit sync word 0x65A8F9 and a 16-bit Fletcher
checksum.

Honesty note: the demodulation and checksum gating are generic; the *packet
layout* below (type codes, field offsets, ephemeris scaling) is a best-effort
reconstruction written without a reference capture. It is covered only by
synthetic round trips, so field contents of real frames are unverified. The
ephemeris position is therefore only reported when it is physically plausible
for a ~750 km LEO orbit.
"""

from __future__ import annotations

import asyncio
import math
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, LoopEventQueue
from msdr.decoder.pydecoders.rs41 import _StreamingResampler

_RATE = 57_600
_BAUD = 4_800
_SPS = _RATE // _BAUD  # 12
_ALPHA = 0.4
_SYNC = 0x65A8F9
_SYNC_BITS = 24
_FRAME_BITS = 600
_BODY_BYTES = (_FRAME_BITS - _SYNC_BITS) // 8  # 72, the last two are the Fletcher-16
_MAX_SYNC_ERRORS = 2  # the checksum, not the sync word, is the real gate
_MIN_SAMPLES = 2 * _FRAME_BITS * _SPS
_TAIL_SAMPLES = (_FRAME_BITS + 64) * _SPS
_OVERLAP = 8 * _SPS  # matched-filter edge settling before a back-to-back frame

PACKET_TYPES = {
    0x01: "sync",
    0x02: "message",
    0x03: "uplink_channel",
    0x04: "downlink_channel",
    0x05: "network",
    0x06: "fill",
    0x07: "ephemeris",
}

_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def rrc_taps(alpha: float = _ALPHA, sps: int = _SPS, span: int = 8) -> np.ndarray:
    n = np.arange(-(span * sps // 2), span * sps // 2 + 1)
    t = n / sps
    h = np.empty(len(t))
    for i, ti in enumerate(t):
        if ti == 0:
            h[i] = 1 - alpha + 4 * alpha / math.pi
        elif abs(abs(ti) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / math.sqrt(2) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * alpha))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * alpha))
            )
        else:
            h[i] = (
                math.sin(math.pi * ti * (1 - alpha))
                + 4 * alpha * ti * math.cos(math.pi * ti * (1 + alpha))
            ) / (math.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return h / math.sqrt(np.sum(h**2))


_RRC = rrc_taps().astype(np.float32)


def fletcher16(data: bytes) -> int:
    c0 = c1 = 0
    for b in data:
        c0 = (c0 + b) % 255
        c1 = (c1 + c0) % 255
    return (c1 << 8) | c0


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """WGS84 ECEF metres -> (lat deg, lon deg, altitude m)."""
    p = math.hypot(x, y)
    if p == 0 and z == 0:
        return 0.0, 0.0, -_A  # Earth's centre: callers reject it as implausible
    lat = math.atan2(z, p * (1 - _E2))
    h = 0.0
    for _ in range(8):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        h = p / max(math.cos(lat), 1e-12) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(math.atan2(y, x)), h


def _peak_freq(x: np.ndarray, fmax: float) -> float:
    """Carrier offset in Hz from the squared signal's spectral line at 2f.

    BPSK data squares away (a^2 = 1), leaving a tone at twice the carrier."""
    n = len(x)
    size = max(65_536, 1 << (n - 1).bit_length())
    spec = np.abs(np.fft.fft((x * x) * np.hanning(n), size))
    freqs = np.fft.fftfreq(size, 1 / _RATE)
    spec[np.abs(freqs) > 2 * fmax] = 0
    k = int(np.argmax(spec))
    delta = 0.0
    if 0 < k < size - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        denom = a - 2 * b + c
        if denom:
            delta = 0.5 * (a - c) / denom
    return 0.5 * (freqs[k] + delta * _RATE / size)


def _mix(x: np.ndarray, hz: float) -> np.ndarray:
    return x * np.exp(-2j * np.pi * hz * np.arange(len(x)) / _RATE).astype(np.complex64)


def parse_frame(body: bytes) -> dict | None:
    """Validate one 72-byte frame body; None when the checksum or type is bad."""
    if len(body) != _BODY_BYTES:
        return None
    if fletcher16(body[:-2]) != int.from_bytes(body[-2:], "big"):
        return None
    ptype = PACKET_TYPES.get(body[0])
    if ptype is None:
        return None
    fields: dict[str, object] = {"packet_type": ptype}
    payload = body[2:-2]
    if ptype != "fill":
        fields["satellite_id"] = body[1]
        fields["entity_id"] = f"ORBCOMM-{body[1]}"
    if ptype == "sync":
        fields["frame_counter"] = int.from_bytes(payload[:2], "big")
    elif ptype == "ephemeris":
        x, y, z = (int.from_bytes(payload[i : i + 4], "big", signed=True) for i in (0, 4, 8))
        fields["position_ecef_m"] = [x, y, z]
        lat, lon, h = ecef_to_geodetic(x, y, z)
        # The field scaling is unverified; publish a position only when it is a
        # plausible LEO one so a wrong layout can't put a satellite on the Map.
        if 200_000 <= h <= 2_000_000:
            fields.update(lat=round(lat, 4), lon=round(lon, 4), altitude_m=round(h))
    elif ptype != "fill":
        fields["payload_hex"] = payload.hex()
    return fields


def _text(fields: dict) -> str:
    text = f"ORBCOMM {fields['packet_type']}"
    if "satellite_id" in fields:
        text += f" sat {fields['satellite_id']}"
    if "lat" in fields:
        text += (
            f" lat {fields['lat']:.2f} lon {fields['lon']:.2f}"
            f" alt {fields['altitude_m'] / 1000:.0f} km"
        )
    return text


class OrbcommBackend:
    """Streaming standalone decoder with start/feed/events/stop interface."""

    decoder_id = "orbcomm"

    def __init__(self, spec=None, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.rate = int(sample_rate_hz)
        self.offset = float(frequency_hz - center_hz)
        self.error = None
        self._running = False
        self._events: asyncio.Queue[DecodeEvent | None] = LoopEventQueue()
        self._resampler = _StreamingResampler(self.rate, self.offset)
        self._buf = np.empty(0, np.complex64)
        self._abs0 = 0  # absolute sample index of _buf[0]
        self._emitted_end = -(10**12)  # absolute sample where the last frame ended

    async def start(self):
        self._running = True

    def feed(self, data: bytes):
        if not self._running or not data:
            return
        if len(data) % 4:
            self.error = "incomplete interleaved int16 IQ pair"
            return
        iq = self._resampler.process(data)
        if not len(iq):
            return
        self._buf = np.concatenate((self._buf, iq))
        if len(self._buf) >= _MIN_SAMPLES:
            self._process()

    def _process(self):
        x = self._buf
        f1 = _peak_freq(x, 6_000)
        y = np.convolve(_mix(x, f1), _RRC, mode="same")
        y = _mix(y, _peak_freq(y, 600))
        best = None
        for phase in range(_SPS):
            z = y[phase::_SPS]
            if len(z) < _FRAME_BITS + 2:
                continue
            d = z[1:] * np.conj(z[:-1])
            score = float(np.sum(np.abs(d.real)))
            if best is None or score > best[0]:
                best = (score, phase, d)
        last_end = None
        if best is not None:
            _, phase, d = best
            # Residual carrier error rotates every differential product equally.
            theta = 0.5 * np.angle(np.sum(d * d))
            flips = ((d * np.exp(-1j * theta)).real < 0).astype(np.uint8)
            last_end = self._scan(flips, phase)
        keep_from = len(x) - _TAIL_SAMPLES
        if last_end is not None:
            keep_from = max(keep_from, last_end - _OVERLAP)
        keep_from = max(0, keep_from)
        self._buf = x[keep_from:].copy()
        self._abs0 += keep_from

    def _scan(self, flips: np.ndarray, phase: int) -> int | None:
        sync = np.array([(_SYNC >> (_SYNC_BITS - 1 - i)) & 1 for i in range(_SYNC_BITS)], np.uint8)
        windows = np.lib.stride_tricks.sliding_window_view(flips, _SYNC_BITS)
        errors = np.count_nonzero(windows != sync, axis=1)
        # A polarity-inverted stream matches the complemented sync word.
        inv_errors = _SYNC_BITS - errors
        cands = sorted(
            [(int(i), False) for i in np.flatnonzero(errors <= _MAX_SYNC_ERRORS)]
            + [(int(i), True) for i in np.flatnonzero(inv_errors <= _MAX_SYNC_ERRORS)]
        )
        last_end = None
        for start, inverted in cands:
            start_sample = phase + (start + 1) * _SPS
            # Frames already emitted from an earlier, overlapping pass.
            if self._abs0 + start_sample < self._emitted_end - 6 * _SPS:
                continue
            if start + _FRAME_BITS > len(flips):
                continue
            bits = flips[start + _SYNC_BITS : start + _FRAME_BITS]
            if inverted:
                bits = bits ^ 1
            fields = parse_frame(np.packbits(bits).tobytes())
            if fields is None:
                continue
            end_abs = self._abs0 + start_sample + _FRAME_BITS * _SPS
            self._emitted_end = end_abs
            last_end = start_sample + _FRAME_BITS * _SPS
            self._events.put_nowait(
                DecodeEvent(
                    ts=time.time(),
                    decoder="orbcomm",
                    kind="message",
                    text=_text(fields),
                    fields=fields,
                    frequency_hz=None,
                    integrity="passed",
                    integrity_note=(
                        "Fletcher-16 verified; packet layout reconstructed, synthetic-only "
                        "validation"
                    ),
                )
            )
        return last_end

    def events(self):
        async def gen():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return gen()

    async def get_event(self):
        return await self._events.get()

    async def stop(self):
        # A short capture can end before _MIN_SAMPLES accumulated; decode what is left.
        if self._running and len(self._buf) >= (_FRAME_BITS + 8) * _SPS:
            self._process()
        self._running = False
        self._buf = np.empty(0, np.complex64)
        self._events.put_nowait(None)
