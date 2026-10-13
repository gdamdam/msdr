"""Fans decoder events out to whichever outbound feeds are enabled."""

from __future__ import annotations

import logging
import time
from typing import Any

from msdr.feeds.nmea import NmeaUdpFeed
from msdr.feeds.sbs import SbsServer, format_sbs
from msdr.settings import FeedsSettings

logger = logging.getLogger(__name__)


class FeedsRuntime:
    def __init__(self, settings: FeedsSettings) -> None:
        self.settings = settings
        self._sbs: SbsServer | None = None
        self._nmea: NmeaUdpFeed | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self._apply(None)

    async def stop(self) -> None:
        self._running = False
        await self._stop_sbs()
        await self._stop_nmea()

    async def configure(self, settings: FeedsSettings) -> None:
        old, self.settings = self.settings, settings
        if self._running:
            await self._apply(old)

    def on_message(self, msg: dict[str, Any]) -> None:
        """Called with every decoder websocket message; never raises, since it
        runs inside the decoder's synchronous publish path."""
        if msg.get("type") != "decoder_event" or (self._sbs is None and self._nmea is None):
            return
        event = msg.get("event") or {}
        if event.get("integrity") == "failed":
            return
        try:
            decoder = event.get("decoder")
            fields = event.get("fields") or {}
            if decoder == "adsb" and self._sbs is not None:
                line = format_sbs(fields, float(event.get("ts") or time.time()))
                if line:
                    self._sbs.publish(line)
            elif decoder == "ais" and self._nmea is not None and fields.get("payload_hex"):
                self._nmea.publish_frame(bytes.fromhex(fields["payload_hex"]))
        except Exception:
            logger.exception("feeds: failed to forward decoder event")

    def status(self) -> dict[str, Any]:
        s = self.settings
        sbs = self._sbs.status() if self._sbs else {"listening": False, "clients": 0, "sent": 0,
                                                    "last_error": None}
        nmea = self._nmea.status() if self._nmea else {"active": False, "sent": 0,
                                                       "last_error": None}
        # Configured values win over the live feed's, so the UI shows what is saved
        # even while a feed is off.
        return {
            "sbs": {**sbs, "enabled": s.sbs_enabled, "host": s.sbs_host, "port": s.sbs_port},
            "nmea": {**nmea, "enabled": s.nmea_enabled, "host": s.nmea_host, "port": s.nmea_port},
        }

    # -- internals ---------------------------------------------------------

    async def _apply(self, old: FeedsSettings | None) -> None:
        s = self.settings
        if self._sbs is not None and (
            not s.sbs_enabled or (old and (old.sbs_host, old.sbs_port) != (s.sbs_host, s.sbs_port))
        ):
            await self._stop_sbs()
        if s.sbs_enabled and self._sbs is None:
            self._sbs = SbsServer(s.sbs_host, s.sbs_port)
            await self._sbs.start()
        if self._nmea is not None and (
            not s.nmea_enabled
            or (old and (old.nmea_host, old.nmea_port) != (s.nmea_host, s.nmea_port))
        ):
            await self._stop_nmea()
        if s.nmea_enabled and self._nmea is None:
            self._nmea = NmeaUdpFeed(s.nmea_host, s.nmea_port)
            await self._nmea.start()

    async def _stop_sbs(self) -> None:
        feed, self._sbs = self._sbs, None
        if feed is not None:
            await feed.stop()

    async def _stop_nmea(self) -> None:
        feed, self._nmea = self._nmea, None
        if feed is not None:
            await feed.stop()
