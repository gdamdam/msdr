"""Lifecycle, status and bounded receive history for external applications."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from msdr.integrations.freedata import FreeDATAClient
from msdr.integrations.js8call import JS8CallClient
from msdr.integrations.pat import PatClient
from msdr.settings import IntegrationsSettings

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
HISTORY_LIMIT = 200


class IntegrationManager:
    def __init__(self, settings: IntegrationsSettings, *, on_event: EventCallback) -> None:
        self.on_event = on_event
        self.settings = settings
        self._clients: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._generation = {"js8call": 0, "pat": 0, "freedata": 0}
        self._configure_lock = asyncio.Lock()
        self._status: dict[str, dict[str, Any]] = {}
        self._messages: list[dict[str, Any]] = []
        self._message_ids: set[tuple[str, str]] = set()
        self._build_status(settings)

    def status(self) -> dict[str, Any]:
        return {key: dict(value) for key, value in self._status.items()}

    def messages(self, limit: int = 100) -> list[dict[str, Any]]:
        selected = self._messages[-max(1, min(limit, HISTORY_LIMIT)) :]
        return [dict(message) for message in reversed(selected)]

    async def start(self) -> None:
        for key in ("js8call", "pat", "freedata"):
            await self._start_one(key)

    async def stop(self) -> None:
        for key in tuple(self._tasks):
            await self._stop_one(key)

    async def configure(self, settings: IntegrationsSettings) -> None:
        async with self._configure_lock:
            old = self.settings.model_dump()
            new = settings.model_dump()
            self.settings = settings
            for key in ("js8call", "pat", "freedata"):
                if old[key] == new[key]:
                    continue
                await self._stop_one(key)
                self._status[key] = self._disabled_status(key, new[key]["enabled"])
                await self.on_event({"type": "integration_status", **self._status[key]})
                await self._start_one(key)

    async def _start_one(self, key: str) -> None:
        config = getattr(self.settings, key)
        if not config.enabled:
            self._status[key] = self._disabled_status(key, False)
            return
        generation = self._generation[key]

        async def publish_current(event: dict[str, Any]) -> None:
            if self._generation[key] == generation:
                await self._handle_event(event)

        if key == "js8call":
            client = JS8CallClient(
                config.host,
                config.port,
                on_event=publish_current,
            )
        elif key == "pat":
            client = PatClient(config.base_url, on_event=publish_current)
        else:
            client = FreeDATAClient(config.base_url, on_event=publish_current)
        self._clients[key] = client
        self._status[key] = client.status()
        self._tasks[key] = asyncio.create_task(client.run(), name=f"integration-{key}")

    async def _stop_one(self, key: str) -> None:
        self._generation[key] += 1
        task = self._tasks.pop(key, None)
        self._clients.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _handle_event(self, event: dict[str, Any]) -> None:
        publish_status = None
        if event.get("type") == "integration_status":
            key = str(event.get("source"))
            self._status[key] = {k: v for k, v in event.items() if k != "type"}
        elif event.get("type") == "integration_message":
            message = event.get("message")
            if isinstance(message, dict):
                key = str(message.get("source"))
                identity = (key, str(message.get("id", "")))
                if identity in self._message_ids:
                    return
                self._message_ids.add(identity)
                self._messages.append(message)
                self._messages = self._messages[-HISTORY_LIMIT:]
                self._message_ids = {
                    (str(item.get("source")), str(item.get("id", ""))) for item in self._messages
                }
                if key in self._status:
                    self._status[key]["last_message_at"] = message.get("received_at")
                    publish_status = {"type": "integration_status", **self._status[key]}
        await self.on_event(event)
        if publish_status is not None:
            await self.on_event(publish_status)

    def _build_status(self, settings: IntegrationsSettings) -> None:
        self._status = {
            "js8call": self._disabled_status("js8call", settings.js8call.enabled),
            "pat": self._disabled_status("pat", settings.pat.enabled),
            "freedata": self._disabled_status("freedata", settings.freedata.enabled),
        }

    def _disabled_status(self, key: str, enabled: bool) -> dict[str, Any]:
        status = {
            "source": key,
            "enabled": enabled,
            "connected": False,
            "error": "connecting" if enabled else None,
            "last_message_at": None,
        }
        if key == "js8call":
            status.update(host=self.settings.js8call.host, port=self.settings.js8call.port)
        elif key == "pat":
            status.update(base_url=self.settings.pat.base_url, inbox_count=0, unread_count=0)
        else:
            status.update(
                base_url=self.settings.freedata.base_url,
                inbox_count=0,
                unread_count=0,
                modem_running=False,
                radio_connected=False,
                radio_frequency_hz=None,
                radio_mode=None,
            )
        return status
