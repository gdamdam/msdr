from msdr.catalog.parsers.userlists import parse_gqrx_csv, parse_sdrpp_json

SDRPP_FIXTURE = """\
{
  "bookmarks": {
    "NOAA 15": {
      "bandwidth": 12500, "frequency": 137620000, "mode": 0,
      "latitude": 38.5, "longitude": -122.5
    },
    "WWV 10": {"bandwidth": 9000, "frequency": 10000000, "mode": 2}
  }
}
"""

GQRX_FIXTURE = """\
# Frequency ; Name ; Modulation ; Bandwidth ; Tags
10000000;WWV;AM;9000;time
137620000;NOAA 15;NFM;12500;wx;38.5;-122.5
"""


def test_parse_sdrpp_json():
    entries = parse_sdrpp_json(SDRPP_FIXTURE, filename="bookmarks.json")
    assert len(entries) == 2
    assert all(e.source == "user:bookmarks.json" for e in entries)
    noaa = next(e for e in entries if e.station == "NOAA 15")
    assert noaa.frequency_hz == 137_620_000
    assert noaa.mode == "NFM"  # mode index 0
    assert (noaa.latitude, noaa.longitude) == (38.5, -122.5)
    wwv = next(e for e in entries if e.station == "WWV 10")
    assert wwv.mode == "AM"  # mode index 2


def test_parse_gqrx_csv():
    entries = parse_gqrx_csv(GQRX_FIXTURE, filename="gqrx.csv")
    assert len(entries) == 2
    assert all(e.source == "user:gqrx.csv" for e in entries)
    wwv = next(e for e in entries if e.station == "WWV")
    assert wwv.frequency_hz == 10_000_000
    assert wwv.mode == "AM"
    assert wwv.bandwidth_hz == 9000
    assert wwv.notes == "time"
    noaa = next(e for e in entries if e.station == "NOAA 15")
    assert (noaa.latitude, noaa.longitude) == (38.5, -122.5)
