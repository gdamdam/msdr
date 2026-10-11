from msdr.allocations.importer import find_disagreements, validate
from msdr.allocations.model import Band, Footnote, Service

FN = [Footnote(id="5.53", text="x"), Footnote(id="US340", text="y", kind="us")]


def _band(lo, hi, column="itu_r2", services=None, footnotes=(), unallocated=False):
    return Band(
        lo_hz=lo,
        hi_hz=hi,
        column=column,
        services=services or [],
        footnotes=list(footnotes),
        unallocated=unallocated,
    )


def _svc(name="FIXED", priority="primary", footnotes=()):
    return Service(name=name, priority=priority, footnotes=list(footnotes), raw=name)


def test_contiguous_column_passes():
    bands = [
        _band(0, 10, services=[_svc()]),
        _band(10, 20, services=[_svc(footnotes=["5.53"])]),
        _band(20, 30, unallocated=True, footnotes=["US340"]),
    ]
    assert validate(bands, FN, lo_hz=0, hi_hz=30) == []


def test_overlap_rejected():
    bands = [_band(0, 10, services=[_svc()]), _band(5, 20, services=[_svc()])]
    errors = validate(bands, FN)
    assert any("overlap" in e and "itu_r2" in e for e in errors), errors


def test_gap_rejected_and_not_bridged():
    bands = [_band(0, 10, services=[_svc()]), _band(20, 30, services=[_svc()])]
    errors = validate(bands, FN)
    assert errors == ["itu_r2: gap 10 Hz-20 Hz"]


def test_unresolved_footnote_rejected_but_missing_placeholder_accepted():
    bands = [_band(0, 10, services=[_svc(footnotes=["NG999"])])]
    assert validate(bands, FN) == ["itu_r2 0 Hz-10 Hz: footnote NG999 unresolved"]
    placeholder = Footnote(id="NG999", text=None, source_id=None, kind="non_federal", missing=True)
    assert validate(bands, FN + [placeholder]) == []


def test_unknown_priority_floor_and_extent():
    bands = [_band(0, 10, services=[_svc(priority="tertiary")])]
    errors = validate(bands, FN, min_bands=2, lo_hz=0, hi_hz=20)
    assert "only 1 bands, floor is 2" in errors
    assert any("unknown priority 'tertiary'" in e for e in errors)
    assert "itu_r2: ends at 10 Hz, expected 20 Hz" in errors


def test_empty_band_must_be_marked_unallocated():
    assert validate([_band(0, 10)], FN) == [
        "itu_r2 0 Hz-10 Hz: no services but not marked unallocated"
    ]


def test_disagreements_ignore_formatting_but_not_wording():
    fns = [
        Footnote(id="US340", text="See § 87.149 of this chapter for the list.", kind="us"),
        Footnote(id="5.118", text="in the United States, Mexico and Peru, the frequency band"),
    ]
    ntia = {
        "US340": "See 47 CFR 87.149 for the list. (WRC-12)",
        "5.118": "in the United States, Mexico, Peru and Uruguay, the band",
    }
    assert find_disagreements(fns, ntia) == [
        {"kind": "footnote_text", "id": "5.118", "sources": ["ntia_table", "ecfr_2_106"]}
    ]
