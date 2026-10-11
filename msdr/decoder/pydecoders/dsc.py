"""In-process DSC FSK decoder for signed interleaved int16 complex IQ.

The phasing, 10-bit symbol, time-diversity, and ECC rules are ported from
SDRangel's DSC decoder (Copyright (C) 2023 Jon Beniston, M7RCE), distributed
under GPL-3.0-or-later. See https://www.gnu.org/licenses/gpl-3.0.html.

Feed tuned-channel IQ centered on the DSC signal. The protocol decoder has no
frequency/band assumption, so it supports both HF and VHF channel 70. Target
frequencies near 156.525 MHz select the VHF 1200-baud, 1300/2100 Hz profile;
other targets use the MF/HF 100-baud, +/-85 Hz profile. The front end tunes
the requested carrier offset, but the supplied IQ must retain the selected
FSK tones without prior audio demodulation.
"""

from __future__ import annotations

import asyncio
import functools
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.pydecoders.dsc_message import parse_dsc_symbols
from msdr.decoder.pydecoders.rs41 import _StreamingResampler

_PHASING = (
    (0b1011111001_1111011001_1011111001, 9),
    (0b1111011001_1011111001_0111011010, 8),
    (0b1011111001_0111011010_1011111001, 7),
    (0b0111011010_1011111001_1011011010, 6),
    (0b1011111001_1011011010_1011111001, 5),
    (0b1011011010_1011111001_0011011011, 4),
    (0b1011111001_0011011011_1011111001, 3),
    (0b0011011011_1011111001_1101011010, 2),
    (0b1011111001_1101011010_1011111001, 1),
    (0b1101011010_1011111001_0101011011, 0),
)


def _symbol(bits: int) -> int:
    """Convert ten bits to a seven-bit DSC symbol, checking its parity."""
    data = int(f"{(bits >> 3) & 0xFF:08b}"[::-1], 2) >> 1
    return data if 7 - data.bit_count() == (bits & 7) else -1


class _Frame:
    """DSC phasing, DX/RX diversity and XOR ECC framing."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.bits = 0
        self.bit_count = 0
        self.phased = False
        self.phase = 0
        self.state = "phase"
        self.dx: list[int] = []
        self.idx = 0
        self.data: list[int] = []
        self.errors = 0
        self.erasures = 0

    def bit(self, bit: int) -> dict | None:
        self.bits = ((self.bits << 1) | int(bit)) & 0xFFFFFFFF
        self.bit_count += 1
        if not self.phased:
            if self.bit_count >= 30:
                pattern = self.bits & 0x3FFFFFFF
                matches = [
                    (candidate, offset, (pattern ^ candidate).bit_count())
                    for candidate, offset in _PHASING
                    if (pattern ^ candidate).bit_count() <= 1
                ]
                if matches:
                    best_distance = min(distance for _, _, distance in matches)
                    best = [item for item in matches if item[2] == best_distance]
                    if len(best) == 1:
                        _, offset, distance = best[0]
                        self.phased = True
                        self.phase = offset
                        self.errors += distance
                        self.state = "phasing" if offset else "fill_dx"
                        self.bit_count = 0
                        self.data = []
                        self.dx = []
                        return None
                self.bit_count = 29
            return None
        if self.bit_count < 10:
            return None
        self.bit_count = 0
        value = _symbol(self.bits & 0x3FF)
        if self.state == "phasing":
            expected = (110, 125, 109, 125, 108, 125, 107, 125, 106)
            if value != expected[9 - self.phase]:
                self.errors += 1
            self.phase -= 1
            if self.phase == 0:
                self.state = "fill_dx"
        elif self.state == "dx":
            self.dx[self.idx] = value
            self.idx = (self.idx + 1) % 3
            self.state = "rx"
        elif self.state == "fill_dx":
            self.dx.append(value)
            self.state = "fill_rx" if len(self.dx) < 3 else "rx"
        elif self.state == "fill_rx":
            expected = 106 if len(self.dx) == 1 else 105
            if value != expected:
                self.errors += 1
            self.state = "fill_dx"
        elif self.state == "rx":
            dx = self.dx[self.idx]
            chosen = dx if dx != -1 else value if value != -1 else ord("*")
            if dx != -1 and dx != value:
                self.errors += 1
            elif dx == -1:
                self.errors += 1 if value != -1 else 2
                self.erasures += int(value == -1)
            self.data.append(chosen)
            # EOS ends the application data; the following symbol is XOR ECC.
            if chosen in (117, 122, 127):
                self.state = "dx_eos"
            elif len(self.data) > 40:
                self.reset()
            else:
                self.state = "dx"
        elif self.state == "dx_eos":
            self.dx[self.idx] = value
            self.idx = (self.idx + 1) % 3
            self.state = "rx_eos"
        elif self.state == "rx_eos":
            dx = self.dx[self.idx]
            chosen = dx if dx != -1 else value if value != -1 else ord("*")
            if dx != -1 and dx != value:
                self.errors += 1
            elif dx == -1:
                self.errors += 1 if value != -1 else 2
                self.erasures += int(value == -1)
            self.data.append(chosen)
            self.state = "done"
            return self._result()
        elif self.state == "done":
            return self._result()
        return None

    def _result(self) -> dict:
        values = self.data[:]
        # SDRangel stores the message through ECC; verify its XOR exactly as
        # the protocol specifies. Do not infer validity from successful framing.
        ecc_ok = (
            self.erasures == 0
            and len(values) >= 3
            and functools.reduce(int.__xor__, values[1:-1], 0) == values[-1]
        )
        return {"symbols": values, "ecc_ok": bool(ecc_ok), "symbol_errors": self.errors}


class DSCBackend:
    """DSC backend matching the asynchronous in-process decoder surface."""

    decoder_id = "dsc"

    def __init__(self, spec, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.rate = int(sample_rate_hz)
        self.offset_hz = float(frequency_hz - center_hz)
        # DSC channel 70 is the VHF DSC allocation. MF/HF DSC uses 100 baud;
        # VHF DSC uses 1200 baud and audio-frequency tones at 1300/2100 Hz.
        self.vhf = 156_400_000 <= float(frequency_hz) <= 156_700_000
        # _Frame assembles ten serial bits into each transmitted symbol.
        self.baud = 1200 if self.vhf else 100
        self.tones_hz = (1300.0, 2100.0) if self.vhf else (-85.0, 85.0)
        minimum_rate = 24_000 if self.vhf else 8_000
        if self.rate < minimum_rate:
            raise DecoderError(
                f"DSC requires at least {minimum_rate} Hz IQ sample rate for this band"
            )
        self.error: str | None = None
        self._resampler = (
            _StreamingResampler(self.rate, self.offset_hz) if self.rate >= 57_600 else None
        )
        self._work_rate = 57_600 if self._resampler is not None else self.rate
        self._events: asyncio.Queue[DecodeEvent | None] = asyncio.Queue()
        self._running = False
        self._frame = _Frame()
        self._sample_count = 0
        self._chunks: list[complex] = []
        self._symbol_phase = 0.0
        self._previous = 0j
        self._have_previous = False

    async def start(self) -> None:
        self._running = True

    def feed(self, data: bytes) -> None:
        if not self._running or not data:
            return
        if len(data) % 4:
            self.error = "incomplete interleaved int16 IQ pair"
            return
        if self._resampler is not None:
            tuned = self._resampler.process(data)
        else:
            raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
            iq = (raw[:, 0] + 1j * raw[:, 1]) / 32768.0
            n = np.arange(len(iq), dtype=np.float64) + self._sample_count
            tuned = iq * np.exp(-2j * np.pi * self.offset_hz * n / self.rate)
            self._sample_count += len(iq)
        if self.vhf and len(tuned):
            previous = np.concatenate(
                ([self._previous] if self._have_previous else [tuned[0]], tuned[:-1])
            )
            samples = np.angle(tuned * np.conj(previous))
            self._previous = complex(tuned[-1])
            self._have_previous = True
        else:
            samples = tuned
        if not len(samples):
            return
        # Locate every symbol boundary in this block from the cumulative
        # phase (baud need not divide _work_rate evenly), instead of walking
        # every IQ sample in a Python for-loop.
        cumulative = self._symbol_phase + self.baud * np.arange(1, len(samples) + 1)
        crossed = np.floor(cumulative / self._work_rate).astype(np.int64)
        boundaries = np.flatnonzero(np.diff(crossed, prepend=0) > 0)
        samples_c = samples.astype(np.complex128, copy=False)
        start = 0
        for end in boundaries:
            piece = samples_c[start : end + 1]
            block = np.concatenate((self._chunks, piece)) if self._chunks else piece
            self._chunks = []
            start = end + 1
            # Compare coherent energy at both FSK tones over this symbol.
            t = np.arange(len(block), dtype=np.float64) / self._work_rate
            first = abs(np.sum(block * np.exp(-2j * np.pi * self.tones_hz[0] * t)))
            second = abs(np.sum(block * np.exp(-2j * np.pi * self.tones_hz[1] * t)))
            result = self._frame.bit(int(second > first))
            if result is not None:
                self._emit(result)
        self._chunks.extend(samples_c[start:].tolist())
        self._symbol_phase = float(cumulative[-1] % self._work_rate)

    def _emit(self, result: dict) -> None:
        symbols = result["symbols"]
        # The raw symbol list is retained even when a message's ECC fails; the
        # consumer can inspect it, while integrity accurately reports the XOR.
        text = "DSC " + " ".join(str(x) for x in symbols)
        integrity = "passed" if result["ecc_ok"] else "failed"
        note = "DSC XOR ECC verified" if result["ecc_ok"] else "DSC XOR ECC mismatch"
        fields = {
            "symbols": symbols,
            "symbol_errors": result["symbol_errors"],
            "ecc_ok": result["ecc_ok"],
        }
        fields.update(parse_dsc_symbols(symbols, ecc_ok=result["ecc_ok"]))
        if fields.get("sender_mmsi"):
            if fields.get("distress_nature"):
                text = f"DSC distress alert from MMSI {fields['sender_mmsi']}"
                text += f" ({fields['distress_nature'].replace('_', ' ')})"
                if "latitude" in fields and "longitude" in fields:
                    text += f" at {fields['latitude']:.4f}, {fields['longitude']:.4f}"
                if fields.get("utc_time"):
                    text += f" UTC {fields['utc_time']}"
            elif fields.get("category"):
                text = f"DSC {fields['category']} call from MMSI {fields['sender_mmsi']}"
                destination = fields.get("recipient_mmsi") or fields.get("group_mmsi")
                if destination:
                    text += f" to MMSI {destination}"
        self._events.put_nowait(
            DecodeEvent(
                ts=time.time(),
                decoder="dsc",
                kind="message",
                text=text,
                fields=fields,
                frequency_hz=None,
                integrity=integrity,
                integrity_note=note,
            )
        )
        self._frame.reset()

    def events(self):
        async def _gen():
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

        return _gen()

    async def get_event(self) -> DecodeEvent | None:
        return await self._events.get()

    async def stop(self) -> None:
        self._running = False
        self._chunks.clear()
        self._events.put_nowait(None)
