"""Pure watch-rule evaluation (docs/rules.md). No asyncio, no database, no
FastAPI -- `msdr.api.services.rules.RulesServiceMixin` gathers live state
into a `Context`, keeps one `EngineState` across ticks, and persists whatever
`evaluate()` returns as `rule_events` rows. This separation is what lets
`tests/rules/test_engine.py` exercise every rule kind on plain fixtures.

Three rule kinds:

- `beacon_presence`: a Wi-Fi BSSID or stable BLE address (never a rotating
  `rpa`/`nrpa` -- refused at the API layer the same way
  `msdr.history.labels.check_labelable` refuses a label) appears, or has not
  been heard for N minutes *while its receiver was listening* (coverage-
  aware: `Context.coverage_fraction` reports how much of that window the
  receiver actually covered, and a "missing" rule never fires when that
  fraction is too low to mean anything).
- `sensor_threshold`: a BLE sensor reading above/below a value, with
  hysteresis so a value oscillating around the threshold does not refire
  every tick.
- `drone_geofence`: a Remote ID drone's *reported* position (never "verified"
  or "located") inside an operator-defined circle or polygon, only using
  fresh, non-stale positions.

Every rule kind shares one cooldown/dedup mechanism (`_cooldown_ok`/
`_record_fire`, keyed by `(rule.id, target)`) so stale or duplicate inputs
-- the same unchanged reading handed to `evaluate()` again, or a tick that
runs twice for the same timestamp -- never produce a second event for a
condition that has not actually changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from msdr.rules.geo import point_in_circle, point_in_polygon

RuleKind = Literal["beacon_presence", "sensor_threshold", "drone_geofence"]

#: An entry/reading this old or newer counts as "currently present"/"fresh".
PRESENCE_WINDOW_S = 30.0
#: Default freshness ceiling for a drone_geofence rule's reported position.
DEFAULT_MAX_AGE_S = 60.0
#: A "missing" beacon rule only fires when the receiver covered at least this
#: fraction of the missing window -- otherwise "not seen" could just mean
#: "receiver was off", which is never an honest alert.
MIN_COVERAGE_FRACTION = 0.5


@dataclass(frozen=True)
class Rule:
    id: int
    kind: RuleKind
    name: str
    enabled: bool
    config: dict
    cooldown_s: float = 60.0


@dataclass(frozen=True)
class WifiNetwork:
    bssid: str
    last_seen: float


@dataclass(frozen=True)
class BleAdvertiser:
    addr: str
    identity: str  # public | static | rpa | nrpa | random
    last_seen: float
    readings: dict[str, float] = field(default_factory=dict)
    readings_at: float | None = None


@dataclass(frozen=True)
class DronePosition:
    key: str
    latitude_deg: float
    longitude_deg: float
    age_s: float
    stale: bool


@dataclass
class Context:
    now: float
    wifi: dict[str, WifiNetwork] = field(default_factory=dict)
    ble: dict[str, BleAdvertiser] = field(default_factory=dict)
    drones: list[DronePosition] = field(default_factory=list)
    #: `(receiver, start, end) -> fraction in [0, 1]` of that window the
    #: receiver was listening. Supplied by the caller (HistoryStore-backed in
    #: production); a constant function in tests.
    coverage_fraction: Callable[[str, float, float], float] = lambda receiver, start, end: 0.0  # noqa: E731


@dataclass
class FiredEvent:
    rule_id: int
    kind: RuleKind
    target: str
    message: str
    at: float
    detail: dict = field(default_factory=dict)


@dataclass
class _BeaconState:
    present: bool = False
    missing_fired: bool = False


@dataclass
class _SensorState:
    armed_above: bool = False
    armed_below: bool = False


@dataclass
class _GeofenceState:
    inside: set = field(default_factory=set)


@dataclass
class EngineState:
    """Per-rule working memory carried between ticks by the caller. Not
    itself persisted across a restart (docs/rules.md "Limits") except for
    `last_fired`, which `RulesServiceMixin` seeds from `rule_events` at
    startup so a cooldown already in progress survives a restart even though
    the presence/armed/inside edges do not."""

    beacon: dict = field(default_factory=dict)
    sensor: dict = field(default_factory=dict)
    geofence: dict = field(default_factory=dict)
    last_fired: dict = field(default_factory=dict)  # (rule_id, target) -> ts


def _cooldown_ok(state: EngineState, rule: Rule, target: str, now: float) -> bool:
    last = state.last_fired.get((rule.id, target))
    return last is None or (now - last) >= rule.cooldown_s


def _record_fire(state: EngineState, rule: Rule, target: str, now: float) -> None:
    state.last_fired[(rule.id, target)] = now


def _fire(rule: Rule, target: str, now: float, message: str, **detail) -> FiredEvent:
    return FiredEvent(
        rule_id=rule.id, kind=rule.kind, target=target, message=message, at=now, detail=detail
    )


def evaluate(rules: list[Rule], ctx: Context, state: EngineState) -> list[FiredEvent]:
    events: list[FiredEvent] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.kind == "beacon_presence":
            events.extend(_eval_beacon(rule, ctx, state))
        elif rule.kind == "sensor_threshold":
            events.extend(_eval_sensor(rule, ctx, state))
        elif rule.kind == "drone_geofence":
            events.extend(_eval_geofence(rule, ctx, state))
    return events


def _eval_beacon(rule: Rule, ctx: Context, state: EngineState) -> list[FiredEvent]:
    cfg = rule.config
    receiver = cfg["receiver"]
    ident = cfg["ident"]
    mode = cfg.get("mode", "seen")
    table = ctx.wifi if receiver == "wifi" else ctx.ble
    entry = table.get(ident)
    st = state.beacon.setdefault(rule.id, _BeaconState())
    now = ctx.now
    out: list[FiredEvent] = []
    currently_present = entry is not None and (now - entry.last_seen) <= PRESENCE_WINDOW_S

    if mode == "seen":
        if currently_present and not st.present:
            if _cooldown_ok(state, rule, ident, now):
                out.append(
                    _fire(rule, ident, now, f"{ident} seen on {receiver}", receiver=receiver)
                )
                _record_fire(state, rule, ident, now)
        st.present = currently_present
        return out

    # mode == "missing": edge-triggered once per absence episode, re-arms
    # the next time the beacon is seen again.
    if currently_present:
        st.present = True
        st.missing_fired = False
        return out
    st.present = False
    if entry is None or st.missing_fired:
        # `entry is None` means never seen (or evicted from the live table's
        # bounded size) rather than "currently quiet": a device that stops
        # transmitting keeps its aging last_seen row in the live
        # NetworkTable/AdvertiserTable, it does not disappear the instant it
        # goes quiet, so this case never needs an age check of its own.
        return out
    minutes = float(cfg["missing_after_minutes"])
    window_s = minutes * 60.0
    age_s = now - entry.last_seen
    if age_s < window_s:
        return out
    fraction = ctx.coverage_fraction(receiver, now - window_s, now)
    if fraction < MIN_COVERAGE_FRACTION:
        return out  # the receiver was not actually listening enough to call this a miss
    if _cooldown_ok(state, rule, ident, now):
        out.append(
            _fire(
                rule, ident, now,
                f"{ident} not seen on {receiver} for {minutes:.0f} min "
                f"(receiver listened {fraction * 100:.0f}% of that time)",
                receiver=receiver, coverage_fraction=fraction,
            )
        )
        _record_fire(state, rule, ident, now)
        st.missing_fired = True
    return out


def _eval_sensor(rule: Rule, ctx: Context, state: EngineState) -> list[FiredEvent]:
    cfg = rule.config
    addr = cfg["addr"]
    name = cfg["sensor_name"]
    comparison = cfg["comparison"]  # "above" | "below"
    threshold = float(cfg["threshold"])
    hysteresis = float(cfg.get("hysteresis", 0.0))
    entry = ctx.ble.get(addr)
    if entry is None:
        return []
    reading_time = entry.readings_at if entry.readings_at is not None else entry.last_seen
    if not 0 <= ctx.now - reading_time <= PRESENCE_WINDOW_S:
        return []
    value = entry.readings.get(name)
    if value is None:
        return []  # sensor not present on this advertiser (yet, or ever) -- never an error
    st = state.sensor.setdefault(rule.id, _SensorState())
    target = f"{addr}:{name}"
    now = ctx.now
    out: list[FiredEvent] = []
    if comparison == "above":
        if not st.armed_above and value >= threshold:
            if _cooldown_ok(state, rule, target, now):
                out.append(
                    _fire(rule, target, now, f"{name} = {value:g} (>= {threshold:g})", value=value)
                )
                _record_fire(state, rule, target, now)
            st.armed_above = True
        elif st.armed_above and value < threshold and value <= threshold - hysteresis:
            st.armed_above = False
    elif comparison == "below":
        if not st.armed_below and value <= threshold:
            if _cooldown_ok(state, rule, target, now):
                out.append(
                    _fire(rule, target, now, f"{name} = {value:g} (<= {threshold:g})", value=value)
                )
                _record_fire(state, rule, target, now)
            st.armed_below = True
        elif st.armed_below and value > threshold and value >= threshold + hysteresis:
            st.armed_below = False
    else:
        raise ValueError(f"unknown comparison: {comparison!r}")
    return out


def _inside_geometry(geometry: dict, lat: float, lon: float) -> bool:
    if geometry["type"] == "circle":
        center = geometry["center"]
        return point_in_circle(lat, lon, center["lat"], center["lon"], geometry["radius_m"])
    if geometry["type"] == "polygon":
        return point_in_polygon(lat, lon, [(p["lat"], p["lon"]) for p in geometry["points"]])
    raise ValueError(f"unknown geometry type: {geometry['type']!r}")


def _eval_geofence(rule: Rule, ctx: Context, state: EngineState) -> list[FiredEvent]:
    cfg = rule.config
    geometry = cfg["geometry"]
    max_age_s = float(cfg.get("max_age_s", DEFAULT_MAX_AGE_S))
    st = state.geofence.setdefault(rule.id, _GeofenceState())
    now = ctx.now
    out: list[FiredEvent] = []
    current_inside = set()
    for drone in ctx.drones:
        if drone.stale or drone.age_s > max_age_s:
            continue
        if not _inside_geometry(geometry, drone.latitude_deg, drone.longitude_deg):
            continue
        current_inside.add(drone.key)
        if drone.key in st.inside:
            continue  # already inside last tick -- edge-triggered, not re-fired every tick
        if _cooldown_ok(state, rule, drone.key, now):
            out.append(
                _fire(
                    rule, drone.key, now,
                    f"drone {drone.key} reported position inside the watched area",
                    latitude_deg=drone.latitude_deg, longitude_deg=drone.longitude_deg,
                    age_s=drone.age_s,
                )
            )
            _record_fire(state, rule, drone.key, now)
    st.inside = current_inside
    return out
