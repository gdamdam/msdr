import array
import asyncio
import time
from dataclasses import dataclass, field

import pytest

from msdr.decoder.audio_stream import AudioFrame


def make_stereo_frame(n_samples: int, left: int = 1000) -> bytes:
    """Synthetic int16 stereo PCM, L=R duplicated (matches SDRconnect's measured format)."""
    return array.array("h", [left, left] * n_samples).tobytes()


class FakeAudioStream:
    """AudioStream double: no socket, just a queue the test feeds directly."""

    def __init__(self):
        self.started = False
        self.enabled = False
        self.connected = False
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True
        self.connected = True

    async def stop(self) -> None:
        self.started = False
        self.connected = False
        self.enabled = False

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        while True:
            yield await self._queue.get()

    def push(self, data: bytes, received_at: float | None = None) -> None:
        at = received_at if received_at is not None else time.time()
        self._queue.put_nowait(AudioFrame(data, at))


class FakeAdapter:
    """SDRconnectAdapter double: dict-backed get/set, records every set()."""

    def __init__(self, props: dict[str, object]):
        self.props = dict(props)
        self.sets: list[tuple[str, object]] = []

    async def get(self, prop: str) -> object:
        return self.props.get(prop)

    async def set(self, prop: str, value: object) -> None:
        self.props[prop] = value
        self.sets.append((prop, value))


class FakeRepo:
    """DecodeEventRepo double: add() returns a dict like the real repo would."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, event) -> dict:
        row = {
            "id": f"evt{len(self.rows)}",
            "ts": event.ts,
            "decoder": event.decoder,
            "kind": event.kind,
            "text": event.text,
            "fields": event.fields,
            "frequency_hz": event.frequency_hz,
            "signal_id": event.signal_id,
            "image_path": getattr(event, "image_path", None),
        }
        self.rows.append(row)
        return row

    def get(self, event_id: str) -> dict | None:
        return next((r for r in self.rows if r["id"] == event_id), None)

    def update_event(
        self, event_id: str, *, text: str | None = None, fields: dict | None = None
    ) -> dict | None:
        row = self.get(event_id)
        if row is None:
            return None
        if fields:
            row["fields"] = {**row["fields"], **fields}
        if text is not None:
            row["text"] = text
        return row

    def count(self) -> int:
        return len(self.rows)

    def trim(self, limit: int) -> int:
        return 0


class FakeConfirmed:
    """ConfirmedState double: subscribe()/unsubscribe() plus a test-only emit()."""

    def __init__(self):
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, sc) -> None:
        for q in self._subs:
            q.put_nowait(sc)


@dataclass
class FakeDecoderSettings:
    """DecoderSettings double (Stream B owns the real pydantic model)."""

    bin_dirs: list[str] = field(default_factory=list)
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    history_limit: int = 5000


@pytest.fixture
def adapter():
    return FakeAdapter(
        {
            "squelch_enable": False,
            "demodulator": "NFM",
            "filter_bandwidth": 12500,
            "started": True,
        }
    )


@pytest.fixture
def stream():
    return FakeAudioStream()


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def confirmed():
    return FakeConfirmed()


@pytest.fixture
def published():
    return []


@pytest.fixture
def settings():
    return FakeDecoderSettings()
