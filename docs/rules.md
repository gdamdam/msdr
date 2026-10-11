# Watch rules

Rules that raise notifications for selected beacons, sensor thresholds and reported drone positions inside a geofence — for operators watching Wi-Fi, BLE and Remote ID. Notifications never imply a verified location.

Status: 0.61.0 (new). Contract for TODO "Phase 6 -- Watch rules and
additional hardware", first bullet: "Watch rules for selected beacons,
sensor thresholds and reported drone positions inside an operator-defined
geofence, raised as notifications without implying verified location."

Reuses these, and adds no second notification channel and no second copy of device state:

- the Wi-Fi and BLE receivers (docs/wifi.md, docs/ble.md)
- device history coverage (docs/history.md) and labels (docs/history.md "Labels")
- the live Remote ID tracker (docs/remote-id.md "Live")

## Rule kinds

| Kind | Fires when |
|---|---|
| `beacon_presence` | a Wi-Fi/BLE beacon is heard again, or goes missing |
| `sensor_threshold` | a BLE sensor reading crosses a threshold |
| `drone_geofence` | a reported drone position enters an area |

All three kinds share one row shape: `{id, kind, name, enabled, config,
cooldown_s, created_at, updated_at}`. `config`'s shape depends on `kind` and
is validated server-side (`msdr.api.services.rules.RulesServiceMixin`,
needs live context) before being stored.

### `beacon_presence`

`config: {receiver: "wifi"|"ble", ident, mode: "seen"|"missing", missing_after_minutes?}`

- A Wi-Fi BSSID or a BLE address. The BLE address must be `public` or
  `static` -- a rotating `rpa`/`nrpa` address is refused with the same
  message a label refusal uses (`msdr.history.labels.check_labelable`),
  since a rule on a rotating address would silently stop matching.
- `mode: "seen"`: fires (edge-triggered) the moment the beacon is heard
  again after not being currently present.
- `mode: "missing"`:
  - Fires once when the beacon has not been heard for `missing_after_minutes`, **and only when the receiver had enough coverage during that window** (docs/history.md "coverage", `coverage` table).
  - `missing_after_minutes` of silence while the receiver itself was off is never reported as the beacon being gone.
  - Re-arms the next time the beacon is seen again.

### `sensor_threshold`

`config: {addr, sensor_name, comparison: "above"|"below", threshold, hysteresis}`

- `addr` is a BLE address (same `public`/`static`-only rule as above).
- Reads `sensor.readings[name].value` off the live `AdvertiserTable` entry
  (added by `msdr/ble/sensors.py`, a concurrent addition) -- looked up
  defensively: no such attribute, no such reading, or no numeric `.value`
  all mean "does not fire yet", never an error. A rule for a sensor name
  that never appears on that device simply never fires.
- Hysteresis is a standard Schmitt trigger:
  - `comparison: "above"` fires once `value >= threshold`, then re-arms only once `value <= threshold - hysteresis`; `"below"` is the mirror image.
  - `hysteresis: 0` requires crossing strictly to the safe side before rearming; an unchanged value equal to the threshold never rearms.
  - Crossings respect `cooldown_s`.
- Sensor readings must be no more than 30 seconds old, using the sensor
  report timestamp. Unrelated advertisements do not refresh old readings.

### `drone_geofence`

`config: {geometry: {type: "circle", center: {lat, lon}, radius_m} | {type: "polygon", points: [{lat, lon}, ...]}, max_age_s?}`

- Uses the live Remote ID tracker's (`msdr.api.services.remoteid`,
  `msdr.remoteid.tracker.Tracker`) per-UAS *reported* drone position --
  never a verified or inferred one. A stale position (the tracker's own
  staleness threshold) or one older than `max_age_s` (default 60 s) is
  never used.
- Fires (edge-triggered) when a UAS's reported position moves from outside
  to inside the geometry; fires again on a later re-entry. The fired
  message always reads "reported position", e.g. *"drone uas:1 reported
  position inside the watched area"* -- it never claims a verified location.
- `circle`: haversine distance from `center`. `polygon`: ray-casting point
  containment (`msdr.rules.geo`), at least 3 points, treated as a small
  local area (no map projection).

## Engine (`msdr/rules/`)

| File | Role |
|---|---|
| `msdr/rules/geo.py` | `haversine_m`, `point_in_circle`, `point_in_polygon` — plain geometry, no I/O. |
| `msdr/rules/engine.py` | `Rule`/`Context`/`EngineState`/`FiredEvent` dataclasses and `evaluate(rules, ctx, state) -> list[FiredEvent]`. Pure: no asyncio, no database, no FastAPI — this is what `tests/rules/` runs directly on fixtures (TODO acceptance: "rule evaluation tests on fixtures, including stale and duplicate inputs"). |
| `msdr/rules/store.py` | `RulesRepo`, the SQLite side in the **main** database (migration 022, alongside `device_labels` from migration 021), so backup and restore carry rules and the recent event log. |

Every rule kind shares one cooldown/dedup mechanism keyed by
`(rule_id, target)` -- `target` is the beacon ident, the `addr:sensor_name`
pair, or the drone's tracker key. A tick that hands the engine an unchanged
reading, or runs twice for the same instant, never produces a second event
for a condition that has not actually changed (edge-triggered logic plus the
cooldown gate).

## Service (`msdr/api/services/rules.py`)

`RulesServiceMixin`, same shape as `RemoteidServiceMixin`/
`HistoryServiceMixin`: owns a background tick (`RULES_TICK_S = 1.5`, within
the Signal workbench watch's own 1-2 s range) that re-evaluates every
enabled rule and persists whatever fires to `rule_events`, reading:

- `self.wifi.table`/`self.ble.table` (`WifiServiceMixin`/`BleServiceMixin`)
  for beacon presence and sensor readings.
- `self.history_store` (`HistoryServiceMixin`) for the "missing" mode's
  coverage fraction via `HistoryStore.covered_seconds()`. Minute totals
  are clipped conservatively at the window edges: time that might fall
  outside the window is subtracted, never credited toward an alert.
  Disabled history provides no coverage evidence.
- `self.remoteid_tracker` (`RemoteidServiceMixin`) for drone positions.

## API

| Route | Notes |
|---|---|
| `GET /api/rules` | `{"items": [rule, ...]}` |
| `POST /api/rules` | body `{kind, name, config, enabled?, cooldown_s?}`; 422 on an invalid shape or a rotating BLE address |
| `PATCH /api/rules/{id}` | partial `{name?, config?, enabled?, cooldown_s?}`; 404 when absent |
| `DELETE /api/rules/{id}` | 404 when absent |
| `GET /api/rules/events?rule_id=&limit=` | `{"items": [event, ...]}`, newest first |
| `POST /api/rules/{id}/test` | evaluates that one rule against the live context *right now*, against a **fresh** engine state (ignores cooldown and any in-progress presence/armed/inside edge) -- `{"rule_id", "would_fire": [{target, message, detail}, ...]}`. Never persists an event. |

An event row: `{id, rule_id, kind, target, message, detail, fired_at}`.

## UI

Discover -> **Watch rules** (`rules-view.js`), alongside the other
cross-receiver Discover sub-views (Remote ID, Timeline): create/edit/enable
rules with pickers fed from the live Wi-Fi/BLE tables and labels
(`beaconOptions`, private BLE addresses filtered out) and the Remote ID
list (a "center on a live drone's reported position" convenience for the
geofence editor); a Test button shows what would fire right now; a fired
events table polls `GET /api/rules/events` the same way
`band-scan-inbox.js` polls its own history, rather than depending on the
websocket.

## Limits

- `EngineState`'s presence/armed/inside edges are **not** persisted across
  a restart -- only the cooldown clock is (seeded from `rule_events` at
  startup), so a rule could in principle refire once right after a restart
  if its condition was already true and the cooldown had expired. Accepted
  for 0.61.0; revisit if this proves noisy in practice.
- `rule_events` is bounded by row count (`MAX_EVENTS = 5000` in
  `msdr.rules.store`), pruned opportunistically on insert -- not a
  time-based retention like device history.
- `sensor_threshold` depends entirely on `msdr/ble/sensors.py` (added
  alongside this feature) actually populating `.sensor.readings` on live
  advertiser entries; until it does, such rules simply never fire.
- A `drone_geofence` polygon is tested in plain lat/lon (no map
  projection) -- accurate for the small local areas this feature targets,
  not for anything continent-scale.

See also: [BLE](ble.md), [Remote ID](remote-id.md), [Wi-Fi](wifi.md), [Device history](history.md).
