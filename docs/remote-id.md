# Drone Remote ID decoding

Pure-Python decoder and live tracker for ASTM F3411 / ASD-STAN Open Drone ID messages, for operators and developers correlating drone broadcasts across the Wi-Fi (docs/wifi.md) and BLE (docs/ble.md) receivers.

Status: Phase 3 (offline) and Phase 4 (live + Map) of the "Wi-Fi and BLE receivers (Remote ID first)" TODO section are both implemented.

- **Offline:** a Wi-Fi PCAP session replayed through the decoder, `GET /api/wifi/sessions/{id}/remoteid`, also reachable from Review's "Remote ID" action on a saved Wi-Fi session.
- **Live:** both receivers feed one shared in-memory `Tracker`, exposed at `GET /api/remoteid/live` / `GET /api/remoteid/uas/{key}` and a coalescable "remoteid" websocket message, shown in Discover's "Remote ID" tab and as a Map layer — see "Live" and "Map" below.

## Provenance

- Message encode/decode ported from `opendroneid/opendroneid-core-c`
  (Apache-2.0, https://github.com/opendroneid/opendroneid-core-c) at commit
  `6484f26545d4f012682524e2d843fab0fbdc0b34`. Files consulted:
  `libopendroneid/opendroneid.c`/`.h` (Basic ID, Location/Vector,
  Authentication, Self-ID, System, Operator ID and Message Pack encode/decode;
  sentinel "unknown"/"invalid" encodings; accuracy enums) and
  `libopendroneid/wifi.c`/`odid_wifi.h` (the ASD-STAN vendor IE: OUI
  `FA:0B:BC`, OUI type `0x0D`, `struct ODID_service_info` = 1-byte message
  counter + `ODID_MessagePack_encoded`, carried in a Wi-Fi Beacon/probe
  response information element 221).
- msdr is GPL-3.0-only, which may incorporate Apache-2.0 code. New files carry
  `SPDX-License-Identifier: GPL-3.0-only` plus a header noting they are
  derived from opendroneid-core-c (Apache-2.0) and the commit above, per the
  Apache-2.0 attribution requirements; `THIRD_PARTY_NOTICES.md` carries the
  full notice. This is a hand-written Python port (not transliterated/
  copy-pasted C), cross-checked against independently generated test vectors
  (`tests/remoteid/data/vectors.json`, see its README for provenance) rather
  than against the C library's own in-repo test harness.
- BLE Bluetooth 4 legacy advertising framing (service data UUID `0xFFFA`,
  first byte application code `0x0D`, then a 1-byte message counter and one
  25-byte message) is not covered by the downloaded opendroneid-core-c files
  (no `bluetooth.c` was fetched); it follows the ASTM F3411 / ASD-STAN
  specification referenced in `opendroneid-core-c`'s own README and is
  consistent with `msdr/ble/adv.py`'s existing `REMOTE_ID_UUID`/
  `REMOTE_ID_APP_CODE` constants (0.59.0, docs/ble.md).

## Modules (`msdr/remoteid/`)

- `messages.py`: the 25-byte message codec, no I/O.
  - `decode_message(data: bytes) -> BasicId | Location | Authentication |
    SelfId | System | OperatorId | RemoteIdError`: dispatches on the high
    nibble of byte 0 (message type; low nibble is `protocol_version`, carried
    through but never gates decoding -- matching opendroneid's own policy of
    decoding newer protocol versions best-effort). Returns `RemoteIdError
    ("truncated"|"malformed"|"unsupported_type", detail)` for anything
    other than exactly 25 bytes, an out-of-range message type, or a
    type-specific validity failure (e.g. an Authentication page whose
    `Length` exceeds its declared page count); never raises.
  - `decode_message_pack(data: bytes) -> MessagePack | RemoteIdError`:
    decodes a `[type=0xF/version][single_size][count][messages...]` buffer
    (2 to 9 embedded messages per the spec's content limits -- at most 2
    Basic ID, 1 each of the rest). Per-message decode failures are collected
    in `MessagePack.errors` and reported, not hidden, matching the Location
    gate's "cases marked reference_decode: false are reported, not hidden"
    convention (`docs/lora-native.md`).
  - Every field with a spec-defined "no value"/"unknown" sentinel encoding
    (direction 361 deg, horizontal speed 255 m/s, vertical speed 63 m/s,
    latitude+longitude both exactly 0 deg, altitude/height -1000 m, timestamp
    `0xFFFF`) decodes to `None`, never to that sentinel number. Accuracy
    enums (`*_enum`, 0 = unknown) are kept as their raw 0-15 value *and*
    exposed as a decoded meters/seconds float that is `None` when the enum is
    0 -- opendroneid's own `decodeHorizontalAccuracy()` et al. return a
    worst-case *default* (e.g. 18520 m) for the unknown enum, which this
    port never surfaces as if it were a reported accuracy.
  - Units and resolution follow `opendroneid.h` exactly: lat/lon at 1e-7 deg
    (`int32 / 1e7`), altitude/height at 0.5 m steps with a -1000 m offset,
    horizontal speed at 0.25 m/s steps below 63.75 m/s and 0.75 m/s above (the
    multiplier bit), vertical speed at 0.5 m/s steps, `Location.timestamp_s`
    in tenths of a second since the top of the hour (0-3599.9, wraps every
    hour -- freshness is never derived from this field, see `tracker.py`).
- `transport.py`: pulls the message bytes out of an already-parsed Wi-Fi
  management frame or BLE advertisement; does not itself touch radiotap,
  pcap or HCI.
  - `extract_wifi_remote_id(frame: msdr.wifi.dot11.ManagementFrame) ->
    WifiRemoteId | None`: scans `frame.ies` for IE 221 (vendor-specific) with
    OUI `fa:0b:bc` and OUI type `0x0D`; decodes the trailing
    `ODID_service_info` (message counter + message pack) with
    `messages.decode_message_pack`.
  - `extract_ble_remote_id(adv: msdr.ble.adv.Advertisement) -> BleRemoteId |
    None`: reads `adv.service_data[adv.REMOTE_ID_UUID]`
    (`msdr/ble/adv.py` already flags `adv.remote_id` the same way); the first
    byte is the application code (checked against
    `msdr.ble.adv.REMOTE_ID_APP_CODE`), the second is the message counter,
    the rest is a 25-byte legacy message or a Message Pack, selected by
    its message type. The HCI backend carries legacy messages; Sniffle can
    also deliver extended advertisements containing packs.
  - `extract_ble_extended_remote_id(payload: bytes) -> BleRemoteId | None`:
    same service-data framing, but BLE 5 extended advertising's larger PDU
    carries a full message counter + Message Pack (decoded with
    `messages.decode_message_pack`) instead of one message. Live ingestion
    dispatches packs from Sniffle extended advertisements through the same
    service-data entry point.
  - `decode_wifi_records(records: Iterable[msdr.wifi.pcap.Record]) ->
    tracker.Tracker`: the offline entry point. Replays the same
    radiotap-parse / dot11-parse pipeline as `msdr/wifi/replay.py` and
    `NetworkTable.ingest`, feeding every decoded Remote ID report into a
    fresh `Tracker` with each record's own pcap timestamp (`record.ts`) as
    the receive time -- never wall-clock `time.time()` -- so offline decoding
    of the same capture is reproducible.
  - `decode_wifi_session(store: msdr.wifi.sessions.WifiSessionStore,
    session_id: str) -> tracker.Tracker`: reads a recorded/imported session's
    `part-*.pcap` files via `WifiSessionStore.session_records()` (a small
    addition to `sessions.py`, factored out of the loop already duplicated
    across `replay_table`/`import_pcap`/`export`) and decodes them.
- `tracker.py`: pure in-memory aggregation, no storage/network I/O.
  - Tracks one `AddressTrack` per physical transport address
    (`"wifi:<mac>"` / `"ble:<addr>"`) -- the session-scoped key the spec
    calls for before a Basic ID is seen. `Tracker.ingest_wifi_pack()` and
    `Tracker.ingest_ble_message()` each take an explicit `received_at`
    (defaults to `time.time()` for live use; offline replay always passes
    `record.ts`).
  - Fields are kept apart exactly as asked: `drone_position` (from Location:
    lat/lon/alt/status/speed/direction/height, one `LocationFix` with its own
    `received_at`), `operator_position` (System message when
    `OperatorLocationType == LIVE_GNSS`), `takeoff_position` (System message
    when `OperatorLocationType` is `TAKEOFF` or `FIXED`) -- the spec encodes
    which of the latter two a given System message updates in that one enum
    field, so the tracker branches on it rather than guessing. `self_id` and
    `operator_id` are metadata-only updates with their own freshness
    timestamps, never touching position freshness.
  - Dedup: per address and per message type, the last applied
    `message_counter` is remembered; a repeat of the same counter (a resent
    burst) updates `last_seen` only, never re-processed as new content,
    per the spec's note that the counter need not change for identical
    retransmissions.
  - Grouping/correlation: `Tracker.groups()` groups `AddressTrack`s by Basic
    ID UAS ID (raw 20-byte identifier, not the display string, so a binary
    UTM UUID and a short serial number are never confused) -- this is the
    *only* signal used to say "these two transports are the same aircraft",
    never timing or RSSI. A group with more than one address and
    simultaneously-fresh drone positions that disagree by more than
    `conflict_distance_m` (great-circle, default 5 km) is returned with
    `conflict=True` and its member tracks kept separate (never averaged or
    silently picked) -- two drones sharing an identifier stay visibly two
    drones.
  - `AddressTrack.is_stale(now, max_age_s)` / `Tracker.groups(stale_after_s=
    ...)` mark a `drone_position` stale by wall/pcap-clock age, independent
    of the wrapping, unitless `Location.timestamp_s` reported field.

## Live (`msdr/api/services/remoteid.py`)

- One `Tracker` (`AppService.remoteid_tracker`) shared by both receivers, built in `RemoteidServiceMixin._build_remoteid()`. It is fed inline, not through a queue:
  - Wi-Fi: `msdr/wifi/observations.py` (`NetworkTable._ingest`, the good-FCS path, right where the device-history hook `self.history.add(...)` sits) calls `transport.extract_wifi_remote_id(frame)` and, if the frame carries a valid (non-`RemoteIdError`) vendor IE, `tracker.ingest_wifi_pack()` keyed `"wifi:<bssid>"`.
  - BLE: `msdr/ble/observations.py` (`AdvertiserTable.ingest`, same place as its own `self.history.add(...)`) does the equivalent with `transport.extract_ble_remote_id()` and `tracker.ingest_ble_message()` keyed `"ble:<addr>"`.
  - Both tables get the tracker through an optional `.remoteid` sink attribute — exactly the `self.table.history = self.history` pattern already used for device history.
  - The sink is re-attached every tick by `RemoteidServiceMixin._sync_remoteid_sinks()` (mirrors `HistoryServiceMixin._sync_history_sinks()`) so a Wi-Fi channel hop or a BLE reconnect, both of which replace the table object, never silently stops feeding the tracker.
- `AddressTrack.recent_messages` (new, bounded to 20, newest last) keeps
  `(received_at, message)` pairs of every message that actually decoded (never
  a `RemoteIdError`) so `GET /api/remoteid/uas/{key}` can return "recent
  messages" without a separate persistence tier -- this is still in-memory
  only, not `DecodeEventRepo`, per the cross-cutting "three data tiers" rule.
- `LocationFix` now also carries `horiz_accuracy_m`/`vert_accuracy_m`, decoded
  from the Location message's accuracy enums with
  `messages.decode_horizontal_accuracy_m`/`decode_vertical_accuracy_m` (`None`
  for the "unknown" enum, same convention as every other accuracy field).
| Route | Purpose | Returns |
|---|---|---|
| `GET /api/remoteid/live` | Live tracked UAS | `{now, items: [...]}`, one item per `Tracker.groups()` group: id/UAS type, drone/operator/takeoff positions (each with `age_s`/`stale` computed against a 60 s threshold, `None` when never reported or not a valid position), transports (`sources`), last seen and `conflict` |
| `GET /api/remoteid/uas/{key}` | One UAS | The same group shape plus `recent_messages` (every track's recent messages merged, newest last, capped at 50); 404 if the key isn't currently tracked |

Binary fields (`uas_id_bytes`, `auth_data`) are hex strings in these JSON responses.

- A coalescable `{"type": "remoteid", "now", "items"}` websocket message
  (`_COALESCABLE_TYPES` in `msdr/api/service.py`) is published at most once a
  second, and only when the tracked UAS set's last-seen signature actually
  changed (`RemoteidServiceMixin._maybe_publish_remoteid`) -- same reasoning
  as the existing "wifi"/"ble" messages.
- Discover's "Remote ID" tab (`msdr/ui/components/remoteid-view.js`):
  - Polls `GET /api/remoteid/live` every second (self-contained, same pattern as `wifi-view.js`/`ble-view.js`).
  - Shows a compact live UAS table plus a details pane (positions, altitude/speed/heading/accuracy, recent messages).
- Review's "Wi-Fi sessions" panel gets a "Remote ID" row action that expands the existing offline decode (`GET /api/wifi/sessions/{id}/remoteid`) inline.

## Map (`msdr/ui/map-data.js`, `msdr/ui/components/discover-map.js`)

- `remoteIdRows(items)` turns each live group into up to three map rows --
  one per explicitly reported, valid position (`drone`/`operator`/`takeoff`;
  a position missing or decoded as unknown/invalid produces no row at all, so
  ordinary Wi-Fi/BLE devices with no Remote ID never appear on the map). Each
  row gets its own `trackId` (`remoteid:<key>:<kind>`) so `trackSegments()`
  keeps each position kind's trail independent and persistent.
- `discover-map.js` adds a "Drones (Remote ID)" layer:
  - Distinct marker shapes per kind (`.remoteid-marker.drone/operator/takeoff` in `styles.css` — diamond/circle/square, not color alone).
  - A thin dashed line connecting a UAS's simultaneously-reported positions (not a trail).
  - Trails computed with a tight 60 s gap threshold (`REMOTEID_TRAIL_GAP_S`, matching the live staleness threshold) instead of the default 1800 s used for aircraft/AIS — a stale position or a receiver reconnect always breaks the trail rather than drawing a straight line across the gap.
  - The hover card shows UAS ID, type, altitude (barometric and geodetic labelled separately), speed, heading, accuracy, transport and last-report age, always with "reported claim, not a verified location".
  - Selecting a marker opens a details pane via the existing bottom-panel mechanism (not the aircraft/maritime floating card, which stays aircraft/maritime-only).
- The layer polls `GET /api/remoteid/live` itself (2 s interval, `discover-map.js`'s own `_loadRemoteId`):
  - Independent of the decoder-event props the rest of the map uses.
  - Only on the Map workspace (not the Table view, which has no Remote ID row shape).
- "Clear map" hides it the same way it hides decoder events: by timestamp cutoff (`_mapRemoteIdGroups`), so a new report after clearing reappears on its own.

## Not in this phase

- Wi-Fi NAN is decoded (`transport.extract_nan_remote_id`, live through `NetworkTable._ingest_nan` and offline in `decode_wifi_records`):
  - A Public Action frame (0x04/0x09) with the Wi-Fi Alliance OUI 50:6F:9A type 0x13, whose Service Descriptor Attribute (0x03) carries service ID `88 69 19 9d 92 09` (first 48 bits of SHA-256 of "org.opendroneid.remoteid") and, in its service info, the same message counter + Message Pack as the Beacon IE.
  - Bad-FCS frames are skipped.
  - NAN discovery windows are on channel 6 (2.4 GHz) and 149 (5 GHz), so a fixed capture on 6 is the place to listen.
  - Fixture-tested only (synthetic frames around the opendroneid message-pack vectors); not yet verified against a real NAN broadcaster.
- No watch rules/geofence notifications on reported positions -- Phase 6.
- No hardware-tested live reception yet (fixture/unit-tested only in this
  repo); the Phase 4 acceptance's hardware tier (exact ACH/SONOFF units
  against a known Remote ID broadcaster) is still open.

## Validation gate (must pass before this phase is called done)

1. `tests/remoteid/test_messages.py` against `tests/remoteid/data/vectors.json`
   (independently generated by linking the real opendroneid-core-c sources,
   not derived from this port -- see that directory's README) plus the
   `test/test_inout.c` example values transcribed by hand: every message
   type, Message Pack, unknown/sentinel values, and boundary values (max
   lat/lon/altitude/speed, direction wrap at 360 deg, timestamp wrap at the
   hour) decode to the expected fields; truncated and malformed buffers
   return `RemoteIdError`, never raise.
2. `tests/remoteid/test_transport.py`: the Wi-Fi vendor IE and BLE legacy
   service-data extraction against both the generated beacon-frame vector and
   hand-built malformed/truncated cases; the BLE extended-advertising
   (Message Pack) path exercised directly since no capture backend reaches it
   yet.
3. `tests/remoteid/test_tracker.py`: malformed/truncated input, unknown
   fields, timestamp wrap at the hour, stale-position detection, duplicate
   message-counter bursts, and two drones transmitting the same UAS ID with
   diverging positions (conflict reported, not merged).
4. Offline decoding is reproducible: decoding the same PCAP session twice
   (direct `decode_wifi_records()` call and through
   `GET /api/wifi/sessions/{id}/remoteid`) yields identical output.
5. `tests/remoteid/test_live.py`: a trusted (good-FCS) Wi-Fi beacon/probe
   response carrying the vendor IE, and a BLE advertisement carrying the
   legacy service data, each reach a `Tracker` through the table's optional
   `.remoteid` sink and nowhere else (bad-FCS frames, ordinary non-Remote-ID
   traffic, and a table with no sink attached are all no-ops).
6. `tests/api/test_remoteid.py`: `GET /api/remoteid/live`/`uas/{key}` shapes
   and 404, stale/conflict flags, the "remoteid" websocket message publishing
   on change and staying silent otherwise, `_sync_remoteid_sinks` re-attaching
   the tracker to a freshly (re)created table, and "remoteid" being a
   coalescable broadcast type.
7. `tests/ui/test_map_data.py`: `remoteIdRows()` produces exactly the valid,
   explicitly reported position rows (never an unreported or invalid one)
   with independent per-kind `trackId`s, and `trackSegments()` with the tight
   gap threshold never bridges a stale gap with a drawn line.
8. `tests/e2e/test_remote_id_map.py` (written, not run in the sandbox used to
   build this feature -- no Playwright browser available there): seeding
   `AppService.remoteid_tracker` directly and loading the real app renders
   all three marker kinds linked by a connecting line, with a working hover
   card and details pane, on the live `GET /api/remoteid/live` polling path.
9. Hardware tier (open): on the exact ACH and SONOFF units, receive a known
   Remote ID broadcaster on each transport and confirm markers/trails match
   the broadcaster's own log, including stale/reconnect gap behavior.

See also: [Wi-Fi](wifi.md), [BLE](ble.md), [Watch rules](rules.md), [Discover Map and Table](discover-map.md), [LoRa native](lora-native.md).
