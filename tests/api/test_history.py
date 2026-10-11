"""/api/history/* and /api/labels/* end to end (docs/history.md): the real
BLE supervisor runs tests/ble/fake_helper.py, the service feeds the
accumulator, and reads flush it into the in-memory history database."""

from __future__ import annotations

import asyncio
import time

from msdr.api.services import history as history_module
from msdr.settings import HistorySettings, Settings
from tests.api.test_app import running_app
from tests.api.test_ble import _ble_settings, _wait_for_advertisers

STATIC = "c0:11:22:33:44:55"
PUBLIC = "00:11:22:33:44:66"
RPA = "4a:34:3f:50:52:88"


async def test_history_records_ble_and_timeline_joins_labels(monkeypatch):
    monkeypatch.setattr(history_module, "SAMPLE_EVERY_S", 0.05)
    settings = _ble_settings()
    settings.history = HistorySettings(enabled=True)  # off by default
    async with running_app(settings=settings) as (app, client, adapter):
        assert (await client.post("/api/ble/start")).status_code == 200
        await _wait_for_advertisers(client)
        await asyncio.sleep(0.3)  # a few coverage samples while receiving

        # Labels: a static address can be labelled, a rotating one cannot.
        put = await client.put(
            f"/api/labels/ble/{STATIC.upper()}", json={"name": "Bean", "class": "mine"}
        )
        assert put.status_code == 200 and put.json()["ident"] == STATIC
        refused = await client.put(f"/api/labels/ble/{RPA}", json={"name": "x", "class": "mine"})
        assert refused.status_code == 422 and "rotates" in refused.json()["detail"]
        bad = await client.put(f"/api/labels/ble/{PUBLIC}", json={"name": "x", "class": "best"})
        assert bad.status_code == 422
        await client.put(f"/api/labels/ble/{PUBLIC}", json={"name": "Drone", "class": "ignore"})
        labels = (await client.get("/api/labels", params={"receiver": "ble"})).json()["items"]
        assert {lab["ident"] for lab in labels} == {STATIC, PUBLIC}

        tl = (await client.get("/api/history/timeline", params={"hours": 1})).json()
        by = {d["ident"]: d for d in tl["devices"]}
        assert STATIC in by and RPA not in by  # private hidden by default
        assert by[STATIC]["label"] == {"name": "Bean", "class": "mine"}
        assert by[STATIC]["kind"] == "static" and by[STATIC]["signal_kind"] == "dbm"
        assert by[STATIC]["cells"][0][1] >= 1 and by[STATIC]["cells"][0][2] == -50.0
        assert tl["coverage"]["ble"] and tl["coverage"]["ble"][0][1] > 0
        assert tl["coverage"]["wifi"] == []

        hidden = (
            await client.get("/api/history/timeline", params={"hours": 1, "hide": "ignore"})
        ).json()
        assert PUBLIC not in {d["ident"] for d in hidden["devices"]}
        searched = (await client.get("/api/history/timeline", params={"q": "bean"})).json()
        assert [d["ident"] for d in searched["devices"]] == [STATIC]
        private = (
            await client.get("/api/history/timeline", params={"include_private": "true"})
        ).json()
        assert RPA in {d["ident"] for d in private["devices"]}
        assert (await client.get("/api/history/timeline", params={"bucket": 7})).status_code == 422
        assert (
            await client.get("/api/history/timeline", params={"receivers": "sdr"})
        ).status_code == 422

        dev = (await client.get(f"/api/history/device/ble/{STATIC}")).json()
        assert dev["device"]["name"] == "TestBean" and dev["label"]["name"] == "Bean"
        assert (await client.get("/api/history/device/ble/ff:ff:ff:ff:ff:ff")).status_code == 404
        assert (await client.get("/api/history/device/ble/nope")).status_code == 422

        status = (await client.get("/api/history/status")).json()
        assert status["available"] and status["enabled"] and status["devices"] >= 4
        assert status["db_path"] == ":memory:"

        # Stop live ingestion before asserting the database stays empty; a
        # new advertisement may otherwise legitimately repopulate history.
        await client.post("/api/ble/stop")
        # Clearing history keeps labels; deleting a label twice is a 404.
        assert (await client.post("/api/history/clear")).status_code == 200
        assert (await client.get("/api/history/status")).json()["devices"] == 0
        assert len((await client.get("/api/labels")).json()["items"]) == 2
        assert (await client.delete(f"/api/labels/ble/{STATIC}")).status_code == 200
        assert (await client.delete(f"/api/labels/ble/{STATIC}")).status_code == 404


async def test_history_disabled_records_nothing():
    settings = _ble_settings()
    settings.history = HistorySettings(enabled=False)
    async with running_app(settings=settings) as (app, client, adapter):
        await client.post("/api/ble/start")
        await _wait_for_advertisers(client)
        tl = (await client.get("/api/history/timeline", params={"hours": 1})).json()
        assert tl["devices"] == [] and tl["device_total"] == 0
        await client.post("/api/ble/stop")


async def test_settings_patch_history_and_defaults():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        body = (await client.patch("/api/settings", json={"history": {"retention_days": 7}})).json()
        assert body["history"]["retention_days"] == 7 and body["history"]["enabled"] is False
        bad = await client.patch("/api/settings", json={"history": {"retention_days": 0}})
        assert bad.status_code == 422
        assert (await client.get("/api/history/timeline")).json()["devices"] == []


async def test_history_compare_endpoint():
    """/api/history/compare (docs/history.md "Capture comparison"): writes
    directly to the service's history store (no real receiver needed) and
    checks the endpoint's shape and validation."""
    from msdr.history.buckets import MinuteAccumulator

    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        service = app.state.service
        store = service.history_store
        t0 = 1_790_000_000.0
        acc = MinuteAccumulator()
        acc.add("ble", "aa:bb:cc:dd:ee:ff", t0, -70, kind="static", signal_kind="dbm")
        store.write(acc.drain())
        store.add_coverage("ble", t0 + 60, 0, 60)
        acc2 = MinuteAccumulator()
        acc2.add("ble", "aa:bb:cc:dd:ee:ff", t0 + 1200, -60, kind="static", signal_kind="dbm")
        store.write(acc2.drain())
        store.add_coverage("ble", t0 + 1260, 0, 60)

        resp = await client.get(
            "/api/history/compare",
            params={
                "a_start": t0, "a_end": t0 + 60,
                "b_start": t0 + 1200, "b_end": t0 + 1260,
                "receivers": "ble",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["comparable"] == {"ble": True}
        assert [d["ident"] for d in body["changed"]] == ["aa:bb:cc:dd:ee:ff"]
        assert body["changed"][0]["signal_change"] == 10.0
        assert "excluded_private" not in body
        assert any("percentages" in n for n in body["notes"])

        invalid = await client.get(
            "/api/history/compare",
            params={"a_start": t0, "a_end": t0, "b_start": t0, "b_end": t0 + 60},
        )
        assert invalid.status_code == 422


def test_history_db_path_defaults_next_to_main_db(tmp_path):
    s = Settings(db_path=tmp_path / "msdr.db")
    assert history_module.history_db_path(s) == tmp_path / "history.db"
    assert history_module.history_db_path(Settings(db_path=":memory:")) == ":memory:"
    s.history = HistorySettings(db_path=tmp_path / "elsewhere.db")
    assert history_module.history_db_path(s) == tmp_path / "elsewhere.db"
    assert time.time() > 0
