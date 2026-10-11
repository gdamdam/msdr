from datetime import date
from pathlib import Path

from msdr.catalog.parsers.ioos_hfradar import (
    default_url,
    erddap_url,
    parse_ioos_hfradar_rows,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "ioos_hfradar_sample.csv").read_text()

_NOW = date(2026, 10, 2)  # fixed "now" for status-by-date tests


def _by_station(rows, station_id):
    return next(r for r in rows if r["station_id"] == station_id)


def test_default_urls():
    assert default_url == "https://hfradar.ioos.us/hfrnet/sitediag/stationList.php?output=CSV"
    assert erddap_url == "https://hfradar.ioos.us/erddap/tabledap/allDatasets.csv"


def test_frequencies_mhz_to_hz():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert _by_station(rows, "ACI1")["frequency_hz"] == 13_475_000
    assert _by_station(rows, "AGL1")["frequency_hz"] == 4_450_500
    assert _by_station(rows, "ANGL")["frequency_hz"] == 41_340_000
    assert _by_station(rows, "CDN")["frequency_hz"] == 16_050_000


def test_manufacturer_component_mapping():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert _by_station(rows, "ACI1")["component"] == "CODAR SeaSonde"
    assert _by_station(rows, "AGL1")["component"] == "CODAR SeaSonde"
    assert _by_station(rows, "CDN")["component"] == "WERA"


def test_mode_and_decoder_always_none():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert all(r["mode"] is None for r in rows)
    assert all(r["decoder"] is None for r in rows)


def test_status_by_last_radial_date():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    # ACI1: 2026-10-01, 1 day before "now" -> recent
    assert _by_station(rows, "ACI1")["status"] == "data_recent"
    assert _by_station(rows, "ACI1")["status_at"] == "2026-10-01"
    # CDN: 2026-10-02, same day as "now" -> recent
    assert _by_station(rows, "CDN")["status"] == "data_recent"
    # ANGL: 2026-09-25, 7 days before "now" -> still recent (within 30 days)
    assert _by_station(rows, "ANGL")["status"] == "data_recent"


def test_status_unknown_when_stale():
    stale_csv = (
        'Station,"Station Name",Network,Frequency,Bandwidth,SweepRate,'
        '"Date Added","Last Radial File",Latitude,Longitude,Manufacturer\n'
        'OLD1,"Old Site",TEST,10.00000,1.0,1.0,2010-01-01,2026-01-01,'
        '30.0,-80.0,"CODAR Ocean Sensors. SeaSonde"\n'
    )
    rows = parse_ioos_hfradar_rows(stale_csv, now=_NOW)
    assert rows[0]["status"] == "unknown"
    assert rows[0]["status_at"] == "2026-01-01"


def test_bandwidth_never_used_for_freq_lo_hi():
    # Live Bandwidth values are negative/sentinel-ridden and not a
    # defensible kHz span -- freq_lo_hz/freq_hi_hz always stay null.
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert all(r["freq_lo_hz"] is None and r["freq_hi_hz"] is None for r in rows)


def test_notes_mention_last_radial_caveat():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert all("not a transmission indicator" in r["notes"] for r in rows)


def test_network_and_ref_type_constants():
    rows = parse_ioos_hfradar_rows(FIXTURE, now=_NOW)
    assert all(r["ref_type"] == "radar_site" for r in rows)
    assert all(r["network"] == "IOOS HFRNet" for r in rows)
    assert _by_station(rows, "ACI1")["operator"] == "UCSB"
    assert _by_station(rows, "CDN")["operator"] == "UMiami"


def test_erddap_failure_does_not_break_import():
    rows = parse_ioos_hfradar_rows(FIXTURE, "not,valid\nerddap<<<garbage", now=_NOW)
    assert len(rows) == 4


def test_erddap_enrichment_adds_institution_when_it_differs():
    erddap_csv = "datasetID,institution\nUCSB_ACI1,University of California Santa Barbara\n"
    rows = parse_ioos_hfradar_rows(FIXTURE, erddap_csv, now=_NOW)
    aci1 = _by_station(rows, "ACI1")
    assert "ERDDAP institution: University of California Santa Barbara" in aci1["notes"]
