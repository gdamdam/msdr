"""Asynchronous shadow RF classifier worker.

Inference and model loading run on one dedicated thread. The queue is deliberately
small and lossy: RF ML is advisory and must never put pressure on the DSP path.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import copy
import importlib
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np


class RFMLWorker:
    """Run optional classifier inference behind a bounded, drop-oldest queue."""

    def __init__(
        self,
        settings: Any,
        on_result: Callable[[Any, dict[str, Any]], Any],
        classifier_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.on_result = on_result
        self._factory = classifier_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._classifier: Any = None
        self._queue: collections.deque[tuple[np.ndarray, float, Any, int, float]] = (
            collections.deque()
        )
        self._lock = threading.Lock()
        self._epoch = 0
        self._started = False
        self._stopped = False
        self._state = "disabled" if not settings.enabled else "idle"
        self._device = str(getattr(settings, "device", "auto"))
        self._error: str | None = None
        self._device_note: str | None = None
        self._dropped = 0
        self._stale = 0
        self._slow = 0
        self._latency_ms: float | None = None
        self._completed = 0
        self._last_result_at = None
        self._model_id = None
        self._export_error: str | None = None
        self._writer = None

    async def start(self) -> None:
        """Start loading without blocking the event loop; repeated starts are harmless."""
        if self._started or self._stopped:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        if not self.settings.enabled:
            self._state = "disabled"
            return
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rfml"
        )
        self._wake_event = asyncio.Event()
        self._state = "loading"
        self._pump_task = asyncio.create_task(self._run(), name="rfml-worker")

    def submit(self, iq: Any, sample_rate_hz: float, metadata: Any) -> bool:
        """Queue up to three copied IQ windows. Returns False when disabled, stopped, or invalid."""
        if not self.settings.enabled or self._stopped or not self._started:
            return False
        if self._state == "unavailable":
            return False
        try:
            samples = np.asarray(iq)
            if samples.ndim != 1 or samples.size < 4096 or samples.dtype.kind != "c":
                return False
            rate = float(sample_rate_hz)
            if not np.isfinite(rate) or rate <= 0:
                return False
            copied = np.array(samples[:3 * 4096], dtype=np.complex64, copy=True)
            if not np.all(np.isfinite(copied)):
                return False
            metadata_copy = copy.deepcopy(metadata) if isinstance(metadata, dict) else metadata
        except (TypeError, ValueError, OverflowError):
            return False

        limit = max(1, int(self.settings.queue_size))
        with self._lock:
            if self._stopped:
                return False
            while len(self._queue) >= limit:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((copied, rate, metadata_copy, self._epoch, time.monotonic()))
        if self._loop is not None and self._wake_event is not None:
            self._loop.call_soon_threadsafe(self._wake_event.set)
        return True

    def invalidate(self) -> None:
        """Drop pending work and make any currently-running result obsolete."""
        with self._lock:
            self._queue.clear()
            self._epoch += 1
            self._last_result_at = None

    async def stop(self) -> None:
        """Stop callbacks promptly; an already-running native inference may finish in its thread."""
        if self._stopped:
            return
        self._stopped = True
        with self._lock:
            self._queue.clear()
            self._epoch += 1
        task = self._pump_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._executor is not None:
            # The only possibly-live operation is the current serialized inference.
            # wait=False keeps shutdown bounded and no second executor is created.
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._state != "disabled":
            self._state = "stopped"

    def status(self) -> dict[str, Any]:
        with self._lock:
            depth = len(self._queue)
        return {
            "state": self._state,
            "device": self._device,
            "device_note": self._device_note,
            "error": self._error,
            "queue": depth,
            "dropped": self._dropped,
            "stale": self._stale,
            "slow": self._slow,
            "latency_ms": self._latency_ms,
            "completed": self._completed,
            "last_result_age_s": (time.monotonic() - self._last_result_at
                                  if self._last_result_at is not None else None),
            "model_id": self._model_id,
            "export_error": self._export_error,
            "examples_saved": self._writer.saved if self._writer else 0,
        }

    async def _run(self) -> None:
        assert self._executor is not None
        try:
            classifier = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._load_classifier
            )
            if self._stopped:
                return
            self._classifier = classifier
            from msdr.rfml.corpus import ExampleWriter, comparison

            self._writer = ExampleWriter(self.settings)
            self._device = str(getattr(classifier, "backend", self._device))
            self._state = "ready"
            while not self._stopped:
                assert self._wake_event is not None
                self._wake_event.clear()
                item = self._pop()
                if item is None:
                    await self._wake_event.wait()
                    continue
                iq, rate, metadata, epoch, queued_at = item
                max_age = float(getattr(self.settings, "max_job_age_s", 0) or 0)
                if max_age > 0 and time.monotonic() - queued_at > max_age:
                    self._dropped += 1
                    continue
                began = time.monotonic()
                self._state = "analyzing"
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        self._executor, classifier.predict, iq, rate
                    )
                except Exception as exc:
                    self._error = f"inference failed: {exc}"
                    self._state = "error"
                    continue
                self._state = "ready"
                inference_s = time.monotonic() - began
                self._latency_ms = inference_s * 1000
                self._device = str(getattr(classifier, "backend", self._device))
                # Inference itself can be slow on a loaded CPU; that no longer
                # discards the result (the state already read "ready" while it
                # ran) -- it's just recorded (see detector_runtime.py).
                if max_age > 0 and inference_s > max_age:
                    self._slow += 1
                with self._lock:
                    stale = self._stopped or epoch != self._epoch
                    if stale:
                        self._stale += 1
                if stale:
                    continue
                if isinstance(result, dict):
                    result = dict(result)
                    result.setdefault("backend", self._device)
                    result["inference_s"] = inference_s
                    result["comparison"] = comparison(
                        metadata.get("dsp", {}) if isinstance(metadata, dict) else {}, result
                    )
                if self._writer.directory is not None:
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            self._executor, self._writer.save, iq, rate, metadata, result
                        )
                        self._export_error = None
                    except Exception as exc:
                        self._export_error = f"example export failed: {exc}"
                    # Export may yield across a retune/stop. Never apply that old result.
                    if self._stopped or epoch != self._epoch:
                        self._stale += 1
                        continue
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
            self._error = f"classifier unavailable: {exc}"
            self._state = "unavailable"

    def _pop(self) -> tuple[np.ndarray, float, Any, int, float] | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def _load_classifier(self) -> Any:
        factory = self._factory
        if factory is None:
            # Keep optional backend imports out of disabled operation and module import.
            module = importlib.import_module("msdr.rfml.classifier")
            factory = module.RFMLClassifier
        from msdr.rfml.live_device import live_settings

        settings, self._device_note = live_settings(self.settings)
        classifier = factory(settings)
        classifier.load()
        return classifier
