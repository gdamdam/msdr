"""WWV/WWVH, DCF77 and MSF time-code decoders -- round 2.

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
