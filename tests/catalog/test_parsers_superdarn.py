# ruff: noqa: E501 -- the hdw.dat and radar-info fixtures mirror real line layouts
from msdr.catalog.parsers.superdarn import (
    build_snapshot,
    entries_from_snapshot,
    parse_hdw_dat,
    parse_radar_info,
    parse_superdarn,
)

HDW_BKS = """\
#  Hardware parameters for Blackstone radar.
#       YYYYMMDD HH:MM:SS     LAT       LON      ALT   SIGHT   SET  SEP  SG PS  TDIFF   TDIFF   X      Y     Z    RXRIS  ATTN     BM
  33  1 20080201 00:00:00  37.10211  -77.95033  125.0  -40.0  8.00  3.86  1  1 -0.324   0.000   0.0  -58.9  -2.7    0.0   0 0 110 16
  33  1 20240327 00:00:00  37.10211  -77.95033  125.0  -40.0  0.00  3.24 -1 -1 -0.3364  0.000   0.0  -58.9  -2.7    0.0   0 0 110 24
# EOF
"""
HDW_TST = """\
# test radar, no names entry on radar-info
 999  1 20200101 00:00:00  -75.08952  123.35125  3000.0  115.0  0.00  3.24  1  1  0.0  0.0  0.0  0.0  0.0  0.0  0 0  75 16
"""
FLAG = "https://res.cloudinary.com/x/superDARN/flags/001-united-states_opyd17.svg"
RADAR_INFO = f"""
<table>
<tr><th></th><th>Code</th><th>Station ID</th><th>Radar, Country Located</th><th>PI</th><th>Institution, Country</th></tr>
<tr data-toggle='collapse' class='clickable' id='bks-table-row'>
 <td class='row-css'><i class="fa"></i></td> <th>bks</th> <td></td> <td>33</td>
 <td>Blackstone <img src=' {FLAG} ' style="height:20px;"></td>
 <td><a href="mailto:x@vt.edu">Dr. J. Michael Ruohoniemi</a></td>
 <td><a href="http://vt.superdarn.org/">Virginia Tech <img src=' {FLAG} '></a></td>
</tr>
<tr><td colspan='7'><div id='bks-collapse'><table><tr><td>Geographic Latitude</td><td>37.1</td></tr></table></div></td></tr>
</table>
"""


def test_parse_hdw_dat_takes_latest_configuration():
    hw = parse_hdw_dat(HDW_BKS)
    assert hw == {
        "station_id": 33, "lat": 37.10211, "lon": -77.95033, "boresight": -40.0,
        "gates": 110, "beams": 24, "config_valid_from": "2024-03-27 00:00:00",
    }
    assert parse_hdw_dat("# only comments\n") is None


def test_parse_radar_info_rows():
    assert parse_radar_info(RADAR_INFO) == {
        "bks": {
            "name": "Blackstone", "pi": "Dr. J. Michael Ruohoniemi",
            "institution": "Virginia Tech", "country": "United States",
        }
    }


def test_build_snapshot_and_entries():
    snap = build_snapshot({"bks": HDW_BKS, "tst": HDW_TST}, RADAR_INFO, retrieved_at="2026-10-01T00:00:00Z")
    assert [r["code"] for r in snap["radars"]] == ["bks", "tst"]
    assert snap["radars"][1]["name"] is None  # unjoined names stay null, never guessed
    assert "PI agreement" in snap["licence"]
    entries = entries_from_snapshot(snap)
    bks, tst = entries
    assert bks.source == "superdarn" and bks.ref_type == "radar_site" and bks.network == "SuperDARN"
    assert bks.station_id == "bks" and bks.station == "SuperDARN Blackstone"
    assert bks.operator == "Virginia Tech" and bks.country == "United States"
    assert (bks.latitude, bks.longitude) == (37.10211, -77.95033)
    assert bks.frequency_hz is None and bks.freq_lo_hz is None and bks.freq_hi_hz is None
    assert bks.mode is None and bks.decoder is None and bks.status == "unknown"
    assert "No operating frequency is published" in bks.notes
    assert "8–22 MHz (unverified)" in bks.notes and "PI agreement" in bks.notes
    assert bks.edition == "2026-10-01" and bks.source_url.endswith("hdw.dat.bks")
    assert tst.station == "SuperDARN TST" and tst.operator is None
    # Stable ids: a second build of the same snapshot yields the same ids.
    assert [e.id for e in entries_from_snapshot(snap)] == [bks.id, tst.id]


def test_bundled_snapshot_parses():
    entries = parse_superdarn()
    assert len(entries) >= 30
    named = [e for e in entries if not e.station.startswith("SuperDARN " + e.station_id.upper())]
    assert len(named) >= 0.9 * len(entries)
    assert all(e.frequency_hz is None and e.latitude is not None for e in entries)
    assert {e.id for e in entries} == {e.id for e in parse_superdarn()}
