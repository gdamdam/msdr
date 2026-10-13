"""BPSK31 decoder (31.25 baud differential BPSK, varicode), in-process.

Signal model (see `tests/decoder/test_psk31.py` for the matching modulator):
each symbol keeps (bit 1) or reverses (bit 0) the carrier phase, with raised-
cosine amplitude shaping, so the envelope passes through zero half-way between
symbol centres whenever the phase flips. Text is varicode: each character is a
bit string with no "00" inside, separated by "00".

Pipeline: carrier search on an averaged, smoothed spectrum (the strongest
~60 Hz wide hump in 300-2700 Hz) -> mix to complex baseband and boxcar-decimate
to 16 samples/symbol -> symbol clock from the 31.25 Hz line in |z|^2 (the
envelope dips at phase reversals) -> differential detection per symbol, with
a frequency loop driven by the symbol-to-symbol phase step (arg(d^2)/2 removes
the 0/pi data ambiguity) -> varicode -> text lines.

Known limitations: BPSK31 only (no QPSK/PSK63); one carrier per decoder (the
strongest); no squelch beyond the spectrum-hump test and a decision-quality
gate, so a strong non-PSK tone locks but decodes to nothing; the varicode table
was written out from the published table and is not checked against an
off-air recording.
"""

import time
from collections import deque

import numpy as np

from msdr.decoder.backends import DecodeEvent

_FS = 8000
_BAUD = 31.25
_SPS = int(_FS / _BAUD)  # 256 samples/symbol
_DECIM = 16
_SYM_D = _SPS // _DECIM  # decimated samples per symbol

# Carrier search.
_FFT_N = 8192
_ACQ_SAMPLES = 2 * _FFT_N
_BAND_HZ = (300.0, 2700.0)
_HUMP_BINS = 33  # ~32 Hz: wide enough to merge the two idle-pattern lines
_MIN_HUMP_RATIO = 4.0  # smoothed peak / median; white noise sits near 1.5

# Decision-quality gate (mean |cos| of the symbol phase step; noise ~0.64).
_Q_EMIT = 0.8
_Q_DROP = 0.6

_VARICODE: dict[str, str] = {
    "\t": "11101111", "\n": "11101", "\r": "11111", "\b": "1011111111",
    " ": "1", "!": "111111111", '"': "101011111", "#": "111110101",
    "$": "111011011", "%": "1011010101", "&": "1010111011", "'": "101111111",
    "(": "11111011", ")": "11110111", "*": "101101111", "+": "111011111",
    ",": "1110101", "-": "110101", ".": "1010111", "/": "110101111",
    "0": "10110111", "1": "10111101", "2": "11101101", "3": "11111111",
    "4": "101110111", "5": "101011011", "6": "101101011", "7": "110101101",
    "8": "110101011", "9": "110110111", ":": "11110101", ";": "110111101",
    "<": "111101101", "=": "1010101", ">": "111010111", "?": "1010101111",
    "@": "1010111101", "A": "1111101", "B": "11101011", "C": "10101101",
    "D": "10110101", "E": "1110111", "F": "11011011", "G": "11111101",
    "H": "101010101", "I": "1111111", "J": "111111101", "K": "101111101",
    "L": "11010111", "M": "10111011", "N": "11011101", "O": "10101011",
    "P": "11010101", "Q": "111011101", "R": "10101111", "S": "1101111",
    "T": "1101101", "U": "101010111", "V": "110110101", "W": "101011101",
    "X": "101110101", "Y": "101111011", "Z": "1010101101", "[": "111110111",
    "\\": "111101111", "]": "111111011", "^": "1010111111", "_": "101101101",
    "`": "1011011111", "a": "1011", "b": "1011111", "c": "101111",
    "d": "101101", "e": "11", "f": "111101", "g": "1011011", "h": "101011",
    "i": "1101", "j": "111101011", "k": "10111111", "l": "11011", "m": "111011",
    "n": "1111", "o": "111", "p": "111111", "q": "110111111", "r": "10101",
    "s": "10111", "t": "101", "u": "110111", "v": "1111011", "w": "1101011",
    "x": "11011111", "y": "1011101", "z": "111010101", "{": "1010110111",
    "|": "110111011", "}": "1010110101", "~": "1011010111",
}  # fmt: skip
_DECODE = {code: ch for ch, code in _VARICODE.items()}


def encode_varicode(text: str) -> list[int]:
    """Reference encoder (used by the tests): the bit stream for `text`, each
    character's varicode followed by the "00" separator. Characters without a
    table entry are skipped."""
    bits: list[int] = []
    for ch in text:
        code = _VARICODE.get(ch)
        if code is not None:
            bits.extend(int(c) for c in code)
            bits.extend((0, 0))
    return bits


class Psk31Decoder:
    """BPSK31 text decoder -- see module docstring."""

    input_rate = _FS

    def __init__(self) -> None:
        self._acq: list[np.ndarray] = []
        self._acq_n = 0
        self._locked = False
        self._reset_tracking(0.0)

    # -- public ---------------------------------------------------------------

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        x = samples.astype(np.float64)
        if not self._locked:
            self._acq.append(x)
            self._acq_n += len(x)
            if self._acq_n < _ACQ_SAMPLES:
                return []
            buf = np.concatenate(self._acq)
            self._acq, self._acq_n = [], 0
            fc = self._find_carrier(buf)
            if fc is None:
                return []
            self._reset_tracking(fc)
            self._locked = True
            x = buf  # replay the audio the search consumed
        events: list[DecodeEvent] = []
        for i in range(0, len(x), 2 * _SPS):
            self._mix_decimate(x[i : i + 2 * _SPS])
            events.extend(self._symbols())
            if not self._locked:
                break
        return events

    def flush(self) -> list[DecodeEvent]:
        return self._emit_line()

    # -- acquisition ----------------------------------------------------------

    @staticmethod
    def _find_carrier(buf: np.ndarray) -> float | None:
        n_seg = len(buf) // _FFT_N
        win = np.hanning(_FFT_N)
        power = np.zeros(_FFT_N // 2 + 1)
        for i in range(n_seg):
            power += np.abs(np.fft.rfft(buf[i * _FFT_N : (i + 1) * _FFT_N] * win)) ** 2
        bin_hz = _FS / _FFT_N
        lo, hi = int(_BAND_HZ[0] / bin_hz), int(_BAND_HZ[1] / bin_hz)
        smooth = np.convolve(power, np.ones(_HUMP_BINS) / _HUMP_BINS, mode="same")
        band = smooth[lo:hi]
        median = float(np.median(band))
        if median <= 0.0 or float(band.max()) < _MIN_HUMP_RATIO * median:
            return None
        peak = lo + int(np.argmax(band))
        a, b = max(peak - 32, 0), peak + 33
        freqs = np.arange(a, b) * bin_hz
        # Centroid, not the peak bin: idle (all phase reversals) has no carrier
        # line, only two lines at +-15.6 Hz that average to the carrier.
        return float(np.sum(freqs * power[a:b]) / np.sum(power[a:b]))

    # -- tracking -------------------------------------------------------------

    def _reset_tracking(self, fc: float) -> None:
        self._fc0 = fc
        self._fc = fc
        self._phase = 0.0
        self._zraw = np.zeros(0, dtype=np.complex128)
        self._r = np.zeros(0, dtype=np.complex128)  # decimated baseband
        self._r_base = 0  # absolute decimated index of _r[0]
        self._timing = 0j  # decayed sum of |r|^2 e^{-j2pi m/16}
        self._n_dec = 0
        self._next_c: int | None = None
        self._y_prev: complex | None = None
        self._eps = 0.0
        self._q = 0.5
        self._n_sym = 0
        self._code = ""
        self._line: list[str] = []
        self._idle = 0
        self._recent_q: deque[float] = deque(maxlen=8)

    def _mix_decimate(self, sub: np.ndarray) -> None:
        n = len(sub)
        step = 2 * np.pi * self._fc / _FS
        ph = self._phase + step * np.arange(n)
        self._phase = (self._phase + step * n) % (2 * np.pi)
        self._zraw = np.concatenate((self._zraw, sub * np.exp(-1j * ph)))
        # Boxcar of 2*_DECIM taps, one output per _DECIM samples: nulls the
        # 2*fc mixing image when it lands on a multiple of 250 Hz and
        # attenuates it by >30 dB elsewhere, at negligible cost to the
        # +-31 Hz signal.
        width = 2 * _DECIM
        n_out = (len(self._zraw) - width) // _DECIM + 1
        if n_out <= 0:
            return
        c = np.concatenate(([0j], np.cumsum(self._zraw)))
        idx = np.arange(n_out) * _DECIM
        r = (c[idx + width] - c[idx]) / width
        self._zraw = self._zraw[n_out * _DECIM :]
        m = self._r_base + len(self._r) + np.arange(n_out)
        # Symbol-clock line: the envelope peaks at symbol centres.
        decay = 0.999 ** n_out
        line = np.abs(r) ** 2 * np.exp(-2j * np.pi * m / _SYM_D)
        self._timing = self._timing * decay + np.sum(line)
        self._n_dec += n_out
        self._r = np.concatenate((self._r, r))

    def _symbols(self) -> list[DecodeEvent]:
        events: list[DecodeEvent] = []
        if self._next_c is None:
            if self._n_dec < 24 * _SYM_D:
                return events
            phase = (-np.angle(self._timing) / (2 * np.pi) * _SYM_D) % _SYM_D
            start = self._r_base + 2
            self._next_c = start + int(round(phase - start)) % _SYM_D
        end = self._r_base + len(self._r)
        while self._next_c + 2 < end:
            c = self._next_c - self._r_base
            y = complex(np.mean(self._r[c - 2 : c + 3]))
            events.extend(self._on_symbol(y))
            if not self._locked:
                return events
            phase = (-np.angle(self._timing) / (2 * np.pi) * _SYM_D) % _SYM_D
            nxt = self._next_c + _SYM_D
            delta = (phase - nxt + _SYM_D / 2) % _SYM_D - _SYM_D / 2
            # Hysteresis: a half-sample estimate must not make the clock dither.
            self._next_c = nxt + (int(np.sign(delta)) if abs(delta) > 0.75 else 0)
        # Clamp: next_c can already be past the samples we have.
        keep = min(max(self._next_c - 3 - self._r_base, 0), len(self._r))
        self._r = self._r[keep:]
        self._r_base += keep
        return events

    def _on_symbol(self, y: complex) -> list[DecodeEvent]:
        prev, self._y_prev = self._y_prev, y
        if prev is None:
            return []
        d = y * prev.conjugate()
        if abs(d) < 1e-12:
            return []
        # Residual carrier offset: the phase step is theta (bit 1) or theta+pi
        # (bit 0); squaring folds both onto 2*theta.
        eps = float(np.angle(d * d)) / 2
        self._eps = 0.8 * self._eps + 0.2 * eps
        dp = d * np.exp(-1j * self._eps)
        bit = 1 if dp.real > 0 else 0
        q = abs(dp.real) / abs(dp)
        self._q = 0.9 * self._q + 0.1 * q
        self._n_sym += 1
        # AFC: rad/symbol -> Hz is eps * baud / 2pi; pull-in is bounded to
        # +-10 Hz around the acquired carrier so a bad lock cannot wander off.
        self._fc = float(np.clip(self._fc + 0.1 * self._eps * _BAUD / (2 * np.pi),
                                 self._fc0 - 10.0, self._fc0 + 10.0))
        self._eps *= 0.9
        events: list[DecodeEvent] = []
        if self._n_sym > 48 and self._q < _Q_DROP:
            events.extend(self._emit_line())
            self._locked = False
            self._reset_tracking(0.0)
            return events
        events.extend(self._on_bit(bit))
        return events

    # -- varicode -> text -----------------------------------------------------

    def _on_bit(self, bit: int) -> list[DecodeEvent]:
        self._idle += 1
        events: list[DecodeEvent] = []
        if self._code == "0" and bit == 1:
            self._code = ""  # lone 0 can't start a code: resync
        self._code += str(bit)
        if self._code.endswith("00"):
            code, self._code = self._code[:-2], ""
            ch = _DECODE.get(code)
            if ch is not None and self._q >= _Q_EMIT:
                self._idle = 0
                events.extend(self._on_char(ch))
        elif len(self._code) > 12:
            self._code = ""
        if self._line and self._idle > 64:  # ~2 s of idle: the sender paused
            events.extend(self._emit_line())
        return events

    def _on_char(self, ch: str) -> list[DecodeEvent]:
        if ch in "\r\n":
            return self._emit_line()
        if ch == "\b":
            if self._line:
                self._line.pop()
            return []
        self._line.append(" " if ch == "\t" else ch)
        return self._emit_line() if len(self._line) >= 100 else []

    def _emit_line(self) -> list[DecodeEvent]:
        text = "".join(self._line).strip()
        self._line = []
        if not text:
            return []
        return [
            DecodeEvent(
                ts=time.time(),
                decoder="psk31",
                kind="text",
                text=text,
                fields={"carrier_hz": round(self._fc, 1), "quality": round(self._q, 2)},
            )
        ]
