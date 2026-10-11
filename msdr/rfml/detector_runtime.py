"""Optional, lossy shadow worker for wideband RF detections."""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import copy
import dataclasses
import functools
import importlib
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np


def _plain(value: Any) -> Any:
    """Convert detector dataclasses and NumPy scalars to JSON-friendly values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


class RFMLDetectorWorker:
    """Run the experimental detector outside the capture and event-loop threads."""

    def __init__(
        self,
        settings: Any,
        on_result: Callable[[dict[str, Any], dict[str, Any]], Any],
        detector_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.on_result = on_result
        self._factory = detector_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._queue: collections.deque[
            tuple[np.ndarray, float, dict[str, Any], int, float]
        ] = collections.deque()
        self._lock = threading.Lock()
        self._epoch = 0
        self._started = False
        self._stopped = False
        self._last_submit = float("-inf")
        self._state = "disabled" if not settings.enabled else "idle"
        self._device = str(getattr(settings, "device", "auto"))
        self._error: str | None = None
        self._device_note: str | None = None
        self._dropped = 0
        self._stale = 0
        self._latency_ms: float | None = None
        self._completed = 0
        self._inferred_samples = 0
        self._processing_seconds = 0.0
        self._processing_started_at: float | None = None
        self._slow = 0
        self._last_result_at = None
        self._model_id = None

    def can_accept(self) -> bool:
        """Cheap admission check for callers assembling an IQ window."""
        if not self.settings.enabled or not self._started or self._stopped:
            return False
        if self._state == "unavailable":
            return False
        interval = max(0.0, float(getattr(self.settings, "interval_s", 0) or 0))
        with self._lock:
            return not self._stopped and time.monotonic() - self._last_submit >= interval

    async def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        if not self.settings.enabled:
            return
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rfml-detector"
        )
        self._wake = asyncio.Event()
        self._state = "loading"
        self._task = asyncio.create_task(self._run(), name="rfml-detector-worker")

    def submit(self, iq: Any, sample_rate_hz: float, metadata: dict[str, Any]) -> bool:
        if not self.can_accept():
            return False
        try:
            duration_ms = getattr(self.settings, "window_duration_ms", None)
            if duration_ms is not None:
                window = max(1, int(round(float(sample_rate_hz) * float(duration_ms) / 1000.0)))
            else:
                # Compatibility for small callers/tests that still provide a
                # legacy settings object. Real Settings instances migrate the
                # old field to window_duration_ms.
                default_window = round(float(sample_rate_hz) * 0.1)
                window = int(getattr(self.settings, "window_samples", default_window))
            if window < 2:
                return False
            samples = np.asarray(iq)
            if samples.ndim != 1 or samples.dtype.kind != "c" or samples.size < window:
                return False
            rate = float(sample_rate_hz)
            if not np.isfinite(rate) or rate <= 0 or not isinstance(metadata, dict):
                return False
            center = float(metadata["center_frequency_hz"])
            if not np.isfinite(center):
                return False
            copied = np.array(samples[:window], dtype=np.complex64, copy=True)
            if not np.isfinite(copied).all():
                return False
            metadata_copy = copy.deepcopy(metadata)
        except (TypeError, ValueError, OverflowError, KeyError):
            return False

        now = time.monotonic()
        interval = max(0.0, float(getattr(self.settings, "interval_s", 0) or 0))
        limit = max(1, int(getattr(self.settings, "queue_size", 1)))
        with self._lock:
            if self._stopped or now - self._last_submit < interval:
                return False
            self._last_submit = now
            while len(self._queue) >= limit:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((copied, rate, metadata_copy, self._epoch, now))
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)
        return True

    def invalidate(self) -> None:
        with self._lock:
            self._queue.clear()
            self._epoch += 1
            self._inferred_samples = 0
            self._processing_seconds = 0.0
            self._processing_started_at = None
            self._last_result_at = None
            self._last_submit = float("-inf")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.invalidate()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._state != "disabled":
            self._state = "stopped"

    def status(self) -> dict[str, Any]:
        with self._lock:
            depth = len(self._queue)
            dropped = self._dropped
            stale = self._stale
            inferred_samples = self._inferred_samples
            processing_seconds = self._processing_seconds
            processing_started_at = self._processing_started_at
        observed_s = (
            time.monotonic() - processing_started_at
            if processing_started_at is not None
            else 0.0
        )
        return {
            "state": self._state,
            "device": self._device,
            "device_note": self._device_note,
            "error": self._error,
            "queue": depth,
            "dropped": dropped,
            "stale": stale,
            "slow": self._slow,
            "latency_ms": self._latency_ms,
            "completed": self._completed,
            "inferred_samples": inferred_samples,
            "processing_seconds": processing_seconds,
            "processing_duty": (
                min(1.0, processing_seconds / observed_s) if observed_s > 0 else None
            ),
            "processing_duty_basis": "inference wall time / elapsed wall time; not CPU utilization",
            "last_result_age_s": (time.monotonic() - self._last_result_at
                                  if self._last_result_at is not None else None),
            "model_id": self._model_id,
        }

    def _load(self) -> Any:
        factory = self._factory
        if factory is None:
            module = importlib.import_module("msdr.rfml.detector")

            def factory(settings: Any) -> Any:
                return module.RFMLDetector(
                    settings.model_path,
                    device=settings.device,
                    score_threshold=settings.score_threshold,
                    target_bin_hz=getattr(settings, "target_bin_hz", 1500),
                    max_tiles=getattr(settings, "max_tiles", 4),
                )
        from msdr.rfml.live_device import live_settings

        settings, self._device_note = live_settings(self.settings)
        detector = factory(settings)
        detector.load()
        return detector

    def _pop(self) -> tuple[np.ndarray, float, dict[str, Any], int, float] | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    async def _run(self) -> None:
        assert self._executor is not None
        try:
            loop = asyncio.get_running_loop()
            detector = await loop.run_in_executor(self._executor, self._load)
            if self._stopped:
                return
            self._device = str(getattr(detector, "backend", self._device))
            self._state = "ready"
            while not self._stopped:
                assert self._wake is not None
                self._wake.clear()
                item = self._pop()
                if item is None:
                    await self._wake.wait()
                    continue
                iq, rate, metadata, epoch, queued_at = item
                max_age = float(getattr(self.settings, "max_job_age_s", 0) or 0)
                capture_at = metadata.get("capture_monotonic", queued_at)
                try:
                    capture_at = float(capture_at)
                    if not np.isfinite(capture_at):
                        capture_at = queued_at
                except (TypeError, ValueError):
                    capture_at = queued_at
                if max_age > 0 and time.monotonic() - capture_at > max_age:
                    self._dropped += 1
                    continue
                with self._lock:
                    obsolete = epoch != self._epoch
                if obsolete:
                    self._stale += 1
                    continue
                began = time.monotonic()
                with self._lock:
                    if self._processing_started_at is None:
                        self._processing_started_at = began
                self._state = "analyzing"
                try:
                    kwargs = {
                        "sample_rate_hz": rate,
                        "center_frequency_hz": float(metadata["center_frequency_hz"]),
                    }
                    if "focus_frequencies_hz" in metadata:
                        kwargs["focus_frequencies_hz"] = metadata["focus_frequencies_hz"]
                    result = await loop.run_in_executor(
                        self._executor,
                        functools.partial(
                            detector.predict,
                            iq,
                            **kwargs,
                        ),
                    )
                    result = _plain(result)
                    if not isinstance(result, dict):
                        raise TypeError("detector returned a non-dict result")
                except Exception as exc:
                    inference_s = time.monotonic() - began
                    with self._lock:
                        if epoch == self._epoch:
                            self._processing_seconds += inference_s
                    self._error = f"inference failed: {exc}"
                    self._state = "error"
                    continue
                self._state = "ready"
                inference_s = time.monotonic() - began
                self._latency_ms = inference_s * 1000
                with self._lock:
                    if epoch == self._epoch:
                        self._processing_seconds += inference_s
                self._device = str(getattr(detector, "backend", self._device))
                with self._lock:
                    obsolete = self._stopped or epoch != self._epoch
                    if obsolete:
                        self._stale += 1
                    else:
                        self._inferred_samples += len(iq)
                if obsolete:
                    continue
                # Inference itself can be slow on a loaded CPU; that no longer
                # discards the result (the state already read "ready" while it
                # ran) -- it's just recorded, so a slow model degrades latency
                # rather than throughput.
                if max_age > 0 and inference_s > max_age:
                    self._slow += 1
                result["shadow"] = True
                result.setdefault("backend", self._device)
                result["inference_s"] = inference_s
                self._state = "ready"
                self._error = None
                self._completed += 1
                self._last_result_at = time.monotonic()
                self._model_id = result.get("model_id") if isinstance(result, dict) else None
                try:
                    callback_result = self.on_result(metadata, result)
                    if asyncio.iscoroutine(callback_result):
                        await callback_result
                except Exception as exc:
                    self._error = f"result callback failed: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"detector unavailable: {exc}"
            self._state = "unavailable"
