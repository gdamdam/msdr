import datetime as dt
import json

import pytest

from msdr.allocations.importer import build_dataset
from msdr.allocations.model import Band, Footnote, Service, SourceStatus
from msdr.allocations.store import AllocationStore

GHZ = 1_000_000_000
TOP_HZ = 3000 * GHZ


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    # Empty data dir: only the bundled dataset is in play, never the user's home.
    return AllocationStore(data_dir=tmp_path_factory.mktemp("alloc-empty"))


def _names(block):
    return [(s["name"], s["priority"]) for s in block["services"]]


def test_bundled_dataset_loads_with_both_sources_ok(store):
    statuses = {s["id"]: s for s in store.sources()}
    assert set(statuses) == {"ntia_table", "ecfr_2_106", "ntia_footnotes"}
    assert statuses["ntia_footnotes"]["footnote_count"] == 16
    assert statuses["ntia_table"]["origin"] == "bundled"
    assert statuses["ntia_table"]["lo_hz"] == 0 and statuses["ntia_table"]["hi_hz"] == TOP_HZ
    assert statuses["ntia_table"]["band_count"] >= 2000
    assert statuses["ecfr_2_106"]["effective_date"] >= "2026-09-29"
    assert statuses["ntia_table"]["provenance"]["sha256"]


def test_lookup_5264_khz_us(store):
    r = store.lookup(5_264_000, "US")
    assert r["status"] == "ok"
    assert (r["lo_hz"], r["hi_hz"]) == (5_250_000, 5_275_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    # One grid cell spans both US columns here, so Non-Federal is identical
    # to Federal (RADIOLOCATION is primary on both, not secondary).
    assert _names(fed) == [
        ("FIXED", "primary"),
        ("MOBILE", "primary"),
        ("RADIOLOCATION", "primary"),
    ]
    assert _names(nonfed) == _names(fed)
    assert fed["services"][1]["qualifier"] == "except aeronautical mobile"
    assert fed["services"][2]["footnotes"] == ["5.132A"]
    assert fed["footnotes"] == ["US340"] and nonfed["footnotes"] == ["US340"]
    assert nonfed["rule_parts"] == ["Maritime (80)", "Private Land Mobile (90)"]
    assert r["national"]["shared"] is True
    intl = r["international"]
    assert intl["region"] == "ITU2"
    assert _names(intl) == [
        ("FIXED", "primary"),
        ("MOBILE", "primary"),
        ("RADIOLOCATION", "primary"),
    ]
    resolved = {f["id"]: f for f in r["footnotes"]}
    assert set(resolved) == {"US340", "5.132A"}
    assert resolved["US340"]["text"].startswith("The band 2-30 MHz is available")
    assert resolved["5.132A"]["source_id"] == "ecfr_2_106" and not resolved["5.132A"]["missing"]
    assert [c["source_id"] for c in r["citations"]] == ["ntia_table", "ecfr_2_106"]
    assert r["sources"]["statuses"] == {
        "ntia_table": "ok",
        "ecfr_2_106": "ok",
        "ntia_footnotes": "ok",
    }


def test_lookup_5264_khz_itu1_has_no_national_block(store):
    r = store.lookup(5_264_000, "ITU1")
    assert r["status"] == "ok" and r["national"] is None
    assert r["international"]["region"] == "ITU1"
    assert _names(r["international"]) == [
        ("FIXED", "primary"),
        ("MOBILE", "primary"),
        ("Radiolocation", "secondary"),
    ]
    assert r["international"]["footnotes"] == ["5.133A"]
    assert (r["lo_hz"], r["hi_hz"]) == (5_250_000, 5_275_000)


def test_lookup_420_450_mhz_shared_services(store):
    r = store.lookup(435_000_000, "US")
    assert r["status"] == "ok" and (r["lo_hz"], r["hi_hz"]) == (420_000_000, 450_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _names(fed) == [("RADIOLOCATION", "primary")]
    assert fed["services"][0]["footnotes"] == ["G129", "G2"]
    assert _names(nonfed) == [("Amateur", "secondary")]
    assert nonfed["services"][0]["footnotes"] == ["US270"]
    assert "Amateur Radio (97)" in nonfed["rule_parts"]
    assert r["national"]["shared"] is True
    # ITU Region 2 splits 420-450 into sub-bands; the context band is the one containing 435 MHz.
    assert r["international"]["lo_hz"] <= 435_000_000 < r["international"]["hi_hz"]
    assert r["international"]["lo_hz"] >= 420_000_000
    ids = {f["id"] for f in r["footnotes"]}
    assert {"G129", "G2", "US270", "5.286", "US87"} <= ids


def test_exact_boundary_belongs_to_upper_band(store):
    below = store.lookup(5_275_000 - 1, "US")
    on = store.lookup(5_275_000, "US")
    assert (below["lo_hz"], below["hi_hz"]) == (5_250_000, 5_275_000)
    assert on["lo_hz"] == 5_275_000 and on["hi_hz"] > 5_275_000
    assert below["status"] == on["status"] == "ok"


def test_top_edge_inclusive_and_beyond_not_covered(store):
    top = store.lookup(TOP_HZ, "US")
    assert top["status"] == "unallocated"
    assert (top["lo_hz"], top["hi_hz"]) == (275 * GHZ, TOP_HZ)
    assert "275000000000-3000000000000 Hz is not allocated" in top["message"]
    assert {f["id"] for f in top["footnotes"]} == {"US565", "5.564A", "5.565"}
    beyond = store.lookup(TOP_HZ + 1, "US")
    assert beyond["status"] == "not_covered"
    assert beyond["message"] == (
        f"{TOP_HZ + 1} Hz is outside the loaded US allocation range 0-{TOP_HZ} Hz"
    )
    assert store.lookup(-1, "ITU2")["status"] == "not_covered"


def test_table_starts_at_zero_and_is_unallocated(store):
    r = store.lookup(0, "ITU3")
    assert r["status"] == "unallocated" and (r["lo_hz"], r["hi_hz"]) == (0, 8300)
    assert r["international"]["footnotes"] == ["5.53", "5.54"]


def test_unsupported_jurisdiction(store):
    r = store.lookup(1_000_000, "EU")
    assert r["status"] == "unsupported_jurisdiction"
    assert r["message"] == "jurisdiction 'EU' is not supported; use one of US, ITU1, ITU2, ITU3"
    assert r["national"] is None and r["footnotes"] == []


def test_footnote_absent_from_ecfr_falls_back_to_ntia_page(store):
    # US115 is referenced by the table but printed as "US1157" in the eCFR
    # text; its prose comes from the NTIA footnote page, flagged as such.
    r = store.lookup(5_005_000_000, "US")
    assert r["status"] == "ok"
    fn = next(f for f in r["footnotes"] if f["id"] == "US115")
    assert fn["source_id"] == "ntia_footnotes" and fn["fallback"] is True
    assert fn["missing"] is False and fn["text"].startswith("In the bands 5000-5010 MHz")
    assert [c["source_id"] for c in r["citations"]] == [
        "ntia_table",
        "ecfr_2_106",
        "ntia_footnotes",
    ]
    # eCFR stays primary for everything it has.
    us = next(f for f in r["footnotes"] if f["id"] != "US115" and f["id"].startswith("US"))
    assert us["source_id"] == "ecfr_2_106" and us["fallback"] is False


def test_bands_window(store):
    rows = store.bands(5_200_000, 5_300_000, "US")
    cols = {b["column"] for b in rows}
    assert cols == {"us_federal", "us_non_federal", "itu_r2"}
    assert all(b["lo_hz"] < 5_300_000 and b["hi_hz"] > 5_200_000 for b in rows)
    fed = store.bands(5_200_000, 5_300_000, "US", column="us_federal")
    assert [b["column"] for b in fed] == ["us_federal"] * len(fed)
    assert [b["lo_hz"] for b in fed] == sorted(b["lo_hz"] for b in fed)
    assert store.bands(1, 2, "ITU9") == []


def test_source_unavailable_when_bundled_missing(tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "dataset.json"
    monkeypatch.setattr(AllocationStore, "bundled_path", staticmethod(lambda: missing))
    store = AllocationStore(data_dir=tmp_path / "data")
    r = store.lookup(5_264_000, "US")
    assert r["status"] == "source_unavailable"
    assert str(missing) in r["message"] and "file not found" in r["message"]
    errs = store.sources()
    assert errs[0]["status"] == "error" and errs[0]["url"] == str(missing)
    assert store.bands(0, 10, "US") == []


def _tiny_dataset(
    generated_at: str,
    lo=0,
    hi=1_000,
    columns=("itu_r1", "itu_r2", "itu_r3", "us_federal", "us_non_federal"),
) -> dict:
    bands = [
        Band(
            lo_hz=lo,
            hi_hz=hi,
            column=c,
            services=[
                Service(name="AMATEUR", priority="primary", footnotes=["5.53"], raw="AMATEUR 5.53")
            ],
            source_id="user_table",
        )
        for c in columns
    ]
    fns = [Footnote(id="5.53", text="tiny")]
    src = [
        SourceStatus(
            id="user_table",
            title="tiny",
            publisher="t",
            url="file",
            lo_hz=lo,
            hi_hz=hi,
            band_count=len(bands),
        )
    ]
    ds = build_dataset(bands, fns, src)
    ds["generated_at"] = generated_at
    return ds


def test_overlay_with_all_columns_replaces_every_column(tmp_path):
    (tmp_path / "dataset.json").write_text(json.dumps(_tiny_dataset("2999-01-01T00:00:00Z")))
    store = AllocationStore(data_dir=tmp_path)
    origins = {s["id"]: s["origin"] for s in store.sources()}
    assert origins == {
        "ntia_table": "bundled",
        "ecfr_2_106": "bundled",
        "ntia_footnotes": "bundled",
        "user_table": "imported",
    }
    r = store.lookup(500, "US")
    assert r["status"] == "ok" and r["national"]["federal"]["services"][0]["name"] == "AMATEUR"
    assert [c["source_id"] for c in r["citations"]] == ["ecfr_2_106", "user_table"]
    assert r["footnotes"][0]["text"] == "tiny"  # overlay footnote wins over the bundled 5.53
    assert store.lookup(5_264_000, "US")["status"] == "not_covered"


def test_partial_overlay_replaces_only_its_columns(tmp_path):
    # An ITU-only import (itu_r1, 0-1 kHz) must leave the US columns bundled.
    ds = _tiny_dataset("2999-01-01T00:00:00Z", columns=("itu_r1",))
    ds["footnotes"] = []  # 5.53 resolves against the bundled footnotes
    (tmp_path / "dataset.json").write_text(json.dumps(ds))
    store = AllocationStore(data_dir=tmp_path)
    assert [s["id"] for s in store.sources()] == [
        "ntia_table",
        "ecfr_2_106",
        "ntia_footnotes",
        "user_table",
    ]

    us = store.lookup(5_264_000, "US")
    assert us["status"] == "ok"
    assert [s["name"] for s in us["national"]["federal"]["services"]] == [
        "FIXED",
        "MOBILE",
        "RADIOLOCATION",
    ]
    assert us["international"]["region"] == "ITU2"
    assert [c["source_id"] for c in us["citations"]] == ["ntia_table", "ecfr_2_106"]

    itu1 = store.lookup(500, "ITU1")
    assert itu1["status"] == "ok" and (itu1["lo_hz"], itu1["hi_hz"]) == (0, 1_000)
    assert itu1["international"]["services"][0]["name"] == "AMATEUR"
    assert [c["source_id"] for c in itu1["citations"]] == ["ecfr_2_106", "user_table"]
    assert store.lookup(5_264_000, "ITU1")["status"] == "not_covered"
    assert store.lookup(5_264_000, "ITU3")["status"] == "ok"  # bundled column untouched
    assert {b["column"] for b in store.bands(0, 10_000, "US")} == {
        "us_federal",
        "us_non_federal",
        "itu_r2",
    }


def test_older_overlay_is_ignored_and_reported(tmp_path):
    (tmp_path / "dataset.json").write_text(json.dumps(_tiny_dataset("2000-01-01T00:00:00Z")))
    store = AllocationStore(data_dir=tmp_path)
    ids = [s["id"] for s in store.sources()]
    assert ids[:2] == ["ntia_table", "ecfr_2_106"]
    assert store.sources()[-1]["status"] == "error"
    assert "older than bundled dataset" in store.sources()[-1]["error"]
    assert store.lookup(5_264_000, "US")["status"] == "ok"


def test_corrupt_overlay_falls_back_to_bundled(tmp_path):
    user = tmp_path / "dataset.json"
    user.write_text('{"schema_version": 1, "bands": [')
    store = AllocationStore(data_dir=tmp_path)
    assert store.lookup(5_264_000, "US")["status"] == "ok"
    bad = [s for s in store.sources() if s["status"] == "error"]
    assert len(bad) == 1 and bad[0]["url"] == str(user) and "unreadable" in bad[0]["error"]

    # Structurally fine JSON but failing validation (overlap) is also ignored.
    ds = _tiny_dataset("2999-01-01T00:00:00Z")
    ds["bands"].append(dict(ds["bands"][0], lo_hz=500))
    user.write_text(json.dumps(ds))
    store.reload()
    assert store.lookup(5_264_000, "US")["status"] == "ok"
    bad = [s for s in store.sources() if s["status"] == "error"]
    assert "invalid" in bad[0]["error"] and "overlap" in bad[0]["error"]


def test_stale_flag_from_import_age(tmp_path):
    ds = _tiny_dataset("2999-01-01T00:00:00Z")
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=400)).isoformat()
    ds["sources"][0]["imported_at"] = old
    (tmp_path / "dataset.json").write_text(json.dumps(ds))
    store = AllocationStore(data_dir=tmp_path)
    by_id = {s["id"]: s for s in store.sources()}
    assert by_id["user_table"]["stale"] is True and by_id["ntia_table"]["stale"] is False
    assert store.lookup(10, "ITU1")["sources"]["stale"] is True


def test_lo_edge_belongs_to_that_band_and_hi_edge_to_the_next(store):
    # [lo_hz, hi_hz): the lower edge is inside the band, the upper edge is the
    # first Hz of the next one (docs/allocations.md "Boundary rule").
    on_lo = store.lookup(5_250_000, "US")
    assert (on_lo["lo_hz"], on_lo["hi_hz"]) == (5_250_000, 5_275_000)
    on_hi = store.lookup(5_275_000, "US")
    assert on_hi["lo_hz"] == 5_275_000
    for jurisdiction in ("ITU1", "ITU2", "ITU3"):
        r = store.lookup(5_250_000, jurisdiction)
        assert (r["lo_hz"], r["hi_hz"]) == (5_250_000, 5_275_000), jurisdiction


def test_itu_regions_differ_and_us_overrides_the_international_table(store):
    us = store.lookup(5_264_000, "US")
    itu1 = store.lookup(5_264_000, "ITU1")
    itu3 = store.lookup(5_264_000, "ITU3")
    assert us["national"] is not None and us["international"]["region"] == "ITU2"
    assert itu1["national"] is None and itu3["national"] is None
    # Regions 1 and 3 carry Radiolocation secondary; the US table makes it primary.
    assert ("Radiolocation", "secondary") in _names(itu1["international"])
    assert ("Radiolocation", "secondary") in _names(itu3["international"])
    assert ("RADIOLOCATION", "primary") in _names(us["national"]["federal"])
    # Region 1 alone references 5.133A on this band.
    assert itu1["international"]["footnotes"] == ["5.133A"]
    assert itu3["international"]["footnotes"] == []

    # 1.9 MHz: amateur primary in the US (Non-Federal) and Region 3, absent
    # from the Region 1 table.
    us = store.lookup(1_900_000, "US")
    itu1 = store.lookup(1_900_000, "ITU1")
    itu3 = store.lookup(1_900_000, "ITU3")
    assert ("AMATEUR", "primary") in _names(us["national"]["non_federal"])
    assert ("AMATEUR", "primary") in _names(itu3["international"])
    assert "AMATEUR" not in {n for n, _ in _names(itu1["international"])}
    assert _names(itu1["international"]) != _names(itu3["international"])


def test_store_never_opens_a_socket(tmp_path, monkeypatch):
    import socket

    def _no_network(*args, **kwargs):
        raise AssertionError("allocation store must not touch the network")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "socket", _no_network)
    offline = AllocationStore(data_dir=tmp_path)
    assert offline.lookup(5_264_000, "US")["status"] == "ok"
    assert offline.bands(5_200_000, 5_300_000, "ITU1")
    assert offline.sources()
    offline.reload()
    assert offline.lookup(435_000_000, "ITU3")["status"] == "ok"


def test_lookup_and_bands_are_fast_enough_for_live_tuning(store):
    import random
    import time

    rng = random.Random(1)
    probes = [rng.randint(0, TOP_HZ) for _ in range(10_000)]
    t0 = time.perf_counter()
    for hz in probes:
        store.lookup(hz, "US")
    lookups = time.perf_counter() - t0
    assert lookups < 1.0, f"10,000 lookups took {lookups:.2f}s"

    t0 = time.perf_counter()
    rows = store.bands(0, TOP_HZ, "US", column="us_non_federal")
    full_column = time.perf_counter() - t0
    assert len(rows) > 500 and full_column < 0.05, f"full-column bands() took {full_column:.3f}s"


def test_wrong_typed_generated_at_or_disagreements_fall_back_to_bundled(tmp_path):
    user = tmp_path / "dataset.json"
    for key, value in (("generated_at", 12345), ("disagreements", ["not-a-dict"])):
        ds = _tiny_dataset("2999-01-01T00:00:00Z")
        ds[key] = value
        user.write_text(json.dumps(ds))
        store = AllocationStore(data_dir=tmp_path)  # must not raise
        assert store.lookup(5_264_000, "US")["status"] == "ok"
        assert [s for s in store.sources() if s["status"] == "error"]
