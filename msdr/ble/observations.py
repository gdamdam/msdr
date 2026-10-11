"""`AdvertiserTable`: what the BLE dongle heard, per advertiser address, plus
a bounded live feed of individual reports (docs/ble.md "msdr side").

Random addresses are session identities only: an `rpa`/`nrpa` entry is
whatever one rotating address did while it lasted, never a device.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, deque

from msdr import oui
from msdr.ble import adv
from msdr.ble import apple as ble_apple
from msdr.ble import sensors as ble_sensors
from msdr.remoteid import messages as remoteid_messages
from msdr.remoteid import transport as remoteid_transport

DEFAULT_MAX_ENTRIES = 2000
RSSI_MAXLEN = 60
PAYLOAD_MAXLEN = 8
RECENT_MAXLEN = 20
LIVE_FEED_MAXLEN = 500
SENSOR_HISTORY_MAXLEN = 20
# Per-address caps: a spoofed/rotating-payload advertiser must not grow these forever.
PER_ADDR_MAXLEN = 32

CONNECTABLE_EVENTS = frozenset({"ADV_IND", "ADV_DIRECT_IND"})


class AdvertiserEntry:
    def __init__(self, addr: str, addr_type: str, first_seen: float) -> None:
        self.addr = addr
        self.addr_type = addr_type
        self.identity = adv.classify_address(addr, addr_type)
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.updated_at = first_seen
        self.reports = 0
        self.evt_types: Counter[str] = Counter()
        self.name: str | None = None
        self.name_complete: bool | None = None
        self.flags: int | None = None
        self.tx_power: int | None = None
        self.appearance: int | None = None
        self.service_uuids: list[str] = []
        self.service_data: dict[str, bytes] = {}
        self.manufacturer_data: dict[int, bytes] = {}
        self.company_id: int | None = None
        self.remote_id = False
        self.rssi: deque[tuple[float, int]] = deque(maxlen=RSSI_MAXLEN)
        self.payloads: OrderedDict[str, dict] = OrderedDict()
        self.recent: deque[dict] = deque(maxlen=RECENT_MAXLEN)
        # Sensor beacon decoding (docs/ble.md "Sensors", msdr/ble/sensors.py):
        # the latest decoded reading set, plus a short bounded history so the
        # UI can show a trend without the table growing unbounded.
        self.sensor: dict | None = None
        self.sensor_history: deque[dict] = deque(maxlen=SENSOR_HISTORY_MAXLEN)
        # Apple Continuity (msdr/ble/apple.py): message types seen from this
        # address (names only) and the latest AirPods/Beats model + battery.
        self.apple_types: dict[str, str | None] = {}
        self.apple_audio: dict | None = None
        # Sniffle backend only (docs/ble.md): last reported PHY/channel, and
        # whether any report from this advertiser used extended advertising.
        # Both stay None/False on the hci backend, which reports neither.
        self.phy: str | None = None
        self.channel: int | None = None
        self.extended = False

    @property
    def connectable(self) -> bool:
        return any(self.evt_types[e] for e in CONNECTABLE_EVENTS)

    @property
    def latest_rssi(self) -> int | None:
        return self.rssi[-1][1] if self.rssi else None

    @property
    def company(self) -> str | None:
        return adv.company_name(self.company_id)

    def observe(
        self,
        ts: float,
        evt: str,
        rssi: int | None,
        data: bytes,
        parsed: adv.Advertisement,
        *,
        phy: str | None = None,
        channel: int | None = None,
        extended: bool = False,
    ) -> None:
        self.reports += 1
        self.evt_types[evt] += 1
        self.last_seen = ts
        self.updated_at = ts
        if rssi is not None:
            self.rssi.append((ts, rssi))
        if phy is not None:
            self.phy = phy
        if channel is not None:
            self.channel = channel
        self.extended = self.extended or extended
        if parsed.name is not None and (parsed.name_complete or not self.name_complete):
            self.name, self.name_complete = parsed.name, parsed.name_complete
        if parsed.flags is not None:
            self.flags = parsed.flags
        if parsed.tx_power is not None:
            self.tx_power = parsed.tx_power
        if parsed.appearance is not None:
            self.appearance = parsed.appearance
        for u in parsed.service_uuids:
            if u not in self.service_uuids:
                self.service_uuids.append(u)
        del self.service_uuids[:-PER_ADDR_MAXLEN]
        # pop-then-set moves a refreshed key to the newest slot, so the
        # oldest-inserted key is the one evicted past the cap.
        for k, v in parsed.service_data.items():
            self.service_data.pop(k, None)
            self.service_data[k] = v
        while len(self.service_data) > PER_ADDR_MAXLEN:
            del self.service_data[next(iter(self.service_data))]
        for k, v in parsed.manufacturer_data.items():
            self.manufacturer_data.pop(k, None)
            self.manufacturer_data[k] = v
        while len(self.manufacturer_data) > PER_ADDR_MAXLEN:
            del self.manufacturer_data[next(iter(self.manufacturer_data))]
        if parsed.company_id is not None:
            self.company_id = parsed.company_id
        if parsed.remote_id:
            self.remote_id = True
        apple_data = parsed.manufacturer_data.get(ble_apple.APPLE_COMPANY)
        if apple_data is not None:
            continuity = ble_apple.parse_continuity(apple_data)
            if continuity is not None:
                for t in continuity["types"]:
                    self.apple_types[t["type"]] = t["name"]
                if continuity["audio"] is not None:
                    self.apple_audio = {**continuity["audio"], "ts": ts}
        report = ble_sensors.decode(parsed.service_data, parsed.manufacturer_data)
        if report is not None:
            entry = report.to_dict()
            entry["ts"] = ts
            self.sensor = entry
            self.sensor_history.append(entry)
        key = data.hex()
        slot = self.payloads.get(key)
        if slot is None:
            self.payloads[key] = {"hex": key, "first_seen": ts, "last_seen": ts, "count": 1}
            while len(self.payloads) > PAYLOAD_MAXLEN:
                oldest = min(self.payloads, key=lambda k: self.payloads[k]["last_seen"])
                del self.payloads[oldest]
        else:
            slot["last_seen"], slot["count"] = ts, slot["count"] + 1
        self.recent.append({"ts": ts, "evt": evt, "rssi": rssi, "length": len(data)})

    def summary(self) -> dict:
        return {
            "addr": self.addr,
            "addr_type": self.addr_type,
            "identity": self.identity,
            "name": self.name,
            "company_id": self.company_id,
            "company": self.company,
            "connectable": self.connectable,
            "service_uuids": list(self.service_uuids),
            "remote_id": self.remote_id,
            "rssi": self.latest_rssi,
            "reports": self.reports,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "evt_types": dict(self.evt_types),
            "phy": self.phy,
            "channel": self.channel,
            "extended": self.extended,
            "sensor": self.sensor,
            # Names for the services column: built-ins plus the downloaded
            # SIG list when present (msdr/ble/sig.py).
            "service_names": {
                u: n for u in self.service_uuids if (n := adv.service_name(u)) is not None
            },
            "apple": (
                {
                    "types": [{"type": t, "name": n} for t, n in self.apple_types.items()],
                    "audio": self.apple_audio,
                }
                if self.apple_types else None
            ),
        }

    def details(self) -> dict:
        out = self.summary()
        out.update(
            name_complete=self.name_complete,
            flags=self.flags,
            tx_power=self.tx_power,
            appearance=self.appearance,
            appearance_name=adv.appearance_name(self.appearance),
            # Only a public address comes from the IEEE registry; random
            # addresses (RPA/NRPA/static) never have a vendor.
            vendor=oui.vendor(self.addr) if self.addr_type == "public" else None,
            service_data={u: d.hex() for u, d in self.service_data.items()},
            manufacturer_data={str(cid): d.hex() for cid, d in self.manufacturer_data.items()},
            rssi_history=[[ts, v] for ts, v in self.rssi],
            payloads=list(self.payloads.values()),
            recent=list(self.recent),
            sensor_history=list(self.sensor_history),
        )
        return out


DRONES = "Drones (Remote ID)"
SENSORS = "Sensor beacons"
APPLE = "Apple"
UNKNOWN = "Unknown"
OTHER_MAKERS = "Other makers"

# Apple message type -> summary subgroup, highest priority first: a device
# advertising several types lands in the first that matches.
_APPLE_SUBGROUPS = (
    ({"0x07"}, "AirPods / Beats"),
    ({"0x10", "0x0c"}, "iPhone / iPad / Mac nearby"),
    ({"0x12"}, "Find My"),
    ({"0x06", "0x11"}, "HomeKit"),
    ({"0x09", "0x0a"}, "AirPlay"),
)


def device_category(entry: AdvertiserEntry) -> tuple[str, str | None]:
    """(group, subgroup) for the device summary -- each advertiser in exactly
    one. Uses only the kind of device (Remote ID, sensor format, Apple
    message types, maker), never a device's activity or state."""
    if entry.remote_id:
        return DRONES, None
    if entry.sensor is not None:
        return SENSORS, entry.sensor.get("protocol")
    if entry.apple_types or entry.company_id == ble_apple.APPLE_COMPANY:
        types = set(entry.apple_types)
        if entry.apple_audio is not None:
            return APPLE, "AirPods / Beats"
        for codes, label in _APPLE_SUBGROUPS:
            if types & codes:
                return APPLE, label
        return APPLE, "other"
    return (entry.company or UNKNOWN), None


def _rssi_key(e: AdvertiserEntry, display_names=None):
    return e.latest_rssi


#: Fixed privacy order for the Identity column: real/stable identities first,
#: then rotating ones (docs/ble.md "Identities and privacy").
_IDENTITY_ORDER = {"public": 0, "static": 1, "rpa": 2, "nrpa": 3, "random": 4}


def _device_key(e: AdvertiserEntry, display_names: dict[str, str] | None = None):
    """Display name sort: a user label beats the advertised name, which beats
    the company (same precedence as the UI's Device column)."""
    names = display_names or {}
    name = names.get(e.addr) or e.name or e.company
    return name.lower() if name else None


class AdvertiserTable:
    SORT_KEYS = {
        "first_seen": lambda e, display_names=None: e.first_seen,
        "last_seen": lambda e, display_names=None: e.last_seen,
        "addr": lambda e, display_names=None: e.addr,
        "name": lambda e, display_names=None: e.name.lower() if e.name else None,
        "rssi": _rssi_key,
        "reports": lambda e, display_names=None: e.reports,
        "company": lambda e, display_names=None: (e.company or "").lower() or None,
        "identity": (
            lambda e, display_names=None: _IDENTITY_ORDER.get(e.identity, len(_IDENTITY_ORDER))
        ),
        "services": lambda e, display_names=None: len(e.service_uuids) or None,
        "sensor": lambda e, display_names=None: e.sensor["protocol"] if e.sensor else None,
        "device": _device_key,
    }

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, AdvertiserEntry] = OrderedDict()
        self.evictions = 0
        self.parse_errors = 0
        self.malformed = 0
        self.live: deque[dict] = deque(maxlen=LIVE_FEED_MAXLEN)
        self.live_seq = 0
        # Device history (docs/history.md): set by the supervisor.
        self.history = None
        # Live Remote ID tracker (docs/remote-id.md "Live"): set by the
        # service, same sink pattern as `history` -- fed below, in `ingest`.
        self.remoteid = None

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        """Forget every advertiser and the counters; capture and the live
        feed continue."""
        self._entries.clear()
        self.evictions = 0
        self.parse_errors = 0
        self.malformed = 0

    def ingest(self, item: dict) -> AdvertiserEntry | None:
        """One helper report `{ts, addr, addr_type, evt, rssi, data(hex)}`.
        Returns the touched entry, or None (counted) when the line is not a
        usable report."""
        try:
            ts = float(item["ts"])
            addr = str(item["addr"]).lower()
            addr_type = "public" if item.get("addr_type") == "public" else "random"
            evt = str(item.get("evt") or "unknown")
            rssi = item.get("rssi")
            rssi = int(rssi) if rssi is not None else None
            data = bytes.fromhex(item.get("data") or "")
        except (KeyError, TypeError, ValueError, OverflowError):
            # OverflowError: int(inf) from a non-finite "rssi" (Infinity in JSON).
            self.parse_errors += 1
            return None
        # Sniffle backend only (docs/ble.md); absent (None/False) from hci reports.
        phy = item.get("phy")
        channel = item.get("channel")
        extended = bool(item.get("extended"))
        parsed = adv.parse(data)
        if parsed.malformed:
            self.malformed += 1
        entry = self._entries.get(addr)
        if entry is None:
            entry = AdvertiserEntry(addr, addr_type, ts)
            self._entries[addr] = entry
            self._evict()
        entry.observe(ts, evt, rssi, data, parsed, phy=phy, channel=channel, extended=extended)
        if self.history is not None:
            self.history.add(
                "ble", addr, ts, rssi, name=entry.name, kind=entry.identity, signal_kind="dbm"
            )
        if self.remoteid is not None:
            ble_rid = remoteid_transport.extract_ble_remote_id(parsed)
            if ble_rid is not None and not isinstance(
                ble_rid.message, remoteid_messages.RemoteIdError
            ):
                self.remoteid.ingest_ble_message(
                    f"ble:{addr}", ble_rid.message_counter, ble_rid.message, received_at=ts
                )
        self.live_seq += 1
        self.live.append(
            {
                "seq": self.live_seq,
                "ts": ts,
                "addr": addr,
                "identity": entry.identity,
                "evt": evt,
                "rssi": rssi,
                "name": parsed.name or entry.name,
                "company": adv.company_name(parsed.company_id) or entry.company,
                "length": len(data),
                "remote_id": parsed.remote_id,
                "sensor": entry.sensor,
                "phy": phy,
                "channel": channel,
                "extended": extended,
            }
        )
        return entry

    def _evict(self) -> None:
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.values(), key=lambda e: e.last_seen)
            del self._entries[oldest.addr]
            self.evictions += 1

    def device_summary(self, now: float, window_s: float = 60.0, top_makers: int = 6) -> dict:
        """Counts by device *type* for advertisers heard in the last
        `window_s` (docs/ble.md "Device summary"). No activity or state of
        any device is used -- only what kind of device it is. Approximate:
        a phone that rotates its address mid-window counts twice."""
        groups: dict[str, Counter] = {}
        totals: Counter = Counter()
        for entry in self._entries.values():
            if now - entry.last_seen > window_s:
                continue
            group, sub = device_category(entry)
            totals[group] += 1
            groups.setdefault(group, Counter())[sub] += 1
        fixed = {DRONES, SENSORS, APPLE, UNKNOWN}
        makers = [g for g, _ in totals.most_common() if g not in fixed]
        folded = makers[top_makers:]
        out = []
        for group, count in totals.most_common():
            if group in folded:
                continue
            subs = [
                {"label": label, "count": n}
                for label, n in groups[group].most_common() if label is not None
            ]
            out.append({"label": group, "count": count, "subgroups": subs})
        if folded:
            out.append({"label": OTHER_MAKERS, "count": sum(totals[g] for g in folded),
                        "subgroups": []})
        out.sort(key=lambda g: (g["label"] in (UNKNOWN, OTHER_MAKERS), -g["count"]))
        return {"window_s": window_s, "total": sum(totals.values()), "groups": out}

    def live_since(self, after: int = 0, limit: int = 200) -> dict:
        items = [e for e in self.live if e["seq"] > after]
        return {"items": items[-limit:], "seq": self.live_seq, "maxlen": LIVE_FEED_MAXLEN}

    def rows(
        self,
        q: str | None = None,
        offset: int = 0,
        limit: int = 100,
        sort: str = "first_seen",
        order: str = "asc",
        display_names: dict[str, str] | None = None,
    ) -> tuple[list[dict], int]:
        needle = q.lower() if q else None
        candidates = list(self._entries.values())
        if needle is not None:
            candidates = [
                e
                for e in candidates
                if needle in e.addr
                or (e.name and needle in e.name.lower())
                or (e.company and needle in e.company.lower())
                or any(needle in u for u in e.service_uuids)
            ]
        key = self.SORT_KEYS.get(sort, self.SORT_KEYS["first_seen"])
        # Same stable two-pass sort as the Wi-Fi table: address first, then
        # the column with None last, so rows never jitter between polls.
        candidates.sort(key=lambda e: e.addr)
        present = [e for e in candidates if key(e, display_names) is not None]
        missing = [e for e in candidates if key(e, display_names) is None]
        present.sort(key=lambda e: key(e, display_names), reverse=(order == "desc"))
        candidates = present + missing
        total = len(candidates)
        return [e.summary() for e in candidates[offset : offset + limit]], total

    def details(self, addr: str) -> dict | None:
        entry = self._entries.get(addr.lower())
        return entry.details() if entry is not None else None

    def snapshot_changed_since(self, t: float) -> list[str]:
        return [e.addr for e in self._entries.values() if e.updated_at > t]

    def since(self) -> float | None:
        return min((e.first_seen for e in self._entries.values()), default=None)
