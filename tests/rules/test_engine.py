"""msdr/rules/engine.py: pure rule evaluation on fixtures (TODO "Phase 6 --
Watch rules" acceptance: "rule evaluation tests on fixtures, including stale
and duplicate inputs")."""

from __future__ import annotations

import pytest

from msdr.rules.engine import (
    BleAdvertiser,
    Context,
    DronePosition,
    EngineState,
    Rule,
    WifiNetwork,
    evaluate,
)

ADDR = "c0:11:22:33:44:55"


def no_coverage(receiver, start, end):
    return 0.0


def full_coverage(receiver, start, end):
    return 1.0


def _ble(last_seen: float, readings: dict | None = None) -> dict:
    return {
        ADDR: BleAdvertiser(
            addr=ADDR, identity="static", last_seen=last_seen, readings=readings or {}
        )
    }


def _drone(
    key: str, lat: float, lon: float, age_s: float = 1.0, stale: bool = False
) -> DronePosition:
    return DronePosition(key=key, latitude_deg=lat, longitude_deg=lon, age_s=age_s, stale=stale)


def _beacon_rule(mode="seen", cooldown_s=60.0, **extra) -> Rule:
    config = {"receiver": "ble", "ident": ADDR, "mode": mode, **extra}
    return Rule(
        id=1, kind="beacon_presence", name="r", enabled=True, config=config, cooldown_s=cooldown_s
    )


# --- beacon_presence: seen -------------------------------------------------


def test_beacon_seen_fires_once_on_appearance_then_not_again_while_present():
    rule = _beacon_rule(mode="seen")
    state = EngineState()
    assert evaluate([rule], Context(now=0.0, ble={}), state) == []

    ble = _ble(10.0)
    ctx_present = Context(now=10.0, ble=ble)
    events = evaluate([rule], ctx_present, state)
    assert len(events) == 1 and "seen on ble" in events[0].message

    # Duplicate tick, same unchanged input: must not refire.
    assert evaluate([rule], ctx_present, state) == []

    # A later tick where the device is still within the presence window.
    assert evaluate([rule], Context(now=15.0, ble=ble), state) == []


def test_beacon_seen_refires_after_disappearing_and_reappearing_past_cooldown():
    rule = _beacon_rule(mode="seen", cooldown_s=5.0)
    state = EngineState()
    assert len(evaluate([rule], Context(now=0.0, ble=_ble(0.0)), state)) == 1

    # Disappears (never seen again -- no entry at all).
    assert evaluate([rule], Context(now=60.0, ble={}), state) == []

    # Reappears well past cooldown: fires again.
    events = evaluate([rule], Context(now=120.0, ble=_ble(120.0)), state)
    assert len(events) == 1


def test_beacon_seen_within_cooldown_is_suppressed_even_on_a_new_edge():
    rule = _beacon_rule(mode="seen")  # default cooldown_s=60.0
    state = EngineState()
    assert len(evaluate([rule], Context(now=0.0, ble=_ble(0.0)), state)) == 1
    # Gone, then back within the 60 s cooldown -- a real new edge, but still
    # inside cooldown, so no second event.
    assert evaluate([rule], Context(now=10.0, ble={}), state) == []
    assert evaluate([rule], Context(now=20.0, ble=_ble(20.0)), state) == []


# --- beacon_presence: missing, coverage-aware ------------------------------


def test_beacon_missing_fires_only_when_receiver_had_coverage():
    rule = _beacon_rule(mode="missing", missing_after_minutes=10)
    ble = _ble(0.0)
    now = 10 * 60 + 1

    # No coverage during the missing window -- never an honest "missing" alert.
    state = EngineState()
    ctx_no_cov = Context(now=now, ble=ble, coverage_fraction=no_coverage)
    assert evaluate([rule], ctx_no_cov, state) == []

    # Full coverage -- fires.
    state = EngineState()
    ctx_full_cov = Context(now=now, ble=ble, coverage_fraction=full_coverage)
    events = evaluate([rule], ctx_full_cov, state)
    assert len(events) == 1
    assert "not seen on ble" in events[0].message
    assert "10 min" in events[0].message


def test_beacon_missing_fires_once_then_rearms_after_being_seen_again():
    # A device that has gone quiet stays in the live table with an aging
    # last_seen (real AdvertiserTable/NetworkTable behaviour) -- it does not
    # vanish from `ctx.ble` the instant it stops transmitting.
    rule = _beacon_rule(mode="missing", missing_after_minutes=1, cooldown_s=0.0)
    state = EngineState()
    quiet = _ble(0.0)
    now = 60.0 + 1
    ctx = Context(now=now, ble=quiet, coverage_fraction=full_coverage)
    assert len(evaluate([rule], ctx, state)) == 1

    # Duplicate/stale tick a little later, still the same aged last_seen:
    # already fired for this absence episode, must not refire.
    ctx2 = Context(now=now + 5, ble=quiet, coverage_fraction=full_coverage)
    assert evaluate([rule], ctx2, state) == []

    # Seen again -- re-arms -- then missing again later fires a fresh event.
    seen_again = now + 10
    ctx3 = Context(now=seen_again, ble=_ble(seen_again), coverage_fraction=full_coverage)
    assert evaluate([rule], ctx3, state) == []
    later = seen_again + 61
    ctx4 = Context(now=later, ble=_ble(seen_again), coverage_fraction=full_coverage)
    assert len(evaluate([rule], ctx4, state)) == 1


def test_beacon_missing_never_seen_does_not_fire():
    rule = _beacon_rule(mode="missing", missing_after_minutes=1)
    state = EngineState()
    ctx = Context(now=1000.0, ble={}, coverage_fraction=full_coverage)
    assert evaluate([rule], ctx, state) == []


def test_wifi_beacon_presence_uses_the_wifi_table():
    config = {"receiver": "wifi", "ident": "aa:bb:cc:dd:ee:ff", "mode": "seen"}
    rule = Rule(
        id=2, kind="beacon_presence", name="ap", enabled=True, config=config, cooldown_s=60.0
    )
    state = EngineState()
    wifi = {"aa:bb:cc:dd:ee:ff": WifiNetwork(bssid="aa:bb:cc:dd:ee:ff", last_seen=0.0)}
    events = evaluate([rule], Context(now=0.0, wifi=wifi), state)
    assert len(events) == 1 and "seen on wifi" in events[0].message


def test_disabled_rule_never_fires():
    base = _beacon_rule(mode="seen")
    rule = Rule(id=base.id, kind=base.kind, name=base.name, enabled=False, config=base.config)
    state = EngineState()
    assert evaluate([rule], Context(now=0.0, ble=_ble(0.0)), state) == []


# --- sensor_threshold: hysteresis ------------------------------------------


def _sensor_rule(comparison="above", threshold=30.0, hysteresis=2.0, cooldown_s=0.0) -> Rule:
    config = {
        "addr": ADDR, "sensor_name": "temp_c",
        "comparison": comparison, "threshold": threshold, "hysteresis": hysteresis,
    }
    return Rule(
        id=3, kind="sensor_threshold", name="hot", enabled=True,
        config=config, cooldown_s=cooldown_s,
    )


def _reading(value: float) -> dict:
    return _ble(0.0, {"temp_c": value})


def test_sensor_above_threshold_hysteresis_prevents_chatter():
    rule = _sensor_rule(comparison="above", threshold=30.0, hysteresis=2.0)
    state = EngineState()
    assert evaluate([rule], Context(now=0.0, ble=_reading(29.0)), state) == []
    # Crosses above -- fires.
    assert len(evaluate([rule], Context(now=1.0, ble=_reading(31.0)), state)) == 1
    # Oscillates just under threshold (inside the hysteresis band): no refire.
    assert evaluate([rule], Context(now=2.0, ble=_reading(29.5)), state) == []
    assert evaluate([rule], Context(now=3.0, ble=_reading(31.0)), state) == []
    # Drops past the reset level (threshold - hysteresis = 28.0): rearms.
    assert evaluate([rule], Context(now=4.0, ble=_reading(27.0)), state) == []
    # Crosses above again: fires again.
    assert len(evaluate([rule], Context(now=5.0, ble=_reading(31.0)), state)) == 1


def test_sensor_below_threshold_hysteresis():
    rule = _sensor_rule(comparison="below", threshold=10.0, hysteresis=1.0)
    state = EngineState()
    assert evaluate([rule], Context(now=0.0, ble=_reading(20.0)), state) == []
    assert len(evaluate([rule], Context(now=1.0, ble=_reading(9.0)), state)) == 1
    assert evaluate([rule], Context(now=2.0, ble=_reading(9.5)), state) == []
    # Past the reset level (threshold + hysteresis = 11.0): no refire yet.
    assert evaluate([rule], Context(now=3.0, ble=_reading(12.0)), state) == []
    assert len(evaluate([rule], Context(now=4.0, ble=_reading(9.0)), state)) == 1


@pytest.mark.parametrize(
    ("comparison", "threshold", "trigger", "equal", "safe"),
    [("above", 30.0, 31.0, 30.0, 29.0), ("below", 10.0, 9.0, 10.0, 11.0)],
)
def test_zero_hysteresis_requires_crossing_safe_side_to_rearm(
    comparison, threshold, trigger, equal, safe
):
    rule = _sensor_rule(comparison=comparison, threshold=threshold, hysteresis=0.0)
    state = EngineState()
    assert len(evaluate([rule], Context(now=0.0, ble=_reading(trigger)), state)) == 1
    # Equality remains on the triggered side when hysteresis is zero.
    assert evaluate([rule], Context(now=1.0, ble=_reading(equal)), state) == []
    assert evaluate([rule], Context(now=2.0, ble=_reading(trigger)), state) == []
    assert evaluate([rule], Context(now=3.0, ble=_reading(equal)), state) == []
    assert evaluate([rule], Context(now=4.0, ble=_reading(safe)), state) == []
    assert len(evaluate([rule], Context(now=5.0, ble=_reading(trigger)), state)) == 1


def test_sensor_threshold_uses_sensor_freshness_not_advertisement_freshness():
    rule = _sensor_rule(comparison="above", threshold=30.0, hysteresis=0.0)
    state = EngineState()
    old_sensor = {
        ADDR: BleAdvertiser(
            addr=ADDR, identity="static", last_seen=100.0,
            readings={"temp_c": 31.0}, readings_at=69.0,
        )
    }
    assert evaluate([rule], Context(now=100.0, ble=old_sensor), state) == []
    fresh_sensor = {
        ADDR: BleAdvertiser(
            addr=ADDR, identity="static", last_seen=100.0,
            readings={"temp_c": 31.0}, readings_at=100.0,
        )
    }
    assert len(evaluate([rule], Context(now=100.0, ble=fresh_sensor), state)) == 1


def test_sensor_reading_absent_never_raises_and_never_fires():
    rule = _sensor_rule()
    state = EngineState()
    assert evaluate([rule], Context(now=0.0, ble=_ble(0.0)), state) == []
    assert evaluate([rule], Context(now=0.0, ble={}), state) == []


# --- drone_geofence ---------------------------------------------------------


def _geofence_rule(**extra) -> Rule:
    config = {
        "geometry": {"type": "circle", "center": {"lat": 0.0, "lon": 0.0}, "radius_m": 100.0},
        **extra,
    }
    return Rule(
        id=4, kind="drone_geofence", name="geo", enabled=True, config=config, cooldown_s=0.0
    )


def test_drone_geofence_fires_on_entry_reported_position_wording():
    rule = _geofence_rule()
    state = EngineState()
    inside = [_drone("uas:1", 0.0001, 0.0)]
    events = evaluate([rule], Context(now=0.0, drones=inside), state)
    assert len(events) == 1
    assert "reported position" in events[0].message
    assert "verified" not in events[0].message.lower()
    assert "located" not in events[0].message.lower()


def test_drone_geofence_outside_never_fires():
    rule = _geofence_rule()
    state = EngineState()
    outside = [_drone("uas:1", 5.0, 5.0)]
    assert evaluate([rule], Context(now=0.0, drones=outside), state) == []


def test_drone_geofence_stale_position_never_fires():
    rule = _geofence_rule()
    state = EngineState()
    stale = [_drone("uas:1", 0.0001, 0.0, age_s=500.0, stale=True)]
    assert evaluate([rule], Context(now=0.0, drones=stale), state) == []


def test_drone_geofence_too_old_for_rules_max_age_never_fires():
    rule = _geofence_rule(max_age_s=10.0)
    state = EngineState()
    old_but_not_stale = [_drone("uas:1", 0.0001, 0.0, age_s=20.0, stale=False)]
    assert evaluate([rule], Context(now=0.0, drones=old_but_not_stale), state) == []


def test_drone_geofence_does_not_refire_every_tick_while_still_inside():
    rule = _geofence_rule()
    state = EngineState()
    inside = [_drone("uas:1", 0.0001, 0.0)]
    assert len(evaluate([rule], Context(now=0.0, drones=inside), state)) == 1
    # Still inside on the next tick (a duplicate-like, unchanged position).
    assert evaluate([rule], Context(now=1.0, drones=inside), state) == []


def test_drone_geofence_refires_on_leave_and_reenter():
    rule = _geofence_rule()
    state = EngineState()
    inside = [_drone("uas:1", 0.0001, 0.0)]
    outside = [_drone("uas:1", 5.0, 5.0)]
    assert len(evaluate([rule], Context(now=0.0, drones=inside), state)) == 1
    assert evaluate([rule], Context(now=1.0, drones=outside), state) == []
    events = evaluate([rule], Context(now=2.0, drones=inside), state)
    assert len(events) == 1


def test_drone_geofence_polygon():
    square = [
        {"lat": -0.001, "lon": -0.001}, {"lat": -0.001, "lon": 0.001},
        {"lat": 0.001, "lon": 0.001}, {"lat": 0.001, "lon": -0.001},
    ]
    config = {"geometry": {"type": "polygon", "points": square}}
    rule = Rule(
        id=5, kind="drone_geofence", name="poly", enabled=True, config=config, cooldown_s=0.0
    )
    state = EngineState()
    inside = [_drone("uas:1", 0.0, 0.0)]
    assert len(evaluate([rule], Context(now=0.0, drones=inside), state)) == 1
