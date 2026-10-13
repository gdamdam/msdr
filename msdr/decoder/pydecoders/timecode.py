"""WWV/WWVH, DCF77 and MSF time-code decoders -- round 2 -- plus JJY, WWVB and CHU.

JJY and WWVB (bottom of this file) reuse the same envelope front end with a
shared frame engine (`_PulseFrameDecoder`); CHU is different in kind (a
Bell 103 300-baud FSK burst in seconds 31-39, not a pulse width) and has its
own FSK demodulator. Their frame layouts are reconstructions too, with the
same caveats as the three below, and CHU's A/B byte layout in particular is
this module's own reading of the format (not checked against an off-air
recording).

Shared signal model: a once-per-second pulse whose ON (WWV/WWVH) or OFF
(DCF77/MSF) duration carries one symbol. All three decoders share the same
DSP front end (`_EnvelopeEdges`: full-wave rectify -> one-pole low-pass
envelope -> adaptive peak-hold Schmitt trigger) and the same host-clock
mapping (`_HostClock`: anchors the sample count to `clock()` at the end of
each feed() call, so a pulse's sample index can be turned into an estimated
host wall-clock time -- this is what `offset_s` is measured against).

Frame layouts below are this module's own reconstructions of the real
WWV/WWVH (NIST SP 432), DCF77 and MSF (Anthorn) time codes -- correct in
spirit (pulse widths, marker framing, field set: minutes/hours/date/year,
DUT1, leap flag, parity) but not verified against an off-air recording, and
in places (MSF especially) simplified rather than bit-exact to the published
spec. Each decoder's own docstring documents its exact bit table; the
matching `encode_*` functions (this module's reference encoders, used by
this package's own tests the same way `navtex.encode_message` is) make the
layout unambiguous.

Known limitations, all three decoders:
 - No self-clocking bit/carrier recovery beyond the Schmitt-trigger edge
   detector -- fine for a clean synthetic signal, not for a noisy HF/LF
   receive path with fading or multipath.
 - `offset_s` carries a small fixed bias from the envelope filter's group
   delay (a few ms to ~12 ms for WWV's band-pass) -- not compensated.
 - DCF77's frame boundary is a *missing* pulse (real spec), so a decode only
   fires once the following minute's first pulse arrives (~1-2s after the
   minute it describes) -- WWV/MSF, which frame on a present marker pulse,
   decode immediately.
 - WWV/WWVH station identification (500/600 Hz tone schedule) is not
   implemented; `station` always reports the "WWV/WWVH" fallback the plan
   allows for "if detectable, else -- ".
"""

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import numpy as np

from msdr.decoder.backends import DecodeEvent
from msdr.decoder.pydecoders.wefax import _StreamFIR

_FS = 11025


# -- shared DSP / framing helpers --------------------------------------------


def _bandpass_taps(fs: float, center_hz: float, half_bw_hz: float, num_taps: int) -> np.ndarray:
    """Windowed-sinc band-pass FIR (difference of two low-pass sincs) -- the
    plan's "narrow band-pass" for WWV's 100 Hz subcarrier."""
    m = (num_taps - 1) // 2
    n = np.arange(-m, m + 1)
    f_lo = (center_hz - half_bw_hz) / fs
    f_hi = (center_hz + half_bw_hz) / fs
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(
            n == 0,
            2.0 * (f_hi - f_lo),
            (np.sin(2 * np.pi * f_hi * n) - np.sin(2 * np.pi * f_lo * n)) / (np.pi * n),
        )
    h = h * np.hamming(num_taps)
    return h.astype(np.float64)


class _EnvelopeEdges:
    """Full-wave rectify -> one-pole low-pass envelope -> adaptive peak-hold
    Schmitt trigger. process() returns [(is_high, sample_index), ...], one
    entry per state change, sample_index counting from this detector's first
    sample ever (stable across feed() calls)."""

    def __init__(
        self,
        fs: float,
        smooth_s: float = 0.004,
        peak_tau_s: float = 1.5,
        high_frac: float = 0.6,
        low_frac: float = 0.3,
    ):
        smooth_n = max(1, int(round(fs * smooth_s)))
        self._alpha = 1.0 / smooth_n
        self._peak_decay = math.exp(-1.0 / (fs * peak_tau_s))
        self._high_frac = high_frac
        self._low_frac = low_frac
        self._env = 0.0
        # Seeded at the input's theoretical max (PythonBackend normalises
        # s16 PCM to +-1.0) rather than ~0: peak-hold's own update rule
        # (`peak = max(peak * decay, env)`) means an initial peak smaller
        # than the very first sample's envelope collapses instantly to
        # env itself, making every threshold trivially satisfied (a
        # spurious transition at t=0). Starting high and decaying down
        # (tau=peak_tau_s) converges on the real envelope peak within a
        # fraction of a second for any realistic signal, at the cost of
        # possibly missing (never misclassifying) the first pulse or two.
        self._peak = 1.0
        self._is_high = False
        self._sample_idx = 0

    def process(self, samples: np.ndarray) -> list[tuple[bool, int]]:
        events: list[tuple[bool, int]] = []
        env = self._env
        peak = self._peak
        is_high = self._is_high
        alpha = self._alpha
        decay = self._peak_decay
        high_frac = self._high_frac
        low_frac = self._low_frac
        idx0 = self._sample_idx
        for i, v in enumerate(np.abs(samples)):
            env += alpha * (float(v) - env)
            peak = max(peak * decay, env)
            if is_high and env < low_frac * peak:
                is_high = False
                events.append((False, idx0 + i))
            elif not is_high and env > high_frac * peak:
                is_high = True
                events.append((True, idx0 + i))
        self._env = env
        self._peak = peak
        self._is_high = is_high
        self._sample_idx = idx0 + len(samples)
        return events


class _HostClock:
    """Maps a sample index to an estimated host wall-clock time, by
    anchoring the instant `clock()` is called (once per feed()) to the total
    sample count at that instant -- see module docstring."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._total = 0
        self._anchor_time: float | None = None
        self._anchor_total = 0

    def mark_feed(self, n_new_samples: int) -> None:
        self._total += n_new_samples
        self._anchor_time = self._clock()
        self._anchor_total = self._total

    def host_time_at(self, sample_idx: int, fs: float) -> float:
        if self._anchor_time is None:
            return self._clock()
        return self._anchor_time - (self._anchor_total - sample_idx) / fs


def _bcd(bits: list[str], weights: list[int]) -> int:
    return sum(w for b, w in zip(bits, weights, strict=True) if b == "1")


def _set_digit(bits: list[str], start: int, digit: int, n: int) -> None:
    """Inverse of _bcd: writes `digit`'s own low n binary bits into
    bits[start:start+n] (bit i -> bits[start+i]) -- encode-side counterpart,
    shared by all three `encode_*` reference encoders below."""
    for i in range(n):
        bits[start + i] = "1" if digit & (1 << i) else "0"


def _parity_integrity(checks: dict[str, bool]) -> dict[str, str]:
    """Matrix policy "parity": passed/failed from this frame's own parity
    (and framing) bits -- DecodeEvent integrity/integrity_note kwargs."""
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return {"integrity": "failed", "integrity_note": "parity failed: " + ", ".join(failed)}
    return {"integrity": "passed", "integrity_note": "parity verified: " + ", ".join(checks)}


def _even_parity_bit(bits: list[str]) -> str:
    ones = sum(1 for b in bits if b == "1")
    return "1" if ones % 2 == 1 else "0"


# =============================================================================
# WWV / WWVH
# =============================================================================

_WWV_MARKER_POSITIONS = (0, 9, 19, 29, 39, 49, 59)
_WWV_NOMINAL_S = {"0": 0.170, "1": 0.470, "M": 0.770}


def _classify_wwv_pulse(width_s: float) -> tuple[str | None, float]:
    bands = (("0", 0.170, 0.12, 0.32), ("1", 0.470, 0.32, 0.62), ("M", 0.770, 0.62, 0.90))
    for label, nominal, lo, hi in bands:
        if lo <= width_s < hi:
            conf = max(0.4, 1.0 - abs(width_s - nominal) / (nominal * 0.6))
            return label, conf
    return None, 0.0


def encode_wwv_minute(dt: datetime, *, dut1_s: float = 0.0, leap: bool = False) -> list[str]:
    """Reference encoder (used by this package's tests): the 60 per-second
    labels ('0'/'1'/'M') for the UTC minute starting at `dt` (dt.second==0).

    Bit table (this decoder's own reconstruction, see module docstring):
      0,9,19,29,39,49,59: marker (770 ms)
      1-4: minute units [1,2,4,8]      6-8: minute tens [10,20,40]
      10-13: hour units [1,2,4,8]      15-16: hour tens [10,20]
      20-23: day-of-year units [1,2,4,8]   25-28: day-of-year tens [10,20,40,80]
      30-31: day-of-year hundreds [100,200]
      36: DUT1 sign (1=positive)       40-43: DUT1 magnitude, tenths of a second
      45-48: year units [1,2,4,8]      50-53: year tens [10,20,40,80]
      54: leap-second pending
      all other seconds: unused (0)
    """
    minute, hour = dt.minute, dt.hour
    day = dt.timetuple().tm_yday
    year = dt.year % 100
    bits = ["0"] * 60
    for pos in _WWV_MARKER_POSITIONS:
        bits[pos] = "M"
    _set_digit(bits, 1, minute % 10, 4)
    _set_digit(bits, 6, minute // 10, 3)
    _set_digit(bits, 10, hour % 10, 4)
    _set_digit(bits, 15, hour // 10, 2)
    _set_digit(bits, 20, day % 10, 4)
    _set_digit(bits, 25, (day // 10) % 10, 4)
    _set_digit(bits, 30, day // 100, 2)
    bits[36] = "1" if dut1_s >= 0 else "0"
    _set_digit(bits, 40, int(round(abs(dut1_s) * 10)), 4)
    _set_digit(bits, 45, year % 10, 4)
    _set_digit(bits, 50, year // 10, 4)
    bits[54] = "1" if leap else "0"
    return bits


class WwvDecoder:
    """WWV/WWVH 100 Hz subcarrier BCD time code -- see module + this class's
    docstring in `encode_wwv_minute` for the bit table."""

    input_rate = _FS

    def __init__(self, clock: Callable[[], float] = time.time):
        fs = self.input_rate
        self._bp = _StreamFIR(_bandpass_taps(fs, 100.0, 15.0, 257))
        self._edges = _EnvelopeEdges(fs)
        self._host_clock = _HostClock(clock)
        self._rise: int | None = None
        self._pulses: list[tuple[int, str, float]] = []

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._host_clock.mark_feed(len(samples))
        filtered = self._bp.process(samples.astype(np.float64))
        out: list[DecodeEvent] = []
        for is_high, idx in self._edges.process(filtered):
            if is_high:
                self._rise = idx
            elif self._rise is not None:
                width_s = (idx - self._rise) / self.input_rate
                out.extend(self._on_pulse(self._rise, width_s))
                self._rise = None
        return out

    def flush(self) -> list[DecodeEvent]:
        return []

    def _on_pulse(self, start_idx: int, width_s: float) -> list[DecodeEvent]:
        label, conf = _classify_wwv_pulse(width_s)
        if label is None:
            return []
        self._pulses.append((start_idx, label, conf))
        if len(self._pulses) > 130:
            self._pulses = self._pulses[-130:]
        if len(self._pulses) < 60:
            return []
        window = self._pulses[-60:]
        if all(window[p][1] == "M" for p in _WWV_MARKER_POSITIONS):
            event = self._decode_minute(window)
            self._pulses = []
            return [event] if event is not None else []
        return []

    def _decode_minute(self, window: list[tuple[int, str, float]]) -> DecodeEvent | None:
        bits = [w[1] for w in window]
        confs = [w[2] for w in window]

        minute = _bcd(bits[1:5], [1, 2, 4, 8]) + _bcd(bits[6:9], [10, 20, 40])
        hour = _bcd(bits[10:14], [1, 2, 4, 8]) + _bcd(bits[15:17], [10, 20])
        day = (
            _bcd(bits[20:24], [1, 2, 4, 8])
            + _bcd(bits[25:29], [10, 20, 40, 80])
            + _bcd(bits[30:32], [100, 200])
        )
        dut1_sign = 1 if bits[36] == "1" else -1
        dut1_mag = _bcd(bits[40:44], [1, 2, 4, 8]) * 0.1
        year = _bcd(bits[45:49], [1, 2, 4, 8]) + _bcd(bits[50:54], [10, 20, 40, 80])
        leap = bits[54] == "1"

        if not (0 <= minute <= 59 and 0 <= hour <= 23 and 1 <= day <= 366):
            return None
        try:
            minute_start = datetime(2000 + year, 1, 1, tzinfo=UTC) + timedelta(
                days=day - 1, hours=hour, minutes=minute
            )
        except (ValueError, OverflowError):
            return None

        host_time = self._host_clock.host_time_at(window[0][0], self.input_rate)
        offset_s = host_time - minute_start.timestamp()
        quality = max(0.0, min(1.0, sum(confs) / len(confs)))

        return DecodeEvent(
            ts=time.time(),
            decoder="wwv",
            kind="time",
            text=f"UTC {minute_start:%H:%M:%S} · host {offset_s:+.2f} s",
            fields={
                "utc": minute_start.isoformat(),
                "offset_s": offset_s,
                "dut1": dut1_sign * dut1_mag,
                "leap": leap,
                "station": "WWV/WWVH",
                "quality": quality,
            },
            # Matrix policy "builtin_check": the marker framing and the
            # minute/hour/day range + calendar check above passed (a frame
            # failing either never reaches here).
            integrity="passed",
            integrity_note="WWV marker framing and field range check",
        )


# =============================================================================
# DCF77
# =============================================================================


def _classify_dcf77_pulse(width_s: float) -> tuple[str | None, float]:
    if 0.05 <= width_s < 0.15:
        return "0", 1.0
    if 0.15 <= width_s < 0.30:
        return "1", 1.0
    return None, 0.0


def encode_dcf77_minute(dt: datetime, *, dst: bool = False, leap: bool = False) -> list[str]:
    """Reference encoder: the 59 per-second bit labels ('0'/'1') for bits
    0-58 of the DCF77 frame describing the CET/CEST minute starting at
    `dt` (dt.second==0, dt.tzinfo set to the fixed CET/CEST offset the
    caller intends -- this function does not itself apply DST math).

    Bit table (real DCF77 layout):
      0: always 0             17: CEST active   18: CET active
      19: leap-second announce   20: start-of-time (always 1)
      21-24: minute units [1,2,4,8]   25-27: minute tens [10,20,40]   28: parity
      29-32: hour units [1,2,4,8]     33-34: hour tens [10,20]        35: parity
      36-39: day units [1,2,4,8]      40-41: day tens [10,20]
      42-44: weekday [1,2,4]
      45-48: month units [1,2,4,8]    49: month tens [10]
      50-53: year units [1,2,4,8]     54-57: year tens [10,20,40,80]  58: parity
      second 59: no pulse (the minute marker) -- not part of this list.
    """
    bits = ["0"] * 59
    bits[17] = "1" if dst else "0"
    bits[18] = "0" if dst else "1"
    bits[19] = "1" if leap else "0"
    bits[20] = "1"
    _set_digit(bits, 21, dt.minute % 10, 4)
    _set_digit(bits, 25, dt.minute // 10, 3)
    bits[28] = _even_parity_bit(bits[21:28])
    _set_digit(bits, 29, dt.hour % 10, 4)
    _set_digit(bits, 33, dt.hour // 10, 2)
    bits[35] = _even_parity_bit(bits[29:35])
    _set_digit(bits, 36, dt.day % 10, 4)
    _set_digit(bits, 40, dt.day // 10, 2)
    _set_digit(bits, 42, dt.isoweekday(), 3)
    _set_digit(bits, 45, dt.month % 10, 4)
    _set_digit(bits, 49, dt.month // 10, 1)
    year = dt.year % 100
    _set_digit(bits, 50, year % 10, 4)
    _set_digit(bits, 54, year // 10, 4)
    bits[58] = _even_parity_bit(bits[36:58])
    return bits


class Dcf77Decoder:
    """DCF77 77.5 kHz carrier drops -- 100 ms = 0, 200 ms = 1, second 59 has
    no drop (the minute marker: this decoder detects it as a ~2 s gap
    between consecutive pulses instead of a ~1 s one). See
    `encode_dcf77_minute` for the bit table."""

    input_rate = _FS

    def __init__(self, clock: Callable[[], float] = time.time):
        fs = self.input_rate
        self._edges = _EnvelopeEdges(fs)
        self._host_clock = _HostClock(clock)
        self._fall: int | None = None
        self._pulses: list[tuple[int, str, float]] = []
        self._prev_start: int | None = None

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._host_clock.mark_feed(len(samples))
        out: list[DecodeEvent] = []
        for is_high, idx in self._edges.process(samples.astype(np.float64)):
            if not is_high:
                self._fall = idx
            elif self._fall is not None:
                width_s = (idx - self._fall) / self.input_rate
                out.extend(self._on_pulse(self._fall, width_s))
                self._fall = None
        return out

    def flush(self) -> list[DecodeEvent]:
        return []

    def _on_pulse(self, start_idx: int, width_s: float) -> list[DecodeEvent]:
        label, conf = _classify_dcf77_pulse(width_s)
        if label is None:
            return []
        events: list[DecodeEvent] = []
        if self._prev_start is not None:
            gap_s = (start_idx - self._prev_start) / self.input_rate
            if gap_s > 1.5:
                if len(self._pulses) == 59:
                    event = self._decode_minute(self._pulses)
                    if event is not None:
                        events.append(event)
                self._pulses = []
        self._pulses.append((start_idx, label, conf))
        self._prev_start = start_idx
        if len(self._pulses) > 65:
            self._pulses = self._pulses[-65:]
        return events

    def _decode_minute(self, pulses: list[tuple[int, str, float]]) -> DecodeEvent | None:
        bits = [p[1] for p in pulses]
        confs = [p[2] for p in pulses]

        minute = _bcd(bits[21:25], [1, 2, 4, 8]) + _bcd(bits[25:28], [10, 20, 40])
        minute_ok = _even_parity_bit(bits[21:28]) == bits[28]
        hour = _bcd(bits[29:33], [1, 2, 4, 8]) + _bcd(bits[33:35], [10, 20])
        hour_ok = _even_parity_bit(bits[29:35]) == bits[35]
        day = _bcd(bits[36:40], [1, 2, 4, 8]) + _bcd(bits[40:42], [10, 20])
        month = _bcd(bits[45:49], [1, 2, 4, 8]) + _bcd(bits[49:50], [10])
        year = _bcd(bits[50:54], [1, 2, 4, 8]) + _bcd(bits[54:58], [10, 20, 40, 80])
        date_ok = _even_parity_bit(bits[36:58]) == bits[58]
        start_ok = bits[20] == "1"
        cest, cet = bits[17] == "1", bits[18] == "1"
        leap = bits[19] == "1"

        utc_offset_h = 2 if cest else 1
        anomaly = cest == cet  # both or neither set -- ambiguous, fall back to CET

        try:
            local_dt = datetime(2000 + year, month, day, hour, minute)
        except ValueError:
            return None
        utc_dt = (local_dt - timedelta(hours=utc_offset_h)).replace(tzinfo=UTC)

        host_time = self._host_clock.host_time_at(pulses[0][0], self.input_rate)
        offset_s = host_time - utc_dt.timestamp()

        dcf_parity_ok = minute_ok and hour_ok and date_ok and start_ok
        quality = sum(confs) / len(confs)
        for ok in (minute_ok, hour_ok, date_ok, start_ok):
            if not ok:
                quality *= 0.6
        if anomaly:
            quality *= 0.6
        quality = max(0.0, min(1.0, quality))

        return DecodeEvent(
            ts=time.time(),
            decoder="dcf77",
            kind="time",
            text=f"UTC {utc_dt:%H:%M:%S} · host {offset_s:+.2f} s",
            fields={
                "utc": utc_dt.isoformat(),
                "offset_s": offset_s,
                "dut1": None,
                "leap": leap,
                "station": "DCF77",
                "quality": quality,
                "parity_ok": dcf_parity_ok,
            },
            **_parity_integrity(
                {"minute": minute_ok, "hour": hour_ok, "date": date_ok, "start bit": start_ok}
            ),
        )


# =============================================================================
# MSF
# =============================================================================


def _classify_msf_pulse(width_s: float) -> tuple[str | None, float]:
    bands = (
        ("00", 0.100, 0.05, 0.15),
        ("10", 0.200, 0.15, 0.25),
        ("01", 0.300, 0.25, 0.35),
        ("11", 0.400, 0.35, 0.45),
        ("M", 0.500, 0.45, 0.60),
    )
    for label, _nominal, lo, hi in bands:
        if lo <= width_s < hi:
            return label, 1.0
    return None, 0.0


def encode_msf_minute(dt: datetime, *, dst: bool = False) -> list[str]:
    """Reference encoder: the 60 per-second labels for the MSF frame
    describing the UK-local minute starting at `dt` (dt.second==0). Label
    at second 0 is "M" (the 500 ms minute-marker pulse); at seconds 1-59 it
    is a 2-character "AB" pair (each in "01") from the width table above.

    Bit table (this decoder's own simplified layout -- not the real MSF
    on-air code, which time-division-multiplexes DUT1 into the same
    seconds; see module docstring):
      A channel (data): 1-4 minute units [1,2,4,8], 5-7 minute tens [10,20,40]
        8-11 hour units [1,2,4,8], 12-13 hour tens [10,20]
        14-17 day units [1,2,4,8], 18-19 day tens [10,20]
        20-22 weekday [1,2,4]
        23-26 month units [1,2,4,8], 27 month tens [10]
        28-31 year units [1,2,4,8], 32-35 year tens [10,20,40,80]
      B channel (all 0 except): 54 minute parity, 55 hour parity,
        56 day+weekday parity, 57 month+year parity, 58 DST active
    """
    a = ["0"] * 60
    b = ["0"] * 60
    _set_digit(a, 1, dt.minute % 10, 4)
    _set_digit(a, 5, dt.minute // 10, 3)
    _set_digit(a, 8, dt.hour % 10, 4)
    _set_digit(a, 12, dt.hour // 10, 2)
    _set_digit(a, 14, dt.day % 10, 4)
    _set_digit(a, 18, dt.day // 10, 2)
    _set_digit(a, 20, dt.isoweekday(), 3)
    _set_digit(a, 23, dt.month % 10, 4)
    _set_digit(a, 27, dt.month // 10, 1)
    year = dt.year % 100
    _set_digit(a, 28, year % 10, 4)
    _set_digit(a, 32, year // 10, 4)
    b[54] = _even_parity_bit(a[1:8])
    b[55] = _even_parity_bit(a[8:14])
    b[56] = _even_parity_bit(a[14:23])
    b[57] = _even_parity_bit(a[23:36])
    b[58] = "1" if dst else "0"

    labels = ["M"]
    for i in range(1, 60):
        labels.append(a[i] + b[i])
    return labels


class MsfDecoder:
    """MSF 60 kHz carrier drops -- 100/200/300/400 ms = the four (A,B) bit
    pairs, 500 ms = the minute marker. See `encode_msf_minute` for the bit
    table (this decoder's own simplified layout, not the real on-air MSF
    code -- see module docstring)."""

    input_rate = _FS

    def __init__(self, clock: Callable[[], float] = time.time):
        fs = self.input_rate
        self._edges = _EnvelopeEdges(fs)
        self._host_clock = _HostClock(clock)
        self._fall: int | None = None
        self._pulses: list[tuple[int, str, float]] = []

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._host_clock.mark_feed(len(samples))
        out: list[DecodeEvent] = []
        for is_high, idx in self._edges.process(samples.astype(np.float64)):
            if not is_high:
                self._fall = idx
            elif self._fall is not None:
                width_s = (idx - self._fall) / self.input_rate
                out.extend(self._on_pulse(self._fall, width_s))
                self._fall = None
        return out

    def flush(self) -> list[DecodeEvent]:
        return []

    def _on_pulse(self, start_idx: int, width_s: float) -> list[DecodeEvent]:
        label, conf = _classify_msf_pulse(width_s)
        if label is None:
            return []
        self._pulses.append((start_idx, label, conf))
        if len(self._pulses) > 130:
            self._pulses = self._pulses[-130:]
        if len(self._pulses) < 60:
            return []
        window = self._pulses[-60:]
        if window[0][1] == "M":
            event = self._decode_minute(window)
            self._pulses = []
            return [event] if event is not None else []
        return []

    def _decode_minute(self, window: list[tuple[int, str, float]]) -> DecodeEvent | None:
        a = ["0"] * 60
        b = ["0"] * 60
        confs = [w[2] for w in window]
        for i in range(1, 60):
            lbl = window[i][1]
            a[i], b[i] = lbl[0], lbl[1]

        minute = _bcd(a[1:5], [1, 2, 4, 8]) + _bcd(a[5:8], [10, 20, 40])
        minute_ok = b[54] == _even_parity_bit(a[1:8])
        hour = _bcd(a[8:12], [1, 2, 4, 8]) + _bcd(a[12:14], [10, 20])
        hour_ok = b[55] == _even_parity_bit(a[8:14])
        day = _bcd(a[14:18], [1, 2, 4, 8]) + _bcd(a[18:20], [10, 20])
        day_ok = b[56] == _even_parity_bit(a[14:23])
        month = _bcd(a[23:27], [1, 2, 4, 8]) + _bcd(a[27:28], [10])
        year = _bcd(a[28:32], [1, 2, 4, 8]) + _bcd(a[32:36], [10, 20, 40, 80])
        month_year_ok = b[57] == _even_parity_bit(a[23:36])
        dst = b[58] == "1"

        try:
            local_dt = datetime(2000 + year, month, day, hour, minute)
        except ValueError:
            return None
        utc_offset_h = 1 if dst else 0
        utc_dt = (local_dt - timedelta(hours=utc_offset_h)).replace(tzinfo=UTC)

        host_time = self._host_clock.host_time_at(window[0][0], self.input_rate)
        offset_s = host_time - utc_dt.timestamp()

        quality = sum(confs) / len(confs)
        for ok in (minute_ok, hour_ok, day_ok, month_year_ok):
            if not ok:
                quality *= 0.6
        quality = max(0.0, min(1.0, quality))

        return DecodeEvent(
            ts=time.time(),
            decoder="msf",
            kind="time",
            text=f"UTC {utc_dt:%H:%M:%S} · host {offset_s:+.2f} s",
            fields={
                "utc": utc_dt.isoformat(),
                "offset_s": offset_s,
                "dut1": None,
                "leap": None,
                "station": "MSF",
                "quality": quality,
                "parity_ok": minute_ok and hour_ok and day_ok and month_year_ok,
            },
            **_parity_integrity(
                {
                    "minute": minute_ok,
                    "hour": hour_ok,
                    "day": day_ok,
                    "month/year": month_year_ok,
                }
            ),
        )


# =============================================================================
# JJY (Japan, 40/60 kHz) and WWVB (USA, 60 kHz) -- shared pulse-frame engine
# =============================================================================

_MINUTE_MARKERS = (0, 9, 19, 29, 39, 49, 59)


def _set_msb(bits: list[str], start: int, value: int, n: int) -> None:
    """Writes `value` as n binary digits, most significant first, into
    bits[start:start+n] -- JJY/WWVB list their BCD weights high-to-low
    (unlike WWV/DCF77's low-to-high _set_digit)."""
    for i in range(n):
        bits[start + i] = "1" if value & (1 << (n - 1 - i)) else "0"


def _classify_by_bands(
    width_s: float, bands: tuple[tuple[str, float, float, float], ...]
) -> tuple[str | None, float]:
    for label, nominal, lo, hi in bands:
        if lo <= width_s < hi:
            return label, max(0.4, 1.0 - abs(width_s - nominal) / (nominal * 0.6))
    return None, 0.0


class _PulseFrameDecoder:
    """Once-per-second pulse whose *width* carries 0/1/marker, 60 pulses per
    frame with markers at seconds 0, 9, 19, ... 59. Subclasses set the pulse
    polarity (`_START_IS_HIGH`: the width is the high time -> JJY, the low
    time -> WWVB), the width bands and `_decode_frame`. Like WWV, a frame is
    recognised by all seven marker positions reading "M" in the last 60
    pulses, so a missed pulse just delays lock by up to one minute."""

    input_rate = _FS
    _DECODER_ID = ""
    _START_IS_HIGH = True
    _BANDS: tuple[tuple[str, float, float, float], ...] = ()

    def __init__(self, clock: Callable[[], float] = time.time):
        self._edges = _EnvelopeEdges(self.input_rate)
        self._host_clock = _HostClock(clock)
        self._start: int | None = None
        self._pulses: list[tuple[int, str, float]] = []

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._host_clock.mark_feed(len(samples))
        out: list[DecodeEvent] = []
        for is_high, idx in self._edges.process(samples.astype(np.float64)):
            if is_high == self._START_IS_HIGH:
                self._start = idx
            elif self._start is not None:
                width_s = (idx - self._start) / self.input_rate
                out.extend(self._on_pulse(self._start, width_s))
                self._start = None
        return out

    def flush(self) -> list[DecodeEvent]:
        return []

    def _on_pulse(self, start_idx: int, width_s: float) -> list[DecodeEvent]:
        label, conf = _classify_by_bands(width_s, self._BANDS)
        if label is None:
            return []
        self._pulses.append((start_idx, label, conf))
        if len(self._pulses) > 130:
            self._pulses = self._pulses[-130:]
        if len(self._pulses) < 60:
            return []
        window = self._pulses[-60:]
        if not all(window[p][1] == "M" for p in _MINUTE_MARKERS):
            return []
        self._pulses = []
        decoded = self._decode_frame([w[1] for w in window])
        if decoded is None:
            return []
        utc_dt, extra, integrity = decoded
        host_time = self._host_clock.host_time_at(window[0][0], self.input_rate)
        offset_s = host_time - utc_dt.timestamp()
        quality = max(0.0, min(1.0, sum(w[2] for w in window) / 60))
        if integrity["integrity"] == "failed":
            quality *= 0.6
        return [
            DecodeEvent(
                ts=time.time(),
                decoder=self._DECODER_ID,
                kind="time",
                text=f"UTC {utc_dt:%H:%M:%S} · host {offset_s:+.2f} s",
                fields={
                    "utc": utc_dt.isoformat(),
                    "offset_s": offset_s,
                    "quality": quality,
                    **extra,
                },
                **integrity,
            )
        ]

    def _decode_frame(
        self, bits: list[str]
    ) -> tuple[datetime, dict[str, object], dict[str, str]] | None:
        raise NotImplementedError


# -- JJY ----------------------------------------------------------------------

# JJY keys the carrier at full power for 0.2 s (marker) / 0.5 s (1) / 0.8 s
# (0) at the start of each second, then drops it to ~10 %.
_JJY_BANDS = (
    ("M", 0.2, 0.10, 0.35),
    ("1", 0.5, 0.35, 0.65),
    ("0", 0.8, 0.65, 0.95),
)
_JJY_UTC_OFFSET_H = 9  # the code carries JST


def encode_jjy_minute(dt: datetime) -> list[str]:
    """Reference encoder: the 60 per-second labels ('0'/'1'/'M') for the JST
    minute starting at `dt` (dt.second==0; naive local time).

    Bit table (BCD, most significant weight first; unlisted seconds are 0):
      0,9,19,29,39,49,59: marker
      1-3: minute tens [40,20,10]   5-8: minute units [8,4,2,1]
      12-13: hour tens [20,10]      15-18: hour units [8,4,2,1]
      22-23: day-of-year hundreds [200,100]   25-28: tens [80,40,20,10]
      30-33: day-of-year units [8,4,2,1]
      36: PA1 even parity of the hour bits   37: PA2 even parity of the minute bits
      41-44: year tens [80,40,20,10]   45-48: year units [8,4,2,1]
      50-52: weekday [4,2,1] (Sunday=0)
    Summer-time (38,40) and leap-second (53-54) bits are not used (Japan has
    no DST) and are sent as 0.
    """
    day = dt.timetuple().tm_yday
    bits = ["0"] * 60
    for pos in _MINUTE_MARKERS:
        bits[pos] = "M"
    _set_msb(bits, 1, dt.minute // 10, 3)
    _set_msb(bits, 5, dt.minute % 10, 4)
    _set_msb(bits, 12, dt.hour // 10, 2)
    _set_msb(bits, 15, dt.hour % 10, 4)
    _set_msb(bits, 22, day // 100, 2)
    _set_msb(bits, 25, (day // 10) % 10, 4)
    _set_msb(bits, 30, day % 10, 4)
    bits[36] = _even_parity_bit(bits[12:14] + bits[15:19])
    bits[37] = _even_parity_bit(bits[1:4] + bits[5:9])
    _set_msb(bits, 41, (dt.year % 100) // 10, 4)
    _set_msb(bits, 45, dt.year % 10, 4)
    _set_msb(bits, 50, dt.isoweekday() % 7, 3)
    return bits


class JjyDecoder(_PulseFrameDecoder):
    """JJY 40/60 kHz: carrier-on width 0.2/0.5/0.8 s = marker/1/0; see
    `encode_jjy_minute`. Parity (PA1 hour, PA2 minute) is checked and drives
    the integrity flag and quality."""

    _DECODER_ID = "jjy"
    _START_IS_HIGH = True
    _BANDS = _JJY_BANDS

    def _decode_frame(self, bits):
        minute = _bcd(bits[1:4], [40, 20, 10]) + _bcd(bits[5:9], [8, 4, 2, 1])
        hour = _bcd(bits[12:14], [20, 10]) + _bcd(bits[15:19], [8, 4, 2, 1])
        day = (
            _bcd(bits[22:24], [200, 100])
            + _bcd(bits[25:29], [80, 40, 20, 10])
            + _bcd(bits[30:34], [8, 4, 2, 1])
        )
        year = _bcd(bits[41:45], [80, 40, 20, 10]) + _bcd(bits[45:49], [8, 4, 2, 1])
        hour_ok = _even_parity_bit(bits[12:14] + bits[15:19]) == bits[36]
        minute_ok = _even_parity_bit(bits[1:4] + bits[5:9]) == bits[37]
        if not (0 <= minute <= 59 and 0 <= hour <= 23 and 1 <= day <= 366):
            return None
        try:
            jst = datetime(2000 + year, 1, 1) + timedelta(days=day - 1, hours=hour, minutes=minute)
        except (ValueError, OverflowError):
            return None
        utc_dt = (jst - timedelta(hours=_JJY_UTC_OFFSET_H)).replace(tzinfo=UTC)
        extra = {
            "dut1": None,
            "leap": None,
            "station": "JJY",
            "parity_ok": hour_ok and minute_ok,
        }
        return utc_dt, extra, _parity_integrity({"hour": hour_ok, "minute": minute_ok})


# -- WWVB ---------------------------------------------------------------------

# WWVB drops the carrier ~17 dB at the start of each second for 0.2 s (0),
# 0.5 s (1) or 0.8 s (marker) -- the low time is the symbol, the opposite
# polarity to JJY.
_WWVB_BANDS = (
    ("0", 0.2, 0.10, 0.35),
    ("1", 0.5, 0.35, 0.65),
    ("M", 0.8, 0.65, 0.95),
)


def encode_wwvb_minute(dt: datetime, *, dut1_s: float = 0.0, leap_year: bool | None = None):
    """Reference encoder: the 60 per-second labels ('0'/'1'/'M') for the UTC
    minute starting at `dt` (dt.second==0).

    Bit table (BCD, most significant weight first; unlisted seconds are 0):
      0,9,19,29,39,49,59: marker
      1-3: minute tens [40,20,10]   5-8: minute units [8,4,2,1]
      12-13: hour tens [20,10]      15-18: hour units [8,4,2,1]
      22-23: day-of-year hundreds [200,100]   25-28: tens [80,40,20,10]
      30-33: day-of-year units [8,4,2,1]
      36-38: UT1 sign (positive = 1,0,1; negative = 0,1,0)
      40-43: UT1 magnitude [0.8,0.4,0.2,0.1] s
      45-48: year tens [80,40,20,10]   50-53: year units [8,4,2,1]
      55: leap-year flag
    Real WWVB has no parity bits; the DST (57-58) and leap-second (56) bits
    are not used and are sent as 0.
    """
    day = dt.timetuple().tm_yday
    bits = ["0"] * 60
    for pos in _MINUTE_MARKERS:
        bits[pos] = "M"
    _set_msb(bits, 1, dt.minute // 10, 3)
    _set_msb(bits, 5, dt.minute % 10, 4)
    _set_msb(bits, 12, dt.hour // 10, 2)
    _set_msb(bits, 15, dt.hour % 10, 4)
    _set_msb(bits, 22, day // 100, 2)
    _set_msb(bits, 25, (day // 10) % 10, 4)
    _set_msb(bits, 30, day % 10, 4)
    positive = dut1_s >= 0
    bits[36], bits[37], bits[38] = ("1", "0", "1") if positive else ("0", "1", "0")
    _set_msb(bits, 40, int(round(abs(dut1_s) * 10)), 4)
    _set_msb(bits, 45, (dt.year % 100) // 10, 4)
    _set_msb(bits, 50, dt.year % 10, 4)
    if leap_year is None:
        leap_year = dt.year % 4 == 0 and (dt.year % 100 != 0 or dt.year % 400 == 0)
    bits[55] = "1" if leap_year else "0"
    return bits


class WwvbDecoder(_PulseFrameDecoder):
    """WWVB 60 kHz: carrier-low width 0.2/0.5/0.8 s = 0/1/marker; see
    `encode_wwvb_minute`. No parity on the air, so (like WWV) integrity is the
    marker framing plus a field range check."""

    _DECODER_ID = "wwvb"
    _START_IS_HIGH = False
    _BANDS = _WWVB_BANDS

    def _decode_frame(self, bits):
        minute = _bcd(bits[1:4], [40, 20, 10]) + _bcd(bits[5:9], [8, 4, 2, 1])
        hour = _bcd(bits[12:14], [20, 10]) + _bcd(bits[15:19], [8, 4, 2, 1])
        day = (
            _bcd(bits[22:24], [200, 100])
            + _bcd(bits[25:29], [80, 40, 20, 10])
            + _bcd(bits[30:34], [8, 4, 2, 1])
        )
        year = _bcd(bits[45:49], [80, 40, 20, 10]) + _bcd(bits[50:54], [8, 4, 2, 1])
        sign = 1 if bits[36] == "1" and bits[38] == "1" else -1
        dut1 = sign * _bcd(bits[40:44], [8, 4, 2, 1]) / 10.0
        if not (0 <= minute <= 59 and 0 <= hour <= 23 and 1 <= day <= 366):
            return None
        try:
            utc_dt = datetime(2000 + year, 1, 1, tzinfo=UTC) + timedelta(
                days=day - 1, hours=hour, minutes=minute
            )
        except (ValueError, OverflowError):
            return None
        extra = {"dut1": dut1, "leap": None, "station": "WWVB"}
        integrity = {
            "integrity": "passed",
            "integrity_note": "WWVB marker framing and field range check",
        }
        return utc_dt, extra, integrity


# =============================================================================
# CHU (Canada, 3330 / 7850 / 14670 kHz) -- Bell 103 FSK time bursts
# =============================================================================

_CHU_MARK_HZ = 2225.0
_CHU_SPACE_HZ = 2025.0
_CHU_BAUD = 300.0
_CHU_BYTE_BITS = 11  # start + 8 data (LSB first) + 2 stop


def encode_chu_burst(dt: datetime) -> bytes:
    """Reference encoder: the 10-byte FSK burst CHU sends in second
    `dt.second` (31-39) of the UTC minute.

    Format A (seconds 32-39): bytes 0-4 are BCD nibbles 6, day-of-year (3
    digits), hour, minute, second -- `6d dd hh mm ss` -- and bytes 5-9 repeat
    them exactly. (Format B for second 31: see `encode_chu_b_burst`.)
    """
    day = dt.timetuple().tm_yday
    nib = [
        6,
        day // 100,
        (day // 10) % 10,
        day % 10,
        dt.hour // 10,
        dt.hour % 10,
        dt.minute // 10,
        dt.minute % 10,
        dt.second // 10,
        dt.second % 10,
    ]
    a = bytes((nib[i] << 4) | nib[i + 1] for i in range(0, 10, 2))
    return a + a


def encode_chu_b_burst(year: int, *, dut1_s: float = 0.0, tai_utc: int = 37, dst: int = 0) -> bytes:
    """Format B (second 31): bytes 0-4 are DUT1 (high nibble 0 = positive, 1 =
    negative; low nibble tenths of a second), year (4 BCD digits), TAI-UTC
    (2 BCD digits) and a DST byte; bytes 5-9 are their bitwise complement."""
    d = int(round(abs(dut1_s) * 10))
    y = [int(c) for c in f"{year:04d}"]
    t = [tai_utc // 10, tai_utc % 10]
    a = bytes(
        [((1 if dut1_s < 0 else 0) << 4) | d, (y[0] << 4) | y[1], (y[2] << 4) | y[3],
         (t[0] << 4) | t[1], dst & 0xFF]
    )
    return a + bytes(~x & 0xFF for x in a)


def chu_burst_bits(burst: bytes) -> list[int]:
    """UART bit stream for a burst: per byte 0 (start), 8 data bits LSB first,
    1, 1 (stop). 1 = mark (2225 Hz), 0 = space (2025 Hz)."""
    out: list[int] = []
    for byte in burst:
        out.append(0)
        out.extend((byte >> i) & 1 for i in range(8))
        out.extend((1, 1))
    return out


class ChuDecoder:
    """CHU Bell 103 time bursts (USB audio: mark 2225 Hz, space 2025 Hz,
    300 baud, 11-bit bytes, 10 bytes per second in seconds 31-39).

    Demod: one-bit boxcar tone correlators -> sign of (mark - space energy);
    each mark->space transition is a candidate start bit and the byte is read
    at the bit centres. Ten back-to-back bytes form a burst: format A when the
    two halves are identical (`encode_chu_burst`), format B when the second
    half is the complement of the first; BCD fields must be plausible. One
    event per UTC minute: the first valid burst.

    Limits: the burst carries day-of-year but no year, so the year comes from
    the host clock; `offset_s` assumes the burst starts on the second mark
    (the real fixed delay after the tick is not compensated).
    """

    input_rate = _FS

    def __init__(self, clock: Callable[[], float] = time.time):
        fs = self.input_rate
        self._clock = clock
        self._host_clock = _HostClock(clock)
        self._bit = fs / _CHU_BAUD
        self._win = int(round(self._bit))
        self._tail = np.zeros(self._win - 1)
        self._n = 0  # samples consumed so far
        self._d = np.zeros(0)  # mark-minus-space energy, one value per sample
        self._d_base = 0  # absolute sample index of _d[0]
        self._skip_until = 0  # absolute index: ignore transitions before it
        self._burst: list[int] = []
        self._burst_x = 0
        self._last_x = 0
        self._last_minute: tuple[int, int, int] | None = None
        self._b: dict[str, object] = {}  # latest format-B facts (year, DUT1, TAI-UTC)

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._host_clock.mark_feed(len(samples))
        fs = self.input_rate
        ext = np.concatenate((self._tail, samples.astype(np.float64)))
        t = (self._n - len(self._tail) + np.arange(len(ext))) / fs
        box = np.ones(self._win)
        mark = np.convolve(ext * np.exp(-2j * np.pi * _CHU_MARK_HZ * t), box, "valid")
        space = np.convolve(ext * np.exp(-2j * np.pi * _CHU_SPACE_HZ * t), box, "valid")
        self._tail = ext[-(self._win - 1) :]
        self._n += len(samples)
        self._d = np.concatenate((self._d, np.abs(mark) ** 2 - np.abs(space) ** 2))
        return self._parse()

    def flush(self) -> list[DecodeEvent]:
        return []

    def _parse(self) -> list[DecodeEvent]:
        d, bit = self._d, self._bit
        events: list[DecodeEvent] = []
        positive = d > 0
        falls = np.flatnonzero(positive[:-1] & ~positive[1:]) + 1
        reach = int(round((_CHU_BYTE_BITS - 0.5) * bit))
        keep = max(len(d) - 1, 0)
        for x in falls:
            if self._d_base + x < self._skip_until:
                continue
            if x + reach >= len(d):
                keep = max(int(x) - 1, 0)  # wait for the rest of this byte
                break
            # A boxcar window ending at index m is centred T/2 earlier, so the
            # transition at x sits T/2 behind the true edge and bit k is read
            # at x + (k + 0.5) T.
            bits = [d[x + int(round((k + 0.5) * bit))] > 0 for k in range(_CHU_BYTE_BITS)]
            if bits[0] or not (bits[9] and bits[10]):
                continue
            byte = sum(int(b) << i for i, b in enumerate(bits[1:9]))
            self._skip_until = self._d_base + x + int(10.5 * bit)
            event = self._take_byte(byte, self._d_base + int(x))
            if event is not None:
                events.append(event)
        self._d = d[keep:]
        self._d_base += keep
        return events

    def _take_byte(self, byte: int, x_abs: int) -> DecodeEvent | None:
        contiguous = abs(x_abs - self._last_x - _CHU_BYTE_BITS * self._bit) <= 0.6 * self._bit
        if self._burst and contiguous:
            self._burst.append(byte)
        else:
            self._burst = [byte]
            self._burst_x = x_abs
        self._last_x = x_abs
        if len(self._burst) < 10:
            return None
        burst, self._burst = self._burst, []
        return self._decode_burst(burst)

    def _take_b(self, a: list[int]) -> None:
        nib = [n for byte in a for n in (byte >> 4, byte & 0xF)]
        if nib[0] > 1 or any(n > 9 for n in nib[1:8]):
            return
        year = nib[2] * 1000 + nib[3] * 100 + nib[4] * 10 + nib[5]
        self._b = {
            "year": year,
            "dut1": (-1 if nib[0] else 1) * nib[1] / 10.0,
            "tai_utc": nib[6] * 10 + nib[7],
        }

    def _decode_burst(self, burst: list[int]) -> DecodeEvent | None:
        a, b = burst[:5], burst[5:]
        if all(b[i] == (~a[i] & 0xFF) for i in range(5)):
            self._take_b(a)  # format B carries no time, only context for A
            return None
        if a != b:
            return None
        nib = [n for byte in a for n in (byte >> 4, byte & 0xF)]
        if nib[0] != 6 or any(n > 9 for n in nib[1:]):
            return None
        day = nib[1] * 100 + nib[2] * 10 + nib[3]
        hour, minute, second = nib[4] * 10 + nib[5], nib[6] * 10 + nib[7], nib[8] * 10 + nib[9]
        if not (1 <= day <= 366 and hour <= 23 and minute <= 59 and 31 <= second <= 39):
            return None
        # Year from the host clock, not format B: B's byte layout is a reconstruction
        # (not checked off air), and a wrong year would skew offset_s by whole years.
        year = datetime.fromtimestamp(self._clock(), UTC).year
        key = (year, day, hour * 60 + minute)
        if key == self._last_minute:
            return None
        try:
            utc_dt = datetime(year, 1, 1, tzinfo=UTC) + timedelta(
                days=day - 1, hours=hour, minutes=minute, seconds=second
            )
        except (ValueError, OverflowError):
            return None
        self._last_minute = key
        host_time = self._host_clock.host_time_at(self._burst_x, self.input_rate)
        offset_s = host_time - utc_dt.timestamp()
        return DecodeEvent(
            ts=time.time(),
            decoder="chu",
            kind="time",
            text=f"UTC {utc_dt:%H:%M:%S} · host {offset_s:+.2f} s",
            fields={
                "utc": utc_dt.isoformat(),
                "offset_s": offset_s,
                "dut1": self._b.get("dut1"),
                "leap": None,
                "tai_utc": self._b.get("tai_utc"),
                "station": "CHU",
                "quality": 1.0,
            },
            integrity="passed",
            integrity_note="CHU format A: both 5-byte halves identical",
        )
