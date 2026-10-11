from msdr.wifi import radiotap
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import LINKTYPE_IEEE802_11_RADIOTAP, Record
from tests.wifi.fixtures import OUI_MSDR_RAW, append_bad_fcs, build_beacon_frame, build_radiotap


def _record(ts, rt_bytes, frame_bytes) -> Record:
    return Record(
        ts=ts, linktype=LINKTYPE_IEEE802_11_RADIOTAP, data=rt_bytes + frame_bytes, orig_len=0
    )


def test_bad_fcs_excluded_from_observations():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap(flags=radiotap.FLAG_FCS_AT_END | radiotap.FLAG_BAD_FCS)
    frame = append_bad_fcs(build_beacon_frame(bssid="aa:bb:cc:dd:ee:01"))
    rt = radiotap.parse(rt_bytes)
    table.ingest(_record(1.0, rt_bytes, frame), rt)
    rows, total = table.rows()
    # A bad-FCS frame alone never creates a network (its BSSID may be corrupt).
    assert total == 0
    assert table.bad_fcs_global == 1


def test_hidden_ssid_in_table():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"")
    table.ingest(_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))
    rows, _ = table.rows()
    assert rows[0]["ssid_hidden"] is True


def test_two_bssids_same_ssid_kept_separate():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    f1 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"SharedSSID", frame_timestamp=1)
    f2 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:02", ssid=b"SharedSSID", frame_timestamp=1)
    table.ingest(_record(1.0, rt_bytes, f1), radiotap.parse(rt_bytes))
    table.ingest(_record(2.0, rt_bytes, f2), radiotap.parse(rt_bytes))
    assert len(table) == 2
    rows, total = table.rows()
    assert total == 2
    assert {r["bssid"] for r in rows} == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}


def test_channel_mismatch_flagged():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ds_channel=11)
    table.ingest(_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))
    rows, _ = table.rows()
    assert rows[0]["channel"] == 11
    assert rows[0]["channel_source"] == "ds_ie"
    assert rows[0]["channel_mismatch"] is True


def test_channel_falls_back_to_configured():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ds_channel=None)
    table.ingest(_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))
    rows, _ = table.rows()
    assert rows[0]["channel"] == 6
    assert rows[0]["channel_source"] == "configured"
    assert rows[0]["channel_mismatch"] is False


def test_duplicate_suppression():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", frame_timestamp=999)
    rt = radiotap.parse(rt_bytes)
    table.ingest(_record(1.0, rt_bytes, frame), rt)
    table.ingest(_record(1.4, rt_bytes, frame), rt)  # same bytes, same TSFT, within 1s
    rows, _ = table.rows()
    assert rows[0]["beacons"] == 1
    assert rows[0]["duplicates"] == 1

    # More than 1s later and a different TSFT: a genuinely new beacon.
    frame2 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", frame_timestamp=1000)
    table.ingest(_record(3.0, rt_bytes, frame2), rt)
    rows, _ = table.rows()
    assert rows[0]["beacons"] == 2
    assert rows[0]["duplicates"] == 1


def test_caps_and_lru_eviction():
    table = NetworkTable(configured_channel=6, max_bssids=2)
    rt_bytes = build_radiotap()
    for i, ts in enumerate([1.0, 2.0, 3.0], start=1):
        frame = build_beacon_frame(bssid=f"aa:bb:cc:dd:ee:0{i}")
        table.ingest(_record(ts, rt_bytes, frame), radiotap.parse(rt_bytes))
    assert len(table) == 2
    assert table.evictions == 1
    rows, _ = table.rows()
    bssids = {r["bssid"] for r in rows}
    assert bssids == {"aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:03"}


def test_pagination_and_stable_order():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    # Insert out of first_seen order; rows() must still come back sorted.
    order = [("aa:bb:cc:dd:ee:03", 3.0), ("aa:bb:cc:dd:ee:01", 1.0), ("aa:bb:cc:dd:ee:02", 2.0)]
    for bssid, ts in order:
        frame = build_beacon_frame(bssid=bssid)
        table.ingest(_record(ts, rt_bytes, frame), radiotap.parse(rt_bytes))

    rows, total = table.rows(limit=2, offset=0)
    assert total == 3
    assert [r["bssid"] for r in rows] == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]

    rows2, total2 = table.rows(limit=2, offset=2)
    assert total2 == 3
    assert [r["bssid"] for r in rows2] == ["aa:bb:cc:dd:ee:03"]


def test_rows_filter_by_q_and_channel():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame_a = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"CafeWifi", ds_channel=6)
    frame_b = build_beacon_frame(bssid="aa:bb:cc:dd:ee:02", ssid=b"HomeNet", ds_channel=11)
    table.ingest(_record(1.0, rt_bytes, frame_a), radiotap.parse(rt_bytes))
    table.ingest(_record(2.0, rt_bytes, frame_b), radiotap.parse(rt_bytes))
    rows, total = table.rows(q="cafe")
    assert total == 1
    assert rows[0]["bssid"] == "aa:bb:cc:dd:ee:01"

    rows, total = table.rows(channel=11)
    assert total == 1
    assert rows[0]["bssid"] == "aa:bb:cc:dd:ee:02"


def test_signal_history_vendor_then_dbm():
    table = NetworkTable(configured_channel=6)
    rt_vendor = build_radiotap(vendor=(OUI_MSDR_RAW, 0, b"\x80"))
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01")
    table.ingest(_record(1.0, rt_vendor, frame), radiotap.parse(rt_vendor))
    details = table.details("aa:bb:cc:dd:ee:01")
    assert details["signal_history"][-1]["source"] == "vendor"
    assert details["signal_history"][-1]["raw_code"] == 0x80  # raw byte, not dBm

    rt_dbm = build_radiotap(antsignal=-55)
    frame2 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:02")
    table.ingest(_record(2.0, rt_dbm, frame2), radiotap.parse(rt_dbm))
    details2 = table.details("aa:bb:cc:dd:ee:02")
    assert details2["signal_history"][-1]["source"] == "dbm"
    assert details2["signal_history"][-1]["raw_code"] == -55


def test_details_and_frame_hex_on_demand():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"CafeWifi")
    table.ingest(_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))

    details = table.details("aa:bb:cc:dd:ee:01")
    assert details is not None
    assert details["ssid_history"] == ["CafeWifi"]
    assert details["recent_frame_count"] == 1

    raw = table.frame("aa:bb:cc:dd:ee:01", 0)
    assert raw == frame
    assert table.frame("aa:bb:cc:dd:ee:01", 1) is None
    assert table.frame("nonexistent", 0) is None
    assert table.details("nonexistent") is None


def test_snapshot_changed_since():
    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    import time

    t0 = time.time()
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01")
    table.ingest(_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))
    changed = table.snapshot_changed_since(t0)
    assert "aa:bb:cc:dd:ee:01" in changed
    assert table.snapshot_changed_since(time.time() + 10) == []


def test_bad_fcs_frame_never_creates_a_phantom_network():
    """A corrupted frame may carry a corrupted BSSID; it can only add to a network
    already confirmed by a good frame, and never moves that network's last_seen."""
    from msdr.wifi import pcap, radiotap
    from msdr.wifi.observations import NetworkTable
    from tests.wifi.fixtures import build_beacon_frame, build_radiotap

    table = NetworkTable(configured_channel=6)

    def feed(bssid, ts, bad):
        rt_bytes = build_radiotap(channel_mhz=2437, flags=0x40 if bad else None)
        frame = build_beacon_frame(bssid=bssid, ssid=b"A", ds_channel=6)
        rec = pcap.Record(ts=ts, linktype=127, data=rt_bytes + frame, orig_len=0)
        table.ingest(rec, radiotap.parse(rec.data))

    feed("aa:bb:cc:dd:ee:01", 100.0, bad=False)
    feed("aa:bb:cc:dd:ee:01", 105.0, bad=True)    # known network: counted, last_seen unchanged
    feed("de:ad:be:ef:00:01", 106.0, bad=True)    # unknown BSSID: no new row
    rows, total = table.rows()
    assert total == 1
    assert rows[0]["bad_fcs_seen"] == 1
    assert rows[0]["last_seen"] == 100.0
    assert table.bad_fcs_global == 1


def test_live_feed_records_every_frame_type_in_order():
    """The live feed keeps one summary per received frame (beacons, control,
    bad checksums), with increasing sequence numbers and a kind filter."""
    from msdr.wifi import pcap, radiotap
    from msdr.wifi.observations import NetworkTable
    from tests.wifi.fixtures import build_beacon_frame, build_radiotap

    table = NetworkTable(configured_channel=6)
    rt = build_radiotap(channel_mhz=2437)
    beacon = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"Cafe", ds_channel=6)
    ack = bytes([0xD4, 0x00, 0, 0]) + bytes.fromhex("aabbccddee02")  # control: ACK
    for i, frame in enumerate((beacon, ack, beacon)):
        rec = pcap.Record(ts=100.0 + i, linktype=127, data=rt + frame, orig_len=0)
        table.ingest(rec, radiotap.parse(rec.data))

    feed = table.live_since(0)
    assert [f["seq"] for f in feed["items"]] == [1, 2, 3]
    assert [f["name"] for f in feed["items"]] == ["beacon", "ACK", "beacon"]
    assert feed["items"][0]["ssid"] == "Cafe" and feed["items"][0]["channel"] == 6
    assert feed["items"][1]["kind"] == "control" and feed["items"][1]["ssid"] is None
    assert [f["seq"] for f in table.live_since(2)["items"]] == [3]
    assert [f["name"] for f in table.live_since(0, kind="control")["items"]] == ["ACK"]


def test_rows_sort_by_any_column_with_unknowns_last_and_stable_ties():
    from msdr.wifi import pcap, radiotap
    from msdr.wifi.observations import NetworkTable
    from tests.wifi.fixtures import build_beacon_frame, build_radiotap

    table = NetworkTable(configured_channel=6)
    rt = build_radiotap(channel_mhz=2437)
    spec = [("aa:00:00:00:00:03", b"Zeta", 1), ("aa:00:00:00:00:01", b"alpha", 3),
            ("aa:00:00:00:00:02", b"", 2)]  # hidden SSID
    t = 100.0
    for bssid, ssid, count in spec:
        for _ in range(count):
            t += 1
            rec = pcap.Record(ts=t, linktype=127,
                              data=rt + build_beacon_frame(bssid=bssid, ssid=ssid, ds_channel=6),
                              orig_len=0)
            table.ingest(rec, radiotap.parse(rec.data))

    def order(sort, direction):
        return [r["bssid"][-2:] for r in table.rows(sort=sort, order=direction)[0]]

    assert order("ssid", "asc") == ["01", "03", "02"]   # case-insensitive, hidden last
    assert order("ssid", "desc") == ["03", "01", "02"]  # hidden still last
    assert order("packets", "desc") == ["01", "02", "03"]
    assert order("bssid", "asc") == ["01", "02", "03"]
    assert order("channel", "asc") == ["01", "02", "03"]  # all equal -> BSSID tie-break


def test_clear_empties_networks_but_keeps_the_live_feed():
    from msdr.wifi import pcap, radiotap
    from msdr.wifi.observations import NetworkTable
    from tests.wifi.fixtures import build_beacon_frame, build_radiotap

    table = NetworkTable(configured_channel=6)
    rt = build_radiotap(channel_mhz=2437)
    frame = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"A")
    rec = pcap.Record(ts=1.0, linktype=127, data=rt + frame, orig_len=0)
    table.ingest(rec, radiotap.parse(rec.data))
    assert table.rows()[1] == 1
    table.clear()
    assert table.rows() == ([], 0)
    assert table.since() is None
    assert len(table.live_since(0)["items"]) == 1  # the live feed has its own Clear
    table.ingest(rec, radiotap.parse(rec.data))
    assert table.rows()[1] == 1  # new frames repopulate the list


def test_rows_sort_by_signal_uses_the_latest_sample():
    # Regression: the sort key indexed SignalSample like a tuple and raised
    # TypeError as soon as any network had a reading (broke the channel survey).
    from msdr.wifi.observations import NetworkTable, SignalSample

    t = NetworkTable(configured_channel=1)
    for bssid, code in (("aa:00:00:00:00:01", -70), ("aa:00:00:00:00:02", -40)):
        sample = SignalSample(ts=1.0, raw_code=code, source="dbm")
        t._touch(bssid, 1.0).signal_history.append(sample)
    t._touch("aa:00:00:00:00:03", 1.0)  # no reading: sorts last
    rows, _ = t.rows(sort="signal", order="desc")
    assert [r["bssid"][-1] for r in rows] == ["2", "1", "3"]


def test_summary_keeps_names_a_hidden_bssid_revealed():
    from msdr.wifi.observations import NetworkTable

    t = NetworkTable(configured_channel=1)
    entry = t._touch("aa:00:00:00:00:09", 1.0)
    entry.ssid_history.update({"Backhaul", "Alpha"})
    assert entry.summary()["revealed_ssids"] == ["Alpha", "Backhaul"]


def test_typical_signal_is_the_median_and_drives_the_sort():
    from msdr.wifi.observations import NetworkTable, SignalSample

    t = NetworkTable(configured_channel=6)
    steady = t._touch("aa:00:00:00:00:0a", 1.0)
    for code in (30, 31, 29, 30, 52):  # one odd frame last
        steady.signal_history.append(SignalSample(ts=1.0, raw_code=code, source="vendor"))
    other = t._touch("aa:00:00:00:00:0b", 1.0)
    for code in (40, 41, 40):
        other.signal_history.append(SignalSample(ts=1.0, raw_code=code, source="vendor"))
    assert steady.typical_signal() == 30
    assert steady.summary()["signal_raw_code"] == 52 and steady.summary()["signal_median"] == 30
    rows, _ = t.rows(sort="signal", order="desc")
    assert [r["bssid"][-1] for r in rows] == ["b", "a"]  # the odd 52 no longer wins


def test_network_summary_counts_recent_networks_by_band_security_maker_and_ap():
    from msdr.wifi.observations import NetworkTable

    t = NetworkTable(configured_channel=6)
    def net(bssid, ts, channel, security, hidden=False):
        e = t._touch(bssid, ts)
        e.channel, e.security_summary, e.ssid_hidden = channel, security, hidden
    net("a4:13:4e:8b:7d:d1", 100.0, 6, "WPA2-PSK")
    net("a4:13:4e:8b:7d:d5", 100.0, 6, "WPA2-PSK", hidden=True)  # same AP, last digit differs
    net("a6:13:4e:8b:7d:d1", 100.0, 6, "WPA2-Enterprise")  # same AP, LA bit set
    net("64:97:14:7c:70:c3", 100.0, 36, "WPA3-SAE")
    net("10:00:00:00:00:01", 100.0, 1, "Open")
    net("10:00:00:00:00:02", 10.0, 1, "Open")  # too old
    s = t.network_summary(now=110.0, window_s=60)
    assert s["total"] == 5 and s["access_points"] == 3
    assert s["hidden"] == 1 and s["local_admin"] == 1
    assert s["bands"] == [{"label": "2.4 GHz", "count": 4}, {"label": "5 GHz", "count": 1}]
    assert [(x["label"], x["count"]) for x in s["security"]] == [
        ("Open", 1), ("WPA2", 2), ("WPA3", 1), ("Enterprise", 1)
    ]
    # No vendor names downloaded: makers are only the tails.
    assert s["makers"] == [
        {"label": "Locally administered", "count": 1}, {"label": "Unknown", "count": 4}
    ]


def test_ssid_history_is_capped_to_most_recent_names():
    from msdr.wifi.observations import SSID_HISTORY_MAXLEN

    table = NetworkTable(configured_channel=6)
    rt_bytes = build_radiotap()
    for i in range(SSID_HISTORY_MAXLEN + 5):
        frame = build_beacon_frame(
            bssid="aa:bb:cc:dd:ee:01", ssid=f"net{i}".encode(), frame_timestamp=i
        )
        table.ingest(_record(float(i), rt_bytes, frame), radiotap.parse(rt_bytes))
    history = table._entries["aa:bb:cc:dd:ee:01"].ssid_history
    assert len(history) == SSID_HISTORY_MAXLEN
    assert "net0" not in history and f"net{SSID_HISTORY_MAXLEN + 4}" in history
