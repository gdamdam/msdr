# SPDX-License-Identifier: GPL-3.0-or-later
"""Native ADS-B / Mode S backend: feeds msdr's raw IQ stream to
`AdsbReceiver` (adsb_phy.py) instead of FlightAware's `dump1090-fa`
(standalone_adsb.py, kept as the alternative backend -- see
docs/adsb-native.md). `NativeADSBBackend` keeps the same constructor shape,
`start`/`feed`/`stop`/`events` behaviour and DecodeEvent field schema as
`StandaloneADSBBackend` (msdr/decoder/standalone_adsb.py's `parse_sbs_line`)
so the map and UI consume it unchanged; `backend_name = "msdr-native"`.
"""

from __future__ import annotations

import asyncio
import math
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.pydecoders.adsb_phy import _SAMPLE_RATE, AdsbMessage, AdsbReceiver

_FIR_HALF_TAPS = 48
_FIR_BATCH_OUTPUTS = 4096


class _Resampler:
    """Centered windowed-sinc low-pass + rational-rate IQ resampler to
    `target_rate_hz`, continuous phase across `process()` calls -- same
    structure as standalone_adsb.py's `_StreamingIQResampler`, with the
    target rate as an instance attribute instead of a module constant
    (dump1090 wants 2.4 MHz; the native receiver works at a multiple of 2
    MHz -- see NativeADSBBackend's samples_per_chip choice).
    """

    def __init__(self, sample_rate_hz: int, offset_hz: float, target_rate_hz: int = _SAMPLE_RATE):
        common = math.gcd(sample_rate_hz, target_rate_hz)
        self._step_num = sample_rate_hz // common
        self._step_den = target_rate_hz // common
        self._sample_rate = sample_rate_hz
        self._offset_hz = offset_hz
        self._half = _FIR_HALF_TAPS
        self._offsets = np.arange(-self._half, self._half + 1, dtype=np.int64)
        self._window = np.hamming(len(self._offsets)).astype(np.float64)
        # Bound the passband by both Nyquist limits: interpolation must
        # reject input images as well as decimation rejecting output aliases.
        self._cutoff = 0.45 * min(sample_rate_hz, target_rate_hz) / sample_rate_hz
        self._phase_weights = None
        if self._step_den <= 4096:
            fractions = np.arange(self._step_den)[:, None] / self._step_den
            distance = self._offsets[None, :] - fractions
            weights = 2 * self._cutoff * np.sinc(2 * self._cutoff * distance)
            weights *= self._window[None, :]
            self._phase_weights = (weights / weights.sum(axis=1, keepdims=True)).astype(
                np.float32
            )
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = 0
        self._input_count = 0
        self._next_output = (self._half * self._step_den + self._step_num - 1) // self._step_num

    def process(self, data: bytes) -> np.ndarray:
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
        if not len(raw):
            return np.empty(0, dtype=np.complex64)
        iq = ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)
        start = self._input_count
        indices = np.arange(start, start + len(iq), dtype=np.float64)
        self._input_count += len(iq)
        if self._offset_hz:
            phase = np.remainder(indices * self._offset_hz / self._sample_rate, 1.0)
            iq *= np.exp(-2j * np.pi * phase).astype(np.complex64)
        if self._step_num == 1 and self._step_den == 1:
            return iq
        if len(self._buffer):
            self._buffer = np.concatenate((self._buffer, iq))
        else:
            self._buffer = iq
            self._buffer_start = start

        safe_centers = self._input_count - self._half
        limit = (safe_centers * self._step_den + self._step_num - 1) // self._step_num
        chunks: list[np.ndarray] = []
        offsets = self._offsets
        while self._next_output < limit:
            stop = min(limit, self._next_output + _FIR_BATCH_OUTPUTS)
            out_numbers = np.arange(self._next_output, stop, dtype=np.int64)
            time_numerators = out_numbers * self._step_num
            centers = time_numerators // self._step_den
            fractions = (time_numerators % self._step_den) / self._step_den
            sample_indices = centers[:, None] + offsets[None, :]
            local_indices = sample_indices - self._buffer_start
            if self._phase_weights is not None:
                weights = self._phase_weights[time_numerators % self._step_den]
            else:
                distance = offsets[None, :] - fractions[:, None]
                weights = 2.0 * self._cutoff * np.sinc(2.0 * self._cutoff * distance)
                weights *= self._window[None, :]
                weights = (weights / weights.sum(axis=1, keepdims=True)).astype(np.float32)
            filtered = np.einsum("ij,ij->i", self._buffer[local_indices], weights)
            chunks.append(filtered.astype(np.complex64, copy=False))
            self._next_output = stop

        next_center = (self._next_output * self._step_num) // self._step_den
        keep_from = max(self._buffer_start, next_center - self._half)
        discard = keep_from - self._buffer_start
        if discard > 0:
            self._buffer = self._buffer[discard:].copy()
            self._buffer_start = keep_from
        if not chunks:
            return np.empty(0, dtype=np.complex64)
        return np.concatenate(chunks)


# DF17/18 type-code groups -> SBS-style transmission_type, for map/UI
# compatibility with standalone_adsb.py's BaseStation field names.
_TRANSMISSION_TYPE_BY_TC = {}
for _tc in range(1, 5):
    _TRANSMISSION_TYPE_BY_TC[_tc] = 1  # identification
for _tc in range(9, 19):
    _TRANSMISSION_TYPE_BY_TC[_tc] = 3  # airborne position
_TRANSMISSION_TYPE_BY_TC[19] = 4  # airborne velocity


def _message_to_event(msg: AdsbMessage) -> DecodeEvent:
    icao_hex = f"{msg.icao:06X}"
    fields: dict[str, object] = {"icao": icao_hex}
    if msg.df == 11:
        fields["transmission_type"] = 8  # all-call reply, SBS "ESIdentAndCategory"-adjacent
    else:
        tc = None
        if "callsign" in msg.fields:
            tc = 1
        elif "latitude" in msg.fields or "altitude_ft" in msg.fields:
            tc = 9
        elif "ground_speed_kt" in msg.fields or "track_deg" in msg.fields:
            tc = 19
        if tc is not None:
            fields["transmission_type"] = _TRANSMISSION_TYPE_BY_TC.get(tc, 0)
        fields.update(msg.fields)
    callsign = fields.get("callsign", "")
    summary = f"{icao_hex} {callsign}".strip()
    return DecodeEvent(
        ts=time.time(),
        decoder="adsb",
        kind="text",
        text=summary,
        fields=fields,
        integrity="passed" if msg.crc_ok else "failed",
        integrity_note=(
            "CRC-24 verified (msdr native receiver); no bit-flip correction "
            "is applied, matching SDRangel's ADS-B demodulator"
        ),
    )


class NativeADSBBackend:
    """Raw-IQ ADS-B/Mode S backend running msdr's own numpy demodulator
    (adsb_phy.AdsbReceiver) instead of dump1090-fa. Same constructor shape
    as StandaloneADSBBackend (spec first, to match the standalone_protocol
    dispatch in manager.py's `_make_protocol_iq_backend`)."""

    decoder_id = "adsb"
    backend_name = "msdr-native"

    def __init__(self, spec, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.sample_rate_hz = int(sample_rate_hz)
        self.offset_hz = float(frequency_hz - center_hz)
        self.error: str | None = None
        self.dropped_frames = 0
        if self.sample_rate_hz < _SAMPLE_RATE:
            raise DecoderError("ADS-B requires an IQ sample rate of at least 2 MHz")
        if abs(self.offset_hz) + _SAMPLE_RATE / 2 > self.sample_rate_hz / 2:
            raise DecoderError("ADS-B channel falls outside the available IQ span")
        # The demodulator works on power (I^2+Q^2), so a frequency offset
        # inside the IQ span needs no mixing. When the IQ rate is a whole
        # multiple of the 2 Mchip/s chip rate (2, 4, 6, 8, 10 MS/s) it is fed
        # directly at that many samples/chip: the boxcar over each chip is
        # the matched filter. Measured 2026-10-03 on a live 6 MS/s RSPdx-R2
        # stream: the previous resample-to-4-MS/s path ran at 1.08x real
        # time (the windowed-sinc resampler was ~98 % of it), so the IQ
        # session fell behind and dropped most samples.
        spc = self.sample_rate_hz // _SAMPLE_RATE
        integer_spc = self.sample_rate_hz % _SAMPLE_RATE == 0 and spc <= 8
        needs_resample = not integer_spc
        # Any other input rate is resampled, and *oversampled* to 2
        # samples/chip (4 MS/s) rather than decimated to 1 -- a plain
        # decimation to 2 MS/s can land its fixed output phase on a chip
        # transition (measured: ~15% raw bit errors, see
        # tests/decoder/test_adsb_native.py), where summing 2 samples/chip
        # (SDRangel's samplesPerChip, same as the native path) recovers it.
        samples_per_chip = 2 if needs_resample else spc
        target_rate = _SAMPLE_RATE * samples_per_chip
        self._resampler = (
            _Resampler(self.sample_rate_hz, self.offset_hz, target_rate) if needs_resample else None
        )
        self._receiver = AdsbReceiver(samples_per_chip=samples_per_chip)
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()

    async def start(self) -> None:
        return None

    def feed(self, data: bytes) -> None:
        if not data or self.error:
            return
        if len(data) % 4:
            self.dropped_frames += 1
            return
        try:
            if self._resampler is not None:
                samples = self._resampler.process(data)
            else:
                raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
                samples = ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)
        except Exception as exc:  # noqa: BLE001 -- malformed chunk, don't crash the channel
            self.error = f"ADS-B native resample failed: {exc}"
            return
        if not len(samples):
            return
        try:
            messages = self._receiver.feed(samples, now=time.time())
        except Exception as exc:  # noqa: BLE001
            self.error = f"ADS-B native receiver error: {exc}"
            return
        for message in messages:
            self._events.put_nowait(_message_to_event(message))

    def events(self):
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def stop(self) -> None:
        self._events = asyncio.Queue()
