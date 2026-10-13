"""ACARS (VHF, 2400 bd MSK on AM audio) decoder -- audio path.

Signal chain: AM audio -> mix 1800 Hz to baseband -> low-pass -> per-sample
frequency discriminator (MSK tones 1200/2400 Hz sit at -/+600 Hz) -> bit-clock
PLL locked to tone changes -> NRZI ("no tone change" = 1, "change" = 0) -> bit
stream searched for the SYN SYN character sync. Bytes are 7-bit ASCII plus an
odd-parity bit, sent LSB first. Frame: SYN SYN SOH mode registration(7) ack label(2)
block-id [STX text] ETX|ETB, then CRC-16 (Kermit: poly 0x8408 reflected, init 0,
computed over mode..ETX with parity bits, sent low byte first) and DEL.

Receive-only. Not verified off-air: the NRZI polarity and tone assignment follow
my reading of ARINC 618 and are exercised only by this module's synthetic tests.
Frames with a bad parity bit are dropped silently (a lone bit error is
indistinguishable from noise-triggered false syncs); a frame that is parity-clean
but fails its CRC is emitted with crc_ok=False, integrity "failed".
"""

import re
import time
from collections import deque

import numpy as np

from msdr.decoder.backends import DecodeEvent

_FS = 22050
_BAUD = 2400.0
_CENTRE_HZ = 1800.0  # midway between the 1200 and 2400 Hz tones
_SYN = 0x16
_SOH = 0x01
_STX = 0x02
_ETX = 0x03
_ETB = 0x17
_MAX_FRAME_BYTES = 270  # a full 220-char block plus header, with margin
_FLIGHT_RE = re.compile(r"^M(\d{2})([A-Z])([A-Z0-9]{6})")

_PLL_GAIN = 0.35
_TAPS = 49
_CUTOFF_HZ = 1150.0


def crc16_kermit(data: bytes, crc: int = 0) -> int:
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def _odd_parity_ok(byte: int) -> bool:
    return bin(byte).count("1") & 1 == 1


def _lowpass_taps() -> np.ndarray:
    n = np.arange(_TAPS) - (_TAPS - 1) / 2
    h = np.sinc(2 * _CUTOFF_HZ / _FS * n) * np.hamming(_TAPS)
    return h / h.sum()


def _syn_bits() -> list[int]:
    # Parity bit (bit 7) of SYN is 0: three 1-bits already make it odd.
    return [(_SYN >> i) & 1 for i in range(8)] * 2


_SYN_SYN = _syn_bits()


class AcarsDecoder:
    """PyDecoder: ACARS over a 22,050 Hz mono AM-demodulated channel."""

    input_rate = _FS

    def __init__(self) -> None:
        self._h = _lowpass_taps()
        self._tail = np.zeros(_TAPS - 1, dtype=np.complex128)
        self._n = 0  # global sample index, keeps the 1800 Hz mixer phase continuous
        self._prev_y = 0j
        self._spb = _FS / _BAUD
        self._win = max(3, int(round(0.6 * self._spb)))
        self._dz: deque[complex] = deque(maxlen=self._win)
        self._inst: deque[float] = deque(maxlen=3)
        self._prev_sign = 0
        self._phase = 0.0
        self._decided = False
        self._prev_tone = 1
        # NRZI polarity (does "no tone change" mean 1 or 0?) is unconfirmed, so both
        # readings are searched for SYN SYN; only the one that syncs parses a frame.
        self._framers = (_Framer(False), _Framer(True))

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        x = samples.astype(np.float64)
        idx = np.arange(self._n, self._n + x.size)
        self._n += x.size
        # Remove the AM carrier/DC so it cannot leak through the mixer.
        z = (x - x.mean()) * np.exp(-2j * np.pi * _CENTRE_HZ * idx / _FS)
        buf = np.concatenate([self._tail, z])
        self._tail = buf[-(_TAPS - 1) :]
        y = np.convolve(buf, self._h, mode="valid")
        prev = np.concatenate([[self._prev_y], y[:-1]])
        self._prev_y = y[-1]
        dz = y * np.conj(prev)
        events: list[DecodeEvent] = []
        for d in dz.tolist():
            bit = self._clock(d)
            if bit is not None:
                for framer in self._framers:
                    events.extend(framer.on_bit(bit))
                if events:
                    for framer in self._framers:
                        framer.abort()
        return events

    def flush(self) -> list[DecodeEvent]:
        return []

    def _clock(self, d: complex) -> int | None:
        self._dz.append(d)
        self._inst.append(d.imag)
        sign = 1 if sum(self._inst) > 0 else -1
        spb = self._spb
        self._phase += 1.0
        if self._prev_sign and sign != self._prev_sign:
            # A tone change marks a bit boundary; pull the clock toward it.
            err = self._phase if self._phase < spb / 2 else self._phase - spb
            err = max(-spb / 4, min(spb / 4, err))
            self._phase -= _PLL_GAIN * err
            if self._phase < 0:
                self._phase += spb
                self._decided = True
        self._prev_sign = sign
        bit = None
        if not self._decided and self._phase >= 0.8 * spb:
            self._decided = True
            tone = 1 if sum(self._dz).imag > 0 else 0
            bit = 1 if tone == self._prev_tone else 0
            self._prev_tone = tone
        if self._phase >= spb:
            self._phase -= spb
            self._decided = False
        return bit


class _Framer:
    """SYN SYN search and frame assembly for one bit polarity."""

    def __init__(self, invert: bool) -> None:
        self._invert = int(invert)
        self._reg: deque[int] = deque(maxlen=16)
        self._in_frame = False
        self._bits: list[int] = []
        self._bytes: list[int] = []

    def on_bit(self, bit: int) -> list[DecodeEvent]:
        bit ^= self._invert
        if not self._in_frame:
            self._reg.append(bit)
            if len(self._reg) == 16 and list(self._reg) == _SYN_SYN:
                self._in_frame = True
                self._bits = []
                self._bytes = []
                self._reg.clear()
            return []
        self._bits.append(bit)
        if len(self._bits) < 8:
            return []
        byte = sum(b << i for i, b in enumerate(self._bits))
        self._bits = []
        return self._on_byte(byte)

    def abort(self) -> list[DecodeEvent]:
        self._in_frame = False
        self._bytes = []
        self._bits = []
        self._reg.clear()
        return []

    def _on_byte(self, byte: int) -> list[DecodeEvent]:
        data = self._bytes
        if not data:
            if byte == _SYN:  # extra sync characters before SOH
                return []
            if byte != _SOH:
                return self.abort()
            data.append(byte)
            return []
        data.append(byte)
        if len(data) > _MAX_FRAME_BYTES:
            return self.abort()
        # The first ETX/ETB (valid parity) ends the text; exactly 2 CRC bytes follow it,
        # so test the byte two positions back as each new byte arrives.
        i = len(data) - 3
        if i >= 1 and data[i] & 0x7F in (_ETX, _ETB) and _odd_parity_ok(data[i]):
            events = self._finish(data, i)
            self.abort()
            return events
        return []

    def _finish(self, data: list[int], end: int) -> list[DecodeEvent]:
        body = data[1 : end + 1]  # mode .. ETX/ETB, parity bits intact for the CRC
        crc_bytes = data[end + 1 : end + 3]
        if not all(_odd_parity_ok(b) for b in body):
            return []
        chars = [b & 0x7F for b in body]
        if len(chars) < 12:
            return []
        crc_ok = crc16_kermit(bytes(body + crc_bytes)) == 0
        terminator = chars[-1]
        text_chars = chars[12:-1]
        if text_chars and text_chars[0] == _STX:
            text_chars = text_chars[1:]
        elif text_chars:
            return []
        reg = "".join(chr(c) for c in chars[1:8]).lstrip(".")
        label = "".join(chr(c) for c in chars[9:11]).replace("\x7f", "d")
        ack = "NAK" if chars[8] == 0x15 else chr(chars[8])
        block_id = chr(chars[11])
        text = "".join(chr(c) for c in text_chars if c in (0x09, 0x0A, 0x0D) or 0x20 <= c < 0x7F)
        fields: dict[str, object] = {
            "registration": reg,
            "mode": chr(chars[0]),
            "ack": ack,
            "label": label,
            "block_id": block_id,
            "more_blocks": terminator == _ETB,
            "crc_ok": crc_ok,
        }
        # Downlink blocks (digit block id) open with "M<seq><A-Z><flight id>".
        m = _FLIGHT_RE.match(text) if block_id.isdigit() else None
        if m:
            fields["message_no"] = m.group(1) + m.group(2)
            fields["flight_id"] = m.group(3)
            text = text[m.end() :]
        text = text.strip()
        return [
            DecodeEvent(
                ts=time.time(),
                decoder="acars",
                kind="text",
                text=text,
                fields=fields,
                integrity="passed" if crc_ok else "failed",
                integrity_note=(
                    "ACARS CRC-16 and per-character parity verified"
                    if crc_ok
                    else "ACARS CRC-16 mismatch (parity clean)"
                ),
            )
        ]
