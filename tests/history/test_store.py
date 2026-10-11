"""MinuteAccumulator + HistoryStore + LabelsRepo (docs/history.md)."""

from __future__ import annotations

import pytest

from msdr.history.buckets import MinuteAccumulator, minute_of
from msdr.history.labels import LabelError, LabelsRepo, check_labelable, normalize_ident
from msdr.history.store import HistoryStore, choose_bucket
from msdr.storage.db import Database
from msdr.wifi.activity import ActivityCell

T0 = 1_790_000_040.0  # a minute boundary + 0 s


@pytest.fixture
def store():
    s = HistoryStore(":memory:")
    s.connect()
    yield s
    s.close()


def _fill(acc: MinuteAccumulator) -> None:
    for i in range(3):
        acc.add(
            "ble",
            "c0:11:22:33:44:55",
            T0 + i,
            -50 - i,
            name="Bean",
            kind="static",
            signal_kind="dbm",
        )
    acc.add("ble", "c0:11:22:33:44:55", T0 + 61, None, name=None, kind="static")
    acc.add("wifi", "aa:bb:cc:dd:ee:01", T0 + 5, 120, name="home", kind="bssid", signal_kind="raw")
    acc.add("ble", "4a:34:3f:50:52:88", T0 + 7, -70, kind="rpa", signal_kind="dbm")


def test_accumulator_buckets_and_meta():
    acc = MinuteAccumulator()
    _fill(acc)
    d = acc.drain()
    b = d.buckets[("ble", "c0:11:22:33:44:55", minute_of(T0))]
    assert (b.reports, b.sig_min, b.sig_max, b.sig_sum, b.sig_n) == (3, -52, -50, -153, 3)
    later = d.buckets[("ble", "c0:11:22:33:44:55", minute_of(T0 + 61))]
    assert later.reports == 1 and later.sig_n == 0 and later.sig_min is None
    meta = d.devices[("ble", "c0:11:22:33:44:55")]
    assert meta.name == "Bean" and meta.first_seen == T0 and meta.last_seen == T0 + 61
    assert len(acc) == 0 and not acc.drain()


def test_additive_flushes_and_timeline(store):
    acc = MinuteAccumulator()
    _fill(acc)
    store.write(acc.drain())
    # Same minute again, flushed separately: sums, never overwrites.
    acc.add("ble", "c0:11:22:33:44:55", T0 + 10, -40, name=None, kind="static", signal_kind="dbm")
    store.write(acc.drain())
    store.add_coverage("ble", T0 + 50, 0, 50)
    store.add_coverage("wifi", T0 + 70, 6, 20)  # spans a minute boundary: 10 s + 10 s

    tl = store.timeline(start=T0 - 60, end=T0 + 180, bucket=60)
    assert tl["bucket_s"] == 60 and tl["device_total"] == 2 and not tl["truncated"]
    by = {d["ident"]: d for d in tl["devices"]}
    assert "4a:34:3f:50:52:88" not in by  # private address hidden by default
    bean = by["c0:11:22:33:44:55"]
    assert bean["name"] == "Bean" and bean["kind"] == "static" and bean["reports"] == 5
    assert bean["cells"][0] == [minute_of(T0), 4, -48.2, -52, -40]
    assert bean["cells"][1] == [minute_of(T0 + 61), 1, None, None, None]
    assert tl["coverage"]["ble"] == [[minute_of(T0), 50.0]]
    assert tl["coverage"]["wifi"] == [[minute_of(T0), 10.0, [6]], [minute_of(T0 + 60), 10.0, [6]]]

    with_private = store.timeline(start=T0 - 60, end=T0 + 180, include_private=True)
    assert with_private["device_total"] == 3
    only_wifi = store.timeline(start=T0 - 60, end=T0 + 180, receivers=("wifi",))
    assert [d["ident"] for d in only_wifi["devices"]] == ["aa:bb:cc:dd:ee:01"]
    excluded = store.timeline(start=T0 - 60, end=T0 + 180, exclude={("wifi", "aa:bb:cc:dd:ee:01")})
    assert [d["receiver"] for d in excluded["devices"]] == ["ble"]
    limited = store.timeline(start=T0 - 60, end=T0 + 180, limit=1)
    assert limited["truncated"] and limited["devices"][0]["reports"] == 5
    assert store.search("home") == {("wifi", "aa:bb:cc:dd:ee:01")}

    dev = store.device("ble", "c0:11:22:33:44:55", start=T0 - 60, end=T0 + 180, bucket=300)
    assert dev["bucket_s"] == 300 and sum(c[1] for c in dev["cells"]) == 5
    assert store.device("ble", "ff:ff:ff:ff:ff:ff", start=T0, end=T0 + 60) is None

    st = store.status()
    assert st["devices"] == 3 and st["oldest"] == minute_of(T0)
    assert store.prune(1, now=T0 + 3 * 86400) > 0 and store.status()["buckets"] == 0
    assert store.status()["devices"] == 0


def test_choose_bucket_keeps_columns_bounded():
    assert choose_bucket(6 * 3600) == 60
    assert choose_bucket(24 * 3600) == 300
    assert choose_bucket(7 * 86400) == 3600
    assert choose_bucket(3600, 900) == 900
    assert choose_bucket(3600, 7) == 60


def test_wifi_activity_migration_applies_and_prunes_separately_from_buckets(store):
    # 002_wifi_activity.sql applied cleanly (connect() already ran migrations).
    assert store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wifi_activity'"
    ).fetchone() is not None

    store.write_activity(
        {(int(T0), 6): ActivityCell(packets=5, listened_s=1.0, sig_sum=-80, sig_n=1)}
    )
    old_second = int(T0 - 100 * 3600)  # well past any sane activity_retention_hours
    store.write_activity({(old_second, 6): ActivityCell(packets=1, listened_s=1.0)})
    assert store.conn.execute("SELECT COUNT(*) FROM wifi_activity").fetchone()[0] == 2

    # retention is independent of buckets/coverage's retention_days: a short
    # activity_retention_hours prunes the stale row without touching buckets.
    store.write(MinuteAccumulator().drain())  # no-op, just exercises both tables existing
    pruned = store.prune_activity(48, now=T0)
    assert pruned == 1
    remaining = store.conn.execute("SELECT second, channel FROM wifi_activity").fetchall()
    assert [(r["second"], r["channel"]) for r in remaining] == [(int(T0), 6)]

    store.clear()
    assert store.conn.execute("SELECT COUNT(*) FROM wifi_activity").fetchone()[0] == 0


def test_labels_repo_and_rules():
    db = Database(":memory:")
    db.connect()
    repo = LabelsRepo(db)
    assert normalize_ident("ble", " C0-11-22-33-44-55 ") == "c0:11:22:33:44:55"
    with pytest.raises(LabelError):
        normalize_ident("sdr", "c0:11:22:33:44:55")
    with pytest.raises(LabelError):
        normalize_ident("wifi", "nope")
    with pytest.raises(LabelError, match="rotates"):
        check_labelable("ble", "4a:34:3f:50:52:88", "rpa")
    with pytest.raises(LabelError, match="rotates"):
        check_labelable("ble", "4a:34:3f:50:52:88", None)  # unobserved: address bits decide
    check_labelable("ble", "4a:34:3f:50:52:88", "public")  # a public address is never private
    check_labelable("wifi", "4a:34:3f:50:52:88", None)

    lab = repo.put("ble", "c0:11:22:33:44:55", name=" Kitchen ", note="", cls="mine")
    assert lab["name"] == "Kitchen" and lab["class"] == "mine"
    again = repo.put("ble", "c0:11:22:33:44:55", name="Kitchen sensor", note="n", cls="known")
    assert again["created_at"] == lab["created_at"] and again["class"] == "known"
    repo.put("wifi", "aa:bb:cc:dd:ee:01", name="Router", note="", cls="ignore")
    assert repo.keys({"ignore"}) == {("wifi", "aa:bb:cc:dd:ee:01")}
    assert repo.search("sensor") == {("ble", "c0:11:22:33:44:55")}
    assert [x["receiver"] for x in repo.list("wifi")] == ["wifi"]
    for bad in (
        {"name": "", "cls": "mine"},
        {"name": "x" * 81, "cls": "mine"},
        {"name": "x", "cls": "best"},
    ):
        with pytest.raises(LabelError):
            repo.put("wifi", "aa:bb:cc:dd:ee:02", note="", **bad)
    assert repo.delete("wifi", "aa:bb:cc:dd:ee:01") and not repo.delete("wifi", "aa:bb:cc:dd:ee:01")
    db.close()
