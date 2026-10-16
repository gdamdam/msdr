# Passive Wi-Fi reception (AWUS036ACH)

msdr can passively capture Wi-Fi beacons and probe responses with an Alfa AWUS036ACH adapter, build a table of the networks heard, record raw PCAP sessions and run a channel survey. This page is for anyone setting up the capture helper or working on the Wi-Fi code. For day-to-day use see the [user guide](user-guide.md).

The Wi-Fi receiver is independent of the SDRconnect receiver, and capture is receive-only.

## Set up

The adapter is driven by a separate helper program, `msdr-wifi-capture`, which msdr starts as a subprocess. It is installed in its own directory and virtualenv.

1. Install libusb with Homebrew (`brew install libusb`).
2. Create the helper:

   ```sh
   mkdir -p ~/dev/radio/rtl8812au-helper && cd ~/dev/radio/rtl8812au-helper
   git clone https://github.com/xen-proc/rtl8812au-macos upstream
   git -C upstream checkout 347e9f172750e0c0b7c45b6dcfef7e780d70d144
   python3 -m venv .venv && .venv/bin/pip install -r upstream/requirements.txt -e upstream -e wrapper
   brew list libusb   # libusb must already be installed (Homebrew)
   .venv/bin/msdr-wifi-capture --list
   ```

3. In Setup, open the Wi-Fi receiver card and turn on Wi-Fi. The card shows the helper path, the detected adapter, the channel, and (after Start) status with frame counts, rate, drops, last frame and any error.
4. In Discover, open the **Wi-Fi** tab and press Start. Pick a channel first, since the adapter listens on one fixed 20 MHz channel at a time.

msdr finds the helper in this order, using the first one that exists:

1. the `wifi.helper_path` setting (path to `msdr-wifi-capture`),
2. `msdr-wifi-capture` on `PATH`,
3. `rtl8812au-helper/.venv/bin` beside the msdr checkout (for example `~/dev/SDR/rtl8812au-helper` beside `~/dev/SDR/msdr`),
4. `~/dev/radio/rtl8812au-helper/.venv/bin`.

A helper whose venv was moved is reported as "venv is broken", with the rebuild command, rather than as missing.

### Settings

| Setting | Type | Default |
|---|---|---|
| `wifi.enabled` | bool | `False` |
| `wifi.helper_path` | `str \| None` | none |
| `wifi.channel` | int | `6` |
| `wifi.record_max_mb` | int | `200` |
| `wifi.record_max_minutes` | int | `60` |
| `wifi.retention_days` | int | `14` |
| `wifi.sensing_source` | `str \| None` (MAC address) | none, sensing off |

## What you see in the UI

- **Setup:** the Wi-Fi receiver card described above.
- **Discover, Wi-Fi tab:** the network table (SSID or "hidden", BSSID, channel, security, signal when available, first and last seen, packets), with search, a channel filter, stable row order and selection. The details pane shows capabilities and IEs, reception history, recent broadcasts, the raw frame on demand and receiver provenance. The table is labelled "Networks observed on channel N since HH:MM", never an inventory.
- **Discover, Channel survey tab:** see [Wi-Fi channel survey](history.md#wi-fi-channel-survey).
- **Discover, Activity tab:** a packets-per-channel-over-time heatmap (see [Wi-Fi activity](#wi-fi-activity)).
- **Review:** Wi-Fi sessions with health, size, duration and PCAP export.

## Licensing boundary between msdr and the helper

msdr is GPL-3.0-only. The RTL8812AU userspace driver (`xen-proc/rtl8812au-macos`, GPL-2.0-only, pinned at commit `347e9f172750e0c0b7c45b6dcfef7e780d70d144`, package 0.1.1) and a small wrapper program live in their own directory and virtualenv and are installed separately.

- msdr never imports, links, vendors or copies driver code. It only starts the helper as a separate program and reads standard pcap from it.
- GPL-2.0-only code cannot be combined with GPL-3.0-only code. This document does not judge whether the arrangement is legally an aggregate rather than a combined work.
- msdr does not redistribute the helper, its firmware or any of its code. The firmware images carry no separate redistribution terms beyond the GPL-2.0 header of the C file they were extracted from (upstream `firmware/README.md`).

## Helper command line

```
msdr-wifi-capture --list
msdr-wifi-capture --channel <n> [--no-reset] [--max-seconds <s>] [--keep-tx-dma | --clear-tx]
```

- **Default:** after bring-up, the two TX DMA bits are cleared (`REG_CR` 0x06ff to 0x06fa on this adapter). `--keep-tx-dma` skips that. `--clear-tx` also clears `MACTXEN`, which stops reception on this chip.
- **`--list`:** prints one JSON object per line on stdout, then exits 0. No device is opened.

  ```
  {"vid": 3034, "pid": 34834, "bus": 1, "address": 5, "product": "802.11n NIC", "serial": "123456", "supported": true}
  ```

- **Capture mode:** opens the first `0bda:8812` adapter (upstream has no selector, so one adapter per Mac), tunes the fixed 20 MHz channel and writes a classic pcap stream (`LINKTYPE_IEEE802_11_RADIOTAP`, 127) to **stdout**, flushed after every packet. Nothing else is written to stdout.
- **Diagnostics** go to stderr only, one JSON object per line:
  - `{"event": "ready", "channel": 6, "frequency_mhz": 2437, "helper_version": "...", "upstream_commit": "347e9f1...", "host_tx_dma_disabled": true, "mac_tx_enabled": true, "reg_cr_before": "0x06ff", "reg_cr_after": "0x06fa"}`
  - `{"event": "stats", "frames": 1234, "bad_fcs": 210, "usb_errors": 0, "elapsed_s": 30.0}`, every 5 s
  - `{"event": "error", "code": "open_failed" | "bringup_failed" | "usb_error", "message": "..."}`
  - `{"event": "stopped", "reason": "signal" | "max_seconds" | "error"}`
- **Stop:** SIGTERM or SIGINT stop cleanly (finish the current record, flush, emit `stopped`, release the USB interface). SIGKILL is tolerated: the adapter reopens on the next start (verified), though the last record may be cut.
- **Channel change:** stop and restart with a new `--channel`.
- **Exit codes:** 0 ok, 1 open failed, 2 bring-up failed, 3 USB error during capture.

### Radiotap fields written by the helper

| Field | Source | Meaning |
|---|---|---|
| TSFT | RX descriptor TSF | chip timer, microseconds; not wall clock |
| Flags | FCS present, bad-FCS from descriptor CRC flag | FCS is always appended |
| Rate | descriptor rate index | only legacy rates were seen |
| Channel | **configured** channel | not measured (see [Limitations](#limitations)) |
| Vendor namespace, OUI `00:00:00` sub-ns 0 "msdr-raw" | upstream's decoded PHY status, per frame | uncalibrated codes, never shown as dBm; layout below |

The msdr-raw payload, version 1, is eleven bytes. `packet_power_code` is unsigned, 0 to 255; every other field is a signed byte where `-128` means absent. A frame without a power code carries no payload. `radiotap.msdr_raw()` decodes it, and the activity heatmap and signal history use its `packet_power_code`. Payloads of any other length (older helpers wrote raw descriptor bytes) fall back to the first vendor byte.

| Byte | Field |
|---|---|
| 0 | version, `1` |
| 1 | `packet_power_code`, unsigned |
| 2 | `signal_evm_code` |
| 3, 4 | `antenna_a`, `antenna_b` |
| 5, 6, 7 | chain 0 `gain_code`, `snr_code`, `evm_code` |
| 8, 9, 10 | chain 1 `gain_code`, `snr_code`, `evm_code` |

pcap record timestamps are host wall clock at USB read time.

## Passive-only design

- msdr exposes no injection, deauthentication, probing, association or joining.
- **Upstream transmit paths** are never reachable from the helper wrapper: `tx.py` `Injector.send`, `Injector.send_many`, `build_txdesc`; `mac.py` `init_tx`, `init_tx_dma`; `phy.py` `config_tx_power`; `dot11.py` `beacon()`, `probe_request()`, `deauth()`; `tools/inject.py`, `tools/deauth.py`.
- The wrapper imports only `session`, `monitor`, `transport`, `phy`, `regs` and `phy_status`.
- **MAC TX engine:** upstream's monitor setup enables it (`MACTXEN`, `TXDMA_EN`, `HCI_TXDMA_EN` in `REG_CR`). On this adapter, clearing `MACTXEN` stops reception entirely (channel 6: 5,958 frames in 20 s before, 1 frame in 40 s after), so it is left set.
- **TX DMA bits:** by default the wrapper clears the two TX DMA bits after bring-up. This is a volatile register write, reset at the next power-on, and it closes the host-to-radio transmit path. The `ready` event reports `host_tx_dma_disabled` and `mac_tx_enabled`.
- **Remaining caveat:** with `MACTXEN` set, the MAC can still answer frames addressed to its own MAC address with a hardware ACK/CTS. The adapter never transmits otherwise, so that address is not advertised. This was not measured with a second receiver.

## msdr side

### Package `msdr/wifi/`

- `pcap.py`: incremental pcap reader for a byte stream (pipe or file), with bounded record size, tolerant of a truncated final record.
- `radiotap.py`: parses the fields above; unknown fields are skipped by their declared alignment and size.
- `dot11.py`: parser for beacons (subtype 8) and probe responses (subtype 5). It extracts BSSID, SSID (hidden when length is 0 or all zero bytes), DS Parameter Set channel, supported rates, RSN and WPA vendor IEs (AKM and cipher suites, PMF), HT/VHT/HE capability presence, country, beacon interval and capability bits (privacy). Every IE is kept as `(id, ext_id, bytes)` for the details view. Malformed or truncated frames return a typed error and never raise.
- `observations.py`: aggregates by BSSID: SSID history, channel from the DS IE (falls back to the configured channel, marked as such), security summary, first and last seen, counts (beacons, probe responses, bad FCS), a bounded signal history of raw codes when present, and a bounded ring of recent frames.
- `supervisor.py`: starts and stops the helper subprocess (stdin closed, stdout = pcap, stderr = JSON events), with a bounded queue between reader and aggregator (drops are counted). It restarts on a crash only when the user asked for receiving, and terminates cleanly on stop and app shutdown.
- `sessions.py`: explicit recording of the raw pcap stream into bounded rotating files under the recordings root (`wifi/<session-id>/part-NNN.pcap`), with size and duration limits and retention. Session metadata JSON sits beside them. Ordinary packets never enter `DecodeEventRepo`.
- `replay.py`: imports a pcap file and runs it through the same parser and aggregator (development and offline inspection).
- `survey.py`: the channel survey ([Wi-Fi channel survey](history.md#wi-fi-channel-survey)).
- `activity.py`: the activity heatmap accumulator ([Wi-Fi activity](#wi-fi-activity)).

Supervisor health states: `unavailable` (helper not installed), `dependency_missing`, `no_device`, `stopped`, `starting`, `receiving`, `stale` (no frames for 10 s) and `failed`.

Remote ID plugs in at the parser stage (beacon vendor IE, OUI `FA:0B:BC`, type `0x0D`). The trusted (good-FCS) path of `NetworkTable._ingest` feeds every vendor IE it finds into the shared live `Tracker` through an optional `.remoteid` sink, the same way it feeds device history. Capture and sessions do not change, and nothing is persisted through `DecodeEventRepo`. See [Remote ID](remote-id.md#live).

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/wifi/sensing` | sensing for `wifi.sensing_source`: latest per-second row, presence, crossing, breathing; `enabled: false` when no source is set (see [Sensing](#sensing)) |
| GET | `/api/wifi/status` | health, channel, frames, rate, drops, last frame, error, helper info; works while `wifi.enabled` is false (`state: "stopped"`, `enabled: false`) |
| GET | `/api/wifi/devices` | helper `--list` output; also works while disabled |
| POST | `/api/wifi/start` `{channel}` | start capture; 409 `"Enable Wi-Fi in Setup first"` while disabled |
| POST | `/api/wifi/stop` | stop capture; always allowed (idempotent cleanup) |
| GET | `/api/wifi/networks?q=&channel=&offset=&limit=` | paginated observation rows |
| GET | `/api/wifi/live?after=&kind=all\|management\|control\|data&limit=` | frames received since sequence `after` (bounded ring of 500, every frame type) |
| GET | `/api/wifi/networks/{bssid}` | details: IEs, history, recent frames |
| GET | `/api/wifi/networks/{bssid}/frames/{n}` | raw frame hex on demand |
| POST | `/api/wifi/sessions/start` / `stop` | recording; 409 while disabled |
| GET | `/api/wifi/sessions` | saved sessions for Review |
| GET | `/api/wifi/sessions/{id}/export.pcap` | concatenated pcap for Wireshark |
| POST | `/api/wifi/import` (file) | fixture or offline pcap replay into a session; 409 while disabled |
| GET | `/api/wifi/activity?minutes=&bucket=` | activity heatmap rows (see [Wi-Fi activity](#wi-fi-activity)) |
| GET | `/api/wifi/vendors` | vendor-name download status |
| POST | `/api/wifi/vendors/refresh` | download vendor names |
| GET | `/api/wifi/summary?window_s=60` | network summary tiles |
| POST | `/api/wifi/survey/start`, `/stop`; GET `/api/wifi/survey` | channel survey ([history.md](history.md#wi-fi-channel-survey)) |

Only the routes that operate the receiver or recorder (start, sessions start/stop, import) refuse with 409 `"Enable Wi-Fi in Setup first"` while `wifi.enabled` is false. Every read works, including `status` and `devices`, which the Setup card needs in order to show "stopped" in the first place.

Browsers get a `wifi` websocket message at most once a second, carrying status and changed row summaries (a coalescable type): `{type: "wifi", status, changed: [<networks item>, ...]}`.

### Response shapes

`GET /api/wifi/status` (also the `status` field of the `wifi` websocket message):

```
{
  state, enabled (settings.wifi.enabled), wanted, channel (None while
  stopped), configured_channel (settings.wifi.channel: what Start would
  tune to), frequency_mhz,
  frames, frame_rate (Hz), drops, bad_fcs, parse_errors, oversize_dropped,
  evictions (networks aged out of the bounded table),
  last_frame_at, last_error, last_exit_code, retry_attempt, retry_at,
  helper: {path, upstream_commit, host_tx_dma_disabled, mac_tx_enabled} | null,
  device: {product, serial, bus, address} | null,  # from the last --list
  recording: {session_id, started_at, bytes, frames} | null,
  network_count,
}
```

`parse_errors` combines radiotap-level and dot11-level errors. Only malformed or truncated beacons and probe responses count. An `unsupported_frame` (for example a control or data frame, which this parser never decodes) is not an error and is not counted.

`GET /api/wifi/networks`:

```
{
  items: [{
    bssid, ssid, ssid_hidden, channel, channel_source, channel_mismatch,
    security, signal: {kind: "raw" | "dbm", latest} | null,
    first_seen, last_seen, beacons, probe_responses, duplicates,
  }],
  total, configured_channel, since,  # since: first frame time of the table
}
```

`GET /api/wifi/networks/{bssid}` returns every `networks` item field plus:

```
{
  ies: [{id, ext_id, name, length, hex}],  # bytes always hex-encoded here
  rsn, wpa1, ht, vht, he, country, beacon_interval, capabilities,
  ssid_history, signal_history: [[ts, value]],
  recent: [{n, ts, subtype, length, bad_fcs}],
}
```

`GET /api/wifi/sessions`:

```
[{id, started_at, stopped_at, channel, frames, bytes, drops, imported, health}]
```

## Vendor names

Setup → Wi-Fi → **Download** fetches the IEEE MAC address registry (MA-L, MA-M and MA-S CSVs from standards-oui.ieee.org, about 5 MB). It keeps only prefix-to-organization names in `<data folder>/wifi/oui_vendors.json`. **Update** refreshes it. The file is not shipped with msdr. The most specific block wins (MA-S, then MA-M, then MA-L).

Rows, details and the survey's network list carry `vendor` and `local_admin`. A locally administered BSSID (bit 0x02 of the first byte, as in a router's derived guest/mesh BSSID or a random MAC) is shown as "locally administered" instead of a vendor. BLE devices with a public address get the same lookup.

API: `GET /api/wifi/vendors` returns `{downloaded, prefixes, fetched_at, source}`. `POST /api/wifi/vendors/refresh` refreshes it (502 with the error when the download fails).

## Network summary and survey verdict

Above the Networks table, tiles summarise the networks heard on the current channel in the last minute:

- probable access points (BSSIDs grouped by the same heuristic as "group by access point"),
- by band,
- by security (Open, WEP, WPA1, WPA2, WPA2/WPA3, WPA3, Enterprise, OWE),
- by maker (needs the vendor names download; locally administered and unknown listed last),
- hidden.

`GET /api/wifi/summary?window_s=60` (10-3600) returns `{window_s, total, access_points, hidden, local_admin, bands, security, makers}`, each list as `[{label, count}]`. This is infrastructure only: msdr never tracks client devices.

The Channel survey tab shows a verdict from the channels listened to so far:

- busiest channel (frames per listened second),
- most networks,
- least congested per band (fewest frames/s, then fewest networks; only when the band has at least two surveyed channels),
- overlap (networks heard off their own channel).

It is a starting point for picking a router channel, not an interference measurement.

## Hidden networks and grouping

A hidden network beacons with an empty SSID. Every name a BSSID has ever revealed (in a beacon, or in a probe response to a client that knew the name) is kept in `revealed_ssids`, and the table shows such a row as "hidden (seen as <name>)".

The Networks tab's **Group** menu groups the rows on the current page in one of two ways:

- by network name (a hidden network joins its revealed name);
- by probable access point: BSSIDs equal apart from the locally-administered bit and the last hex digit, which is how routers usually derive guest/mesh BSSIDs from their base MAC. This is a heuristic and is labelled "probably one access point".

## Wi-Fi activity

Discover → Wi-Fi → **Activity** is a heatmap of packets this adapter actually received. Activity is recorded into device history, so it needs `"history": {"enabled": true}` in settings.json (off by default, see [history.md](history.md)). With history off the heatmap stays empty.

- Columns are channels, 2.4 GHz then 5 GHz with a gap between, each headed by its number and 20 MHz centre frequency (e.g. "ch 6 / 2437 MHz").
- Rows are time, newest at top.
- Colour is packets received per *listened* second in that cell.
- An optional second mode shows the mean raw PHY signal code, labelled "uncalibrated signal code (not dBm)". It is offered only when at least one returned cell carries one. The code is the helper's uncalibrated `packet_power_code`; this adapter does not report calibrated dBm.
- A "?" help mark beside the controls states that the heatmap shows Wi-Fi packets this adapter received. It is not measured RF spectrum, continuous spectrum coverage, channel utilisation, or detection of non-Wi-Fi interference.

### Coverage rules

- A cell is **listened, no packets** (zero intensity, not hatched) when the receiver was tuned to that channel for part of that time bucket but heard nothing.
- A cell is **not listened** (hatched) when the receiver was never tuned to that channel during that time bucket: a channel hop, the receiver stopped, the helper starting or restarting, or a capture gap. Every `(bucket, channel)` pair the server never recorded simply has no row, and nothing is interpolated across unobserved channels or times.
- It works unmodified with fixed-channel reception and with the channel survey (`msdr/wifi/survey.py`). A survey's hops are ordinary `start()` calls, so each visited channel accumulates its own listened seconds and packets.
- Viewing the tab never starts or stops reception or hopping. It only reads the history database.

### How it is recorded

`msdr/wifi/activity.py`'s `ActivityAccumulator` is owned by `WifiSupervisor`, in the same way as its `MinuteAccumulator` (`self.history`):

- `_aggregate` adds one packet per ingested pcap record to `(int(record.ts), self.channel)`. The channel is the one the receiver was tuned to at receipt, never a frame's own DS Parameter Set channel.
- `_tick` credits elapsed wall time to the same key once a second, but only while `health_state()` is `receiving` or `stale` on a known channel. It caps credit at 2 s per tick, so a stalled event loop cannot invent coverage.
- `start()` (a plain restart or a survey hop) resets that crediting reference, so the starting or restarting interval is never credited as listened time.

### Persistence and retention

The same history flush that drains device-history buckets (`HistoryServiceMixin._history_flush` and `_sync_history_sinks`, which give the Wi-Fi supervisor an `activity` sink alongside `history`) writes into a `wifi_activity(second, channel, packets, listened_s, sig_sum, sig_n)` table. The table is created by `msdr/history/migrations/002_wifi_activity.sql`, lives in the same separate history database as `buckets` and `coverage`, and is upserted additively, so a flush boundary never loses or double-counts a second.

Per-second, per-channel rows are bulky (about 86,400 a day per channel), so retention is its own setting, `settings.history.activity_retention_hours` (default 48, range 1-720). It is pruned alongside, but independently of, the `retention_days` retention of `buckets` and `coverage`.

### Activity API

`GET /api/wifi/activity?minutes=30&bucket=` takes a bucket of 1, 5, 15, 60 or 300 seconds, auto-chosen so the range stays at or under 360 rows (like the bucket choice of `history_timeline`). It flushes pending history first, then returns:

```
{
  start, end, bucket_s,
  channels: [<observed channels in range, sorted>],
  rows: [[t, channel, packets, listened_s, sig_mean | null], ...],  # listened_s > 0 only
  max_rate,  # highest packets-per-listened-second across the returned rows
}
```

The UI polls this at most every 5 s, only while the Activity tab is visible.

## Sensing

With `wifi.sensing_source` set to one transmitter's address (normally your own router), `msdr/wifi/sensing.py` turns that transmitter's per-frame `packet_power_code` into a motion and breathing sensor. A body in the path between the transmitter and the adapter changes the received power. Frames from every other address, and frames with a bad FCS, are dropped before anything is stored or counted. Only good-FCS management and data frames are used, since control frames carry no transmitter address.

- **Presence:** the spread of the last 2 s against a baseline, the quietest 60 s seen so far. Movement in the path raises it.
- **Crossing:** a dip in the mean of more than 6 codes lasting 0.3 to 3 s, as when someone walks through the line between the two radios.
- **Breathing:** the dominant 0.1 to 0.7 Hz rhythm of the last 30 s, in breaths per minute with a quality ratio. It needs at least 30 frames/s from the source. A router's beacons (about 10/s) report "needs dense source"; a second device sending 100 to 200 small frames/s on the same channel is needed.

All values are uncalibrated codes. No frame is ever transmitted to make sensing work.

## Verified setup (2026-10-02)

Results from the feasibility run:

- The adapter enumerates as `0bda:8812`, "802.11n NIC", bcdDevice 0, generic serial "123456".
- It is claimable as a normal user with Homebrew libusb 1.0.30, with no kernel extension.
- Channel 6: 2,545 frames in 30 s, 20 % bad FCS, 925 good beacons.
- Channel 36: 6,324 frames, with 795 of 795 beacon DS channels matching.
- Channel 149: 85 % bad FCS and only 10 beacons; poor.
- 10 minutes on channel 6: 45,918 frames, 76.5 fps mean, no errors, flat process memory, CPU under 1 %.
- Five start/stop cycles and reopen after SIGTERM and SIGKILL all worked. SDRconnect ran throughout and was unaffected.
- No HT/VHT-rate frames were received, TSFT was not written by upstream's capture tool. Signal is available as raw PHY codes only; since 2026-10-16 the helper writes them per frame (msdr-raw, above).

## Limitations

- One adapter per Mac (the first `0bda:8812` found), and one fixed 20 MHz channel at a time. There is no continuous hopping; the channel survey restarts the helper once per channel.
- The radiotap channel is the configured channel, not a measured one: a strong 5 GHz access point was received while tuned to 2.4 GHz channel 6. The DS Parameter Set channel is authoritative and is what the table shows. Frames whose DS channel differs are labelled.
- There is no calibrated signal strength. Trends use raw PHY codes, labelled as raw and relative, and are hidden when absent.
- Observed networks are those heard on the monitored channel during the session, not an inventory.
- Sensing watches every path between the source and the adapter, not one room: on 2026-10-16 people moving in the next room triggered presence and crossings as strongly as a walk across the line. Place the adapter so the line to the source crosses only the space you care about.

See also: [User guide](user-guide.md) · [History](history.md) · [Remote ID](remote-id.md) · [BLE](ble.md)
