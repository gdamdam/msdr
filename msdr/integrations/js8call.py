"""Read-only client for JS8Call's local Network Message API.

This client only listens for packets emitted by JS8Call. It deliberately has
no command sender, so it cannot queue or transmit a message.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class JS8CallClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2242,
        *,
        on_event: EventCallback,
        reconnect_s: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_event = on_event
        self.reconnect_s = reconnect_s
        self._status: dict[str, Any] = {
            "source": "js8call",
            "enabled": True,
            "connected": False,
            "host": host,
            "port": port,
            "error": None,
            "last_message_at": None,
        }

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    async def run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=1_048_576), timeout=2.0
                )
                self._status.update(connected=True, error=None)
                await self.on_event({"type": "integration_status", **self.status()})
                try:
                    while True:
                        line = await reader.readline()
                        if not line:
                            raise ConnectionError("JS8Call closed the API connection")
                        if len(line) > 1_048_576:
                            raise ValueError("JS8Call API packet exceeds 1 MiB")
                        try:
                            packet = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        message = normalize_js8call_packet(packet)
                        if message is not None:
                            self._status["last_message_at"] = message["received_at"]
                            await self.on_event({"type": "integration_message", "message": message})
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, ConnectionError, ValueError) as exc:
                self._status.update(connected=False, error=str(exc))
                await self.on_event({"type": "integration_status", **self.status()})
                await asyncio.sleep(self.reconnect_s)


def normalize_js8call_packet(packet: Any) -> dict[str, Any] | None:
    """Return a provenance-tagged decoded receive event or ``None``.

    RX.DIRECTED carries finalized text in TEXT or value. RX.ACTIVITY carries
    decoded text in value; empty receive events are ignored.
    Other packets, including all TX events, are ignored.
    """
    if not isinstance(packet, dict):
        return None
    kind = packet.get("type")
    if not isinstance(kind, str) or kind not in {"RX.DIRECTED", "RX.ACTIVITY"}:
        return None
    params = packet.get("params")
    if not isinstance(params, dict):
        params = {}
    text = params.get("TEXT")
    if not isinstance(text, str) or not text.strip():
        text = packet.get("value")
    if not isinstance(text, str) or not text.strip():
        return None
    stamp = _js8_timestamp(params.get("UTC"))
    frequency = _positive_int(params.get("FREQ"))
    dial = _positive_int(params.get("DIAL"))
    stable_parts = [
        stamp,
        str(frequency or dial or ""),
        str(params.get("FROM", "")),
        text,
    ]
    return {
        "id": ":".join(stable_parts),
        "source": "js8call",
        "protocol": "JS8",
        "provenance": "Decoded text reported by JS8Call Network Message API",
        "received_at": stamp,
        "from": _clean_string(params.get("FROM")),
        "to": _clean_string(params.get("TO")),
        "text": text.strip(),
        "frequency_hz": frequency,
        "dial_frequency_hz": dial,
        "snr_db": _number(params.get("SNR")),
        "grid": _clean_string(params.get("GRID")),
        "source_event": kind,
    }


def _js8_timestamp(value: Any) -> str:
    try:
        millis = int(value)
        return datetime.fromtimestamp(millis / 1000, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC).isoformat()


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clean_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
