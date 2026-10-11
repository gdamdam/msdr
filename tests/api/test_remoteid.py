"""/api/remoteid/*: live UAS list, details and the coalescable "remoteid" ws
message (docs/remote-id.md "Live"). The BLE path uses tests/ble/fake_helper.py
as a real subprocess (it already emits one legacy Remote ID advertisement,
docs/ble.md) so the table -> tracker -> API wiring is exercised end to end;
the rest seed `service.remoteid_tracker` directly, same as tests/remoteid/
test_tracker.py, to control freshness/staleness without sleeping."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from msdr.remoteid import messages
from msdr.settings import BleSettings, Settings
from tests.api.test_app import running_app

FAKE_BLE_HELPER = Path(__file__).resolve().parent.parent / "ble" / "fake_helper.py"
BLE_HELPER_COMMAND = [sys.executable, str(FAKE_BLE_HELPER)]


def _ble_settings(**overrides) -> Settings:
    return Settings(
        db_path=":memory:",
        ble=BleSettings(enabled=True, helper_command=BLE_HELPER_COMMAND, **overrides),
    )


def _basic_id(uas_id: bytes, *, uas_id_text: str | None = "SERIAL") -> messages.BasicId:
    return messages.BasicId(
        protocol_version=2, ua_type=2, id_type=1, uas_id_bytes=uas_id, uas_id_text=uas_id_text
    )


def _location(*, lat: float, lon: float) -> messages.Location:
    return messages.Location(
        protocol_version=2,
        status=2,
        height_type=0,
        direction_deg=90.0,
        speed_horizontal_ms=5.0,
        speed_vertical_ms=0.0,
        latitude_deg=lat,
        longitude_deg=lon,
        altitude_baro_m=100.0,
        altitude_geo_m=110.0,
        height_m=50.0,
        horiz_accuracy_enum=11,
        vert_accuracy_enum=6,
        baro_accuracy_enum=6,
        speed_accuracy_enum=3,
        timestamp_accuracy_enum=1,
        timestamp_s=10.0,
    )


def _pack(*msgs: messages.DecodedMessage) -> messages.MessagePack:
    return messages.MessagePack(protocol_version=2, message_count=len(msgs), messages=tuple(msgs))


async def test_live_empty_by_default():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.get("/api/remoteid/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert "now" in body


async def test_live_reflects_ble_remote_id_advertisement():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        await client.post("/api/ble/start")
        service = app.state.service

        deadline = time.monotonic() + 5
        items: list[dict] = []
        while time.monotonic() < deadline:
            items = (await client.get("/api/remoteid/live")).json()["items"]
            if items:
                break
            await asyncio.sleep(0.02)
        assert items, "BLE fake_helper's Remote ID advertisement never reached the tracker"

        group = items[0]
        track = group["tracks"][0]
        assert track["transport"] == "ble"
        assert track["address"] == "ble:00:11:22:33:44:66"
        assert "ble" in group["sources"]

        # The table's `.remoteid` sink is the service's one shared tracker.
        assert service.ble.table.remoteid is service.remoteid_tracker

        await client.post("/api/ble/stop")


async def test_uas_details_404_for_unknown_key():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.get("/api/remoteid/uas/does-not-exist")
        assert resp.status_code == 404


async def test_uas_details_includes_recent_messages_and_position_freshness():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        service = app.state.service
        uas_id = bytes([0x11]) + bytes(19)
        now = time.time()
        service.remoteid_tracker.ingest_wifi_pack(
            "wifi:aa:bb:cc:dd:ee:ff",
            1,
            _pack(_basic_id(uas_id), _location(lat=45.0, lon=-122.0)),
            received_at=now,
        )

        live = (await client.get("/api/remoteid/live")).json()
        assert len(live["items"]) == 1
        key = live["items"][0]["key"]

        details = await client.get(f"/api/remoteid/uas/{key}")
        assert details.status_code == 200
        body = details.json()
        assert body["uas_id_text"] == "SERIAL"
        assert body["drone_position"]["latitude_deg"] == 45.0
        assert body["drone_position"]["stale"] is False
        assert len(body["recent_messages"]) == 2
        kinds = {m["message_type"] for m in body["recent_messages"]}
        assert kinds == {messages.MESSAGETYPE_BASIC_ID, messages.MESSAGETYPE_LOCATION}


async def test_details_hex_serializes_binary_basic_id_and_authentication_fields():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        uas_id = bytes.fromhex("ff0080fe") + bytes(16)
        auth_data = bytes.fromhex("ff008000")
        auth = messages.Authentication(
            protocol_version=2, auth_type=1, data_page=0, last_page_index=0,
            length=len(auth_data), timestamp_s_since_2019=123, auth_data=auth_data,
        )
        app.state.service.remoteid_tracker.ingest_wifi_pack(
            "wifi:aa:bb:cc:dd:ee:ff", 1,
            _pack(_basic_id(uas_id, uas_id_text=None), auth), received_at=time.time(),
        )
        live = (await client.get("/api/remoteid/live")).json()
        key = live["items"][0]["key"]
        details = (await client.get(f"/api/remoteid/uas/{key}")).json()

        basic = next(m for m in details["recent_messages"] if m["message_type"] == 0)
        authentication = next(m for m in details["recent_messages"] if m["message_type"] == 2)
        assert basic["uas_id_bytes"] == uas_id.hex()
        assert authentication["auth_data"] == auth_data.hex()


async def test_stale_flag_true_after_max_age():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        service = app.state.service
        old = time.time() - 120.0  # well past the 60s staleness threshold
        service.remoteid_tracker.ingest_wifi_pack(
            "wifi:11:22:33:44:55:66", 1, _pack(_location(lat=1.0, lon=2.0)), received_at=old
        )

        body = (await client.get("/api/remoteid/live")).json()
        track = body["items"][0]["tracks"][0]
        assert track["drone_position"]["stale"] is True
        assert track["drone_position"]["age_s"] >= 120.0
        assert track["stale"] is True


async def test_conflict_flag_surfaces_on_live_and_details():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        service = app.state.service
        uas_id = bytes([0x33]) + bytes(19)
        now = time.time()
        service.remoteid_tracker.ingest_wifi_pack(
            "wifi:aa",
            1,
            _pack(_basic_id(uas_id), _location(lat=48.8566, lon=2.3522)),
            received_at=now,
        )
        service.remoteid_tracker.ingest_ble_message("ble:bb", 1, _basic_id(uas_id), received_at=now)
        service.remoteid_tracker.ingest_ble_message(
            "ble:bb", 2, _location(lat=51.5074, lon=-0.1278), received_at=now
        )

        body = (await client.get("/api/remoteid/live")).json()
        assert len(body["items"]) == 1
        assert body["items"][0]["conflict"] is True

        details = (await client.get(f"/api/remoteid/uas/{body['items'][0]['key']}")).json()
        assert details["conflict"] is True
        assert len(details["tracks"]) == 2


async def test_sync_remoteid_sinks_attaches_tracker_to_current_tables():
    async with running_app(settings=_ble_settings()) as (app, client, adapter):
        service = app.state.service
        await client.post("/api/ble/start")
        assert service.ble.table is not None
        service.ble.table.remoteid = None  # simulate a freshly (re)created table
        service._sync_remoteid_sinks()
        assert service.ble.table.remoteid is service.remoteid_tracker
        await client.post("/api/ble/stop")


async def test_publish_on_change_and_suppressed_when_unchanged():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        service = app.state.service
        q = service.broadcaster.subscribe()
        try:
            service.remoteid_tracker.ingest_wifi_pack(
                "wifi:aa", 1, _pack(_location(lat=1.0, lon=2.0)), received_at=time.time()
            )
            await service._maybe_publish_remoteid()
            msg = q.get_nowait()
            assert msg["type"] == "remoteid"
            assert len(msg["items"]) == 1

            # No change since the last push -- nothing new queued.
            await service._maybe_publish_remoteid()
            assert q.empty()
        finally:
            service.broadcaster.unsubscribe(q)


async def test_remoteid_is_coalescable_broadcast_type():
    from msdr.api.service import _COALESCABLE_TYPES

    assert "remoteid" in _COALESCABLE_TYPES
