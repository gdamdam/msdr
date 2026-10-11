"""WifiRecordingSession/WifiSessionStore: rotation, limits, retention,
export round-trip and import/replay (docs/wifi.md's `sessions.py` contract)."""

from __future__ import annotations

import json
import time

from msdr.wifi import pcap
from msdr.wifi.pcap import Record
from msdr.wifi.sessions import WifiSessionStore
from tests.wifi import fixtures


def _record(ts: float, *, bssid: str = "aa:bb:cc:dd:ee:01", payload_size: int = 0) -> Record:
    rt = fixtures.build_radiotap(channel_mhz=2437, flags=0x10)
    frame = fixtures.append_fcs(fixtures.build_beacon_frame(bssid=bssid, ds_channel=6))
    data = rt + frame
    if payload_size:
        data += b"\x00" * payload_size
    return Record(
        ts=ts, linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP, data=data, orig_len=len(data)
    )


def _write_fixture_pcap(path, n: int) -> list[Record]:
    records = [
        _record(1_700_000_000.0 + i * 0.01, bssid=f"aa:bb:cc:dd:ee:{i % 5:02x}") for i in range(n)
    ]
    with path.open("wb") as f:
        f.write(pcap.write_header())
        for r in records:
            f.write(pcap.write_record(r.ts, r.data, r.orig_len))
    return records


def _new_session(store: WifiSessionStore, **overrides):
    kwargs = dict(
        channel=6, receiver_id="wifi:x", helper_commit=None, max_bytes=10**9, max_seconds=3600
    )
    kwargs.update(overrides)
    return store.new_session(**kwargs)


def test_new_session_writes_header_and_rotates_parts(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    session = _new_session(
        store, receiver_id="wifi:123456", helper_commit="deadbeef", max_bytes=10 * 1024 * 1024
    )
    session.part_max_bytes = 200  # force rotation almost immediately
    for i in range(20):
        keep_going = session.write_record(_record(time.time(), bssid=f"aa:bb:cc:dd:ee:{i:02x}"))
        assert keep_going
    meta = session.finalize()
    assert meta["frames"] == 20
    assert meta["part_count"] >= 2
    parts = sorted(session.dir.glob("part-*.pcap"))
    assert len(parts) == meta["part_count"]
    for part in parts:
        assert part.read_bytes()[:4] in (b"\xa1\xb2\xc3\xd4", b"\xd4\xc3\xb2\xa1")
    assert (session.dir / "session.json").is_file()


def test_record_max_mb_and_minutes_stop_the_session(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    small = _new_session(store, max_bytes=200)
    kept = True
    count = 0
    while kept and count < 1000:
        kept = small.write_record(_record(time.time()))
        count += 1
    assert not kept
    assert small.total_bytes >= 200
    meta = small.finalize()
    assert meta["bytes"] >= 200

    short = _new_session(store, max_seconds=0.01)
    time.sleep(0.02)
    assert short.write_record(_record(time.time())) is False
    short.finalize()


def test_retention_prune_removes_old_sessions(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    old = _new_session(store)
    old.write_record(_record(time.time()))
    old.finalize()
    # Backdate it past the retention window.
    meta_path = old.dir / "session.json"
    meta = json.loads(meta_path.read_text())
    meta["started"] = meta["stopped"] = time.time() - 100 * 86400
    meta_path.write_text(json.dumps(meta))

    fresh = _new_session(store)
    fresh.write_record(_record(time.time()))
    fresh.finalize()

    removed = store.prune(retention_days=14)
    assert removed == 1
    ids = {m["id"] for m in store.list_sessions()}
    assert fresh.id in ids
    assert old.id not in ids


def test_list_sessions_sorted_newest_first(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    first = _new_session(store)
    first.write_record(_record(time.time()))
    first.finalize()
    time.sleep(0.01)
    second = _new_session(store)
    second.write_record(_record(time.time()))
    second.finalize()
    rows = store.list_sessions()
    assert [r["id"] for r in rows] == [second.id, first.id]


def test_export_round_trips_record_count_and_timestamps(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    session = _new_session(store)
    session.part_max_bytes = 150  # force a few rotations
    records = [_record(1_700_000_000.0 + i, bssid=f"aa:bb:cc:dd:ee:{i:02x}") for i in range(15)]
    for r in records:
        session.write_record(r)
    session.finalize()

    export_path = store.export(session.id)
    exported = pcap.read_file(export_path)
    assert len(exported) == len(records)
    assert [round(r.ts, 3) for r in exported] == [round(r.ts, 3) for r in records]
    # A single global header at the very start -- if the per-part headers had
    # leaked through unstripped, PcapStreamReader would desync and the
    # round-trip count/timestamps above would not match.
    assert export_path.read_bytes()[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4")


def test_export_unknown_session_raises_keyerror(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    try:
        store.export("does-not-exist")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_import_pcap_marks_imported_and_replays_into_its_own_table(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    source = tmp_path / "fixture.pcap"
    records = _write_fixture_pcap(source, 8)
    meta = store.import_pcap(source, configured_channel=6)
    assert meta["imported"] is True
    assert meta["frames"] == len(records)
    assert meta["channel"] == 6
    assert meta["receiver_id"] == "wifi:imported"

    table = store.replay_table(meta["id"], configured_channel=6)
    assert table is not None
    rows, total = table.rows()
    assert total == 5  # 5 distinct BSSIDs cycled across 8 records
    # The 3 repeats (same bssid/frame_timestamp within NetworkTable's 1 s
    # dedup window) count as duplicates, not fresh beacons.
    assert sum(row["beacons"] for row in rows) + sum(row["duplicates"] for row in rows) == len(
        records
    )

    # Cache hit: a second call returns the same object without re-reading.
    assert store.replay_table(meta["id"], configured_channel=6) is table


def test_replay_table_rebuilds_from_disk_when_cache_cold(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    session = _new_session(store)
    for i in range(4):
        session.write_record(_record(time.time(), bssid=f"aa:bb:cc:dd:ee:{i:02x}"))
    session.finalize()

    fresh_store = WifiSessionStore(tmp_path / "wifi")  # simulates a process restart: no cache
    table = fresh_store.replay_table(session.id, configured_channel=6)
    assert table is not None
    _, total = table.rows()
    assert total == 4


def test_session_dir_traversal_is_rejected(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    assert store.get("../escape") is None
    assert store.replay_table("../escape", configured_channel=6) is None


def test_prune_removes_crashed_session_without_session_json_by_mtime(tmp_path):
    import os

    store = WifiSessionStore(tmp_path)
    d = tmp_path / "20200101T000000-crashed"
    d.mkdir()
    part = d / "capture.pcap.part"
    part.write_bytes(b"x")
    old = time.time() - 10 * 86400
    for p in (part, d):
        os.utime(p, (old, old))
    assert store.prune(retention_days=1) == 1
    assert not d.exists()
