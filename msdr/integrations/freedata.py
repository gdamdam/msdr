"""Read-only receive/status link to a separately running FreeDATA server."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
MAX_MESSAGES = 50
MAX_BODY_CHARS = 4_000
MAX_ATTACHMENTS = 20
SEEN_LIMIT = 500
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class FreeDATAClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5000",
        *,
        on_event: EventCallback,
        poll_s: float = 10.0,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.on_event = on_event
        self.poll_s = poll_s
        self._status: dict[str, Any] = {
            "source": "freedata",
            "enabled": True,
            "connected": False,
            "base_url": self.base_url,
            "error": None,
            "inbox_count": 0,
            "unread_count": 0,
            "modem_running": False,
            "radio_connected": False,
            "radio_frequency_hz": None,
            "radio_mode": None,
            "last_message_at": None,
        }
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    async def run(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self._fetch_snapshot)
                self._status.update(
                    connected=True,
                    error=None,
                    inbox_count=snapshot["inbox_count"],
                    unread_count=snapshot["unread_count"],
                    modem_running=snapshot["modem_running"],
                    radio_connected=snapshot["radio_connected"],
                    radio_frequency_hz=snapshot["radio_frequency_hz"],
                    radio_mode=snapshot["radio_mode"],
                )
                await self.on_event({"type": "integration_status", **self.status()})
                # Oldest first so the shared history is in arrival order.
                for item in reversed(snapshot["items"]):
                    mid = item["id"]
                    if mid in self._seen:
                        continue
                    message = await asyncio.to_thread(self._message, item)
                    self._remember(mid)
                    self._status["last_message_at"] = message["received_at"]
                    await self.on_event({"type": "integration_message", "message": message})
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                self._status.update(connected=False, error=str(exc))
                await self.on_event({"type": "integration_status", **self.status()})
            await asyncio.sleep(self.poll_s)

    def _fetch_snapshot(self) -> dict[str, Any]:
        identity = _get_json(self.base_url + "/")
        if not isinstance(identity, dict) or identity.get("name") != "FreeDATA API":
            raise ValueError("URL is not a FreeDATA API server")
        state = _get_json(self.base_url + "/modem/state")
        if not isinstance(state, dict):
            raise ValueError("FreeDATA /modem/state returned an unexpected response")
        mailbox = _get_json(self.base_url + "/freedata/messages")
        if not isinstance(mailbox, dict) or not isinstance(mailbox.get("messages"), list):
            raise ValueError("FreeDATA /freedata/messages returned an unexpected response")
        received = [
            item
            for item in mailbox["messages"]
            if isinstance(item, dict)
            and item.get("direction") in {"receive", "received"}
            and isinstance(item.get("id"), str)
            and item["id"]
        ]
        received.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return {
            "inbox_count": len(received),
            "unread_count": sum(not bool(item.get("is_read", True)) for item in received),
            "items": received[:MAX_MESSAGES],
            "modem_running": bool(state.get("is_modem_running")),
            "radio_connected": bool(state.get("radio_status")),
            "radio_frequency_hz": _frequency(state.get("radio_frequency")),
            "radio_mode": state.get("radio_mode")
            if isinstance(state.get("radio_mode"), str)
            else None,
        }

    def _message(self, item: dict[str, Any]) -> dict[str, Any]:
        mid = item["id"]
        raw_attachments = item.get("attachments")
        if not isinstance(raw_attachments, list):
            raw_attachments = []
        if raw_attachments:
            # The mailbox list may contain only attachment IDs. Fetch metadata,
            # never attachment bytes, and only once for each new message.
            try:
                details = _get_json(
                    self.base_url + "/freedata/messages/" + quote(mid, safe="") + "/attachments"
                )
                if isinstance(details, dict) and isinstance(details.get("attachments"), list):
                    raw_attachments = details["attachments"]
            except (OSError, TimeoutError, ValueError):
                # Keep the received body visible if attachment metadata is
                # temporarily unavailable. The source app still owns files.
                pass
        names = [
            str(attachment["filename"])[:255]
            for attachment in raw_attachments[:MAX_ATTACHMENTS]
            if isinstance(attachment, dict) and isinstance(attachment.get("filename"), str)
        ]
        body = item.get("body")
        if not isinstance(body, str):
            body = ""
        return {
            "id": mid,
            "source": "freedata",
            "protocol": "FreeDATA",
            "provenance": "FreeDATA receive mailbox via its HTTP API",
            "received_at": item.get("timestamp"),
            "from": item.get("origin") if isinstance(item.get("origin"), str) else "",
            "to": item.get("destination") if isinstance(item.get("destination"), str) else "",
            "text": body[:MAX_BODY_CHARS],
            "body_truncated": len(body) > MAX_BODY_CHARS,
            "unread": not bool(item.get("is_read", True)),
            "attachment_count": len(raw_attachments),
            "attachment_names": names,
        }

    def _remember(self, mid: str) -> None:
        self._seen.add(mid)
        self._seen_order.append(mid)
        while len(self._seen_order) > SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("FreeDATA URL must be a base http(s) URL without credentials or a path")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("FreeDATA URL has an invalid port") from exc
    return value.strip().rstrip("/")


def _frequency(value: Any) -> int | None:
    try:
        frequency = int(value)
    except (TypeError, ValueError):
        return None
    return frequency if frequency > 0 else None


def _get_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "msdr-integration"})
    try:
        with urlopen(request, timeout=2.5) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise ValueError(
                    f"FreeDATA endpoint returned {content_type}, expected application/json"
                )
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("FreeDATA API response exceeds 8 MiB")
            return json.loads(data)
    except HTTPError as exc:
        raise ValueError(f"FreeDATA API returned HTTP {exc.code} for {urlsplit(url).path}") from exc
    except URLError as exc:
        raise OSError(f"FreeDATA API unavailable: {exc.reason}") from exc
