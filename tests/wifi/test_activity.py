"""ActivityAccumulator (msdr/wifi/activity.py) and its wiring into
WifiSupervisor/HistoryStore for the Wi-Fi activity heatmap (docs/wifi.md
"Wi-Fi activity")."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

import pytest

from msdr.history.store import HistoryStore
from msdr.wifi import supervisor as supervisor_module
from msdr.wifi.activity import ActivityAccumulator, ActivityCell
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import Record
from msdr.wifi.supervisor import WifiSupervisor

sys.path.insert(0, str(Path(__file__).parent))
import fixtures  # noqa: E402

FAKE_HELPER = Path(__file__).parent / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


# --- ActivityAccumulator (fake clock) ---------------------------------------


def test_add_packet_keys_by_second_and_channel():
    acc = ActivityAccumulator()
    acc.add_packet(1_800_000_000.4, 6, -70)
    acc.add_packet(1_800_000_000.9, 6, None)
    acc.add_packet(1_800_000_001.0, 36, -50)
    drained = acc.drain()
    assert drained.cells[(1_800_000_000, 6)].packets == 2
    assert drained.cells[(1_800_000_000, 6)].sig_sum == -70
    assert drained.cells[(1_800_000_000, 6)].sig_n == 1
    assert drained.cells[(1_800_000_001, 36)].packets == 1


def test_credit_listened_adds_to_the_named_second_only():
    acc = ActivityAccumulator()
    acc.credit_listened(1_800_000_010.0, 6, 1.0)
    acc.credit_listened(1_800_000_010.0, 6, 0.5)
    drained = acc.drain()
    assert len(drained.cells) == 1
    assert drained.cells[(1_800_000_010, 6)].listened_s == pytest.approx(1.5)
    assert drained.cells[(1_800_000_010, 6)].packets == 0


def test_credit_listened_ignores_non_positive_elapsed():
    acc = ActivityAccumulator()
    acc.credit_listened(1_800_000_010.0, 6, 0.0)
    acc.credit_listened(1_800_000_010.0, 6, -1.0)
    assert len(acc.drain().cells) == 0


def test_drain_resets_and_never_interpolates_unobserved_seconds():
    acc = ActivityAccumulator()
    acc.add_packet(1000.0, 6, None)
    acc.add_packet(1005.0, 6, None)  # a gap of 4 unobserved seconds in between
    drained = acc.drain()
    assert set(drained.cells) == {(1000, 6), (1005, 6)}
    assert len(acc.drain().cells) == 0  # drained accumulator starts empty again


# --- WifiSupervisor wiring ---------------------------------------------------


async def test_aggregate_attributes_packet_to_tuned_channel_not_ds_channel():
    """A beacon whose own DS Parameter Set channel (36) differs from the
    channel the receiver is actually tuned to (6) must still be counted
    against channel 6 -- docs/wifi.md: "Attribute every packet to the
    channel the receiver was tuned to ... never a beacon's DS channel.\""""
    sup = WifiSupervisor()
    sup.channel = 6
    sup.table = NetworkTable(configured_channel=6)
    sup.activity = ActivityAccumulator()
    task = asyncio.create_task(sup._aggregate())
    try:
        radiotap = fixtures.build_radiotap(channel_mhz=5180, antsignal=-40)
        frame = fixtures.append_fcs(
            fixtures.build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ds_channel=36)
        )
        ts = 1_800_000_000.0
        data = radiotap + frame
        sup._enqueue(Record(ts=ts, linktype=127, data=data, orig_len=len(data)))
        await _wait_for(lambda: sup.frames > 0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    drained = sup.activity.drain()
    assert set(drained.cells) == {(int(ts), 6)}
    assert drained.cells[(int(ts), 6)].packets == 1


async def test_aggregate_counts_every_record_not_only_beacons():
    """docs/wifi.md "Wi-Fi activity": colour is packets received, not just
    the beacons/probe-responses NetworkTable aggregates -- a frame the
    dot11 parser doesn't decode (e.g. a bad-version radiotap header) still
    counts as a received packet."""
    sup = WifiSupervisor()
    sup.channel = 11
    sup.table = NetworkTable(configured_channel=11)
    sup.activity = ActivityAccumulator()
    task = asyncio.create_task(sup._aggregate())
    try:
        bad_radiotap = b"\x99\x00\x08\x00\x00\x00\x00\x00"  # bad version byte
        ts = 1_800_000_500.0
        sup._enqueue(
            Record(ts=ts, linktype=127, data=bad_radiotap, orig_len=len(bad_radiotap))
        )
        await _wait_for(lambda: sup.frames > 0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    drained = sup.activity.drain()
    assert drained.cells[(int(ts), 11)].packets == 1
    assert drained.cells[(int(ts), 11)].sig_n == 0  # unparseable: no signal


def test_tick_credits_listened_time_only_while_receiving(monkeypatch):
    sup = WifiSupervisor()
    sup.activity = ActivityAccumulator()
    sup.channel = 6
    clock = [1_800_001_000.0]
    monkeypatch.setattr(supervisor_module.time, "time", lambda: clock[0])

    monkeypatch.setattr(sup, "health_state", lambda: "receiving")
    sup._credit_activity_tick()  # first tick: sets the reference, credits nothing
    assert len(sup.activity._pending.cells) == 0
    clock[0] += 1.0
    sup._credit_activity_tick()  # second tick: 1.0 s elapsed, credited
    drained = sup.activity.drain()
    assert drained.cells[(int(clock[0]), 6)].listened_s == pytest.approx(1.0)

    # Not listening (e.g. "starting", "stopped", "failed") must never credit,
    # and must reset the reference so the next receiving tick doesn't
    # retroactively credit the gap.
    monkeypatch.setattr(sup, "health_state", lambda: "starting")
    clock[0] += 5.0
    sup._credit_activity_tick()
    assert len(sup.activity._pending.cells) == 0
    monkeypatch.setattr(sup, "health_state", lambda: "receiving")
    clock[0] += 1.0
    sup._credit_activity_tick()  # reference was None: no credit for this tick either
    assert len(sup.activity._pending.cells) == 0


def test_tick_caps_credit_per_tick(monkeypatch):
    sup = WifiSupervisor()
    sup.activity = ActivityAccumulator()
    sup.channel = 6
    clock = [2_000_000_000.0]
    monkeypatch.setattr(supervisor_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(sup, "health_state", lambda: "receiving")
    sup._credit_activity_tick()
    clock[0] += 30.0  # a stalled loop must never invent this much coverage
    sup._credit_activity_tick()
    drained = sup.activity.drain()
    assert drained.cells[(int(clock[0]), 6)].listened_s == pytest.approx(2.0)


async def test_start_resets_activity_tick_reference_so_restart_is_not_credited():
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    sup.activity = ActivityAccumulator()
    sup._activity_tick_ref = 123.0
    await sup.start(6)
    assert sup._activity_tick_ref is None
    await _wait_for(lambda: sup.frames > 0)
    await sup.stop()


# --- HistoryStore.activity() (normalisation, no interpolation, bucketing) ---


def test_store_activity_normalises_by_listened_seconds_with_no_interpolation():
    store = HistoryStore(":memory:")
    store.connect()
    try:
        cells = {
            (1000, 6): ActivityCell(packets=10, listened_s=2.0, sig_sum=-160.0, sig_n=2),
            (1001, 6): ActivityCell(packets=0, listened_s=1.0, sig_sum=0.0, sig_n=0),
            (1005, 36): ActivityCell(packets=4, listened_s=1.0, sig_sum=0.0, sig_n=0),
        }
        store.write_activity(cells)
        result = store.activity(start=999, end=1010, bucket=1)
        rows = {(r[0], r[1]): r for r in result["rows"]}
        assert (1000, 6) in rows and (1001, 6) in rows and (1005, 36) in rows
        # No interpolation: seconds between 1001 and 1005 on channel 6/36 were
        # never observed and must not appear at all.
        assert (1002, 6) not in rows and (1003, 36) not in rows
        assert result["channels"] == [6, 36]
        # 10 packets over 2 listened seconds -> 5/s, the heatmap's max_rate.
        assert result["max_rate"] == pytest.approx(5.0)
        _, _, packets, listened, sig_mean = rows[(1000, 6)]
        assert packets == 10 and listened == pytest.approx(2.0)
        assert sig_mean == pytest.approx(-80.0)
        # listened with zero packets is kept (listened-but-silent, not hatched).
        assert rows[(1001, 6)][2] == 0
    finally:
        store.close()


def test_store_activity_buckets_aggregate_seconds_additively():
    store = HistoryStore(":memory:")
    store.connect()
    try:
        cells = {
            (100, 6): ActivityCell(packets=3, listened_s=1.0, sig_sum=-30.0, sig_n=1),
            (101, 6): ActivityCell(packets=2, listened_s=1.0, sig_sum=-20.0, sig_n=1),
            (102, 6): ActivityCell(packets=1, listened_s=1.0, sig_sum=0.0, sig_n=0),
        }
        store.write_activity(cells)
        result = store.activity(start=100, end=105, bucket=5)
        assert result["bucket_s"] == 5
        assert len(result["rows"]) == 1
        t, channel, packets, listened, sig_mean = result["rows"][0]
        assert (t, channel) == (100, 6)
        assert packets == 6
        assert listened == pytest.approx(3.0)
        assert sig_mean == pytest.approx(-25.0)
    finally:
        store.close()


def test_write_activity_upserts_additively_across_flushes():
    store = HistoryStore(":memory:")
    store.connect()
    try:
        store.write_activity({(500, 6): ActivityCell(packets=3, listened_s=1.0)})
        store.write_activity({(500, 6): ActivityCell(packets=2, listened_s=0.5)})
        result = store.activity(start=499, end=502, bucket=1)
        t, channel, packets, listened, _ = result["rows"][0]
        assert packets == 5
        assert listened == pytest.approx(1.5)
    finally:
        store.close()
