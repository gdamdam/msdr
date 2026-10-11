# SPDX-License-Identifier: GPL-3.0-or-later
"""RTTY (Baudot/ITA2, 2-tone FSK) decoder with automatic baud-rate and
frequency-shift estimation -- audio-input backend (no external process).

Ported from SDRangel's RTTY demodulator, `plugins/channelrx/demodrtty/`
at commit `030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later,
Copyright (C) 2019-2021 Edouard Griffiths F4EXB, 2020-2023 Jon Beniston
M7RCE, 2020 Kacper Michajlow). Files consulted: `rttydemodsink.cpp/.h`
(mark/space correlator, automatic threshold correction (ATC), baud-rate
histogram estimator `estimateBaudRate()`, FFT-based frequency-shift
estimator `estimateFrequencyShift()`, bit-sync/Baudot framing in
`receiveBit()`), `rttydemodsettings.cpp/.h` (default baud 45.45 / shift
170 Hz, mark = higher frequency by default). The ITA2 character tables
are from `sdrbase/util/baudot.cpp` (same commit; Copyright (C) 2023 Jon
Beniston M7RCE), `m_ita2Letter`/`m_ita2Figure`.

Deliberate departures from SDRangel, all because this decoder runs on
real-valued SSB/mono audio rather than SDRangel's complex IQ baseband
with a user-set channel centre between mark and space:
 - Two absolute-frequency oscillators (mark_hz, space_hz) replace
   SDRangel's single +-shift/2 complex oscillator pair; mathematically
   equivalent correlators, just referenced to 0 Hz audio instead of a
   baseband centre.
 - Mark/space tone *position* in the audio passband is found by an FFT
   peak search (not supplied by a channel-centre tuning knob); "auto"
   mode searches freely for the strongest tone pair, "preset" modes
   constrain the search to pairs separated by the preset's shift.
 - Inverted (reversed) shift is detected by running the bit-sync state
   machine on both polarities of the same correlator output in
   parallel (the inverted stream is exactly the logical complement of
   the normal one) and keeping whichever has the better stop-bit
   framing record, rather than requiring the user to set "Mark/Space
   Frequency" by hand.
 - Only the ITA2 character set and LSB-first bit order are ported (the
   two settings every real ham/NWS RTTY transmission uses); SDRangel's
   UK/European/US/Russian/Murray tables, MSB-first option, squelch
   threshold and non-MAV correlator filters (lowpass/raised-cosine) are
   not ported -- the MAV (sliding-sum) filter path is used unconditionally.
 - A character is only ever appended to the decoded-text output once
   `RttyDecoder._state` reaches "locked"; before that (states
   "seeking_tone"/"seeking_baud"), both polarity pipelines run only to
   gather the statistics the lock decision needs. This -- plus requiring
   a clear FFT peak pair and several agreeing baud-histogram estimates
   before locking -- is what keeps pure-noise input silent.

Validation is synthetic only (tests/decoder/test_rtty.py): synthesized
FSK at the common rates (45.45/170, 50/450, 75/850) and an inverted
case, round-tripped against known ITA2 text.
"""

from __future__ import annotations

import collections
import math
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent

# -- ITA2 (Baudot) character tables, sdrbase/util/baudot.cpp m_ita2Letter/m_ita2Figure --
_ITA2_LETTERS = (
    "\x00",
    "E",
    "\n",
    "A",
    " ",
    "S",
    "I",
    "U",
    "\r",
    "D",
    "R",
    "J",
    "N",
    "F",
    "C",
    "K",
    "T",
    "Z",
    "L",
    "W",
    "H",
    "Y",
    "P",
    "Q",
    "O",
    "B",
    "G",
    "<",
    "M",
    "X",
    "V",
    ">",
)
_ITA2_FIGURES = (
    "\x00",
    "3",
    "\n",
    "-",
    " ",
    "'",
    "8",
    "7",
    "\r",
    "\x05",
    "4",
    "\a",
    ",",
    "!",
    ":",
    "(",
    "5",
    "+",
    ")",
    "2",
    "£",
    "6",
    "0",
    "1",
    "9",
    "?",
    "&",
    "<",
    ".",
    "/",
    "=",
    ">",
)

# Fixed presets (baud, shift_hz); mark = higher frequency (not inverted),
# matching RttyDemodSettings::resetToDefaults() m_spaceHigh=false default.
PRESETS: dict[str, tuple[float, float]] = {
    "amateur_45_170": (45.45, 170.0),
    "weather_50_450": (50.0, 450.0),
    "weather_50_85": (50.0, 85.0),
}

_COMMON_BAUDS = (45.45, 50.0, 75.0, 100.0)
_BAUD_SNAP_TOL = 1.5  # +/- baud: snap an estimate onto a common rate within this

_FFT_N = 1024  # ~10.8 Hz/bin @ 11,025 Hz -- SDRangel readme: shift accurate to ~10-20 Hz
_TONE_BAND_LO_HZ = 150.0
_TONE_BAND_HI_MARGIN_HZ = 150.0
_MIN_SEPARATION_HZ = 60.0  # keeps the two picks from being adjacent bins of one spectral lobe
_PEAK_SNR_MIN = 3.0  # peak bin power vs band median
_PRESET_SHIFT_TOL_HZ = 30.0
_TONE_STABLE_BLOCKS = 3  # consecutive FFT blocks agreeing before "stable"
_TONE_STABLE_TOL_HZ = 20.0

_EDGE_HIST_TRIGGER = 100  # RttyDemodSink m_edgeCount trigger
# SDRangel's m_clockHistogram (size 100) counts edges in samples at its
# internal 1 kHz decimated "channel sample rate", where a 45.45-baud bit
# period is ~22 samples. This decoder runs directly on the input audio
# (no decimation stage), where that same bit period is ~fs/45.45 samples
# (~242 at 11,025 Hz) -- the histogram is sized to cover several bit
# periods' worth of inter-edge gaps at that rate instead of a fixed 100.
_EDGE_HIST_MARGIN_BITS = 10
_BAUD_MA_LEN = 5  # MovingAverageUtil<Real, Real, 5> m_baudRateAverage
_BAUD_HISTORY_LEN = 5
# max-min over the last _BAUD_HISTORY_LEN raw estimates, for an arbitrary (non-common) rate
_BAUD_SPREAD_TOL = 0.3
_LOCK_BAUD_AGREEMENTS = 2
_DEFAULT_SEEK_BAUD = 45.45  # starting assumption while the real baud is being estimated

_WINNER_MIN_ATTEMPTS = 10  # valid+invalid framings before a polarity is trusted
_RESYNC_MIN_ATTEMPTS = 30
_RESYNC_INVALID_RATIO = 0.6

_IDLE_FLUSH_S = 2.0


def _nearest_common_baud(estimate: float) -> tuple[float, bool]:
    """(snapped_or_raw, snapped) -- snapped onto a common rate within tolerance."""
    best = min(_COMMON_BAUDS, key=lambda b: abs(b - estimate))
    if abs(best - estimate) <= _BAUD_SNAP_TOL:
        return best, True
    return estimate, False


class _MovingAvg:
    """Fixed-length moving average -- port of MovingAverageUtil<T, T, N>."""

    def __init__(self, n: int):
        self._buf: collections.deque[float] = collections.deque(maxlen=n)

    def push(self, value: float) -> float:
        self._buf.append(value)
        return self.value()

    def value(self) -> float:
        return sum(self._buf) / len(self._buf) if self._buf else 0.0

    def reset(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


class _MovingMax:
    """O(1)-amortised sliding-window maximum -- port of util/movingmaximum.h."""

    def __init__(self, size: int):
        self.size = max(1, size)
        self._dq: collections.deque[tuple[int, float]] = collections.deque()
        self._n = 0

    def resize(self, size: int) -> None:
        self.size = max(1, size)
        self._dq.clear()
        self._n = 0

    def push(self, value: float) -> float:
        i = self._n
        self._n += 1
        while self._dq and self._dq[-1][1] <= value:
            self._dq.pop()
        self._dq.append((i, value))
        while self._dq[0][0] <= i - self.size:
            self._dq.popleft()
        return self._dq[0][1]


class _BaudEstimator:
    """Inter-edge-interval histogram baud estimator -- port of
    RttyDemodSink::estimateBaudRate() / m_clockHistogram (see
    _EDGE_HIST_MARGIN_BITS for why the histogram is sized from `fs`
    rather than SDRangel's fixed 100 bins)."""

    def __init__(self, hist_size: int):
        self._hist_size = max(hist_size, 8)
        self._hist = [0] * self._hist_size
        self._edge_count = 0
        self._cycle_count = 0
        self._ma = _MovingAvg(_BAUD_MA_LEN)

    def tick(self) -> None:
        self._cycle_count += 1

    def edge(self, fs: float) -> float | None:
        """Call on every data transition; returns a fresh baud estimate once
        enough edges have been seen (every 100, as SDRangel), else None."""
        if self._cycle_count < self._hist_size:
            self._hist[self._cycle_count] += 1
            self._edge_count += 1
        self._cycle_count = 0
        if self._edge_count < _EDGE_HIST_TRIGGER:
            return None
        return self._estimate(fs)

    def _estimate(self, fs: float) -> float | None:
        idx = max(range(self._hist_size), key=lambda i: self._hist[i])
        idx = min(max(idx, 1), self._hist_size - 2)
        terms = [(self._hist[k], fs / k) for k in (idx - 1, idx, idx + 1) if k > 0]
        self._hist = [0] * self._hist_size
        self._edge_count = 0
        total = sum(c for c, _ in terms)
        if total == 0:
            return None
        est = sum(c * b for c, b in terms) / total
        return self._ma.push(est)

    def reset(self) -> None:
        self._hist = [0] * self._hist_size
        self._edge_count = 0
        self._cycle_count = 0
        self._ma.reset()


class _ToneEstimator:
    """FFT peak-pair (mark/space) finder -- port of
    RttyDemodSink::estimateFrequencyShift(), adapted to real audio (see
    module docstring): searches the whole passband for the strongest
    pair of tones instead of splitting a complex spectrum's +/- halves."""

    def __init__(self, fs: int, fft_n: int = _FFT_N):
        self.fs = fs
        self.n = fft_n
        self._window = np.hanning(fft_n)
        self._buf = np.zeros(fft_n, dtype=np.float64)
        self._fill = 0
        self._avg_mag: np.ndarray | None = None
        self._stable_pair: tuple[float, float] | None = None
        self._stable_count = 0

    def push(self, x: float) -> bool:
        """Feed one audio sample; True exactly when a fresh spectrum formed."""
        self._buf[self._fill] = x
        self._fill += 1
        if self._fill < self.n:
            return False
        self._fill = 0
        spectrum = np.fft.rfft(self._buf * self._window)
        mag = spectrum.real**2 + spectrum.imag**2
        self._avg_mag = mag if self._avg_mag is None else 0.875 * self._avg_mag + 0.125 * mag
        return True

    def find_pair(
        self, *, expect_shift_hz: float | None = None, tol_hz: float = _PRESET_SHIFT_TOL_HZ
    ) -> tuple[float, float] | None:
        """(lo_hz, hi_hz) of the strongest tone pair, or None if no clear pair.

        With expect_shift_hz set (fixed presets), only a pair separated by
        close to that shift is accepted."""
        if self._avg_mag is None:
            return None
        res = self.fs / self.n
        lo_bin = max(1, int(_TONE_BAND_LO_HZ / res))
        hi_bin = min(len(self._avg_mag) - 1, int((self.fs / 2 - _TONE_BAND_HI_MARGIN_HZ) / res))
        if hi_bin - lo_bin < 4:
            return None
        band = self._avg_mag[lo_bin : hi_bin + 1]
        median = float(np.median(band))
        if median <= 0:
            return None
        min_sep_bins = max(1, int(_MIN_SEPARATION_HZ / res))
        order = np.argsort(band)[::-1]
        picks: list[int] = []
        for i in order:
            b = int(i) + lo_bin
            if any(abs(b - p) < min_sep_bins for p in picks):
                continue
            if expect_shift_hz is not None and picks:
                if abs(abs(b - picks[0]) * res - expect_shift_hz) > tol_hz:
                    continue
            picks.append(b)
            if len(picks) == 2:
                break
        if len(picks) < 2:
            return None
        if min(band[p - lo_bin] for p in picks) < _PEAK_SNR_MIN * median:
            return None
        lo_hz, hi_hz = sorted(self._refine(p) * res for p in picks)
        return lo_hz, hi_hz

    def _refine(self, bin_idx: int) -> float:
        """Quadratic (parabolic) interpolation around a magnitude peak for
        sub-bin frequency accuracy -- standard DFT peak-interpolation trick,
        cheaper than raising the FFT size to hit the same resolution."""
        mag = self._avg_mag
        if bin_idx <= 0 or bin_idx >= len(mag) - 1:
            return float(bin_idx)
        left, centre, right = mag[bin_idx - 1], mag[bin_idx], mag[bin_idx + 1]
        denom = left - 2.0 * centre + right
        if denom == 0:
            return float(bin_idx)
        delta = 0.5 * (left - right) / denom
        return bin_idx + float(np.clip(delta, -0.5, 0.5))

    def update_stability(self, pair: tuple[float, float] | None) -> bool:
        """True once the same pair (within tolerance) has been seen for
        _TONE_STABLE_BLOCKS consecutive updates."""
        if pair is None:
            self._stable_pair = None
            self._stable_count = 0
            return False
        if self._stable_pair is not None and all(
            abs(a - b) <= _TONE_STABLE_TOL_HZ for a, b in zip(pair, self._stable_pair, strict=True)
        ):
            self._stable_count += 1
        else:
            self._stable_count = 1
        self._stable_pair = pair
        return self._stable_count >= _TONE_STABLE_BLOCKS

    def reset(self) -> None:
        self._avg_mag = None
        self._stable_pair = None
        self._stable_count = 0


class _Correlator:
    """Mark/space matched-filter correlator (sliding-sum/"FILTERED_MAV" filter
    path) + automatic threshold correction -- port of
    RttyDemodSink::processOneSample(), with two absolute-frequency
    oscillators in place of SDRangel's single +-shift/2 oscillator (see
    module docstring). The sliding sum alone ("MAV") rings for a sample or
    two right at each mark/space transition (its sum window straddles both
    symbols); SDRangel offers an extra lowpass stage for exactly this
    ("FILTERED_MAV": `m_lowpass1/2.filter()`, cutoff `baudRate * 1.1`) --
    used here unconditionally, as a single-pole IIR of the same cutoff, to
    keep those transients out of the edge-interval histogram."""

    def __init__(self, fs: int):
        self.fs = fs
        self._hi_inc = 0.0
        self._lo_inc = 0.0
        self._hi_phase = 0.0
        self._lo_phase = 0.0
        self.samples_per_bit = 1
        self._prods_hi: list[complex] = [0j]
        self._prods_lo: list[complex] = [0j]
        self._idx = 0
        self._sum_hi = 0j
        self._sum_lo = 0j
        self._lp_hi = 0.0
        self._lp_lo = 0.0
        self._alpha = 1.0
        self._mm_hi = _MovingMax(8)
        self._mm_lo = _MovingMax(8)

    def configure(self, lo_hz: float, hi_hz: float, samples_per_bit: int) -> None:
        self._hi_inc = 2.0 * math.pi * hi_hz / self.fs
        self._lo_inc = 2.0 * math.pi * lo_hz / self.fs
        samples_per_bit = max(1, int(round(samples_per_bit)))
        baud = self.fs / samples_per_bit
        cutoff_hz = baud * 1.1
        self._alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / self.fs)
        if samples_per_bit != self.samples_per_bit:
            self.samples_per_bit = samples_per_bit
            self._prods_hi = [0j] * samples_per_bit
            self._prods_lo = [0j] * samples_per_bit
            self._idx = 0
            self._sum_hi = 0j
            self._sum_lo = 0j
            self._lp_hi = 0.0
            self._lp_lo = 0.0
            self._mm_hi.resize(samples_per_bit * 8)
            self._mm_lo.resize(samples_per_bit * 8)

    def step(self, x: float) -> bool:
        """One audio sample -> data_normal bit (True = mark/idle, hi tone)."""
        self._hi_phase = math.fmod(self._hi_phase + self._hi_inc, 2.0 * math.pi)
        self._lo_phase = math.fmod(self._lo_phase + self._lo_inc, 2.0 * math.pi)
        corr_hi = x * complex(math.cos(self._hi_phase), -math.sin(self._hi_phase))
        corr_lo = x * complex(math.cos(self._lo_phase), -math.sin(self._lo_phase))
        old_hi = self._prods_hi[self._idx]
        old_lo = self._prods_lo[self._idx]
        self._prods_hi[self._idx] = corr_hi
        self._prods_lo[self._idx] = corr_lo
        self._sum_hi += corr_hi - old_hi
        self._sum_lo += corr_lo - old_lo
        self._idx = (self._idx + 1) % self.samples_per_bit
        self._lp_hi += self._alpha * (abs(self._sum_hi) - self._lp_hi)
        self._lp_lo += self._alpha * (abs(self._sum_lo) - self._lp_lo)
        env_hi = self._mm_hi.push(self._lp_hi)
        env_lo = self._mm_lo.push(self._lp_lo)
        bias_hi = self._lp_hi - 0.5 * env_hi
        bias_lo = self._lp_lo - 0.5 * env_lo
        return (bias_hi - bias_lo) > 0.0


class _BitSync:
    """SOP detection, bit-clock recovery and Baudot framing for one assumed
    polarity -- port of the SOP/clock-recovery/receiveBit() parts of
    RttyDemodSink::processOneSample()/receiveBit(). Unshift-on-space (a
    SDRangel setting, `m_unshiftOnSpace`) is applied unconditionally rather
    than defaulting off: with no manual "resync" control, a single
    misdecoded FIGS shift would otherwise garble everything after it for
    the rest of the session, and a space is common enough in real traffic
    to recover quickly."""

    def __init__(self):
        self.samples_per_bit = 1
        self._got_sop = False
        self._data_prev = False
        self._bits = 0
        self._bit_count = 0
        self._clock_count = 0
        self.figs = False
        self.valid = 0
        self.invalid = 0

    def configure(self, samples_per_bit: int) -> None:
        samples_per_bit = max(1, int(round(samples_per_bit)))
        if samples_per_bit != self.samples_per_bit:
            self.samples_per_bit = samples_per_bit
            self._got_sop = False
            self._bit_count = 0
            self._clock_count = 0

    def reset_stats(self) -> None:
        self.valid = 0
        self.invalid = 0

    def step(self, data: bool) -> str | None:
        """One data bit decision -> a printable character, or None."""
        out = None
        if not self._got_sop:
            if (not data) and self._data_prev:  # falling edge = start bit (space)
                self._got_sop = True
                self._bits = 0
                self._bit_count = 0
                self._clock_count = 0
        else:
            if self._clock_count == self.samples_per_bit // 2:
                out = self._receive_bit(data)
            self._clock_count = (self._clock_count + 1) % self.samples_per_bit
        self._data_prev = data
        return out

    def _receive_bit(self, bit: bool) -> str | None:
        self._bits = (int(bit) << 6) | (self._bits >> 1)
        self._bit_count += 1
        out = None
        if self._bit_count == 7:
            if (self._bits & 0x40) != 0x40:  # stop bit must be mark (1)
                self.invalid += 1
            else:
                self.valid += 1
                code = (self._bits >> 1) & 0x1F
                table = _ITA2_FIGURES if self.figs else _ITA2_LETTERS
                ch = table[code]
                if ch == ">":
                    self.figs = False
                elif ch == "<":
                    self.figs = True
                else:
                    if ch == " ":  # unshift-on-space (see class docstring)
                        self.figs = False
                    if ch != "\x00":
                        out = ch
            self._got_sop = False
        return out


class RttyDecoder:
    """PyDecoder: mono float32 audio -> RTTY (ITA2) text, with automatic
    baud-rate/frequency-shift/polarity estimation (mode="auto", default) or
    a fixed preset (mode in `PRESETS`, or mode="fixed" with explicit
    baud/shift_hz)."""

    input_rate = 11_025

    def __init__(
        self,
        mode: str = "auto",
        baud: float | None = None,
        shift_hz: float | None = None,
        inverted: bool | None = None,
    ):
        self.mode = mode
        self._fixed: tuple[float, float] | None
        if mode == "auto":
            self._fixed = None
        elif mode in PRESETS:
            self._fixed = PRESETS[mode] if baud is None or shift_hz is None else (baud, shift_hz)
        elif mode == "fixed":
            if baud is None or shift_hz is None:
                raise ValueError("mode='fixed' needs baud and shift_hz")
            self._fixed = (baud, shift_hz)
        else:
            raise ValueError(f"unknown RTTY mode {mode!r}")
        self._forced_inverted = inverted

        fs = self.input_rate
        self._tone = _ToneEstimator(fs)
        hist_size = int(math.ceil(fs / min(_COMMON_BAUDS) * _EDGE_HIST_MARGIN_BITS))
        self._baud_est = _BaudEstimator(hist_size)
        self._corr = _Correlator(fs)
        self._normal = _BitSync()
        self._inverted = _BitSync()
        self._sample_index = 0
        self._prev_bit: bool | None = None

        self._state = "seeking_tone"  # seeking_tone -> seeking_baud -> locked
        self._working_baud = _DEFAULT_SEEK_BAUD
        self._lo_hz: float | None = None
        self._hi_hz: float | None = None
        self._lock_streak = 0
        self._active_inverted = False
        self._baud_history: list[float] = []
        self._last_snapped: float | None = None

        self.baud = 0.0
        self.shift_hz = 0.0
        self.mark_hz = 0.0
        self.space_hz = 0.0
        self.inverted = False
        self.estimate_source = "preset" if self._fixed else "auto"
        self.confidence = 0.0

        self._text: list[str] = []
        self._last_char_sample: int | None = None

    # -- public PyDecoder surface --------------------------------------------

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        events: list[DecodeEvent] = []
        for x in np.asarray(samples, dtype=np.float64):
            events.extend(self._process_sample(float(x)))
            self._sample_index += 1
        idle_event = self._maybe_idle_flush()
        if idle_event is not None:
            events.append(idle_event)
        return events

    def flush(self) -> list[DecodeEvent]:
        event = self._emit_text()
        return [event] if event is not None else []

    # -- per-sample pipeline --------------------------------------------------

    def _process_sample(self, x: float) -> list[DecodeEvent]:
        fresh_spectrum = self._tone.push(x)
        if fresh_spectrum:
            self._on_spectrum_update()

        if self._lo_hz is None or self._hi_hz is None:
            return []

        data_normal = self._corr.step(x)
        data_inverted = not data_normal

        if self._prev_bit is not None:
            if data_normal != self._prev_bit:
                est = self._baud_est.edge(self.input_rate)
                if est is not None:
                    self._on_baud_estimate(est)
            else:
                self._baud_est.tick()
        self._prev_bit = data_normal

        normal_char = self._normal.step(data_normal)
        inverted_char = self._inverted.step(data_inverted)

        if self._state != "locked":
            self._maybe_lock()
            return []

        self._update_winner()
        emitted = inverted_char if self._active_inverted else normal_char
        events: list[DecodeEvent] = []
        if emitted is not None:
            event = self._append_char(emitted)
            if event is not None:
                events.append(event)
        self._maybe_resync()
        return events

    def _on_spectrum_update(self) -> None:
        expect = self._fixed[1] if self._fixed else None
        pair = self._tone.find_pair(expect_shift_hz=expect)
        stable = self._tone.update_stability(pair)
        if pair is not None:
            self._lo_hz, self._hi_hz = pair
            spb = self._samples_per_bit(self._working_baud)
            self._corr.configure(self._lo_hz, self._hi_hz, spb)
            self._normal.configure(spb)
            self._inverted.configure(spb)
            if self._state == "seeking_tone" and stable:
                self._state = "seeking_baud"
                if self._fixed:
                    self._working_baud = self._fixed[0]
                self._normal.reset_stats()
                self._inverted.reset_stats()

    def _samples_per_bit(self, baud: float) -> int:
        return max(1, int(round(self.input_rate / baud)))

    def _on_baud_estimate(self, estimate: float) -> None:
        if self._fixed or self._state == "locked":
            return
        # A plain "agrees with the immediately preceding estimate" test falsely
        # looks stable while the estimate is still monotonically converging
        # (e.g. 79.9 -> 77.9 -> 77.3 -> 77.1, each close to the last, none
        # near the true 75.0 yet -- the moving-average estimator is smooth by
        # construction, so small steps don't mean "arrived"). Once an estimate
        # snaps onto a common rate, require that *same* rate again (a strong,
        # discrete signal); otherwise (an arbitrary, non-common rate) require
        # tight agreement across several raw estimates, not just a shrinking
        # last step.
        self._baud_history.append(estimate)
        del self._baud_history[:-_BAUD_HISTORY_LEN]
        snapped, was_snapped = _nearest_common_baud(estimate)
        agree = (was_snapped and snapped == self._last_snapped) or (
            len(self._baud_history) == _BAUD_HISTORY_LEN
            and (max(self._baud_history) - min(self._baud_history)) <= _BAUD_SPREAD_TOL
        )
        self._last_snapped = snapped if was_snapped else None
        self._working_baud = snapped
        self._corr.configure(self._lo_hz, self._hi_hz, self._samples_per_bit(snapped))
        self._normal.configure(self._samples_per_bit(snapped))
        self._inverted.configure(self._samples_per_bit(snapped))
        self._lock_streak = self._lock_streak + 1 if agree else 1
        if self._lock_streak >= _LOCK_BAUD_AGREEMENTS:
            self._enter_lock(snapped)

    def _maybe_lock(self) -> None:
        if self._state != "seeking_baud" or not self._fixed:
            return
        # Fixed baud: only the tone (and polarity) need to be stable.
        if self._normal.valid + self._normal.invalid >= _WINNER_MIN_ATTEMPTS or (
            self._inverted.valid + self._inverted.invalid >= _WINNER_MIN_ATTEMPTS
        ):
            self._enter_lock(self._fixed[0])

    def _enter_lock(self, baud: float) -> None:
        self._state = "locked"
        self.baud = baud
        # Preset: report the fixed shift itself, not the (noisier) located
        # tone-pair spacing -- the located pair only has to be *close* to it
        # (see find_pair's expect_shift_hz tolerance).
        self.shift_hz = self._fixed[1] if self._fixed else round(self._hi_hz - self._lo_hz, 1)
        self._update_winner(force=True)
        self._lock_streak = max(self._lock_streak, 1)
        self.confidence = min(0.95, 0.5 + 0.1 * self._lock_streak)

    def _update_winner(self, force: bool = False) -> None:
        n_total = self._normal.valid + self._normal.invalid
        i_total = self._inverted.valid + self._inverted.invalid
        if not force and min(n_total, i_total) < _WINNER_MIN_ATTEMPTS:
            if n_total < _WINNER_MIN_ATTEMPTS and i_total < _WINNER_MIN_ATTEMPTS:
                return
        winner_inverted = self._inverted.valid > self._normal.valid
        if self._forced_inverted is not None:
            winner_inverted = self._forced_inverted
        self._active_inverted = winner_inverted
        self.inverted = winner_inverted
        if winner_inverted:
            self.mark_hz, self.space_hz = round(self._lo_hz, 1), round(self._hi_hz, 1)
        else:
            self.mark_hz, self.space_hz = round(self._hi_hz, 1), round(self._lo_hz, 1)

    def _maybe_resync(self) -> None:
        active = self._inverted if self._active_inverted else self._normal
        total = active.valid + active.invalid
        if total < _RESYNC_MIN_ATTEMPTS:
            return
        if active.invalid / total > _RESYNC_INVALID_RATIO:
            self._state = "seeking_tone"
            self._tone.reset()
            self._baud_est.reset()
            self._normal.reset_stats()
            self._inverted.reset_stats()
            self._lo_hz = self._hi_hz = None
            self._lock_streak = 0
            self.confidence = 0.0

    # -- text buffering ---------------------------------------------------------

    def _append_char(self, ch: str) -> DecodeEvent | None:
        self._text.append(ch)
        self._last_char_sample = self._sample_index
        if ch == "\n":
            return self._maybe_idle_flush(force=True)
        return None

    def _maybe_idle_flush(self, force: bool = False) -> DecodeEvent | None:
        if not self._text:
            return None
        if not force:
            if self._last_char_sample is None:
                return None
            idle_samples = int(_IDLE_FLUSH_S * self.input_rate)
            if (self._sample_index - self._last_char_sample) < idle_samples:
                return None
        return self._emit_text()

    def _emit_text(self) -> DecodeEvent | None:
        text = "".join(self._text).strip()
        self._text = []
        self._last_char_sample = None
        if not text:
            return None
        return DecodeEvent(
            ts=time.time(),
            decoder="rtty",
            kind="text",
            text=text,
            fields={
                "baud": round(self.baud, 2),
                "shift_hz": self.shift_hz,
                "mark_hz": self.mark_hz,
                "space_hz": self.space_hz,
                "inverted": self.inverted,
                "estimate_source": self.estimate_source,
                "confidence": round(self.confidence, 2),
            },
            integrity="unavailable",
            integrity_note=(
                "msdr native RTTY demodulator exposes no validity flag "
                "(Baudot/ITA2 carries no parity or FEC); stop-bit framing is checked "
                "but is not an error-detecting code."
            ),
        )
