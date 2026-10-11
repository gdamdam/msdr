"""Read-only integration with Pat's local Winlink HTTP interface.

Only GET requests are issued. In particular, this client never calls Pat's
connect, compose, delete, read-state, or position-report routes.
"""

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
MAX_INBOX_ITEMS = 50
MAX_BODY_CHARS = 4_000
SEEN_LIMIT = 500


class PatClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        on_event: EventCallback,
        poll_s: float = 10.0,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.on_event = on_event
        self.poll_s = poll_s
        self._status: dict[str, Any] = {
            "source": "pat",
            "enabled": True,
            "connected": False,
            "base_url": self.base_url,
            "error": None,
            "inbox_count": 0,
            "unread_count": 0,
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
                )
                await self.on_event({"type": "integration_status", **self.status()})
                for item in snapshot["items"]:
                    mid = item.get("MID")
                    if not isinstance(mid, str) or not mid:
                        continue
                    if mid in self._seen:
                        continue
                    # The detailed body is fetched once per new MID, not once
                    # per status poll. Keep the duplicate cache bounded.
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

    def _remember(self, mid: str) -> None:
        self._seen.add(mid)
        self._seen_order.append(mid)
        while len(self._seen_order) > SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())

    def _fetch_snapshot(self) -> dict[str, Any]:
        # Pat exposes these GET routes from its built-in HTTP server. Using
        # `/api/status` identifies a Pat server; `/api/mailbox/in` reads the
        # existing inbox without changing its read state.
        status = _get_json(self.base_url + "/api/status")
        if not isinstance(status, dict):
            raise ValueError("Pat /api/status returned an unexpected response")
        raw_items = _get_json(self.base_url + "/api/mailbox/in")
        if not isinstance(raw_items, list):
            raise ValueError("Pat /api/mailbox/in returned an unexpected response")
        items = [item for item in raw_items if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("Date") or ""), reverse=True)
        items = items[:MAX_INBOX_ITEMS]
        unread_count = sum(bool(item.get("Unread")) for item in items)
        return {"inbox_count": len(raw_items), "unread_count": unread_count, "items": items}

    def _message(self, item: dict[str, Any]) -> dict[str, Any]:
        mid = item.get("MID")
        if not isinstance(mid, str) or not mid:
            raise ValueError("Pat inbox record has no message id")
        details = _get_json(self.base_url + "/api/mailbox/in/" + quote(mid, safe=""))
        if not isinstance(details, dict):
            details = item
        body = details.get("Body")
        return {
            "id": mid,
            "source": "pat",
            "protocol": "Winlink",
            "provenance": "Pat Winlink mailbox via Pat HTTP API",
            "received_at": details.get("Date") or item.get("Date"),
            "from": _address(details.get("From") or item.get("From")),
            "to": [_address(value) for value in (details.get("To") or item.get("To") or [])],
            "subject": details.get("Subject") or item.get("Subject") or "(no subject)",
            "text": body[:MAX_BODY_CHARS] if isinstance(body, str) else "",
            "body_truncated": isinstance(body, str) and len(body) > MAX_BODY_CHARS,
            "unread": bool(details.get("Unread", item.get("Unread", False))),
            "attachment_count": len(details.get("Files") or item.get("Files") or []),
            "pat_message_url": f"{self.base_url}/ui",
        }


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Pat URL must be an http:// or https:// URL with a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Pat URL cannot contain credentials, query, or fragment")
    return value.strip().rstrip("/")


def _get_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "msdr-integration"})
    try:
        # urllib work runs in a thread so network I/O cannot stall the service
        # loop. Its socket timeout also bounds a worker that outlives task
        # cancellation during reconfiguration or application shutdown.
        with urlopen(request, timeout=2.5) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise ValueError(f"Pat endpoint returned {content_type}, expected application/json")
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Pat API response exceeds 8 MiB")
            return json.loads(data)
    except HTTPError as exc:
        raise ValueError(f"Pat API returned HTTP {exc.code} for {urlsplit(url).path}") from exc
    except URLError as exc:
        raise OSError(f"Pat API unavailable: {exc.reason}") from exc


def _address(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("Address") or value.get("address") or "")
    return ""
