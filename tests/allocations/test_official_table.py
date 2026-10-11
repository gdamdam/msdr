"""Import completeness checked against the official table, not against the
importer's own output: the expectations below are transcribed by hand from
the FCC/NTIA Table of Frequency Allocations (47 CFR 2.106 / NTIA Manual ch. 4,
2025 edition) and never read from dataset.json. Service names, priorities
(ALL CAPS = primary, Title Case = secondary), service-bound footnotes and
cell footnotes follow the printed table."""

import pytest

from msdr.allocations.store import AllocationStore


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return AllocationStore(data_dir=tmp_path_factory.mktemp("alloc-official"))


def _services(block):
    return [(s["name"], s["qualifier"], s["priority"]) for s in block["services"]]


def _footnotes_of(block, name):
    return next(s["footnotes"] for s in block["services"] if s["name"] == name)


def test_1800_2000_khz_amateur_primary_non_federal_region_2(store):
    r = store.lookup(1_900_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (1_800_000, 2_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    # The Federal column is blank here; Non-Federal reads "AMATEUR" with NG92.
    assert fed["services"] == [] and fed["unallocated"] is True
    assert _services(nonfed) == [("AMATEUR", None, "primary")]
    assert nonfed["footnotes"] == ["NG92"]
    assert "Amateur Radio (97)" in nonfed["rule_parts"]
    intl = r["international"]
    assert intl["region"] == "ITU2" and (intl["lo_hz"], intl["hi_hz"]) == (1_850_000, 2_000_000)
    assert _services(intl) == [
        ("AMATEUR", None, "primary"),
        ("FIXED", None, "primary"),
        ("MOBILE", "except aeronautical mobile", "primary"),
        ("RADIOLOCATION", None, "primary"),
        ("RADIONAVIGATION", None, "primary"),
    ]
    assert intl["footnotes"] == ["5.102"]


def test_2400_2483_5_mhz_amateur_and_footnotes_5_150_us41(store):
    # 2400-2417 MHz: Non-Federal AMATEUR (primary) 5.150 5.282; Federal blank, 5.150 G122.
    r = store.lookup(2_405_000_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (2_400_000_000, 2_417_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(nonfed) == [("AMATEUR", None, "primary")]
    assert set(nonfed["footnotes"]) == {"5.150", "5.282"}
    assert fed["services"] == [] and set(fed["footnotes"]) == {"5.150", "G122"}

    # 2417-2450 MHz: Federal Radiolocation (secondary) G2; Non-Federal Amateur (secondary).
    r = store.lookup(2_430_000_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (2_417_000_000, 2_450_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(fed) == [("Radiolocation", None, "secondary")]
    assert _footnotes_of(fed, "Radiolocation") == ["G2"]
    assert _services(nonfed) == [("Amateur", None, "secondary")]
    assert set(nonfed["footnotes"]) == {"5.150", "5.282"}

    # 2450-2483.5 MHz: Non-Federal FIXED, MOBILE, Radiolocation; 5.150 US41 in both columns.
    r = store.lookup(2_460_000_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (2_450_000_000, 2_483_500_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(nonfed) == [
        ("FIXED", None, "primary"),
        ("MOBILE", None, "primary"),
        ("Radiolocation", None, "secondary"),
    ]
    assert set(nonfed["footnotes"]) == {"5.150", "US41"}
    assert set(fed["footnotes"]) == {"5.150", "US41"}
    assert "ISM Equipment (18)" in nonfed["rule_parts"]

    # Region 2, 2300-2450 MHz: amateur secondary under 5.150 and 5.282.
    intl = store.lookup(2_405_000_000, "ITU2")["international"]
    assert (intl["lo_hz"], intl["hi_hz"]) == (2_300_000_000, 2_450_000_000)
    assert ("Amateur", None, "secondary") in _services(intl)
    assert {"5.150", "5.282"} <= set(intl["footnotes"])


def test_1215_1240_mhz_federal_eess_radiolocation_rnss(store):
    r = store.lookup(1_227_600_000, "US")  # GPS L2
    assert (r["lo_hz"], r["hi_hz"]) == (1_215_000_000, 1_240_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(fed) == [
        ("EARTH EXPLORATION-SATELLITE", "(active)", "primary"),
        ("RADIOLOCATION", None, "primary"),
        ("RADIONAVIGATION-SATELLITE", "(space-to-Earth) (space-to-space)", "primary"),
        ("SPACE RESEARCH", "(active)", "primary"),
    ]
    assert _footnotes_of(fed, "RADIOLOCATION") == ["G56"]
    assert _footnotes_of(fed, "RADIONAVIGATION-SATELLITE") == ["G132"]
    assert fed["footnotes"] == ["5.332"]
    assert _services(nonfed) == [
        ("Earth Exploration-Satellite", "(active)", "secondary"),
        ("Space Research", "(active)", "secondary"),
    ]
    intl = r["international"]
    assert ("RADIONAVIGATION-SATELLITE", "(space-to-Earth) (space-to-space)", "primary") in (
        _services(intl)
    )
    assert {"5.328B", "5.329", "5.329A"} <= set(_footnotes_of(intl, "RADIONAVIGATION-SATELLITE"))


def test_108_117_975_mhz_aeronautical_radionavigation_both_columns(store):
    r = store.lookup(110_000_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (108_000_000, 117_975_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(fed) == [("AERONAUTICAL RADIONAVIGATION", None, "primary")]
    assert _services(nonfed) == [("AERONAUTICAL RADIONAVIGATION", None, "primary")]
    assert set(fed["footnotes"]) == {"5.197A", "US93"}
    assert set(nonfed["footnotes"]) == {"5.197A", "US93"}
    assert nonfed["rule_parts"] == ["Aviation (87)"]
    assert r["national"]["shared"] is False
    intl = r["international"]
    assert _services(intl) == [("AERONAUTICAL RADIONAVIGATION", None, "primary")]
    assert set(intl["footnotes"]) == {"5.197", "5.197A"}


def test_420_450_mhz_radiolocation_primary_federal_amateur_secondary_non_federal(store):
    r = store.lookup(446_000_000, "US")
    assert (r["lo_hz"], r["hi_hz"]) == (420_000_000, 450_000_000)
    fed, nonfed = r["national"]["federal"], r["national"]["non_federal"]
    assert _services(fed) == [("RADIOLOCATION", None, "primary")]
    assert set(_footnotes_of(fed, "RADIOLOCATION")) == {"G2", "G129"}
    assert _services(nonfed) == [("Amateur", None, "secondary")]
    assert _footnotes_of(nonfed, "Amateur") == ["US270"]
    for block in (fed, nonfed):
        assert {"5.286", "US87", "US230", "US269", "US397"} <= set(block["footnotes"])
    assert {"Private Land Mobile (90)", "Amateur Radio (97)"} <= set(nonfed["rule_parts"])
