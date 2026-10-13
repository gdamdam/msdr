"""AIS decodes forwarded as `!AIVDM` NMEA 0183 sentences over UDP (OpenCPN's
network connection default is UDP 10110).

The AIS decoder keeps the CRC-verified frame as `payload_hex`, so the original
6-bit-armoured payload is rebuilt from it rather than re-encoded from decoded
fields (which would lose everything not yet decoded, e.g. type 5 names).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# NMEA 0183 caps a sentence at 82 characters including `!` and CRLF; 60 payload
# characters leaves room for the longest header/trailer (`!AIVDM,2,1,9,A,` +
# `,0*XX`) with the margin most AIS software uses.
MAX_PAYLOAD_CHARS = 60


def nmea_checksum(body: str) -> str:
    """XOR of the characters between `!`/`$` and `*`, as two hex digits."""
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f"{value:02X}"


def armour_payload(data: bytes) -> tuple[str, int]:
    """(6-bit ASCII armoured payload, fill bits) for AIS frame bytes."""
    nbits = len(data) * 8
    fill = (-nbits) % 6
    value = int.from_bytes(data, "big") << fill if data else 0
    chars = []
    for i in range((nbits + fill) // 6):
        v = (value >> ((nbits + fill) - 6 * (i + 1))) & 0x3F
        chars.append(chr(v + 48 if v < 40 else v + 56))
    return "".join(chars), fill


def format_aivdm(
    data: bytes, *, talker: str = "AIVDM", channel: str = "A", seq_id: int = 0
) -> list[str]:
    """NMEA sentences (no line terminator) carrying one AIS frame. A frame
    over MAX_PAYLOAD_CHARS is split into fragments sharing `seq_id`."""
    payload, fill = armour_payload(data)
    if not payload:
        return []
    parts = [payload[i : i + MAX_PAYLOAD_CHARS] for i in range(0, len(payload), MAX_PAYLOAD_CHARS)]
    total = len(parts)
    out = []
    for n, part in enumerate(parts, start=1):
        # Fill bits belong to the last fragment only; single-part sentences
        # carry an empty sequence id.
        body = (
            f"{talker},{total},{n},{seq_id if total > 1 else ''},{channel},{part},"
            f"{fill if n == total else 0}"
        )
        out.append(f"!{body}*{nmea_checksum(body)}")
    return out


class NmeaUdpFeed:
    """Sends sentences to host:port as UDP datagrams (one sentence each, CRLF
    terminated). UDP never blocks, so a missing listener cannot back up msdr."""

    def __init__(self, host: str = "127.0.0.1", port: int = 10110) -> None:
        self.host = host
        self.port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._seq = 0
        self.sentences_sent = 0
        self.last_error: str | None = None

    @property
    def active(self) -> bool:
        return self._transport is not None

    async def start(self) -> None:
        if self._transport is not None:
            return
        try:
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(
                _Protocol, remote_addr=(self.host, self.port)
            )
            self.last_error = None
        except OSError as exc:
            self.last_error = f"cannot send to {self.host}:{self.port}: {exc}"
            logger.warning("nmea feed: %s", self.last_error)

    async def stop(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def publish_frame(self, data: bytes) -> None:
        if self._transport is None:
            return
        seq = self._seq
        self._seq = (self._seq + 1) % 10
        try:
            for sentence in format_aivdm(data, seq_id=seq):
                self._transport.sendto((sentence + "\r\n").encode("ascii"))
                self.sentences_sent += 1
        except OSError as exc:
            self.last_error = f"send failed: {exc}"

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "host": self.host,
            "port": self.port,
            "sent": self.sentences_sent,
            "last_error": self.last_error,
        }


class _Protocol(asyncio.DatagramProtocol):
    def error_received(self, exc: Exception) -> None:
        # ICMP "port unreachable" when nothing listens; harmless, surfaced via status.
        logger.debug("nmea feed: %s", exc)
