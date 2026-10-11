# SPDX-License-Identifier: GPL-3.0-only
"""Per-UAS aggregation of decoded Remote ID messages (docs/remote-id.md).
Pure in-memory bookkeeping: no storage or network I/O, no persistence
through `DecodeEventRepo` in this phase.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

from msdr.remoteid import messages

#: Recent decoded messages kept per address, newest last, for the live API's
#: "recent messages" detail (docs/remote-id.md "Live") -- not a full replay
#: log, just enough for an operator to see what has actually been received.
RECENT_MESSAGES_MAXLEN = 20

#: ODID_OPERATOR_LOCATION_TYPE_LIVE_GNSS (opendroneid.h): the only
#: OperatorLocationType value that means "this is the operator's own
#: position"; TAKEOFF (0) and FIXED (2) both mean "this is the takeoff or
#: reference position" and are tracked together as `takeoff_position`.
_OPERATOR_LOCATION_LIVE_GNSS = 1

#: Address tracks kept; past this the least-recently-heard is dropped so
#: rotating/spoofed addresses cannot grow the table without bound.
MAX_TRACKS = 1000

_EARTH_RADIUS_M = 6_371_000.0


def _valid_fix(lat: float | None, lon: float | None) -> bool:
    # lat/lon decode from int32/1e7 (~+-214 deg), so a hostile or corrupt
    # message can carry values no real position has. Written to also reject NaN.
    return lat is not None and lon is not None and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    # Rounding can push `a` a hair past 1 (antipodes); asin would raise.
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, max(0.0, a))))


@dataclass(frozen=True)
class PositionFix:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float | None
    received_at: float


@dataclass(frozen=True)
class LocationFix:
    status: int
    height_type: int
    direction_deg: float | None
    speed_horizontal_ms: float | None
    speed_vertical_ms: float | None
    latitude_deg: float | None
    longitude_deg: float | None
    altitude_baro_m: float | None
    altitude_geo_m: float | None
    height_m: float | None
    horiz_accuracy_m: float | None  # decoded worst-case accuracy, never the raw enum
    vert_accuracy_m: float | None
    reported_timestamp_s: float | None  # Location.timestamp_s: wraps hourly, informational only
    received_at: float

    @property
    def has_position(self) -> bool:
        return self.latitude_deg is not None and self.longitude_deg is not None


@dataclass(frozen=True)
class SystemMeta:
    operator_location_type: int
    classification_type: int
    area_count: int
    area_radius_m: int
    area_ceiling_m: float | None
    area_floor_m: float | None
    category_eu: int
    class_eu: int
    timestamp_s_since_2019: int
    received_at: float


@dataclass
class AddressTrack:
    """Everything heard from one physical transport address (a Wi-Fi MAC or
    BLE address) -- the session-scoped key used before a Basic ID message
    pins down a UAS ID, and still the unit `Tracker.groups()` correlates by
    UAS ID afterward."""

    address: str
    uas_id: bytes | None = None
    uas_id_text: str | None = None
    ua_type: int | None = None
    basic_id_received_at: float | None = None
    drone_position: LocationFix | None = None
    operator_position: PositionFix | None = None
    takeoff_position: PositionFix | None = None
    self_id: str | None = None
    self_id_received_at: float | None = None
    operator_id: str | None = None
    operator_id_received_at: float | None = None
    system_meta: SystemMeta | None = None
    sources: set[str] = field(default_factory=set)
    last_seen: float = 0.0
    # (received_at, message) pairs, newest last, bounded -- only messages that
    # actually decoded (never a RemoteIdError; see Tracker._apply).
    recent_messages: deque[tuple[float, messages.DecodedMessage]] = field(
        default_factory=lambda: deque(maxlen=RECENT_MESSAGES_MAXLEN), repr=False
    )
    # (message_type, index within the pack) -> counter: a pack can carry several
    # messages of one type (e.g. two Basic IDs) under a single counter.
    _last_counter: dict[tuple[int, int], int] = field(default_factory=dict, repr=False)

    def is_stale(self, now: float, max_age_s: float) -> bool:
        """True if there is no drone position, or it is older than
        `max_age_s`. Staleness is always judged against the receiver's own
        clock (`received_at`, from `received_at=`/pcap timestamps), never
        `Location.timestamp_s` -- that field wraps every hour and carries no
        date, so it cannot tell "stale" from "the hour rolled over"."""
        if self.drone_position is None:
            return True
        return (now - self.drone_position.received_at) > max_age_s


@dataclass(frozen=True)
class UasGroup:
    """One or more `AddressTrack`s believed to be the same aircraft
    (identical Basic ID UAS ID -- the only signal used for this, never
    timing or RSSI), or a single track with no UAS ID seen yet."""

    key: str  # UAS ID hex, or the lone address when no UAS ID is known yet
    uas_id: bytes | None
    tracks: tuple[AddressTrack, ...]
    conflict: bool  # True: tracks disagree enough to be two drones sharing an id


def _group_conflict(
    tracks: tuple[AddressTrack, ...], now: float, stale_after_s: float, conflict_distance_m: float
) -> bool:
    fresh = [
        t.drone_position
        for t in tracks
        if t.drone_position is not None
        and t.drone_position.has_position
        and not t.is_stale(now, stale_after_s)
    ]
    for i in range(len(fresh)):
        for j in range(i + 1, len(fresh)):
            a, b = fresh[i], fresh[j]
            distance = _haversine_m(
                a.latitude_deg, a.longitude_deg, b.latitude_deg, b.longitude_deg
            )
            if distance > conflict_distance_m:
                return True
    return False


class Tracker:
    """Aggregates decoded Wi-Fi Message Packs and BLE messages per physical
    address, then groups by UAS ID. Never raises on decode errors -- callers
    are expected to have already separated out `messages.RemoteIdError`."""

    def __init__(self, *, conflict_distance_m: float = 5000.0) -> None:
        self.conflict_distance_m = conflict_distance_m
        self._tracks: dict[str, AddressTrack] = {}

    def _track(self, address: str) -> AddressTrack:
        track = self._tracks.get(address)
        if track is None:
            track = AddressTrack(address=address)
            self._tracks[address] = track
            if len(self._tracks) > MAX_TRACKS:
                oldest = min(
                    (a for a in self._tracks if a != address),
                    key=lambda a: self._tracks[a].last_seen,
                )
                del self._tracks[oldest]
        return track

    def ingest_wifi_pack(
        self,
        address: str,
        counter: int,
        pack: messages.MessagePack,
        *,
        received_at: float | None = None,
    ) -> None:
        received_at = time.time() if received_at is None else received_at
        track = self._track(address)
        track.sources.add("wifi")
        track.last_seen = received_at
        for index, msg in enumerate(pack.messages):
            self._apply(track, msg, counter, received_at, index)

    def ingest_ble_message(
        self,
        address: str,
        counter: int,
        message: messages.DecodedMessage | messages.MessagePack,
        *,
        received_at: float | None = None,
    ) -> None:
        received_at = time.time() if received_at is None else received_at
        track = self._track(address)
        track.sources.add("ble")
        track.last_seen = received_at
        if isinstance(message, messages.MessagePack):
            for index, msg in enumerate(message.messages):
                self._apply(track, msg, counter, received_at, index)
        else:
            self._apply(track, message, counter, received_at)

    def _apply(
        self,
        track: AddressTrack,
        msg: messages.DecodedMessage,
        counter: int,
        received_at: float,
        index: int = 0,
    ) -> None:
        # Dedup: a repeated message-counter burst for this message type at
        # this address updates liveness (already done by the caller) but is
        # never reprocessed as new content (opendroneid.h: the counter need
        # not change for a repeated, identical transmission). Keyed by
        # (type, position in the pack) so a second message of the same type in
        # one pack (e.g. two Basic IDs) is not mistaken for a repeat.
        key = (msg.message_type, index)
        if track._last_counter.get(key) == counter:
            return
        track._last_counter[key] = counter
        track.recent_messages.append((received_at, msg))

        if isinstance(msg, messages.BasicId):
            track.uas_id = msg.uas_id_bytes
            track.uas_id_text = msg.uas_id_text
            track.ua_type = msg.ua_type
            track.basic_id_received_at = received_at
        elif isinstance(msg, messages.Location):
            lat, lon = msg.latitude_deg, msg.longitude_deg
            if not _valid_fix(lat, lon):
                lat = lon = None  # keep the rest of the fix; drop the impossible position
            track.drone_position = LocationFix(
                status=msg.status,
                height_type=msg.height_type,
                direction_deg=msg.direction_deg,
                speed_horizontal_ms=msg.speed_horizontal_ms,
                speed_vertical_ms=msg.speed_vertical_ms,
                latitude_deg=lat,
                longitude_deg=lon,
                altitude_baro_m=msg.altitude_baro_m,
                altitude_geo_m=msg.altitude_geo_m,
                height_m=msg.height_m,
                horiz_accuracy_m=messages.decode_horizontal_accuracy_m(msg.horiz_accuracy_enum),
                vert_accuracy_m=messages.decode_vertical_accuracy_m(msg.vert_accuracy_enum),
                reported_timestamp_s=msg.timestamp_s,
                received_at=received_at,
            )
        elif isinstance(msg, messages.System):
            track.system_meta = SystemMeta(
                operator_location_type=msg.operator_location_type,
                classification_type=msg.classification_type,
                area_count=msg.area_count,
                area_radius_m=msg.area_radius_m,
                area_ceiling_m=msg.area_ceiling_m,
                area_floor_m=msg.area_floor_m,
                category_eu=msg.category_eu,
                class_eu=msg.class_eu,
                timestamp_s_since_2019=msg.timestamp_s_since_2019,
                received_at=received_at,
            )
            if _valid_fix(msg.operator_latitude_deg, msg.operator_longitude_deg):
                fix = PositionFix(
                    latitude_deg=msg.operator_latitude_deg,
                    longitude_deg=msg.operator_longitude_deg,
                    altitude_m=msg.operator_altitude_geo_m,
                    received_at=received_at,
                )
                if msg.operator_location_type == _OPERATOR_LOCATION_LIVE_GNSS:
                    track.operator_position = fix
                else:
                    track.takeoff_position = fix
        elif isinstance(msg, messages.SelfId):
            track.self_id = msg.description
            track.self_id_received_at = received_at
        elif isinstance(msg, messages.OperatorId):
            track.operator_id = msg.operator_id
            track.operator_id_received_at = received_at
        # messages.Authentication carries no position/display metadata this
        # phase tracks; it decodes through messages.py but isn't aggregated.

    def groups(self, *, now: float | None = None, stale_after_s: float = 60.0) -> list[UasGroup]:
        now = time.time() if now is None else now
        by_uas: dict[bytes, list[AddressTrack]] = {}
        solo: list[AddressTrack] = []
        for track in self._tracks.values():
            if track.uas_id is not None:
                by_uas.setdefault(track.uas_id, []).append(track)
            else:
                solo.append(track)

        out: list[UasGroup] = []
        for uas_id, tracks in by_uas.items():
            tracks_t = tuple(tracks)
            conflict = _group_conflict(tracks_t, now, stale_after_s, self.conflict_distance_m)
            out.append(
                UasGroup(key=uas_id.hex(), uas_id=uas_id, tracks=tracks_t, conflict=conflict)
            )
        for track in solo:
            out.append(UasGroup(key=track.address, uas_id=None, tracks=(track,), conflict=False))
        return out
