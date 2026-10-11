"""Reference catalogs (docs/reference-catalogs.md) through the API: search
filters, on-air state words, tune gating for untunable rows, distance sort,
source kinds/coverage, refresh idempotency and LISTEN preset gating."""

import io
import zipfile

import msdr.api.services.catalog as catalog_module
import msdr.catalog.parsers.faa_nasr as faa_module
import msdr.catalog.parsers.ioos_hfradar as ioos_module
from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers.nist_time import parse_nist_time
from msdr.catalog.parsers.superdarn import parse_superdarn
from msdr.settings import Location
from tests.api.test_app import running_app

_FORT_COLLINS = Location(lat=40.68, lon=-105.04, altitude_m=1500.0, timezone="America/Denver")


def _schedule(**overrides) -> CatalogEntry:
    base = dict(source="eibi", frequency_hz=6070_000, station="Deutschlandfunk")
    base.update(overrides)
    return CatalogEntry(**base)


def _navaid(**overrides) -> CatalogEntry:
    base = dict(
        source="faa_nasr",
        ref_type="navaid",
        network="FAA NASR",
        station_id="SFO",
        component="VOR/DME",
        station="SAN FRANCISCO VOR/DME",
        frequency_hz=115_800_000,
        status="operational",
        latitude=37.6194,
        longitude=-122.3738,
        decoder="vor",
    )
    base.update(overrides)
    return CatalogEntry(**base)


def _dme_no_frequency() -> CatalogEntry:
    # DME/TACAN carry a channel, not a VHF frequency: published active, untunable.
    return _navaid(component="DME", station="SAN FRANCISCO DME", frequency_hz=None, decoder=None)


def _seed_reference(app) -> dict[str, list[CatalogEntry]]:
    catalog = app.state.service.catalog
    seeded = {
        "nist_time": parse_nist_time(),
        "superdarn": parse_superdarn(),
        "faa_nasr": [_navaid(), _dme_no_frequency()],
        "eibi": [_schedule()],
    }
    for source, entries in seeded.items():
        catalog.replace_source(source, entries)
    return seeded


async def test_search_filters_by_network_station_id_and_ref_type():
    async with running_app() as (app, client, adapter):
        _seed_reference(app)

        resp = await client.get("/api/catalog/search", params={"network": "NIST"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 12
        assert {e["network"] for e in resp.json()["entries"]} == {"NIST"}

        resp = await client.get("/api/catalog/search", params={"station_id": "WWVH"})
        assert resp.json()["count"] == 4
        assert {e["station_id"] for e in resp.json()["entries"]} == {"WWVH"}

        resp = await client.get("/api/catalog/search", params={"ref_type": "navaid"})
        assert resp.json()["count"] == 2
        assert {e["ref_type"] for e in resp.json()["entries"]} == {"navaid"}

        # Blank values are "no filter", like the older source/languages params.
        resp = await client.get(
            "/api/catalog/search", params={"network": "", "station_id": " ", "ref_type": ""}
        )
        assert resp.json()["count"] == 12 + 49 + 2 + 1


async def test_search_entries_carry_on_air_state_tunable_and_null_frequency():
    async with running_app() as (app, client, adapter):
        _seed_reference(app)
        resp = await client.get("/api/catalog/search", params={"limit": 500})
        assert resp.status_code == 200
        rows = resp.json()["entries"]
        by_station = {e["station"]: e for e in rows}

        wwv = by_station["WWV 5 MHz"]
        assert wwv["on_air_state"] == "continuous"
        assert wwv["tunable"] is True and wwv["frequency_hz"] == 5_000_000
        assert wwv["distance_km"] is None

        vor = by_station["SAN FRANCISCO VOR/DME"]
        assert vor["on_air_state"] == "published_active" and vor["tunable"] is True

        dme = by_station["SAN FRANCISCO DME"]
        assert dme["on_air_state"] == "published_active"
        assert dme["tunable"] is False and dme["frequency_hz"] is None

        radar = next(e for e in rows if e["ref_type"] == "radar_site")
        assert radar["on_air_state"] == "unknown"
        assert radar["tunable"] is False and radar["frequency_hz"] is None

        schedule = by_station["Deutschlandfunk"]
        assert schedule["on_air_state"] == "scheduled_on"  # no window = always
        assert schedule["tunable"] is True

        # The on-air flag ranks reference rows by their published state, so
        # a radar site never comes out as airing.
        resp = await client.get(
            "/api/catalog/search", params={"on_air": True, "ref_type": "radar_site", "limit": 5}
        )
        assert {e["on_air_state"] for e in resp.json()["entries"]} == {"unknown"}


async def test_tune_and_save_refuse_entry_without_exact_frequency():
    async with running_app() as (app, client, adapter):
        dme = _dme_no_frequency()
        app.state.service.catalog.replace_source("faa_nasr", [_navaid(), dme])

        resp = await client.post(f"/api/tune/catalog/{dme.id}")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "SAN FRANCISCO DME has no verified exact frequency"

        resp = await client.post(f"/api/catalog/{dme.id}/save")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "SAN FRANCISCO DME has no verified exact frequency"

        # The tunable sibling still works.
        vor = _navaid()
        resp = await client.post(f"/api/catalog/{vor.id}/save")
        assert resp.status_code == 200
        assert resp.json()["frequency_hz"] == 115_800_000


async def test_sort_distance_orders_by_receiver_location_and_sets_distance_km(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        catalog.replace_source(
            "test",
            [
                _navaid(station_id="FAR", station="far", latitude=21.0, longitude=-157.9),
                _navaid(station_id="NOCOORD", station="nowhere", latitude=None, longitude=None),
                _navaid(station_id="NEAR", station="near", latitude=40.7, longitude=-105.0),
                _navaid(station_id="MID", station="mid", latitude=37.6, longitude=-122.4),
            ],
        )
        monkeypatch.setattr(app.state.service, "receiver_location", lambda: _FORT_COLLINS)

        resp = await client.get("/api/catalog/search", params={"sort": "distance"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sort"] == "distance" and body["count"] == 4
        assert [e["station"] for e in body["entries"]] == ["near", "mid", "far", "nowhere"]
        distances = [e["distance_km"] for e in body["entries"]]
        assert distances[-1] is None
        assert distances[0] < 10
        assert 1400 < distances[1] < 1600
        assert distances[2] > 5000

        # Pagination applies after the sort.
        resp = await client.get(
            "/api/catalog/search", params={"sort": "distance", "limit": 2, "offset": 1}
        )
        assert [e["station"] for e in resp.json()["entries"]] == ["mid", "far"]
        assert resp.json()["count"] == 4


async def test_sort_distance_without_receiver_location_is_400(monkeypatch):
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source("test", [_navaid()])
        monkeypatch.setattr(app.state.service, "receiver_location", lambda: None)
        resp = await client.get("/api/catalog/search", params={"sort": "distance"})
        assert resp.status_code == 400
        assert resp.json() == {
            "detail": "No receiver location configured; set it in Settings → Location"
        }
        # The default sort is unaffected by the missing location.
        resp = await client.get("/api/catalog/search")
        assert resp.status_code == 200
        assert resp.json()["entries"][0]["distance_km"] is None


async def test_sort_frequency_puts_null_frequencies_last_and_rejects_unknown_sort():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "test",
            [
                _navaid(station_id="B", station="b", frequency_hz=116_800_000),
                _dme_no_frequency(),
                _navaid(station_id="A", station="a", frequency_hz=108_900_000),
            ],
        )
        resp = await client.get("/api/catalog/search", params={"sort": "frequency"})
        assert resp.status_code == 200
        assert [e["frequency_hz"] for e in resp.json()["entries"]] == [
            108_900_000,
            116_800_000,
            None,
        ]
        resp = await client.get("/api/catalog/search", params={"sort": "nearest"})
        assert resp.status_code == 422


async def test_sort_time_and_name():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi",
            [
                _schedule(station="Radio Romania", start_utc="2300", stop_utc="0100"),
                _schedule(station="china radio", start_utc="0100", stop_utc="0200"),
                _schedule(station="Always On"),
            ],
        )
        resp = await client.get("/api/catalog/search", params={"sort": "time"})
        assert [e["station"] for e in resp.json()["entries"]] == [
            "china radio", "Radio Romania", "Always On",
        ]
        resp = await client.get("/api/catalog/search", params={"sort": "name"})
        assert [e["station"] for e in resp.json()["entries"]] == [
            "Always On", "china radio", "Radio Romania",
        ]


async def test_on_air_ranks_first_under_name_and_distance_sorts(monkeypatch):
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi",
            [
                _schedule(station="A off", start_utc="0000", stop_utc="0000", days=".......",
                          latitude=40.7, longitude=-105.0),
                _schedule(station="B on far", latitude=-33.9, longitude=151.2),
                _schedule(station="C on near", latitude=40.6, longitude=-105.1),
            ],
        )
        monkeypatch.setattr(app.state.service, "receiver_location", lambda: _FORT_COLLINS)
        for sort, expected in (
            ("name", ["B on far", "C on near", "A off"]),
            ("distance", ["C on near", "B on far", "A off"]),
        ):
            resp = await client.get("/api/catalog/search", params={"sort": sort, "on_air": 1})
            assert [e["station"] for e in resp.json()["entries"]] == expected
        resp = await client.get("/api/catalog/search", params={"sort": "distance"})
        assert resp.json()["entries"][0]["station"] == "A off"


async def test_sources_rows_carry_kind_and_coverage():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/catalog/sources")
        assert resp.status_code == 200
        by_source = {row["source"]: row for row in resp.json()["available"]}

        nist = by_source["nist_time"]
        assert nist["group"] == "catalog" and nist["kind"] == "static" and nist["static"] is True
        assert nist["coverage"] == "WWV, WWVH, WWVB (12 records, snapshot 2026-10-01)"
        superdarn = by_source["superdarn"]
        assert superdarn["kind"] == "static"
        assert superdarn["coverage"].startswith("49 radars")
        assert by_source["eibi"]["kind"] == "refreshable" and by_source["eibi"]["coverage"]
        for key in ("faa_nasr", "ioos_hfradar"):
            if key in by_source:  # registered by the parser modules when present
                assert by_source[key]["kind"] == catalog_module.CATALOG_KINDS[key]
                assert by_source[key]["coverage"] == catalog_module.CATALOG_COVERAGE[key]
        assert by_source["ntia_table"]["group"] == "allocation"
        assert "kind" in by_source["ntia_table"] and "coverage" in by_source["ntia_table"]


async def test_refresh_static_reference_source_is_idempotent_and_isolated(monkeypatch):
    def _no_fetch(*args, **kwargs):
        raise AssertionError("bundled reference sources must not fetch")

    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", _no_fetch)
        catalog = app.state.service.catalog
        catalog.replace_source("eibi", [_schedule()])
        catalog.replace_source("superdarn", parse_superdarn())

        first = await client.post("/api/catalog/refresh", json={"source": "nist_time"})
        assert first.status_code == 200
        assert first.json() == {"source": "nist_time", "entries": 12, "error": None}
        ids_first = {e.id for e in catalog.search("", source="nist_time", limit=100)}

        second = await client.post("/api/catalog/refresh", json={"source": "nist_time"})
        assert second.json()["entries"] == 12
        ids_second = {e.id for e in catalog.search("", source="nist_time", limit=100)}
        assert ids_first == ids_second and len(ids_first) == 12
        assert catalog.count_matches("", source="nist_time") == 12

        # Other sources are untouched by a reference refresh.
        assert catalog.count_matches("", source="eibi") == 1
        assert catalog.count_matches("", source="superdarn") == 49


async def test_refresh_error_leaves_previous_rows_in_place(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        catalog.replace_source("nist_time", parse_nist_time())
        before = sorted(e.id for e in catalog.search("", source="nist_time", limit=100))

        def _broken(path=None):
            raise ValueError("snapshot unreadable")

        monkeypatch.setitem(catalog_module.ALL_CATALOG_PARSERS, "nist_time", _broken)
        resp = await client.post("/api/catalog/refresh", json={"source": "nist_time"})
        assert resp.status_code == 200
        assert resp.json() == {"source": "nist_time", "entries": 0, "error": "snapshot unreadable"}
        after = sorted(e.id for e in catalog.search("", source="nist_time", limit=100))
        assert after == before


async def test_refresh_faa_nasr_uses_its_fetcher_and_reports_download_errors(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        rows = [_navaid(), _dme_no_frequency()]
        monkeypatch.setitem(
            catalog_module._CATALOG_FETCHERS, "faa_nasr", lambda: (rows, "https://nfdc.faa.gov/x")
        )
        resp = await client.post("/api/catalog/refresh", json={"source": "faa_nasr"})
        assert resp.status_code == 200
        assert resp.json()["entries"] == 2 and resp.json()["error"] is None
        assert catalog.count_matches("", source="faa_nasr") == 2

        def _offline():
            raise OSError("network unreachable")

        monkeypatch.setitem(catalog_module._CATALOG_FETCHERS, "faa_nasr", _offline)
        resp = await client.post("/api/catalog/refresh", json={"source": "faa_nasr"})
        assert resp.status_code == 200
        assert resp.json()["entries"] == 0 and "network unreachable" in resp.json()["error"]
        # The previous cycle's rows survive a failed download.
        assert catalog.count_matches("", source="faa_nasr") == 2


async def test_listen_presets_skip_rows_without_frequency():
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        catalog.replace_source("nist_time", parse_nist_time())
        catalog.replace_source("superdarn", parse_superdarn())
        catalog.replace_source("faa_nasr", [_navaid(), _dme_no_frequency()])

        for preset in ("on_air_my_languages", "unheard", "decodable", "starting_soon"):
            resp = await client.get("/api/listen", params={"preset": preset, "limit": 100})
            assert resp.status_code == 200, preset
            items = resp.json()["items"]
            assert all(item["frequency_hz"] is not None for item in items), preset
            assert "SAN FRANCISCO DME" not in {item["title"] for item in items}, preset
            assert not any(item["title"].startswith("SuperDARN") for item in items), preset

        titles = {
            i["title"]
            for i in (
                await client.get("/api/listen", params={"preset": "unheard", "limit": 100})
            ).json()["items"]
        }
        assert "WWV 5 MHz" in titles and "SAN FRANCISCO VOR/DME" in titles


async def test_timestamped_current_frequency_round_trips_through_api():
    async with running_app() as (app, client, adapter):
        radar = CatalogEntry(
            source="test_radar", ref_type="radar_site", network="Test", station_id="r1",
            station="Synthetic radar", current_freq_hz=12_345_000,
            current_freq_at="2026-10-01T12:00:00Z", freq_lo_hz=8_000_000, freq_hi_hz=22_000_000,
        )
        app.state.service.catalog.replace_source("test_radar", [radar])
        resp = await client.get("/api/catalog/search", params={"station_id": "r1"})
        assert resp.status_code == 200
        (row,) = resp.json()["entries"]
        assert row["id"] == radar.id
        assert (row["current_freq_hz"], row["current_freq_at"]) == (
            12_345_000, "2026-10-01T12:00:00Z"
        )
        assert (row["freq_lo_hz"], row["freq_hi_hz"]) == (8_000_000, 22_000_000)
        # A current reading is not a verified fixed frequency: untunable, unknown.
        assert row["frequency_hz"] is None and row["tunable"] is False
        assert row["on_air_state"] == "unknown"
        resp = await client.post(f"/api/tune/catalog/{radar.id}")
        assert resp.status_code == 409


async def test_sort_distance_pages_are_stable_and_non_overlapping(monkeypatch):
    async with running_app() as (app, client, adapter):
        rows = [
            _navaid(station_id=f"S{i}", station=f"s{i}", latitude=40.0 + i * 0.7, longitude=-105.0)
            for i in range(7)
        ] + [_navaid(station_id="NC", station="nocoord", latitude=None, longitude=None)]
        app.state.service.catalog.replace_source("test", rows)
        monkeypatch.setattr(app.state.service, "receiver_location", lambda: _FORT_COLLINS)

        async def page(offset):
            resp = await client.get(
                "/api/catalog/search", params={"sort": "distance", "limit": 3, "offset": offset}
            )
            assert resp.status_code == 200 and resp.json()["count"] == 8
            return [e["station"] for e in resp.json()["entries"]]

        pages = [await page(0), await page(3), await page(6)]
        assert [len(p) for p in pages] == [3, 3, 2]
        seen = [s for p in pages for s in p]
        assert len(set(seen)) == 8  # no overlap, nothing dropped
        assert seen == ["s1", "s0", "s2", "s3", "s4", "s5", "s6", "nocoord"]
        assert [await page(0), await page(3), await page(6)] == pages  # stable on repeat


def _truncated_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("NAV_BASE.csv", "EFF_DATE,NAV_ID\n2026/10/01,SFO\n")
    return buf.getvalue()[: len(buf.getvalue()) // 2]


async def test_refresh_faa_nasr_truncated_zip_reports_error_and_keeps_rows(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        catalog.replace_source("faa_nasr", [_navaid(), _dme_no_frequency()])
        before = sorted(e.id for e in catalog.search("", source="faa_nasr", limit=10))
        monkeypatch.setattr(faa_module, "_download", lambda url: _truncated_zip())

        resp = await client.post("/api/catalog/refresh", json={"source": "faa_nasr"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == 0 and body["error"]
        assert "zip" in body["error"].lower()
        assert sorted(e.id for e in catalog.search("", source="faa_nasr", limit=10)) == before


async def test_refresh_ioos_truncated_csv_reports_error_and_keeps_rows(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        good = ioos_module.parse_ioos_hfradar()
        catalog.replace_source("ioos_hfradar", good)
        assert catalog.count_matches("", source="ioos_hfradar") == 184
        # A download cut off inside the header parses to nothing.
        monkeypatch.setattr(ioos_module, "_download", lambda url: b'Station,"Station Na')

        resp = await client.post("/api/catalog/refresh", json={"source": "ioos_hfradar"})
        assert resp.status_code == 200
        assert resp.json() == {
            "source": "ioos_hfradar",
            "entries": 0,
            "error": "no entries parsed; catalog unchanged",
        }
        assert catalog.count_matches("", source="ioos_hfradar") == 184
        assert {e.id for e in catalog.search("", source="ioos_hfradar", limit=200)} == {
            e.id for e in good
        }


async def test_refresh_ioos_first_load_offline_falls_back_to_bundled_snapshot(monkeypatch):
    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog

        def _offline(url):
            raise OSError("network unreachable")

        monkeypatch.setattr(ioos_module, "_download", _offline)
        resp = await client.post("/api/catalog/refresh", json={"source": "ioos_hfradar"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == 184 and body["error"] is None
        assert "loaded bundled snapshot" in body["note"]
        assert catalog.count_matches("", source="ioos_hfradar") == 184
        angl = catalog.search("", source="ioos_hfradar", station_id="ANGL", limit=5)
        assert [e.frequency_hz for e in angl] == [41_340_000]
        # With rows in place, a later failed download is an error, not a reload.
        resp = await client.post("/api/catalog/refresh", json={"source": "ioos_hfradar"})
        assert resp.json()["entries"] == 0 and "network unreachable" in resp.json()["error"]
        assert catalog.count_matches("", source="ioos_hfradar") == 184


async def test_refresh_refuses_to_shrink_a_source_by_more_than_half(monkeypatch):
    def _rows(n, source="faa_nasr"):
        return [_navaid(source=source, station_id=f"N{i}", station=f"n{i}") for i in range(n)]

    async with running_app() as (app, client, adapter):
        catalog = app.state.service.catalog
        catalog.replace_source("faa_nasr", _rows(100))
        before = {e.id for e in catalog.search("", source="faa_nasr", limit=200)}

        # Parsed fine, but 30 of 100 rows: most likely a truncated download.
        monkeypatch.setitem(
            catalog_module._CATALOG_FETCHERS, "faa_nasr", lambda: (_rows(30), "https://x")
        )
        resp = await client.post("/api/catalog/refresh", json={"source": "faa_nasr"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == 0 and body["kept_previous"] is True
        assert body["error"] == (
            "refresh produced 30 rows, previous dataset has 100; "
            "refusing to shrink by more than half (download may be truncated)"
        )
        assert {e.id for e in catalog.search("", source="faa_nasr", limit=200)} == before

        # 60 of 100 is within the allowance and replaces.
        monkeypatch.setitem(
            catalog_module._CATALOG_FETCHERS, "faa_nasr", lambda: (_rows(60), "https://x")
        )
        resp = await client.post("/api/catalog/refresh", json={"source": "faa_nasr"})
        assert resp.json() == {"source": "faa_nasr", "entries": 60, "error": None}
        assert catalog.count_matches("", source="faa_nasr") == 60

        # The same guard covers the static path (nist_time is 12 rows: a
        # source that small never trips it, so seed it larger first).
        catalog.replace_source("nist_time", _rows(40, "nist_time"))
        resp = await client.post("/api/catalog/refresh", json={"source": "nist_time"})
        assert resp.json()["kept_previous"] is True and "12 rows" in resp.json()["error"]
        assert catalog.count_matches("", source="nist_time") == 40
        # Below 20 existing rows the ratio is ignored.
        catalog.replace_source("nist_time", _rows(19, "nist_time"))
        resp = await client.post("/api/catalog/refresh", json={"source": "nist_time"})
        assert resp.json() == {"source": "nist_time", "entries": 12, "error": None}
