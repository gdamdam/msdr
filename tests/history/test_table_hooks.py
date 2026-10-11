"""Both observation tables feed the history accumulator only with trusted,
non-duplicate observations (docs/history.md "Device history")."""

from __future__ import annotations

from msdr.ble.observations import AdvertiserTable
from msdr.history.buckets import MinuteAccumulator, minute_of
from msdr.wifi import radiotap
from msdr.wifi.observations import NetworkTable
from tests.ble.fake_helper import REPORTS
from tests.wifi.fixtures import append_bad_fcs, build_beacon_frame, build_radiotap
from tests.wifi.test_observations import _record


def test_wifi_table_feeds_good_non_duplicate_beacons_only():
    acc = MinuteAccumulator()
    table = NetworkTable(configured_channel=6)
    table.history = acc
    rt_bytes = build_radiotap()
    rt = radiotap.parse(rt_bytes)
    beacon = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", frame_timestamp=1)
    table.ingest(_record(60.0, rt_bytes, beacon), rt)
    table.ingest(_record(60.4, rt_bytes, beacon), rt)  # duplicate: not counted
    hidden = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", frame_timestamp=2, ssid=b"")
    table.ingest(_record(62.0, rt_bytes, hidden), rt)
    bad_rt_bytes = build_radiotap(flags=radiotap.FLAG_FCS_AT_END | radiotap.FLAG_BAD_FCS)
    bad = append_bad_fcs(build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", frame_timestamp=3))
    table.ingest(_record(63.0, bad_rt_bytes, bad), radiotap.parse(bad_rt_bytes))
    drained = acc.drain()
    bucket = drained.buckets[("wifi", "aa:bb:cc:dd:ee:01", minute_of(60.0))]
    assert bucket.reports == 2
    meta = drained.devices[("wifi", "aa:bb:cc:dd:ee:01")]
    assert meta.name == "TestNet"  # a hidden-SSID beacon never erases the name
    assert meta.kind == "bssid"
    table.clear()
    table.ingest(_record(70.0, rt_bytes, build_beacon_frame(frame_timestamp=9)), rt)
    assert len(acc) == 1  # clearing the list does not stop history


def test_ble_table_feeds_every_report_with_identity():
    acc = MinuteAccumulator()
    table = AdvertiserTable()
    table.history = acc
    for i in range(len(REPORTS)):
        table.ingest(dict(REPORTS[i], ts=120.0 + i))
    table.ingest({"ts": "bad"})
    drained = acc.drain()
    kinds = {ident: m.kind for (_, ident), m in drained.devices.items()}
    assert kinds == {
        "4a:34:3f:50:52:88": "rpa",
        "f5:53:6b:f8:c2:0b": "static",
        "c0:11:22:33:44:55": "static",
        "00:11:22:33:44:66": "public",
    }
    assert drained.devices[("ble", "c0:11:22:33:44:55")].name == "TestBean"
    assert sum(b.reports for b in drained.buckets.values()) == len(REPORTS)


def test_accumulator_caps_pending_keys_and_counts_drops():
    acc = MinuteAccumulator(max_pending=2)
    acc.add("ble", "aa", 60.0, -50)
    acc.add("ble", "bb", 60.0, -51)
    acc.add("ble", "cc", 60.0, -52)  # third key: dropped, not stored
    acc.add("ble", "aa", 61.0, -53)  # existing key keeps updating
    assert len(acc) == 2
    assert acc.dropped == 1
    drained = acc.drain()
    assert set(k[1] for k in drained.buckets) == {"aa", "bb"}
    assert drained.buckets[("ble", "aa", 60)].reports == 2
    assert ("ble", "cc") not in drained.devices
    # Drain frees the cap; the counter is cumulative.
    acc.add("ble", "cc", 120.0, -52)
    assert len(acc) == 1 and acc.dropped == 1

