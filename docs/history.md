# Device history, timeline, labels and Wi-Fi channel survey

msdr can keep a long-running history of the Wi-Fi and BLE devices it hears. On top of that history it offers a shared timeline, device labels, a comparison of two listening windows, and a Wi-Fi channel survey. This page is for people running the Wi-Fi ([wifi.md](wifi.md)) and BLE ([ble.md](ble.md)) receivers for long periods. The SDR receiver is not part of this history.

## Turn it on

History is off by default and has no UI toggle.

1. Add `"history": {"enabled": true}` to `~/Library/Application Support/msdr/settings.json`.
2. Restart msdr.
3. Start the Wi-Fi or BLE receiver. Devices appear in Discover → **Timeline** as they are heard.

Settings files saved by earlier versions already contain `"enabled": true` and keep recording until it is set to false. With history off, reads still work, but nothing new is recorded.

| Setting | Meaning | Default |
|---|---|---|
| `settings.history.enabled` | Turns recording on | **false** |
| `settings.history.retention_days` | Retention; pruned at startup and hourly | 30 |
| `settings.history.activity_retention_hours` | Retention of the Wi-Fi activity heatmap data ([wifi.md](wifi.md#wi-fi-activity)) | 48 |
| `settings.history.db_path` | Overrides the database location | `history.db` next to `msdr.db` |

## Device history

While a Wi-Fi or BLE receiver runs, every trusted observation is folded into **per-minute buckets** per device, so an overnight run keeps memory flat and the database small.

- **Wi-Fi:** one device per BSSID, from good-FCS beacons and probe responses. Duplicates and bad-FCS frames are never counted. Signal is the raw PHY code (`signal_kind: "raw"`), or dBm when radiotap carries it.
- **BLE:** one device per advertiser address. Signal is RSSI in dBm.
- **Identity kinds:** Wi-Fi `bssid`; BLE `public`, `static`, `rpa`, `nrpa`, `random`. A private BLE address rotates (typically about every 15 minutes), so its history ends when the address changes. The timeline hides private addresses unless asked (`include_private=true`).

### Storage

History lives in a separate SQLite file, `history.db`, next to `msdr.db` (`settings.history.db_path` overrides this; it is `:memory:` when the main database is `:memory:`). It has its own migrations (`msdr/history/migrations/`). It is not part of backups, because it is bulk, regenerable observation data.

Tables:

- `buckets(receiver, ident, minute, reports, sig_min, sig_max, sig_sum, sig_n)`, primary key `(receiver, ident, minute)`. `minute` is unix seconds floored to 60.
- `devices(receiver, ident, name, kind, signal_kind, first_seen, last_seen)`, primary key `(receiver, ident)`. `name` is the latest SSID or BLE name.
- `coverage(receiver, minute, channel, seconds)`, primary key `(receiver, minute, channel)`. It records how many seconds of that minute the receiver was actually listening (`receiving` or `stale`) and on which Wi-Fi channel (`channel = 0` for BLE, whose channels are controller-scheduled).

### How writes work

- Tables feed an in-memory `MinuteAccumulator` (`msdr/history/buckets.py`).
- The service samples coverage every 2 s and flushes to SQLite every 30 s, on stop, and before every history read, using additive upserts.
- The accumulator holds at most 50 000 pending `(receiver, ident, minute)` keys between flushes. Beyond that, new keys are dropped and counted in `pending_dropped` (`GET /api/history/status`, cumulative since start). Only a flood of distinct identifiers reaches the cap, and pending buckets keep updating.

## Labels

You can label devices with a name, a note and a class. Labels live in the **main database** (migration 021, table `device_labels`), so backup and restore carry them.

- **Key:** `(receiver, ident)`, where receiver is `wifi` or `ble` and ident is a lowercase MAC.
- **Fields:** `name` (1-80 chars), `note` (0-500), `class` (one of `mine`, `known`, `ignore`), `created_at`, `updated_at`.
- **Rotating BLE addresses:** a BLE `rpa` or `nrpa` address is refused (422), because it rotates and a label on it would silently stop matching. Payload fingerprints were considered and dropped (2026-10-03): rotating devices also vary their payload, and the stable parts (company id, service UUIDs) are shared by every device of the same kind, so a fingerprint would label other people's devices. Rotation is a privacy protection, and msdr does not link rotating addresses.
- **No merging:** two devices with the same label stay two rows.
- **Export and import:** library export/import (`msdr/storage/transfer.py`, format `msdr/1`) carries labels as `device_labels`. Import merges by `(receiver, ident)`, the newer `updated_at` wins, and invalid rows are skipped. Full backups already include labels.

## API

| Route | Notes |
|---|---|
| `GET /api/history/status` | `{enabled, db_path, size_bytes, retention_days, oldest, newest, devices, buckets}` |
| `POST /api/history/clear` | deletes all history (not labels) |
| `GET /api/history/timeline` | see [Timeline query](#timeline-query) |
| `GET /api/history/device/{receiver}/{ident}?hours=24&bucket=60` | `{device, cells, coverage, bucket_s, start, end}`; 404 when never seen |
| `GET /api/history/compare` | see [Capture comparison](#capture-comparison) |
| `GET /api/labels?receiver=` | `{"items": [label, ...]}` |
| `PUT /api/labels/{receiver}/{ident}` | body `{name, note?, class}`; returns the label |
| `DELETE /api/labels/{receiver}/{ident}` | 404 when absent |

### Timeline query

| Parameter | Meaning | Default |
|---|---|---|
| `hours` | 0.25-168 | 6 |
| `start`/`end` | unix s, instead of `hours` | none |
| `receivers` | comma list of `wifi`,`ble` | both |
| `include_private` | include private BLE addresses | false |
| `hide` | comma list of label classes to exclude, e.g. `ignore` | none |
| `q` | substring of ident, name or label | none |
| `limit` | 1-500 | 200 |
| `bucket` | 60, 300, 900 or 3600 s | chosen so the range has at most about 360 columns |

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

- `cells` and `coverage` list only buckets with data.
- Devices are the `limit` devices with the most reports in range, then sorted by first appearance in range.
- `first_seen` and `last_seen` are lifetime values from `devices`.

## Shared timeline

Discover → **Timeline** (`timeline-view`) shows one time axis for both receivers.

- **Coverage bands:** at the top, one band per receiver. The Wi-Fi band shows the tuned channel(s) per column. A column where the receiver was not listening is hatched, so "not listening" never looks like "device absent".
- **Rows:** one per device, in the receiver colour (`--context-wifi`, `--context-ble`), with the label name in bold when labelled.
- **Colour select:** chooses what cell intensity means.
  - `reports` (the default): count relative to that device's busiest slice.
  - `signal strength`: the slice's mean signal, scaled from the weakest to the strongest value seen for that receiver in the range. There is one scale per receiver, since Wi-Fi may be an uncalibrated code while BLE is dBm. A heard cell with no signal value stays faint.
  - The grid tooltip adds the mean signal either way.
- **Filters:** receivers, range (1 h, 6 h, 24 h, 7 d), include private BLE addresses, hide classes (`ignore` is hidden by default), and search.
- **Details panel:** selecting a row shows identity, a label editor, and a signal chart over time (min/avg/max band) for that device.
- **Co-occurrence** is shown, never asserted: nothing links Wi-Fi and BLE rows.

The Wi-Fi and BLE details panes also show the label editor and a 24 h history chart for the selected device. Their tables show the label and a "hide ignored" filter.

## Capture comparison

`GET /api/history/compare?a_start=&a_end=&b_start=&b_end=&receivers=&include_private=` compares two listening windows of recorded history. Use it for "device off" versus "device on", an antenna or placement A/B test, or any two time ranges.

- `receivers` and `include_private` mean the same as in the timeline query.
- `a_end` and `b_end` must each be after their own start (422 otherwise).

Every device present in the buckets table across either window is placed in exactly one list:

- `disappeared`: window A only.
- `appeared`: window B only.
- `changed`: both windows, with reports, mean signal and a reports-per-minute rate for each window, plus the signal change (B minus A).

**Normalisation.** Rates and means are normalised by each window's own **listened** time from the `coverage` table, summed across the whole window rather than taken as wall-clock duration, per receiver. A receiver that was only half-listening in one window never looks quieter than it really was.

**Private BLE addresses.** BLE is matched by stable address only. A resolvable or non-resolvable private address is never treated as one device across two separate windows (it may not even be the same physical device). `rpa` and `nrpa` rows are excluded from all three lists and counted in `excluded_private_rows`. With `include_private=true` they are also listed in `excluded_private` (`{receiver, ident, kind, window}`, where `window` is `a`, `b` or `both`).

**`comparable`** is a `{receiver: bool}` map. A receiver is not comparable when:

- either window has zero listened seconds for it (it never ran), or
- for Wi-Fi only, the two windows' listened channel sets differ (a genuine coverage mismatch, not just a device going quiet).

Every row from a non-comparable receiver is still reported, with its own `comparable: false`, so the UI can flag it rather than imply a fair comparison.

**`notes`** always states that reception percentages are not reported. Nothing here knows how many packets or advertisements a device actually sent, so no count of what this receiver heard can be turned into a percentage of it.

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

### Compare mode in the UI

- The Timeline (`timeline-view`) has a Compare mode tab beside the grid.
- Pick the windows with a preset ("Last 10 min vs previous 10 min") or with two explicit `datetime-local` start/end pairs.
- Results render as three lists (appeared, disappeared, changed), with a coverage-comparable chip per receiver and a "not comparable" flag on any row from a mismatched receiver.
- Dragging on the time axis to pick a window is not supported. It would need hit-testing against the coverage band's time-to-pixel mapping and a second drag interaction that conflicts with the click-to-select-a-row handler on the same canvas, for no gain over the explicit fields.

## Wi-Fi channel survey

The AWUS036ACH helper cannot hop channels on its own, so a survey restarts it once per channel and listens for a set dwell time on each.

### Run a survey

1. Open Discover → Wi-Fi → **Channel survey**.
2. Choose a preset or a set of channels, a dwell time and a number of rounds.
3. Check the estimate shown before Start, then press Start.
4. Watch the progress line and the results table. Press Stop to end early.

Starting a survey stops a plain capture. When the survey ends or is stopped, capture resumes on the channel it had before, or stays stopped if it was stopped. On stop, the partial dwell of the channel being heard is kept. Cancelling the hop and restoring the prior capture (helper restarts) finish in the background, bounded at 20 s.

### Survey API

| Route | Purpose and notes |
|---|---|
| `POST /api/wifi/survey/start` | Body `{"channels": [1, 6, 11], "dwell_s": 10, "rounds": 1}` (dwell 5-120 s; rounds 0 = until stopped, else 1-100). 409 when Wi-Fi is disabled or a packet recording session is active. |
| `POST /api/wifi/survey/stop` | Replies at once with state `"stopping"`; the state then becomes `"stopped"`. |
| `GET /api/wifi/survey` | Current state and per-channel results. |

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
- The row of the channel being heard is reported live (listened time, frames and networks so far).
- `estimated_total_s` is (dwell + one helper restart) per channel per round, and null when rounds is 0. `switch_s` is the measured average restart, which replaces a 5 s guess once a hop has been timed.
- `listened_s` counts only time the helper was receiving (startup time per hop is excluded).
- `networks` counts distinct BSSIDs whose good frames arrived while that channel was tuned. `ds_channel` is shown when a beacon heard there announces another channel (2.4 GHz overlap).
- The network table keeps accumulating across hops during a survey.

### Survey failures

- A channel whose helper cannot be brought up within 45 s (long enough for the supervisor's crash retries plus a firmware load) is recorded as `"failed"`, with the helper's own error in `error`, and skipped. The survey still ends `"done"`, with `failed_channels` counted.
- Three failures in a row end the survey as `"failed"` (the adapter is gone). So does a survey where no channel started at all.

### Survey UI

The **Channel survey** tab has:

- **Presets:** 2.4 GHz 1-13; 2.4 GHz 1/6/11; 5 GHz non-DFS 36-48/149-165; 5 GHz incl. DFS; all channels without DFS; all channels incl. DFS. The DFS range 52-144 is left out by default because tuning the ACH there has dropped it off USB.
- **Controls:** dwell, rounds, Start/Stop.
- **Estimate** before Start ("about 0:45 for 3 channels").
- **Progress** with an animated marker ("round 1 of 1 · listening on channel 44 · 6 s left · 0:35 of ~1:00", or "switching to channel 11…").
- **Results table**, sortable: channel, status, band, listened, frames, frames/s bar, load, networks, strongest. Clicking a row shows that channel's network list. With no row picked, it follows the channel being heard.
- **Load (±4):** the frames/s of the channel plus every 2.4 GHz channel within ±4. Those channels' 20 MHz signals overlap its window and do not decode on it, so its own count understates its air. On 5 GHz, load equals frames/s. The "least congested 2.4 GHz" verdict is chosen among 1/6/11 by this load.
- **Why these picks** (collapsed under the verdict): findings computed from the results. They cover candidate channels with their loads, quiet-looking channels next to busy ones, an 80 MHz stream seen on four 5 GHz slices, one strong radio heard across many channels, hidden networks, and the limits of the method. A **Copy findings** button copies the plain text.

The tab states: "Each channel is heard only during its dwell. Not a spectrum measurement."

See also: [Wi-Fi](wifi.md) · [BLE](ble.md) · [Remote ID](remote-id.md) · [User guide](user-guide.md)
