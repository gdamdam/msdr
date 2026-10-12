# Watch rules

Watch rules raise notifications when a selected Wi-Fi or BLE beacon appears or goes missing, when a BLE sensor reading crosses a threshold, or when a drone reports a position inside an area you define. This page is for operators watching Wi-Fi, BLE and Remote ID. Notifications never claim a verified location: drone alerts always say "reported position".

Rules reuse the existing receivers ([wifi.md](wifi.md), [ble.md](ble.md)), device-history coverage and labels ([history.md](history.md)), and the live Remote ID tracker ([remote-id.md](remote-id.md#live)). They add no second notification channel and no second copy of device state.

## Create a rule

1. Open Discover → **Watch rules** (`rules-view.js`), next to the Remote ID and Timeline sub-views.
2. Create a rule and choose its kind. Pickers are filled from the live Wi-Fi and BLE tables, from labels, and from the Remote ID list. Private BLE addresses are filtered out.
3. For a drone geofence, you can centre the area on a live drone's reported position.
4. Press **Test** to see what would fire right now.
5. Enable the rule. Fired events appear in the events table below, which polls `GET /api/rules/events` (like `band-scan-inbox.js` polls its own history) rather than using the websocket.

## Rule kinds

| Kind | Fires when |
|---|---|
| `beacon_presence` | a Wi-Fi/BLE beacon is heard again, or goes missing |
| `sensor_threshold` | a BLE sensor reading crosses a threshold |
| `drone_geofence` | a reported drone position enters an area |

All three kinds share one row shape: `{id, kind, name, enabled, config, cooldown_s, created_at, updated_at}`. The shape of `config` depends on `kind`. It is validated server-side (`msdr.api.services.rules.RulesServiceMixin`, which needs live context) before it is stored.

### `beacon_presence`

`config: {receiver: "wifi"|"ble", ident, mode: "seen"|"missing", missing_after_minutes?}`

- `ident` is a Wi-Fi BSSID or a BLE address. A BLE address must be `public` or `static`. A rotating `rpa` or `nrpa` address is refused with the same message a label refusal uses (`msdr.history.labels.check_labelable`), since a rule on a rotating address would silently stop matching.
- `mode: "seen"` fires (edge-triggered) the moment the beacon is heard again after not being currently present.
- `mode: "missing"` fires once when the beacon has not been heard for `missing_after_minutes`, **and only when the receiver had enough coverage during that window** (the `coverage` table, see [history.md](history.md#storage)). Silence while the receiver itself was off is never reported as the beacon being gone. The rule re-arms the next time the beacon is seen.

### `sensor_threshold`

`config: {addr, sensor_name, comparison: "above"|"below", threshold, hysteresis}`

- `addr` is a BLE address, with the same `public`/`static`-only rule as above.
- The rule reads `sensor.readings[name].value` from the live `AdvertiserTable` entry (populated by `msdr/ble/sensors.py`, see [ble.md](ble.md#sensors)). The lookup is defensive: a missing attribute, a missing reading or a non-numeric `.value` all mean "does not fire yet", never an error. A rule for a sensor name that never appears on that device never fires.
- Readings must be no more than 30 seconds old, judged by the sensor report's own timestamp. Unrelated advertisements do not refresh old readings.
- Hysteresis works as a Schmitt trigger:
  - `"above"` fires once `value >= threshold`, then re-arms only once `value <= threshold - hysteresis`. `"below"` is the mirror image.
  - With `hysteresis: 0`, the value must cross strictly to the safe side before re-arming. An unchanged value equal to the threshold never re-arms.
  - Crossings respect `cooldown_s`.

### `drone_geofence`

`config: {geometry: {type: "circle", center: {lat, lon}, radius_m} | {type: "polygon", points: [{lat, lon}, ...]}, max_age_s?}`

- The rule uses each UAS's reported drone position from the live Remote ID tracker (`msdr.api.services.remoteid`, `msdr.remoteid.tracker.Tracker`), never a verified or inferred one. A position the tracker marks stale, or one older than `max_age_s` (default 60 s), is never used.
- It fires (edge-triggered) when a UAS's reported position moves from outside to inside the geometry, and fires again on a later re-entry. The message always reads "reported position", for example *"drone uas:1 reported position inside the watched area"*.
- `circle` uses haversine distance from `center`. `polygon` uses ray-casting point containment (`msdr.rules.geo`), needs at least 3 points, and is treated as a small local area with no map projection.

## Engine (`msdr/rules/`)

| File | Role |
|---|---|
| `msdr/rules/geo.py` | `haversine_m`, `point_in_circle`, `point_in_polygon`: plain geometry, no I/O. |
| `msdr/rules/engine.py` | `Rule`, `Context`, `EngineState` and `FiredEvent` dataclasses and `evaluate(rules, ctx, state) -> list[FiredEvent]`. It is pure (no asyncio, database or FastAPI), so `tests/rules/` runs it directly on fixtures, including stale and duplicate inputs. |
| `msdr/rules/store.py` | `RulesRepo`, the SQLite side, in the **main** database (migration 022, alongside `device_labels` from migration 021), so backup and restore carry rules and the recent event log. |

Every rule kind shares one cooldown and dedup mechanism, keyed by `(rule_id, target)`. `target` is the beacon ident, the `addr:sensor_name` pair, or the drone's tracker key. A tick that hands the engine an unchanged reading, or runs twice for the same instant, never produces a second event for a condition that has not changed, thanks to edge-triggered logic plus the cooldown gate.

## Service (`msdr/api/services/rules.py`)

`RulesServiceMixin` has the same shape as `RemoteidServiceMixin` and `HistoryServiceMixin`. It owns a background tick (`RULES_TICK_S = 1.5`, within the Signal workbench watch's own 1-2 s range) that re-evaluates every enabled rule and persists whatever fires to `rule_events`. It reads:

- `self.wifi.table` and `self.ble.table` (`WifiServiceMixin`, `BleServiceMixin`) for beacon presence and sensor readings.
- `self.history_store` (`HistoryServiceMixin`) for the coverage fraction behind the "missing" mode, via `HistoryStore.covered_seconds()`. Minute totals are clipped conservatively at the window edges: time that might fall outside the window is subtracted, never credited toward an alert. With history disabled there is no coverage evidence, so "missing" rules cannot fire.
- `self.remoteid_tracker` (`RemoteidServiceMixin`) for drone positions.

## API

| Route | Notes |
|---|---|
| `GET /api/rules` | `{"items": [rule, ...]}` |
| `POST /api/rules` | body `{kind, name, config, enabled?, cooldown_s?}`; 422 on an invalid shape or a rotating BLE address |
| `PATCH /api/rules/{id}` | partial `{name?, config?, enabled?, cooldown_s?}`; 404 when absent |
| `DELETE /api/rules/{id}` | 404 when absent |
| `GET /api/rules/events?rule_id=&limit=` | `{"items": [event, ...]}`, newest first |
| `POST /api/rules/{id}/test` | evaluates that one rule against the live context right now, with a **fresh** engine state (it ignores cooldown and any in-progress presence/armed/inside edge). Returns `{"rule_id", "would_fire": [{target, message, detail}, ...]}`. Never persists an event. |

An event row is `{id, rule_id, kind, target, message, detail, fired_at}`.

## Limitations

- The engine's presence, armed and inside edges are **not** persisted across a restart. Only the cooldown clock is (seeded from `rule_events` at startup). A rule can therefore fire once more right after a restart if its condition was already true and the cooldown had expired.
- `rule_events` is bounded by row count (`MAX_EVENTS = 5000` in `msdr.rules.store`) and pruned opportunistically on insert. This is not time-based retention like device history.
- A `drone_geofence` polygon is tested in plain lat/lon with no map projection. That is accurate for small local areas, not for anything continent-scale.

See also: [BLE](ble.md), [Remote ID](remote-id.md), [Wi-Fi](wifi.md), [Device history](history.md).
