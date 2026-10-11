import datetime as dt
from datetime import date
from pathlib import Path

from msdr.catalog.models import on_air_state
from msdr.catalog.parsers.faa_nasr import (
    current_cycle_date,
    ils_csv_url,
    nav_csv_url,
    parse_faa_nasr,
    parse_ils_rows,
    parse_nav_rows,
    parse_report,
    to_entries,
)

_FIXTURES = Path(__file__).parent / "fixtures"
NAV_FIXTURE = (_FIXTURES / "faa_nasr_nav_sample.csv").read_text()
ILS_FIXTURE = (_FIXTURES / "faa_nasr_ils_sample.csv").read_text()


def test_current_cycle_date_epoch_and_next():
    assert current_cycle_date(date(2026, 10, 1)) == date(2026, 10, 1)
    assert current_cycle_date(date(2026, 10, 15)) == date(2026, 10, 1)
    assert current_cycle_date(date(2026, 10, 29)) == date(2026, 10, 29)
    assert current_cycle_date(date(2026, 9, 30)) == date(2026, 9, 3)


def test_cycle_urls():
    cycle = date(2026, 10, 1)
    assert nav_csv_url(cycle) == (
        "https://nfdc.faa.gov/webContent/28DaySub/extra/01_Oct_2026_NAV_CSV.zip"
    )
    assert ils_csv_url(cycle) == (
        "https://nfdc.faa.gov/webContent/28DaySub/extra/01_Oct_2026_ILS_CSV.zip"
    )


def _by_station(rows, station_id):
    return [r for r in rows if r["station_id"] == station_id]


def test_sfo_oak_pye_sau_vor_dme_rows():
    rows, skipped = parse_nav_rows(NAV_FIXTURE, source_url="nav.zip", edition="2026-10-01")
    assert skipped == 0
    by_id = {r["station_id"]: r for r in rows if r["component"] == "VOR/DME"}
    assert by_id["SFO"]["frequency_hz"] == 115_800_000
    assert by_id["SFO"]["latitude"] == 37.61948513
    assert by_id["SFO"]["longitude"] == -122.37389347
    assert by_id["SFO"]["status"] == "operational_restricted"
    assert by_id["OAK"]["frequency_hz"] == 116_800_000
    assert by_id["PYE"]["frequency_hz"] == 113_700_000
    assert by_id["SAU"]["frequency_hz"] == 116_200_000
    for row in by_id.values():
        assert row["decoder"] == "vor"
        assert row["mode"] == "AM"
        assert row["ref_type"] == "navaid"
        assert row["network"] == "FAA NASR"
        assert row["operator"] == "FAA"
        assert row["country"] == "USA"


def test_ndb_row_khz_to_hz_no_decoder():
    rows, _ = parse_nav_rows(NAV_FIXTURE)
    ndb = _by_station(rows, "AA")[0]
    assert ndb["frequency_hz"] == 365_000  # 365 kHz -> Hz
    assert ndb["decoder"] is None
    assert ndb["mode"] == "AM"
    assert ndb["location"] == "FARGO, ND"


def test_dme_only_row_gets_channel_component_no_frequency():
    rows, _ = parse_nav_rows(NAV_FIXTURE)
    dme = _by_station(rows, "AAT")[0]
    assert dme["frequency_hz"] is None  # never a derived VHF frequency
    assert dme["component"] == "DME channel 113Y"
    assert dme["decoder"] is None
    assert dme["mode"] is None


def test_sfo_localizers():
    rows, skipped = parse_ils_rows(ILS_FIXTURE, source_url="ils.zip", edition="2026-10-01")
    assert skipped == 0
    by_id = {r["station_id"]: r for r in rows}
    assert by_id["SIA"]["frequency_hz"] == 108_900_000
    assert by_id["SIA"]["component"] == "LOC RWY 19L"
    assert by_id["SFO"]["frequency_hz"] == 109_550_000
    assert by_id["SFO"]["component"] == "LOC RWY 28L"
    assert by_id["GWQ"]["frequency_hz"] == 111_700_000
    assert by_id["GWQ"]["component"] == "LOC RWY 28R"
    for row in by_id.values():
        assert row["decoder"] == "ils"
        assert row["mode"] == "AM"
        assert row["status"] == "operational"  # raw "OPERATIONAL IFR"
        assert row["status_at"]  # COMPONENT_STATUS_DATE present


def test_parse_report_counts():
    report = parse_report(NAV_FIXTURE, ILS_FIXTURE)
    assert report["nav_rows"] == 6
    assert report["nav_skipped"] == 0
    assert report["ils_rows"] == 3
    assert report["ils_skipped"] == 0


def test_status_mapping_real_wording():
    rows, _ = parse_nav_rows(NAV_FIXTURE)
    sfo = _by_station(rows, "SFO")[0]
    assert sfo["status"] == "operational_restricted"
    assert "OPERATIONAL RESTRICTED" in sfo["notes"]


def test_missing_nav_id_is_skipped_and_counted():
    header = NAV_FIXTURE.splitlines()[0]
    blank_id_row = NAV_FIXTURE.splitlines()[1].replace('"OAK"', '""', 1)
    text = "\n".join([header, blank_id_row])
    rows, skipped = parse_nav_rows(text)
    assert skipped == 1
    assert rows == []


def _ndb_row_at(city: str, state: str, lat: str, lon: str) -> str:
    """The fixture's Fargo "AA" NDB row, relocated. The real cycle has 33
    (ident, type) pairs that repeat nationally, so the place has to take
    part in the stable id."""
    row = next(line for line in NAV_FIXTURE.splitlines() if line.startswith('"2026/10/01","AA"'))
    return (
        row.replace('"ND","FARGO"', f'"{state}","{city}"', 1)
        .replace('"47.00905216"', f'"{lat}"', 1)
        .replace('"-96.8151835"', f'"{lon}"', 1)
    )


def test_same_ident_ndbs_at_different_cities_get_distinct_stable_ids():
    header = NAV_FIXTURE.splitlines()[0]
    text = "\n".join(
        [
            header,
            _ndb_row_at("FARGO", "ND", "47.00905216", "-96.8151835"),
            _ndb_row_at("THOMSON", "GA", "33.53", "-82.51"),
        ]
    )
    rows, skipped = parse_nav_rows(text)
    assert skipped == 0
    first = to_entries(rows)
    assert [e.station_id for e in first] == ["AA", "AA"]
    assert [e.component for e in first] == ["NDB", "NDB"]
    assert [e.location for e in first] == ["FARGO, ND", "THOMSON, GA"]
    assert first[0].id != first[1].id
    # Deterministic across refreshes: a repeat parse yields the same ids.
    second = to_entries(parse_nav_rows(text)[0])
    assert [e.id for e in second] == [e.id for e in first]


def test_bundled_snapshot_counts_and_distinct_ids():
    entries = parse_faa_nasr()
    assert len(entries) == 3173
    assert len({e.id for e in entries}) == 3173
    assert sum(1 for e in entries if e.component.startswith("LOC")) == 1556
    assert {e.edition for e in entries} == {"2026-10-01"}
    assert len({e.id for e in parse_faa_nasr()} & {e.id for e in entries}) == 3173


def test_shutdown_status_is_unknown_on_air():
    header = NAV_FIXTURE.splitlines()[0]
    row = _ndb_row_at("FARGO", "ND", "47.00905216", "-96.8151835").replace(
        '"OPERATIONAL IFR"', '"SHUTDOWN"', 1
    )
    (entry,) = to_entries(parse_nav_rows("\n".join([header, row]))[0])
    assert entry.status == "shutdown"
    assert "status: SHUTDOWN" in entry.notes
    assert on_air_state(entry, dt.datetime(2026, 10, 1, 12, 0)) == "unknown"
