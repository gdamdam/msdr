"""HistoryStore.compare (docs/history.md "Capture comparison"): appeared /
disappeared / changed devices across two listening windows, normalised by
each window's own coverage, rpa/nrpa exclusion, and the "not comparable"
coverage-mismatch flag."""

from __future__ import annotations

import pytest

from msdr.history.buckets import MinuteAccumulator
from msdr.history.store import HistoryStore

T0 = 1_790_000_000.0
A_START, A_END = T0, T0 + 600  # window A: 10 minutes
B_START, B_END = T0 + 1200, T0 + 1800  # window B: a later, separate 10 minutes


@pytest.fixture
def store():
    s = HistoryStore(":memory:")
    s.connect()
    yield s
    s.close()


def _acc_write(store, entries):
    acc = MinuteAccumulator()
    for receiver, ident, ts, signal, kind in entries:
        acc.add(receiver, ident, ts, signal, kind=kind, signal_kind="dbm")
    store.write(acc.drain())


def test_disappeared_appeared_and_changed_with_rate_normalisation(store):
    # Window A: full 10 minutes of BLE coverage on channel 0 (BLE has no
    # per-report channel). "gone" sends 60 reports (6/min); "steady" sends 30
    # (3/min) at -60 dBm.
    store.add_coverage("ble", A_END, 0, 600)
    _acc_write(
        store,
        [("ble", "aa:aa:aa:aa:aa:aa", A_START + i, -70, "static") for i in range(60)]
        + [("ble", "bb:bb:bb:bb:bb:bb", A_START + i * 10, -60, "public") for i in range(30)],
    )
    # Window B: also full 10 minutes of BLE coverage. "steady" now sends 30
    # reports at -50 dBm (10 dB stronger); "new" is a fresh device.
    store.add_coverage("ble", B_END, 0, 600)
    _acc_write(
        store,
        [("ble", "bb:bb:bb:bb:bb:bb", B_START + i * 10, -50, "public") for i in range(30)]
        + [("ble", "cc:cc:cc:cc:cc:cc", B_START + i * 20, -80, "static") for i in range(15)],
    )

    result = store.compare(
        a_start=A_START, a_end=A_END, b_start=B_START, b_end=B_END, receivers=("ble",)
    )
    assert result["comparable"] == {"ble": True}
    assert result["a"]["coverage"]["ble"]["listened_s"] == 600.0
    assert result["b"]["coverage"]["ble"]["listened_s"] == 600.0

    disappeared = {d["ident"]: d for d in result["disappeared"]}
    assert set(disappeared) == {"aa:aa:aa:aa:aa:aa"}
    assert disappeared["aa:aa:aa:aa:aa:aa"]["rate_per_min"] == 6.0
    assert disappeared["aa:aa:aa:aa:aa:aa"]["comparable"] is True

    appeared = {d["ident"]: d for d in result["appeared"]}
    assert set(appeared) == {"cc:cc:cc:cc:cc:cc"}
    assert appeared["cc:cc:cc:cc:cc:cc"]["rate_per_min"] == 1.5

    changed = {d["ident"]: d for d in result["changed"]}
    assert set(changed) == {"bb:bb:bb:bb:bb:bb"}
    steady = changed["bb:bb:bb:bb:bb:bb"]
    assert steady["rate_a"] == 3.0 and steady["rate_b"] == 3.0
    assert steady["mean_signal_a"] == -60 and steady["mean_signal_b"] == -50
    assert steady["signal_change"] == 10.0

    assert any("percentages" in n for n in result["notes"])
    assert "excluded_private" not in result  # include_private defaults to false


def test_rpa_and_nrpa_excluded_from_matching_and_counted(store):
    store.add_coverage("ble", A_END, 0, 600)
    store.add_coverage("ble", B_END, 0, 600)
    _acc_write(
        store,
        [
            ("ble", "11:22:33:44:55:66", A_START, -70, "rpa"),
            ("ble", "22:33:44:55:66:77", A_START, -70, "nrpa"),
            ("ble", "aa:bb:cc:dd:ee:ff", A_START, -70, "static"),
        ],
    )
    _acc_write(
        store,
        [
            ("ble", "11:22:33:44:55:66", B_START, -70, "rpa"),  # different session, same text addr
            ("ble", "aa:bb:cc:dd:ee:ff", B_START, -70, "static"),
        ],
    )
    result = store.compare(
        a_start=A_START, a_end=A_END, b_start=B_START, b_end=B_END, receivers=("ble",)
    )
    # Only the stable address is compared; both private kinds are excluded
    # from appeared/disappeared/changed entirely.
    assert result["disappeared"] == []
    assert result["appeared"] == []
    assert [d["ident"] for d in result["changed"]] == ["aa:bb:cc:dd:ee:ff"]
    assert result["excluded_private_rows"] == 2

    with_private = store.compare(
        a_start=A_START,
        a_end=A_END,
        b_start=B_START,
        b_end=B_END,
        receivers=("ble",),
        include_private=True,
    )
    kinds = {row["ident"]: row["kind"] for row in with_private["excluded_private"]}
    assert kinds == {"11:22:33:44:55:66": "rpa", "22:33:44:55:66:77": "nrpa"}
    windows = {row["ident"]: row["window"] for row in with_private["excluded_private"]}
    assert windows["11:22:33:44:55:66"] == "both"
    assert windows["22:33:44:55:66:77"] == "a"


def test_no_coverage_in_one_window_is_not_comparable(store):
    # BLE never listened at all during window A (no add_coverage call);
    # window B did listen. Devices must still be reported but flagged.
    store.add_coverage("ble", B_END, 0, 600)
    _acc_write(store, [("ble", "aa:bb:cc:dd:ee:ff", B_START, -70, "static")])
    result = store.compare(
        a_start=A_START, a_end=A_END, b_start=B_START, b_end=B_END, receivers=("ble",)
    )
    assert result["comparable"] == {"ble": False}
    assert result["appeared"][0]["comparable"] is False
    assert any("coverage" in n.lower() for n in result["notes"])


def test_different_wifi_channels_not_comparable(store):
    store.add_coverage("wifi", A_END, 6, 600)
    store.add_coverage("wifi", B_END, 11, 600)
    _acc_write(store, [("wifi", "aa:bb:cc:dd:ee:01", A_START, 120, "bssid")])
    _acc_write(store, [("wifi", "aa:bb:cc:dd:ee:02", B_START, 110, "bssid")])
    result = store.compare(
        a_start=A_START, a_end=A_END, b_start=B_START, b_end=B_END, receivers=("wifi",)
    )
    assert result["comparable"] == {"wifi": False}
    assert result["a"]["coverage"]["wifi"]["channels"] == [6]
    assert result["b"]["coverage"]["wifi"]["channels"] == [11]
    # Still reported, just flagged -- never silently dropped.
    assert {d["ident"] for d in result["disappeared"]} == {"aa:bb:cc:dd:ee:01"}
    assert {d["ident"] for d in result["appeared"]} == {"aa:bb:cc:dd:ee:02"}
    assert all(d["comparable"] is False for d in result["disappeared"] + result["appeared"])
