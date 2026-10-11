"""/ws/audio single-consumer slot: a disconnect during a sample gap (no audio
flowing) must release the reservation immediately, not only on the next
sample delivery attempt.

The handler loop only ever awaited the next PCM sample (`pending_audio`) with
a 0.5s poll timeout -- it never read from the socket, so a client that
disconnects while no sample is pending went unnoticed until (if ever) a
sample eventually arrived and a send attempt failed. This drives the actual
`ws_audio` coroutine registered on the app (extracted from its routes) with a
fake websocket/receiver double, since a real sample gap can't be produced
through the synchronous TestClient without a hang.
"""

import asyncio
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database


def _ws_audio_endpoint():
    # create_app() only registers routes/middleware -- AppService (and any
    # background work) is built inside the lifespan context, which this test
    # never enters, so this is cheap and side-effect-free.
    app = create_app(Settings(db_path=":memory:"), adapter=FakeAdapter(), db=Database(":memory:"))
    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/ws/audio"]
    return route.endpoint


class _FakeRx:
    """A receiver whose audio() never yields -- the sample gap."""

    async def audio(self):
        await asyncio.sleep(10_000)
        if False:  # pragma: no cover - makes this an async generator
            yield None

    def status(self):
        return {"channel_generation": 0}


class _FakeWebSocket:
    def __init__(self, service):
        self.headers: dict = {}
        self.app = SimpleNamespace(state=SimpleNamespace(service=service))
        self.url = SimpleNamespace(path="/ws/audio")
        self.sent_json: list = []
        self.sent_bytes: list = []
        self.closed = False

    async def accept(self):
        pass

    async def receive_json(self):
        return {"type": "start"}

    async def receive_text(self):
        # Simulates the client's TCP connection dropping partway through a
        # sample gap -- no sample is pending when this fires.
        await asyncio.sleep(0.05)
        raise WebSocketDisconnect(1000)

    async def send_json(self, obj):
        self.sent_json.append(obj)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, code=None):
        self.closed = True


async def test_disconnect_during_sample_gap_releases_slot_promptly():
    ws_audio = _ws_audio_endpoint()
    service = SimpleNamespace(virtual_rx=_FakeRx(), _audio_ws_active=False)
    fake_ws = _FakeWebSocket(service)

    task = asyncio.create_task(ws_audio(fake_ws))
    # Let the handshake complete and the slot get reserved.
    await asyncio.sleep(0.01)
    assert service._audio_ws_active is True

    # The handler must notice the disconnect (fired at ~0.05s by the fake
    # socket) well before the 0.5s sample-wait poll would next elapse, since
    # no sample is ever pending in this gap.
    await asyncio.wait_for(task, timeout=0.3)

    assert service._audio_ws_active is False
    assert fake_ws.closed is True
