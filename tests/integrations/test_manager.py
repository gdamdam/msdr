import asyncio

import pytest

from msdr.integrations import manager as manager_module
from msdr.settings import (
    FreeDATAIntegrationSettings,
    IntegrationsSettings,
    JS8CallIntegrationSettings,
)


class FakeJS8Client:
    instances = []

    def __init__(self, host, port, *, on_event):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.instances.append(self)

    def status(self):
        return {"source": "js8call", "enabled": True, "connected": False, "error": None}

    async def run(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_reconfigure_ignores_old_client_events_and_updates_last_message(monkeypatch):
    FakeJS8Client.instances = []
    monkeypatch.setattr(manager_module, "JS8CallClient", FakeJS8Client)
    events = []

    async def publish(event):
        events.append(event)

    settings = IntegrationsSettings(
        js8call=JS8CallIntegrationSettings(enabled=True, host="127.0.0.1", port=2242),
    )
    manager = manager_module.IntegrationManager(settings, on_event=publish)
    await manager.start()
    old_callback = FakeJS8Client.instances[0].on_event

    updated = IntegrationsSettings(
        js8call=JS8CallIntegrationSettings(enabled=True, host="localhost", port=2243),
    )
    await manager.configure(updated)
    new_callback = FakeJS8Client.instances[1].on_event
    before = len(events)
    await old_callback(
        {
            "type": "integration_message",
            "message": {"source": "js8call", "id": "old"},
        }
    )
    assert len(events) == before

    await new_callback(
        {
            "type": "integration_message",
            "message": {
                "source": "js8call",
                "id": "fresh",
                "received_at": "2026-09-30T19:00:00+00:00",
                "text": "fresh event",
            },
        }
    )
    assert manager.status()["js8call"]["last_message_at"] == "2026-09-30T19:00:00+00:00"
    assert manager.messages()[0]["id"] == "fresh"
    await manager.stop()


@pytest.mark.asyncio
async def test_freedata_lifecycle_is_independent_of_pat(monkeypatch):
    class FakeFreeDATAClient(FakeJS8Client):
        def __init__(self, base_url, *, on_event):
            self.base_url = base_url
            self.on_event = on_event
            self.instances.append(self)

        def status(self):
            return {"source": "freedata", "enabled": True, "connected": False, "error": None}

    FakeFreeDATAClient.instances = []
    monkeypatch.setattr(manager_module, "FreeDATAClient", FakeFreeDATAClient)
    events = []

    async def publish(event):
        events.append(event)

    manager = manager_module.IntegrationManager(IntegrationsSettings(), on_event=publish)
    assert manager.status()["freedata"]["enabled"] is False
    await manager.start()
    assert not FakeFreeDATAClient.instances
    await manager.configure(
        IntegrationsSettings(
            freedata=FreeDATAIntegrationSettings(enabled=True, base_url="http://127.0.0.1:5000"),
        )
    )
    assert len(FakeFreeDATAClient.instances) == 1
    assert manager.status()["pat"]["enabled"] is False
    await manager.stop()
