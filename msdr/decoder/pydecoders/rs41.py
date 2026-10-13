"""Standalone RS41 radiosonde decoder for interleaved signed-int16 IQ.

Demodulation, alternating preamble correlation, frame sync and bit ordering are
ported from SDRangel RadiosondeDemodSink (Copyright (C) 2021-2022 Edouard
Griffiths and Jon Beniston), GPL-3.0-or-later. This implementation uses the
same 57.6 ksample/s channel rate, 4800 baud, 200-bit preamble and RS41 sync
word. Two interleaved shortened RS(255, 231) codewords are corrected before
sub-block CRC validation; frames are emitted only when all sub-block CRCs pass.
"""

from __future__ import annotations

import asyncio
import math
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, LoopEventQueue
from msdr.decoder.payloads import decode_payload
from msdr.decoder.pydecoders.rs41_fec import correct_rs41_frame

_TARGET_RATE = 57_600
_SPS = 12
_PREAMBLE_BITS = 200
_FRAME_SYNC = 0xF812962211CAB610
_DESCRAMBLE = bytes(
    (
        0x96,
        0x83,
        0x3E,
        0x51,
        0xB1,
        0x49,
        0x08,
        0x98,
        0x32,
        0x05,
        0x59,
        0x0E,
        0xF9,
        0x44,
        0xC6,
        0x26,
        0x21,
        0x60,
        0xC2,
        0xEA,
        0x79,
        0x5D,
        0x6D,
        0xA1,
        0x54,
        0x69,
        0x47,
        0x0C,
        0xDC,
        0xE8,
        0x5C,
        0xF1,
        0xF7,
        0x76,
        0x82,
        0x7F,
        0x07,
        0x99,
        0xA2,
        0x2C,
        0x93,
        0x7C,
        0x30,
        0x63,
        0xF5,
        0x10,
        0x2E,
        0x61,
        0xD0,
        0xBC,
        0xB4,
        0xB6,
        0x06,
        0xAA,
        0xF4,
        0x23,
        0x78,
        0x6E,
        0x3B,
        0xAE,
        0xBF,
        0x7B,
        0x4C,
        0xC1,
    )
)


class _StreamingResampler:
    """Bounded vectorized rational low-pass resampler, with continuous phase."""

    def __init__(self, rate: int, offset: float):
        if rate < _TARGET_RATE:
            raise DecoderError("RS41 requires IQ sample rate >= 57600 Hz")
        if abs(offset) + 12_000 > rate / 2:
            raise DecoderError("RS41 channel falls outside the available IQ span")
        # A 49-tap filter at a multi-MHz device rate is too narrow in time
        # to prevent aliases at 57.6 kHz. Decimate in octave stages first.
        stages = 0
        while rate / (1 << (stages + 1)) >= _TARGET_RATE:
            stages += 1
        g = math.gcd(rate, _TARGET_RATE * (1 << stages))
        self.step, self.den = rate // g, _TARGET_RATE * (1 << stages) // g
        self.rate, self.offset = rate, offset
        taps = np.arange(49) - 24
        h = 0.45 * np.sinc(0.45 * taps) * np.hamming(49)
        self.stage_taps = (h / h.sum()).astype(np.float32)
        self.stages = [[np.zeros(48, np.complex64), 0] for _ in range(stages)]
        self.half = 24
        self.offsets = np.arange(-self.half, self.half + 1)
        self.window = np.hamming(len(self.offsets))
        self.cutoff = 0.45 * self.den / self.step
        self.buf = np.empty(0, np.complex64)
        self.start = self.count = self.raw_count = 0
        self.next = (self.half * self.den + self.step - 1) // self.step

    def process(self, data: bytes | np.ndarray) -> np.ndarray:
        if isinstance(data, bytes):
            raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
            x = ((raw[:, 0] + 1j * raw[:, 1]) / 32768).astype(np.complex64)
        else:
            arr = np.asarray(data)
            if np.iscomplexobj(arr):
                x = arr.astype(np.complex64, copy=False).ravel()
            else:
                x = ((arr[:, 0] + 1j * arr[:, 1]) / 32768).astype(np.complex64)
        if not len(x):
            return np.empty(0, np.complex64)
        n = np.arange(self.raw_count, self.raw_count + len(x), dtype=np.float64)
        self.raw_count += len(x)
        if self.offset:
            # Out-of-place: `x` may alias the caller's array (astype copy=False).
            x = x * np.exp(-2j * np.pi * np.remainder(n * self.offset / self.rate, 1)).astype(
                np.complex64
            )
        for stage in self.stages:
            if not len(x):
                break
            tail, count = stage
            joined = np.concatenate((tail, x))
            filtered = np.convolve(joined, self.stage_taps, mode="valid")
            x = filtered[count & 1 :: 2].astype(np.complex64)
            stage[0] = joined[-48:].copy()
            stage[1] = count + len(filtered)
        self.count += len(x)
        if len(self.buf):
            self.buf = np.concatenate((self.buf, x))
        else:
            self.buf, self.start = x, self.count - len(x)
        limit = ((self.count - self.half) * self.den + self.step - 1) // self.step
        outs = []
        while self.next < limit:
            end = min(limit, self.next + 2048)
            k = np.arange(self.next, end, dtype=np.int64)
            num = k * self.step
            centers = num // self.den
            frac = (num % self.den) / self.den
            idx = centers[:, None] + self.offsets[None, :] - self.start
            dist = self.offsets[None, :] - frac[:, None]
            w = 2 * self.cutoff * np.sinc(2 * self.cutoff * dist) * self.window
            w /= w.sum(axis=1, keepdims=True)
            outs.append(np.sum(self.buf[idx] * w.astype(np.float32), axis=1).astype(np.complex64))
            self.next = end
        keep = max(self.start, self.next * self.step // self.den - self.half)
        if keep > self.start:
            self.buf = self.buf[keep - self.start :].copy()
            self.start = keep
        return np.concatenate(outs) if outs else np.empty(0, np.complex64)


class RS41Backend:
    """Streaming standalone decoder with start/feed/events/stop interface."""

    decoder_id = "rs41"

    def __init__(self, spec=None, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.rate = int(sample_rate_hz)
        self.offset = float(frequency_hz - center_hz)
        self.error = None
        self._running = False
        self._events: asyncio.Queue[DecodeEvent | None] = LoopEventQueue()
        self._resampler = _StreamingResampler(self.rate, self.offset)
        self._prev = 0j
        self._have_prev = False
        self._sps_tail = np.empty(0, np.float32)
        self._acquisition = np.empty(0, np.float32)
        self._samples = 0
        self._symbols = np.empty(0, np.float32)
        self._bits = 0
        self._bit_count = 0
        self._frame_bits: list[int] = []
        self._in_frame = False
        self._sample_invert = False
        # Discriminator bias per sample from a carrier offset (sonde drift or
        # off-centre tuning), measured on the preamble and removed before slicing.
        self._dc = 0.0
        self._sync_found = False
        self._sync_search_bits = 0
        self._frame_bytes = bytearray()

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
        prev = np.concatenate(([self._prev] if self._have_prev else [iq[0]], iq[:-1]))
        discr = np.angle(iq * np.conj(prev)).astype(np.float32)
        self._prev = complex(iq[-1])
        self._have_prev = True
        if not self._in_frame:
            self._acquisition = np.concatenate((self._acquisition, discr))
            acquired = self._acquire_preamble()
            if acquired is None:
                # Keep enough history for a complete 200-bit preamble plus
                # every integer sample phase and symbol offset.
                keep = (_PREAMBLE_BITS + 64 + 12) * _SPS - 1
                if len(self._acquisition) > keep:
                    self._acquisition = self._acquisition[-keep:].copy()
                return
            remaining = acquired - np.float32(self._dc)
            complete = len(remaining) // _SPS
            self._sps_tail = remaining[complete * _SPS :].copy()
            if not complete:
                return
            self._symbols = np.concatenate(
                (self._symbols, remaining[: complete * _SPS].reshape(-1, _SPS).sum(axis=1))
            )
        else:
            discr = discr - np.float32(self._dc)
            if len(self._sps_tail):
                discr = np.concatenate((self._sps_tail, discr))
            complete = len(discr) // _SPS
            self._sps_tail = discr[complete * _SPS :].copy()
            if not complete:
                return
            sums = discr[: complete * _SPS].reshape(-1, _SPS).sum(axis=1)
            self._symbols = np.concatenate((self._symbols, sums))
        while len(self._symbols) >= (_PREAMBLE_BITS if not self._in_frame else 1):
            if not self._in_frame:
                window = self._symbols[:_PREAMBLE_BITS]
                signs = np.where(np.arange(_PREAMBLE_BITS) & 1, 1.0, -1.0)
                corr = float(np.dot(window, signs))
                if abs(corr) >= _PREAMBLE_BITS * 0.18:
                    best = 0
                    best_score = abs(corr)
                    for shift in range(1, min(12, len(self._symbols) - _PREAMBLE_BITS + 1)):
                        candidate = abs(
                            float(np.dot(self._symbols[shift : shift + _PREAMBLE_BITS], signs))
                        )
                        if candidate > best_score:
                            best, best_score = shift, candidate
                    self._symbols = self._symbols[best + _PREAMBLE_BITS :]
                    self._in_frame = True
                    self._bits = 0
                    self._bit_count = 0
                    self._frame_bits = []
                    self._sync_found = False
                    self._sync_search_bits = 0
                    continue
                self._symbols = self._symbols[1:]
            else:
                value = int(self._symbols[0] >= 0) ^ self._sample_invert
                self._symbols = self._symbols[1:]
                self._bit_count += 1
                self._bits |= value << (self._bit_count - 1)
                if not self._sync_found:
                    self._sync_search_bits += 1
                    if self._bit_count == 64:
                        if self._bits == _FRAME_SYNC:
                            self._sync_found = True
                            self._bits = 0
                            self._bit_count = 0
                            # SDRangel inserts the received sync bytes before
                            # descrambling the entire frame.
                            self._frame_bytes = bytearray(
                                (0x10, 0xB6, 0xCA, 0x11, 0x22, 0x96, 0x12, 0xF8)
                            )
                        else:
                            # Search for sync through the remaining preamble
                            # neighborhood while maintaining a 64-bit window.
                            self._bits >>= 1
                            self._bit_count = 63
                    if self._sync_search_bits >= 128 and not self._sync_found:
                        self._in_frame = False
                    continue
                if self._bit_count == 8:
                    b = sum(((self._bits >> i) & 1) << i for i in range(8))
                    self._bit_count = 0
                    self._bits = 0
                    self._frame_bytes.append(b)
                    if len(self._frame_bytes) in (320, 518):
                        plain = bytes(
                            v ^ _DESCRAMBLE[i & 63] for i, v in enumerate(self._frame_bytes)
                        )
                        if len(plain) == 320:
                            # Attempt the standard frame even when its marker
                            # is damaged: the marker is protected by RS too.
                            emitted = self._emit_frame(plain)
                            if emitted:
                                self._in_frame = False
                                self._frame_bits = []
                                self._frame_bytes.clear()
                                self._sync_found = False
                        else:
                            # A failed 320-byte attempt may be the prefix of
                            # an extended frame. Try the full protected frame
                            # once, then reset regardless of CRC outcome.
                            self._emit_frame(plain)
                            self._in_frame = False
                            self._frame_bits = []
                            self._frame_bytes.clear()
                            self._sync_found = False
            if not self._in_frame and len(self._symbols) < _PREAMBLE_BITS:
                break

    def _acquire_preamble(self) -> np.ndarray | None:
        """Find the alternating preamble across all 12 integer sample phases."""
        samples = self._acquisition
        need = (_PREAMBLE_BITS + 64 + 12) * _SPS
        if len(samples) < need:
            return None
        sync_bits = np.asarray([(_FRAME_SYNC >> i) & 1 for i in range(64)])
        candidates = []
        for phase in range(_SPS):
            symbols = samples[phase : (len(samples) - phase) // _SPS * _SPS + phase]
            symbols = symbols[: len(symbols) // _SPS * _SPS].reshape(-1, _SPS).sum(axis=1)
            if len(symbols) < _PREAMBLE_BITS + 64:
                continue
            # Slice against a local mean, not zero: a carrier offset larger than
            # the 2.4 kHz deviation otherwise puts every symbol on one side.
            local_mean = np.convolve(symbols, np.full(64, 1 / 64), mode="same")
            bits = symbols >= local_mean
            windows = np.lib.stride_tricks.sliding_window_view(bits, 64)
            errors = np.count_nonzero(windows != sync_bits, axis=1)
            inverted = 64 - errors
            best_errors = np.minimum(errors, inverted)
            for sync_index in np.flatnonzero(best_errors <= 8):
                signs = np.where(np.arange(_PREAMBLE_BITS) & 1, 1.0, -1.0)
                preamble = symbols[max(0, sync_index - _PREAMBLE_BITS) : sync_index]
                reference = signs[-len(preamble) :] if sync_index < _PREAMBLE_BITS else signs
                score = abs(float(np.dot(preamble, reference)))
                if score < _PREAMBLE_BITS * 0.18:
                    continue
                invert = bool(inverted[sync_index] < errors[sync_index])
                start = phase + sync_index * _SPS
                # The alternating preamble averages to zero, so its mean over an
                # even number of symbols is the offset's bias.
                even = len(preamble) // 2 * 2
                dc = float(preamble[-even:].mean()) / _SPS if even else 0.0
                candidates.append((int(best_errors[sync_index]), -score, start, invert, dc))
        if not candidates:
            return None
        # Prefer the first complete frame in a large feed. Selecting the
        # strongest later burst can leave a complete earlier frame unused.
        _, _, sync_start, self._sample_invert, self._dc = min(
            candidates, key=lambda item: (item[2], item[0], item[1])
        )
        self._in_frame = True
        self._bits = self._bit_count = 0
        self._frame_bits = []
        self._sync_found = False
        self._sync_search_bits = 0
        self._symbols = np.empty(0, np.float32)
        self._acquisition = np.empty(0, np.float32)
        return samples[sync_start:].copy()

    def _emit_frame(self, frame: bytes):
        corrected = correct_rs41_frame(frame)
        if corrected is None:
            return False
        frame, corrected_symbols = corrected
        try:
            text, fields, kind, integrity, note = decode_payload("rs41", frame)
        except ValueError:
            return False
        if integrity != "passed":
            return False
        fields["rs_corrected_symbols"] = corrected_symbols
        self._events.put_nowait(
            DecodeEvent(
                ts=time.time(),
                decoder="rs41",
                kind="message",
                text=text,
                fields=fields,
                frequency_hz=None,
                integrity=integrity,
                integrity_note=note + f"; Reed-Solomon corrected {corrected_symbols} symbol(s)",
            )
        )
        return True

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
        self._running = False
        self._symbols = np.empty(0, np.float32)
        self._events.put_nowait(None)
