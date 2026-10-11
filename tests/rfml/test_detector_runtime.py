"""Dependency-free behavior tests for the optional detector worker."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml.detector_runtime import RFMLDetectorWorker
from msdr.rfml.fusion import Candidate


@dataclass
class Settings:
    enabled: bool = True
    model_path: str = "unused.pt"
    device: str = "cpu"
    score_threshold: float = 0.25
    queue_size: int = 2
    max_job_age_s: float = 0
    window_samples: int = 16
    interval_s: float = 0


class FakeDetector:
    backend = "fake"

    def __init__(self, predict=None):
        self.loads = 0
        self.calls = []
        self.predict_fn = predict

    def load(self):
        self.loads += 1

    def predict(self, iq, *, sample_rate_hz, center_frequency_hz):
        self.calls.append((iq, sample_rate_hz, center_frequency_hz))
        if self.predict_fn:
            return self.predict_fn(iq, sample_rate_hz, center_frequency_hz)
        return {"candidates": []}


async def wait_for(predicate, timeout=1):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.005)


def block(value=1, size=16):
    return np.full(size, value, dtype=np.complex64)


def meta(name="one"):
    return {"center_frequency_hz": 2_000_000, "source": {"name": name}}


@pytest.mark.asyncio
async def test_load_once_copy_window_and_serialize_candidates():
    detector = FakeDetector(
        lambda *_: {
            "candidates": [
                Candidate("ml", 1_000_000, 1_100_000, 0, 0.1, 0.9, {"label": "signal"})
            ]
        }
    )
    received = []
    worker = RFMLDetectorWorker(
        Settings(), lambda m, r: received.append((m, r)), lambda _: detector
    )
    await worker.start()
    await worker.start()
    iq = block(size=32)
    metadata = meta()
    assert worker.submit(iq, 1_000_000, metadata)
    iq[:] = 9
    metadata["source"]["name"] = "changed"
    await wait_for(lambda: len(received) == 1)
    assert detector.loads == 1
    assert len(detector.calls[0][0]) == 16
    assert np.all(detector.calls[0][0] == 1)
    assert detector.calls[0][1:] == (1_000_000, 2_000_000)
    assert received[0][0]["source"]["name"] == "one"
    assert received[0][1]["shadow"] is True
    assert received[0][1]["candidates"][0]["source"] == "ml"
    json.dumps(received[0][1])
    assert worker.status()["device"] == "fake"
    assert worker.status()["inferred_samples"] == 16
    await worker.stop()


@pytest.mark.asyncio
async def test_duration_window_scales_with_rate_without_sample_cap():
    settings = SimpleNamespace(
        enabled=True, model_path="unused.pt", device="cpu", score_threshold=0.25,
        queue_size=1, max_job_age_s=0, window_duration_ms=100.0, interval_s=0,
    )
    detector = FakeDetector()
    worker = RFMLDetectorWorker(settings, lambda *_: None, lambda _: detector)
    await worker.start()
    iq = block(size=250_000)
    assert worker.submit(iq, 2_000_000, meta())
    await wait_for(lambda: worker.status()["completed"] == 1)
    assert len(detector.calls[0][0]) == 200_000
    assert worker.status()["inferred_samples"] == 200_000
    await worker.stop()


@pytest.mark.asyncio
async def test_disabled_never_constructs_backend():
    def forbidden(_):
        raise AssertionError("disabled worker imported a backend")

    worker = RFMLDetectorWorker(Settings(enabled=False), lambda *_: None, forbidden)
    await worker.start()
    assert not worker.submit(block(), 1, meta())
    assert worker.status()["state"] == "disabled"
    await worker.stop()


@pytest.mark.asyncio
async def test_drop_oldest_while_inference_busy():
    entered = threading.Event()
    release = threading.Event()

    def slow(iq, *_):
        entered.set()
        release.wait(1)
        return {"value": int(iq[0].real)}

    received = []
    worker = RFMLDetectorWorker(
        Settings(queue_size=2), lambda m, r: received.append((m["source"]["name"], r)),
        lambda _: FakeDetector(slow),
    )
    await worker.start()
    assert worker.submit(block(0), 1, meta("active"))
    assert await asyncio.to_thread(entered.wait, 1)
    for value, name in ((1, "old"), (2, "newer"), (3, "newest")):
        assert worker.submit(block(value), 1, meta(name))
    assert worker.status()["dropped"] == 1
    release.set()
    await wait_for(lambda: len(received) == 3)
    assert [name for name, _ in received] == ["active", "newer", "newest"]
    await worker.stop()


@pytest.mark.asyncio
async def test_invalidate_suppresses_inflight_and_clears_queue():
    entered = threading.Event()
    release = threading.Event()

    def slow(*_):
        entered.set()
        release.wait(1)
        return {"candidates": []}

    received = []
    worker = RFMLDetectorWorker(
        Settings(), lambda *args: received.append(args), lambda _: FakeDetector(slow)
    )
    await worker.start()
    assert worker.submit(block(), 1, meta())
    assert await asyncio.to_thread(entered.wait, 1)
    assert worker.submit(block(), 1, meta("queued"))
    worker.invalidate()
    assert worker.status()["queue"] == 0
    assert worker.status()["inferred_samples"] == 0
    assert worker.status()["processing_duty"] is None
    release.set()
    await wait_for(lambda: worker.status()["stale"] == 1)
    assert received == []
    assert worker.status()["inferred_samples"] == 0
    assert worker.status()["processing_duty"] is None
    await worker.stop()


@pytest.mark.asyncio
async def test_load_and_inference_failures_remain_nonfatal():
    def missing(_):
        raise ImportError("ultralytics missing")

    unavailable = RFMLDetectorWorker(Settings(), lambda *_: None, missing)
    await unavailable.start()
    await wait_for(lambda: unavailable.status()["state"] == "unavailable")
    assert "ultralytics missing" in unavailable.status()["error"]
    assert not unavailable.submit(block(), 1, meta())
    await unavailable.stop()

    calls = 0

    def flaky(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return {"candidates": []}

    received = []
    worker = RFMLDetectorWorker(
        Settings(), lambda *args: received.append(args), lambda _: FakeDetector(flaky)
    )
    await worker.start()
    assert worker.submit(block(), 1, meta())
    await wait_for(lambda: worker.status()["state"] == "error")
    assert worker.submit(block(), 1, meta())
    await wait_for(lambda: len(received) == 1)
    assert worker.status()["state"] == "ready"
    assert worker.status()["error"] is None
    await worker.stop()


@pytest.mark.asyncio
async def test_rejects_malformed_input_and_throttles():
    worker = RFMLDetectorWorker(Settings(interval_s=10), lambda *_: None, lambda _: FakeDetector())
    await worker.start()
    assert not worker.submit(block(size=15), 1, meta())
    assert not worker.submit(np.ones(16), 1, meta())
    assert not worker.submit(block(), 0, meta())
    assert not worker.submit(block(), 1, {})
    bad = block()
    bad[0] = np.nan
    assert not worker.submit(bad, 1, meta())
    assert worker.submit(block(), 1, meta())
    assert not worker.submit(block(), 1, meta())
    assert not worker.can_accept()

    class MustNotConvert:
        def __array__(self, *_):
            raise AssertionError("throttled input was converted")

    assert not worker.submit(MustNotConvert(), 1, meta())
    worker.invalidate()
    assert worker.can_accept()
    assert worker.submit(block(), 1, meta())
    await worker.stop()


@pytest.mark.asyncio
async def test_capture_age_drops_stale_job_before_inference():
    detector = FakeDetector()
    received = []
    worker = RFMLDetectorWorker(
        Settings(max_job_age_s=0.01), lambda *args: received.append(args), lambda _: detector
    )
    await worker.start()
    metadata = meta()
    metadata["capture_monotonic"] = time.monotonic() - 1
    assert worker.submit(block(), 1, metadata)
    await wait_for(lambda: worker.status()["dropped"] == 1)
    assert detector.calls == []
    assert received == []
    assert worker.status()["inferred_samples"] == 0
    await worker.stop()


@pytest.mark.asyncio
async def test_slow_inference_still_publishes_and_is_counted_as_slow():
    def slow_predict(iq, *_):
        time.sleep(0.05)
        return {"candidates": []}

    detector = FakeDetector(slow_predict)
    received = []
    worker = RFMLDetectorWorker(
        Settings(max_job_age_s=0.01), lambda *args: received.append(args), lambda _: detector
    )
    await worker.start()
    metadata = meta()
    metadata["capture_monotonic"] = time.monotonic()  # fresh: passes the pre-inference gate
    assert worker.submit(block(), 1, metadata)
    await wait_for(lambda: len(received) == 1)
    assert worker.status()["dropped"] == 0  # slow inference no longer discards the result
    assert worker.status()["slow"] == 1
    assert worker.status()["inferred_samples"] == 16
    assert received[0][1]["inference_s"] >= 0.01
    await worker.stop()
