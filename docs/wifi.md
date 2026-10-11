# Passive Wi-Fi reception (AWUS036ACH)

Reference for passive Wi-Fi capture with the AWUS036ACH, for developers and anyone setting up the helper. For day-to-day use see the [user guide](user-guide.md).

Status: design contract for 0.57.0. Code is built against the interfaces below; the "Verified setup" and "Limitations" sections record what was actually measured.

## Boundary: msdr and the capture helper

- msdr (GPL-3.0-only) never imports, links, vendors or copies driver code.
- The RTL8812AU userspace driver (`xen-proc/rtl8812au-macos`, GPL-2.0-only, pinned at commit `347e9f172750e0c0b7c45b6dcfef7e780d70d144`, package 0.1.1) and a small wrapper program live in their own directory and virtualenv, installed separately (see "Helper setup"). msdr finds it automatically next to the msdr checkout, e.g. `~/dev/SDR/rtl8812au-helper` beside `~/dev/SDR/msdr`.
- GPL-2.0-only code cannot be combined with GPL-3.0-only code. msdr only starts the helper as a separate program and reads standard pcap from it.
- Whether that is legally an aggregate rather than a combined work is a judgement this document does not make.
- msdr does not redistribute the helper, its firmware or any of its code. The firmware images carry no separate redistribution terms beyond the GPL-2.0 header of the C file they were extracted from (upstream `firmware/README.md`); msdr never bundles them.

### Helper command line (`msdr-wifi-capture`)

```
msdr-wifi-capture --list
msdr-wifi-capture --channel <n> [--no-reset] [--max-seconds <s>] [--keep-tx-dma | --clear-tx]
```

- Default: after bring-up the two TX DMA bits are cleared (`REG_CR` 0x06ff to 0x06fa on this adapter).
  `--keep-tx-dma` skips that; `--clear-tx` also clears `MACTXEN` and stops reception on this chip.

- `--list` prints one JSON object per line on stdout:
  `{"vid": 3034, "pid": 34834, "bus": 1, "address": 5, "product": "802.11n NIC", "serial": "123456", "supported": true}`,
  then exits 0. No device is opened.
- Capture mode opens the first `0bda:8812` adapter (upstream has no selector;
  one adapter per Mac in this release), tunes the fixed 20 MHz channel and
  writes a classic pcap stream (`LINKTYPE_IEEE802_11_RADIOTAP`, 127) to
  **stdout**, flushed after every packet. Nothing else is written to stdout.
- **Diagnostics go to stderr only**, one JSON object per line:
  - `{"event": "ready", "channel": 6, "frequency_mhz": 2437, "helper_version": "...", "upstream_commit": "347e9f1...", "host_tx_dma_disabled": true, "mac_tx_enabled": true, "reg_cr_before": "0x06ff", "reg_cr_after": "0x06fa"}`
  - `{"event": "stats", "frames": 1234, "bad_fcs": 210, "usb_errors": 0, "elapsed_s": 30.0}` every 5 s
  - `{"event": "error", "code": "open_failed" | "bringup_failed" | "usb_error", "message": "..."}`
  - `{"event": "stopped", "reason": "signal" | "max_seconds" | "error"}`
- **Stop:** SIGTERM or SIGINT stop cleanly (finish the current record, flush,
  emit `stopped`, release the USB interface). SIGKILL is tolerated: the
  adapter reopens on the next start (verified), the last record may be cut.
- **Channel change:** stop and restart with a new `--channel`.
- Exit codes: 0 ok, 1 open failed, 2 bring-up failed, 3 USB error during capture.

### Radiotap fields written by the helper

| Field | Source | Meaning |
|---|---|---|
| TSFT | RX descriptor TSF | chip timer, microseconds; not wall clock |
| Flags | FCS present, bad-FCS from descriptor CRC flag | FCS is always appended |
| Rate | descriptor rate index | legacy rates only were seen |
| Channel | **configured** channel | not measured; see below |
| Vendor namespace, OUI `00:00:00` sub-ns 0 "msdr-raw" | raw PHY status bytes | uncalibrated, never shown as dBm |

pcap record timestamps are host wall clock at USB read time.

### Passive-only guarantees

- **Upstream transmit paths** (never reachable from the helper wrapper): `tx.py` `Injector.send`, `Injector.send_many`, `build_txdesc`; `mac.py` `init_tx`, `init_tx_dma`; `phy.py` `config_tx_power`; `dot11.py` `beacon()`, `probe_request()`, `deauth()`; `tools/inject.py`, `tools/deauth.py`.
- **Wrapper imports** only `session`, `monitor`, `transport`, `phy`, `regs`, `phy_status`.
- **MAC TX engine:** upstream's monitor setup enables it (`MACTXEN`, `TXDMA_EN`, `HCI_TXDMA_EN` in `REG_CR`). Measured on this adapter: clearing `MACTXEN` stops reception entirely (channel 6: 5,958 frames in 20 s before, 1 frame in 40 s after), so it cannot be cleared.
- **TX DMA bits:** by default the wrapper clears the two TX DMA bits after bring-up (a volatile register write, reset at the next power-on), which closes the host-to-radio transmit path, and reports `host_tx_dma_disabled` and `mac_tx_enabled` in `ready`.
- **Remaining caveat:** with `MACTXEN` set, the MAC can still answer frames addressed to its own MAC address with hardware ACK/CTS. The adapter never transmits, so that address is not advertised; this was not measured with a second receiver.
- msdr exposes no injection, deauthentication, probing, association or joining.

## msdr side

### Package `msdr/wifi/`

- `pcap.py`: incremental pcap reader for a byte stream (pipe or file), bounded
  record size, tolerant of a truncated final record.
- `radiotap.py`: parse the fields above; unknown fields skipped by their
  declared alignment and size.
- `dot11.py`: management frame parser for beacons (subtype 8) and probe
  responses (5): BSSID, SSID (hidden when length 0 or all zero bytes), DS
  Parameter Set channel, supported rates, RSN and WPA vendor IEs (AKM and
  cipher suites, PMF), HT/VHT/HE capability presence, country, beacon
  interval, capability bits (privacy). Every IE kept as `(id, ext_id, bytes)`
  for the details view. Malformed or truncated frames return a typed error,
  never raise.
- `observations.py`: aggregate by BSSID: SSID history, channel from the DS IE
  (falls back to configured channel, marked as such), security summary, first
  and last seen, counts (beacons, probe responses, bad FCS), bounded signal
  history of raw codes when present, recent frames ring (bounded).
- `supervisor.py`: start and stop the helper subprocess (stdin closed,
  stdout = pcap, stderr = JSON events), bounded queue between reader and
  aggregator (drops counted), restart on crash only when the user asked for
  receiving, clean termination on stop and app shutdown. Health states:
  `unavailable` (helper not installed), `dependency_missing`, `no_device`,
  `stopped`, `starting`, `receiving`, `stale` (no frames for 10 s), `failed`.
- `sessions.py`: explicit recording of the raw pcap stream into bounded
  rotating files under the recordings root (`wifi/<session-id>/part-NNN.pcap`)
  with size and duration limits and retention; session metadata JSON beside
  them. Ordinary packets never enter `DecodeEventRepo`.
- `replay.py`: import a pcap file and run it through the same parser and
  aggregator (development and offline inspection).

Remote ID plugs in at the parser stage (Beacon vendor IE OUI `FA:0B:BC`, type
`0x0D`): `NetworkTable._ingest`'s trusted (good-FCS) path feeds every vendor
IE it finds into the shared live `Tracker` through an optional `.remoteid`
sink, the same way it feeds device history; capture and sessions do not
change, and nothing is persisted through `DecodeEventRepo` -- see
docs/remote-id.md "Live".

### Settings

| Setting | Type | Default |
|---|---|---|
| `wifi.enabled` | bool | `False` |
| `wifi.helper_path` | `str \| None` | none (see below) |
| `wifi.channel` | int | `6` |
| `wifi.record_max_mb` | int | `200` |
| `wifi.record_max_minutes` | int | `60` |
| `wifi.retention_days` | int | `14` |

`wifi.helper_path` is the path to `msdr-wifi-capture`. msdr uses the first existing of: this setting, `msdr-wifi-capture` on `PATH`, `rtl8812au-helper/.venv/bin` beside the msdr checkout, then `~/dev/radio/rtl8812au-helper/.venv/bin`. A helper whose venv was moved is reported as "venv is broken" with the rebuild command, not as missing.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/wifi/status` | health, channel, frames, rate, drops, last frame, error, helper info -- works even while `wifi.enabled` is false (`state: "stopped"`, `enabled: false`) |
| GET | `/api/wifi/devices` | helper `--list` output -- also works while disabled |
| POST | `/api/wifi/start` `{channel}` | lifecycle -- 409 `"Enable Wi-Fi in Setup first"` while disabled |
| POST | `/api/wifi/stop` | lifecycle -- always allowed (idempotent cleanup), never gated |
| GET | `/api/wifi/networks?q=&channel=&offset=&limit=` | paginated observation rows |
| GET | `/api/wifi/live?after=&kind=all|management|control|data&limit=` | frames received since sequence `after` (bounded ring of 500, every frame type) |
| GET | `/api/wifi/networks/{bssid}` | details: IEs, history, recent frames |
| GET | `/api/wifi/networks/{bssid}/frames/{n}` | raw frame hex on demand |
| POST | `/api/wifi/sessions/start` / `stop` | recording -- 409 while disabled |
| GET | `/api/wifi/sessions` | saved sessions for Review |
| GET | `/api/wifi/sessions/{id}/export.pcap` | concatenated pcap for Wireshark |
| POST | `/api/wifi/import` (file) | fixture or offline pcap replay into a session -- 409 while disabled |
| GET | `/api/wifi/activity?minutes=&bucket=` | activity heatmap rows (packets per listened second by channel/time bucket) -- see "Wi-Fi activity" below |

Only the routes that operate the receiver or recorder (start, sessions start/stop, import) refuse with 409 `"Enable Wi-Fi in Setup first"` while `wifi.enabled` is false. Every read always works — including `status`/`devices`, which the Setup card needs in order to show "stopped"/disabled in the first place.

Browser updates: a `wifi` websocket message at most 1 Hz carrying status and
changed row summaries (coalescable type).

#### Shapes

`status` (`GET /api/wifi/status`, and the `wifi` websocket message's
`status` field):

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

`parse_errors` folds together radiotap-level parse errors and dot11-level
ones (malformed or genuinely truncated beacons/probe-responses only --
"unsupported_frame", e.g. control/data frames this parser never decodes, is
not an error and is never counted here).

`networks` (`GET /api/wifi/networks`):

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

`details` (`GET /api/wifi/networks/{bssid}`): every `networks` item field,
plus:

```
{
  ies: [{id, ext_id, name, length, hex}],  # bytes always hex-encoded here
  rsn, wpa1, ht, vht, he, country, beacon_interval, capabilities,
  ssid_history, signal_history: [[ts, value]],
  recent: [{n, ts, subtype, length, bad_fcs}],
}
```

`sessions` list (`GET /api/wifi/sessions`):

```
[{id, started_at, stopped_at, channel, frames, bytes, drops, imported, health}]
```

`wifi` websocket message: `{type: "wifi", status, changed: [<networks item>, ...]}`.

### UI

- Setup: a Wi-Fi receiver card (enable, helper path, detected device, channel,
  Start/Stop, confirmed status with counts, rate, drops, last frame, error).
- Discover: a **Wi-Fi** tab with the network table (SSID or "hidden", BSSID,
  channel, security, signal when available, first and last seen, packets),
  search and channel filter, stable row order and selection, and a details
  pane (capabilities and IEs, reception history, recent broadcasts, raw frame
  on demand, receiver provenance). Labelled "Networks observed on channel N
  since HH:MM", never an inventory. A fourth tab, **Activity**, shows the
  packets-received-per-channel-over-time heatmap -- see "Wi-Fi activity"
  below.
- Review: Wi-Fi sessions with health, size, duration and PCAP export.

## Vendor names

Setup → Wi-Fi → **Download** fetches the IEEE MAC address registry (MA-L,
MA-M and MA-S CSVs from standards-oui.ieee.org, about 5 MB) and keeps only
prefix → organization name in `<data folder>/wifi/oui_vendors.json`;
**Update** refreshes it. It is not shipped with msdr. The most specific
block wins (MA-S, then MA-M, then MA-L). Rows, details and the survey's
network list carry `vendor` and `local_admin`: a locally administered
BSSID (bit 0x02 of the first byte -- a router's derived guest/mesh BSSID,
or a random MAC) is shown as "locally administered" instead of a vendor.
BLE devices with a public address get the same lookup. API: `GET
/api/wifi/vendors` → `{downloaded, prefixes, fetched_at, source}`; `POST
/api/wifi/vendors/refresh` (502 with the error when the download fails).

## Network summary and survey verdict

Above the Networks table, tiles summarise the networks heard on the current
channel in the last minute: probable access points (BSSIDs grouped by the
same heuristic as "group by access point"), by band, by security (Open,
WEP, WPA1, WPA2, WPA2/WPA3, WPA3, Enterprise, OWE), by maker (needs the
vendor names download; locally administered and unknown listed last) and
hidden. `GET /api/wifi/summary?window_s=60` (10-3600) returns
`{window_s, total, access_points, hidden, local_admin, bands, security,
makers}`, each list as `[{label, count}]`. Infrastructure only: msdr never
tracks client devices.

The Channel survey tab shows a verdict from the channels listened to so
far: busiest (frames per listened second), most networks, least congested
per band (fewest frames/s, then fewest networks; only when the band has at
least two surveyed channels) and overlap (networks heard off their own
channel). It is a starting point for picking a router channel, not an
interference measurement.

## Hidden networks and grouping

A hidden network beacons with an empty SSID. Every name a BSSID has ever
revealed (in a beacon, or in a probe response to a client that knew the
name) is kept in `revealed_ssids`, and the table shows such a row as
"hidden (seen as <name>)". The Networks tab's **Group** menu groups the
rows on the current page by network name (a hidden network joins its
revealed name), or by probable access point: BSSIDs equal apart from the
locally-administered bit and the last hex digit, which is how routers
usually derive guest/mesh BSSIDs from their base MAC. The access-point
grouping is a heuristic and is labelled "probably one access point".

## Wi-Fi activity

Activity is recorded into device history, so it needs
`"history": {"enabled": true}` in settings.json (off by default since
0.73.1, docs/history.md); with history off the heatmap stays empty.

Discover -> Wi-Fi -> **Activity**: a heatmap of packets this adapter actually
received, by channel (columns, 2.4 GHz then 5 GHz with a gap between, each
headed by its number and 20 MHz centre frequency, e.g. "ch 6 / 2437 MHz")
and time (rows, newest at top). Colour is packets received per *listened*
second in that cell; an optional second mode shows the mean raw PHY signal
code instead, labelled "uncalibrated signal code (not dBm)" (offered only
when at least one returned cell carries one -- this adapter has never been
seen to report calibrated dBm, see "Verified setup" above). A "?" help
mark beside the controls states plainly that this is Wi-Fi packets this
adapter received, never measured RF spectrum, continuous spectrum coverage,
channel utilisation, or detection of non-Wi-Fi interference.

Backed by `msdr/wifi/activity.py`'s `ActivityAccumulator`, owned by `WifiSupervisor` the same way its `MinuteAccumulator` (`self.history`) is:

- `_aggregate` adds one packet per ingested pcap record to `(int(record.ts), self.channel)` — the channel the receiver was actually tuned to at receipt, **never** a frame's own DS Parameter Set channel.
- `_tick` credits elapsed wall time to the same key once a second, but only while `health_state()` is `receiving`/`stale` on a known channel, capped at 2 s per tick so a stalled event loop can never invent coverage.
- `start()` (a plain restart or a survey hop) resets that crediting reference, so the starting/restart interval is never credited as listened time.

### Coverage rules

- A cell is **listened, no packets** (drawn at zero intensity, not hatched)
  when the receiver was tuned to that channel for part of that time bucket
  but heard nothing.
- A cell is **not listened** (hatched) when this receiver was never tuned to
  that channel during that time bucket at all -- a channel hop, the receiver
  stopped, the helper starting or restarting, or a capture gap. Every
  `(bucket, channel)` pair the server never recorded simply has no row; nothing
  is interpolated across unobserved channels or times.
- Works unmodified with both fixed-channel reception and the channel survey
  (`msdr/wifi/survey.py`): a survey's hops are ordinary `start()` calls, so
  each visited channel accumulates its own listened seconds and packets.
  Viewing this tab never starts or stops reception or hopping -- it only
  reads the history database, reusing whatever the receiver controls
  elsewhere are already doing.

### Persistence and retention

Drained by the same history flush as device-history buckets
(`HistoryServiceMixin._history_flush`/`_sync_history_sinks`, which gives the
Wi-Fi supervisor an `activity` sink alongside `history`), into a
`wifi_activity(second, channel, packets, listened_s, sig_sum, sig_n)` table
(`msdr/history/migrations/002_wifi_activity.sql`) in the same separate
history database as `buckets`/`coverage`, upserted additively so a flush
boundary never loses or double-counts a second. Per-second, per-channel rows
are bulky (~86,400/day per channel), so retention is its own setting,
`settings.history.activity_retention_hours` (default 48, 1-720), pruned
alongside (but independently of) `buckets`/`coverage`'s `retention_days`.

### API

`GET /api/wifi/activity?minutes=30&bucket=` (bucket one of 1, 5, 15, 60, 300
seconds, auto-chosen so the range stays at or under 360 rows, like
`history_timeline`'s bucket choice): flushes pending history first, then
returns

```
{
  start, end, bucket_s,
  channels: [<observed channels in range, sorted>],
  rows: [[t, channel, packets, listened_s, sig_mean | null], ...],  # listened_s > 0 only
  max_rate,  # highest packets-per-listened-second across the returned rows
}
```

The UI polls this at most every 5 s, only while the Activity tab is visible.

## Helper setup

```sh
mkdir -p ~/dev/radio/rtl8812au-helper && cd ~/dev/radio/rtl8812au-helper
git clone https://github.com/xen-proc/rtl8812au-macos upstream
git -C upstream checkout 347e9f172750e0c0b7c45b6dcfef7e780d70d144
python3 -m venv .venv && .venv/bin/pip install -r upstream/requirements.txt -e upstream -e wrapper
brew list libusb   # libusb must already be installed (Homebrew)
.venv/bin/msdr-wifi-capture --list
```

## Verified setup (2026-10-02)

Recorded from the feasibility run; the release section below is filled in
from the final hardware checks.

- Adapter enumerates as `0bda:8812`, "802.11n NIC", bcdDevice 0, generic serial "123456".
- Claimable as a normal user with Homebrew libusb 1.0.30; no kernel extension.
- Channel 6: 2,545 frames in 30 s, 20 % bad FCS, 925 good beacons. Channel 36:
  6,324 frames, 795 of 795 beacon DS channels matched. Channel 149: 85 % bad
  FCS, only 10 beacons; poor.
- 10 minutes on channel 6: 45,918 frames, 76.5 fps mean, no errors, process
  memory flat, CPU under 1 %. Five start/stop cycles and reopen after SIGTERM
  and SIGKILL all worked. SDRconnect ran throughout and was unaffected.
- No HT/VHT-rate frames were received; TSFT was not written by upstream's
  capture tool; signal is available only as raw PHY codes.

## Limitations

- One adapter per Mac (first `0bda:8812` found); fixed 20 MHz channel; no hopping.
- Radiotap channel is the configured channel; a strong 5 GHz access point was
  received while tuned to 2.4 GHz channel 6. The DS Parameter Set channel is
  authoritative and shown; frames whose DS channel differs are labelled.
- No calibrated signal strength. Trends use raw PHY codes, labelled as raw and
  relative, and are hidden when absent.
- Observed networks are those heard on the monitored channel during the
  session, not an inventory.

See also: [User guide](user-guide.md) · [History](history.md) · [Remote ID](remote-id.md) · [BLE](ble.md)
