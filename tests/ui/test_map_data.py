"""Map normalization behavior exercised in Node, matching the UI's ES module."""

# ruff: noqa: E501 -- embedded JavaScript source lines
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MAP_JS = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "map-data.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_map_rows_explicit_coords_ecef_integrity_and_unmapped_messages():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows }} from {module};
const events = [
  {{id:"ais1", decoder:"ais", ts:10, frequency_hz:162000000, integrity:"passed", integrity_note:"CRC verified", text:"ship", fields:{{mmsi:123456789, lat_deg:37.8, lon_deg:-122.4, cog_deg:271}}}},
  {{id:"bad", decoder:"ais", ts:11, integrity:"failed", text:"bad frame", fields:{{lat_deg:1, lon_deg:2}}}},
  {{id:"rs-ok", decoder:"rs41", ts:12, integrity:"passed", fields:{{serial:"R1", ecef_position_m:[6378137,0,0]}}}},
  {{id:"rs-bad", decoder:"rs41", ts:13, integrity:"failed", fields:{{ecef_position_m:[6378137,0,0]}}}},
  {{id:"none", decoder:"dsc", ts:14, fields:{{}}}},
  {{id:"invalid", decoder:"ais", ts:15, integrity:"passed", fields:{{lat_deg:91, lon_deg:181}}}},
];
const rows = mapRows(events, [], []);
const byId = Object.fromEntries(rows.map(r => [r.id, r]));
if (rows.length !== events.length) throw new Error("decoded messages were dropped");
if (byId.ais1.layer !== "maritime" || byId.ais1.lat !== 37.8 || byId.ais1.lon !== -122.4) throw new Error("AIS point mismatch");
if (byId.ais1.courseDeg !== 271 || byId.bad.courseDeg !== null) throw new Error("course normalization mismatch");
if (byId.bad.lat !== null || byId.bad.lon !== null) throw new Error("failed integrity mapped");
if (Math.abs(byId["rs-ok"].lat) > 1e-7 || Math.abs(byId["rs-ok"].lon) > 1e-7 || Math.abs(byId["rs-ok"].altitude) > 1) throw new Error("ECEF conversion mismatch");
if (byId["rs-bad"].lat !== null || byId.none.lat !== null || byId.invalid.lon !== null) throw new Error("invalid/unmapped point accepted");
if (byId.ais1.trackId !== "unknown-source:ais:123456789" || !byId.ais1.evidence.includes("CRC verified")) throw new Error("track/evidence missing");
if (byId.none.layer !== "maritime") throw new Error("DSC should be maritime");
""")


def test_station_coords_and_track_splits():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows, trackSegments }} from {module};
const signals = [{{id:"s1", name:"Base", frequency_hz:145000000, latitude:40, longitude:-75}}];
const rows = mapRows([
  {{id:"a", decoder:"aprs", ts:1, frequency_hz:145000000, fields:{{from:"N0CALL"}}}},
  {{id:"b", decoder:"aprs", ts:2, source_id:"src", frequency_hz:145000000, fields:{{from:"N0CALL", latitude:40, longitude:178}}}},
  {{id:"c", decoder:"aprs", ts:3, source_id:"src", frequency_hz:145000000, fields:{{from:"N0CALL", latitude:41, longitude:179}}}},
  {{id:"d", decoder:"aprs", ts:4, source_id:"src", frequency_hz:145000000, fields:{{from:"N0CALL", latitude:42, longitude:-179}}}},
], signals, [{{id:"c1", station:"Catalog base", latitude:1, longitude:2}}]);
if (rows[0].lat !== null || rows[0].signal !== null) throw new Error("unpositioned message was guessed or frequency-joined");
const signalRow = rows.find(r => r.kind === "station");
const catalogRow = rows.find(r => r.kind === "catalog");
if (signalRow.lat !== 40 || signalRow.lon !== -75 || signalRow.refId !== "s1") throw new Error("signal station coords missing");
if (catalogRow.lat !== 1 || catalogRow.lon !== 2 || catalogRow.refId !== "c1") throw new Error("catalog coords missing");
const segments = trackSegments(rows);
if (segments.length !== 1 || segments[0].length !== 2) throw new Error("dateline should split tracks");
if (segments[0][0].trackId !== "src:aprs:N0CALL") throw new Error("source/decoder/entity track namespace missing");
if (signalRow.tuneTarget !== "signal:s1" || catalogRow.tuneTarget !== "catalog:c1") throw new Error("tune target missing");
""")


def test_remote_id_rows_three_marker_kinds_and_only_valid_positions_mapped():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ remoteIdRows }} from {module};
const items = [
  {{
    key: "1122330000000000000000000000000000000000",
    uas_id_hex: "1122330000000000000000000000000000000000",
    uas_id_text: "SERIAL1",
    ua_type: 2,
    conflict: false,
    sources: ["wifi"],
    last_seen: 1000,
    drone_position: {{latitude_deg: 45.0, longitude_deg: -122.0, altitude_baro_m: 100.0,
      altitude_geo_m: 110.0, height_m: 50.0, speed_horizontal_ms: 5.0, speed_vertical_ms: 0.0,
      direction_deg: 90.0, horiz_accuracy_m: 3.0, vert_accuracy_m: 1.0, received_at: 1000, age_s: 0, stale: false}},
    operator_position: {{latitude_deg: 45.1, longitude_deg: -122.1, altitude_m: 60.0, received_at: 1000, age_s: 0, stale: false}},
    takeoff_position: null,
  }},
  {{
    key: "no-position",
    uas_id_hex: null, uas_id_text: null, ua_type: null, conflict: false, sources: ["ble"],
    last_seen: 999, drone_position: null, operator_position: null, takeoff_position: null,
  }},
];
const rows = remoteIdRows(items);
if (rows.length !== 2) throw new Error(`expected exactly 2 positioned rows (drone+operator), got $${{rows.length}}`);
const byKind = Object.fromEntries(rows.map(r => [r.posKind, r]));
if (!byKind.drone || !byKind.operator || byKind.takeoff) throw new Error("wrong set of marker kinds produced");
if (byKind.drone.lat !== 45.0 || byKind.drone.lon !== -122.0) throw new Error("drone position mismatch");
if (byKind.drone.uasIdText !== "SERIAL1" || byKind.drone.transports[0] !== "wifi") throw new Error("drone metadata missing");
if (byKind.drone.horizAccuracyM !== 3.0 || byKind.drone.headingDeg !== 90.0) throw new Error("drone accuracy/heading missing");
if (byKind.operator.altitude !== 60.0) throw new Error("operator altitude mismatch");
if (byKind.drone.trackId === byKind.operator.trackId) throw new Error("drone and operator must have independent trails");
if (!rows.every(r => r.event && r.positionTimeKnown === true)) throw new Error("remote id rows must count as live positioned reports");
""")


def test_remote_id_trail_breaks_on_stale_gap_not_joined_across():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ remoteIdRows, trackSegments }} from {module};
const fix = (lat, lon, ts) => ({{latitude_deg: lat, longitude_deg: lon, received_at: ts, age_s: 0, stale: false}});
const group = key => ({{
  key, uas_id_hex: null, uas_id_text: null, ua_type: null, conflict: false, sources: ["wifi"],
  last_seen: 0, operator_position: null, takeoff_position: null, drone_position: null,
}});
// Three reports of the same UAS, with a 200s gap (past the 60s staleness
// threshold) between the second and third -- the trail must break there,
// never drawing a straight line across the stale gap or a reconnect.
const rows = [0, 1, 2].flatMap(i => {{
  const g = group("aa");
  g.drone_position = fix(45.0 + i * 0.001, -122.0, i === 2 ? 300 : i * 10);
  return remoteIdRows([g]);
}});
const segments = trackSegments(rows, {{maxGapSeconds: 60}});
// The pre-gap pair forms one 2-point segment; the lone post-gap report is
// never joined to it by a line (trackSegments only emits 2+ point segments) --
// exactly "an explicit gap, never a straight line across it".
if (segments.length !== 1 || segments[0].length !== 2) throw new Error("stale gap must not be bridged by a drawn line");
if (segments[0][1].ts !== 10) throw new Error("wrong points kept in the pre-gap segment");
""")


def test_invalid_coordinate_types_and_adsb_identity():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows, trackSegments }} from {module};
const rows = mapRows([
  {{id:"bool", decoder:"adsb", ts:1, source_id:"live", fields:{{icao:"ABC123", latitude:true, longitude:2}}}},
  {{id:"space", decoder:"adsb", ts:2, source_id:"live", fields:{{icao:"ABC123", latitude:" 1 ", longitude:2}}}},
  {{id:"ok", decoder:"adsb", ts:3, source_id:"live", fields:{{icao:"ABC123", latitude:1, longitude:2}}}},
  {{id:"anon", decoder:"adsb", ts:4, fields:{{latitude:2, longitude:3}}}},
], [], []);
if (rows[0].lat !== null || rows[1].lat !== null) throw new Error("non-numeric coordinates accepted");
if (rows[0].trackId !== "live:adsb:ABC123" || rows[2].trackId !== "live:adsb:ABC123") throw new Error("ICAO identity ignored");
if (rows[3].trackId !== null || trackSegments(rows).length !== 0) throw new Error("anonymous event joined a track");
""")


def test_adsb_envelope_and_aprs_uncompressed_position():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows }} from {module};
const rows = mapRows([
  {{id:"report", decoder:"adsb", ts:100, source_id:"live:sdrconnect", fields:{{report:{{aircraftState:[
    {{icao:"ABC123", callsign:"TEST1", latitude:37.5, longitude:-122.25, altitude:10000}},
    {{icao:"DEF456", callsign:"TEST2"}},
  ]}}}}}},
  {{id:"aprs", decoder:"aprs", ts:101, source_id:"live:rx", text:"N0CALL>APRS,WIDE1-1:!4903.50N/07201.75W:hello:world", fields:{{from:"N0CALL"}}}},
  {{id:"compressed", decoder:"aprs", ts:102, text:"N0CALL>APRS:!compressed payload", fields:{{from:"N0CALL"}}}},
  {{id:"bad-minute", decoder:"aprs", ts:103, text:"N0CALL>APRS:!4960.00N/07201.75W", fields:{{from:"N0CALL"}}}},
], [], []);
const air = rows.filter(r => r.event.id === "report");
if (air.length !== 2 || air[0].trackId !== "live:sdrconnect:adsb:ABC123" || air[0].lat !== 37.5 || air[0].altitude !== 3048) throw new Error("ADS-B state not expanded correctly");
if (air[0].positionTimeKnown !== false || air[1].lat !== null) throw new Error("ADS-B position time/unknown position wrong");
const aprs = rows.find(r => r.id === "aprs");
if (Math.abs(aprs.lat - 49.0583333333) > 1e-8 || Math.abs(aprs.lon + 72.0291666667) > 1e-8) throw new Error("APRS uncompressed fix parsing failed");
if (rows.find(r => r.id === "compressed").lat !== null || rows.find(r => r.id === "bad-minute").lat !== null) throw new Error("unsafe APRS position accepted");
""")


def test_dsc_decoded_sender_and_position_map_without_guessing():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows }} from {module};
const rows = mapRows([
  {{id:'dsc', decoder:'dsc', source_id:'live:test', integrity:'passed',
    integrity_note:'DSC XOR ECC verified', fields:{{sender_mmsi:'123456789',latitude:37,longitude:-122}}}},
  {{id:'bad', decoder:'dsc', integrity:'failed', fields:{{latitude:37,longitude:-122}}}},
  {{id:'raw', decoder:'dsc', fields:{{symbols:[112,12,34]}}}}
]);
if (rows[0].label !== '123456789' || rows[0].trackId !== 'live:test:dsc:123456789') throw Error('sender missing');
if (rows[0].lat !== 37 || rows[0].layer !== 'maritime') throw Error('position missing');
if (!rows[0].evidence.includes('XOR ECC')) throw Error('integrity mislabeled');
if (rows[1].lat !== null || rows[2].lat !== null) throw Error('invented location');
""")


def test_vehicle_summary_metadata_is_per_entity_and_missing_values_are_null():
    module = json.dumps(MAP_JS.as_uri())
    _run(f"""
import {{ mapRows }} from {module};
const rows = mapRows([
  {{id:"air", decoder:"adsb", ts:200, fields:{{callsign:"ENVELOPE", icao:"ENVELOPE", registration:"N-ENV", report:{{aircraftState:[
    {{icao:"ABC123", callsign:"AIR1", registration:"N123AB", latitude:37, longitude:-122, positionDateTime:"2026-01-01T00:00:00Z", speed_knots:123.5}},
    {{hex:"DEF456", callsign:"AIR2", ground_speed_kt:88.2}},
  ]}}}}}},
  {{id:"ship", decoder:"ais", ts:300, fields:{{mmsi:123456789, sog_kn:12.3}}}},
  {{id:"missing", decoder:"ais", ts:301, fields:{{}}}},
  {{id:"bad-speed", decoder:"ais", ts:"302", fields:{{sog_kn:"8.2", speed_knots:true}}}},
]);
const [a, b] = rows.filter(r => r.event.id === "air");
if (a.callsign !== "AIR1" || a.icao !== "ABC123" || a.registration !== "N123AB" || a.speedKn !== 123.5) throw Error("expanded aircraft metadata missing");
if (a.decodedFields.callsign !== "AIR1" || a.decodedFields.aircraftState !== undefined || a.receivedAt !== 200 || a.ts === a.receivedAt) throw Error("aircraft fields or receive time incorrectly sourced");
if (b.callsign !== "AIR2" || b.icao !== "DEF456" || b.registration !== null || b.speedKn !== 88.2 || b.receivedAt !== 200) throw Error("second aircraft metadata or nulls incorrect");
const ship = rows.find(r => r.event.id === "ship");
if (ship.mmsi !== 123456789 || ship.speedKn !== 12.3 || ship.receivedAt !== 300) throw Error("AIS metadata missing");
const missing = rows.find(r => r.event.id === "missing");
if (missing.callsign !== null || missing.icao !== null || missing.registration !== null || missing.mmsi !== null || missing.speedKn !== null || missing.receivedAt !== 301) throw Error("missing metadata should be null");
const bad = rows.find(r => r.event.id === "bad-speed");
if (bad.speedKn !== null || bad.receivedAt !== null) throw Error("invalid numeric types accepted");
""")


def test_vehicle_lookup_links_use_valid_received_identifiers():
    module = json.dumps((MAP_JS.parent / 'map-vehicle-links.js').as_uri())
    _run(f"""
import {{ vehicleExternalLinks }} from {module};
const aircraft = vehicleExternalLinks({{layer:'aircraft',callsign:'UAL123',icao:'a0b1c2'}});
if (aircraft.length !== 2 || aircraft[0].name !== 'FlightAware' || aircraft[0].url !== 'https://www.flightaware.com/live/flight/UAL123' || aircraft[1].url !== 'https://www.flightradar24.com/data') throw Error('callsign lookup missing');
const registered = vehicleExternalLinks({{layer:'aircraft',registration:'N123AB',icao:'A05ED9'}});
if (registered.length !== 2 || registered[0].url !== 'https://www.flightaware.com/live/flight/N123AB') throw Error('registration lookup missing');
const hex = vehicleExternalLinks({{layer:'aircraft',icao:'A0B1C2'}});
if (hex.length !== 2 || !hex[0].url.endsWith('/a0b1c2/redirect')) throw Error('ICAO-only lookup missing');
const bad = vehicleExternalLinks({{layer:'aircraft',callsign:'https://evil.test',registration:'../bad',icao:'javascript:alert(1)'}});
if (bad.length || vehicleExternalLinks({{layer:'maritime',mmsi:'123'}}).length) throw Error('invalid lookup identifier accepted');
const boat = vehicleExternalLinks({{layer:'maritime',mmsi:123456789}});
if (boat.length !== 2 || boat[0].url !== 'https://www.vesselfinder.com/vessels/details/123456789' || boat[1].url !== 'https://www.marinetraffic.com/en/ais/home' || !boat[1].note.includes('123456789')) throw Error('vessel identifier missing');
""")
