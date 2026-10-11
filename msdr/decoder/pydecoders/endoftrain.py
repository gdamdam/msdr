# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone EOT Bell 202 demodulator for signed int16 complex IQ.

The dual-tone discriminator, bit timing, and sync/framing logic follow
SDRangel's EndOfTrainDemodSink (Copyright (C) 2021-2024 Jon Beniston,
M7RCE; Copyright (C) 2021-2022 Edouard Griffiths, F4EXB),
plugins/channelrx/demodendoftrain/endoftraindemodsink.cpp, under GPL-3.0-or-later.
EOT CRC and payload interpretation are shared with payloads.py.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, LoopEventQueue
from msdr.decoder.payloads import decode_payload
from msdr.decoder.pydecoders.rs41 import _StreamingResampler

_FS = 48_000
_BAUD = 1_200
_SYNC = 0x91D5


def _lowpass_taps(fs: float) -> np.ndarray:
    # SDRangel applies an approximately 1.1-baud lowpass after tone
    # correlation. This input-side anti-alias filter protects the 48 kHz tap.
    cutoff = min(3_000.0, fs * 0.45)
    n = np.arange(129, dtype=np.float64) - 64
    h = 2 * cutoff / fs * np.sinc(2 * cutoff / fs * n)
    h *= np.kaiser(len(h), 8.0)
    return h / h.sum()


class _Framer:
    def __init__(self):
        self.bits = 0
        self.count = 0
        self.packet: list[int] = []
        self.active = False

    def push(self, bit: int) -> bytes | None:
        self.bits |= (bit & 1) << self.count  # SDRangel's LSB-first shift register
        self.count += 1
        if not self.active:
            if self.count >= 17:
                if self.bits & 0x1FFFF == _SYNC:
                    self.active = True
                    self.bits = self.count = 0
                    self.packet.clear()
                else:
                    self.count -= 1
                    self.bits >>= 1
            return None
        if self.count == 8:
            if len(self.packet) == 8:
                result = bytes(self.packet)
                self.active = False
                self.bits = self.count = 0
                self.packet.clear()
                return result
            self.packet.append(self.bits & 0xFF)
            self.bits = self.count = 0
        return None


class EndOfTrainBackend:
    """Stateful channelized 1200-baud EOT decoder."""

    decoder_id = "endoftrain"

    def __init__(self, spec=None, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.rate = int(sample_rate_hz)
        self.center_hz = float(center_hz)
        self.frequency_hz = float(frequency_hz)
        self.offset_hz = self.frequency_hz - self.center_hz
        if self.rate < 9_600:
            raise DecoderError("End-of-Train requires at least 9600 Hz IQ sample rate")
        self.error: str | None = None
        self._resampler = (
            _StreamingResampler(self.rate, self.offset_hz) if self.rate >= 57_600 else None
        )
        self._process_rate = 57_600 if self._resampler is not None else self.rate
        self._running = False
        self._events: asyncio.Queue[DecodeEvent | None] = LoopEventQueue()
        self._n = 0
        self._h = _lowpass_taps(self._process_rate)
        self._zi = np.zeros(len(self._h) - 1, dtype=np.complex128)
        self._resample_phase = 0.0
        self._prev = 0j
        self._have_prev = False
        self._corr = np.zeros(40, dtype=np.float64)
        self._corr_i = 0
        self._corr_count = 0
        self._smooth0 = self._smooth1 = 0.0
        self._smooth_alpha = 1.0 - np.exp(-2 * np.pi * (_BAUD * 1.1) / _FS)
        self._last_sample = 0
        self._sync_count = 0
        self._framer = _Framer()
        # One-symbol complex matched correlator templates (1800 and 1200 Hz).
        t = np.arange(40, dtype=np.float64)
        self._tone0 = np.exp(2j * np.pi * 1800 * t / _FS)
        self._tone1 = np.exp(2j * np.pi * 1200 * t / _FS)

    async def start(self) -> None:
        self._running = True

    def feed(self, data: bytes | np.ndarray) -> None:
        if not self._running or data is None:
            return
        if isinstance(data, bytes):
            if len(data) % 4:
                self.error = "incomplete interleaved int16 IQ pair"
                return
            raw = np.frombuffer(data, dtype="<i2").astype(np.float64).reshape(-1, 2)
            iq = (raw[:, 0] + 1j * raw[:, 1]) / 32768.0
        else:
            arr = np.asarray(data)
            if np.iscomplexobj(arr):
                iq = arr.astype(np.complex128, copy=False).ravel()
            elif arr.ndim == 2 and arr.shape[1] == 2:
                iq = (arr[:, 0].astype(float) + 1j * arr[:, 1].astype(float)) / 32768.0
            else:
                self.error = "IQ must be complex samples or Nx2 signed int16"
                return
        if not len(iq):
            return
        if self._resampler is not None:
            tuned = self._resampler.process(iq)
        else:
            idx = np.arange(len(iq), dtype=np.float64) + self._n
            tuned = iq * np.exp(-2j * np.pi * self.offset_hz * idx / self.rate)
            self._n += len(iq)
        if not len(tuned):
            return
        # Stateful FIR, followed by phase-accumulator rational resampling to
        # the 48 kHz decoder clock. Supports integer and fractional ratios.
        # NumPy-only stateful FIR; scipy is deliberately not a runtime
        # dependency. Keep the overlap bounded to the FIR's 128-sample tail.
        joined = np.concatenate((self._zi, tuned))
        conv = np.convolve(joined, self._h, mode="full")
        filtered = conv[len(self._h) - 1 : len(self._h) - 1 + len(tuned)]
        self._zi = joined[-(len(self._h) - 1) :].copy()
        step = self._process_rate / _FS
        positions = np.arange(len(filtered), dtype=np.float64)
        counts = (
            np.floor((self._resample_phase + positions + 1) / step)
            - np.floor((self._resample_phase + positions) / step)
        ).astype(np.intp)
        self._resample_phase = (self._resample_phase + len(filtered)) % step
        selected = np.repeat(np.arange(len(filtered)), counts)
        if len(selected):
            # Mapping is nearest-neighbour when upsampling; input-rate minimum
            # keeps this path in downsampling operation for normal SDR streams.
            self._process(filtered[selected])

    def _process(self, samples: np.ndarray) -> None:
        for x in samples:
            if not self._have_prev:
                self._prev, self._have_prev = x, True
                continue
            fm = float(np.angle(x * self._prev.conjugate()))
            self._prev = x
            self._corr[self._corr_i] = fm
            self._corr_i = (self._corr_i + 1) % 40
            self._corr_count = min(40, self._corr_count + 1)
            if self._corr_count < 40:
                continue
            # Ring buffer in chronological order for the frequency correlator.
            block = np.roll(self._corr, -self._corr_i)
            c0 = abs(np.dot(block, self._tone0))
            c1 = abs(np.dot(block, self._tone1))
            self._smooth0 += self._smooth_alpha * (c0 - self._smooth0)
            self._smooth1 += self._smooth_alpha * (c1 - self._smooth1)
            sample = int(self._smooth1 >= self._smooth0)
            if sample != self._last_sample:
                self._sync_count = _FS // _BAUD // 2
            else:
                self._sync_count -= 1
                if self._sync_count <= 0:
                    packet = self._framer.push(sample)
                    if packet is not None:
                        self._emit(packet)
                    self._sync_count = _FS // _BAUD
            self._last_sample = sample

    def _emit(self, packet: bytes) -> None:
        try:
            text, fields, kind, integrity, note = decode_payload("endoftrain", packet)
        except ValueError:
            self._framer = _Framer()
            return
        # The payload helper verifies the SDRangel CRC-18; only valid frames
        # are surfaced by this live decoder.
        if integrity != "passed":
            return
        self._events.put_nowait(
            DecodeEvent(
                ts=time.time(),
                decoder="endoftrain",
                kind=kind,
                text=text,
                fields=fields,
                frequency_hz=int(self.frequency_hz),
                integrity=integrity,
                integrity_note=note,
            )
        )

    def events(self):
        async def gen():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return gen()

    async def get_event(self) -> DecodeEvent | None:
        return await self._events.get()

    async def stop(self) -> None:
        self._running = False
        self._events.put_nowait(None)
