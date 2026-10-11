# SPDX-License-Identifier: GPL-3.0-or-later
"""Morse station-identifier (ident) decoder shared by the VOR and ILS
navigation decoders.

Ported from SDRangel, commit 030963609f7a667c62259dbcf3cee7522880885c
(GPL-3.0-or-later), two source pairs:

- ``sdrbase/dsp/morsedemod.cpp`` / ``.h`` -- the CW ident demodulator state
  machine (bandpass -> mix to baseband -> lowpass -> moving average ->
  adaptive noise floor -> threshold -> dot/dash/space timing).
  Copyright (C) 2020, 2023 Jon Beniston, M7RCE <jon@beniston.com>;
  Copyright (C) 2020, 2022 Edouard Griffiths, F4EXB <f4exb06@gmail.com>.
- ``sdrbase/util/morse.cpp`` / ``.h`` -- the ASCII<->Morse code table.
  Copyright (C) 2012 maintech GmbH, written by Christian Daniel;
  Copyright (C) 2015-2020 Edouard Griffiths, F4EXB <f4exb06@gmail.com>;
  Copyright (C) 2020 Jon Beniston, M7RCE <jon@beniston.com>.

``plugins/channelrx/demodvor/vordemodsink.cpp`` feeds the demodulator the
AM envelope (``Complex(sqrt(magsq), 0.0)``) -- no voice bandpass, no prior
envelope-mean removal -- at a fixed identThreshold of 4.0 (linear SNR); that
sink and ``plugins/channelrx/demodils/ilsdemodsink.cpp`` additionally credit
Copyright (C) 2019-2020, 2022-2023 Edouard Griffiths, F4EXB and (C) 2020
Kacper Michajlow <kasper93@gmail.com>. msdr's ILS decoder already shares its
AM envelope computation with VOR (``navigation.py``'s ``magnitude``), so both
feed this module the same way SDRangel's VOR sink does, rather than
SDRangel ILS's separate raw-IQ path -- functionally equivalent, one caller
instead of two.

The bandpass+NCO+lowpass front end is collapsed into one step: mix the
1020 Hz ident subcarrier to baseband (a running sample counter keeps the
mixing phase continuous across calls), then a single real windowed-sinc
lowpass (Kaiser, beta 8 -- the same design used by
``pydecoders/endoftrain.py``'s ``_lowpass_taps``) stands in for upstream's
separate bandpass + lowpass stages. The filter keeps a carried-overlap
state (``_zi``) across calls, the same stateful-FIR pattern
``pydecoders/endoftrain.py`` uses, so there is no block-boundary ringing.
An earlier brick-wall FFT-per-block version (matching how ``navigation.py``
filters its own one-second measurement blocks) rang for hundreds of
milliseconds after a dot ended and mis-timed every symbol; a real FIR with
a tapered window was necessary here, unlike the measurement path's coarser
subcarrier isolation. Moving-average and noise-floor state are carried
across calls too (``process()`` is meant to be called once per streamed
block from a persistent instance), since idents repeat every ~10-30 s --
far longer than one block.

Timing matches upstream: dot/dash and letter/word gaps are judged against
fractions of the 10 wpm dot period regardless of actual keying speed (~7 wpm
for VOR/ILS), and a message is flushed on a >=7-dot10 gap after a character,
or after a >=10-dot7 run of silence with no new character.

Unlike upstream (which reports every flushed group), this module only
confirms an ident after the identical text has been flushed at least twice
in a row -- a single noisy decode should not become an identity. See
``_flush()``.
"""

from __future__ import annotations

import numpy as np

# ITU Morse table, A-Z and 0-9 (SDRangel's util/morse.cpp m_asciiToMorse,
# restricted to the characters a VOR/ILS ident can contain).
CHAR_TO_MORSE: dict[str, str] = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
}
MORSE_TO_CHAR: dict[str, str] = {code: char for char, code in CHAR_TO_MORSE.items()}

_IDENT_HZ = 1020.0
_LOWPASS_HZ = 100.0  # MorseDemod::applyChannelSettings: m_lowpassIdent cutoff
_LOWPASS_TAPS = 1001  # matches upstream's m_bandpassIdent tap count
_KAISER_BETA = 8.0
_N_BINS = 20  # MorseDemod::m_identBins
_DEFAULT_THRESHOLD = 4.0  # VORDemodSettings/ILSDemodSettings m_identThreshold


def _lowpass_taps(rate: float, cutoff_hz: float, numtaps: int) -> np.ndarray:
    # Windowed-sinc FIR, same design as pydecoders/endoftrain.py's
    # _lowpass_taps (Kaiser, beta 8): a tapered window keeps the impulse
    # response short-lived, unlike an abrupt frequency-domain cutoff.
    m = (numtaps - 1) / 2.0
    n = np.arange(numtaps, dtype=np.float64) - m
    h = 2 * cutoff_hz / rate * np.sinc(2 * cutoff_hz / rate * n)
    h *= np.kaiser(numtaps, _KAISER_BETA)
    return h / h.sum()


def morse_to_text(raw: str) -> str:
    """Port of ``Morse::toString``: space-separated dot/dash groups to text,
    silently dropping groups that don't match (unknown or empty)."""
    chars = []
    for group in raw.split(" "):
        char = MORSE_TO_CHAR.get(group)
        if char is not None:
            chars.append(char)
    return "".join(chars)


class MorseIdentDecoder:
    """Stateful Morse ident decoder fed one envelope block at a time.

    ``rate`` is the sample rate of the AM envelope passed to ``process()``
    (the same rate as the navigation decoder's measurement block). Create
    one instance per channel and keep feeding it across blocks -- its
    timing state, adaptive noise floor and two-repeat confirmation all span
    many calls.
    """

    def __init__(self, rate: float, *, ident_threshold: float = _DEFAULT_THRESHOLD):
        self.rate = float(rate)
        self.ident_threshold = float(ident_threshold)
        # MorseDemod::applyChannelSettings
        self._dot_10wpm = self.rate * 60.0 / (50 * 10)
        self._dot_7wpm = self.rate * 60.0 / (50 * 7)
        self._bin_len = max(1, int(self._dot_10wpm / 4))
        self._mavg_len = max(1, int(self._dot_10wpm / 5))
        self._thresh_dash = 2.0 * self._dot_10wpm
        self._thresh_dot = 0.2 * self._dot_10wpm
        self._thresh_word_gap = 7.0 * self._dot_10wpm
        self._thresh_letter_gap = 2.5 * self._dot_10wpm
        self._thresh_timeout = 10.0 * self._dot_7wpm

        self._h = _lowpass_taps(self.rate, _LOWPASS_HZ, _LOWPASS_TAPS)
        self._zi = np.zeros(len(self._h) - 1, dtype=np.complex128)
        self._mavg_tail = np.zeros(max(0, self._mavg_len - 1))
        self._bin_maxes = np.zeros(_N_BINS)
        self._bin_idx = 0
        self._bin_fill = 0
        self._noise_floor = 1e-4  # MorseDemod::reset: m_identNoise = 0.0001f
        self._prev_bit = 0
        self._bit_time = 0.0
        self._ident_raw = ""
        self._sample_pos = 0
        self._last_text: str | None = None
        self._repeat_count = 0

        # Confirmed output (None until two consecutive identical decodes).
        self.ident: str | None = None
        self.ident_repeats = 0
        self.ident_confidence: float | None = None

    def process(self, envelope: np.ndarray) -> None:
        """Feed one block of AM envelope samples (real, same rate as
        ``self.rate``, same block the navigation decoder already measures)."""
        n = len(envelope)
        if n == 0:
            return
        t = (np.arange(n, dtype=np.float64) + self._sample_pos) / self.rate
        self._sample_pos += n

        # Mix the 1020 Hz ident subcarrier to baseband (mixing is memoryless
        # per sample, so only the absolute sample count need carry over),
        # then the stateful lowpass FIR (overlap in self._zi, same pattern
        # as endoftrain.py's stateful convolution).
        mixed = envelope * np.exp(-2j * np.pi * _IDENT_HZ * t)
        joined = np.concatenate((self._zi, mixed))
        conv = np.convolve(joined, self._h, mode="full")
        baseband = conv[len(self._h) - 1 : len(self._h) - 1 + n]
        self._zi = joined[-(len(self._h) - 1) :].copy()
        c2 = np.abs(baseband)

        # Moving average (noise filter that preserves edges); carries its
        # tail across calls like the stateful FIR in endoftrain.py.
        joined = np.concatenate((self._mavg_tail, c2))
        mav = np.convolve(joined, np.ones(self._mavg_len) / self._mavg_len, mode="valid")
        if self._mavg_len > 1:
            self._mavg_tail = joined[-(self._mavg_len - 1) :].copy()

        noise = self._update_noise_floor(mav)
        bit = (mav / np.maximum(noise, 1e-20)) >= self.ident_threshold
        self._decode_bits(bit.astype(np.int8))

    def _update_noise_floor(self, mav: np.ndarray) -> np.ndarray:
        """Port of the bin-of-maximums noise floor in processOneSample.
        Approximation: the sample that completes a bin still sees the
        *previous* noise value (upstream recomputes before thresholding
        that exact sample) -- a one-sample-per-~30ms-bin discrepancy that
        cannot affect dot/dash timing at 7 wpm (dots are ~0.17 s)."""
        n = len(mav)
        out = np.empty(n)
        i = 0
        while i < n:
            take = min(self._bin_len - self._bin_fill, n - i)
            chunk_max = float(mav[i : i + take].max()) if take else 0.0
            if chunk_max > self._bin_maxes[self._bin_idx]:
                self._bin_maxes[self._bin_idx] = chunk_max
            out[i : i + take] = self._noise_floor
            self._bin_fill += take
            i += take
            if self._bin_fill >= self._bin_len:
                self._noise_floor = max(float(self._bin_maxes.min()), 1e-20)
                self._bin_fill = 0
                self._bin_idx = (self._bin_idx + 1) % _N_BINS
                self._bin_maxes[self._bin_idx] = 0.0
        return out

    def _decode_bits(self, bit: np.ndarray) -> None:
        n = len(bit)
        change_idx = np.flatnonzero(np.diff(bit)) + 1
        bounds = np.concatenate(([0], change_idx, [n]))
        for k in range(len(bounds) - 1):
            value = int(bit[bounds[k]])
            length = float(bounds[k + 1] - bounds[k])
            self._apply_run(value, length)

    def _apply_run(self, value: int, length: float) -> None:
        if value == self._prev_bit:
            # Continuation of a run spanning the previous call's boundary:
            # no edge, so no character/word decision here.
            self._bit_time += length
            if value == 0 and self._bit_time >= self._thresh_timeout:
                self._flush()
                self._bit_time %= self._thresh_timeout
            return
        if value == 1:
            # Rising edge: self._bit_time is the silence duration just ended.
            if self._bit_time > self._thresh_word_gap:
                self._flush()
            elif self._bit_time > self._thresh_letter_gap:
                self._ident_raw += " "
        else:
            # Falling edge: self._bit_time is the mark duration just ended.
            if self._bit_time > self._thresh_dash:
                self._ident_raw += "-"
            elif self._bit_time > self._thresh_dot:
                self._ident_raw += "."
            # else: too short even for a dot -- noise, ignored.
        self._prev_bit = value
        self._bit_time = length
        if value == 0 and self._bit_time >= self._thresh_timeout:
            self._flush()
            self._bit_time %= self._thresh_timeout

    def _flush(self) -> None:
        raw = self._ident_raw.strip()
        self._ident_raw = ""
        # Filter out noise that may appear as one or two characters
        # (MorseDemod::processOneSample's m_ident.trimmed().size() > 2).
        if len(raw) <= 2:
            return
        text = morse_to_text(raw)
        # Navaid idents are 2-4 letters/digits (VOR/DME 2-3, ILS "I" + 2-3);
        # anything else is noise. A repeated 1-letter "S" was observed at
        # about -9 dB SNR in testing, which this rejects.
        if not (2 <= len(text) <= 4 and text.isalnum()):
            return
        if text == self._last_text:
            self._repeat_count += 1
        else:
            self._last_text = text
            self._repeat_count = 1
        # An ident becomes an event only after being heard identically at
        # least twice in a row -- this is msdr's own rule (not upstream's),
        # so a single noisy decode can't masquerade as a station identity.
        if self._repeat_count >= 2:
            self.ident = text
            self.ident_repeats = self._repeat_count
            # Rises toward 1.0 with more consecutive identical decodes;
            # 0.5 at the minimum (two-repeat) confirmation.
            self.ident_confidence = round(1.0 - 1.0 / self._repeat_count, 3)
