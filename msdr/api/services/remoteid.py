"""Live Remote ID (docs/remote-id.md "Live"): the API-facing mixin over one
`msdr.remoteid.tracker.Tracker` shared by both receivers.

AppService mixin: methods here assume the attributes this module's
`_build_remoteid`/`_start_remoteid_runtime` set up, plus `self.wifi`/`self.ble`
(WifiServiceMixin/BleServiceMixin) and `self.broadcaster` (msdr.api.service).

The tracker is fed inline by `NetworkTable`/`AdvertiserTable` themselves
(msdr/wifi/observations.py, msdr/ble/observations.py) through their optional
`.remoteid` sink attribute -- exactly the `self.table.history = self.history`
pattern `HistoryServiceMixin._sync_history_sinks` already uses for device
history. This mixin owns the `Tracker`, re-attaches it to whichever table
objects the two supervisors currently hold (a Wi-Fi channel hop or a
reconnect replaces the `NetworkTable`/`AdvertiserTable` instance), and pushes
a coalescable "remoteid" websocket message at most once a second when the
tracked UAS set actually changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time

from fastapi import HTTPException

from msdr.remoteid.tracker import AddressTrack, Tracker, UasGroup

logger = logging.getLogger(__name__)

#: How often the tracker is re-synced to the current tables and checked for
#: a change to push -- independent of either receiver's own tick, since the
#: tracker has no supervisor of its own.
REMOTEID_TICK_S = 1.0

#: A group/track with no drone position newer than this is "stale" (matches
#: `Tracker.groups()`'s own default `stale_after_s`).
STALE_AFTER_S = 60.0

#: Recent messages returned by GET /api/remoteid/uas/{key}.
RECENT_MESSAGES_LIMIT = 50


def _with_freshness(pos: dict | None, now: float) -> dict | None:
    if pos is None:
        return None
    age_s = now - pos["received_at"]
    return {**pos, "age_s": age_s, "stale": age_s > STALE_AFTER_S}


def _position_dict(fix) -> dict | None:
    if fix is None:
        return None
    return {
        "latitude_deg": fix.latitude_deg,
        "longitude_deg": fix.longitude_deg,
        "altitude_m": fix.altitude_m,
        "received_at": fix.received_at,
    }


def _drone_position_dict(fix) -> dict | None:
    if fix is None or not fix.has_position:
        return None
    return {
        "status": fix.status,
        "height_type": fix.height_type,
        "direction_deg": fix.direction_deg,
        "speed_horizontal_ms": fix.speed_horizontal_ms,
        "speed_vertical_ms": fix.speed_vertical_ms,
        "latitude_deg": fix.latitude_deg,
        "longitude_deg": fix.longitude_deg,
        "altitude_baro_m": fix.altitude_baro_m,
        "altitude_geo_m": fix.altitude_geo_m,
        "height_m": fix.height_m,
        "horiz_accuracy_m": fix.horiz_accuracy_m,
        "vert_accuracy_m": fix.vert_accuracy_m,
        "reported_timestamp_s": fix.reported_timestamp_s,
        "received_at": fix.received_at,
    }


def _track_summary(track: AddressTrack, now: float) -> dict:
    transport = "wifi" if track.address.startswith("wifi:") else (
        "ble" if track.address.startswith("ble:") else None
    )
    return {
        "address": track.address,
        "transport": transport,
        "sources": sorted(track.sources),
        "uas_id_hex": track.uas_id.hex() if track.uas_id is not None else None,
        "uas_id_text": track.uas_id_text,
        "ua_type": track.ua_type,
        "self_id": track.self_id,
        "operator_id": track.operator_id,
        "drone_position": _with_freshness(_drone_position_dict(track.drone_position), now),
        "operator_position": _with_freshness(_position_dict(track.operator_position), now),
        "takeoff_position": _with_freshness(_position_dict(track.takeoff_position), now),
        "last_seen": track.last_seen,
        "age_s": now - track.last_seen,
        "stale": track.is_stale(now, STALE_AFTER_S),
    }


def _freshest(fixes) -> object | None:
    present = [f for f in fixes if f is not None]
    return max(present, key=lambda f: f.received_at, default=None)


def _first_truthy(values):
    for v in values:
        if v:
            return v
    return None


def _group_summary(group: UasGroup, now: float) -> dict:
    tracks = [_track_summary(t, now) for t in group.tracks]
    sources = sorted({s for t in group.tracks for s in t.sources})
    drone = _freshest(
        t.drone_position for t in group.tracks
        if t.drone_position is not None and t.drone_position.has_position
    )
    operator = _freshest(t.operator_position for t in group.tracks)
    takeoff = _freshest(t.takeoff_position for t in group.tracks)
    last_seen = max((t.last_seen for t in group.tracks), default=0.0)
    return {
        "key": group.key,
        "uas_id_hex": group.uas_id.hex() if group.uas_id is not None else None,
        "uas_id_text": _first_truthy(t.uas_id_text for t in group.tracks),
        "ua_type": _first_truthy(t.ua_type for t in group.tracks),
        "self_id": _first_truthy(t.self_id for t in group.tracks),
        "operator_id": _first_truthy(t.operator_id for t in group.tracks),
        "conflict": group.conflict,
        "sources": sources,
        "last_seen": last_seen,
        "age_s": now - last_seen,
        "stale": all(t.is_stale(now, STALE_AFTER_S) for t in group.tracks),
        "drone_position": _with_freshness(_drone_position_dict(drone), now),
        "operator_position": _with_freshness(_position_dict(operator), now),
        "takeoff_position": _with_freshness(_position_dict(takeoff), now),
        "tracks": tracks,
    }


def _message_dict(received_at: float, msg) -> dict:
    fields = dataclasses.asdict(msg)
    # IDs and authentication data may be arbitrary binary, never UTF-8.
    return {"received_at": received_at, **{
        key: value.hex() if isinstance(value, bytes) else value
        for key, value in fields.items()
    }}


class RemoteidServiceMixin:
    def _build_remoteid(self) -> None:
        self.remoteid_tracker = Tracker()
        self._remoteid_task: asyncio.Task | None = None
        self._remoteid_last_signature: dict[str, float] = {}

    def _sync_remoteid_sinks(self) -> None:
        """Point both tables at the live tracker. Re-run every tick because a
        Wi-Fi channel hop or a reconnect replaces the table object (same
        reasoning as `HistoryServiceMixin._sync_history_sinks`)."""
        for sup in (self.wifi, self.ble):
            if sup.table is not None:
                sup.table.remoteid = self.remoteid_tracker

    async def _start_remoteid_runtime(self) -> None:
        self._sync_remoteid_sinks()
        self._remoteid_task = asyncio.create_task(self._remoteid_loop())

    async def _stop_remoteid_runtime(self) -> None:
        task, self._remoteid_task = self._remoteid_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _remoteid_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(REMOTEID_TICK_S)
                try:
                    self._sync_remoteid_sinks()
                    await self._maybe_publish_remoteid()
                except Exception:
                    logger.exception("remoteid: tick failed")
        except asyncio.CancelledError:
            raise

    async def _maybe_publish_remoteid(self) -> None:
        now = time.time()
        groups = self.remoteid_tracker.groups(now=now, stale_after_s=STALE_AFTER_S)
        signature = {g.key: max((t.last_seen for t in g.tracks), default=0.0) for g in groups}
        if signature == self._remoteid_last_signature:
            return
        self._remoteid_last_signature = signature
        await self.broadcaster.publish(
            {
                "type": "remoteid",
                "now": now,
                "items": [_group_summary(g, now) for g in groups],
            }
        )

    # --- read API -----------------------------------------------------------

    def remoteid_live(self) -> dict:
        now = time.time()
        groups = self.remoteid_tracker.groups(now=now, stale_after_s=STALE_AFTER_S)
        return {"now": now, "items": [_group_summary(g, now) for g in groups]}

    def remoteid_uas_details(self, key: str) -> dict:
        now = time.time()
        groups = self.remoteid_tracker.groups(now=now, stale_after_s=STALE_AFTER_S)
        group = next((g for g in groups if g.key == key), None)
        if group is None:
            raise HTTPException(404, f"UAS not found: {key!r}")
        messages_all = [
            (ts, msg) for t in group.tracks for ts, msg in t.recent_messages
        ]
        messages_all.sort(key=lambda pair: pair[0])
        recent_messages = [
            _message_dict(ts, msg) for ts, msg in messages_all[-RECENT_MESSAGES_LIMIT:]
        ]
        return {**_group_summary(group, now), "recent_messages": recent_messages}
