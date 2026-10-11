"""Radiofax (WEFAX) FM-subcarrier decoder -- audio path, fixed 120 LPM / IOC
576 (S7-S10 round 1).

Signal model: a single audio tone whose instantaneous frequency carries the
information -- 300 Hz start tone (>=3s) -> FM video (1500 Hz black .. 2300 Hz
white) at 120 lines/min -> 450 Hz stop tone. Demodulated the same way for all
three phases: an analytic signal from a windowed-sinc Hilbert FIR gives
instantaneous phase, whose sample-to-sample rotation gives instantaneous
frequency (spec: "Hilbert/quadrature demod -> instantaneous frequency").

Simplifications (plan: "auto-slant not required (fixed rate)"):
 - Line timing is locked once, at the end of the start-tone run, and then
   free-runs at the nominal 120 LPM rate for the whole transmission -- no
   per-line phasing/white-pulse re-sync against drift.
 - Stop tone is detected sample-by-sample (not just at line boundaries), so a
   stop tone that begins mid-line still ends the image at the right sample,
   but the in-progress (partial) line is dropped rather than padded.
"""

import time
import uuid
from pathlib import Path

import numpy as np

from msdr.decoder.backends import DecodeEvent
from msdr.decoder.pydecoders.png import write_gray8

_BLACK_HZ = 1500.0
_WHITE_HZ = 2300.0
_START_HZ = 300.0
_STOP_HZ = 450.0
_TONE_TOL_HZ = 40.0
_START_MIN_S = 3.0
_STOP_MIN_S = 0.3
_LINE_WIDTH_PX = 1809  # ~pi * IOC576, the standard WEFAX line-width-in-pixels
_MAX_LINES = 4000
_WRITE_EVERY = 20
_HILBERT_TAPS = 129


def _hilbert_taps(num_taps: int) -> np.ndarray:
    """Windowed-sinc discrete Hilbert transformer (odd-indexed taps only,
    centre tap 0) -- an FIR approximation of a 90-degree phase shifter."""
    m = (num_taps - 1) // 2
    n = np.arange(-m, m + 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(n % 2 != 0, 2.0 / (np.pi * n), 0.0)
    h = h * np.hamming(num_taps)
    return h.astype(np.float64)


class _StreamFIR:
    """Same-rate FIR filter with state (last taps-1 input samples) carried
    across process() calls, so audio fed in arbitrary chunks filters exactly
    as if it had all arrived in one call."""

    def __init__(self, taps: np.ndarray):
        self._h = taps
        self._state = np.zeros(len(taps) - 1, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=np.float64)
        combined = np.concatenate([self._state, x])
        y = np.convolve(combined, self._h, mode="valid")
        self._state = combined[-(len(self._h) - 1) :]
        return y


class _Delay:
    """Delays a real signal by `n` samples, to align it with a same-length
    FIR filter's output (which has the same n = (taps-1)//2 group delay)."""

    def __init__(self, n: int):
        self._n = n
        self._buf = np.zeros(n, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self._n == 0:
            return x
        combined = np.concatenate([self._buf, x])
        out = combined[: x.size]
        self._buf = combined[x.size :]
        return out


class WefaxDecoder:
    """PyDecoder: 120 LPM / IOC 576 radiofax over an 11,025 Hz mono channel."""

    input_rate = 11025

    def __init__(self, fax_dir: Path, ioc: int = 576, lpm: int = 120):
        self._fax_dir = Path(fax_dir)
        self._fax_dir.mkdir(parents=True, exist_ok=True)
        self.ioc = ioc
        self.lpm = lpm

        taps = _hilbert_taps(_HILBERT_TAPS)
        self._hilbert = _StreamFIR(taps)
        self._delay = _Delay((_HILBERT_TAPS - 1) // 2)
        self._last_z: complex | None = None

        self._state = "idle"  # idle -> tone_confirmed -> imaging -> done
        self._start_run = 0
        self._stop_run = 0
        self._image_path: Path | None = None
        self._rows: list[bytes] = []
        self._line_freq_buf: list[float] = []
        # Bresenham-style line clock: samples/line is fractional (5512.5 at
        # 120 LPM/11,025 Hz), so the *cumulative* target is what's tracked --
        # comparing it to a per-line-reset counter would let lines drift
        # longer and longer (each line's target growing while its own
        # consumed-count restarts from 0).
        self._total_consumed = 0
        self._next_line_boundary = 0.0

    def _samples_per_line(self) -> float:
        return self.input_rate * 60.0 / self.lpm

    def _instantaneous_freq(self, x: np.ndarray) -> np.ndarray:
        fs = self.input_rate
        x = x.astype(np.float64)
        q = self._hilbert.process(x)
        i = self._delay.process(x)
        z = i + 1j * q
        prev = self._last_z if self._last_z is not None else z[0]
        z_full = np.concatenate([[prev], z])
        diffs = z_full[1:] * np.conj(z_full[:-1])
        freq = np.angle(diffs) * fs / (2 * np.pi)
        self._last_z = z[-1]
        return freq

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0 or self._state == "done":
            return []
        freqs = self._instantaneous_freq(samples)
        events: list[DecodeEvent] = []
        for f in freqs:
            if self._state == "done":
                break
            if self._state == "idle":
                if abs(f - _START_HZ) <= _TONE_TOL_HZ:
                    self._start_run += 1
                    if self._start_run >= int(self.input_rate * _START_MIN_S):
                        self._state = "tone_confirmed"
                else:
                    self._start_run = 0
            elif self._state == "tone_confirmed":
                # >=3s of start tone seen -- but video only begins once the
                # tone itself ends (else the first line's buffer starts
                # early, still full of 300 Hz start-tone samples).
                if abs(f - _START_HZ) <= _TONE_TOL_HZ:
                    continue
                events.append(self._begin_image())
                ev = self._imaging_sample(f)
                if ev is not None:
                    events.append(ev)
            elif self._state == "imaging":
                ev = self._imaging_sample(f)
                if ev is not None:
                    events.append(ev)
        return events

    def _begin_image(self) -> DecodeEvent:
        self._state = "imaging"
        self._total_consumed = 0
        self._next_line_boundary = self._samples_per_line()
        started_at = time.time()
        self._image_path = self._fax_dir / f"{uuid.uuid4().hex}.png"
        return DecodeEvent(
            ts=started_at,
            decoder="wefax",
            kind="image",
            text="fax started",
            fields={"lpm": self.lpm, "ioc": self.ioc, "started_at": started_at},
            image_path=str(self._image_path),
            # An analogue image has no frame check to pass or fail.
            integrity="not_applicable",
            integrity_note="analogue fax image: no integrity check exists",
        )

    def _imaging_sample(self, f: float) -> DecodeEvent | None:
        self._line_freq_buf.append(f)
        self._total_consumed += 1

        if abs(f - _STOP_HZ) <= _TONE_TOL_HZ:
            self._stop_run += 1
        else:
            self._stop_run = 0

        if self._stop_run >= int(self.input_rate * _STOP_MIN_S):
            # The in-progress line is a mix of trailing video and stop tone --
            # drop it (like flush()'s partial-line handling) rather than push
            # a contaminated row.
            return self._finish()

        if self._total_consumed < round(self._next_line_boundary):
            return None

        row = self._downsample_row(self._line_freq_buf)
        self._rows.append(row)
        self._line_freq_buf = []
        self._next_line_boundary += self._samples_per_line()

        if len(self._rows) % _WRITE_EVERY == 0:
            self._write_png()
        if len(self._rows) >= _MAX_LINES:
            return self._finish()
        return None

    def _downsample_row(self, freqs: list[float]) -> bytes:
        arr = np.asarray(freqs, dtype=np.float64)
        if arr.size == 0:
            return bytes(_LINE_WIDTH_PX)
        edges = np.linspace(0, arr.size, _LINE_WIDTH_PX + 1)
        row = np.empty(_LINE_WIDTH_PX, dtype=np.uint8)
        for j in range(_LINE_WIDTH_PX):
            lo = int(edges[j])
            hi = min(max(int(edges[j + 1]), lo + 1), arr.size)
            mean_hz = float(arr[lo:hi].mean())
            gray = (mean_hz - _BLACK_HZ) / (_WHITE_HZ - _BLACK_HZ) * 255.0
            row[j] = np.uint8(np.clip(gray, 0, 255))
        return row.tobytes()

    def _write_png(self) -> None:
        if self._image_path is not None and self._rows:
            write_gray8(self._image_path, _LINE_WIDTH_PX, self._rows)

    def _finish(self) -> DecodeEvent:
        self._write_png()
        self._state = "done"
        n = len(self._rows)
        return DecodeEvent(
            ts=time.time(),
            decoder="wefax",
            kind="image",
            text=f"fax {self.lpm} lpm IOC {self.ioc} · {n} lines",
            fields={"lpm": self.lpm, "ioc": self.ioc, "lines": n},
            update_of_path=str(self._image_path) if self._image_path is not None else None,
            integrity="not_applicable",
            integrity_note="analogue fax image: no integrity check exists",
        )

    def flush(self) -> list[DecodeEvent]:
        if self._state == "imaging":
            return [self._finish()]
        return []
