# ruff: noqa: E501 -- fixture lines below are copied verbatim, fixed-width,
# from the real HFCC file; reflowing them would break column alignment.
import datetime as dt
from pathlib import Path

import pytest

from msdr.catalog.parsers.hfcc import build_tables, load_tables, parse_hfcc, write_tables

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_HFCC_FILE = REPO_ROOT / "tmp" / "a26allx2" / "A26all00.TXT"

# Verbatim excerpt of A26all00.TXT: the ruler+header+ruler block, 7 rows of
# a NZL/USA transmission run, and the AGM broadcaster row (line 83).
HFCC_FIXTURE = """\
;----+----+----+------------------------------+---+----+-------+---+---+-------+------+------+-+-----+----------+---+---+---+-----+-+-----+-----+-----+-------
;FREQ STRT STOP CIRAF ZONES                    LOC POWR AZIMUTH SLW ANT DAYS    FDATE  TDATE MOD AFRQ LANGUAGE   ADM BRC FMO REQ# OLD ALT1 ALT2  ALT3  NOTES
;----+----+----+------------------------------+---+----+-------+---+---+-------+------+------+-+-----+----------+---+---+---+-----+-+-----+-----+-----+-------
 2485 1000 1900 56,51                          PVL   10 0         0 400 1234567 290326 241026 D  9000 Bis        VUT VBT RNZ  4347                     NZL
 3195 2100 0100 4,9,27,37-39                   WCR  100 46        0 902 1234567 010626 310826 D       Eng        USA WCR FCC  2347                     
 3195 2100 0100 4,9,27,37-39                   WCR  100 46        0 902 1234567 290326 310526 D       Eng        USA WCR FCC  2354                     
 3195 2100 0100 4,9,27,37-39                   WCR  100 46        0 902 1234567 010926 251026 D       Eng        USA WCR FCC  2355                     
 3215 0100 1200 4,9,27,37-39                   WCR  100 46        0 902 1234567 290326 310526 D       Eng        USA WCR FCC  2304                     
 3215 0100 1200 4,9,27,37-39                   WCR  100 46        0 902 1234567 010626 310826 D       Eng        USA WCR FCC  2396                     
 3215 0100 1200 4,9,27,37-39                   WCR  100 46        0 902 1234567 010926 251026 D       Eng        USA WCR FCC  2461                     
 4930 0530 0700 53,57                          BOT  100 20        0 750 36      220526 241026 D 10750 Eng        BOT AGM AGM  4324                     
"""


# Only the rows the fixture needs, in the real files' fixed-width layouts.
HFCC_TABLE_FILES = {
    "admin.txt": (";ADM table\nVUT " + "Vanuatu".ljust(51) + "Vanuatu\n").encode(),
    "broadcas.txt": b"AGM United States Agency for Global Media (USAGM)\n",
    "site.txt": b"PVL Port Vila                        VUT 17S44 168E19\n",
    # language.txt is UTF-8 with a BOM
    "language.txt": "\ufeff;Language\nBis Bislama\nAae Arbëreshë Albanian\n".encode(),
}
HFCC_TABLES = build_tables(HFCC_TABLE_FILES)


def test_parse_hfcc_fields():
    entries = parse_hfcc(HFCC_FIXTURE)
    assert len(entries) == 8  # every data row in the fixture (>=98% of 8)
    assert all(e.source == "hfcc" and e.mode == "AM" and e.bandwidth_hz == 9000 for e in entries)


def test_parse_hfcc_frequency_and_window():
    e = parse_hfcc(HFCC_FIXTURE)[0]
    assert e.frequency_hz == 2_485_000
    assert e.start_utc == "1000"
    assert e.stop_utc == "1900"


def test_hfcc_coordinates_are_read_only_from_explicit_decimal_fields():
    ruler = ";" + "-" * 4 + "+" + "-" * 10 + "+" + "-" * 10
    header = ";" + "FREQ".ljust(5) + "LATITUDE".ljust(11) + "LONGITUDE".ljust(10)
    row = " 6070 " + "37.5".ljust(11) + "-122.4"
    parsed = parse_hfcc("\n".join([ruler, header, ruler, row]))[0]
    assert (parsed.latitude, parsed.longitude) == (37.5, -122.4)


def test_parse_hfcc_partial_week_mask():
    # AGM row's DAYS is '36' -> Monday-first digits 3=Wed, 6=Sat.
    e = next(e for e in parse_hfcc(HFCC_FIXTURE) if e.frequency_hz == 4_930_000)
    assert e.days == "..W..S."


def test_parse_hfcc_broadcaster_resolved_via_table():
    e = next(e for e in parse_hfcc(HFCC_FIXTURE, tables=HFCC_TABLES) if e.frequency_hz == 4_930_000)
    assert e.station == "United States Agency for Global Media (USAGM)"


def test_parse_hfcc_country_and_language_resolved_via_tables():
    e = parse_hfcc(HFCC_FIXTURE, tables=HFCC_TABLES)[0]
    assert e.country == "Vanuatu"  # ADM 'VUT' via admin.txt
    assert e.language == "Bislama"  # LANGUAGE 'Bis' via languages.json
    assert e.location == "Port Vila"  # LOC 'PVL' via site.txt


def test_hfcc_language_names_are_decoded_as_utf8_and_tables_round_trip(tmp_path):
    assert HFCC_TABLES["languages"]["Aae"] == "Arbëreshë Albanian"
    write_tables(tmp_path, HFCC_TABLES)
    assert load_tables(tmp_path) == HFCC_TABLES
    assert load_tables(tmp_path / "missing")["languages"] == {}


def test_parse_hfcc_without_tables_keeps_raw_codes():
    e = parse_hfcc(HFCC_FIXTURE)[0]
    assert (e.country, e.language, e.location) == ("VUT", "Bis", "PVL")


def test_parse_hfcc_unknown_code_falls_back_to_code():
    e = parse_hfcc("""\
;----+----+----+------------------------------+---+----+-------+---+---+-------+------+------+-+-----+----------+---+---+---+-----+-+-----+-----+-----+-------
;FREQ STRT STOP CIRAF ZONES                    LOC POWR AZIMUTH SLW ANT DAYS    FDATE  TDATE MOD AFRQ LANGUAGE   ADM BRC FMO REQ# OLD ALT1 ALT2  ALT3  NOTES
;----+----+----+------------------------------+---+----+-------+---+---+-------+------+------+-+-----+----------+---+---+---+-----+-+-----+-----+-----+-------
 9999 0000 0100 1                              ZZZ   10 0         0 100 1234567 010126 311226 D       Xyz        ZZZ ZZZ ZZZ  0001
""")[0]
    assert e.station == "ZZZ"  # unmapped BRC keeps the raw code
    assert e.country == "ZZZ"  # unmapped ADM keeps the raw code


def test_parse_hfcc_today_filters_by_fdate_tdate():
    all_entries = parse_hfcc(HFCC_FIXTURE)
    wcr_all = [e for e in all_entries if e.frequency_hz in (3_195_000, 3_215_000)]
    assert len(wcr_all) == 6  # 3 FDATE/TDATE variants x 2 frequencies

    filtered = parse_hfcc(HFCC_FIXTURE, today=dt.date(2026, 7, 1))
    wcr_filtered = [e for e in filtered if e.frequency_hz in (3_195_000, 3_215_000)]
    assert len(wcr_filtered) == 2  # only the 010626-310826 validity window covers Jul 1


def test_parse_hfcc_smoke_real_file():
    if not REAL_HFCC_FILE.exists():
        pytest.skip("real HFCC file not present (tmp/ is gitignored)")
    text = REAL_HFCC_FILE.read_text(encoding="latin-1")
    entries = parse_hfcc(text)
    assert len(entries) == 3342
    agm = [e for e in entries if e.station == "United States Agency for Global Media (USAGM)"]
    assert len(agm) > 0
