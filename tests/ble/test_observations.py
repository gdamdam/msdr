"""AdvertiserTable: rows, sorting, eviction, live feed (docs/ble.md)."""

from __future__ import annotations

from msdr.ble.observations import LIVE_FEED_MAXLEN, PAYLOAD_MAXLEN, AdvertiserTable
from tests.ble.fake_helper import REPORTS


def _report(i: int, ts: float, **over) -> dict:
    return dict(REPORTS[i % len(REPORTS)], ts=ts, **over)


def test_ingest_builds_rows_and_details():
    t = AdvertiserTable()
    for i in range(8):
        t.ingest(_report(i, 100.0 + i))
    rows, total = t.rows()
    assert total == 4 and len(t) == 4
    by_addr = {r["addr"]: r for r in rows}
    apple = by_addr["4a:34:3f:50:52:88"]
    assert apple["identity"] == "rpa" and apple["company"] == "Apple" and apple["connectable"]
    assert apple["reports"] == 2 and apple["rssi"] == -66 and apple["evt_types"] == {"ADV_IND": 2}
    beacon = by_addr["c0:11:22:33:44:55"]
    assert (
        beacon["identity"] == "static"
        and beacon["name"] == "TestBean"
        and not beacon["connectable"]
    )
    assert beacon["company"] == "Nordic Semiconductor"
    rid = by_addr["00:11:22:33:44:66"]
    assert rid["identity"] == "public" and rid["remote_id"] is True
    d = t.details("00:11:22:33:44:66")
    assert d["service_data"] == {"0xfffa": "0d00" + "00" * 25}
    assert d["payloads"][0]["count"] == 2 and len(d["recent"]) == 2
    assert d["rssi_history"] == [[103.0, -61], [107.0, -61]]
    assert t.details("ff:ff:ff:ff:ff:ff") is None
    assert t.since() == 100.0


def test_rows_filter_sort_and_pagination():
    t = AdvertiserTable()
    for i in range(4):
        t.ingest(_report(i, 10.0 + i))
    assert [r["addr"] for (r,) in zip(t.rows(sort="rssi", order="desc")[0], strict=True)] == [
        "c0:11:22:33:44:55",
        "00:11:22:33:44:66",
        "4a:34:3f:50:52:88",
        "f5:53:6b:f8:c2:0b",
    ]
    names = [r["name"] for r in t.rows(sort="name")[0]]
    assert names[0] == "TestBean" and names[1:] == [None, None, None]  # None sorts last
    assert [r["addr"] for r in t.rows(q="apple")[0]] == ["4a:34:3f:50:52:88"]
    assert [r["addr"] for r in t.rows(q="0xfffa")[0]] == []  # service data is not a service uuid
    assert t.rows(q="testbean")[1] == 1
    page, total = t.rows(sort="addr", offset=1, limit=2)
    assert total == 4 and [r["addr"] for r in page] == ["4a:34:3f:50:52:88", "c0:11:22:33:44:55"]


def test_bad_items_are_counted_not_raised():
    t = AdvertiserTable()
    assert t.ingest({"ts": "x"}) is None
    assert t.ingest({"ts": 1, "addr": "aa", "data": "zz"}) is None
    assert t.parse_errors == 2 and len(t) == 0
    e = t.ingest(
        {
            "ts": 1.0,
            "addr": "AA:BB:CC:DD:EE:FF",
            "addr_type": "public",
            "evt": "ADV_IND",
            "rssi": None,
            "data": "0a09546573",
        }
    )
    assert (
        e is not None
        and e.addr == "aa:bb:cc:dd:ee:ff"
        and t.malformed == 1
        and e.latest_rssi is None
    )


def _mk(addr, addr_type, data, ts=1.0, rssi=-40, evt="ADV_IND"):
    return {"ts": ts, "addr": addr, "addr_type": addr_type, "evt": evt, "rssi": rssi, "data": data}


def test_sort_identity_and_services():
    t = AdvertiserTable()
    pub, static, rpa, nrpa = (
        "00:11:22:33:44:01",
        "c0:00:00:00:00:02",
        "40:00:00:00:00:03",
        "00:00:00:00:00:04",
    )
    t.ingest(_mk(pub, "public", ""))  # no service uuids
    t.ingest(_mk(static, "random", "03030d18"))  # one 16-bit uuid (0x180d)
    t.ingest(_mk(rpa, "random", "05030d180f18"))  # two 16-bit uuids
    t.ingest(_mk(nrpa, "random", ""))  # no service uuids

    assert [r["addr"] for r in t.rows(sort="identity")[0]] == [pub, static, rpa, nrpa]
    assert [r["addr"] for r in t.rows(sort="identity", order="desc")[0]] == [nrpa, rpa, static, pub]

    # services: count of service uuids, None (last) when zero.
    svc_asc = t.rows(sort="services")[0]
    assert [r["addr"] for r in svc_asc] == [static, rpa, nrpa, pub]
    assert svc_asc[0]["service_uuids"] == ["0x180d"] and len(svc_asc[1]["service_uuids"]) == 2
    assert [r["addr"] for r in t.rows(sort="services", order="desc")[0]] == [rpa, static, nrpa, pub]


def test_sort_device_uses_display_names_then_name_then_company():
    t = AdvertiserTable()
    aa, bb, cc = "aa:11:22:33:44:01", "bb:11:22:33:44:02", "cc:11:22:33:44:03"
    t.ingest(_mk(aa, "public", "05095a756c75"))  # AD_NAME_COMPLETE "Zulu"
    t.ingest(_mk(bb, "public", "03ff4c00"))  # manufacturer 0x004c -> company "Apple"
    t.ingest(_mk(cc, "public", ""))  # no name, no company -> None (sorts last)

    # No display_names: falls back to advertised name ("zulu") vs company
    # ("apple") -- "apple" sorts first.
    plain = t.rows(sort="device")[0]
    assert [r["addr"] for r in plain] == [bb, aa, cc]

    # A label name overrides the advertised name and can change the order.
    labeled = t.rows(sort="device", display_names={aa: "Aardvark"})[0]
    assert [r["addr"] for r in labeled] == [aa, bb, cc]


def test_eviction_payload_bound_live_feed_and_clear():
    t = AdvertiserTable(max_entries=3)
    for i in range(5):
        t.ingest(
            {
                "ts": float(i),
                "addr": f"00:00:00:00:00:{i:02x}",
                "addr_type": "public",
                "evt": "ADV_NONCONN_IND",
                "rssi": -50,
                "data": f"03ff4c00{i:02x}",
            }
        )
    assert len(t) == 3 and t.evictions == 2
    assert t.details("00:00:00:00:00:00") is None and t.details("00:00:00:00:00:04") is not None
    for i in range(PAYLOAD_MAXLEN + 3):
        t.ingest(
            {
                "ts": 10.0 + i,
                "addr": "00:00:00:00:00:04",
                "addr_type": "public",
                "evt": "ADV_NONCONN_IND",
                "rssi": -50,
                "data": f"02ff{i:02x}",
            }
        )
    assert len(t.details("00:00:00:00:00:04")["payloads"]) == PAYLOAD_MAXLEN
    live = t.live_since(after=0)
    assert live["seq"] == 5 + PAYLOAD_MAXLEN + 3 and live["maxlen"] == LIVE_FEED_MAXLEN
    assert live["items"][0]["seq"] == 1 and live["items"][-1]["seq"] == live["seq"]
    assert t.live_since(after=live["seq"] - 2)["items"][0]["seq"] == live["seq"] - 1
    assert t.live_since(after=0, limit=2)["items"][0]["seq"] == live["seq"] - 1
    assert len(t.snapshot_changed_since(10.0 + PAYLOAD_MAXLEN)) == 1
    t.clear()
    assert len(t) == 0 and t.evictions == 0 and len(t.live) > 0  # live feed survives clear


def test_ingest_decodes_sensor_beacons_into_summary_details_and_live():
    # Service-data AD structure wrapping msdr/ble/sensors.py's BTHome worked
    # example (0xD2FC 40 02C409 03BF13, docs/ble.md "Sensors").
    data = "0a16d2fc4002c40903bf13"
    t = AdvertiserTable()
    entry = t.ingest(
        {
            "ts": 1.0,
            "addr": "11:22:33:44:55:66",
            "addr_type": "random",
            "evt": "ADV_NONCONN_IND",
            "rssi": -70,
            "data": data,
        }
    )
    assert entry.sensor["protocol"] == "bthome"
    assert entry.sensor["readings"]["temperature"]["value"] == 25.0
    assert entry.sensor["readings"]["humidity"]["value"] == 50.55
    assert entry.sensor["encrypted"] is False
    assert entry.sensor["ts"] == 1.0

    rows, _ = t.rows()
    row = next(r for r in rows if r["addr"] == "11:22:33:44:55:66")
    assert row["sensor"]["protocol"] == "bthome"

    details = t.details("11:22:33:44:55:66")
    assert details["sensor"]["protocol"] == "bthome"
    assert len(details["sensor_history"]) == 1
    assert details["sensor_history"][0]["readings"]["temperature"]["value"] == 25.0

    live = t.live_since(after=0)
    assert live["items"][0]["sensor"]["protocol"] == "bthome"

    # A later, non-sensor report must not erase the last known sensor reading.
    t.ingest(
        {
            "ts": 2.0,
            "addr": "11:22:33:44:55:66",
            "addr_type": "random",
            "evt": "ADV_NONCONN_IND",
            "rssi": -70,
            "data": "020106",
        }
    )
    assert t.details("11:22:33:44:55:66")["sensor"]["protocol"] == "bthome"
    assert len(t.details("11:22:33:44:55:66")["sensor_history"]) == 1


def test_ingest_collects_apple_continuity_types_and_airpods_battery():
    def ingest(t, ts, apple_payload: bytes):
        # Manufacturer-data AD: length, 0xFF, company 0x004C little-endian, payload.
        body = bytes([0xFF, 0x4C, 0x00]) + apple_payload
        t.ingest({"ts": ts, "addr": "aa:bb:cc:dd:ee:01", "addr_type": "random",
                  "evt": "ADV_IND", "rssi": -50, "data": (bytes([len(body)]) + body).hex()})

    t = AdvertiserTable()
    ingest(t, 1.0, bytes([0x10, 1, 0x00]))  # Nearby Info
    pods = bytes([0x01, 0x14, 0x20, 0x20, 0x49, 0x2F, 0x00, 0x00])
    ingest(t, 2.0, bytes([0x07, len(pods)]) + pods)
    row = t.rows()[0][0]
    # Types accumulate across messages from the same address.
    assert [x["name"] for x in row["apple"]["types"]] == [
        "Nearby Info", "AirPods / proximity pairing"
    ]
    assert row["apple"]["audio"]["model"] == "AirPods Pro (2nd generation)"
    assert row["apple"]["audio"]["battery_left"] == 90 and row["apple"]["audio"]["ts"] == 2.0


def test_device_summary_counts_by_type_only_recent_and_folds_small_makers():
    def mfr(company: int, payload: bytes) -> str:
        body = bytes([0xFF]) + company.to_bytes(2, "little") + payload
        return (bytes([len(body)]) + body).hex()

    t = AdvertiserTable()
    def add(addr: str, ts: float, data: str) -> None:
        t.ingest({"ts": ts, "addr": addr, "addr_type": "random", "evt": "ADV_IND",
                  "rssi": -60, "data": data})

    pods = bytes([0x01, 0x14, 0x20, 0x20, 0x49, 0x2F, 0x00, 0x00])
    add("aa:00:00:00:00:01", 100.0, mfr(0x004C, bytes([0x10, 1, 0x00])))  # Nearby Info
    add("aa:00:00:00:00:02", 100.0, mfr(0x004C, bytes([0x0C, 1, 0x00, 0x10, 1, 0x00])))
    add("aa:00:00:00:00:03", 100.0, mfr(0x004C, bytes([0x07, len(pods)]) + pods))
    add("aa:00:00:00:00:04", 100.0, mfr(0x0006, b"\x01\x09"))  # Microsoft
    add("aa:00:00:00:00:05", 100.0, "020106")  # flags only: unknown maker
    add("aa:00:00:00:00:06", 10.0, mfr(0x004C, bytes([0x12, 1, 0x00])))  # too old
    summary = t.device_summary(now=110.0, window_s=60)
    assert summary["total"] == 5
    groups = {g["label"]: g for g in summary["groups"]}
    assert groups["Apple"]["count"] == 3
    assert {s["label"]: s["count"] for s in groups["Apple"]["subgroups"]} == {
        "iPhone / iPad / Mac nearby": 2, "AirPods / Beats": 1
    }
    assert groups["Microsoft"]["count"] == 1 and groups["Unknown"]["count"] == 1
    assert summary["groups"][-1]["label"] == "Unknown"  # unknown sorts last

    folded = t.device_summary(now=110.0, window_s=60, top_makers=0)
    assert "Microsoft" not in {g["label"] for g in folded["groups"]}
    assert {g["label"]: g["count"] for g in folded["groups"]}["Other makers"] == 1


def test_non_finite_rssi_is_a_bad_item_not_an_exception():
    t = AdvertiserTable()
    assert t.ingest(_report(0, 100.0, rssi=float("inf"))) is None
    assert t.parse_errors == 1


def test_per_address_uuid_and_manufacturer_data_are_capped_keeping_newest():
    from msdr.ble.observations import PER_ADDR_MAXLEN

    t = AdvertiserTable()
    addr = "c0:11:22:33:44:99"
    n = PER_ADDR_MAXLEN + 8
    for i in range(n):
        uuid = bytes([i]) * 16
        cid = (0x1000 + i).to_bytes(2, "little")
        data = bytes([17, 0x07]) + uuid + bytes([4, 0xFF]) + cid + b"\x01"
        item = {"ts": 100.0 + i, "addr": addr, "addr_type": "public", "evt": "ADV_IND"}
        t.ingest({**item, "rssi": -60, "data": data.hex()})
    entry = t._entries[addr]
    assert len(entry.service_uuids) == PER_ADDR_MAXLEN
    assert len(entry.manufacturer_data) == PER_ADDR_MAXLEN
    assert 0x1000 not in entry.manufacturer_data and 0x1000 + n - 1 in entry.manufacturer_data
    assert entry.service_uuids[-1] != entry.service_uuids[0]
    details = t.details(addr)
    assert len(details["manufacturer_data"]) == PER_ADDR_MAXLEN
    assert len(details["service_uuids"]) == PER_ADDR_MAXLEN
    assert entry.summary()["service_uuids"] == entry.service_uuids
