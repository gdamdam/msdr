"""`SensingAccumulator`: presence, crossing and breathing cues from the
received signal strength of frames sent by ONE configured transmitter
(design decision 3). Pure numpy, no asyncio.

Privacy: `add` drops every frame whose transmitter is not `source` before
anything is stored or counted -- other addresses leave no trace, not even a
count. The caller feeds it `(ts, transmitter, power_code)` per frame; all
queries take an explicit `now` so the class never reads a clock.
"""

from __future__ import annotations

import statistics
from collections import deque

import numpy as np

#: Per-second rows kept: 1 h, the history the "quietest 60 s" baseline spans.
ROWS_KEPT = 3600
#: Raw samples are pruned by age (`window_s`); this bounds memory at a
#: pathological frame rate (200 frames/s x 30 s is 6000).
SAMPLES_MAX = 50_000

#: Completed seconds needed before a presence baseline exists.
BASELINE_MIN_ROWS = 10
#: Baseline = quietest rolling median of per-second std over this many seconds.
BASELINE_SPAN_S = 60
#: Floor on the baseline std (power codes are integers, so a dead-still link
#: can read ~0) so the ratio cannot explode.
BASELINE_FLOOR = 0.3
#: Presence looks at the std of the last this many seconds of raw samples.
PRESENCE_WINDOW_S = 2.0
#: Motion when recent std exceeds the baseline by this factor.
PRESENCE_RATIO = 2.0
#: Fewer raw samples than this in the presence window say nothing.
PRESENCE_MIN_SAMPLES = 5

#: A crossing is a dip in the short-window mean deeper than this many codes
#: below the reference (a body between transmitter and receiver).
CROSSING_DEPTH = 6.0
#: Short-window length for the dip test; long enough to average a few frames.
CROSSING_SHORT_S = 0.4
#: Fewer samples than this in the short window are not evaluated.
CROSSING_MIN_SAMPLES = 3
#: The reference is the median of this many preceding per-second means.
CROSSING_REF_ROWS = 10
#: Reference needs at least this many rows.
CROSSING_REF_MIN_ROWS = 5
#: A dip counts only when it lasts between these bounds (seconds).
CROSSING_MIN_S = 0.3
CROSSING_MAX_S = 3.0
#: `active` stays true this long after a counted dip ends.
CROSSING_HOLD_S = 2.0
#: A dip with no below-threshold sample for this long is closed by a query.
CROSSING_CLOSE_S = 1.0

#: Breathing needs a dense source; below this many frames/s it returns None.
BREATH_MIN_FPS = 30.0
#: Resampling rate for the spectrum (Hz) and the breathing band (Hz).
BREATH_FS = 10.0
BREATH_BAND = (0.1, 0.7)
#: Peak over median in-band magnitude below this is "no clear rhythm".
BREATH_MIN_QUALITY = 3.0
#: Zero-pad to at least this many points so the peak resolves finer than 1 bpm.
BREATH_MIN_FFT = 4096
#: Need at least this many seconds of samples to say anything.
BREATH_MIN_SPAN_S = 15.0
# Longest hole in the samples breathing accepts. Interpolating across a helper stall would
# draw a straight ramp that can pass as a slow rhythm.
BREATH_MAX_GAP_S = 2.0


class SensingAccumulator:
    def __init__(self, source: str, window_s: float = 30.0) -> None:
        self.source = source.lower()
        self.window_s = float(window_s)
        self.crossings = 0  # dips counted so far; each is counted once
        self._samples: deque[tuple[float, int]] = deque(maxlen=SAMPLES_MAX)
        self._rows: deque[tuple[int, int, float, float]] = deque(maxlen=ROWS_KEPT)
        self._first_ts: float | None = None
        # Running sums of the second still being filled.
        self._sec: int | None = None
        self._n = 0
        self._sum = 0.0
        self._sumsq = 0.0
        self._baseline: float | None = None
        self._ref: float | None = None
        # Crossing state.
        self._dip_start: float | None = None
        self._dip_last: float | None = None
        self._dip_depth = 0.0
        self._last_ts: float | None = None
        self._last_end: float | None = None
        self._last_depth: float | None = None

    def add(self, ts: float, transmitter: str | None, power_code: int | None) -> None:
        if power_code is None or transmitter is None or transmitter.lower() != self.source:
            return
        if self._samples and ts < self._samples[-1][0]:
            return      # the host clock stepped back: np.interp needs increasing times
        if self._first_ts is None:
            self._first_ts = ts
        sec = int(ts)
        if self._sec is None:
            self._sec = sec
        elif sec > self._sec:
            self._finish_second()
            self._sec = sec
        self._n += 1
        self._sum += power_code
        self._sumsq += power_code * power_code
        self._samples.append((ts, power_code))
        while self._samples and self._samples[0][0] < ts - self.window_s:
            self._samples.popleft()
        self._scan_crossing(ts)

    def _finish_second(self) -> None:
        n = self._n
        mean = self._sum / n
        # Clamp: float cancellation can leave a tiny negative variance.
        std = max(self._sumsq / n - mean * mean, 0.0) ** 0.5
        self._rows.append((self._sec, n, mean, std))
        self._n, self._sum, self._sumsq = 0, 0.0, 0.0
        if len(self._rows) >= BASELINE_MIN_ROWS:
            med = statistics.median(r[3] for r in list(self._rows)[-BASELINE_SPAN_S:])
            if self._baseline is None or med < self._baseline:
                self._baseline = med
        if len(self._rows) >= CROSSING_REF_MIN_ROWS:
            self._ref = statistics.median(r[2] for r in list(self._rows)[-CROSSING_REF_ROWS:])

    def _scan_crossing(self, ts: float) -> None:
        if self._ref is None:
            return
        recent = []
        for t, c in reversed(self._samples):
            if t <= ts - CROSSING_SHORT_S:
                break
            recent.append(c)
        if len(recent) < CROSSING_MIN_SAMPLES:
            return
        depth = self._ref - sum(recent) / len(recent)
        if depth > CROSSING_DEPTH:
            if self._dip_start is None:
                self._dip_start, self._dip_depth = ts, 0.0
            self._dip_last = ts
            self._dip_depth = max(self._dip_depth, depth)
        elif self._dip_start is not None:
            self._finish_dip()

    def _finish_dip(self) -> None:
        start, last = self._dip_start, self._dip_last
        if start is None or last is None:
            return
        if CROSSING_MIN_S <= last - start <= CROSSING_MAX_S:
            self.crossings += 1
            # Midpoint: the trailing window lags both edges, so it is inside the dip.
            self._last_ts = (start + last) / 2
            self._last_end = last
            self._last_depth = self._dip_depth
        self._dip_start = self._dip_last = None

    def rows(self) -> list[tuple[int, int, float, float]]:
        return list(self._rows)

    def _recent(self, now: float, span: float) -> list[tuple[float, int]]:
        return [s for s in self._samples if now - span < s[0] <= now]

    def presence(self, now: float) -> dict:
        base = None if self._baseline is None else max(self._baseline, BASELINE_FLOOR)
        recent = self._recent(now, PRESENCE_WINDOW_S)
        if base is None or len(recent) < PRESENCE_MIN_SAMPLES:
            return {"motion": False, "ratio": None, "baseline": base}
        ratio = float(np.std([c for _, c in recent])) / base
        return {"motion": bool(ratio > PRESENCE_RATIO), "ratio": ratio, "baseline": base}

    def crossing(self, now: float) -> dict:
        if self._dip_start is not None and now - (self._dip_last or now) > CROSSING_CLOSE_S:
            self._finish_dip()
        ongoing = (
            self._dip_start is not None
            and self._dip_last is not None
            and self._dip_last - self._dip_start >= CROSSING_MIN_S
        )
        held = self._last_end is not None and now - self._last_end < CROSSING_HOLD_S
        return {
            "active": bool(ongoing or held),
            "last_ts": self._last_ts,
            "depth": self._last_depth,
        }

    def fps(self, now: float) -> float:
        if self._first_ts is None:
            return 0.0
        n = len(self._recent(now, self.window_s))
        span = min(self.window_s, max(now - self._first_ts, 1.0))
        return n / span

    def breathing(self, now: float) -> dict | None:
        fps = self.fps(now)
        if fps < BREATH_MIN_FPS:
            return None
        s = self._recent(now, self.window_s)
        if len(s) < 2 or s[-1][0] - s[0][0] < BREATH_MIN_SPAN_S:
            return None
        t = np.array([x[0] for x in s])
        v = np.array([x[1] for x in s], dtype=float)
        if np.diff(t).max() > BREATH_MAX_GAP_S:
            return None
        grid = np.arange(t[0], t[-1], 1.0 / BREATH_FS)
        y = np.interp(grid, t, v)
        x = grid - grid[0]
        y = y - np.polyval(np.polyfit(x, y, 1), x)  # detrend
        y = y * np.hanning(len(y))
        nfft = max(BREATH_MIN_FFT, len(y))
        mag = np.abs(np.fft.rfft(y, nfft))
        freqs = np.fft.rfftfreq(nfft, 1.0 / BREATH_FS)
        band = np.flatnonzero((freqs >= BREATH_BAND[0]) & (freqs <= BREATH_BAND[1]))
        if band.size < 3:
            return None
        bm = mag[band]
        med = float(np.median(bm))
        if med <= 0:
            return None
        k = int(band[np.argmax(bm)])
        quality = float(mag[k] / med)
        if quality < BREATH_MIN_QUALITY:
            return None
        delta = 0.0
        if 0 < k < len(mag) - 1:
            a, b, c = mag[k - 1], mag[k], mag[k + 1]
            denom = a - 2 * b + c
            if denom != 0:
                delta = 0.5 * (a - c) / denom
        hz = freqs[k] + delta * (freqs[1] - freqs[0])
        return {"bpm": float(hz * 60), "quality": quality, "fps": fps}

    def snapshot(self, now: float) -> dict:
        breathing = self.breathing(now)
        fps = self.fps(now)
        if breathing is not None:
            status = "ok"
        elif fps < BREATH_MIN_FPS:
            status = "needs dense source"
        else:
            status = "no clear rhythm"
        row = None
        if self._rows:
            sec, n, mean, std = self._rows[-1]
            row = {"second": sec, "n": n, "mean": mean, "std": std}
        return {
            "source": self.source,
            "fps": fps,
            "row": row,
            "presence": self.presence(now),
            "crossing": self.crossing(now),
            "breathing": breathing,
            "breathing_status": status,
        }
