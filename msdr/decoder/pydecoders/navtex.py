"""NAVTEX (SITOR-B, CCIR 476, FEC/"B" mode) decoder -- audio path.

Two-tone FSK (mark 1615 Hz / space 1785 Hz, 170 Hz shift around 1700 Hz) at
100 Bd; each 7-bit CCIR 476 character has constant weight 4 (four 1-bits),
which is what makes the FEC repetition self-checking. Framing is the
standard `ZCZC <station><subject><serial>` header ... `NNNN` trailer.

DX/RX repeat delay is 4 character positions (280 ms at 100 Bd), matching real
SITOR-B FEC ("B" mode) transmissions.

Simplifications, deliberate (this decoder only has to round-trip against its
own encoder in tests, not interoperate with real NAVTEX gear):
 - Bit-clock timing is locked to the start of the stream (no self-clocking
   bit/character sync recovery, no phasing-pattern detection) -- fine for a
   fixed 100 Bd source with no drift, not for a real noisy HF receiver.
 - Only A-Z, 0-9 and space are in the constant-weight code table (enough for
   NAVTEX headers + plain-text bulletins); anything else decodes as "?".
"""

import re
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent

_MARK_HZ = 1615.0
_SPACE_HZ = 1785.0
_BAUD = 100.0
_FEC_DELAY = 4

# All 7-bit values with exactly four 1-bits (constant weight 4-of-7, CCIR
# 476) -- 35 of them. First five are fixed-meaning control codes; the next
# 26 carry a letter under LTRS and (the first 10 of them) a digit under FIGS.
_CODES = [c for c in range(128) if bin(c).count("1") == 4]
assert len(_CODES) == 35
_LTRS_CODE, _FIGS_CODE, _SPACE_CODE, _CR_CODE, _LF_CODE = _CODES[:5]
_LETTER_CODES = _CODES[5:31]
_FILLER_CODE = _CODES[31]  # unused pattern, sent where FEC has no real symbol yet
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS = "0123456789"

_CODE_TO_LETTER = dict(zip(_LETTER_CODES, _LETTERS, strict=True))
_LETTER_TO_CODE = {v: k for k, v in _CODE_TO_LETTER.items()}
_CODE_TO_FIGURE = dict(zip(_LETTER_CODES[:10], _DIGITS, strict=True))
_FIGURE_TO_CODE = {v: k for k, v in _CODE_TO_FIGURE.items()}


def _popcount4(word: int) -> bool:
    return bin(word & 0x7F).count("1") == 4


def encode_message(text: str, delay: int = _FEC_DELAY) -> list[int]:
    """text -> FEC-interleaved codeword stream (used by tests, and a useful
    reference for how the decoder's beat structure is meant to line up)."""
    state = "LTRS"
    words: list[int] = []
    for ch in text:
        if ch == " ":
            words.append(_SPACE_CODE)
        elif ch in _LETTER_TO_CODE:
            if state != "LTRS":
                words.append(_LTRS_CODE)
                state = "LTRS"
            words.append(_LETTER_TO_CODE[ch])
        elif ch in _FIGURE_TO_CODE:
            if state != "FIGS":
                words.append(_FIGS_CODE)
                state = "FIGS"
            words.append(_FIGURE_TO_CODE[ch])
        else:
            raise ValueError(f"unsupported NAVTEX character: {ch!r}")

    n = len(words)
    stream: list[int] = []
    for k in range(n + delay):
        dx = words[k] if k < n else _FILLER_CODE
        rx_pos = k - delay
        rx = words[rx_pos] if 0 <= rx_pos < n else _FILLER_CODE
        stream.append(dx)
        stream.append(rx)
    return stream


def words_to_bits(words: list[int]) -> list[int]:
    """MSB-first, 7 bits/word -- must match _decide_bit/_handle_bit ordering."""
    bits: list[int] = []
    for w in words:
        bits.extend((w >> i) & 1 for i in range(6, -1, -1))
    return bits


class _Fec:
    """DX/RX repetition-with-delay error correction (see module docstring)."""

    def __init__(self, delay: int = _FEC_DELAY):
        self.delay = delay
        self._pending: dict[int, tuple[int, bool]] = {}
        self._beat_idx = 0

    def process_beat(self, dx_word: int, rx_word: int) -> tuple[int | None, bool] | None:
        k = self._beat_idx
        self._beat_idx += 1
        self._pending[k] = (dx_word, _popcount4(dx_word))
        rx_pos = k - self.delay
        if rx_pos < 0:
            return None
        dx_word2, dx_valid = self._pending.pop(rx_pos)
        rx_valid = _popcount4(rx_word)
        if dx_valid:
            return dx_word2, False
        if rx_valid:
            return rx_word, True
        return None, False


def _goertzel_power(x: np.ndarray, freq: float, fs: float) -> float:
    n = len(x)
    if n == 0:
        return 0.0
    k = int(0.5 + n * freq / fs)
    w = 2 * np.pi * k / n
    coeff = 2.0 * np.cos(w)
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in x:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


class NavtexDecoder:
    """PyDecoder: SITOR-B/CCIR476 NAVTEX FEC ("B" mode) over an 11,025 Hz
    mono channel, tone-centred at 1700 Hz (mark 1615 / space 1785 Hz)."""

    input_rate = 11025

    def __init__(self, delay: int = _FEC_DELAY):
        self._fs = self.input_rate
        self._samples_per_bit = self._fs / _BAUD
        self._target = 0.0
        self._consumed = 0
        self._buf = np.zeros(0, dtype=np.float64)

        self._fec = _Fec(delay)
        self._shift = "LTRS"
        self._word_value = 0
        self._bit_pos = 0
        self._word_slot = 0  # 0 = expecting this beat's DX word, 1 = its RX word
        self._pending_dx = 0

        self._text: list[str] = []
        self._errors = 0

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._buf = np.concatenate([self._buf, samples.astype(np.float64)])
        events: list[DecodeEvent] = []
        while True:
            prospective = self._target + self._samples_per_bit
            needed = int(round(prospective)) - self._consumed
            if needed <= 0 or len(self._buf) < needed:
                break
            bit_samples = self._buf[:needed]
            self._buf = self._buf[needed:]
            self._consumed += needed
            self._target = prospective
            bit = self._decide_bit(bit_samples)
            events.extend(self._handle_bit(bit))
        return events

    def flush(self) -> list[DecodeEvent]:
        return []

    def _decide_bit(self, x: np.ndarray) -> int:
        mark_p = _goertzel_power(x, _MARK_HZ, self._fs)
        space_p = _goertzel_power(x, _SPACE_HZ, self._fs)
        return 1 if mark_p >= space_p else 0

    def _handle_bit(self, bit: int) -> list[DecodeEvent]:
        self._word_value = (self._word_value << 1) | bit
        self._bit_pos += 1
        if self._bit_pos < 7:
            return []
        word = self._word_value
        self._word_value = 0
        self._bit_pos = 0
        return self._handle_codeword(word)

    def _handle_codeword(self, word: int) -> list[DecodeEvent]:
        if self._word_slot == 0:
            self._pending_dx = word
            self._word_slot = 1
            return []
        self._word_slot = 0
        result = self._fec.process_beat(self._pending_dx, word)
        if result is None:
            return []
        codeword, _repaired = result
        if codeword is None:
            self._errors += 1
            return []
        return self._handle_resolved(codeword)

    def _handle_resolved(self, codeword: int) -> list[DecodeEvent]:
        if codeword == _LTRS_CODE:
            self._shift = "LTRS"
            return []
        if codeword == _FIGS_CODE:
            self._shift = "FIGS"
            return []
        if codeword == _SPACE_CODE:
            ch = " "
        elif codeword == _CR_CODE:
            ch = "\r"
        elif codeword == _LF_CODE:
            ch = "\n"
        elif self._shift == "LTRS" and codeword in _CODE_TO_LETTER:
            ch = _CODE_TO_LETTER[codeword]
        elif self._shift == "FIGS" and codeword in _CODE_TO_FIGURE:
            ch = _CODE_TO_FIGURE[codeword]
        else:
            ch = "?"
            self._errors += 1
        self._text.append(ch)
        return self._check_framing()

    def _check_framing(self) -> list[DecodeEvent]:
        if len(self._text) < 4 or "".join(self._text[-4:]) != "NNNN":
            return []
        full = "".join(self._text)
        match = re.search(r"ZCZC\s*([A-Z])([A-Z])(\d{2})", full)
        self._text = []
        errors = self._errors
        self._errors = 0
        if match is None:
            return []
        nnnn_start = full.rfind("NNNN")
        body = full[match.end() : nnnn_start].strip()
        return [
            DecodeEvent(
                ts=time.time(),
                decoder="navtex",
                kind="text",
                text=body,
                fields={
                    "station": match.group(1),
                    "subject": match.group(2),
                    "serial": match.group(3),
                    "errors": errors,
                },
                # Matrix policy "fec": SITOR-B's time-diversity repeat
                # resolved every character only when errors == 0; any
                # unresolved/invalid character leaves the message unverified.
                integrity="passed" if errors == 0 else "unknown",
                integrity_note=(
                    "SITOR-B FEC: every character resolved"
                    if errors == 0
                    else f"SITOR-B FEC: {errors} unresolved character(s)"
                ),
            )
        ]
