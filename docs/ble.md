# Passive BLE reception

msdr can listen for Bluetooth Low Energy advertisements with a USB HCI dongle or a Sniffle-flashed SONOFF dongle. This page is for anyone setting up or using the BLE receiver. It is the BLE counterpart of [Wi-Fi](wifi.md): an independent receiver that runs beside the SDRconnect receiver and shares nothing with it.

msdr listens to BLE advertising only. It never scans actively, never sends scan requests, never connects, pairs or bonds, and never touches Bluetooth Classic.

## Choosing a dongle

| | Bluetooth 4.0 dongle, raw HCI (e.g. BCM20702A0) | SONOFF ZBDongle-P with Sniffle (Bluetooth 5) |
|---|---|---|
| Legacy advertising on 1M PHY | yes | yes |
| Extended advertising (Bluetooth 5) | no | yes |
| Coded PHY ("Long Range") | no | yes |
| 2M PHY (secondary channels only) | no | yes, through aux follow |
| Advertising channel per packet (37/38/39) | no | yes; fixed channel or hop |
| Scan responses | no | yes, when another device's scan triggers one |

The SONOFF adds three things in practice:

- Drone Remote ID sent as Bluetooth 5 Long Range (Coded PHY, extended advertising), which a 4.0 dongle cannot receive at all.
- Newer phones, trackers, sensors and development boards that use extended advertising.
- Which channel each advertiser uses.

A typical home of phones, laptops and earbuds advertises in legacy 1M only, so both dongles see much the same there. The Bluetooth 5 extras appear only near something that transmits them.

Tested hardware:

- A Broadcom BCM20702A0 (USB id 19ff:0239) over raw HCI.
- A SONOFF ZBDongle-P (CC2652P) running Sniffle, tested on 2026-10-08 for 1M reception, channel hop, unplug and Setup Start/Stop. Coded PHY and extended/aux advertising are fixture-tested only (`tests/ble/fake_sniffle.py`), because nothing nearby transmitted them. See [Hardware notes](#hardware-notes).

## Set up and use

1. Install the optional USB dependency in the msdr virtualenv: `pip install 'msdr[ble]'` (raw HCI dongles use `pyusb`). The Sniffle backend needs `pyserial`.
2. Plug in the dongle. For a SONOFF, flash Sniffle first (see [Sniffle backend](#sniffle-backend-sonoff-zbdongle-p)). msdr never flashes firmware.
3. In Setup, open the **BLE receiver** card, turn on the Enable toggle (it saves immediately), and pick a backend: USB HCI dongle or SONOFF Sniffle.
4. Fill in the backend-specific fields:
   - HCI: an optional USB id.
   - Sniffle: serial port, baud (auto, 2000000 or 921600), PHY and channel.
5. Check that the dongle appears in the detected-devices list (use rescan if it does not).
6. Open Discover, then the BLE tab, and press **Start**. Start and Stop live in Discover, not Setup, as with Wi-Fi.
7. Once Start is pressed, the Setup card shows a reception check: "N reports received, R/s", or "no reports received yet".

On macOS the Mac's internal Bluetooth controller keeps working. Only an external dongle that macOS has not claimed can be used.

## What msdr does and does not see

msdr only listens to Bluetooth advertisements: short public broadcasts that devices send to everyone in range so they can be found. It does not see what devices say to each other.

- **Never transmits.** The raw-HCI backend uses passive scanning, so the controller sends nothing, not even a scan request. The Sniffle backend implements only receive commands (channel, PHY, filters, aux follow, version probe). It has no code to transmit, advertise, scan actively or connect, and it switches Sniffle's connection following off. Any scan response it shows was requested by another device.
- **No connections, no content.** msdr never follows a connection, so it cannot see audio, messages, files or other data exchanged between devices.
- **Rotating addresses stay anonymous.** Phones and most modern devices change their address every few minutes. msdr treats those addresses as temporary. It does not link one address to the next, does not fingerprint payloads (the stable parts are shared by every device of the same model, so a fingerprint would mislabel strangers' devices), refuses labels on them, and hides them from the timeline unless asked.
- **No location from signal strength.** RSSI is shown as received and is never turned into a position or distance. Remote ID positions are what the drone claims, not a measurement.
- **Stays on this machine.** Nothing is uploaded. The live table is in memory until Stop or Clear. Device history ([history.md](history.md)) is off by default. When enabled (`"history": {"enabled": true}` in settings.json), it keeps per-minute summaries in `history.db` for 30 days, can be wiped with `POST /api/history/clear`, and is not included in backups. msdr does not record BLE packets.

Use it to learn what is around you and to inspect your own devices. Other people's devices still belong to other people, so don't use it to follow or identify someone.

### Identities

Random addresses are session-scoped. A resolvable or non-resolvable private address (`identity: "rpa"` / `"nrpa"`) rotates every few minutes and is never treated as a device identity beyond the current table. Only `public` and `static` addresses are stable.

## How the HCI backend works

An external USB dongle that macOS has not claimed (no transport driver attached, the normal case for a second controller) can be opened with libusb and driven with HCI commands directly.

The helper `python -m msdr.ble.capture` claims USB interface 0, sends `HCI_Reset`, reads the controller version and LE features, then issues `LE_Set_Scan_Parameters` with **scan type 0x00 (passive)** and `LE_Set_Scan_Enable` with duplicate filtering off. Passive scanning is receive-only by definition, so no SCAN_RSP data is collected.

The helper writes one JSON line per advertising report on stdout and JSON status events on stderr (see [Helper protocol](#helper-protocol)). `BleSupervisor` (`msdr/ble/supervisor.py`) runs it as a subprocess, as the Wi-Fi supervisor does, and feeds an in-memory `AdvertiserTable`.

Scan interval and window are both 10 ms (100 % duty). The controller schedules advertising channels 37, 38 and 39 itself. HCI reports carry no channel number, so the UI describes coverage as "all three primary advertising channels, controller-scheduled".

What a Bluetooth 4.x controller can and cannot see:

| Item | Visible |
|---|---|
| Legacy advertising PDUs (ADV_IND, ADV_NONCONN_IND, ADV_SCAN_IND, ADV_DIRECT_IND) on the 1M PHY | yes |
| Advertiser address and address type, RSSI, raw AD payload (up to 31 bytes) | yes |
| Scan responses | no (would require active scanning) |
| Extended advertising (Bluetooth 5), 2M PHY, Coded PHY long range | no |
| Per-report channel number, exact timestamp from the radio | no (host receive time is used) |
| Bluetooth Classic inquiry/pages | no, by design |

Legacy Remote ID (ASTM F3411 / ASD-STAN over BLE 4 advertising, service data UUID 0xFFFA, application code 0x0D) is visible on this backend. The Bluetooth 5 long-range variant needs the Sniffle backend.

The table flags such advertisers (`remote_id: true`). Every trusted advertisement carrying that service data is also decoded live and fed into the shared Remote ID `Tracker` (`AdvertiserTable.ingest` into its optional `.remoteid` sink). The result is available at `GET /api/remoteid/live` and in Discover's "Remote ID" tab. See [Remote ID](remote-id.md#live).

## Helper protocol

`python -m msdr.ble.capture --list` prints one JSON object per Bluetooth-class USB device and exits 0:

```
{"vid","pid","product","manufacturer","serial","bus","address","known"}
```

`known` is true for ids in `msdr.ble.hci.KNOWN_DONGLES`.

`python -m msdr.ble.capture [--usb-id VID:PID]` captures until SIGTERM/SIGINT.

stdout carries one JSON line per LE Advertising Report:

```
{"ts": <unix float>, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public"|"random", "evt": "ADV_IND"|"ADV_DIRECT_IND"|"ADV_SCAN_IND"|"ADV_NONCONN_IND"|"SCAN_RSP", "rssi": <int dBm or null>, "data": "<hex>"}
```

stderr carries JSON events:

- `{"event":"ready","vid","pid","product","bus","address","hci_version","hci_revision","lmp_version","lmp_subversion","manufacturer_id","manufacturer_name","le_features":"<hex>","scan_type":"passive","scan_interval_ms":10,"scan_window_ms":10,"helper_version"}`
- `{"event":"stats","reports":<n>}`, roughly once per second
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

On SIGTERM the helper disables scanning and releases the interface before exiting, so the dongle is left idle.

## msdr side

Modules:

- `msdr/ble/hci.py`: HCI command/event encoding, event reassembly, the LE Advertising Report parser, `KNOWN_DONGLES`, `is_bluetooth_class()`. Pure functions; pyusb is not imported at module import time.
- `msdr/ble/adv.py`: AD structure parser (flags, names, 16/32/128-bit service UUIDs, service data, manufacturer data with a small company-id name table, TX power, appearance) and `classify_address()`.
- `msdr/ble/capture.py`: the HCI helper CLI above.
- `msdr/ble/sniffle_proto.py` and `msdr/ble/sniffle_capture.py`: the Sniffle backend (see [Sniffle backend](#sniffle-backend-sonoff-zbdongle-p)).
- `msdr/ble/observations.py`: `AdvertiserTable`, with bounded per-address entries (oldest `last_seen` evicted past `max_entries`, default 2000), RSSI history, event-type counts and distinct payload history. It keeps a live feed deque of the last 500 reports with a sequence number (`live_since(after, limit)`), and offers `rows(q, sort, order, offset, limit)`, `details(addr)`, `clear()` and `snapshot_changed_since(t)`. Entries also carry `phy`, `channel` and `extended`. These are filled by the Sniffle backend only; the HCI backend always reports `null`, `null`, `false`.
- `msdr/ble/supervisor.py`: `BleSupervisor(helper_command=None, usb_id=None, backend="hci", serial_port=None, baud=None, phy=None, channel=None, on_status=None)` with `start()`, `stop()`, `devices()`, `status()`, `running` and `table`.

Supervisor health states: `dependency_missing`, `no_device`, `claimed` (the OS owns the dongle; HCI only), `no_le` (HCI only), `firmware_mismatch`, `wrong_baud` (Sniffle only), `stopped`, `starting`, `receiving`, `stale` (no report for 10 s) and `failed`.

A crash is retried with the same backoff as Wi-Fi. Exit codes 1, 2, 4, 5 (HCI) and 1, 4, 6, 7 (Sniffle) are terminal states, not retries. A mid-run serial disconnect (exit 3) retries like any other crash.

### Settings

Default block: `ble: {enabled: false, backend: "hci", usb_id: null, serial_port: null, baud: null, phy: "1m", channel: "37", helper_command: null}`

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
| `GET /api/ble/summary?window_s=60` | device summary (see [Device summary](#device-summary)) |
| `GET /api/ble/names` | Bluetooth SIG names status |
| `POST /api/ble/names/refresh` | download SIG names |

`GET /api/ble/status` also carries `backend` (`"hci"|"sniffle"`). For the Sniffle backend it adds the configured `phy` and `channel`, plus a `coverage` sentence computed for exactly that configuration.

The websocket message `{"type": "ble", "status": {...}, "changed": [rows]}` is sent on every supervisor status publish and can be coalesced, like `wifi`.

### Row shape

`AdvertiserTable` summary rows:

```
addr, addr_type, identity (public|static|rpa|nrpa), name, company_id, company,
connectable, service_uuids (list of strings), remote_id, rssi (latest int or null),
reports, first_seen, last_seen, evt_types ({name: count}), phy, channel, extended,
sensor, service_names, apple
```

- `phy`, `channel`, `extended` are `null, null, false` from the HCI backend.
- `sensor` is `null` unless the latest report decoded as one of the [sensor formats](#sensors).
- `service_names` maps advertised UUIDs to names (built-in plus the downloaded SIG list).
- `apple` is the Continuity summary or `null`.

Details add:

```
flags, tx_power, appearance, service_data ({uuid: hex}), manufacturer_data ({company_id: hex}),
rssi_history ([[ts, rssi], ...]), payloads ([{hex, first_seen, last_seen, count}], last 8 distinct),
recent ([{ts, evt, rssi, length}], last 20), sensor_history
```

`sensor_history` holds the latest 20 decoded sensor reports, in the same shape as `sensor`, each with its own `ts`.

Live feed item:

```
{seq, ts, addr, identity, evt, rssi, name, company, length, remote_id, sensor, phy, channel, extended}
```

## Sensors

`msdr/ble/sensors.py` has explicit parsers for published sensor beacon formats. They run against the service and manufacturer data of every advertisement. An encrypted BTHome packet is reported as `encrypted: true` with no readings. An unrecognised object id inside a packet stops decoding of the rest of that packet, because guessing a length would misalign everything after it.

The `sensor` shape is `{protocol, readings: {name: {value, unit}}, encrypted, ts}`. `AdvertiserTable` keeps the latest one per advertiser plus a bounded history (the last 20).

Supported formats. Test fixtures in `tests/ble/test_sensors.py` come from each format's published specification (source URLs and fetch date are in the module docstring of `msdr/ble/sensors.py`).

| Protocol | Carrier | Readings |
|---|---|---|
| BTHome v2 | service data 0xFCD2 | `temperature, humidity, battery, pressure, illuminance, door, motion` (unencrypted objects only) |
| Ruuvi RAWv2 (data format 5) | manufacturer id 0x0499 | `temperature, humidity, pressure, battery, tx_power, movement_counter` |
| iBeacon | manufacturer id 0x004C, type 0x02 | `uuid, major, minor, measured_power` |
| Eddystone UID/URL/TLM | service data 0xFEAA | UID: `tx_power, namespace, instance`; URL: `tx_power, url`; TLM: `battery_voltage, temperature, adv_count, uptime` |
| pvvx/ATC custom thermometer | service data 0x181A | `mac, temperature, humidity, battery, battery_voltage, counter` (ATC1441 layout, 13 bytes), or the same plus `flags` (pvvx layout, 15 bytes); the two are told apart by length alone |

In Discover, BLE tab (`ble-view.js`), the Advertisers table has a Sensor column: a protocol chip, "<Protocol> (encrypted)" when encrypted, and the full readings in the chip's `title`. The details pane has a "Sensor" section that lists every reading in words, or says the payload is encrypted and cannot be decoded without the bind key.

Sensor readings are not yet folded into the per-minute history buckets. The `buckets` table in `history.db` holds one numeric `sig_*` series per device and nothing else. Adding readings would need its own table and a second accumulator alongside `MinuteAccumulator`.

## Device summary

Above the advertiser table, a strip counts the devices heard in the last minute by kind of device, for example: "About 44 devices heard in the last minute · Apple 18 (iPhone / iPad / Mac nearby 14, AirPods / Beats 3, Find My 1) · Microsoft 5 · ...".

Each advertiser falls in exactly one group: drones (Remote ID), sensor beacons (by format), Apple (split by Continuity message type), its maker's name, or Unknown. Makers beyond the six largest fold into "Other makers". Counts are approximate, because a phone that rotates its address during the minute counts twice.

Only the kind of device is used. Activity and state fields (Nearby Info's screen, lock or call state and similar) are not decoded, not even as an aggregate:

- In a home or a quiet street, a count like "1 device on a call" points at one person.
- Counts over time reveal routines.
- Those fields are reverse-engineered and change between iOS versions.

API: `GET /api/ble/summary?window_s=60` (10-3600) returns `{window_s, total, groups: [{label, count, subgroups: [{label, count}]}]}`.

## Bluetooth SIG names

msdr has short built-in tables of company and service names (`msdr/ble/adv.py`). Setup → BLE → **Download** fetches the Bluetooth SIG's full lists from bitbucket.org/bluetooth-SIG/public into `<data folder>/ble/sig_assigned_numbers.json`. **Update** refreshes it. The lists are:

- company identifiers (about 4,000),
- 16-bit service UUIDs (SIG and member, e.g. Tile, Samsung),
- appearance values ("Wearable Audio Device: Earbud").

The SIG marks these files proprietary with no licence granted, so msdr does not bundle them; the download is for your own use under the SIG's terms. Built-in short names win where both exist ("Apple", not "Apple, Inc."). A failed download keeps the previous file.

API:

- `GET /api/ble/names` returns `{downloaded, companies, services, appearance, fetched_at, source}`.
- `POST /api/ble/names/refresh` downloads; it returns 502 with the error when it fails.

Rows carry `service_names` (built-in plus SIG) and details carry `appearance_name`.

## Apple Continuity

Apple devices advertise "Continuity" messages in manufacturer data (company 0x004C) as a list of type/length messages. `msdr/ble/apple.py` names each message type (Handoff, Nearby Info, AirPlay target, Find My, AirDrop, HomeKit, AirPods / proximity pairing, and others; unknown types keep their hex code). For AirPods/Beats proximity-pairing messages (type 0x07) it also decodes the model and battery levels (left, right, case, in 10 % steps, plus charging flags).

Rows show "Apple device · Handoff, Nearby Info" or the AirPods model. The details pane lists the types and the battery line. Types accumulate per address, so a rotating address starts empty again.

Apple does not publish this format, so decoding is best effort and can change with Apple firmware. The sources are:

- the type list: the furiousMAC "continuity" project and Martin et al., "Handoff All Your Privacy" (PoPETs 2019);
- the proximity-pairing layout: the OpenPods app;
- HomeKit types: the HAP specification's length byte (low 5 bits).

Deliberately not decoded, in line with [What msdr does and does not see](#what-msdr-does-and-does-not-see):

- Fields describing what a person is doing (Nearby Info activity, screen or call state; Nearby Action details).
- Addresses carried inside messages (an AirPlay target's IP address).
- Find My keys.

## UI contract

### Setup

The **BLE receiver** card has the Enable toggle, the backend selector and its fields, detected devices or ports with rescan, a status line, and controller facts once running. For `firmware_mismatch`, `wrong_baud` and `no_device` it shows a plain explanation. msdr never flashes firmware and never guesses a baud rate beyond trying both.

### Discover, BLE tab (`ble-view`)

- Start, Stop and Clear buttons and state words.
- Two sub-tabs: "Advertisers" (sortable table: Address, Identity, Name, Company, Services, RSSI, Reports, Last seen) and "Live" (newest-last feed, using the same merge helper as Wi-Fi).
- With the Sniffle backend, the Identity cell carries a compact PHY/channel chip (e.g. "CODED ch37", "1M ch6 ext"). It is absent with the HCI backend.
- Clicking a row opens a details pane: header chips (identity kind, connectable, Remote ID), Identity / Advertisement / Reception fact lists (with PHY, Channel and Extended advertising facts when the Sniffle backend reported them), an RSSI sparkline when the value varies, distinct payloads and recent reports.

### Coverage statement

Every summary states its coverage from the server's `status.coverage`, never from a hardcoded string and never as "all". For the HCI backend: "legacy advertising, 1M PHY, channels 37-39 controller-scheduled; no extended advertising, no scan responses". For Sniffle it reflects the configuration, for example "Coded PHY on channel 37 only; legacy and extended advertising (aux-channel chaining followed); no scan responses, no connection following", or "...channels 37-39 (dwell hop)..." when the channel is "hop".

### Station status strip

`receiver-status-strip.js` shows a compact, always-visible lamp beside the main SDRconnect strip for each enabled Wi-Fi or BLE receiver (hidden when disabled). It has a state dot and channel/PHY, and its tooltip shows packet rate and time since the last packet. It polls `/api/wifi/status` and `/api/ble/status` and adds no height to the strip (`tests/e2e/test_layout_gate.py` enforces a 100-220 px range).

BLE uses the context colour `--context-ble`, a different hue from `--context-wifi`.

## Sniffle backend (SONOFF ZBDongle-P)

The hardware is a genuine SONOFF ZBDongle-P (CC2652P) running a matching Sniffle release build. A person must flash it before msdr can use it. msdr never flashes firmware. Install one of:

- `sniffle_cc1352p1_cc2652p1.hex` for a CP2102N bridge (2 Mbaud);
- `sniffle_cc1352p1_cc2652p1_1M.hex` for a CP2102 (non-N) bridge from the 2022 shortage (921600 baud).

Flashing the wrong variant risks locking the bootloader.

### Helper commands

`python -m msdr.ble.sniffle_capture --list` prints one JSON object per CP210x-class serial port (VID `10c4`, PID `ea60`) and any other serial port it finds (`known: false`), then exits 0.

`python -m msdr.ble.sniffle_capture [--port PATH] [--baud N] [--phy 1m|2m|coded] [--channel 37|38|39|hop]` captures until SIGTERM/SIGINT.

- Without `--baud`, it tries 2000000, then 921600.
- `--phy` sets the primary-channel PHY. Primary advertising (37/38/39) is only ever 1M or Coded, so `--phy 2m` receives nothing. 2M appears only on secondary channels, reached through aux follow from a 1M or Coded primary.
- `--channel hop` dwells on 37/38/39 one second at a time, resending the channel-select command on the already-open connection. It does not use Sniffle's own MAC/IRK-filtered `hop3`, which needs a known target address that msdr does not have in promiscuous mode. Every report still carries its true `channel` from the firmware, regardless of the configured primary channel.

stdout uses the same JSON report shape as `msdr.ble.capture`, plus:

- `phy` (`"1m"|"2m"|"coded"`),
- `channel` (int, the real per-packet channel),
- `extended` (bool),
- `aux` (`{chan, phy, offset_us}` when the PDU carried an auxiliary pointer, else `null`).

The stderr `ready` event additionally carries `firmware_version` ("1.11.0"), `api_level`, `port`, `baud` (whichever candidate answered), `phy`, `channel` (the configured mode: `"37"`, `"38"`, `"39"` or `"hop"`) and `extended` (always `true`, because aux-advertising-follow is always enabled).

Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | no CP210x serial port (or the requested one) found |
| 3 | serial I/O error (including a mid-run unplug; not terminal, retries like any other crash) |
| 4 | pyserial missing |
| 6 | firmware does not answer the marker/version handshake as Sniffle (`firmware_mismatch`: framed messages arrived but never matched, most often the wrong Sniffle build for this dongle) |
| 7 | no framed message decoded at any candidate baud (`wrong_baud`: a real baud mismatch corrupts every byte, so nothing survives the base64/CRLF framing) |

### Detection and receive-only design

Detection sends the marker command and waits for a matching `MarkerMessage` reply, then sends the version command and waits for a `VersionMeasurement` reply, at each candidate baud in turn. This is the same sync/version exchange that `SniffleHW.__init__` and `probe_fw_version` use upstream (see `THIRD_PARTY_NOTICES.md`).

`msdr/ble/sniffle_proto.py` implements only the channel, PHY, RSSI, CRC-validation, MAC-filter-clear and aux-advertising-follow commands, plus the marker and version probes. It always disables connection following (`cmd_follow(False)`, never re-enabled). It does not implement Sniffle's active-scan, connect, advertise/transmit, address/IRK or channel-map/interval-preload commands at all.

### Test fixture

`tests/ble/fake_sniffle.py` is an in-process fake that speaks the real wire framing. It is injected as the serial object of `sniffle_capture.capture(..., ser=...)`, with no OS pty, so it also runs where pty allocation is sandboxed. It covers `ok`, `firmware_mismatch`, `wrong_baud` and `unplug`. Its `main()` also runs as a real subprocess (`FAKE_SNIFFLE_MODE` env var) for `BleSupervisor`-level tests, the role `tests/ble/fake_helper.py` plays for the HCI backend.

## Hardware notes

SONOFF ZBDongle-P with Sniffle (one unit, tested 2026-10-08):

- `--list` shows it as VID `10c4` PID `ea60` with SONOFF's own product string ("Sonoff Zigbee 3.0 USB Dongle Plus"), which does not say whether the bridge is CP2102 or CP2102N.
- The CP210x vendor request 0xFF / wValue 0x370B (the Linux cp210x driver's GET_PARTNUM, read-only) returned 0x20, meaning CP2102N QFN28. The 2 Mbaud `sniffle_cc1352p1_cc2652p1.hex` was therefore flashed with `cc2538-bsl.py --bootloader-sonoff-usb -ewv`, with no need to open the case.
- Verified: handshake at 2000000 baud with firmware 1.11.0; 1M reception; channel hop with even counts on 37/38/39; unplug mid-capture ending with a `serial` error; Setup Start/Stop.
- Not yet verified on hardware: Coded PHY, extended/aux advertising (and so 2M), and an hour of capture alongside SDRconnect with bounded memory.

BCM20702A0 (Broadcom, Bluetooth 4.0):

- Works from its ROM firmware without the Linux patch file; HCI version 6, LE features byte 0x01.
- Reports arrive at a few hundred per second in a dense environment.

HCI dongle detection:

- Vendor id 0x0a5c (Broadcom), 0x19ff:0x0239 (Rocketfish/Dynex BCM20702A0) and 0x0b05:0x17cb (ASUS BT400) are in `KNOWN_DONGLES`.
- Any device of USB class 0xE0/0x01/0x01 is still listed and can be selected.
- If macOS has claimed the device (state `claimed`), msdr attempts no workaround. It never changes host-controller switching, firmware or system Bluetooth settings.

See also: [Wi-Fi](wifi.md), [Remote ID](remote-id.md), [Device history](history.md), [Watch rules](rules.md).
