import asyncio
import threading
from dataclasses import dataclass

import numpy as np
import pytest

from msdr.rfml.runtime import RFMLWorker


@dataclass
class Settings:
    enabled: bool = True
    model_path: str = "model.pt"
    device: str = "cpu"
    score_threshold: float = 0.5
    queue_size: int = 2
    max_job_age_s: float = 0


class FakeClassifier:
    device = "cpu"
    backend = "fake-backend"

    def __init__(self, predict=None):
        self.loads = 0
        self.predict_fn = predict
        self.calls = []

    def load(self):
        self.loads += 1

    def predict(self, iq, sample_rate_hz):
        self.calls.append((iq, sample_rate_hz))
        if self.predict_fn:
            return self.predict_fn(iq, sample_rate_hz)
        return {"backend": "fake", "n": len(iq)}


async def wait_for(predicate, timeout=1):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_load_once_and_dispatch_result_on_event_loop():
    classifier = FakeClassifier()
    received = []
    worker = RFMLWorker(
        Settings(),
        lambda metadata, result: received.append((metadata, result)),
        lambda _: classifier,
    )
    await worker.start()
    await worker.start()
    assert worker.submit(np.ones(4096, dtype=np.complex64), 1_000_000, {"id": 7})
    await wait_for(lambda: len(received) == 1)
    assert classifier.loads == 1
    assert received[0][0] == {"id": 7}
    assert received[0][1]["backend"] == "fake"
    assert worker.status()["device"] == "fake-backend"
    await worker.stop()


@pytest.mark.asyncio
async def test_disabled_never_constructs_classifier():
    def forbidden(_):
        raise AssertionError("factory called while disabled")

    worker = RFMLWorker(Settings(enabled=False), lambda *_: None, forbidden)
    await worker.start()
    assert not worker.submit(np.ones(4, dtype=np.complex64), 1, None)
    assert worker.status()["state"] == "disabled"
    await worker.stop()


@pytest.mark.asyncio
async def test_load_failure_is_reported_and_worker_does_not_raise():
    def fail(_):
        raise ImportError("torch missing")

    worker = RFMLWorker(Settings(), lambda *_: None, fail)
    await worker.start()
    await wait_for(lambda: worker.status()["state"] == "unavailable")
    assert "torch missing" in worker.status()["error"]
    await worker.stop()


@pytest.mark.asyncio
async def test_drop_oldest_when_queue_is_full():
    entered = threading.Event()
    release = threading.Event()

    def slow(iq, rate):
        entered.set()
        release.wait(1)
        return {"backend": "fake", "first": int(iq[0].real)}

    classifier = FakeClassifier(slow)
    received = []
    settings = Settings(queue_size=2)
    worker = RFMLWorker(settings, lambda m, r: received.append((m, r)), lambda _: classifier)
    await worker.start()
    active = np.zeros(4096, dtype=np.complex64)
    worker.submit(active, 1, "active")
    await asyncio.to_thread(entered.wait, 1)
    worker.submit(np.full(4096, 1, dtype=np.complex64), 1, "old")
    worker.submit(np.full(4096, 2, dtype=np.complex64), 1, "newer")
    worker.submit(np.full(4096, 3, dtype=np.complex64), 1, "newest")
    assert worker.status()["dropped"] == 1
    release.set()
    await wait_for(lambda: len(received) == 3)
    assert [item[0] for item in received] == ["active", "newer", "newest"]
    await worker.stop()


@pytest.mark.asyncio
async def test_invalidate_suppresses_inflight_and_clears_queue():
    entered = threading.Event()
    release = threading.Event()

    def slow(iq, rate):
        entered.set()
        release.wait(1)
        return {"backend": "fake"}

    worker = RFMLWorker(
        Settings(), lambda *_: pytest.fail("stale callback"), lambda _: FakeClassifier(slow)
    )
    await worker.start()
    worker.submit(np.ones(4096, dtype=np.complex64), 1, "stale")
    await asyncio.to_thread(entered.wait, 1)
    worker.submit(np.ones(4096, dtype=np.complex64), 1, "queued")
    worker.invalidate()
    release.set()
    await wait_for(lambda: worker.status()["stale"] == 1)
    assert worker.status()["queue"] == 0
    await worker.stop()


@pytest.mark.asyncio
async def test_stop_suppresses_inflight_callback():
    entered = threading.Event()
    release = threading.Event()
    received = []

    def slow(iq, rate):
        entered.set()
        release.wait(1)
        return {"backend": "fake"}

    worker = RFMLWorker(
        Settings(), lambda *args: received.append(args), lambda _: FakeClassifier(slow)
    )
    await worker.start()
    worker.submit(np.ones(4096, dtype=np.complex64), 1, "x")
    await asyncio.to_thread(entered.wait, 1)
    await worker.stop()
    release.set()
    await asyncio.sleep(0.03)
    assert received == []


@pytest.mark.asyncio
async def test_rejects_short_or_malformed_iq_and_copies_bounded_prefix():
    classifier = FakeClassifier()
    worker = RFMLWorker(Settings(), lambda *_: None, lambda _: classifier)
    await worker.start()
    assert not worker.submit(np.ones(4095, dtype=np.complex64), 1, None)
    assert not worker.submit(np.ones(4096, dtype=np.float32), 1, None)
    source = np.ones(4 * 4096, dtype=np.complex128)
    assert worker.submit(source, 1, None)
    source[:] = 7
    await wait_for(lambda: len(classifier.calls) == 1)
    seen, _ = classifier.calls[0]
    assert len(seen) == 3 * 4096
    assert seen.dtype == np.complex64
    assert np.all(seen == 1)
    await worker.stop()


@pytest.mark.asyncio
async def test_requires_start_and_copies_metadata():
    received = []
    worker = RFMLWorker(
        Settings(), lambda metadata, _: received.append(metadata), lambda _: FakeClassifier()
    )
    block = np.ones(4096, dtype=np.complex64)
    assert not worker.submit(block, 1, {"nested": {"value": 1}})
    await worker.start()
    metadata = {"nested": {"value": 1}}
    assert worker.submit(block, 1, metadata)
    metadata["nested"]["value"] = 2
    await wait_for(lambda: len(received) == 1)
    assert received[0]["nested"]["value"] == 1
    assert worker.status()["device"] == "fake-backend"
    await worker.stop()


@pytest.mark.asyncio
async def test_slow_inference_still_publishes_and_is_counted_as_slow():
    entered = threading.Event()
    release = threading.Event()
    received = []

    def slow(iq, rate):
        entered.set()
        release.wait(1)
        return {"classification": "FM"}

    worker = RFMLWorker(
        Settings(max_job_age_s=0.02),
        lambda *args: received.append(args),
        lambda _: FakeClassifier(slow),
    )
    await worker.start()
    worker.submit(np.ones(4096, dtype=np.complex64), 1, "slow")
    await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.03)  # inference already exceeds max_job_age_s
    release.set()
    await wait_for(lambda: len(received) == 1)
    assert worker.status()["dropped"] == 0  # slow inference no longer discards the result
    assert worker.status()["slow"] == 1
    assert received[0][1]["inference_s"] >= 0.02
    await worker.stop()


@pytest.mark.asyncio
async def test_success_recovers_state_after_inference_error():
    calls = 0

    def flaky(iq, rate):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary inference failure")
        return {"classification": "FM"}

    received = []
    worker = RFMLWorker(
        Settings(), lambda m, r: received.append((m, r)), lambda _: FakeClassifier(flaky)
    )
    await worker.start()
    block = np.ones(4096, dtype=np.complex64)
    worker.submit(block, 1, "fails")
    await wait_for(lambda: worker.status()["state"] == "error")
    worker.submit(block, 1, "recovers")
    await wait_for(lambda: len(received) == 1)
    assert received[0][0] == "recovers"
    assert received[0][1]["backend"] == "fake-backend"
    assert worker.status()["state"] == "ready"
    assert worker.status()["error"] is None
    await worker.stop()
