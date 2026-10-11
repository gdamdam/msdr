# Passive BLE reception (raw HCI dongle, and Sniffle)

Passive Bluetooth Low Energy advertising reception for operators who want to see what is around them, using a USB HCI dongle or a Sniffle-flashed SONOFF dongle. It is the BLE sibling of docs/wifi.md: an independent receiver beside the RSPdx-R2/SDRconnect receiver that shares nothing with it.

msdr listens to BLE advertising only. It never scans actively, never sends scan requests, never connects, pairs or bonds, and never touches Bluetooth Classic.

Status:

- **0.59.0:** Bluetooth 4.x USB dongles driven over raw HCI (hardware-tested on a Broadcom BCM20702A0, USB id 19ff:0239).
- **Sniffle backend:** a genuine SONOFF ZBDongle-P (CC2652P) running Sniffle firmware (`msdr.ble.sniffle_capture`, "Sniffle backend" below), behind the same `AdvertiserTable`/table/UI. It adds extended advertising and Coded PHY.
- **Tested:** on 2026-10-08, a SONOFF unit (CP2102N bridge) for 1M reception, channel hop, unplug and Setup Start/Stop. Coded PHY and extended/aux advertising are still fixture-tested only (tests/ble/fake_sniffle.py) because nothing nearby transmitted them ("Hardware notes" below).

## Which dongle: what each one can see

| | Bluetooth 4.0 dongle, raw HCI (e.g. BCM20702A0) | SONOFF ZBDongle-P with Sniffle (Bluetooth 5) |
|---|---|---|
| Legacy advertising on 1M PHY | yes | yes |
| Extended advertising (Bluetooth 5) | no | yes |
| Coded PHY ("Long Range") | no | yes |
| 2M PHY (secondary channels only) | no | yes, through aux follow |
| Advertising channel per packet (37/38/39) | no | yes; fixed channel or hop |
| Scan responses | no | yes, when another device's scan triggers one |

What the SONOFF adds in practice:

- Drone Remote ID sent as Bluetooth 5 Long Range (Coded PHY, extended advertising), which the 4.0 dongle cannot receive at all.
- Newer phones, trackers, sensors and development boards that use extended advertising.
- Which channel each advertiser uses.

A typical home of phones, laptops and earbuds advertises in legacy 1M only, so both dongles see much the same there; the Bluetooth 5 extras appear only near something that transmits them.

## Passive listening only, and privacy

msdr only listens to Bluetooth advertisements: short public broadcasts that
devices send to everyone in range so they can be found ("I'm here, this is
my type"). It does not see what devices say to each other.

- **Never transmits.** The raw-HCI backend uses passive scanning, so the
  controller sends nothing, not even a scan request. The Sniffle backend
  implements only receive commands (channel, PHY, filters, aux follow, version
  probe): it has no code to transmit, advertise, scan actively or connect, and
  it switches Sniffle's connection following off. Scan responses it shows were
  requested by some other device, not by msdr.
- **No connections, no content.** msdr never connects, pairs or bonds, and
  never follows a connection, so it cannot see audio, messages, files or any
  data exchanged between devices. Bluetooth Classic is not received at all.
- **Rotating addresses stay anonymous.** Phones and most modern devices
  change their address every few minutes for privacy. msdr treats those
  addresses as temporary: it does not link one address to the next,
  deliberately does not fingerprint payloads (the stable parts are shared by
  every device of the same model, so it would mislabel strangers), refuses
  labels on them, and hides them from the timeline unless asked.
- **No location from signal strength.** RSSI is shown as received; msdr does
  not turn it into a position or a distance. Remote ID positions are what the
  drone claims, not a measurement.
- **Stays on this machine.** Nothing is uploaded. The live table is in memory
  until Stop or Clear. Device history (docs/history.md) is **off by
  default**; when turned on (`"history": {"enabled": true}` in
  settings.json) it keeps per-minute summaries in `history.db` for 30 days,
  can be wiped (`POST /api/history/clear`) and is not included in backups.

Use it to learn what is around you and to inspect your own devices. What it
receives is broadcast openly, but other people's devices still belong to
other people: don't use it to follow or identify someone.

## How it works

- macOS keeps using the Mac's internal Bluetooth controller. An **external**
  USB dongle that macOS has not claimed (no transport driver attached, which
  is the normal case for a second controller) can be opened with libusb and
  driven with HCI commands directly.
- The helper `python -m msdr.ble.capture` (same venv as msdr, needs the
  optional `pyusb` dependency: `pip install 'msdr[ble]'`) claims USB
  interface 0, sends `HCI_Reset`, reads the controller version and LE
  features, then issues `LE_Set_Scan_Parameters` with **scan type 0x00
  (passive)** and `LE_Set_Scan_Enable` with duplicate filtering off. Passive
  scanning is receive-only by definition: the controller never transmits a
  SCAN_REQ, so no SCAN_RSP data is collected either.
- The helper writes one JSON line per advertising report on stdout and
  JSON status events on stderr (see "Helper protocol"). `BleSupervisor`
  (msdr/ble/supervisor.py) runs it as a subprocess, exactly like the Wi-Fi
  helper, and feeds an in-memory `AdvertiserTable`.
- Scan interval and window are both 10 ms (100 % duty). The controller
  schedules advertising channels 37, 38 and 39 itself; HCI reports carry no
  channel number, so coverage is "all three primary advertising channels,
  controller-scheduled" and the UI says so instead of inventing a channel.

What a Bluetooth 4.x controller can and cannot see:

| | |
|---|---|
| Legacy advertising PDUs (ADV_IND, ADV_NONCONN_IND, ADV_SCAN_IND, ADV_DIRECT_IND) on the 1M PHY | yes |
| Advertiser address and address type, RSSI, raw AD payload (≤31 bytes) | yes |
| Scan responses | no (would require active scanning) |
| Extended advertising (Bluetooth 5), 2M PHY, Coded PHY long range | no |
| Per-report channel number, exact timestamp from the radio | no (host receive time is used) |
| Bluetooth Classic inquiry/pages | no, by design |

Legacy Remote ID (ASTM F3411 / ASD-STAN over BLE 4 advertising, service data UUID 0xFFFA, application code 0x0D) is therefore visible; the Bluetooth 5 long-range variant is not until the Sniffle backend exists.

- In 0.59.0 the table only **flags** such advertisers (`remote_id: true`).
- Since the Phase 4 pass (docs/remote-id.md) every trusted advertisement carrying that service data is also decoded live and fed into the shared Remote ID `Tracker` (`AdvertiserTable.ingest` -> its optional `.remoteid` sink), reachable at `GET /api/remoteid/live` and Discover's "Remote ID" tab — see docs/remote-id.md "Live".

### Identities and privacy

Random addresses are session-scoped: a resolvable or non-resolvable private
address (`identity: "rpa"` / `"nrpa"`) rotates every few minutes and is never
treated as a device identity beyond the current table. Only `public` and
`static` addresses are stable. The live table lives in memory until Stop or
Clear; per-minute device history is kept separately (docs/history.md), and
there is no packet recording in this release.

## Helper protocol

`python -m msdr.ble.capture --list` prints one JSON object per Bluetooth-class
USB device (`{"vid","pid","product","manufacturer","serial","bus","address","known"}`,
`known` is true for the id list in `msdr.ble.hci.KNOWN_DONGLES`) and exits 0.

`python -m msdr.ble.capture [--usb-id VID:PID]` captures until SIGTERM/SIGINT:

- stdout, one JSON line per LE Advertising Report:
  `{"ts": <unix float>, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public"|"random", "evt": "ADV_IND"|"ADV_DIRECT_IND"|"ADV_SCAN_IND"|"ADV_NONCONN_IND"|"SCAN_RSP", "rssi": <int dBm or null>, "data": "<hex>"}`
- stderr JSON events:
  - `{"event":"ready","vid","pid","product","bus","address","hci_version","hci_revision","lmp_version","lmp_subversion","manufacturer_id","manufacturer_name","le_features":"<hex>","scan_type":"passive","scan_interval_ms":10,"scan_window_ms":10,"helper_version"}`
  - `{"event":"stats","reports":<n>}` roughly once per second
  - `{"event":"error","code":"...","message":"..."}`
  - `{"event":"stopped"}`

Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | no Bluetooth-class USB device (or the requested id) found |
| 2 | cannot claim the device (macOS owns it, or permissions) |
| 3 | USB error during capture |
| 4 | pyusb/libusb missing |
| 5 | controller has no LE support or rejected scanning |

On SIGTERM the helper disables scanning and releases the interface before
exiting, so the dongle is left idle.

## msdr side

- `msdr/ble/hci.py`: HCI command/event encoding, event reassembly, the
  LE Advertising Report parser, `KNOWN_DONGLES`, `is_bluetooth_class()`.
  Pure functions, no pyusb import at module import time.
- `msdr/ble/adv.py`: AD structure parser (flags, names, 16/32/128-bit service
  UUIDs, service data, manufacturer data with a small company-id name table,
  TX power, appearance) and `classify_address()`.
- `msdr/ble/capture.py`: the helper CLI above.
- `msdr/ble/sniffle_proto.py` / `msdr/ble/sniffle_capture.py`: the Sniffle
  backend, see "Sniffle backend" below.
- `msdr/ble/observations.py`: `AdvertiserTable` -- bounded per-address
  entries (oldest `last_seen` evicted past `max_entries`, default 2000),
  RSSI history, event-type counts, distinct payload history, live feed deque
  of the last 500 reports with a sequence number (`live_since(after, limit)`),
  `rows(q, sort, order, offset, limit)`, `details(addr)`, `clear()`,
  `snapshot_changed_since(t)`. Entries also carry `phy`/`channel`/`extended`
  (Sniffle backend only; always `null`/`null`/`false` from the hci backend,
  which reports neither per docs above).
- `msdr/ble/supervisor.py`: `BleSupervisor(helper_command=None, usb_id=None,
  backend="hci", serial_port=None, baud=None, phy=None, channel=None,
  on_status=None)` with `start()`, `stop()`, `devices()`, `status()`,
  `running`, `table`.
  - Health states: `dependency_missing`, `no_device`, `claimed` (the OS owns the dongle, hci only), `no_le` (hci only), `firmware_mismatch`, `wrong_baud` (sniffle only), `stopped`, `starting`, `receiving`, `stale` (no report for 10 s), `failed`.
  - Crash retry with the same backoff as Wi-Fi.
  - Exit codes 1, 2, 4, 5 (hci) and 1, 4, 6, 7 (sniffle) are terminal states, not retries — a mid-run serial disconnect (exit 3) retries like any other crash.
- Settings: `ble: {enabled: false, backend: "hci", usb_id: null, serial_port: null, baud: null, phy: "1m", channel: "37", helper_command: null}`

| Setting | Backend | Notes |
|---|---|---|
| `enabled` | both | default `false` |
| `backend` | both | `"hci"` (default) or `"sniffle"` |
| `usb_id` | hci only | default `null` |
| `serial_port` | sniffle only | default `null` |
| `baud` | sniffle only | `null` means auto (try 2000000, then 921600) |
| `phy` | sniffle only | default `"1m"` |
| `channel` | sniffle only | default `"37"` |
| `helper_command` | both | test-only, like the Wi-Fi one |

## API

| Route | Notes |
|---|---|
| `GET /api/ble/status` | supervisor status (including `drops` from the reader queue and `evictions` from the bounded table) plus `enabled`, `device`, `advertiser_count` |
| `GET /api/ble/devices` | `{"devices":[...], "error"}` from `--list` |
| `POST /api/ble/start` | 409 unless enabled in settings; body `{}` |
| `POST /api/ble/stop` | |
| `GET /api/ble/advertisers?q=&sort=&order=&offset=&limit=` | sort: `first_seen`, `last_seen`, `addr`, `name`, `rssi`, `reports`, `company`, `identity`, `services`, `device` |
| `POST /api/ble/advertisers/clear` | empties the table; capture continues |
| `GET /api/ble/live?after=&limit=` | reports with seq > after, oldest first |
| `GET /api/ble/advertisers/{addr}` | details, 404 when unknown |

Row shape (`AdvertiserTable` summary): `addr, addr_type, identity
(public|static|rpa|nrpa), name, company_id, company, connectable,
service_uuids (list of strings), remote_id, rssi (latest int or null),
reports, first_seen, last_seen, evt_types ({name: count}), phy, channel,
extended, sensor, service_names, apple` (the `phy`/`channel`/`extended` trio
is `null, null, false` from the hci backend; `sensor` is `null` unless the
latest report decoded as one of the formats below; `service_names` maps
advertised UUIDs to names, built-in plus the downloaded SIG list; `apple` is
the Continuity summary or `null`).

Details add: `flags, tx_power, appearance, service_data ({uuid: hex}),
manufacturer_data ({company_id: hex}), rssi_history ([[ts, rssi], ...]),
payloads ([{hex, first_seen, last_seen, count}], last 8 distinct),
recent ([{ts, evt, rssi, length}], last 20), sensor_history` (the latest 20
decoded sensor reports, same shape as `sensor` below, each with its own `ts`).

Live item: `{seq, ts, addr, identity, evt, rssi, name, company, length,
remote_id, sensor, phy, channel, extended}`.

## Sensors

`msdr/ble/sensors.py`: explicit, per-protocol parsers for published sensor
beacon formats, run against every advertisement's service/manufacturer data
(`AdvertiserEntry.observe`, docs/ble.md "msdr side"). Nothing here infers the
contents of an encrypted payload -- an encrypted BTHome packet is reported as
`encrypted: true` with no readings, never guessed. An unrecognised object id
inside a packet stops decoding of the remaining objects in that packet rather
than guessing a length and misaligning everything after.

`sensor` shape: `{protocol, readings: {name: {value, unit}}, encrypted, ts}`.
`AdvertiserTable` keeps the latest one per advertiser plus a bounded history
(last 20, `sensor_history` in details and the live feed's `sensor` field).

Supported formats, fixtures in `tests/ble/test_sensors.py` taken from each
one's published specification (source URLs and fetch date in
`msdr/ble/sensors.py`'s module docstring):

| Protocol | Carrier | Readings |
|---|---|---|
| BTHome v2 | service data 0xFCD2 | `temperature, humidity, battery, pressure, illuminance, door, motion` (unencrypted objects only) |
| Ruuvi RAWv2 (data format 5) | manufacturer id 0x0499 | `temperature, humidity, pressure, battery, tx_power, movement_counter` |
| iBeacon | manufacturer id 0x004C, type 0x02 | `uuid, major, minor, measured_power` |
| Eddystone UID/URL/TLM | service data 0xFEAA | UID: `tx_power, namespace, instance`; URL: `tx_power, url`; TLM: `battery_voltage, temperature, adv_count, uptime` |
| pvvx/ATC custom thermometer | service data 0x181A | `mac, temperature, humidity, battery, battery_voltage, counter` (ATC1441 layout, 13 bytes) or the same plus `flags` (pvvx layout, 15 bytes); disambiguated by length alone |

Device history (docs/history.md): sensor readings are not yet folded into
per-minute buckets. `history.db`'s `buckets` table is signal-only (one
numeric `sig_*` series per device); adding a per-reading-name mean cleanly
needs its own table (a `003_*.sql` migration: `sensor_buckets(receiver,
ident, minute, reading, unit, value_sum, value_n)`) and a second accumulator
path alongside `MinuteAccumulator`. Left for later rather than bolted on here.

Discover → BLE (`ble-view.js`): the Advertisers table gets a Sensor column
(protocol chip, "<Protocol> (encrypted)" when encrypted, full readings in the
chip's `title`); the details pane gets a "Sensor" section listing every
reading in words, or stating that the payload is encrypted and its readings
cannot be decoded without the bind key.

`GET /api/ble/status` also carries `backend` (`"hci"|"sniffle"`) and, for
the sniffle backend, the configured `phy`/`channel` and a `coverage`
sentence computed for exactly that configuration (never "all", see "UI
contract").

Websocket message `{"type": "ble", "status": {...}, "changed": [rows]}` on
every supervisor status publish, coalescable like `wifi`.

## Device summary

Above the advertiser table, a strip counts the devices heard in the last
minute by **kind of device**: "About 44 devices heard in the last minute ·
Apple 18 (iPhone / iPad / Mac nearby 14, AirPods / Beats 3, Find My 1) ·
Microsoft 5 · ...". Each advertiser falls in exactly one group: drones
(Remote ID), sensor beacons (by format), Apple (split by Continuity message
type), its maker's name, or Unknown; makers beyond the six largest fold into
"Other makers". Counts are approximate -- a phone that rotates its address
during the minute counts twice.

Only the kind of device is used. Activity and state fields (Nearby Info's screen, lock or call state and similar) are not decoded, not even as an aggregate:

- In a home or a quiet street a count like "1 device on a call" points at one person.
- Counts over time reveal routines.
- Those fields are reverse-engineered and change between iOS versions.

API: `GET /api/ble/summary?window_s=60` (10-3600) → `{window_s, total,
groups: [{label, count, subgroups: [{label, count}]}]}`.

## Bluetooth SIG names

msdr has short built-in tables of company and service names
(`msdr/ble/adv.py`). Setup → BLE → **Download** fetches the Bluetooth SIG's
full lists -- company identifiers (~4,000), 16-bit service UUIDs (SIG and
member, e.g. Tile, Samsung) and appearance values ("Wearable Audio Device:
Earbud") -- from bitbucket.org/bluetooth-SIG/public into
`<data folder>/ble/sig_assigned_numbers.json`, and **Update** refreshes it.
The SIG marks these files proprietary with no licence granted, so msdr does
not bundle them: the download is for the operator's own use under the SIG's
terms. Built-in short names still win where both exist ("Apple", not
"Apple, Inc."); a failed download keeps the previous file.

API: `GET /api/ble/names` → `{downloaded, companies, services, appearance,
fetched_at, source}`; `POST /api/ble/names/refresh` downloads (502 with the
error when it fails). Rows carry `service_names` (built-in + SIG) and
details carry `appearance_name`.

## Apple Continuity

Apple devices advertise "Continuity" messages in manufacturer data (company
0x004C) as a list of type/length messages. `msdr/ble/apple.py` names each
message **type** (Handoff, Nearby Info, AirPlay target, Find My, AirDrop,
HomeKit, AirPods / proximity pairing, ...; unknown types keep their hex
code) and, for AirPods/Beats proximity-pairing messages (type 0x07), the
**model and battery levels** (left, right, case, in 10 % steps, plus
charging flags). Rows show "Apple device · Handoff, Nearby Info" or the
AirPods model; the details pane lists the types and the battery line.
Types accumulate per address, so a rotating address starts empty again.

Apple does not publish this format: the type list follows the furiousMAC
"continuity" project and Martin et al. ("Handoff All Your Privacy", PoPETs
2019), the proximity-pairing layout follows the OpenPods app, and the
HomeKit types use the HAP specification's length byte (low 5 bits). It is
best effort and can change with Apple firmware.

Deliberately not decoded, in line with "Passive listening only, and privacy" above:

- Fields describing what a person is doing (Nearby Info activity, screen or call state; Nearby Action details).
- Addresses carried inside messages (an AirPlay target's IP address).
- Find My keys.

## UI contract

- Setup → "BLE receiver" card: Enable toggle (saves immediately), a backend
  selector (USB HCI dongle / SONOFF Sniffle), then backend-specific fields
  (hci: optional USB id; sniffle: serial port, baud auto/2000000/921600,
  PHY, channel), detected devices/ports with rescan, status line, a plain
  explanation for `firmware_mismatch`/`wrong_baud`/`no_device` (msdr never
  flashes firmware or guesses a baud rate beyond trying both), controller
  facts once running, and a reception-check line ("N reports received,
  R/s" or "no reports received yet") once Start is pressed. Start/Stop
  live in Discover, not Setup, same as Wi-Fi.
- Discover → BLE tab (`ble-view`): Start/Stop/Clear, state words, two tabs
  "Advertisers" (sortable table: Address, Identity, Name, Company, Services,
  RSSI, Reports, Last seen) and "Live" (newest-last feed, same merge helper
  pattern as Wi-Fi). The Identity cell carries a second, compact PHY/channel
  chip when the sniffle backend reports one (e.g. "CODED ch37", "1M ch6
  ext"); absent entirely for the hci backend. Row click opens a details
  pane (header chips: identity kind, connectable, Remote ID; Identity /
  Advertisement / Reception fact lists, with PHY/Channel/Extended
  advertising facts when the sniffle backend reported them; RSSI sparkline
  when it varies; distinct payloads; recent reports).
- Every summary states the coverage honestly, from the server's
  `status.coverage` (never a hardcoded string, never "all"): the hci
  backend's fixed "legacy advertising, 1M PHY, channels 37-39
  controller-scheduled; no extended advertising, no scan responses", or for
  sniffle exactly what it is configured for, e.g. "Coded PHY on channel 37
  only; legacy and extended advertising (aux-channel chaining followed); no
  scan responses, no connection following", or "...channels 37-39 (dwell
  hop)..." when the channel is "hop".
- Station status strip (`receiver-status-strip.js`, development notes Phase 1):
  a compact, always-visible lamp beside the main SDRconnect strip for each
  *enabled* Wi-Fi/BLE receiver (hidden entirely when disabled) -- state dot,
  channel/PHY, and (in its tooltip) packet rate and time since the last
  packet. Self-polls `/api/wifi/status` and `/api/ble/status`; adds no
  height to the strip (tests/e2e/test_layout_gate.py's 100-220 px gate).
- Context colour `--context-ble` (a distinct hue from `--context-wifi`).

## Sniffle backend (SONOFF ZBDongle-P)

Hardware for this release: a genuine SONOFF ZBDongle-P (CC2652P) running a
matching Sniffle release build. msdr never flashes firmware -- a person
installs `sniffle_cc1352p1_cc2652p1.hex` (CP2102N bridge, 2 Mbaud) or
`sniffle_cc1352p1_cc2652p1_1M.hex` (CP2102 non-N bridge from the 2022
shortage, 921600 baud) themselves before msdr can use the dongle. Flashing
the wrong variant risks locking the bootloader (development notes upstream evidence).

- `python -m msdr.ble.sniffle_capture --list` prints one JSON object per
  CP210x-class serial port (VID `10c4`, PID `ea60`) and any other serial
  port it finds (`known: false`), and exits 0.
- `python -m msdr.ble.sniffle_capture [--port PATH] [--baud N]
  [--phy 1m|2m|coded] [--channel 37|38|39|hop]` captures until
  SIGTERM/SIGINT. `--baud` omitted tries 2000000 then 921600 in order.
  `--channel hop` dwells on 37/38/39 one second at a time, resending the
  channel-select command on the already-open connection (not Sniffle's own
  MAC/IRK-filtered `hop3`, which needs a known target address msdr does not
  have in promiscuous mode); every report still carries its own true
  `channel` from the firmware regardless of the configured primary channel.
  `--phy` sets the primary-channel PHY. Primary advertising (37/38/39) is
  only ever 1M or Coded, so `--phy 2m` receives nothing; 2M appears only on
  secondary channels, reached through aux follow from a 1M or Coded primary.
- stdout: the same JSON report shape as `msdr.ble.capture` plus `phy`
  (`"1m"|"2m"|"coded"`), `channel` (int, the real per-packet channel),
  `extended` (bool), and `aux` (`{chan, phy, offset_us}` when the PDU
  carried an auxiliary pointer, else `null`).
- stderr `ready` event additionally carries `firmware_version` ("1.11.0"),
  `api_level`, `port`, `baud` (whichever candidate answered), `phy`,
  `channel` (the configured mode, `"37"`/`"38"`/`"39"`/`"hop"`), `extended`
  (always `true`: aux-advertising-follow is always enabled).
- Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | no CP210x serial port (or the requested one) found |
| 3 | serial I/O error (including mid-run unplug — not terminal, retries like any other crash) |
| 4 | pyserial missing |
| 6 | firmware does not answer the marker/version handshake as Sniffle (`firmware_mismatch`: framed messages arrived but never matched, most often the wrong Sniffle build for this dongle) |
| 7 | no framed message decoded at any candidate baud at all (`wrong_baud`: a real baud mismatch corrupts every byte, so nothing survives the base64/CRLF framing) |

- Detection: the handshake sends the marker command and waits for a
  matching `MarkerMessage` reply, then the version command and waits for a
  `VersionMeasurement` reply, at each candidate baud in turn -- exactly the
  sync/version exchange `SniffleHW.__init__`/`probe_fw_version` use
  upstream (THIRD_PARTY_NOTICES.md).
- Receive-only by construction: `msdr/ble/sniffle_proto.py` implements only
  the channel/PHY/RSSI/CRC-validation/MAC-filter-clear/aux-advertising-
  follow commands plus the marker and version probes, and always disables
  connection-following (`cmd_follow(False)`, never re-enabled). It does not
  implement Sniffle's active-scan, connect, advertise/transmit, address/IRK
  or channel-map/interval-preload commands at all.
- `tests/ble/fake_sniffle.py`: an in-process fake speaking the real wire
  framing, injected as `sniffle_capture.capture(..., ser=...)`'s serial
  object (no OS pty, so it also runs where pty allocation is sandboxed) for
  `ok`/`firmware_mismatch`/`wrong_baud`/`unplug`; its `main()` also runs as
  a real subprocess (`FAKE_SNIFFLE_MODE` env var) for `BleSupervisor`-level
  tests, the same role `tests/ble/fake_helper.py` plays for the hci backend.

## Hardware notes

- SONOFF ZBDongle-P / Sniffle, hardware-tested 2026-10-08 on one unit:
  `--list` shows it as VID `10c4` PID `ea60` with SONOFF's own product
  string ("Sonoff Zigbee 3.0 USB Dongle Plus"), which does not say CP2102 or
  CP2102N. The CP210x vendor request 0xFF / wValue 0x370B (the Linux cp210x
  driver's GET_PARTNUM, read-only) returned 0x20, CP2102N QFN28, so the
  2 Mbaud `sniffle_cc1352p1_cc2652p1.hex` was flashed with
  `cc2538-bsl.py --bootloader-sonoff-usb -ewv` (no case opening). Verified:
  handshake at 2000000 baud, firmware 1.11.0; 1M reception; channel hop with
  even counts on 37/38/39; unplug mid-capture ends with a `serial` error;
  Setup Start/Stop. Not yet verified on hardware: Coded PHY,
  extended/aux advertising (and so 2M), an hour alongside SDRconnect with
  bounded memory (development notes).
- BCM20702A0 (Broadcom, Bluetooth 4.0): works from its ROM firmware without
  the Linux patch file; HCI version 6, LE features byte 0x01. Reports arrive
  at a few hundred per second in a dense environment.
- Dongles with vendor id 0x0a5c (Broadcom), 0x19ff:0x0239 (Rocketfish/Dynex
  BCM20702A0) and 0x0b05:0x17cb (ASUS BT400) are in `KNOWN_DONGLES`; any
  device of USB class 0xE0/0x01/0x01 is still listed and can be selected.
- If the device is claimed by macOS (state `claimed`), no transport workaround
  is attempted: msdr never changes host-controller switching, firmware or
  system Bluetooth settings.

See also: [Wi-Fi](wifi.md), [Remote ID](remote-id.md), [Device history](history.md), [Watch rules](rules.md).
