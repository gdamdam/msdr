"""/api/ble/*: lifecycle, advertisers, live feed and settings PATCH (docs/
ble.md). Uses tests/ble/fake_helper.py as a real subprocess via
settings.ble.helper_command -- never a real dongle."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from msdr.settings import BleSettings, Settings
from tests.api.test_app import running_app

FAKE_HELPER = Path(__file__).resolve().parent.parent / "ble" / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]

_ROW_KEYS = {
    "addr", "addr_type", "identity", "name", "company_id", "company",
    "connectable", "service_uuids", "remote_id", "rssi", "reports",
    "first_seen", "last_seen", "evt_types", "phy", "channel", "extended",
    "sensor", "service_names", "apple",
}


def _ble_settings(**overrides) -> Settings:
    return Settings(
        db_path=":memory:",
        ble=BleSettings(enabled=True, helper_command=HELPER_COMMAND, **overrides),
    )


async def _wait_for_advertisers(client, minimum: int = 4) -> dict:
    deadline = time.monotonic() + 5
    body = {"items": [], "total": 0}
    while time.monotonic() < deadline:
        body = (await client.get("/api/ble/advertisers")).json()
        if body["total"] >= minimum:
            break
        await asyncio.sleep(0.02)
    assert body["total"] >= minimum
    return body


async def test_status_and_devices_work_while_feature_disabled():
    # docs/ble.md: the Setup card needs status/devices to work while
    # disabled (to show "stopped"/enabled: false in the first place); only
    # start refuses.
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.get("/api/ble/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "stopped"
        assert body["enabled"] is False
        assert body["device"] is None

        resp = await client.get("/api/ble/devices")
        assert resp.status_code == 200

        resp = await client.post("/api/ble/start")
        assert resp.status_code == 409

        # Stop is always allowed (idempotent cleanup), not gated.
        resp = await client.post("/api/ble/stop")
        assert resp.status_code == 200


async def test_lifecycle_start_status_shape_and_stop():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        resp = await client.post("/api/ble/start")
        assert resp.status_code == 200

        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = (await client.get("/api/ble/status")).json()
            if status["state"] == "receiving":
                break
            await asyncio.sleep(0.02)
        assert status is not None and status["state"] == "receiving"
        assert status["enabled"] is True
        assert status["reports"] > 0
        assert status["advertiser_count"] > 0
        assert "coverage" in status

        resp = await client.post("/api/ble/stop")
        assert resp.status_code == 200
        assert resp.json()["state"] == "stopped"


async def test_devices_endpoint():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        resp = await client.get("/api/ble/devices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["devices"][0]["product"] == "BCM20702A0"

        status = (await client.get("/api/ble/status")).json()
        assert status["device"]["product"] == "BCM20702A0"


async def test_advertisers_appear_with_row_shape_and_remote_id():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        body = await _wait_for_advertisers(client)
        assert "since" in body

        row = next(r for r in body["items"] if r["addr"] == "00:11:22:33:44:66")
        assert row["remote_id"] is True
        assert row["addr_type"] == "public"
        assert row["identity"] == "public"
        assert set(row.keys()) == _ROW_KEYS

        others = [r for r in body["items"] if r["addr"] != "00:11:22:33:44:66"]
        assert others and all(r["remote_id"] is False for r in others)

        await client.post("/api/ble/stop")


async def test_advertisers_sort_order_query_and_pagination():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        limited = (await client.get("/api/ble/advertisers?limit=1")).json()
        assert len(limited["items"]) == 1

        over_cap = await client.get("/api/ble/advertisers?limit=10000")
        assert over_cap.status_code == 422

        asc = (await client.get("/api/ble/advertisers", params={"sort": "addr"})).json()["items"]
        desc = (await client.get(
            "/api/ble/advertisers", params={"sort": "addr", "order": "desc"}
        )).json()["items"]
        assert [r["addr"] for r in asc] == sorted(r["addr"] for r in asc)
        assert [r["addr"] for r in desc] == sorted((r["addr"] for r in asc), reverse=True)
        assert (
            await client.get("/api/ble/advertisers", params={"sort": "nope"})
        ).status_code == 422

        filtered = (await client.get("/api/ble/advertisers", params={"q": "44:66"})).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["addr"] == "00:11:22:33:44:66"

        await client.post("/api/ble/stop")


async def test_advertisers_sort_by_identity():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        asc = (await client.get(
            "/api/ble/advertisers", params={"sort": "identity"}
        )).json()["items"]
        kinds = [r["identity"] for r in asc]
        order = {"public": 0, "static": 1, "rpa": 2, "nrpa": 3, "random": 4}
        assert kinds == sorted(kinds, key=lambda k: order[k])

        await client.post("/api/ble/stop")


async def test_advertisers_sort_by_sensor_is_supported():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        # BTHome v2 temperature object: 25.00 °C. The fake helper also supplies
        # several non-sensor rows, exercising the missing-value sort key.
        app.state.service.ble.table.ingest(
            {"ts": time.time(), "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "static",
             "evt": "ADV_NONCONN_IND", "rssi": -40, "data": "0a16d2fc4002c40903bf13"}
        )
        response = await client.get("/api/ble/advertisers", params={"sort": "sensor"})
        assert response.status_code == 200
        rows = response.json()["items"]
        assert rows
        assert rows[0]["addr"] == "aa:bb:cc:dd:ee:ff"

        await client.post("/api/ble/stop")


async def test_ble_phy_settings_changed_while_running_apply_on_next_start():
    async with running_app(settings=_ble_settings(backend="sniffle", phy="1m")) as (
        app, client, adapter
    ):
        assert (await client.post("/api/ble/start")).status_code == 200
        current = app.state.service.ble
        assert current.phy == "1m"

        patched = await client.patch("/api/settings", json={"ble": {"phy": "coded"}})
        assert patched.status_code == 200
        assert app.state.service.ble is current
        assert app.state.service._ble_settings_pending is True

        await client.post("/api/ble/stop")
        assert (await client.post("/api/ble/start")).status_code == 200
        assert app.state.service.ble is not current
        assert app.state.service.ble.phy == "coded"
        await client.post("/api/ble/stop")


async def test_advertisers_sort_by_device_uses_label():
    # c0:11:22:33:44:55 (fake_helper's "TestBean", a static address) is
    # labelable (docs/history.md labels); its advertised name already starts
    # with "T", so a label starting with "A" must move it to the front of an
    # ascending device-name sort -- proof that ble_advertisers() passes
    # display_names through to AdvertiserTable.rows() (sort == "device").
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        put = await client.put(
            "/api/labels/ble/c0:11:22:33:44:55",
            json={"name": "Aardvark Beacon", "class": "mine"},
        )
        assert put.status_code == 200

        asc = (await client.get(
            "/api/ble/advertisers", params={"sort": "device"}
        )).json()["items"]
        assert asc[0]["addr"] == "c0:11:22:33:44:55"

        await client.post("/api/ble/stop")


async def test_advertiser_details_shape_and_404():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        details = await client.get("/api/ble/advertisers/00:11:22:33:44:66")
        assert details.status_code == 200
        d = details.json()
        assert d["addr"] == "00:11:22:33:44:66"
        assert d["remote_id"] is True
        for key in (
            "flags", "tx_power", "appearance", "service_data", "manufacturer_data",
            "rssi_history", "payloads", "recent", "name_complete", "service_names",
        ):
            assert key in d
        assert all(isinstance(pair, list) and len(pair) == 2 for pair in d["rssi_history"])

        missing = await client.get("/api/ble/advertisers/aa:bb:cc:dd:ee:ff")
        assert missing.status_code == 404

        await client.post("/api/ble/stop")


async def test_live_feed_after_semantics():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        deadline = time.monotonic() + 5
        feed = {"items": []}
        while time.monotonic() < deadline:
            feed = (await client.get("/api/ble/live")).json()
            if feed["items"]:
                break
            await asyncio.sleep(0.02)
        assert feed["items"], "live feed stays empty while advertisements arrive"
        seqs = [f["seq"] for f in feed["items"]]
        assert seqs == sorted(seqs)
        newer = (await client.get("/api/ble/live", params={"after": feed["seq"]})).json()
        assert all(f["seq"] > feed["seq"] for f in newer["items"])

        await client.post("/api/ble/stop")


async def test_clear_empties_table_while_receiving():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)

        cleared_at = time.time()
        resp = await client.post("/api/ble/advertisers/clear")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Capture keeps running (the fake helper reports every 20 ms), so a
        # report may land between the clear and this read: everything in
        # the table must have been heard after the clear, not that it is
        # empty.
        cleared = (await client.get("/api/ble/advertisers")).json()
        assert all(row["first_seen"] >= cleared_at for row in cleared["items"])
        assert cleared["total"] < len(cleared["items"]) + 2

        status = (await client.get("/api/ble/status")).json()
        assert status["state"] == "receiving"

        await client.post("/api/ble/stop")


async def test_settings_patch_normalises_usb_id_and_rejects_invalid():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.patch(
            "/api/settings", json={"ble": {"enabled": True, "usb_id": "19FF:0239"}}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ble"]["enabled"] is True
        assert body["ble"]["usb_id"] == "19ff:0239"

        resp2 = await client.patch("/api/settings", json={"ble": {"usb_id": "nonsense"}})
        assert resp2.status_code == 422


async def test_settings_patch_partial_merge_preserves_other_ble_fields():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"ble": {"usb_id": "19ff:0239"}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ble"]["usb_id"] == "19ff:0239"
        assert body["ble"]["enabled"] is False  # untouched by the partial patch

        resp2 = await client.patch("/api/settings", json={"ble": {"enabled": True}})
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["ble"]["enabled"] is True
        assert body2["ble"]["usb_id"] == "19ff:0239"  # preserved from the previous PATCH


async def test_settings_patch_accepts_sniffle_backend_fields():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.patch(
            "/api/settings",
            json={
                "ble": {
                    "backend": "sniffle",
                    "serial_port": "/dev/cu.usbserial-FAKE",
                    "baud": 921_600,
                    "phy": "coded",
                    "channel": "hop",
                }
            },
        )
        assert resp.status_code == 200
        body = resp.json()["ble"]
        assert body["backend"] == "sniffle"
        assert body["serial_port"] == "/dev/cu.usbserial-FAKE"
        assert body["baud"] == 921_600
        assert body["phy"] == "coded"
        assert body["channel"] == "hop"


async def test_settings_patch_rejects_invalid_sniffle_fields():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        assert (
            await client.patch("/api/settings", json={"ble": {"backend": "bluetooth"}})
        ).status_code == 422
        assert (
            await client.patch("/api/settings", json={"ble": {"phy": "3m"}})
        ).status_code == 422
        assert (
            await client.patch("/api/settings", json={"ble": {"channel": "40"}})
        ).status_code == 422
        assert (
            await client.patch("/api/settings", json={"ble": {"baud": 0}})
        ).status_code == 422
        assert (
            await client.patch("/api/settings", json={"ble": {"baud": -1}})
        ).status_code == 422


def test_ble_settings_defaults_to_hci_backend():
    settings = BleSettings()
    assert settings.backend == "hci"
    assert settings.serial_port is None
    assert settings.baud is None
    assert settings.phy == "1m"
    assert settings.channel == "37"


def test_settings_loads_without_ble_key():
    # Backward compatibility: a settings.json saved before this feature
    # landed has no "ble" key at all -- Settings must still load, filling in
    # BleSettings' own defaults.
    data = Settings(db_path=":memory:").model_dump()
    del data["ble"]
    settings = Settings.model_validate(data)
    assert settings.ble.enabled is False
    assert settings.ble.usb_id is None


async def test_ble_is_coalescable_broadcast_type():
    from msdr.api.service import _COALESCABLE_TYPES

    assert "ble" in _COALESCABLE_TYPES
