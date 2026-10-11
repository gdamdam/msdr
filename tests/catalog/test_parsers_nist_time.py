from msdr.catalog.parsers.nist_time import parse_nist_time, snapshot_date


def _by_station(entries):
    out: dict[str, list] = {}
    for e in entries:
        out.setdefault(e.station_id, []).append(e)
    return out


def test_nist_snapshot_frequencies_per_station():
    by = _by_station(parse_nist_time())
    assert set(by) == {"WWV", "WWVH", "WWVB"}
    assert sorted(e.frequency_hz for e in by["WWV"]) == [
        2_500_000, 5_000_000, 10_000_000, 15_000_000, 20_000_000, 25_000_000
    ]
    assert sorted(e.frequency_hz for e in by["WWVH"]) == [
        2_500_000, 5_000_000, 10_000_000, 15_000_000
    ]
    assert {e.frequency_hz for e in by["WWVB"]} == {60_000}
    assert {e.component for e in by["WWVB"]} == {"60 kHz north array", "60 kHz south array"}


def test_nist_fields_and_statuses():
    entries = parse_nist_time()
    assert all(e.source == "nist_time" and e.ref_type == "station" for e in entries)
    assert all(e.network == "NIST" and e.operator == "NIST" and e.country == "USA" for e in entries)
    assert all(e.start_utc is None and e.stop_utc is None and e.days is None for e in entries)
    assert all(e.source_url.startswith("https://www.nist.gov/") for e in entries)
    assert all(e.edition == snapshot_date() == e.status_at for e in entries)
    by = _by_station(entries)
    wwv25 = next(e for e in by["WWV"] if e.component == "25 MHz")
    assert wwv25.status == "experimental"
    assert all(e.status == "operational" for e in entries if e is not wwv25)
    assert all(e.mode == "AM" and e.decoder == "wwv" for e in by["WWV"] + by["WWVH"])
    assert all(e.mode is None and e.decoder is None for e in by["WWVB"])


def test_nist_powers_and_coordinates():
    by = _by_station(parse_nist_time())
    wwv = {e.component: e for e in by["WWV"]}
    assert wwv["2.5 MHz"].power_kw == 2.5 and wwv["20 MHz"].power_kw == 2.5
    assert wwv["5 MHz"].power_kw == 10 and wwv["15 MHz"].power_kw == 10
    assert {e.component: e.power_kw for e in by["WWVH"]}["2.5 MHz"] == 5
    # Each WWV frequency has its own antenna; 2.5 MHz is the verified DMS value.
    assert wwv["2.5 MHz"].latitude == 40.682 and wwv["2.5 MHz"].longitude == -105.042028
    assert len({(e.latitude, e.longitude) for e in by["WWV"]}) == 6
    north = next(e for e in by["WWVB"] if "north" in e.component)
    # WWVH antenna table, read from the complete NIST page on 2026-10-02.
    wwvh = {e.component: e for e in by["WWVH"]}
    assert (wwvh["2.5 MHz"].latitude, wwvh["2.5 MHz"].longitude) == (21.989139, -159.764556)
    assert len({(e.latitude, e.longitude) for e in by["WWVH"]}) == 4
    assert all(e.latitude is not None for e in by["WWVH"])
    assert (north.latitude, north.longitude) == (40.680917, -105.05)


def test_nist_ids_are_stable_across_parses():
    first = {e.id: e.component for e in parse_nist_time()}
    second = {e.id: e.component for e in parse_nist_time()}
    assert first == second and len(first) == 12
