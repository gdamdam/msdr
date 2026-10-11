import array
import asyncio
import time

import pytest

from msdr.recorder import IQFrame, RecorderConfig


def make_frame_bytes(n_samples: int) -> bytes:
    """Synthetic interleaved int16 I/Q, `n_samples` complex samples (4 bytes each)."""
    return array.array("h", [1000, -1000] * n_samples).tobytes()


class FakeStream:
    """IQStream double: no socket, just a queue the test feeds directly.

    Recorder only ever calls start/stop/enable/frames, so this is a drop-in
    replacement for real IQStream in tests.
    """

    def __init__(self):
        self.started = False
        self.enabled = False
        self._queue: asyncio.Queue[IQFrame] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        self.enabled = False

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        while True:
            yield await self._queue.get()

    def push(self, n_samples: int, received_at: float | None = None) -> None:
        """Queue one synthetic frame of `n_samples` complex int16 IQ samples."""
        self.push_raw(make_frame_bytes(n_samples), received_at)

    def push_raw(self, data: bytes, received_at: float | None = None) -> None:
        at = received_at if received_at is not None else time.time()
        self._queue.put_nowait(IQFrame(data, at))

    def push_burst(self, count: int, n_samples: int) -> None:
        """Queue several frames back-to-back, all already buffered (no waiting)."""
        for _ in range(count):
            self.push(n_samples)


class FakeClock:
    """Test-controlled clock for Recorder's injectable `clock` parameter."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def stream():
    return FakeStream()


@pytest.fixture
def config(tmp_path):
    return RecorderConfig(root=tmp_path, preroll_s=0.5, min_free_gb=0, segment_s=0.05)
