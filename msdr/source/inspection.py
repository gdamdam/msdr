"""Bounded review/live signal inspection primitives.

Inspection keeps only the latest contiguous samples for each scope. Its
snapshots include the source clock and discontinuity details so a renderer
can keep time axes honest across seeks, gaps, and live reconnects.
"""

from collections import deque
from dataclasses import dataclass
from typing import Literal

import numpy as np

TriggerMode = Literal["none", "rising", "falling"]


@dataclass(frozen=True)
class ScopeSnapshot:
    stream: str
    sample_rate_hz: float
    sample_index: int
    generation: int
    session_id: str | int | None
    values: list[float]
    min_values: list[float]
    max_values: list[float]
    time_s: list[float]
    trigger_level: float | None
    trigger_mode: TriggerMode
    trigger_sample_index: int | None
    trigger_status: str
    cursor_sample_indices: tuple[int, ...]
    gap_count: int
    last_gap: dict[str, int] | None

    def as_dict(self) -> dict:
        return {
            "stream": self.stream,
            "sample_rate_hz": self.sample_rate_hz,
            "sample_index": self.sample_index,
            "generation": self.generation,
            "session_id": self.session_id,
            "values": self.values,
            "min_values": self.min_values,
            "max_values": self.max_values,
            "time_s": self.time_s,
            "trigger_level": self.trigger_level,
            "trigger_mode": self.trigger_mode,
            "trigger_sample_index": self.trigger_sample_index,
            "trigger_status": self.trigger_status,
            "cursor_sample_indices": list(self.cursor_sample_indices),
            "gap_count": self.gap_count,
            "last_gap": self.last_gap,
        }


class InspectionBuffer:
    """A bounded scalar sample buffer with peak-preserving min/max snapshots."""

    def __init__(self, stream: str, *, max_samples: int = 500_000) -> None:
        if max_samples < 2:
            raise ValueError("max_samples must be at least 2")
        self.stream = stream
        self.max_samples = int(max_samples)
        self._chunks: deque[np.ndarray] = deque()
        self._size = 0
        self._rate = 0.0
        self._first_index = 0
        self._next_index = 0
        self._generation = 0
        self._session_id: str | int | None = None
        self._gap_count = 0
        self._last_gap: dict[str, int] | None = None
        self._trigger_level: float | None = None
        self._trigger_mode: TriggerMode = "none"
        self._cursors: tuple[int, ...] = ()

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0

    def configure(
        self,
        *,
        trigger_level: float | None = None,
        trigger_mode: TriggerMode = "none",
        cursor_sample_indices: tuple[int, ...] | list[int] = (),
    ) -> None:
        if trigger_mode not in ("none", "rising", "falling"):
            raise ValueError("trigger_mode must be none, rising, or falling")
        self._trigger_level = None if trigger_level is None else float(trigger_level)
        self._trigger_mode = trigger_mode
        self._cursors = tuple(int(x) for x in cursor_sample_indices)

    def append(
        self,
        samples: np.ndarray,
        *,
        sample_rate_hz: float,
        sample_index: int,
        generation: int,
        session_id: str | int | None = None,
    ) -> None:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not values.size:
            return
        rate = float(sample_rate_hz)
        if rate <= 0:
            raise ValueError("sample_rate_hz must be positive")
        index = int(sample_index)
        generation = int(generation)
        changed_clock = (
            self._size > 0
            and (
                rate != self._rate
                or generation != self._generation
                or session_id != self._session_id
                or index != self._next_index
            )
        )
        if changed_clock:
            expected = self._next_index
            missing = max(0, index - expected) if index >= expected else 0
            self._gap_count += 1
            self._last_gap = {
                "expected_sample_index": expected,
                "received_sample_index": index,
                "missing_samples": missing,
            }
            self.clear()
        if self._size == 0:
            self._first_index = index
        self._rate = rate
        self._generation = generation
        self._session_id = session_id
        self._chunks.append(values.copy())
        self._size += values.size
        self._next_index = index + values.size
        while self._size > self.max_samples and self._chunks:
            excess = self._size - self.max_samples
            first = self._chunks[0]
            if first.size <= excess:
                self._chunks.popleft()
                self._size -= first.size
                self._first_index += first.size
            else:
                self._chunks[0] = first[excess:]
                self._size -= excess
                self._first_index += excess

    def _trigger_index(self, values: np.ndarray, first_index: int) -> int | None:
        if self._trigger_level is None or self._trigger_mode == "none" or values.size < 2:
            return None
        if self._trigger_mode == "rising":
            hits = np.flatnonzero(
                (values[:-1] < self._trigger_level)
                & (values[1:] >= self._trigger_level)
            )
        else:
            hits = np.flatnonzero(
                (values[:-1] > self._trigger_level)
                & (values[1:] <= self._trigger_level)
            )
        return first_index + int(hits[0]) + 1 if hits.size else None

    def snapshot(self, *, max_points: int = 2_000, window_s: float | None = None) -> ScopeSnapshot:
        if max_points < 2:
            raise ValueError("max_points must be at least 2")
        data = np.concatenate(tuple(self._chunks)) if self._chunks else np.zeros(0, np.float32)
        first_index = self._first_index
        if window_s is not None:
            if not 0.001 <= window_s <= 5.0:
                raise ValueError("window_s must be between 0.001 and 5")
            take = max(2, int(round(window_s * self._rate))) if self._rate else 0
            if take and data.size > take:
                data = data[-take:]
                first_index += self._size - take
        trigger_data = data
        trigger_first = first_index
        trigger_index = self._trigger_index(trigger_data, trigger_first)
        trigger_status = "disabled" if self._trigger_mode == "none" else "waiting_for_trigger"
        if trigger_index is not None:
            trigger_status = "triggered"
            if self._rate:
                # Hold a stable view around the latest crossing with equal
                # pre/post history when enough post-trigger samples exist.
                post = int((window_s or (data.size / self._rate)) * self._rate / 2)
                eligible = []
                if trigger_data.size >= 2:
                    if self._trigger_mode == "rising":
                        hits = np.flatnonzero(
                            (trigger_data[:-1] < self._trigger_level)
                            & (trigger_data[1:] >= self._trigger_level)
                        )
                    else:
                        hits = np.flatnonzero(
                            (trigger_data[:-1] > self._trigger_level)
                            & (trigger_data[1:] <= self._trigger_level)
                        )
                    eligible = [int(h + 1) for h in hits if h + 1 + post <= trigger_data.size]
                if eligible:
                    cross = eligible[-1]
                    duration = window_s or trigger_data.size / self._rate
                    take = min(trigger_data.size, max(2, int(round(duration * self._rate))))
                    start = max(0, min(cross - take // 2, trigger_data.size - take))
                    trigger_data = trigger_data[start : start + take]
                    trigger_first += start
                    trigger_index = self._trigger_index(trigger_data, trigger_first)
                else:
                    trigger_status = "waiting_for_post_trigger"
        data = trigger_data
        first_index = trigger_first
        min_values: list[float] = []
        max_values: list[float] = []
        point_times: list[float] = []
        if data.size > max_points:
            edges = np.linspace(0, data.size, max_points + 1, dtype=np.int64)
            for lo, hi in zip(edges[:-1], edges[1:], strict=True):
                if hi <= lo:
                    continue
                chunk = data[lo:hi]
                min_values.append(float(np.min(chunk)))
                max_values.append(float(np.max(chunk)))
                point_times.append((first_index + (lo + hi - 1) / 2) / self._rate)
            data = (np.asarray(min_values) + np.asarray(max_values)) / 2
        else:
            min_values = [float(x) for x in data]
            max_values = [float(x) for x in data]
            point_times = (
                ((first_index + np.arange(data.size)) / self._rate).astype(float).tolist()
                if self._rate
                else []
            )
        return ScopeSnapshot(
            stream=self.stream,
            sample_rate_hz=self._rate,
            sample_index=first_index,
            generation=self._generation,
            session_id=self._session_id,
            values=[float(x) for x in data],
            min_values=min_values,
            max_values=max_values,
            time_s=point_times,
            trigger_level=self._trigger_level,
            trigger_mode=self._trigger_mode,
            trigger_sample_index=trigger_index,
            trigger_status=trigger_status,
            cursor_sample_indices=self._cursors,
            gap_count=self._gap_count,
            last_gap=dict(self._last_gap) if self._last_gap else None,
        )


def audio_waveform(samples: np.ndarray) -> np.ndarray:
    """Return mono audio as finite float32 samples for a scope."""
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    return np.nan_to_num(values, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)


def iq_envelope(iq: np.ndarray) -> np.ndarray:
    """Return complex IQ magnitude without changing the original samples."""
    values = np.asarray(iq, dtype=np.complex64).reshape(-1)
    return np.abs(values).astype(np.float32)


def audio_spectrum(samples: np.ndarray, sample_rate_hz: float, *, bins: int = 1024) -> dict:
    """Windowed single-channel audio spectrum in dBFS, from the latest samples."""
    if bins < 16 or sample_rate_hz <= 0:
        raise ValueError("bins must be >= 16 and sample_rate_hz must be positive")
    x = np.asarray(samples, dtype=np.float32).reshape(-1)[-bins:]
    if x.size < 2:
        return {"sample_rate_hz": float(sample_rate_hz), "frequency_hz": [], "dbfs": []}
    window = np.hanning(x.size).astype(np.float32)
    scale = float(np.sum(window**2))
    power = np.abs(np.fft.rfft(x * window)) ** 2 / max(scale, 1.0)
    dbfs = 10.0 * np.log10(power + 1e-20)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate_hz)
    return {
        "sample_rate_hz": float(sample_rate_hz),
        "frequency_hz": freqs.astype(float).tolist(),
        "power_dbfs": dbfs.astype(float).tolist(),
        "dbfs": dbfs.astype(float).tolist(),
    }


class ReceiverInspection:
    """Shared inspection state for review playback and a future live source."""

    STREAMS = ("audio", "iq_envelope", "iq_i", "iq_q")

    def __init__(self, *, max_samples: int = 500_000) -> None:
        self.buffers = {
            name: InspectionBuffer(name, max_samples=max_samples) for name in self.STREAMS
        }

    def configure(
        self,
        stream: str,
        *,
        trigger_level: float | None = None,
        trigger_mode: TriggerMode = "none",
        cursor_sample_indices: tuple[int, ...] | list[int] = (),
    ) -> None:
        try:
            buffer = self.buffers[stream]
        except KeyError as exc:
            raise ValueError(f"unknown inspection stream {stream!r}") from exc
        buffer.configure(
            trigger_level=trigger_level,
            trigger_mode=trigger_mode,
            cursor_sample_indices=cursor_sample_indices,
        )

    def append_audio(
        self,
        samples: np.ndarray,
        *,
        sample_rate_hz: float,
        sample_index: int,
        generation: int,
        session_id: str | int | None,
    ) -> None:
        self.buffers["audio"].append(
            audio_waveform(samples), sample_rate_hz=sample_rate_hz,
            sample_index=sample_index, generation=generation, session_id=session_id,
        )

    def append_channel_iq(
        self,
        iq: np.ndarray,
        *,
        sample_rate_hz: float,
        sample_index: int,
        generation: int,
        session_id: str | int | None,
    ) -> None:
        # Same sanitising as audio_waveform: one NaN would otherwise sit in the
        # snapshot values/min/max for the whole window. Copies, so the
        # caller's array is untouched.
        values = np.nan_to_num(
            np.asarray(iq, dtype=np.complex64).reshape(-1), nan=0.0, posinf=1.0, neginf=-1.0
        )
        common = {
            "sample_rate_hz": sample_rate_hz,
            "sample_index": sample_index,
            "generation": generation,
            "session_id": session_id,
        }
        self.buffers["iq_envelope"].append(iq_envelope(values), **common)
        self.buffers["iq_i"].append(values.real, **common)
        self.buffers["iq_q"].append(values.imag, **common)

    def snapshot(
        self,
        stream: str = "all",
        *,
        window_s: float | None = None,
        points: int = 512,
    ) -> dict:
        if stream == "all":
            selected = self.STREAMS
        elif stream in self.buffers:
            selected = (stream,)
        elif stream == "audio_spectrum":
            selected = ()
        else:
            raise ValueError(f"unknown inspection stream {stream!r}")
        snapshots = {
            name: self.buffers[name].snapshot(max_points=points, window_s=window_s).as_dict()
            for name in selected
        }
        result = {"streams": snapshots}
        audio = self.buffers["audio"]
        if stream in ("all", "audio_spectrum"):
            raw = np.concatenate(tuple(audio._chunks)) if audio._chunks else np.zeros(0, np.float32)
            if window_s is not None and not 0.001 <= window_s <= 5.0:
                raise ValueError("window_s must be between 0.001 and 5")
            if window_s is not None and audio._rate:
                raw = raw[-max(2, int(window_s * audio._rate)) :]
            result["audio_spectrum"] = audio_spectrum(raw, audio._rate) if audio._rate else {
                "sample_rate_hz": 0.0, "frequency_hz": [], "power_dbfs": [], "dbfs": []
            }
        return result


@dataclass(frozen=True)
class DriftReading:
    offset_hz: float | None
    smoothed_hz: float | None
    confidence: float
    correction_hz: float
    accepted: bool
    reason: str
    held_last_valid: bool = False

    def as_dict(self) -> dict:
        return {
            "offset_hz": self.offset_hz,
            "smoothed_hz": self.smoothed_hz,
            "confidence": self.confidence,
            "correction_hz": self.correction_hz,
            "accepted": self.accepted,
            "reason": self.reason,
            "held_last_valid": self.held_last_valid,
        }


class CarrierDriftTracker:
    """Bounded carrier offset tracker for explicitly selected unmodulated tones.

    The caller must label the input ``unmodulated_carrier``. Modulated radio
    signals are intentionally unsupported because their modulation can bias
    a carrier estimate. Correction always creates a new array and never edits
    the source IQ buffer.
    """

    SUPPORTED_SIGNALS = ("unmodulated_carrier",)

    def __init__(
        self,
        *,
        max_offset_hz: float = 5_000.0,
        min_rms: float = 1e-3,
        min_coherence: float = 0.985,
        smoothing: float = 0.2,
    ) -> None:
        if max_offset_hz <= 0 or not 0 < smoothing <= 1:
            raise ValueError("max_offset_hz and smoothing must be positive")
        self.max_offset_hz = float(max_offset_hz)
        self.min_rms = float(min_rms)
        self.min_coherence = float(min_coherence)
        self.smoothing = float(smoothing)
        self._smoothed: float | None = None
        self._phase = 0.0
        self._next_index: int | None = None
        self._generation: int | None = None
        self._session_id: str | int | None = None
        self._last = DriftReading(None, None, 0.0, 0.0, False, "not_configured")

    def reset(self) -> None:
        self._smoothed = None
        self._phase = 0.0
        self._next_index = None
        self._last = DriftReading(None, None, 0.0, 0.0, False, "awaiting_samples")

    @property
    def reading(self) -> DriftReading:
        return self._last

    def update(
        self,
        samples: np.ndarray,
        *,
        sample_rate_hz: float,
        sample_index: int,
        generation: int,
        session_id: str | int | None = None,
        signal_kind: str,
    ) -> DriftReading:
        def reject(offset: float | None, confidence: float, reason: str) -> DriftReading:
            correction = self._smoothed or 0.0
            held = self._smoothed is not None
            self._last = DriftReading(
                offset, self._smoothed, confidence, correction, False, reason, held
            )
            return self._last

        if signal_kind not in self.SUPPORTED_SIGNALS:
            return reject(None, 0.0, "unsupported_signal")
        x = np.asarray(samples, dtype=np.complex64).reshape(-1)
        rate = float(sample_rate_hz)
        index = int(sample_index)
        if rate <= 0 or x.size < 16:
            return reject(None, 0.0, "insufficient_samples")
        if (
            self._next_index is not None
            and (
                index != self._next_index
                or generation != self._generation
                or session_id != self._session_id
            )
        ):
            self.reset()
        self._next_index = index + x.size
        self._generation, self._session_id = int(generation), session_id
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
        if rms < self.min_rms:
            return reject(None, 0.0, "below_signal_gate")
        products = x[1:] * np.conj(x[:-1])
        magnitude = np.abs(products)
        active = magnitude > max(float(np.quantile(magnitude, 0.25)) * 0.25, 1e-8)
        products = products[active]
        if products.size < 8:
            return reject(None, 0.0, "below_signal_gate")
        unit = products / np.maximum(np.abs(products), 1e-12)
        coherence = float(abs(np.mean(unit)))
        estimate = float(np.angle(np.sum(unit)) * rate / (2.0 * np.pi))
        confidence = float(
            np.clip((coherence - self.min_coherence) / (1.0 - self.min_coherence), 0.0, 1.0)
        )
        if coherence < self.min_coherence:
            return reject(estimate, confidence, "low_coherence")
        if abs(estimate) > self.max_offset_hz:
            return reject(estimate, confidence, "offset_limit")
        self._smoothed = estimate if self._smoothed is None else (
            self._smoothed + self.smoothing * (estimate - self._smoothed)
        )
        self._last = DriftReading(
            estimate, self._smoothed, confidence, self._smoothed, True, "accepted", False
        )
        return self._last

    def correct(self, samples: np.ndarray, *, sample_rate_hz: float) -> np.ndarray:
        """Apply the smoothed frequency correction to a copy of complex IQ."""
        x = np.asarray(samples, dtype=np.complex64).reshape(-1)
        if not x.size or self._smoothed is None:
            return x.copy()
        step = -2.0 * np.pi * self._smoothed / float(sample_rate_hz)
        phases = self._phase + step * np.arange(x.size, dtype=np.float64)
        self._phase = float((self._phase + step * x.size) % (2.0 * np.pi))
        return (x * np.exp(1j * phases).astype(np.complex64)).astype(np.complex64, copy=False)
