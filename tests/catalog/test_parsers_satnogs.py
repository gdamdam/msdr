import json

from msdr.catalog.parsers.satnogs import default_url, parse_satnogs, url_for_norad

SATNOGS_FIXTURE = json.dumps(
    [
        {
            "uuid": "aaa",
            "description": "FM Voice/Data",
            "alive": True,
            "status": "active",
            "type": "Transmitter",
            "mode": "FM",
            "baud": 9600,
            "downlink_low": 145900000,
            "downlink_high": None,
            "norad_cat_id": 25544,
            "sat_id": "SAT-1",
        },
        {
            "uuid": "bbb",
            "description": "Linear transponder",
            "alive": True,
            "status": "active",
            "type": "Transponder",
            "mode": "BPSK",
            "baud": None,
            "downlink_low": 145850000,
            "downlink_high": 145950000,
            "norad_cat_id": 7530,
            "sat_id": "SAT-2",
        },
        {
            "uuid": "ccc",
            "description": "Dead beacon",
            "alive": False,
            "status": "inactive",
            "type": "Transmitter",
            "mode": "CW",
            "baud": None,
            "downlink_low": 436000000,
            "downlink_high": None,
            "norad_cat_id": 99999,
            "sat_id": "SAT-3",
        },
    ]
)


def test_parse_satnogs_skips_inactive_and_dead():
    entries = parse_satnogs(SATNOGS_FIXTURE)
    assert len(entries) == 2
    assert all(e.source == "satnogs" for e in entries)
    assert all("99999" not in e.station for e in entries)


def test_parse_satnogs_simple_downlink_low_only():
    entries = parse_satnogs(SATNOGS_FIXTURE)
    iss = next(e for e in entries if "25544" in e.station)
    assert iss.frequency_hz == 145_900_000
    assert iss.mode == "NFM"  # SatNOGS FM maps to NFM
    assert iss.bandwidth_hz == 12500
    assert "FM" in iss.notes
    assert "9600 baud" in iss.notes
    assert iss.days is None
    assert iss.start_utc == "0000" and iss.stop_utc == "2400"


def test_parse_satnogs_transponder_midpoint_and_wider_bandwidth():
    entries = parse_satnogs(SATNOGS_FIXTURE)
    transponder = next(e for e in entries if "7530" in e.station)
    assert transponder.frequency_hz == (145_850_000 + 145_950_000) // 2
    assert transponder.bandwidth_hz == 100_000  # wider than the 12500 floor
    assert transponder.mode == "NFM"  # non-FM modes still map to NFM
    assert "BPSK" in transponder.notes


def test_default_url_and_norad_filter_helper():
    assert default_url == "https://db.satnogs.org/api/transmitters/?format=json&alive=true"
    assert url_for_norad(25544) == (
        "https://db.satnogs.org/api/transmitters/?format=json&satellite__norad_cat_id=25544"
    )
