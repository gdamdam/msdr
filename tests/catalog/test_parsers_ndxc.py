from pathlib import Path

import pytest

from msdr.catalog.parsers.ndxc import default_url, parse_ndxc

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SKED_FILE = REPO_ROOT / "tmp" / "sked.csv"

# Verbatim excerpt of sked.csv (no header row in this format): its first 12
# rows, semicolon-separated, Sunday-first digit days.
NDXC_FIXTURE = """\
60;0000-2400;1234567;J;Time Signal;A1B;ND;NICT;
60;0000-2400;1234567;USA;WWVB;PWM/PM;ND;WWVB;
68.5;0000-2400;1234567;CHN;BPC Time Signal;;;PBC;
77.5;0000-2400;1234567;TWN;BSF Time signal;;;BSF;
100;0000-2400;1234567;CHN;BPL Time Signal;;;BPL;
153;0355-2000;1234567;ROU;Antena Satelor;Romanian;ND;SRR;
162;0000-2400;1234567;F;ALS162 Time Signal;French;;TDF;
164;2300-1600;1234567;MNG;Mongolian Radio 1;Mongolian;ND;MNR;
171;0500-2400;1234567;MRC;Radio Mediterranee Int'l ;French/Arabic/;ND;MEDI1;
198;0500-0100;23456;G;BBC R4;English;;BBC R4;
198;0520-0100;1234567;G;BBC R4;English;;BBC R4;
198;0520-0100;1234567;G;BBC R4;English;;BBC R4;
"""


def test_parse_ndxc_fields():
    entries = parse_ndxc(NDXC_FIXTURE)
    assert len(entries) == 12  # every row in the fixture (100% >= 98%)
    assert all(e.source == "ndxc" and e.mode == "AM" and e.bandwidth_hz == 9000 for e in entries)


def test_parse_ndxc_time_signal_is_daily_and_non_directional():
    e = next(
        e
        for e in parse_ndxc(NDXC_FIXTURE)
        if e.frequency_hz == 60_000 and e.station == "Time Signal"
    )
    assert e.days == "MTWTFSS"  # '1234567' -> every day
    assert e.language == "A1B"
    assert e.country == "J"  # ITU code kept as-is
    assert e.start_utc == "0000"
    assert e.stop_utc == "2400"
    assert "non-directional" in e.notes
    assert "NICT" in e.notes  # broadcaster folded into notes


def test_parse_ndxc_bbc_monday_friday_mask():
    e = next(e for e in parse_ndxc(NDXC_FIXTURE) if e.station == "BBC R4" and e.start_utc == "0500")
    assert e.days == "MTWTF.."  # '23456' Sunday-first -> Mon-Fri
    assert e.language == "English"


def test_parse_ndxc_broadcaster_and_blank_azimuth_in_notes():
    # BPC Time Signal's Azimuth/Broadcaster fields are '' and 'PBC'.
    e = next(e for e in parse_ndxc(NDXC_FIXTURE) if e.frequency_hz == 68_500)
    assert e.notes == "PBC"


def test_default_url():
    assert default_url == "https://ndxc.info/wordpress/wp-content/uploads/freqdb/sked.csv"


def test_parse_ndxc_smoke_real_file():
    if not REAL_SKED_FILE.exists():
        pytest.skip("real sked.csv file not present (tmp/ is gitignored)")
    text = REAL_SKED_FILE.read_text(encoding="latin-1")
    entries = parse_ndxc(text)
    data_rows = [ln for ln in text.splitlines() if ln.strip()]
    assert len(entries) >= 0.98 * len(data_rows)
    assert len(entries) == 5343
