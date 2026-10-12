# Drone Remote ID decoding

msdr decodes ASTM F3411 / ASD-STAN Open Drone ID (Remote ID) broadcasts from the Wi-Fi ([wifi.md](wifi.md)) and BLE ([ble.md](ble.md)) receivers, tracks the aircraft it hears, and shows them in Discover and on the map. This page is for operators who want to see nearby drone broadcasts and for developers working on the decoder.

Everything shown is what the drone reports about itself, not a verified location.

## How to use it

Live:

1. Enable and start the Wi-Fi receiver, the BLE receiver, or both (see [wifi.md](wifi.md) and [ble.md](ble.md)).
2. Open Discover, then the **Remote ID** tab. It lists the drones currently heard and has a details pane (positions, altitude, speed, heading, accuracy, recent messages).
3. Open the Map workspace. The "Drones (Remote ID)" layer shows them on the map.

Offline:

- In Review, a saved Wi-Fi session has a "Remote ID" action that decodes the recorded PCAP and shows the result inline (`GET /api/wifi/sessions/{id}/remoteid`).

What you can receive depends on the transport:

- Wi-Fi: Beacon and probe-response vendor IEs, and Wi-Fi NAN frames (see [Limitations](#limitations)).
- BLE with an HCI dongle: legacy (Bluetooth 4) advertising only.
- BLE with the Sniffle SONOFF dongle: also the Bluetooth 5 long-range variant (extended advertising, Coded PHY).

## Provenance

- Message encode/decode is a hand-written Python port of `opendroneid/opendroneid-core-c` (Apache-2.0, https://github.com/opendroneid/opendroneid-core-c) at commit `6484f26545d4f012682524e2d843fab0fbdc0b34`. Files consulted:
  - `libopendroneid/opendroneid.c` and `.h`: Basic ID, Location/Vector, Authentication, Self-ID, System, Operator ID and Message Pack encode/decode; sentinel "unknown"/"invalid" encodings; accuracy enums.
  - `libopendroneid/wifi.c` and `odid_wifi.h`: the ASD-STAN vendor IE (OUI `FA:0B:BC`, OUI type `0x0D`, `struct ODID_service_info` = a 1-byte message counter plus `ODID_MessagePack_encoded`, carried in Wi-Fi Beacon/probe-response information element 221).
- msdr is GPL-3.0-only, which may incorporate Apache-2.0 code. New files carry `SPDX-License-Identifier: GPL-3.0-only` and a header noting they derive from opendroneid-core-c and the commit above, as the Apache-2.0 attribution requires. `THIRD_PARTY_NOTICES.md` carries the full notice.
- The port is cross-checked against independently generated test vectors (`tests/remoteid/data/vectors.json`, see its README) rather than against the C library's own test harness.
- BLE Bluetooth 4 legacy advertising framing (service data UUID `0xFFFA`, first byte application code `0x0D`, then a 1-byte message counter and one 25-byte message) is not covered by the opendroneid-core-c files consulted (no `bluetooth.c`). It follows the ASTM F3411 / ASD-STAN specification referenced in that project's README, and matches the `REMOTE_ID_UUID` and `REMOTE_ID_APP_CODE` constants in `msdr/ble/adv.py`.

## Modules (`msdr/remoteid/`)

### `messages.py`: the 25-byte message codec (no I/O)

`decode_message(data: bytes) -> BasicId | Location | Authentication | SelfId | System | OperatorId | RemoteIdError`

- Dispatches on the high nibble of byte 0 (message type). The low nibble is `protocol_version`; it is carried through but never gates decoding, matching opendroneid's policy of decoding newer protocol versions best-effort.
- Returns `RemoteIdError("truncated" | "malformed" | "unsupported_type", detail)` for anything other than exactly 25 bytes, an out-of-range message type, or a type-specific validity failure (for example an Authentication page whose `Length` exceeds its declared page count). It never raises.

`decode_message_pack(data: bytes) -> MessagePack | RemoteIdError`

- Decodes a `[type=0xF/version][single_size][count][messages...]` buffer with 2 to 9 embedded messages (per the spec's content limits: at most 2 Basic ID, 1 each of the rest).
- Per-message decode failures are collected in `MessagePack.errors` and reported, not hidden.

Unknown values and units:

- Every field with a spec-defined "no value" or "unknown" encoding decodes to `None`, never to the sentinel number. The sentinels are: direction 361 deg, horizontal speed 255 m/s, vertical speed 63 m/s, latitude and longitude both exactly 0 deg, altitude/height -1000 m, timestamp `0xFFFF`.
- Accuracy enums (`*_enum`, 0 = unknown) are kept as their raw 0-15 value and also exposed as a decoded meters or seconds float, which is `None` when the enum is 0. opendroneid's own `decodeHorizontalAccuracy()` and similar return a worst-case default (for example 18520 m) for the unknown enum. This port never surfaces that default as if it were a reported accuracy.
- Units and resolution follow `opendroneid.h`:
  - lat/lon at 1e-7 deg (`int32 / 1e7`);
  - altitude/height in 0.5 m steps with a -1000 m offset;
  - horizontal speed in 0.25 m/s steps below 63.75 m/s and 0.75 m/s above (the multiplier bit);
  - vertical speed in 0.5 m/s steps;
  - `Location.timestamp_s` in tenths of a second since the top of the hour (0-3599.9, wrapping every hour). Freshness is never derived from this field (see `tracker.py`).

### `transport.py`: extracting message bytes

It pulls message bytes out of an already-parsed Wi-Fi management frame or BLE advertisement and does not touch radiotap, pcap or HCI itself.

- `extract_wifi_remote_id(frame: msdr.wifi.dot11.ManagementFrame) -> WifiRemoteId | None`: scans `frame.ies` for IE 221 (vendor-specific) with OUI `fa:0b:bc` and OUI type `0x0D`, and decodes the trailing `ODID_service_info` (message counter plus message pack) with `messages.decode_message_pack`.
- `extract_ble_remote_id(adv: msdr.ble.adv.Advertisement) -> BleRemoteId | None`: reads `adv.service_data[adv.REMOTE_ID_UUID]` (`msdr/ble/adv.py` flags `adv.remote_id` the same way). The first byte is the application code (checked against `msdr.ble.adv.REMOTE_ID_APP_CODE`), the second is the message counter, and the rest is a 25-byte legacy message or a Message Pack, selected by its message type. The HCI backend carries legacy messages. Sniffle can also deliver extended advertisements containing packs, and live ingestion dispatches them through this same entry point.
- `extract_ble_extended_remote_id(service_data: bytes) -> BleRemoteId | None`: the same service-data framing (`service_data` is the raw AD value, application code byte included), but for BLE 5 extended advertising, whose larger PDU carries a full message counter plus Message Pack instead of one message.
- `decode_wifi_records(records: Iterable[msdr.wifi.pcap.Record]) -> tracker.Tracker`: the offline entry point. It replays the same radiotap-parse and dot11-parse pipeline as `msdr/wifi/replay.py` and `NetworkTable.ingest`, and feeds every decoded Remote ID report into a fresh `Tracker`. Each record's own pcap timestamp (`record.ts`) is the receive time, never wall-clock `time.time()`, so offline decoding of the same capture is reproducible.
- `decode_wifi_session(store: msdr.wifi.sessions.WifiSessionStore, session_id: str) -> tracker.Tracker`: reads a recorded or imported session's `part-*.pcap` files via `WifiSessionStore.session_records()` and decodes them.
- `extract_nan_remote_id`: the Wi-Fi NAN path (see [Limitations](#limitations)).

### `tracker.py`: in-memory aggregation (no storage or network I/O)

- **Tracks:** one `AddressTrack` per physical transport address (`"wifi:<mac>"` or `"ble:<addr>"`), which is the session-scoped key the spec calls for before a Basic ID is seen. `Tracker.ingest_wifi_pack()` and `Tracker.ingest_ble_message()` each take an explicit `received_at`. It defaults to `time.time()` for live use, and offline replay always passes `record.ts`.
- **Positions are kept apart:**
  - `drone_position`: from the Location message (lat, lon, alt, status, speed, direction, height), as one `LocationFix` with its own `received_at`.
  - `operator_position`: from the System message when `OperatorLocationType == LIVE_GNSS`.
  - `takeoff_position`: from the System message when `OperatorLocationType` is `TAKEOFF` or `FIXED`. The spec encodes which of the latter two a System message updates in that one enum field, so the tracker branches on it rather than guessing.
  - `self_id` and `operator_id` are metadata-only updates with their own freshness timestamps, and never touch position freshness.
- **Dedup:** per address and message type, the last applied `message_counter` is remembered. A repeat of the same counter (a resent burst) updates `last_seen` only and is not processed as new content, since the spec says the counter need not change for identical retransmissions.
- **Grouping:** `Tracker.groups()` groups `AddressTrack`s by Basic ID UAS ID (the raw 20-byte identifier, not the display string, so a binary UTM UUID and a short serial number are never confused). This is the only signal used to say two transports are the same aircraft, never timing or RSSI.
- **Conflicts:** a group with more than one address and simultaneously fresh drone positions that disagree by more than `conflict_distance_m` (great-circle, default 5 km) is returned with `conflict=True`. Its member tracks stay separate, never averaged or silently picked, so two drones sharing an identifier stay visibly two drones.
- **Staleness:** `AddressTrack.is_stale(now, max_age_s)` and `Tracker.groups(stale_after_s=...)` mark a `drone_position` stale by wall or pcap-clock age, independent of the wrapping `Location.timestamp_s` field.
- **Recent messages:** `AddressTrack.recent_messages` keeps up to 20 `(received_at, message)` pairs, newest last, of every message that decoded (never a `RemoteIdError`). It is in memory only, not in `DecodeEventRepo`.
- **Accuracy:** `LocationFix` also carries `horiz_accuracy_m` and `vert_accuracy_m`, decoded with `messages.decode_horizontal_accuracy_m` and `decode_vertical_accuracy_m` (`None` for the unknown enum).

## Live

`msdr/api/services/remoteid.py` runs one `Tracker` (`AppService.remoteid_tracker`), shared by both receivers and built in `RemoteidServiceMixin._build_remoteid()`. It is fed inline rather than through a queue:

- **Wi-Fi:** `NetworkTable._ingest` in `msdr/wifi/observations.py` (the good-FCS path, right where the device-history hook `self.history.add(...)` sits) calls `transport.extract_wifi_remote_id(frame)`. If the frame carries a valid (non-`RemoteIdError`) vendor IE, it calls `tracker.ingest_wifi_pack()` keyed `"wifi:<bssid>"`.
- **BLE:** `AdvertiserTable.ingest` in `msdr/ble/observations.py` (same place as its own `self.history.add(...)`) does the equivalent with `transport.extract_ble_remote_id()` and `tracker.ingest_ble_message()`, keyed `"ble:<addr>"`.
- Both tables reach the tracker through an optional `.remoteid` sink attribute, the same pattern as `self.table.history = self.history` for device history.
- `RemoteidServiceMixin._sync_remoteid_sinks()` re-attaches the sink every tick (mirroring `HistoryServiceMixin._sync_history_sinks()`), so a Wi-Fi channel hop or a BLE reconnect, both of which replace the table object, never silently stops feeding the tracker.

### API

| Route | Purpose | Returns |
|---|---|---|
| `GET /api/remoteid/live` | Live tracked UAS | `{now, items: [...]}`, one item per `Tracker.groups()` group: id/UAS type, drone, operator and takeoff positions (each with `age_s` and `stale` computed against a 60 s threshold, `None` when never reported or not a valid position), transports (`sources`), last seen and `conflict` |
| `GET /api/remoteid/uas/{key}` | One UAS | The same group shape plus `recent_messages` (every track's recent messages merged, newest last, capped at 50); 404 if the key is not currently tracked |

Binary fields (`uas_id_bytes`, `auth_data`) are hex strings in these JSON responses.

A coalescable `{"type": "remoteid", "now", "items"}` websocket message (`_COALESCABLE_TYPES` in `msdr/api/service.py`) is published at most once a second, and only when the tracked UAS set's last-seen signature changed (`RemoteidServiceMixin._maybe_publish_remoteid`). This follows the same reasoning as the `wifi` and `ble` messages.

### UI

- Discover's "Remote ID" tab (`msdr/ui/components/remoteid-view.js`) polls `GET /api/remoteid/live` every second, in the same self-contained way as `wifi-view.js` and `ble-view.js`. It shows a compact live UAS table and a details pane (positions, altitude, speed, heading, accuracy, recent messages).
- Review's "Wi-Fi sessions" panel has a "Remote ID" row action that expands the offline decode (`GET /api/wifi/sessions/{id}/remoteid`) inline.

## Map

Implemented in `msdr/ui/map-data.js` and `msdr/ui/components/discover-map.js`.

- `remoteIdRows(items)` turns each live group into up to three map rows, one per explicitly reported, valid position (`drone`, `operator`, `takeoff`). A position that is missing or decoded as unknown/invalid produces no row, so ordinary Wi-Fi and BLE devices with no Remote ID never appear on the map. Each row gets its own `trackId` (`remoteid:<key>:<kind>`), so `trackSegments()` keeps each position kind's trail independent and persistent.
- `discover-map.js` adds a "Drones (Remote ID)" layer:
  - Marker shapes differ by kind (`.remoteid-marker.drone`, `.operator`, `.takeoff` in `styles.css`): diamond, circle and square, so kinds are not told apart by colour alone.
  - A thin dashed line connects a UAS's simultaneously reported positions (it is not a trail).
  - Trails use a tight 60 s gap threshold (`REMOTEID_TRAIL_GAP_S`, matching the live staleness threshold) instead of the default 1800 s used for aircraft and AIS. A stale position or a receiver reconnect always breaks the trail instead of drawing a straight line across the gap.
  - The hover card shows UAS ID, type, altitude (barometric and geodetic labelled separately), speed, heading, accuracy, transport and last-report age, always with "reported claim, not a verified location".
  - Selecting a marker opens a details pane through the existing bottom-panel mechanism. The aircraft/maritime floating card stays aircraft and maritime only.
- The layer polls `GET /api/remoteid/live` itself every 2 s (`_loadRemoteId` in `discover-map.js`). This is independent of the decoder-event props the rest of the map uses, and it runs only on the Map workspace (the Table view has no Remote ID row shape).
- "Clear map" hides the layer the same way it hides decoder events, by timestamp cutoff (`_mapRemoteIdGroups`), so a new report after clearing reappears on its own.

## Limitations

- **Wi-Fi NAN** is decoded (`transport.extract_nan_remote_id`, live through `NetworkTable._ingest_nan` and offline in `decode_wifi_records`):
  - It is a Public Action frame (0x04/0x09) with the Wi-Fi Alliance OUI 50:6F:9A type 0x13. Its Service Descriptor Attribute (0x03) carries service ID `88 69 19 9d 92 09` (the first 48 bits of SHA-256 of "org.opendroneid.remoteid"), and its service info carries the same message counter plus Message Pack as the Beacon IE.
  - Bad-FCS frames are skipped.
  - NAN discovery windows are on channel 6 (2.4 GHz) and 149 (5 GHz), so a fixed capture on channel 6 is the place to listen.
  - It is fixture-tested only (synthetic frames built around the opendroneid message-pack vectors) and has not been verified against a real NAN broadcaster.
- Live reception is fixture and unit tested only. It has not yet been checked on the ACH and SONOFF units against a known Remote ID broadcaster, including stale and reconnect gap behaviour.
- Watch rules and geofence notifications on reported positions are in [rules.md](rules.md).

## Tests

- `tests/remoteid/test_messages.py`, against `tests/remoteid/data/vectors.json` (generated by linking the real opendroneid-core-c sources, not derived from this port; see that directory's README) plus the `test/test_inout.c` example values transcribed by hand. Every message type, Message Pack, unknown/sentinel values and boundary values (max lat/lon/altitude/speed, direction wrap at 360 deg, timestamp wrap at the hour) decode to the expected fields. Truncated and malformed buffers return `RemoteIdError` and never raise.
- `tests/remoteid/test_transport.py`: Wi-Fi vendor IE and BLE legacy service-data extraction against the generated beacon-frame vector and hand-built malformed or truncated cases. The BLE extended-advertising (Message Pack) path is exercised directly.
- `tests/remoteid/test_tracker.py`: malformed or truncated input, unknown fields, timestamp wrap at the hour, stale-position detection, duplicate message-counter bursts, and two drones transmitting the same UAS ID with diverging positions (conflict reported, not merged).
- Offline decoding is reproducible: decoding the same PCAP session twice (a direct `decode_wifi_records()` call and `GET /api/wifi/sessions/{id}/remoteid`) yields identical output.
- `tests/remoteid/test_live.py`: a trusted (good-FCS) Wi-Fi beacon or probe response carrying the vendor IE, and a BLE advertisement carrying the legacy service data, each reach a `Tracker` through the table's optional `.remoteid` sink and nowhere else. Bad-FCS frames, ordinary non-Remote-ID traffic and a table with no sink attached are all no-ops.
- `tests/api/test_remoteid.py`: `GET /api/remoteid/live` and `uas/{key}` shapes and 404, stale and conflict flags, the `remoteid` websocket message publishing on change and staying silent otherwise, `_sync_remoteid_sinks` re-attaching the tracker to a freshly created table, and `remoteid` being a coalescable broadcast type.
- `tests/ui/test_map_data.py`: `remoteIdRows()` produces exactly the valid, explicitly reported position rows with independent per-kind `trackId`s, and `trackSegments()` with the tight gap threshold never bridges a stale gap with a drawn line.
- `tests/e2e/test_remote_id_map.py`: seeds `AppService.remoteid_tracker` directly and loads the real app. It checks that all three marker kinds render, linked by a connecting line, with a working hover card and details pane on the `GET /api/remoteid/live` polling path.

See also: [Wi-Fi](wifi.md), [BLE](ble.md), [Watch rules](rules.md), [Discover Map and Table](discover-map.md), [LoRa native](lora-native.md).
