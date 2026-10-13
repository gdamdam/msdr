"""Catalog API wiring: search/near/sources/refresh/import/tune routes, plus
the catalog's integration into IDENT and the command palette."""

import datetime as dt

import msdr.api.service as service_module
import msdr.api.services.catalog as catalog_module
from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers.eibi import parse_eibi, season_code
from tests.api.test_app import running_app

EIBI_FIXTURE = """\
kHz;Time (UTC);Days;ITU;Station;Lng;Target;Remarks;P;Start;Stop
6070;2200-0600;;D;Deutschlandfunk;G;Europe;test fixture;100;;
9410;1000-1057;Mo-Fr;CHN;China Radio Intl;C;Asia;test fixture;;;;
6000;;;C;Radio Habana Cuba;S;Am;test fixture, always on;;;;
"""


def _entry(**overrides) -> CatalogEntry:
    base = dict(source="eibi", frequency_hz=6070_000, station="Deutschlandfunk")
    base.update(overrides)
    return CatalogEntry(**base)


async def test_catalog_search_seeded_from_eibi_parser():
    async with running_app() as (app, client, adapter):
        entries = parse_eibi(EIBI_FIXTURE, filename="sked-a26.csv")
        app.state.service.catalog.replace_source("eibi", entries, url="http://x")

        resp = await client.get("/api/catalog/search", params={"q": "Habana"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert "Habana" in body["entries"][0]["station"]


async def test_catalog_search_on_air_ranks_first():
    async with running_app() as (app, client, adapter):
        now = dt.datetime.now(dt.UTC)
        far = now + dt.timedelta(hours=12)  # guaranteed not "now"
        off_start, off_stop = far.strftime("%H%M"), (far + dt.timedelta(minutes=1)).strftime("%H%M")
        app.state.service.catalog.replace_source(
            "eibi",
            [
                _entry(station="off-air-station", start_utc=off_start, stop_utc=off_stop),
                _entry(station="on-air-station", start_utc=None, stop_utc=None),
            ],
        )

        resp = await client.get("/api/catalog/search", params={"q": "", "on_air": True})
        assert resp.status_code == 200
        stations = [e["station"] for e in resp.json()["entries"]]
        assert stations[0] == "on-air-station"


async def test_catalog_search_filters_multiple_sources_and_pages_consistently():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi",
            [
                _entry(station="EiBi English", language="English"),
                _entry(station="EiBi French", language="French"),
            ],
        )
        app.state.service.catalog.replace_source(
            "aoki", [_entry(source="aoki", station="Aoki English", language="English")]
        )
        app.state.service.catalog.replace_source(
            "ndxc", [_entry(source="ndxc", station="NDXC English", language="English")]
        )

        params = [
            ("sources", "eibi"),
            ("sources", "aoki"),
            ("languages", "English"),
            ("limit", "1"),
        ]
        first = await client.get("/api/catalog/search", params=params)
        assert first.status_code == 200
        assert first.json()["count"] == 2
        assert first.json()["offset"] == 0
        assert len(first.json()["entries"]) == 1

        second = await client.get("/api/catalog/search", params=params + [("offset", "1")])
        assert second.status_code == 200
        assert second.json()["count"] == 2
        assert second.json()["offset"] == 1
        assert len(second.json()["entries"]) == 1
        assert {first.json()["entries"][0]["source"], second.json()["entries"][0]["source"]} == {
            "eibi", "aoki"
        }


async def test_catalog_search_ignores_blank_source_and_language_parameters():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi", [_entry(station="EiBi row", language="English")]
        )
        app.state.service.catalog.replace_source(
            "aoki", [_entry(source="aoki", station="Aoki row", language="Japanese")]
        )
        resp = await client.get(
            "/api/catalog/search",
            params=[("sources", ""), ("languages", ""), ("source", "")],
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        assert {entry["station"] for entry in resp.json()["entries"]} == {"EiBi row", "Aoki row"}


async def test_catalog_near_respects_tolerance():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi",
            [
                _entry(frequency_hz=6070_000, station="close"),
                _entry(frequency_hz=6200_000, station="far"),
            ],
        )
        resp = await client.get("/api/catalog/near", params={"hz": 6070_000, "tol": 5000})
        assert resp.status_code == 200
        assert [e["station"] for e in resp.json()["entries"]] == ["close"]


async def test_catalog_sources_reports_season_and_stale_flag():
    async with running_app() as (app, client, adapter):
        current_season = season_code(dt.datetime.now(dt.UTC).date())
        app.state.service.catalog.replace_source(
            "eibi", [_entry(season="B24")], url="http://x"
        )
        app.state.service.catalog.replace_source(
            "aoki", [_entry(source="aoki", season=current_season)], url="http://y"
        )

        resp = await client.get("/api/catalog/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["season"] == current_season
        by_source = {s["source"]: s for s in body["sources"]}
        assert by_source["eibi"]["stale"] is True
        assert by_source["aoki"]["stale"] is False


async def test_catalog_sources_lists_imported_languages():
    async with running_app() as (app, client, adapter):
        app.state.service.catalog.replace_source(
            "eibi",
            [_entry(language="English"), _entry(station="French station", language="French")],
        )
        response = await client.get("/api/catalog/sources")
        assert response.status_code == 200
        assert response.json()["languages"] == [
            {"language": "English", "language_filter": "identity:english", "entry_count": 1},
            {"language": "French", "language_filter": "identity:french", "entry_count": 1},
        ]


async def test_catalog_sources_available_lists_every_known_source_with_static_flags():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/catalog/sources")
        assert resp.status_code == 200
        available = resp.json()["available"]
        assert len(available) >= 8
        by_source = {a["source"]: a for a in available}
        for key in (
            "eibi", "aoki", "hfcc", "ndxc",
            "noaa_radiofax", "sigidwiki", "priyom", "satnogs",
        ):
            assert key in by_source, key
            entry = by_source[key]
            # "group" distinguishes station catalogs from the allocation sources
            # that the same endpoint now lists (docs/allocations.md); "kind" is
            # the refresh model and "coverage" a one-line summary
            # (docs/reference-catalogs.md).
            assert set(entry) == {
                "source", "label", "description", "has_default_url", "static", "configured_url",
                "group", "kind", "coverage",
            }
            assert entry["group"] == "catalog"
            assert entry["kind"] in ("static", "refreshable", "live")
        # Static (bundled, no fetch) sources vs. ones that need a URL.
        assert by_source["noaa_radiofax"]["static"] is True
        assert by_source["sigidwiki"]["static"] is True
        assert by_source["eibi"]["static"] is False
        assert by_source["eibi"]["has_default_url"] is True
        assert by_source["aoki"]["has_default_url"] is False


async def test_catalog_refresh_uses_monkeypatched_fetch(monkeypatch):
    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(
            catalog_module, "fetch_catalog_text", lambda url, encoding="utf-8": EIBI_FIXTURE
        )
        resp = await client.post("/api/catalog/refresh", json={"source": "eibi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "eibi"
        assert body["entries"] == 3
        assert body["error"] is None

        search = (await client.get("/api/catalog/search", params={"q": ""})).json()
        assert search["count"] == 3


async def test_priyom_refresh_uses_official_json_api_day_windows(monkeypatch):
    urls = []
    today = dt.datetime.now(dt.UTC).date().isoformat()
    payload = (
        f'{{"items":[{{"summary":"E11 9130 kHz USB test",'
        f' "start":{{"dateTime":"{today}T09:00:00Z"}}}}]}}'
    )

    def fake_fetch(url, encoding="utf-8"):
        urls.append(url)
        return payload

    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", fake_fetch)
        resp = await client.post("/api/catalog/refresh", json={"source": "priyom"})
        assert resp.status_code == 200
        assert resp.json() == {"source": "priyom", "entries": 1, "error": None}
        assert len(urls) == 7
        assert all(url.startswith("https://calendar.priyom.org/events?timeMin=") for url in urls)
        stored = app.state.service.catalog.search(q="E11", source="priyom", limit=10)
        assert stored[0].date == today


async def test_priyom_empty_api_result_keeps_existing_catalog(monkeypatch):
    async with running_app() as (app, client, adapter):
        existing = CatalogEntry(source="priyom", frequency_hz=9_130_000, station="E11")
        app.state.service.catalog.replace_source("priyom", [existing], url="old")
        monkeypatch.setattr(
            catalog_module, "fetch_catalog_text",
            lambda url, encoding="utf-8": '{"items":[]}',
        )
        resp = await client.post("/api/catalog/refresh", json={"source": "priyom"})
        assert resp.json()["error"] == "no entries parsed; catalog unchanged"
        assert app.state.service.catalog.search(q="E11", source="priyom", limit=10)


async def test_catalog_refresh_reports_fetch_error_without_raising(monkeypatch):
    async with running_app() as (app, client, adapter):
        def _boom(url, encoding="utf-8"):
            raise OSError("network unreachable")

        monkeypatch.setattr(catalog_module, "fetch_catalog_text", _boom)
        resp = await client.post("/api/catalog/refresh", json={"source": "eibi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == 0
        assert "network unreachable" in body["error"]


async def test_catalog_import_sdrpp_json(tmp_path):
    async with running_app() as (app, client, adapter):
        doc = {
            "bookmarks": {
                "Local repeater": {"frequency": 146_940_000, "bandwidth": 12_500, "mode": 0}
            }
        }
        path = tmp_path / "my_list.json"
        path.write_text(__import__("json").dumps(doc))

        resp = await client.post(
            "/api/catalog/import", json={"path": str(path), "format": "sdrpp"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == 1
        assert body["source"] == "user:my_list.json"

        search = (
            await client.get(
                "/api/catalog/search", params={"q": "", "source": "user:my_list.json"}
            )
        ).json()
        assert search["entries"][0]["station"] == "Local repeater"


async def test_catalog_import_rejects_oversized_file(tmp_path, monkeypatch):
    # The importer reads the whole file into memory, so the cap is what keeps
    # a stray recording from taking the sidecar down. Patched small: writing
    # a real 20 MB file would only slow the suite down.
    monkeypatch.setattr(catalog_module, "_MAX_IMPORT_BYTES", 16)
    async with running_app() as (app, client, adapter):
        path = tmp_path / "big.json"
        path.write_text("x" * 64)

        resp = await client.post(
            "/api/catalog/import", json={"path": str(path), "format": "sdrpp"}
        )

        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"]


async def test_catalog_import_rejects_directory(tmp_path):
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/catalog/import", json={"path": str(tmp_path), "format": "sdrpp"}
        )
        assert resp.status_code == 400


async def test_tune_catalog_restores_frequency_and_am():
    async with running_app() as (app, client, adapter):
        entry = _entry(frequency_hz=15_120_000, station="Voice Of Somewhere", mode=None)
        app.state.service.catalog.replace_source("eibi", [entry])

        resp = await client.post(f"/api/tune/catalog/{entry.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

        state = (await client.get("/api/state")).json()
        assert state["props"]["device_vfo_frequency"] == 15_120_000
        assert state["props"]["demodulator"] == "AM"

        journal = (await client.get("/api/journal")).json()
        entry_row = next(e for e in journal if e["kind"] == "tune")
        assert entry_row["note"] == "Voice Of Somewhere"


async def test_tune_catalog_unknown_id_returns_404():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/tune/catalog/does-not-exist")
        assert resp.status_code == 404


async def test_save_catalog_entry_preserves_usb_mode_and_bandwidth():
    # Regression: the old client-side ★ path hardcoded AM/9000 regardless of
    # the entry's actual mode -- this must save what tuning it would set.
    async with running_app() as (app, client, adapter):
        entry = _entry(
            frequency_hz=14_346_000,
            station="Utility USB Station",
            mode="USB",
            bandwidth_hz=2700,
            language="E",
            target="NAm",
            start_utc="1200",
            stop_utc="1400",
            latitude=37.7749,
            longitude=-122.4194,
        )
        app.state.service.catalog.replace_source("eibi", [entry])

        resp = await client.post(f"/api/catalog/{entry.id}/save")
        assert resp.status_code == 200
        signal = resp.json()
        assert signal["frequency_hz"] == 14_346_000
        assert signal["demodulator"] == "USB"
        assert signal["filter_bandwidth_hz"] == 2700
        assert signal["status"] == "identified"
        assert signal["station"] == "Utility USB Station"
        assert (signal["latitude"], signal["longitude"]) == (37.7749, -122.4194)
        assert signal["notes"] == "eibi 1200-1400 E → NAm"

        # status="identified" signals show up in RECENT, not INBOX (that's
        # unknown-status only) -- see SignalRepo.inbox()/recent().
        recent = await client.get("/api/signals/recent")
        assert any(s["id"] == signal["id"] for s in recent.json())


async def test_save_catalog_entry_defaults_missing_mode_to_am_9khz():
    async with running_app() as (app, client, adapter):
        entry = _entry(frequency_hz=6_070_000, station="Plain Broadcast", mode=None)
        app.state.service.catalog.replace_source("eibi", [entry])

        resp = await client.post(f"/api/catalog/{entry.id}/save")
        assert resp.status_code == 200
        signal = resp.json()
        assert signal["demodulator"] == "AM"
        assert signal["filter_bandwidth_hz"] == 9000


async def test_save_catalog_entry_unknown_id_returns_404():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/catalog/does-not-exist/save")
        assert resp.status_code == 404


async def test_save_catalog_entry_gated_by_catalog_feature():
    async with running_app() as (app, client, adapter):
        entry = _entry()
        app.state.service.catalog.replace_source("eibi", [entry])
        await client.patch("/api/settings", json={"features": {"catalog": False}})

        resp = await client.post(f"/api/catalog/{entry.id}/save")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "feature disabled: catalog"


async def test_identify_includes_catalog_candidate():
    async with running_app() as (app, client, adapter):
        entry = _entry(
            frequency_hz=100_000_000, station="Shortwave Test Station",
            start_utc=None, stop_utc=None, language="E",
        )
        app.state.service.catalog.replace_source("eibi", [entry])

        resp = await client.get(
            "/api/identify", params={"hz": 100_000_000, "mode": "AM", "bw": 10000}
        )
        assert resp.status_code == 200
        candidates = resp.json()["candidates"]
        catalog_hits = [c for c in candidates if c["provider"] == "catalog"]
        assert len(catalog_hits) == 1
        assert "Shortwave Test Station" in catalog_hits[0]["name"]
        assert catalog_hits[0]["confidence"] == 0.85


async def test_identify_keeps_working_when_catalog_empty():
    async with running_app() as (app, client, adapter):
        resp = await client.get(
            "/api/identify", params={"hz": 162_550_000, "mode": "NFM", "bw": 12500}
        )
        assert resp.status_code == 200
        assert resp.json()["candidates"]


async def test_palette_includes_kind_catalog():
    async with running_app() as (app, client, adapter):
        entry = _entry(
            frequency_hz=9_410_000, station="Palette Match Station",
            start_utc=None, stop_utc=None,
        )
        app.state.service.catalog.replace_source("eibi", [entry])

        resp = await client.get("/api/palette", params={"q": "Palette Match"})
        assert resp.status_code == 200
        results = resp.json()
        catalog_results = [r for r in results if r["kind"] == "catalog"]
        assert len(catalog_results) == 1
        assert catalog_results[0]["title"] == "Palette Match Station"
        assert catalog_results[0]["frequency_hz"] == 9_410_000


async def test_catalog_feature_off_gates_all_catalog_routes():
    async with running_app() as (app, client, adapter):
        await client.patch("/api/settings", json={"features": {"catalog": False}})

        for method, url, kwargs in [
            ("get", "/api/catalog/search", {}),
            ("get", "/api/catalog/near", {"params": {"hz": 1000}}),
            ("get", "/api/catalog/sources", {}),
            ("post", "/api/catalog/refresh", {"json": {"source": "eibi"}}),
            ("post", "/api/catalog/import", {"json": {"path": "/tmp/x", "format": "sdrpp"}}),
            ("post", "/api/tune/catalog/whatever", {}),
        ]:
            resp = await getattr(client, method)(url, **kwargs)
            assert resp.status_code == 404, f"{method} {url} -> {resp.status_code}"
            assert resp.json()["detail"] == "feature disabled: catalog"


async def test_identify_omits_catalog_candidates_when_catalog_feature_off():
    async with running_app() as (app, client, adapter):
        entry = _entry(
            frequency_hz=100_000_000, station="Shortwave Test Station",
            start_utc=None, stop_utc=None,
        )
        app.state.service.catalog.replace_source("eibi", [entry])
        await client.patch("/api/settings", json={"features": {"catalog": False}})

        resp = await client.get(
            "/api/identify", params={"hz": 100_000_000, "mode": "AM", "bw": 10000}
        )
        assert resp.status_code == 200
        assert all(c["provider"] != "catalog" for c in resp.json()["candidates"])


async def test_catalog_refresh_falls_back_to_default_url_for_ndxc(monkeypatch):
    # ndxc has a fixed public URL; a blank catalog_sources entry must use it.
    from msdr.catalog import DEFAULT_URLS

    seen = {}

    def fake_fetch(url, encoding="utf-8"):
        seen["url"] = url
        return "6000;0000-0100;1234567;CUB;Radio Habana Cuba;Spanish;ND;RHC;\n"

    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", fake_fetch)
        resp = await client.post("/api/catalog/refresh", json={"source": "ndxc"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert body["entries"] == 1
        assert seen["url"] == DEFAULT_URLS["ndxc"]


async def test_catalog_refresh_static_source_needs_no_url_or_fetch(monkeypatch):
    # Static bundled sources must load without any network access.
    def _no_fetch(url, encoding="utf-8"):
        raise AssertionError("static sources must not fetch")

    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", _no_fetch)
        resp = await client.post("/api/catalog/refresh", json={"source": "noaa_radiofax"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        assert body["entries"] == 19
        near = (await client.get("/api/catalog/near", params={"hz": 8682000, "tol": 1000})).json()
        assert any(e["station"].startswith("NMC") for e in near["entries"])


def _zip_bytes(members: dict[str, str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text.encode("latin-1"))
    return buf.getvalue()


def test_catalog_payload_picks_hfcc_schedule_member_from_zip():
    # HFCC's archive bundles reference tables next to the schedule; only
    # A26all00.TXT is the schedule the parser understands.
    from tests.catalog.test_parsers_hfcc import HFCC_FIXTURE

    data = _zip_bytes({"admin.txt": "ADM table", "A26all00.TXT": HFCC_FIXTURE, "site.txt": "x"})
    member = service_module._CATALOG_ZIP_MEMBERS["hfcc"]
    assert service_module.catalog_payload_text(data, "latin-1", member) == HFCC_FIXTURE


async def test_hfcc_zip_refresh_caches_tables_and_plain_txt_reuses_them(
    tmp_path, monkeypatch
):
    import io
    import zipfile

    from tests.catalog.test_parsers_hfcc import HFCC_FIXTURE, HFCC_TABLE_FILES

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("A26all00.TXT", HFCC_FIXTURE.encode("latin-1"))
        for name, raw in HFCC_TABLE_FILES.items():
            zf.writestr(name, raw)
    zip_data = buf.getvalue()
    payload = {"data": zip_data}

    def fake_fetch(url, encoding="utf-8", member=None, members_out=None):
        return service_module.catalog_payload_text(payload["data"], encoding, member, members_out)

    async with running_app() as (app, client, adapter):
        svc = app.state.service
        monkeypatch.setattr(svc.settings, "db_path", tmp_path / "msdr.db")
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", fake_fetch)
        svc.settings.catalog_sources["hfcc"] = "http://example.test/a26.zip"

        resp = await client.post("/api/catalog/refresh", json={"source": "hfcc"})
        assert resp.json()["error"] is None
        assert (tmp_path / "catalog" / "hfcc" / "languages.json").exists()
        first = svc.catalog.search("", source="hfcc")[0]
        assert (first.country, first.language) == ("Vanuatu", "Bislama")

        # A direct .TXT URL carries no tables: the cached ones are reused.
        payload["data"] = HFCC_FIXTURE.encode("latin-1")
        resp = await client.post("/api/catalog/refresh", json={"source": "hfcc"})
        assert resp.json()["error"] is None
        again = svc.catalog.search("", source="hfcc")[0]
        assert (again.country, again.language) == ("Vanuatu", "Bislama")


def test_catalog_payload_skips_unknown_zip_members_for_members_out():
    # The multi-MB .xlsx is never parsed, so it must not be read into memory.
    from tests.catalog.test_parsers_hfcc import HFCC_FIXTURE

    data = _zip_bytes({"A26all00.TXT": HFCC_FIXTURE, "Site.txt": "s", "big.xlsx": "x" * 1000})
    member = service_module._CATALOG_ZIP_MEMBERS["hfcc"]
    members: dict[str, bytes] = {}
    service_module.catalog_payload_text(data, "latin-1", member, members)
    assert members == {"site.txt": b"s"}


async def test_hfcc_zip_without_tables_keeps_cached_names(tmp_path, monkeypatch):
    from tests.catalog.test_parsers_hfcc import HFCC_FIXTURE, HFCC_TABLE_FILES

    tables = {"A26all00.TXT": HFCC_FIXTURE, "big.xlsx": "x"}
    payload = {"data": None}

    def fake_fetch(url, encoding="utf-8", member=None, members_out=None):
        return service_module.catalog_payload_text(payload["data"], encoding, member, members_out)

    import io
    import zipfile

    def build(with_tables):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, text in tables.items():
                zf.writestr(name, text.encode("latin-1"))
            if with_tables:
                for name, raw in HFCC_TABLE_FILES.items():
                    zf.writestr(name, raw)
        return buf.getvalue()

    async with running_app() as (app, client, adapter):
        svc = app.state.service
        monkeypatch.setattr(svc.settings, "db_path", tmp_path / "msdr.db")
        monkeypatch.setattr(catalog_module, "fetch_catalog_text", fake_fetch)
        svc.settings.catalog_sources["hfcc"] = "http://example.test/a26.zip"
        payload["data"] = build(True)
        await client.post("/api/catalog/refresh", json={"source": "hfcc"})
        # A later ZIP with only schedule + .xlsx must reuse the cached tables.
        payload["data"] = build(False)
        resp = await client.post("/api/catalog/refresh", json={"source": "hfcc"})
        assert resp.json()["error"] is None
        first = svc.catalog.search("", source="hfcc")[0]
        assert (first.country, first.language) == ("Vanuatu", "Bislama")


def test_catalog_payload_zip_errors():
    member = service_module._CATALOG_ZIP_MEMBERS["hfcc"]
    try:
        service_module.catalog_payload_text(_zip_bytes({"admin.txt": "x"}), "latin-1", member)
        raise AssertionError("expected missing-member error")
    except ValueError as exc:
        assert "schedule file not found" in str(exc)
    try:
        service_module.catalog_payload_text(b"PK\x03\x04garbage", "latin-1", member)
        raise AssertionError("expected invalid-zip error")
    except ValueError as exc:
        assert "invalid ZIP" in str(exc)
    # A URL pointing straight at the .TXT still works.
    assert service_module.catalog_payload_text(b"plain", "latin-1", member) == "plain"


async def test_catalog_refresh_empty_parse_keeps_existing_entries(monkeypatch):
    async with running_app() as (app, client, adapter):
        monkeypatch.setattr(
            catalog_module, "fetch_catalog_text", lambda url, encoding="utf-8": EIBI_FIXTURE
        )
        first = (await client.post("/api/catalog/refresh", json={"source": "eibi"})).json()
        assert first["entries"] > 0
        monkeypatch.setattr(
            catalog_module, "fetch_catalog_text", lambda url, encoding="utf-8": "<html>oops</html>"
        )
        body = (await client.post("/api/catalog/refresh", json={"source": "eibi"})).json()
        assert body["entries"] == 0 and "unchanged" in body["error"]
        found = (await client.get("/api/catalog/search", params={"source": "eibi"})).json()
        assert found["count"] == first["entries"]
