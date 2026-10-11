import pytest

from msdr.allocations.importer import (
    normalize_footnote_text,
    parse_band_label,
    parse_ecfr_footnotes,
    parse_ntia_footnotes,
    parse_ntia_table,
    parse_service_text,
    resolve_missing_footnotes,
)
from msdr.allocations.model import Footnote

from .fixture_cells import ROW_1, ROW_96, ROW_339, page


def _by(bands, column, lo):
    return next(b for b in bands if b.column == column and b.lo_hz == lo)


def test_band_label_units_and_exact_hz():
    assert parse_band_label("0-8.3 kHz") == (0, 8300)
    assert parse_band_label("5.25-5.275 MHz") == (5_250_000, 5_275_000)
    assert parse_band_label("275-3000 GHz") == (275_000_000_000, 3_000_000_000_000)
    with pytest.raises(ValueError):
        parse_band_label("5.25 MHz")


def test_service_text_priority_qualifier_and_footnotes():
    s = parse_service_text("MOBILE Except Aeronautical Mobile")
    assert (s.name, s.priority, s.qualifier) == ("MOBILE", "primary", "except aeronautical mobile")
    s = parse_service_text("Radiolocation (5.132A)")
    assert (s.name, s.priority, s.footnotes) == ("Radiolocation", "secondary", ["5.132A"])
    s = parse_service_text("AERONAUTICAL MOBILE (R)")
    assert (s.name, s.qualifier, s.footnotes) == ("AERONAUTICAL MOBILE", "(R)", [])
    s = parse_service_text("FIXED-SATELLITE (Earth-to-space) (US245, NG52)")
    assert (s.name, s.qualifier, s.footnotes) == (
        "FIXED-SATELLITE",
        "(Earth-to-space)",
        ["US245", "NG52"],
    )
    s = parse_service_text("STANDARD FREQUENCY AND TIME SIGNAL (20 kHz)")
    assert (s.name, s.qualifier) == ("STANDARD FREQUENCY AND TIME SIGNAL", "(20 kHz)")
    s = parse_service_text("Mobile-Satellite Except Aeronautical Mobile-Satellite (Earth-to-space)")
    assert s.qualifier == "except aeronautical mobile-satellite (Earth-to-space)"
    assert s.priority == "secondary"
    assert parse_service_text("NOT ALLOCATED") is None
    assert parse_service_text("RADIOLOCATION (G129, G2)").raw == "RADIOLOCATION (G129, G2)"


def test_grid_cells_map_to_columns_rows_and_rule_parts():
    problems = []
    bands = parse_ntia_table(page(ROW_1, ROW_96, ROW_339), problems)
    assert problems == []
    # 2 + 2 (spanning cells) + 5 columns in row 96 + 2 in row 339
    assert len(bands) == 5 + 5 + 2

    r1 = _by(bands, "itu_r1", 5_250_000)
    assert r1.hi_hz == 5_275_000 and r1.cell_id == "alloc-375"
    assert [(s.name, s.priority) for s in r1.services] == [
        ("FIXED", "primary"),
        ("MOBILE", "primary"),
        ("Radiolocation", "secondary"),
    ]
    assert r1.services[2].footnotes == ["5.132A"]
    assert r1.footnotes == ["5.133A"]
    assert r1.rule_parts == []

    fed = _by(bands, "us_federal", 5_250_000)
    nonfed = _by(bands, "us_non_federal", 5_250_000)
    assert fed.cell_id == nonfed.cell_id == "alloc-378"  # one cell spanning both US columns
    assert [s.name for s in fed.services] == ["FIXED", "MOBILE", "RADIOLOCATION"]
    assert fed.footnotes == ["US340"] and nonfed.footnotes == ["US340"]
    assert fed.rule_parts == []
    assert nonfed.rule_parts == ["Maritime (80)", "Private Land Mobile (90)"]

    bottom = _by(bands, "itu_r3", 0)
    assert bottom.unallocated and bottom.services == [] and bottom.footnotes == ["5.53", "5.54"]
    assert _by(bands, "us_non_federal", 0).unallocated

    fed420 = _by(bands, "us_federal", 420_000_000)
    assert fed420.services[0].footnotes == ["G129", "G2"]
    assert fed420.footnotes == ["5.286", "G8", "US230", "US269", "US270", "US397", "US64", "US87"]
    nf420 = _by(bands, "us_non_federal", 420_000_000)
    # Rule-part cells are joined by grid-row overlap; double spaces collapse.
    assert nf420.rule_parts == ["Private Land Mobile (90)", "MedRadio (95I)", "Amateur Radio (97)"]


def test_unparseable_cell_is_reported_not_silently_dropped():
    from .fixture_cells import cell

    bad = cell("alloc-x", "1 / 2", "5 / 6", "five to six MHz", ["FIXED"], None)
    problems = []
    bands = parse_ntia_table(page([bad]), problems)
    assert bands == []
    assert problems[0]["cell_id"] == "alloc-x"


ECFR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<DIV8 N="2.106" TYPE="SECTION">
<HEAD>\xc2\xa7 2.106 Table of Frequency Allocations.</HEAD>
<P>(a) <I>Allocation Table.</I> Intro text.</P>
<P>(b) <I>International footnotes.</I> The list of international footnotes follows:</P>
<P>(1)-(52) [Reserved]</P>
<P>(53) 5.53 Administrations authorizing the use of frequencies below 8.3 kHz shall ensure.</P>
<P>(i) 5.132A Stations in the radiolocation service shall not cause harmful interference.</P>
<P>Note 1 to \xc2\xa7 2.106(b)(132)(i): continuation.</P>
<P>(c) <I>United States Footnotes.</I> The list follows:</P>
<P>(340) US340 The band 2-30 MHz is available on a non-interference basis.</P>
<P>(i) Sub-paragraph of US340.</P>
<DIV><TABLE><TR><TH>Band</TH><TH>Use</TH></TR>
<TR><TD>2-30 MHz</TD><TD>Quality</TD></TR></TABLE></DIV>
<P>(d) <I>Non-Federal Government (NG) Footnotes.</I> The list follows:</P>
<P>(42) <I>NG42.</I> The band 2360-2395 MHz is allocated.</P>
<P>(e) <I>Federal Government (G) footnotes.</I> The list follows:</P>
<P>(2) G2 In the bands 216-225 MHz, the fixed and mobile services are limited.</P>
</DIV8>
"""


def test_ecfr_footnotes_paragraphs_b_to_e():
    fns = {f.id: f for f in parse_ecfr_footnotes(ECFR_XML)}
    assert set(fns) == {"5.53", "5.132A", "US340", "NG42", "G2"}
    assert fns["5.53"].text.startswith("Administrations authorizing")
    assert fns["5.53"].kind == "international"
    assert "Note 1" in fns["5.132A"].text  # continuation paragraph stays with its footnote
    assert fns["US340"].kind == "us" and "Sub-paragraph" in fns["US340"].text
    assert "2-30 MHz | Quality" in fns["US340"].text
    assert fns["NG42"].kind == "non_federal" and fns["NG42"].text.startswith("The band 2360")
    assert fns["G2"].kind == "federal"


NTIA_FOOTNOTE_PAGE = (
    '<div class="pre-rendered-footnotes" style="margin-top: 24px;">'
    '<div style="font-weight: bold;">Footnotes</div>'
    '<div data-footnote-id="toa_5.53" tabindex="0" class="inline-footnote">'
    '<b data-location-id="4-34-1">5.53</b><p class="MsoNormal">'
    '<span class="Artdef"><span> </span></span>'
    "<span>Administrations\nauthorizing the use of frequencies below 8.3 kHz shall ensure that no "
    'harmful\ninterference is caused.<span style="mso-spacerun:yes"> </span>(WRC\u201112)'
    "</span></p>\n"
    '</div><div data-footnote-id="toa_US115" tabindex="0" class="inline-footnote">'
    "<b>US115</b><p>In the bands 5000-5010 MHz and 5010-5030 MHz, the following provisions "
    "apply:</p><p>(a) First &amp; second.</p>\n"
    '</div><div data-footnote-id="toa_5.172" tabindex="0" class="inline-footnote">'
    "<b>5.172</b><p>Different category of service: i n Re g ion 2.</p></div></div>"
)


def test_ntia_footnote_page_text():
    texts = parse_ntia_footnotes(NTIA_FOOTNOTE_PAGE)
    assert list(texts) == ["5.53", "US115", "5.172"]
    assert texts["5.53"] == (
        "Administrations authorizing the use of frequencies below 8.3 kHz shall ensure that no "
        "harmful interference is caused. (WRC\u201112)"
    )
    # Multi-paragraph text joins, entities decode, no "<div" leaks from the next block.
    assert texts["US115"] == (
        "In the bands 5000-5010 MHz and 5010-5030 MHz, the following provisions apply: "
        "(a) First & second."
    )
    assert "<" not in texts["5.172"]
    # Letter-spacing artefacts are kept verbatim; only the comparison key ignores them.
    assert normalize_footnote_text(texts["5.172"]) == normalize_footnote_text(
        "Different category of service: in Region 2."
    )


def test_fallback_footnotes_from_ntia_page_only_for_ids_ecfr_lacks():
    bands = parse_ntia_table(page(ROW_1))  # references 5.53 and 5.54
    ecfr = [Footnote(id="5.53", text="eCFR wording")]
    fallbacks, unresolved = resolve_missing_footnotes(
        bands, ecfr, {"5.53": "NTIA wording", "5.54": "NTIA text for 5.54"}
    )
    assert [(f.id, f.text, f.source_id, f.fallback, f.missing) for f in fallbacks] == [
        ("5.54", "NTIA text for 5.54", "ntia_footnotes", True, False)
    ]
    assert unresolved == []
    assert ecfr[0].text == "eCFR wording"  # eCFR stays primary
    _, unresolved = resolve_missing_footnotes(bands, ecfr, {})
    assert unresolved == ["5.54"]
