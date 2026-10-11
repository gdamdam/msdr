"""Bounded, listening-only audio effects for the native receiver.

All effects keep state between blocks and return exactly one sample per input
sample. The noise reducer has a fixed 512-sample delay (10.7 ms at 48 kHz).
"""

import math

import numpy as np


class _Notch:
    def __init__(self, frequency_hz: float, width_hz: float, fs: int) -> None:
        w = 2 * math.pi * frequency_hz / fs
        alpha = math.sin(w) / (2 * frequency_hz / width_hz)
        a0 = 1 + alpha
        self.b0 = 1 / a0
        self.b1 = -2 * math.cos(w) / a0
        self.b2 = self.b0
        self.a1 = self.b1
        self.a2 = (1 - alpha) / a0
        self.z1 = self.z2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty(x.size, dtype=np.float32)
        z1, z2 = self.z1, self.z2
        for i, sample in enumerate(x):
            y = self.b0 * float(sample) + z1
            z1 = self.b1 * float(sample) - self.a1 * y + z2
            z2 = self.b2 * float(sample) - self.a2 * y
            out[i] = y
        self.z1, self.z2 = z1, z2
        return out


class _SpectralReducer:
    """Half-overlap Wiener-like gate with a per-frame robust noise estimate."""

    SIZE = 512
    HOP = 256

    def __init__(self) -> None:
        self._window = np.sqrt(np.hanning(self.SIZE + 1)[:-1]).astype(np.float32)
        self.reset()

    def reset(self) -> None:
        self._input = np.zeros(0, dtype=np.float32)
        self._overlap = np.zeros(self.HOP, dtype=np.float32)
        # One full frame of causal delay means even an odd-sized caller block
        # always has enough ready samples; output does not depend on framing.
        self._ready = np.zeros(self.SIZE, dtype=np.float32)
        self._gain = np.ones(self.SIZE // 2 + 1, dtype=np.float32)
        self._noise = 0.0

    def process(self, x: np.ndarray, strength: float) -> np.ndarray:
        n = x.size
        self._input = np.concatenate((self._input, x))
        while self._input.size >= self.SIZE:
            frame = self._input[: self.SIZE] * self._window
            spectrum = np.fft.rfft(frame)
            power = np.abs(spectrum) ** 2
            # A median over bins ignores narrow speech/tone peaks. The slow
            # estimate prevents one quiet frame from pumping the whole band.
            observed = float(np.median(power[3:]))
            self._noise = observed if self._noise == 0 else 0.98 * self._noise + 0.02 * observed
            target = np.sqrt(np.maximum(0.25, 1 - strength * self._noise / (power + 1e-12)))
            self._gain = 0.75 * self._gain + 0.25 * target
            restored = np.fft.irfft(spectrum * self._gain, n=self.SIZE).real * self._window
            ready = restored[: self.HOP] + self._overlap
            self._overlap = restored[self.HOP :].astype(np.float32)
            self._ready = np.concatenate((self._ready, ready.astype(np.float32)))
            self._input = self._input[self.HOP :]
        take = min(n, self._ready.size)
        out = np.zeros(n, dtype=np.float32)
        out[:take] = self._ready[:take]
        self._ready = self._ready[take:]
        return out


class NativeAudioEffects:
    """Audio-stage impulse blanker, manual notch and conservative noise reducer.

    The blanker sits after demodulation and before the notch/reducer. It is
    intentionally listening-only; decoder taps branch before this class.
    """

    def __init__(self, fs: int = 48_000) -> None:
        self.fs = fs
        self._notch: _Notch | None = None
        self._notch_key: tuple[float, float] | None = None
        self._old_notch: _Notch | None = None
        self._notch_fade = 0
        self._nr = _SpectralReducer()
        self._nr_mix = 0.0
        self._last = 0.0
        self.configure({})

    def configure(self, values: dict) -> None:
        self.blanker_enabled = bool(values.get("blanker_enabled", False))
        self.blanker_threshold = float(values.get("blanker_threshold", 8.0))
        self.nr_enabled = bool(values.get("nr_enabled", False))
        self.nr_strength = float(values.get("nr_strength", 0.5))
        enabled = bool(values.get("notch_enabled", False))
        freq = float(values.get("notch_frequency_hz", 1000))
        width = float(values.get("notch_width_hz", 100))
        key = (freq, width) if enabled else None
        if key != getattr(self, "_notch_key", None):
            self._old_notch = self._notch
            self._notch = _Notch(freq, width, self.fs) if enabled else None
            self._notch_key = key
            self._notch_fade = min(480, self.fs // 100)
        if not self.nr_enabled and self._nr_mix == 0:
            self._nr.reset()

    def reset(self) -> None:
        self._nr.reset()
        self._nr_mix = 0.0
        self._last = 0.0
        self._old_notch = None
        self._notch_fade = 0
        if self._notch_key is not None:
            self._notch = _Notch(*self._notch_key, self.fs)

    def _blank(self, x: np.ndarray) -> np.ndarray:
        if not self.blanker_enabled or x.size < 3:
            return x
        baseline = float(np.median(np.abs(x))) / 0.6745
        limit = self.blanker_threshold * max(baseline, 0.005)
        bad = np.abs(x) > limit
        if bad.mean() > 0.01 or not bad.any():
            self._last = float(x[-1])
            return x
        positions = np.arange(x.size)
        good = positions[~bad]
        y = x.copy()
        y[bad] = np.interp(positions[bad], good, x[good])
        if bad[0]:
            y[0] = self._last
        self._last = float(y[-1])
        return y

    def process(self, audio: np.ndarray) -> np.ndarray:
        x = np.nan_to_num(np.asarray(audio, dtype=np.float32), nan=0, posinf=1, neginf=-1)
        if x.size == 0:
            return x
        x = self._blank(x)
        if self._notch_fade:
            before = self._old_notch.process(x) if self._old_notch is not None else x
            after = self._notch.process(x) if self._notch is not None else x
            count = min(x.size, self._notch_fade)
            mix = (480 - self._notch_fade + np.arange(1, count + 1)) / 480
            y = after.copy()
            y[:count] = before[:count] * (1 - mix) + after[:count] * mix
            x = y
            self._notch_fade -= count
            if not self._notch_fade:
                self._old_notch = None
        elif self._notch is not None:
            x = self._notch.process(x)
        target = 1.0 if self.nr_enabled else 0.0
        if target or self._nr_mix:
            reduced = self._nr.process(x, self.nr_strength)
            ramp = np.clip(
                self._nr_mix + np.sign(target - self._nr_mix) * np.arange(1, x.size + 1) / 480,
                0,
                1,
            ).astype(np.float32)
            x = x * (1 - ramp) + reduced * ramp
            self._nr_mix = float(ramp[-1])
            if not target and self._nr_mix == 0:
                self._nr.reset()
        return np.nan_to_num(x, nan=0, posinf=1, neginf=-1).astype(np.float32)
