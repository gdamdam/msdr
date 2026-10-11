# ruff: noqa: E501 -- fixture lines below are copied verbatim, fixed-width,
# from the real xta26/userlist1 files; reflowing them would break column
# alignment.
from pathlib import Path

import pytest

from msdr.catalog.parsers._days import digit_ranges_to_mask
from msdr.catalog.parsers.aoki import parse_aoki

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_XTA26_FILE = REPO_ROOT / "tmp" / "xta26" / "xta26.txt"
REAL_USERLIST1_FILE = REPO_ROOT / "tmp" / "userlist1.txt"

# Verbatim excerpt of xta26.txt: its banner+header line and its first 13
# transmissions (fixed-width, Sunday-first positional DAYS column).
XTA26_FIXTURE = """\
A26 Shortwave Frequecy List  September 22 2026,  0400 UTC   Day 1 = Sunday
FRE   STATION                         UTC       Su-W-Sa Language             Pow Azi Location                ADM L/L             Remarks
   40 Time Signal                     0000-2400 1234567 A1B                   50  ND Otakadoyama Tamura C    J   372221N1405056E NICT
   60 Time Signal                     0000-2400 1234567 A1B                   50  ND Haganeyama              J   332756N1301032E NICT
   60 WWVB                            0000-2400 1234567 PWM/PM                 5  ND Colorado Denver         USA 404028N1050239W WWVB
 68.5 BPC Time Signal                 0000-2400 1234567                       90     Shanngqiu               CHN 342723N1155013E PBC
 77.5 BSF Time signal                 0000-2400 1234567                       50     Zhongli                 TWN 250021N1212154E BSF
  100 BPL Time Signal                 0000-2400 1234567                      800     Pucheng                 CHN 345655N1093235E BPL
  153 Antena Satelor                  0355-2000 1234567 Romanian             100  ND Brasov/Bod Colonie      ROU 454513N0253625E SRR
  162 ALS162 Time Signal              0000-2400 1234567 French               800     Allouis                 F   471010N0021216E TDF
  164 Mongolian Radio 1               2300-1600 1234567 Mongolian            500  ND Ulaanbaatar             MNG 474755N1071115E MNR
  171 Radio Mediterranee Int'l,       0500-2400 1234567 French/Arabic/      1600  ND Nador                   MRC 350230N0025515W MEDI1
  198 BBC R4                          0500-0100  23456  English              250     Droitwich               G   521744N0020622W BBC R4
  198 BBC R4                          0520-0100 1234567 English               25     Burghead                G   574153N0032811W BBC R4
  198 BBC R4                          0520-0100 1234567 English               25     Westerglen              G   555828N0034905W BBC R4
"""

# Verbatim excerpt of userlist1.txt: its banner+header+ruler block, its
# first 12 transmissions, plus one non-contiguous row (CNR 8 @ 5975 kHz,
# real Days value '5-3') to exercise a wraparound day-range.
USERLIST1_FIXTURE = """\
A26 Shortwave Frequecy List  September 22 2026,  0400 UTC   Day 1 = Sunday
kHz           Time(UTC)       ITU Station                Lang. Location    Days 
+-------------+---------+-----+---+------------------------+---+-----------+----

40            0000-2400       J   Time Signal              A1B Otakadoyama 1-7  
60            0000-2400       J   Time Signal              A1B Haganeyama  1-7  
60            0000-2400       USA WWVB                     PWM Colorado De 1-7  
69            0000-2400       CHN BPC Time Signal              Shanngqiu   1-7  
78            0000-2400       TWN BSF Time signal              Zhongli     1-7  
100           0000-2400       CHN BPL Time Signal              Pucheng     1-7  
153           0355-2000       ROU Antena Satelor           Rom Brasov/Bod  1-7  
162           0000-2400       F   ALS162 Time Signal       Fre Allouis     1-7  
164           2300-1600       MNG Mongolian Radio 1        Mon Ulaanbaatar 1-7  
171           0500-2400       MRC Radio Mediterranee Int'l Fre Nador       1-7  
198           0500-0100       G   BBC R4                   Eng Droitwich   23456
198           0520-0100       G   BBC R4                   Eng Burghead    1-7  
5975          0600-0800       CHN CNR 8                    Kor Beijing 491 5-3  
"""


def test_parse_xta26_fields():
    entries = parse_aoki(XTA26_FIXTURE)
    assert len(entries) == 13  # every data row in the fixture (100% >= 98%)
    assert all(e.source == "aoki" and e.mode == "AM" and e.bandwidth_hz == 9000 for e in entries)


def test_parse_xta26_time_signal_daily():
    e = next(e for e in parse_aoki(XTA26_FIXTURE) if e.frequency_hz == 40_000)
    assert e.station == "Time Signal"
    assert e.days == "MTWTFSS"
    assert e.language == "A1B"
    assert e.power_kw == 50.0
    assert e.location == "Otakadoyama Tamura C"  # coordinates are not absorbed
    assert e.country == "J"


def test_parse_xta26_bbc_monday_friday_mask():
    # DAYS column ' 23456  ' (positional, Sunday-first): Sun+Sat blanked.
    e = next(e for e in parse_aoki(XTA26_FIXTURE) if e.station == "BBC R4" and e.start_utc == "0500")
    assert e.days == "MTWTF.."
    assert e.language == "English"
    assert e.power_kw == 250.0
    assert e.location == "Droitwich"


def test_parse_xta26_power_overflow_does_not_corrupt_language():
    # BPL Time Signal's Pow is 800 (3 digits, the common case).
    e = next(e for e in parse_aoki(XTA26_FIXTURE) if e.frequency_hz == 100_000)
    assert e.power_kw == 800.0
    assert e.language is None


def test_parse_userlist1_fields():
    entries = parse_aoki(USERLIST1_FIXTURE)
    assert len(entries) == 13  # every data row in the fixture (100% >= 98%)
    assert all(e.source == "aoki" and e.mode == "AM" and e.bandwidth_hz == 9000 for e in entries)


def test_parse_userlist1_bbc_monday_friday_mask():
    # DAYS column '23456' (concatenated digits, Sunday-first): Sun+Sat off.
    e = next(e for e in parse_aoki(USERLIST1_FIXTURE) if e.station == "BBC R4" and e.start_utc == "0500")
    assert e.days == "MTWTF.."
    assert e.language == "Eng"


def test_parse_userlist1_wraparound_range_from_real_file():
    # Real Days value '5-3' wraps past 7 back to 1: 5,6,7,1,2,3 -> Wed off.
    e = next(e for e in parse_aoki(USERLIST1_FIXTURE) if e.station == "CNR 8")
    assert e.days == "MT.TFSS"
    assert e.country == "CHN"


def test_userlist1_day_range_2_dash_6_is_monday_friday():
    assert digit_ranges_to_mask("2-6", first_day="sunday") == "MTWTF.."


def test_parse_xta26_smoke_real_file():
    if not REAL_XTA26_FILE.exists():
        pytest.skip("real xta26 file not present (tmp/ is gitignored)")
    text = REAL_XTA26_FILE.read_text(encoding="latin-1")
    entries = parse_aoki(text)
    data_rows = [ln for ln in text.splitlines()[2:] if ln.strip()]
    assert len(entries) >= 0.98 * len(data_rows)
    assert len(entries) == 5339


def test_parse_userlist1_smoke_real_file():
    if not REAL_USERLIST1_FILE.exists():
        pytest.skip("real userlist1 file not present (tmp/ is gitignored)")
    text = REAL_USERLIST1_FILE.read_text(encoding="latin-1")
    entries = parse_aoki(text)
    data_rows = [ln for ln in text.splitlines()[4:] if ln.strip()]
    assert len(entries) >= 0.98 * len(data_rows)
    assert len(entries) == 5343
