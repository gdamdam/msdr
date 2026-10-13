"""ADS-B / Mode S decodes as SBS-1 (BaseStation) lines on a local TCP server.

tar1090/readsb, Virtual Radar Server and PlanePlotter connect to this port the
same way they connect to dump1090's port 30003.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Per-client backlog before lines are dropped, mirroring api.service.Broadcaster:
# a client that stays full for this many consecutive lines (or can't drain for
# DRAIN_TIMEOUT_S) is disconnected so it reconnects fresh instead of making
# msdr buffer without bound.
CLIENT_QUEUE_LINES = 512
SLOW_FULL_STREAK = 64
DRAIN_TIMEOUT_S = 5.0

# Fields each MSG transmission type carries (the rest stay empty), per the
# BaseStation message spec: 1 ident, 2 surface pos, 3 airborne pos,
# 4 velocity, 5 surveillance alt, 6 surveillance ident/squawk, 7 air-to-air, 8 all-call.
_TYPE_FIELDS: dict[int, frozenset[str]] = {
    1: frozenset({"callsign"}),
    2: frozenset({"altitude", "gs", "track", "lat", "lon", "ground"}),
    3: frozenset({"altitude", "lat", "lon", "flags", "ground"}),
    4: frozenset({"gs", "track", "vr"}),
    5: frozenset({"altitude", "flags", "ground"}),
    6: frozenset({"altitude", "squawk", "flags", "ground"}),
    7: frozenset({"altitude", "ground"}),
    8: frozenset({"ground"}),
}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> str:
    n = _num(value)
    return "" if n is None else str(round(n))


def _deg(value: Any) -> str:
    n = _num(value)
    return "" if n is None else f"{n:.5f}"


def _flag(value: Any) -> str:
    # SBS booleans are 0 / -1 (true); accept bools and the same strings back.
    if value in (True, "-1", -1, "1", 1):
        return "-1"
    return "0"


def transmission_type(fields: dict[str, Any]) -> int | None:
    """MSG type from the decoder's own `transmission_type`, else inferred from
    which fields are present. None when the event has nothing SBS can carry."""
    has_pos = _num(fields.get("latitude")) is not None and _num(fields.get("longitude")) is not None
    has_alt = _num(fields.get("altitude_ft")) is not None
    declared = _num(fields.get("transmission_type"))
    tt = int(declared) if declared is not None and int(declared) in _TYPE_FIELDS else None
    if tt == 3 and not has_pos:
        # A native-decoder "airborne position" with only an altitude decoded.
        tt = 5 if has_alt else None
    if tt is None:
        if fields.get("callsign"):
            tt = 1
        elif has_pos:
            tt = 3
        elif _num(fields.get("ground_speed_kt")) is not None:
            tt = 4
        elif fields.get("squawk"):
            tt = 6
        elif has_alt:
            tt = 5
    return tt


def format_sbs(fields: dict[str, Any], ts: float) -> str | None:
    """One SBS-1 `MSG` line (no line terminator) for an ADS-B decode's fields,
    or None if it has no ICAO address or no field SBS can carry."""
    icao = str(fields.get("icao") or "").strip().upper()
    tt = transmission_type(fields)
    if len(icao) != 6 or tt is None:
        return None
    keep = _TYPE_FIELDS[tt]
    stamp = datetime.fromtimestamp(ts)
    date, clock = stamp.strftime("%Y/%m/%d"), stamp.strftime("%H:%M:%S.%f")[:-3]
    cols = [
        "MSG",
        str(tt),
        "1",  # session id: constants, as dump1090 does
        "1",  # aircraft id
        icao,
        "1",  # flight id
        date,
        clock,
        date,
        clock,
        str(fields.get("callsign") or "").strip() if "callsign" in keep else "",
        _int(fields.get("altitude_ft")) if "altitude" in keep else "",
        _int(fields.get("ground_speed_kt")) if "gs" in keep else "",
        _int(fields.get("track_deg")) if "track" in keep else "",
        _deg(fields.get("latitude")) if "lat" in keep else "",
        _deg(fields.get("longitude")) if "lon" in keep else "",
        _int(fields.get("vertical_rate_fpm")) if "vr" in keep else "",
        str(fields.get("squawk") or "").strip() if "squawk" in keep else "",
        _flag(fields.get("alert")) if "flags" in keep else "0",
        _flag(fields.get("emergency")) if "flags" in keep else "0",
        _flag(fields.get("spi")) if "flags" in keep else "0",
        _flag(fields.get("on_ground")) if "ground" in keep else "0",
    ]
    return ",".join(cols)


class _Client:
    def __init__(self, writer: asyncio.StreamWriter, queue_lines: int) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(queue_lines)
        self.full_streak = 0
        self.dropped = 0
        self.task: asyncio.Task | None = None


class SbsServer:
    """Fan-out TCP server. `publish()` never awaits a socket: each client has
    its own writer task and bounded queue, so one stalled reader can't block
    the event loop or other clients."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 30003,
        *,
        queue_lines: int = CLIENT_QUEUE_LINES,
        slow_full_streak: int = SLOW_FULL_STREAK,
        drain_timeout_s: float = DRAIN_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self._queue_lines = queue_lines
        self._slow_streak = slow_full_streak
        self._drain_timeout_s = drain_timeout_s
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[_Client] = set()
        self.lines_sent = 0
        self.clients_dropped = 0
        self.last_error: str | None = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
            self.last_error = None
        except OSError as exc:
            self.last_error = f"cannot listen on {self.host}:{self.port}: {exc}"
            logger.warning("sbs feed: %s", self.last_error)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for client in list(self._clients):
            self._drop(client)
        if server is not None:
            with contextlib.suppress(Exception):
                await server.wait_closed()

    def publish(self, line: str) -> None:
        data = (line + "\r\n").encode("ascii", "replace")
        for client in list(self._clients):
            try:
                client.queue.put_nowait(data)
                client.full_streak = 0
            except asyncio.QueueFull:
                client.dropped += 1
                client.full_streak += 1
                if client.full_streak >= self._slow_streak:
                    logger.warning("sbs feed: slow client disconnected (%d lines dropped)",
                                   client.dropped)
                    self._drop(client)

    def status(self) -> dict[str, Any]:
        return {
            "listening": self.listening,
            "host": self.host,
            "port": self.bound_port or self.port,
            "clients": self.client_count,
            "sent": self.lines_sent,
            "clients_dropped": self.clients_dropped,
            "last_error": self.last_error,
        }

    # -- internals ---------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer, self._queue_lines)
        self._clients.add(client)
        client.task = asyncio.current_task()
        try:
            while True:
                data = await client.queue.get()
                writer.write(data)
                await asyncio.wait_for(writer.drain(), self._drain_timeout_s)
                self.lines_sent += 1
        except (asyncio.CancelledError, ConnectionError, TimeoutError, OSError):
            pass
        finally:
            self._clients.discard(client)
            writer.close()

    def _drop(self, client: _Client) -> None:
        if client in self._clients:
            self._clients.discard(client)
            self.clients_dropped += 1
        if client.task is not None and client.task is not asyncio.current_task():
            client.task.cancel()
        client.writer.close()
