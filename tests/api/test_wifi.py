"""/api/wifi/*: lifecycle, networks, sessions, import, settings PATCH, and
independence from SDRconnect (docs/wifi.md). Uses tests/wifi/fake_helper.py
as a real subprocess via settings.wifi.helper_command -- never a real
adapter."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import msdr.api.app as app_module
from msdr.api import schemas as schemas_module
from msdr.settings import HistorySettings, Settings, WifiSettings
from msdr.wifi import pcap
from tests.api.test_app import running_app
from tests.wifi import fixtures as wifi_fixtures

FAKE_HELPER = Path(__file__).resolve().parent.parent / "wifi" / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]


def _wifi_settings(**overrides) -> Settings:
    return Settings(
        db_path=":memory:",
        wifi=WifiSettings(enabled=True, helper_command=HELPER_COMMAND, **overrides),
    )


def _wifi_settings_with_history(**overrides) -> Settings:
    # Wi-Fi activity is recorded into device history, off by default.
    settings = _wifi_settings(**overrides)
    settings.history = HistorySettings(enabled=True)
    return settings


def _fixture_pcap_bytes(n: int = 6) -> bytes:
    out = bytearray(pcap.write_header())
    for i in range(n):
        rt = wifi_fixtures.build_radiotap(channel_mhz=2437, flags=0x10)
        frame = wifi_fixtures.append_fcs(
            wifi_fixtures.build_beacon_frame(bssid=f"aa:bb:cc:dd:ee:{i:02x}", ds_channel=6)
        )
        data = rt + frame
        out += pcap.write_record(1_700_000_000.0 + i, data, len(data))
    return bytes(out)


async def test_status_and_devices_work_while_feature_disabled():
    # docs/wifi.md: the Setup card needs status/devices to work while
    # disabled (to show "stopped"/enabled: false in the first place); only
    # the routes that actually operate the receiver/recorder refuse.
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.get("/api/wifi/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "stopped"
        assert body["enabled"] is False
        assert body["device"] is None
        assert body["recording"] is None

        resp = await client.get("/api/wifi/devices")
        assert resp.status_code == 200

        for method, path in (
            ("post", "/api/wifi/start"),
            ("post", "/api/wifi/sessions/start"),
            ("post", "/api/wifi/sessions/stop"),
        ):
            call = getattr(client, method)
            needs_channel = path.endswith("start") and "sessions" not in path
            kwargs = {"json": {"channel": 6}} if needs_channel else {}
            resp = await call(path, **kwargs)
            assert resp.status_code == 409, path

        resp = await client.post(
            "/api/wifi/import",
            content=_fixture_pcap_bytes(1),
            headers={"content-type": "application/vnd.tcpdump.pcap"},
        )
        assert resp.status_code == 409

        # Stop is always allowed (idempotent cleanup), not gated.
        resp = await client.post("/api/wifi/stop")
        assert resp.status_code == 200


async def test_lifecycle_start_status_shape_and_stop():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        resp = await client.post("/api/wifi/start", json={"channel": 6})
        assert resp.status_code == 200

        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = (await client.get("/api/wifi/status")).json()
            if status["state"] == "receiving":
                break
            await asyncio.sleep(0.02)
        assert status is not None and status["state"] == "receiving"
        assert status["channel"] == 6
        assert status["frames"] > 0
        assert status["enabled"] is True
        assert "frame_rate" in status  # renamed from frame_rate_hz for the UI contract
        assert set(status["helper"].keys()) == {
            "path",
            "upstream_commit",
            "host_tx_dma_disabled",
            "mac_tx_enabled",
        }
        assert status["helper"]["host_tx_dma_disabled"] is True

        resp = await client.post("/api/wifi/stop")
        assert resp.status_code == 200
        assert resp.json()["state"] == "stopped"


async def test_invalid_channel_rejected_422():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        resp = await client.post("/api/wifi/start", json={"channel": 15})
        assert resp.status_code == 422


async def test_devices_endpoint():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        resp = await client.get("/api/wifi/devices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["devices"][0]["serial"] == "123456"


async def test_networks_pagination_details_frame_hex_and_404s():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        await client.post("/api/wifi/start", json={"channel": 6})

        deadline = time.monotonic() + 5
        body = None
        while time.monotonic() < deadline:
            body = (await client.get("/api/wifi/networks")).json()
            if body["total"] > 0:
                break
            await asyncio.sleep(0.02)
        assert body is not None and body["total"] > 0
        assert "configured_channel" in body and "since" in body
        bssid = body["items"][0]["bssid"]
        assert "security" in body["items"][0]  # not security_summary (UI contract)

        limited = (await client.get("/api/wifi/networks?limit=1")).json()
        assert len(limited["items"]) == 1

        over_cap = await client.get("/api/wifi/networks?limit=10000")
        assert over_cap.status_code == 422

        details = await client.get(f"/api/wifi/networks/{bssid}")
        assert details.status_code == 200
        d = details.json()
        assert d["bssid"] == bssid
        assert "security" in d and "ies" in d and "capabilities" in d
        assert "ht" in d and "vht" in d and "he" in d
        for ie in d["ies"]:
            assert set(ie.keys()) == {"id", "ext_id", "name", "length", "hex"}
            bytes.fromhex(ie["hex"])  # valid hex, never raises decoding non-UTF8 payloads
        assert all(isinstance(pair, list) and len(pair) == 2 for pair in d["signal_history"])
        for r in d["recent"]:
            assert set(r.keys()) == {"n", "ts", "subtype", "length", "bad_fcs"}

        missing = await client.get("/api/wifi/networks/00:00:00:00:00:00")
        assert missing.status_code == 404

        frame = await client.get(f"/api/wifi/networks/{bssid}/frames/0")
        assert frame.status_code == 200
        assert "hex" in frame.json()
        bytes.fromhex(frame.json()["hex"])  # valid hex

        missing_frame = await client.get(f"/api/wifi/networks/{bssid}/frames/999")
        assert missing_frame.status_code == 404

        await client.post("/api/wifi/stop")


async def test_details_survives_non_utf8_ie_bytes():
    """The details endpoint used to raise UnicodeDecodeError when an IE (or
    the SSID) carried non-UTF-8 bytes -- raw bytes reaching FastAPI's JSON
    encoder. IEs are now hex-encoded at the API layer (msdr/api/services/
    wifi.py's _ie_dict), so a frame with a deliberately non-UTF-8 vendor IE
    must come back as ordinary JSON, never a 500."""
    from msdr.wifi import pcap as pcap_mod

    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        rt = wifi_fixtures.build_radiotap(channel_mhz=2437, flags=0x10)
        frame = wifi_fixtures.append_fcs(
            wifi_fixtures.build_beacon_frame(
                bssid="aa:bb:cc:dd:ee:ff",
                ssid=b"\xff\xfe\x00bad",  # not valid UTF-8
                extra_ies=[(221, b"\xff\xfe\x00\x01")],  # vendor IE, also not valid UTF-8
            )
        )
        data = rt + frame
        pcap_bytes = pcap_mod.write_header()
        pcap_bytes += pcap_mod.write_record(1_700_000_000.0, data, len(data))

        resp = await client.post(
            "/api/wifi/import",
            content=pcap_bytes,
            headers={"content-type": "application/vnd.tcpdump.pcap"},
        )
        assert resp.status_code == 200
        session_id = resp.json()["id"]

        details = await client.get(
            f"/api/wifi/networks/aa:bb:cc:dd:ee:ff?source=session:{session_id}"
        )
        assert details.status_code == 200
        body = details.json()
        assert body["bssid"] == "aa:bb:cc:dd:ee:ff"
        vendor_ies = [ie for ie in body["ies"] if ie["id"] == 221]
        assert vendor_ies
        bytes.fromhex(vendor_ies[0]["hex"])  # round-trips, never raised decoding it


async def test_wifi_helper_settings_changed_while_running_apply_on_next_start(tmp_path):
    first_helper = str(tmp_path / "first-helper")
    second_helper = str(tmp_path / "second-helper")
    settings = _wifi_settings(helper_path=first_helper)
    async with running_app(settings=settings) as (app, client, adapter):
        assert (await client.post("/api/wifi/start", json={"channel": 6})).status_code == 200
        current = app.state.service.wifi
        assert current.helper_path == first_helper

        patched = await client.patch("/api/settings", json={"wifi": {"helper_path": second_helper}})
        assert patched.status_code == 200
        assert app.state.service.wifi is current
        assert app.state.service._wifi_settings_pending is True

        await client.post("/api/wifi/stop")
        assert (await client.post("/api/wifi/start", json={"channel": 6})).status_code == 200
        assert app.state.service.wifi is not current
        assert app.state.service.wifi.helper_path == second_helper
        await client.post("/api/wifi/stop")


async def test_session_lifecycle_requires_receiving_and_export_round_trips():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        # Can't record before receiving.
        resp = await client.post("/api/wifi/sessions/start")
        assert resp.status_code == 409

        await client.post("/api/wifi/start", json={"channel": 6})

        async def frames_flowing() -> bool:
            return (await client.get("/api/wifi/status")).json()["frames"] > 5

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not await frames_flowing():
            await asyncio.sleep(0.02)

        resp = await client.post("/api/wifi/sessions/start")
        assert resp.status_code == 200
        session = resp.json()
        assert session["imported"] is False
        assert session["receiver_id"] == "wifi:123456"

        # Double-start refused.
        assert (await client.post("/api/wifi/sessions/start")).status_code == 409

        await asyncio.sleep(0.3)  # let a few frames land in the session

        resp = await client.post("/api/wifi/sessions/stop")
        assert resp.status_code == 200
        stopped = resp.json()
        assert stopped["frames"] > 0

        # Stop-with-none-active refused.
        assert (await client.post("/api/wifi/sessions/stop")).status_code == 409

        listed = (await client.get("/api/wifi/sessions")).json()
        row = next(r for r in listed if r["id"] == session["id"])
        assert set(row.keys()) == {
            "id",
            "started_at",
            "stopped_at",
            "channel",
            "frames",
            "bytes",
            "drops",
            "imported",
            "health",
        }
        assert row["stopped_at"] is not None
        assert isinstance(row["health"], str)

        export = await client.get(f"/api/wifi/sessions/{session['id']}/export.pcap")
        assert export.status_code == 200
        assert export.headers["content-type"] == "application/vnd.tcpdump.pcap"
        reader_records = len(_parse_pcap_bytes(export.content))
        assert reader_records == stopped["frames"]

        missing_export = await client.get("/api/wifi/sessions/does-not-exist/export.pcap")
        assert missing_export.status_code == 404

        await client.post("/api/wifi/stop")


def _parse_pcap_bytes(data: bytes):
    from msdr.wifi.pcap import PcapStreamReader

    return PcapStreamReader().feed(data)


async def test_import_rejects_non_pcap_and_accepts_valid_pcap():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        bad = await client.post(
            "/api/wifi/import",
            content=b"not a pcap file at all",
            headers={"content-type": "application/octet-stream"},
        )
        assert bad.status_code == 400

        good = await client.post(
            "/api/wifi/import",
            content=_fixture_pcap_bytes(6),
            headers={"content-type": "application/vnd.tcpdump.pcap"},
        )
        assert good.status_code == 200
        meta = good.json()
        assert meta["imported"] is True
        assert meta["frames"] == 6

        rows = (
            await client.get(f"/api/wifi/networks?source=session:{meta['id']}")
        ).json()
        assert rows["total"] == 6

        unknown_source = await client.get("/api/wifi/networks?source=session:nope")
        assert unknown_source.status_code == 404

        bad_source = await client.get("/api/wifi/networks?source=bogus")
        assert bad_source.status_code == 400


async def test_import_size_cap(monkeypatch):
    monkeypatch.setattr(app_module, "WIFI_IMPORT_MAX_BYTES", 32)
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        resp = await client.post(
            "/api/wifi/import",
            content=_fixture_pcap_bytes(6),
            headers={"content-type": "application/vnd.tcpdump.pcap"},
        )
        assert resp.status_code == 413


async def test_import_mid_stream_failure_removes_temp_file(monkeypatch):
    import tempfile

    created: list[Path] = []
    real_ntf = tempfile.NamedTemporaryFile

    def tracking_ntf(*args, **kwargs):
        tmp = real_ntf(*args, **kwargs)
        created.append(Path(tmp.name))
        return tmp

    monkeypatch.setattr(app_module.tempfile, "NamedTemporaryFile", tracking_ntf)

    async def body():
        yield _fixture_pcap_bytes(6)[:40]
        raise ConnectionError("client went away")

    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        try:
            await client.post(
                "/api/wifi/import",
                content=body(),
                headers={"content-type": "application/vnd.tcpdump.pcap"},
            )
        except Exception:
            pass
    assert created, "the upload must have reached the temp-file stage"
    assert not any(p.exists() for p in created), "partial upload left its temp file behind"


async def test_settings_patch_partial_merge_preserves_other_wifi_fields():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"wifi": {"channel": 11}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["wifi"]["channel"] == 11
        assert body["wifi"]["enabled"] is False  # untouched by the partial patch

        resp2 = await client.patch("/api/settings", json={"wifi": {"enabled": True}})
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["wifi"]["enabled"] is True
        assert body2["wifi"]["channel"] == 11  # preserved from the previous PATCH


async def test_settings_patch_rejects_invalid_channel():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.patch("/api/settings", json={"wifi": {"channel": 999}})
        assert resp.status_code == 422


def test_settings_loads_without_wifi_key():
    # Backward compatibility: a settings.json saved before this feature
    # landed has no "wifi" key at all -- Settings must still load, filling in
    # WifiSettings' own defaults.
    data = Settings(db_path=":memory:").model_dump()
    del data["wifi"]
    settings = Settings.model_validate(data)
    assert settings.wifi.enabled is False
    assert settings.wifi.channel == 6


async def test_independence_no_sdr_adapter_writes_during_wifi_lifecycle():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        service = app.state.service
        owner_before = dict(service.confirmed.props)
        baseline = len(adapter.calls)

        await client.post("/api/wifi/start", json={"channel": 6})
        await asyncio.sleep(0.2)
        await client.get("/api/wifi/status")
        await client.get("/api/wifi/networks")
        await client.post("/api/wifi/sessions/start")
        await asyncio.sleep(0.2)
        await client.post("/api/wifi/sessions/stop")
        await client.post("/api/wifi/stop")

        new_calls = adapter.calls[baseline:]
        assert not any(call[0] == "set" for call in new_calls)
        assert service.confirmed.props == owner_before


async def test_wifi_is_coalescable_broadcast_type():
    from msdr.api.service import _COALESCABLE_TYPES

    assert "wifi" in _COALESCABLE_TYPES


async def test_live_feed_and_sorted_networks_over_http():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        assert (await client.post("/api/wifi/start", json={"channel": 6})).status_code == 200
        deadline = time.monotonic() + 5
        feed = {"items": []}
        while time.monotonic() < deadline:
            feed = (await client.get("/api/wifi/live")).json()
            if feed["items"]:
                break
            await asyncio.sleep(0.02)
        assert feed["items"], "live feed stays empty while frames arrive"
        seqs = [f["seq"] for f in feed["items"]]
        assert seqs == sorted(seqs)
        newer = (await client.get("/api/wifi/live", params={"after": feed["seq"]})).json()
        assert all(f["seq"] > feed["seq"] for f in newer["items"])
        assert (await client.get("/api/wifi/live", params={"kind": "bogus"})).status_code == 422

        asc = (await client.get("/api/wifi/networks", params={"sort": "bssid"})).json()["items"]
        desc = (await client.get("/api/wifi/networks",
                                 params={"sort": "bssid", "order": "desc"})).json()["items"]
        assert [r["bssid"] for r in asc] == sorted(r["bssid"] for r in asc)
        # The fake capture keeps emitting frames, so desc may already hold a
        # network asc didn't: check desc's own order, not asc reversed.
        assert [r["bssid"] for r in desc] == sorted((r["bssid"] for r in desc), reverse=True)
        assert (await client.get("/api/wifi/networks", params={"sort": "nope"})).status_code == 422
        await client.post("/api/wifi/stop")


# --- Wi-Fi channel survey (docs/history.md "Wi-Fi channel survey") ----------
#
# dwell_s's real floor is 5 s; WIFI_SURVEY_MIN_DWELL_S is read at validation
# time (not baked into a Field bound), so monkeypatching it lets these tests
# hop channels in well under a second instead of waiting out 5 s per hop.


async def test_survey_requires_feature_enabled_and_no_recording_session():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        resp = await client.post(
            "/api/wifi/survey/start", json={"channels": [1, 6], "dwell_s": 10, "rounds": 1}
        )
        assert resp.status_code == 409

    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        await client.post("/api/wifi/start", json={"channel": 6})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (await client.get("/api/wifi/status")).json()["frames"] > 0:
                break
            await asyncio.sleep(0.02)
        assert (await client.post("/api/wifi/sessions/start")).status_code == 200
        resp = await client.post(
            "/api/wifi/survey/start", json={"channels": [1, 6], "dwell_s": 10, "rounds": 1}
        )
        assert resp.status_code == 409
        await client.post("/api/wifi/sessions/stop")
        await client.post("/api/wifi/stop")


async def test_survey_status_idle_shape():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        body = (await client.get("/api/wifi/survey")).json()
        assert body["state"] == "idle"
        assert body["results"] == []


async def test_survey_invalid_channels_and_dwell_rejected():
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        bad_channel = await client.post(
            "/api/wifi/survey/start", json={"channels": [15], "dwell_s": 10, "rounds": 1}
        )
        assert bad_channel.status_code == 422

        empty = await client.post(
            "/api/wifi/survey/start", json={"channels": [], "dwell_s": 10, "rounds": 1}
        )
        assert empty.status_code == 422

        too_short = await client.post(
            "/api/wifi/survey/start", json={"channels": [6], "dwell_s": 1, "rounds": 1}
        )
        assert too_short.status_code == 422


async def test_survey_hops_channels_and_restores_prior_capture(monkeypatch):
    monkeypatch.setattr(schemas_module, "WIFI_SURVEY_MIN_DWELL_S", 0.01)
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        await client.post("/api/wifi/start", json={"channel": 6})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (await client.get("/api/wifi/status")).json()["frames"] > 0:
                break
            await asyncio.sleep(0.02)

        resp = await client.post(
            "/api/wifi/survey/start",
            json={"channels": [1, 11], "dwell_s": 0.1, "rounds": 1},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "running"
        assert body["channels"] == [1, 11]

        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = (await client.get("/api/wifi/survey")).json()
            if status["state"] == "done":
                break
            await asyncio.sleep(0.02)
        assert status is not None and status["state"] == "done"
        assert {r["channel"] for r in status["results"]} == {1, 11}
        assert all(r["visits"] == 1 for r in status["results"])

        # Capture resumes on the channel it had before the survey started.
        deadline = time.monotonic() + 5
        wifi_status = None
        while time.monotonic() < deadline:
            wifi_status = (await client.get("/api/wifi/status")).json()
            if wifi_status["channel"] == 6:
                break
            await asyncio.sleep(0.02)
        assert wifi_status is not None and wifi_status["channel"] == 6

        await client.post("/api/wifi/stop")


async def test_survey_stop_mid_run_restores_stopped_capture(monkeypatch):
    monkeypatch.setattr(schemas_module, "WIFI_SURVEY_MIN_DWELL_S", 0.01)
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        # No plain capture was running before the survey.
        resp = await client.post(
            "/api/wifi/survey/start",
            json={"channels": [1, 6, 11], "dwell_s": 2.0, "rounds": 1},
        )
        assert resp.status_code == 200

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (await client.get("/api/wifi/survey")).json()["current_channel"] is not None:
                break
            await asyncio.sleep(0.02)

        stop_resp = await client.post("/api/wifi/survey/stop")
        assert stop_resp.status_code == 200
        # Stop replies at once; cancelling the hop and restoring the prior
        # capture finish in the background (docs/history.md).
        assert stop_resp.json()["state"] in ("stopping", "stopped")
        deadline = time.monotonic() + 10
        while (await client.get("/api/wifi/survey")).json()["state"] != "stopped":
            assert time.monotonic() < deadline, "survey never reached stopped"
            await asyncio.sleep(0.02)

        status = (await client.get("/api/wifi/status")).json()
        assert status["state"] == "stopped"

        assert (await client.post("/api/wifi/survey/stop")).status_code == 409


async def test_activity_endpoint_reports_fixed_channel_reception():
    # docs/wifi.md "Wi-Fi activity": a fixed-channel receiver accumulates
    # packets/listened_s on its tuned channel, drained by the history flush
    # the same way the endpoint flushes before reading (history_timeline's
    # pattern).
    async with running_app(settings=_wifi_settings_with_history()) as (app, client, adapter):
        await client.post("/api/wifi/start", json={"channel": 6})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (await client.get("/api/wifi/status")).json()["frames"] > 0:
                break
            await asyncio.sleep(0.02)
        # The 1 Hz tick's first firing only sets its reference (no credit
        # yet, see WifiSupervisor._credit_activity_tick); the second firing
        # is what actually credits elapsed listened time, so wait past it.
        await asyncio.sleep(2.2)

        resp = await client.get("/api/wifi/activity", params={"minutes": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["channels"] == [6]
        assert body["bucket_s"] in (1, 5, 15, 60, 300)
        assert body["rows"], "expected at least one activity row for channel 6"
        for _t, channel, packets, listened_s, _sig_mean in body["rows"]:
            assert channel == 6
            assert listened_s > 0  # only listened cells are ever returned
            assert packets >= 0
        assert body["max_rate"] >= 0

        resp = await client.get("/api/wifi/activity", params={"bucket": 7})
        assert resp.status_code == 422

        await client.post("/api/wifi/stop")


async def test_activity_endpoint_attributes_survey_hops_to_each_channel(monkeypatch):
    monkeypatch.setattr(schemas_module, "WIFI_SURVEY_MIN_DWELL_S", 0.01)
    async with running_app(settings=_wifi_settings_with_history()) as (app, client, adapter):
        resp = await client.post(
            "/api/wifi/survey/start",
            # A dwell long enough for the 1 Hz tick to credit listened time
            # at least once per channel (see the fixed-channel test above).
            json={"channels": [1, 11], "dwell_s": 3.0, "rounds": 1},
        )
        assert resp.status_code == 200

        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = (await client.get("/api/wifi/survey")).json()
            if status["state"] == "done":
                break
            await asyncio.sleep(0.02)
        assert status is not None and status["state"] == "done"

        resp = await client.get("/api/wifi/activity", params={"minutes": 5})
        assert resp.status_code == 200
        body = resp.json()
        # Both surveyed channels were actually tuned to and must show up as
        # listened -- never interpolated, never collapsed into one channel.
        assert set(body["channels"]) >= {1, 11}


async def test_plain_wifi_start_stops_a_running_survey(monkeypatch):
    monkeypatch.setattr(schemas_module, "WIFI_SURVEY_MIN_DWELL_S", 0.01)
    async with running_app(settings=_wifi_settings()) as (app, client, adapter):
        await client.post(
            "/api/wifi/survey/start",
            json={"channels": [1, 6, 11], "dwell_s": 2.0, "rounds": 1},
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (await client.get("/api/wifi/survey")).json()["current_channel"] is not None:
                break
            await asyncio.sleep(0.02)

        resp = await client.post("/api/wifi/start", json={"channel": 6})
        assert resp.status_code == 200

        survey_status = (await client.get("/api/wifi/survey")).json()
        assert survey_status["state"] == "stopped"

        deadline = time.monotonic() + 5
        wifi_status = None
        while time.monotonic() < deadline:
            wifi_status = (await client.get("/api/wifi/status")).json()
            if wifi_status["channel"] == 6:
                break
            await asyncio.sleep(0.02)
        assert wifi_status is not None and wifi_status["channel"] == 6
        await client.post("/api/wifi/stop")


async def test_sensing_route_off_then_on_after_setting_source():
    async with running_app(settings=Settings(db_path=":memory:")) as (app, client, adapter):
        assert (await client.get("/api/wifi/sensing")).json() == {"enabled": False}
        resp = await client.patch("/api/settings",
                                  json={"wifi": {"sensing_source": "22:EB:62:9F:8F:85"}})
        assert resp.status_code == 200
        body = (await client.get("/api/wifi/sensing")).json()
        assert body["enabled"] is True
        assert body["source"] == "22:eb:62:9f:8f:85"
        assert body["breathing_status"] == "needs dense source"
        bad = await client.patch("/api/settings", json={"wifi": {"sensing_source": "nope"}})
        assert bad.status_code in (400, 422)
