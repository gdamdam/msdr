"""/api/rules* end to end (docs/rules.md): CRUD, validation that needs live
context (BLE rpa/nrpa refusal), the background tick actually firing and
persisting events, and the geofence/sensor rule kinds."""

from __future__ import annotations

import asyncio
import time

from tests.api.test_app import running_app
from tests.api.test_ble import _ble_settings, _wait_for_advertisers

STATIC = "c0:11:22:33:44:55"
RPA = "4a:34:3f:50:52:88"


async def test_rules_crud_and_validation():
    async with running_app() as (app, client, adapter):
        bad_kind = await client.post(
            "/api/rules", json={"kind": "nope", "name": "x", "config": {}}
        )
        assert bad_kind.status_code == 422

        bad_geometry = await client.post(
            "/api/rules",
            json={
                "kind": "drone_geofence", "name": "geo",
                "config": {"geometry": {"type": "triangle"}},
            },
        )
        assert bad_geometry.status_code == 422

        created = await client.post(
            "/api/rules",
            json={
                "kind": "drone_geofence", "name": "geo",
                "config": {
                    "geometry": {
                        "type": "circle", "center": {"lat": 1.0, "lon": 2.0}, "radius_m": 500,
                    },
                },
            },
        )
        assert created.status_code == 200
        rule = created.json()
        assert rule["cooldown_s"] == 60.0 and rule["enabled"] is True

        listed = (await client.get("/api/rules")).json()["items"]
        assert len(listed) == 1 and listed[0]["id"] == rule["id"]

        patched = await client.patch(f"/api/rules/{rule['id']}", json={"enabled": False})
        assert patched.status_code == 200 and patched.json()["enabled"] is False

        missing = await client.patch("/api/rules/999999", json={"enabled": True})
        assert missing.status_code == 404

        deleted = await client.delete(f"/api/rules/{rule['id']}")
        assert deleted.status_code == 200 and deleted.json()["ok"] is True
        assert (await client.get("/api/rules")).json()["items"] == []


async def test_beacon_presence_rule_refuses_rotating_ble_address():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)

        refused = await client.post(
            "/api/rules",
            json={
                "kind": "beacon_presence", "name": "rotating",
                "config": {"receiver": "ble", "ident": RPA, "mode": "seen"},
            },
        )
        assert refused.status_code == 422 and "rotates" in refused.json()["detail"]

        ok = await client.post(
            "/api/rules",
            json={
                "kind": "beacon_presence", "name": "bean",
                "config": {"receiver": "ble", "ident": STATIC, "mode": "seen"},
            },
        )
        assert ok.status_code == 200


async def test_beacon_presence_tick_fires_and_persists_an_event():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)

        created = await client.post(
            "/api/rules",
            json={
                "kind": "beacon_presence", "name": "bean", "cooldown_s": 1,
                "config": {"receiver": "ble", "ident": STATIC, "mode": "seen"},
            },
        )
        rule_id = created.json()["id"]

        test_now = await client.post(f"/api/rules/{rule_id}/test")
        assert test_now.status_code == 200
        would_fire = test_now.json()["would_fire"]
        assert len(would_fire) == 1 and would_fire[0]["target"] == STATIC

        for _ in range(40):
            events = (await client.get("/api/rules/events")).json()["items"]
            if events:
                break
            await asyncio.sleep(0.1)
        assert events and events[0]["rule_id"] == rule_id and events[0]["target"] == STATIC

        scoped_resp = await client.get("/api/rules/events", params={"rule_id": rule_id})
        scoped = scoped_resp.json()["items"]
        assert scoped and all(e["rule_id"] == rule_id for e in scoped)


async def test_sensor_threshold_rule_skips_gracefully_without_sensor_data():
    """msdr/ble/sensors.py is a concurrent addition; until an advertiser
    actually carries `.sensor.readings`, a sensor_threshold rule must never
    error, just never fire (docs/rules.md)."""
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)

        created = await client.post(
            "/api/rules",
            json={
                "kind": "sensor_threshold", "name": "hot",
                "config": {
                    "addr": STATIC, "sensor_name": "temp_c",
                    "comparison": "above", "threshold": 30.0, "hysteresis": 1.0,
                },
            },
        )
        assert created.status_code == 200
        result = await client.post(f"/api/rules/{created.json()['id']}/test")
        assert result.status_code == 200 and result.json()["would_fire"] == []


async def test_sensor_threshold_rule_fires_against_the_real_bthome_decoder():
    """msdr/ble/sensors.py now exists (concurrent addition): feed a real
    BTHome advertisement into the live table and confirm the rule reads
    `.sensor.readings["temperature"].value` the way docs/rules.md says."""
    # Service-data AD structure wrapping msdr/ble/sensors.py's BTHome worked
    # example (0xD2FC 40 02C409 03BF13 -> 25.00 degC), same as
    # tests/ble/test_observations.py.
    data = "0a16d2fc4002c40903bf13"
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)
        app.state.service.ble.table.ingest(
            {
                "ts": time.time(), "addr": STATIC, "addr_type": "static",
                "evt": "ADV_NONCONN_IND", "rssi": -70, "data": data,
            }
        )
        created = await client.post(
            "/api/rules",
            json={
                "kind": "sensor_threshold", "name": "hot",
                "config": {
                    "addr": STATIC, "sensor_name": "temperature",
                    "comparison": "above", "threshold": 20.0, "hysteresis": 1.0,
                },
            },
        )
        assert created.status_code == 200
        result = (await client.post(f"/api/rules/{created.json()['id']}/test")).json()
        assert len(result["would_fire"]) == 1
        assert result["would_fire"][0]["detail"]["value"] == 25.0


async def test_fresh_non_sensor_ad_does_not_refresh_sensor_rule_data():
    now = time.time()
    stale_sensor_at = now - 31.0
    data = "0a16d2fc4002c40903bf13"
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)
        table = app.state.service.ble.table
        table.ingest(
            {"ts": stale_sensor_at, "addr": STATIC, "addr_type": "static",
             "evt": "ADV_NONCONN_IND", "rssi": -70, "data": data}
        )
        table.ingest(
            {"ts": time.time(), "addr": STATIC, "addr_type": "static",
             "evt": "ADV_NONCONN_IND", "rssi": -69, "data": "020106"}
        )

        entry = app.state.service._rules_ble_entries()[STATIC]
        assert entry.last_seen > stale_sensor_at + 20
        assert entry.readings_at == stale_sensor_at

        created = await client.post(
            "/api/rules",
            json={"kind": "sensor_threshold", "name": "hot", "config": {
                "addr": STATIC, "sensor_name": "temperature", "comparison": "above",
                "threshold": 20.0,
            }},
        )
        result = await client.post(f"/api/rules/{created.json()['id']}/test")
        assert result.status_code == 200 and result.json()["would_fire"] == []
        await client.post("/api/ble/stop")


async def test_missing_rule_uses_conservative_coverage_and_history_setting():
    from msdr.rules.engine import BleAdvertiser, Context, EngineState, Rule, evaluate
    from msdr.settings import HistorySettings

    settings = _ble_settings()
    settings.history = HistorySettings(enabled=True)  # off by default
    async with running_app(settings=settings) as (app, client, adapter):
        service = app.state.service
        rule = Rule(
            id=50, kind="beacon_presence", name="quiet", enabled=True,
            config={"receiver": "ble", "ident": STATIC, "mode": "missing",
                    "missing_after_minutes": 1.0}, cooldown_s=0,
        )
        quiet = {STATIC: BleAdvertiser(addr=STATIC, identity="static", last_seen=-1.0)}
        # 59 seconds of coverage end before the absence interval [59, 119).
        service.history_store.add_coverage("ble", 59.0, 0, 59.0)
        assert service._rules_coverage_fraction("ble", 59.0, 119.0) == 0.0
        assert evaluate(
            [rule], Context(now=119.0, ble=quiet,
                            coverage_fraction=service._rules_coverage_fraction), EngineState()
        ) == []

        service.history_store.add_coverage("ble", 119.0, 0, 60.0)
        # Minute buckets lose within-minute timing, so the conservative lower
        # bound is slightly below 1.0 even with continuous recorded coverage.
        assert service._rules_coverage_fraction("ble", 59.0, 119.0) >= 0.9
        events = evaluate(
            [rule], Context(now=119.0, ble=quiet,
                            coverage_fraction=service._rules_coverage_fraction), EngineState()
        )
        assert len(events) == 1

        service.settings.history.enabled = False
        assert service._rules_coverage_fraction("ble", 59.0, 119.0) == 0.0


async def test_sensor_threshold_rule_refuses_rotating_ble_address():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)
        refused = await client.post(
            "/api/rules",
            json={
                "kind": "sensor_threshold", "name": "hot",
                "config": {
                    "addr": RPA, "sensor_name": "temp_c",
                    "comparison": "above", "threshold": 30.0,
                },
            },
        )
        assert refused.status_code == 422 and "rotates" in refused.json()["detail"]


async def test_drone_geofence_rule_fires_from_a_reported_position():
    from msdr.rules.engine import DronePosition

    async with running_app() as (app, client, adapter):
        service = app.state.service
        # The Remote ID tracker itself is exercised in tests/remoteid/; here
        # only the rules wiring (_rules_drones -> engine -> event log) is
        # under test, so the drone list is supplied directly.
        service._rules_drones = lambda: [
            DronePosition(key="uas:1", latitude_deg=1.0, longitude_deg=2.0, age_s=1.0, stale=False)
        ]
        created = await client.post(
            "/api/rules",
            json={
                "kind": "drone_geofence", "name": "geo", "cooldown_s": 1,
                "config": {
                    "geometry": {
                        "type": "circle", "center": {"lat": 1.0, "lon": 2.0}, "radius_m": 500,
                    },
                },
            },
        )
        rule_id = created.json()["id"]
        result = (await client.post(f"/api/rules/{rule_id}/test")).json()
        assert len(result["would_fire"]) == 1
        assert "reported position" in result["would_fire"][0]["message"]

        for _ in range(40):
            events = (await client.get("/api/rules/events")).json()["items"]
            if events:
                break
            await asyncio.sleep(0.1)
        assert events and events[0]["target"] == "uas:1"
