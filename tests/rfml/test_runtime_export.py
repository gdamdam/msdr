import asyncio
import threading
from dataclasses import dataclass

import numpy as np
import pytest

from msdr.rfml import corpus
from msdr.rfml.runtime import RFMLWorker


@dataclass
class Settings:
    enabled: bool = True
    device: str = "cpu"
    queue_size: int = 2
    max_job_age_s: float = 0
    export_dir: str = "examples"


class FakeClassifier:
    backend = "fake"

    def load(self):
        pass

    def predict(self, _iq, _rate):
        return {"classification": "FM"}


async def _wait_for(predicate, timeout=1):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.005)


def _install_writer(monkeypatch, writer_class):
    monkeypatch.setattr(corpus, "ExampleWriter", writer_class)


@pytest.mark.asyncio
async def test_export_failure_does_not_stop_result_callbacks(monkeypatch):
    class BrokenWriter:
        directory = "enabled"
        saved = 0

        def __init__(self, _settings):
            pass

        def save(self, *_args):
            raise OSError("disk full")

    _install_writer(monkeypatch, BrokenWriter)
    received = []
    worker = RFMLWorker(
        Settings(),
        lambda meta, result: received.append((meta, result)),
        lambda _: FakeClassifier(),
    )
    await worker.start()
    block = np.ones(4096, dtype=np.complex64)
    worker.submit(block, 1_000_000, "first")
    worker.submit(block, 1_000_000, "second")

    await _wait_for(lambda: len(received) == 2)

    assert [meta for meta, _ in received] == ["first", "second"]
    assert "disk full" in worker.status()["export_error"]
    assert worker.status()["state"] == "ready"
    await worker.stop()


@pytest.mark.asyncio
async def test_invalidation_during_export_suppresses_stale_result(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class SlowWriter:
        directory = "enabled"
        saved = 0

        def __init__(self, _settings):
            pass

        def save(self, *_args):
            entered.set()
            release.wait(1)

    _install_writer(monkeypatch, SlowWriter)
    received = []
    worker = RFMLWorker(Settings(), lambda *args: received.append(args), lambda _: FakeClassifier())
    await worker.start()
    worker.submit(np.ones(4096, dtype=np.complex64), 1_000_000, "old")
    assert await asyncio.to_thread(entered.wait, 1)

    worker.invalidate()
    release.set()
    await _wait_for(lambda: worker.status()["stale"] == 1)

    assert received == []
    await worker.stop()


@pytest.mark.asyncio
async def test_disabled_export_never_calls_writer(monkeypatch):
    class DisabledWriter:
        directory = None
        saved = 0

        def __init__(self, _settings):
            pass

        def save(self, *_args):
            pytest.fail("writer called while export is disabled")

    _install_writer(monkeypatch, DisabledWriter)
    received = []
    worker = RFMLWorker(
        Settings(export_dir=""),
        lambda meta, result: received.append((meta, result)),
        lambda _: FakeClassifier(),
    )
    await worker.start()
    worker.submit(np.ones(4096, dtype=np.complex64), 1_000_000, "no-export")
    await _wait_for(lambda: len(received) == 1)

    assert received[0][0] == "no-export"
    assert worker.status()["examples_saved"] == 0
    await worker.stop()
