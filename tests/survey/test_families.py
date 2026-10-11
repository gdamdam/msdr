"""family_for(): the symbol-rate -> protocol table from the design doc/plan."""

import pytest

from msdr.survey.families import family_for


@pytest.mark.parametrize(
    ("modulation_kind", "symbol_rate_hz", "expected_name", "expected_decoders"),
    [
        ("fm_fsk", 1200.0, "POCSAG1200 / AX.25 AFSK", ["pocsag", "aprs"]),
        ("fm_fsk", 2400.0, "POCSAG2400", ["pocsag"]),
        ("fm_fsk", 1600.0, "FLEX", ["flex"]),
        ("fm_fsk", 3200.0, "FLEX", ["flex"]),
        ("fm_fsk", 4800.0, "DMR / P25 / NXDN", ["dmr", "p25", "nxdn"]),
        ("fm_fsk", 9600.0, "GMSK/FSK 9600 (AIS or packet)", ["ais", "packet9600"]),
        ("fm_fsk", 45.45, "RTTY", ["rtty"]),
        ("fm_fsk", 50.0, "RTTY / NAVTEX", ["rtty"]),
        ("fm_fsk", 100.0, "RTTY / NAVTEX", ["rtty"]),
        ("ook", 20.0, "keyed carrier (unknown code)", []),
        ("carrier", None, "carrier / beacon", []),
    ],
)
def test_family_for_table_cases(modulation_kind, symbol_rate_hz, expected_name, expected_decoders):
    family = family_for(modulation_kind, symbol_rate_hz, "nfm", 5000.0)
    assert family is not None
    assert family["name"] == expected_name
    assert family["suggested_decoders"] == expected_decoders


def test_family_for_unmatched_symbol_rate_is_none():
    assert family_for("fm_fsk", 700.0, "nfm", 5000.0) is None


def test_family_for_ook_fast_is_not_cw():
    # CW is "ook & slow (< 100)" per the plan -- a fast OOK rate isn't CW,
    # even with Morse-like timing; it stays a keyed carrier.
    family = family_for("ook", 1200.0, "nfm", 5000.0, keying={"fits_cw": True})
    assert family["name"] == "keyed carrier (unknown code)"
    assert family["suggested_decoders"] == []


def test_family_for_cw_needs_slow_rate_and_dot_dash_timing():
    cw = family_for("ook", 20.0, "nfm", 500.0, keying={"fits_cw": True})
    assert cw["name"] == "CW"
    assert cw["suggested_decoders"] == ["cw"]
    for keying in (None, {"fits_cw": False}, {"fits_cw": None}):
        family = family_for("ook", 20.0, "nfm", 500.0, keying=keying)
        assert family["name"] == "keyed carrier (unknown code)", keying
        assert family["suggested_decoders"] == []


def test_family_for_9600_row_alternatives_and_next_action():
    family = family_for("fm_fsk", 9600.0, "nfm", 12000.0)
    assert family["alternatives"] == ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s ×2)"]
    assert family["next_action"] == (
        "decode ais or packet9600; AIS identity requires a CRC-valid frame with an MMSI"
    )


@pytest.mark.parametrize(
    ("modulation_kind", "symbol_rate_hz"),
    [("fm_fsk", 1200.0), ("fm_fsk", 4800.0), ("fm_fsk", 9600.0), ("carrier", None), ("ook", 20.0)],
)
def test_every_family_carries_alternatives_and_next_action(modulation_kind, symbol_rate_hz):
    family = family_for(modulation_kind, symbol_rate_hz, "nfm", 5000.0)
    assert isinstance(family["alternatives"], list)
    assert isinstance(family["next_action"], str) and family["next_action"]


def test_family_for_unknown_modulation_is_none():
    assert family_for("unknown", None, "nfm", 5000.0) is None
    assert family_for("am", None, "am", 5000.0) is None


@pytest.mark.parametrize("bw_class", ["wfm", "wide_digital"])
def test_family_for_wide_bw_class_is_always_none(bw_class):
    # Wide labels never reach the analyzer at all, but family_for itself
    # honours the bypass regardless of modulation_kind/symbol_rate_hz.
    assert family_for("fm_fsk", 1200.0, bw_class, 150_000.0) is None
    assert family_for("carrier", None, bw_class, 150_000.0) is None


def test_family_for_available_ids_filters_suggested_decoders():
    # dsd-fme not installed -> dmr/p25/nxdn dropped, pocsag/aprs unaffected.
    family = family_for("fm_fsk", 4800.0, "nfm", 5000.0, available_ids={"pocsag"})
    assert family["suggested_decoders"] == []

    family = family_for("fm_fsk", 4800.0, "nfm", 5000.0, available_ids={"dmr", "nxdn"})
    assert family["suggested_decoders"] == ["dmr", "nxdn"]


def test_family_for_available_ids_default_is_unfiltered():
    family = family_for("fm_fsk", 1200.0, "nfm", 5000.0)
    assert family["suggested_decoders"] == ["pocsag", "aprs"]


def test_cw_can_use_measured_dot_duration_without_symbol_clock():
    family = family_for("ook", None, "nfm", 300, keying={"fits_cw": True, "unit_s": 0.06})
    assert family["suggested_decoders"] == ["cw"]
    assert (
        family_for("ook", None, "nfm", 300, keying={"fits_cw": True, "unit_s": 0.001})[
            "suggested_decoders"
        ]
        == []
    )
