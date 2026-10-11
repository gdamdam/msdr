"""Passive BLE reception (raw HCI dongle, docs/ble.md): the API-facing mixin
over `msdr.ble.supervisor.BleSupervisor`.

AppService mixin: methods moved here mirror the shape of WifiServiceMixin
(msdr/api/services/wifi.py) -- all attributes they use are initialised in
AppService.__init__. There is no sessions/recording/import here: BLE keeps
nothing beyond the current in-memory table (docs/ble.md: "Nothing is
persisted... there is no recording in this release.").
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import HTTPException

from msdr.ble import sig as ble_sig
from msdr.ble.supervisor import BleSupervisor
from msdr.settings import Settings
from msdr.tasks import spawn

# `AdvertiserTable.details()` is a superset of `.summary()` (docs/ble.md
# "Row shape" vs "Details add") -- trimmed down to the row shape for the
# websocket `changed` list, since `snapshot_changed_since` only hands back
# addresses, not summary dicts.
_SUMMARY_FIELDS = (
    "addr",
    "addr_type",
    "identity",
    "name",
    "company_id",
    "company",
    "connectable",
    "service_uuids",
    "remote_id",
    "rssi",
    "reports",
    "first_seen",
    "last_seen",
    "evt_types",
    "phy",
    "channel",
    "extended",
    "sensor",
    "service_names",
    "apple",
)


def _as_summary(d: dict) -> dict:
    return {k: d[k] for k in _SUMMARY_FIELDS}


def ble_sig_path(settings: Settings) -> Path | None:
    """Where downloaded Bluetooth SIG names live (docs/ble.md "Bluetooth SIG
    names"): next to msdr.db; None for an in-memory database (tests)."""
    if str(settings.db_path) == ":memory:":
        return None
    return Path(settings.db_path).parent / "ble" / "sig_assigned_numbers.json"


class BleServiceMixin:
    def _build_ble(self, settings: Settings) -> BleSupervisor:
        path = ble_sig_path(settings)
        if path is not None:
            ble_sig.load(path)
        else:
            ble_sig.set_names(None)
        return BleSupervisor(
            helper_command=settings.ble.helper_command,
            usb_id=settings.ble.usb_id,
            backend=settings.ble.backend,
            serial_port=settings.ble.serial_port,
            baud=settings.ble.baud,
            phy=settings.ble.phy,
            channel=settings.ble.channel,
            on_status=self._on_ble_status,
        )

    def _require_ble_action_enabled(self) -> None:
        # Only the route that actually starts the receiver refuses while the
        # feature is off (docs/ble.md): every read (status, devices,
        # advertisers, live, details) works regardless -- the Setup card
        # needs status/devices to work while disabled to show
        # "stopped"/enabled: false in the first place.
        if not self.settings.ble.enabled:
            raise HTTPException(409, "Enable BLE in Setup first")

    # --- status / websocket -------------------------------------------------

    def _on_ble_status(self, status: dict) -> None:
        """Called synchronously from BleSupervisor (reader/tick tasks) --
        never awaited there, so the actual publish is scheduled instead."""
        spawn(self._publish_ble_status())

    async def _publish_ble_status(self) -> None:
        now = time.time()
        changed = self._ble_changed_summaries(self._ble_last_push)
        self._ble_last_push = now
        await self.broadcaster.publish(
            {"type": "ble", "status": self.ble_status(), "changed": changed}
        )

    def _ble_changed_summaries(self, since: float) -> list[dict]:
        table = self.ble.table
        if table is None:
            return []
        out = []
        for addr in table.snapshot_changed_since(since):
            d = table.details(addr)
            if d is None:
                continue
            out.append(_as_summary(d))
        return out

    def ble_status(self) -> dict:
        base = self.ble.status()
        return {
            **base,
            "enabled": self.settings.ble.enabled,
            "device": self._ble_last_device,
        }

    # --- Bluetooth SIG names (msdr/ble/sig.py) ------------------------------

    def ble_names_status(self) -> dict:
        names = ble_sig.names()
        return {
            "downloaded": names is not None,
            **(names.summary() if names else {}),
            "source": ble_sig.SOURCE_BASE,
        }

    async def ble_names_refresh(self) -> dict:
        path = ble_sig_path(self.settings)
        if path is None:
            raise HTTPException(409, "no data folder (in-memory database)")
        try:
            await asyncio.to_thread(ble_sig.refresh, path)
        except Exception as exc:  # network, HTTP or YAML -- keep the old file
            raise HTTPException(502, f"download failed: {exc}") from exc
        return self.ble_names_status()

    async def ble_devices(self) -> dict:
        result = await self.ble.devices()
        rows = result.get("devices") or []
        if rows:
            d = rows[0]
            self._ble_last_device = {
                "product": d.get("product"),
                "serial": d.get("serial"),
                "bus": d.get("bus"),
                "address": d.get("address"),
            }
        return result

    async def _apply_pending_ble_settings(self) -> None:
        # Cleared only after the stop/rebuild awaits; without the lock a second
        # caller would start the old supervisor and then lose it to the rebuild.
        async with self._ble_settings_lock:
            await self._apply_pending_ble_settings_locked()

    async def _apply_pending_ble_settings_locked(self) -> None:
        if not getattr(self, "_ble_settings_pending", False):
            return
        await self.ble.stop()
        self.ble = self._build_ble(self.settings)
        self._ble_settings_pending = False
        self._sync_history_sinks()

    async def ble_start(self) -> dict:
        self._require_ble_action_enabled()
        await self._apply_pending_ble_settings()
        await self.ble.start()
        return self.ble_status()

    async def ble_stop(self) -> dict:
        await self.ble.stop()
        return self.ble_status()

    # --- advertisers / live feed ---------------------------------------------

    def ble_device_summary(self, window_s: float = 60.0) -> dict:
        """Counts by device type, last `window_s` (docs/ble.md "Device
        summary") -- never per device, never activity."""
        table = self.ble.table
        if table is None:
            return {"window_s": window_s, "total": 0, "groups": []}
        return table.device_summary(time.time(), window_s=window_s)

    def ble_advertisers(
        self,
        *,
        q: str | None = None,
        offset: int = 0,
        limit: int = 100,
        sort: str = "first_seen",
        order: str = "asc",
    ) -> dict:
        table = self.ble.table
        if table is None:
            return {"items": [], "total": 0, "since": None}
        display_names = None
        if sort == "device" and self.labels is not None:
            display_names = {lab["ident"]: lab["name"] for lab in self.labels.list("ble")}
        rows, total = table.rows(
            q=q,
            offset=offset,
            limit=min(limit, 500),
            sort=sort,
            order=order,
            display_names=display_names,
        )
        return {"items": rows, "total": total, "since": table.since()}

    def ble_clear_advertisers(self) -> dict:
        """Empty the live advertiser table; capture continues."""
        if self.ble.table is not None:
            self.ble.table.clear()
        return {"ok": True}

    def ble_live(self, *, after: int = 0, limit: int = 200) -> dict:
        table = self.ble.table
        if table is None:
            return {"items": [], "seq": 0, "maxlen": 0}
        return table.live_since(after=after, limit=min(limit, 500))

    def ble_advertiser_details(self, addr: str) -> dict:
        table = self.ble.table
        details = table.details(addr) if table is not None else None
        if details is None:
            raise HTTPException(404, f"advertiser not found: {addr!r}")
        return details
