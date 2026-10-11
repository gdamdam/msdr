# Long listening: device history, shared timeline, labels, Wi-Fi channel survey

Long-run history of nearby Wi-Fi and BLE devices, and the timeline, labels, comparison and channel survey built on it.

Status: 0.60.0. Contract for the Wi-Fi and BLE receivers (docs/wifi.md, docs/ble.md). The SDR receiver is not part of this history.

## Device history

While a Wi-Fi or BLE receiver runs, every trusted observation is folded into
**per-minute buckets** per device, so an overnight run keeps memory flat and
the database small.

- Wi-Fi: one device per BSSID, from good-FCS beacons and probe responses
  (duplicates and bad-FCS frames are never counted). Signal is the raw PHY
  code (`signal_kind: "raw"`) or dBm when radiotap carries it.
- BLE: one device per advertiser address. Signal is RSSI in dBm.
- Identity kinds: Wi-Fi `bssid`; BLE `public`, `static`, `rpa`, `nrpa`,
  `random`. A private BLE address rotates (typically every ~15 minutes), so
  its history ends when the address changes; the timeline hides private
  addresses unless asked (`include_private=true`).

### Storage and settings

- A **separate SQLite file**, `history.db` next to `msdr.db` (`settings.history.db_path` overrides; `:memory:` when the main database is `:memory:`).
- Own migrations (`msdr/history/migrations/`). Not part of backups: it is bulk, regenerable observation data.

| Setting | Meaning | Default |
|---|---|---|
| `settings.history.retention_days` | Retention; pruned at startup and hourly | 30 |
| `settings.history.enabled` | Turns recording on; reads still work while off | **false** since 0.73.1 |

There is no UI toggle: set `"history": {"enabled": true}` in `~/Library/Application Support/msdr/settings.json` and restart msdr. Settings files saved by earlier versions already contain `"enabled": true` and keep recording until it is set to false.

### Tables

- `buckets(receiver, ident, minute, reports, sig_min, sig_max, sig_sum, sig_n)`,
  primary key `(receiver, ident, minute)`; `minute` is unix seconds floored to 60.
- `devices(receiver, ident, name, kind, signal_kind, first_seen, last_seen)`,
  primary key `(receiver, ident)`; `name` is the latest SSID or BLE name.
- `coverage(receiver, minute, channel, seconds)`, primary key
  `(receiver, minute, channel)`: how many seconds of that minute the receiver
  was actually listening (`receiving` or `stale`), and on which Wi-Fi channel
  (`channel = 0` for BLE, whose channels are controller-scheduled).

### Writes

- Tables feed an in-memory `MinuteAccumulator` (msdr/history/buckets.py).
- The service samples coverage every 2 s and flushes to SQLite every 30 s, on stop and before every history read, with additive upserts.
- The accumulator holds at most 50 000 pending (receiver, ident, minute) keys between flushes; beyond that, new keys are dropped and counted in `pending_dropped` (`GET /api/history/status`, cumulative since start).
- Only a flood of distinct identifiers reaches the cap; pending buckets keep updating.

## Labels

User labels for devices, in the **main database** (migration 021,
`device_labels`), so backup and restore carry them.

- Key: `(receiver, ident)`; receiver `wifi` or `ble`; ident a lowercase MAC.
- Fields: `name` (1-80 chars), `note` (0-500), `class` one of `mine`, `known`,
  `ignore`; `created_at`, `updated_at`.
- A BLE `rpa`/`nrpa` address is refused (422): it rotates, so a label on it
  would silently stop matching. Payload fingerprints were considered and dropped
  (2026-10-03): rotating devices also vary their payload, and the stable parts
  (company id, service UUIDs) are shared by every device of the same kind, so a
  fingerprint would label strangers' devices. Rotation is a privacy protection;
  msdr does not link rotating addresses.
- Labels never merge devices: two devices with the same label stay two rows.
- Library export/import (`msdr/storage/transfer.py`, format `msdr/1`) carries
  labels as `device_labels`: merge by `(receiver, ident)`, newer `updated_at`
  wins, invalid rows are skipped. Full backups already include them.

## API

| Route | Notes |
|---|---|
| `GET /api/history/status` | `{enabled, db_path, size_bytes, retention_days, oldest, newest, devices, buckets}` |
| `POST /api/history/clear` | deletes all history (not labels) |
| `GET /api/history/timeline` | see below |
| `GET /api/history/device/{receiver}/{ident}?hours=24&bucket=60` | `{device, cells, coverage, bucket_s, start, end}`; 404 when never seen |
| `GET /api/labels?receiver=` | `{"items": [label, ...]}` |
| `PUT /api/labels/{receiver}/{ident}` | body `{name, note?, class}`; returns the label |
| `DELETE /api/labels/{receiver}/{ident}` | 404 when absent |

### Timeline query

| Parameter | Meaning | Default |
|---|---|---|
| `hours` | 0.25-168 | 6 |
| `start`/`end` | unix s, instead of `hours` | — |
| `receivers` | comma list of `wifi`,`ble` | both |
| `include_private` | include private BLE addresses | false |
| `hide` | comma list of label classes to exclude, e.g. `ignore` | — |
| `q` | substring of ident, name or label | — |
| `limit` | 1-500 | 200 |
| `bucket` | 60, 300, 900 or 3600 s | chosen so the range has at most ~360 columns |

Response:

```
{
  "start": 1790990000, "end": 1791011600, "bucket_s": 60,
  "coverage": {
    "wifi": [[t, seconds, [channels...]], ...],
    "ble":  [[t, seconds], ...]
  },
  "devices": [
    {"receiver": "ble", "ident": "c0:11:22:33:44:55", "name": "TestBean",
     "kind": "static", "signal_kind": "dbm",
     "label": {"name": "Kitchen sensor", "class": "mine"} | null,
     "first_seen": 1790990123.0, "last_seen": 1791011000.0,
     "reports": 4210,
     "cells": [[t, reports, sig_avg, sig_min, sig_max], ...]}
  ],
  "device_total": 812, "truncated": true
}
```

`cells` and `coverage` only list buckets with data. Devices are the `limit`
with the most reports in range, then sorted by first appearance in range.
`first_seen`/`last_seen` are lifetime values from `devices`.

## Capture comparison

`GET /api/history/compare?a_start=&a_end=&b_start=&b_end=&receivers=&include_private=` compares two listening windows of recorded history — "device off" vs "device on", an antenna/placement A/B test, or any two time ranges (development notes Phase 5 "Session comparisons", "Long listening and local knowledge" "Capture comparison" — one merged comparison view rather than two).

- `receivers`/`include_private` have the same meaning as the timeline.
- `a_end`/`b_end` must each be after their own start (422 otherwise).

Every device present in the buckets table across either window is placed in exactly one list:

- `disappeared`: window A only.
- `appeared`: window B only.
- `changed`: both windows, with reports, mean signal and a reports-per-minute
  rate for each window plus the signal change (B minus A).

**Normalisation:** rates and means are normalised by each window's own **listened** time from the `coverage` table (summed across the whole window, not wall-clock duration), per receiver — a receiver that was only half-listening in one window never looks quieter than it really was.

**Private BLE addresses:** BLE is matched by stable address only. A resolvable or non-resolvable private address is never a device across two separate time windows (it may not even be the same physical device), so `rpa`/`nrpa` rows are excluded from all three lists and counted in `excluded_private_rows`. `include_private=true` additionally lists them in `excluded_private` (`{receiver, ident, kind, window}`, `window` one of `a`/`b`/`both`).

**`comparable`** is a `{receiver: bool}` map. A receiver is not comparable when:

- either window has zero listened seconds for it (it never ran), or
- Wi-Fi only: the two windows' listened channel sets differ (a genuine coverage mismatch, not just a device going quiet).

Every row from a non-comparable receiver is still reported, never dropped, with its own `comparable: false` so the UI can flag it rather than imply a fair comparison.

**`notes`** always states that reception percentages are refused: nothing here knows how many packets or advertisements a device actually sent, so no count of what this receiver heard can be turned into a percentage of it.

Response shape:

```
{
  "a": {"start":..., "end":..., "coverage": {"ble": {"listened_s":..., "channels":[]}, "wifi": {...}}},
  "b": {"start":..., "end":..., "coverage": {...}},
  "receivers": ["wifi", "ble"],
  "comparable": {"wifi": true, "ble": false},
  "excluded_private_rows": 2,
  "appeared": [{"receiver","ident","name","kind","reports","mean_signal","rate_per_min","comparable"}],
  "disappeared": [{...same shape...}],
  "changed": [{"receiver","ident","name","kind","reports_a","reports_b",
               "rate_a","rate_b","mean_signal_a","mean_signal_b","signal_change","comparable"}],
  "notes": ["Reception percentages are not reported: ...", ...]
}
```

### Compare mode (UI)

- Timeline (`timeline-view`) gets a Compare mode tab beside the existing grid.
- A preset ("Last 10 min vs previous 10 min") or two explicit `datetime-local` start/end pairs pick the windows.
- Results render as three lists (appeared, disappeared, changed) with a coverage-comparable chip per receiver and a "not comparable" flag on any row from a mismatched receiver.
- Dragging on the time axis was considered and dropped for this pass: it would need hit-testing against the coverage band's time-to-pixel mapping and a second drag interaction that conflicts with the existing click-to-select-a-row handler on the same canvas, for no gain the explicit fields don't already give.

## Shared timeline (UI)

Discover → **Timeline** (`timeline-view`): one time axis for both receivers.

- Top: one coverage band per receiver. Wi-Fi shows the tuned channel(s) per
  column; a column where the receiver was not listening is hatched, so "not
  listening" never looks like "device absent".
- Rows: one per device, receiver colour (`--context-wifi`, `--context-ble`),
  label name in bold when labelled. A **Colour** select chooses what cell
  intensity means: `reports` (count relative to that device's busiest slice,
  the default) or `signal strength` (the slice's mean signal, scaled from the
  weakest to the strongest value seen for that receiver in the range -- one
  scale per receiver, since Wi-Fi may be an uncalibrated code while BLE is
  dBm; a heard cell with no signal value stays faint). The grid tooltip adds
  the mean signal either way.
- Filters: receivers, range (1 h, 6 h, 24 h, 7 d), include private BLE
  addresses, hide classes (`ignore` hidden by default), search.
- Selecting a row opens a details panel: identity, label editor, and a signal
  chart over time (min/avg/max band) for that device.
- Co-occurrence is shown, never asserted: nothing links Wi-Fi and BLE rows.

Wi-Fi and BLE details panes also show the label editor and a 24 h history
chart for the selected device, and their tables show the label and a "hide
ignored" filter.

## Wi-Fi channel survey

The ACH helper has no channel hopping; a survey restarts it per channel.

| Route | Purpose and notes |
|---|---|
| `POST /api/wifi/survey/start` | Body `{"channels": [1, 6, 11], "dwell_s": 10, "rounds": 1}` (dwell 5-120 s; rounds 0 = until stopped, else 1-100). 409 when Wi-Fi is disabled or a packet recording session is active. |
| `POST /api/wifi/survey/stop` | Replies at once with state `"stopping"`; the state then becomes `"stopped"`. |
| `GET /api/wifi/survey` | Current state and per-channel results (shape below). |

- Starting a survey stops a plain capture; when the survey ends or is stopped, capture resumes on the channel it had before (or stays stopped if it was stopped).
- On stop, the partial dwell of the channel being heard is kept, and cancelling the hop and restoring the prior capture (helper restarts) finish in the background, bounded at 20 s.

Shape of `GET /api/wifi/survey`:

```
{"state": "idle"|"running"|"stopping"|"done"|"stopped"|"failed",
 "phase": "switching"|"listening"|null,
 "estimated_total_s": 60.0, "switch_s": 4.2,
 "channels": [...], "dwell_s": 10, "rounds": 1, "round": 1,
 "current_channel": 6, "channel_ends_at": 1791000010.0,
 "started_at": ..., "ended_at": ..., "error": null,
 "results": [{"channel": 6, "status": "pending"|"switching"|"listening"|"done"|"failed", "error": null, "visits": 1, "listened_s": 8.7, "frames": 2104,
              "networks": 14,
              "top": [{"bssid", "ssid", "ds_channel", "signal"}, ... up to 50]}]}
```

- `phase` is `switching` while the helper restarts on the next channel (`channel_ends_at` is null then) and `listening` during the dwell.
- The row of the channel being heard is reported live (listened time, frames, networks so far).
- `estimated_total_s` is (dwell + one helper restart) per channel per round, null for rounds 0; `switch_s` is the measured average restart, replacing a 5 s guess once a hop has been timed.
- `listened_s` counts only time the helper was receiving (startup time per hop is excluded).
- `networks` counts distinct BSSIDs whose good frames arrived while that channel was tuned; `ds_channel` shows when a beacon heard there announces another channel (2.4 GHz overlap).
- The network table keeps accumulating across hops during a survey.

**Failures:**

- A channel whose helper can't be brought up within 45 s (long enough for the supervisor's crash retries plus a firmware load) is recorded as `"failed"` with the helper's own error in `error` and skipped; the survey still ends `"done"` with `failed_channels` counted.
- Three failures in a row end the survey as `"failed"` (the adapter is gone), as does a survey where no channel started at all.

### Channel survey (UI)

Discover → Wi-Fi gets a third tab **Channel survey** with:

- **Presets:** 2.4 GHz 1-13; 2.4 GHz 1/6/11; 5 GHz non-DFS 36-48/149-165; 5 GHz incl. DFS; all channels without DFS; all channels incl. DFS. The DFS range 52-144 is left out by default because tuning the ACH there has dropped it off USB.
- **Controls:** dwell, rounds, Start/Stop.
- **Estimate** before Start ("about 0:45 for 3 channels").
- **Progress** with an animated marker ("round 1 of 1 · listening on channel 44 · 6 s left · 0:35 of ~1:00", or "switching to channel 11…").
- **Results table**, sortable: channel, status, band, listened, frames, frames/s bar, load, networks, strongest.
- **Load (±4)** is the frames/s of the channel plus every 2.4 GHz channel within ±4 (whose 20 MHz signals overlap its window and don't decode on it, so its own count understates its air); on 5 GHz it equals frames/s. The "least congested 2.4 GHz" verdict is chosen among 1/6/11 by that load.
- **Why these picks** (collapsed under the verdict) lists findings computed from the results — candidate channels with their loads, quiet-looking channels next to busy ones, an 80 MHz stream seen on four 5 GHz slices, one strong radio heard across many channels, hidden networks, and the method's limits — with a **Copy findings** button for the plain text with the per-channel network list on row click; with no row picked it follows the channel being heard.

It states: "Each channel is heard only during its dwell. Not a spectrum measurement."

See also: [Wi-Fi](wifi.md) · [BLE](ble.md) · [Remote ID](remote-id.md) · [User guide](user-guide.md)
