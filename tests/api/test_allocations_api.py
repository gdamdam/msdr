"""Allocation API wiring (docs/allocations.md "Backend integration"): lookup /
bands / sources / refresh routes, the catalog sources group, the jurisdiction
setting, and the consumers that now carry the full allocation (signal detail
evidence, finds classification)."""

import json

import msdr.api.services.allocations as allocations_module
from msdr.allocations.importer import build_dataset
from msdr.allocations.model import Band, Footnote, Service, SourceStatus
from msdr.settings import Settings
from tests.api.test_app import running_app

_OCEANOGRAPHIC = "Oceanographic radar (47 CFR 90.103)"


def _tiny_dataset() -> dict:
    cols = ("itu_r1", "itu_r2", "itu_r3", "us_federal", "us_non_federal")
    bands = [
        Band(
            lo_hz=0,
            hi_hz=1_000,
            column=c,
            services=[Service(name="AMATEUR", priority="primary", raw="AMATEUR")],
            source_id="user_table",
        )
        for c in cols
    ]
    src = [
        SourceStatus(
            id="user_table", title="tiny", publisher="t", url="file", lo_hz=0, hi_hz=1_000,
            band_count=len(bands),
        )
    ]
    ds = build_dataset(bands, [Footnote(id="5.53", text="tiny")], src)
    ds["generated_at"] = "2999-01-01T00:00:00Z"  # newer than the bundled dataset
    return ds


# --- routes ------------------------------------------------------------------


async def test_lookup_route_returns_all_services_and_applications():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/allocations/lookup", params={"hz": 5_264_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok" and body["jurisdiction"] == "US"
        assert (body["lo_hz"], body["hi_hz"]) == (5_250_000, 5_275_000)
        names = [s["name"] for s in body["national"]["federal"]["services"]]
        assert names == ["FIXED", "MOBILE", "RADIOLOCATION"]
        assert body["international"]["region"] == "ITU2"
        # Supplemental layer: the §90.103 application rule, by containment only.
        assert _OCEANOGRAPHIC in [a["name"] for a in body["applications"]]
        assert body["channels"] == []

        assert body["supplemental"] == {"available": True, "reason": None}

        # Channel overlay: a kind "channel" band-plan entry within 5 kHz.
        resp = await client.get("/api/allocations/lookup", params={"hz": 162_401_000})
        body = resp.json()
        assert "NOAA Weather Radio WX2" in [c["name"] for c in body["channels"]]

        # An explicit jurisdiction overrides the setting. The band-plan hints
        # are US-specific, so ITU jurisdictions get none and a stated reason.
        resp = await client.get(
            "/api/allocations/lookup", params={"hz": 162_401_000, "jurisdiction": "ITU2"}
        )
        body = resp.json()
        assert body["jurisdiction"] == "ITU2" and body["national"] is None
        assert body["applications"] == [] and body["channels"] == []
        assert body["supplemental"] == {
            "available": False,
            "reason": "Channel and application hints are US-specific",
        }


async def test_bands_route_window_and_column_filter():
    async with running_app() as (app, client, _adapter):
        resp = await client.get(
            "/api/allocations/bands", params={"lo_hz": 5_000_000, "hi_hz": 5_500_000}
        )
        assert resp.status_code == 200
        bands = resp.json()
        assert bands and {b["column"] for b in bands} == {
            "us_federal", "us_non_federal", "itu_r2",
        }
        assert all(b["hi_hz"] > 5_000_000 and b["lo_hz"] < 5_500_000 for b in bands)

        resp = await client.get(
            "/api/allocations/bands",
            params={"lo_hz": 5_000_000, "hi_hz": 5_500_000, "column": "us_federal"},
        )
        assert {b["column"] for b in resp.json()} == {"us_federal"}


async def test_sources_route_lists_bundled_sources():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/allocations/sources")
        assert resp.status_code == 200
        by_id = {s["id"]: s for s in resp.json()}
        assert {"ntia_table", "ecfr_2_106"} <= set(by_id)
        assert by_id["ntia_table"]["status"] == "ok" and by_id["ntia_table"]["origin"] == "bundled"


async def test_refresh_failure_keeps_last_known_good(tmp_path, monkeypatch):
    def boom(source_id):
        raise RuntimeError("offline")

    monkeypatch.setattr(allocations_module, "build_allocation_dataset", boom)
    settings = Settings(db_path=":memory:", allocation_data_dir=tmp_path)
    async with running_app(settings=settings) as (app, client, _adapter):
        resp = await client.post("/api/allocations/refresh", json={"source": "ntia_table"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "ntia_table" and body["ok"] is False
        assert "offline" in body["error"]
        assert {s["id"] for s in body["sources"]} >= {"ntia_table", "ecfr_2_106"}
        assert not (tmp_path / "dataset.json").exists()
        # The bundled dataset still answers.
        lookup = await client.get("/api/allocations/lookup", params={"hz": 5_264_000})
        assert lookup.json()["status"] == "ok"


async def test_refresh_success_writes_user_dataset_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(
        allocations_module, "build_allocation_dataset", lambda source_id: _tiny_dataset()
    )
    settings = Settings(db_path=":memory:", allocation_data_dir=tmp_path)
    async with running_app(settings=settings) as (app, client, _adapter):
        resp = await client.post("/api/allocations/refresh", json={"source": "ntia_table"})
        body = resp.json()
        assert body["ok"] is True and body["error"] is None
        written = json.loads((tmp_path / "dataset.json").read_text())
        assert written["generated_at"] == "2999-01-01T00:00:00Z"
        by_id = {s["id"]: s for s in body["sources"]}
        assert by_id["user_table"]["origin"] == "imported"
        lookup = await client.get("/api/allocations/lookup", params={"hz": 500})
        assert lookup.json()["national"]["federal"]["services"][0]["name"] == "AMATEUR"


# --- catalog integration ---------------------------------------------------


async def test_catalog_sources_include_allocation_kind_and_refresh_delegates(monkeypatch):
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/catalog/sources")
        available = resp.json()["available"]
        groups = {row["source"]: row["group"] for row in available}
        assert groups["eibi"] == "catalog"
        assert groups["ntia_table"] == "allocation" and groups["ecfr_2_106"] == "allocation"

        called = []

        async def fake_refresh(source_id):
            called.append(source_id)
            return {"source": source_id, "ok": True, "error": None, "sources": []}

        monkeypatch.setattr(app.state.service, "allocation_refresh", fake_refresh)
        resp = await client.post("/api/catalog/refresh", json={"source": "ecfr_2_106"})
        assert resp.status_code == 200 and resp.json()["ok"] is True
        assert called == ["ecfr_2_106"]


# --- settings --------------------------------------------------------------


async def test_patch_allocation_jurisdiction_changes_default_and_rejects_unknown():
    async with running_app() as (app, client, _adapter):
        resp = await client.patch("/api/settings", json={"allocation_jurisdiction": "ITU1"})
        assert resp.status_code == 200
        assert resp.json()["allocation_jurisdiction"] == "ITU1"

        lookup = await client.get("/api/allocations/lookup", params={"hz": 5_264_000})
        body = lookup.json()
        assert body["jurisdiction"] == "ITU1" and body["national"] is None
        assert body["international"]["region"] == "ITU1"

        resp = await client.patch("/api/settings", json={"allocation_jurisdiction": "XX"})
        assert resp.status_code == 422
        assert app.state.service.settings.allocation_jurisdiction == "ITU1"


# --- consumers -------------------------------------------------------------


async def test_signal_detail_evidence_has_allocation_and_every_bandplan_hint(monkeypatch):
    async with running_app() as (app, client, _adapter):
        service = app.state.service

        def fake_identify(frequency_hz, width_hz):
            return [
                {
                    "name": "70cm amateur",
                    "category": "ham",
                    "confidence": 0.6,
                    "reason": "hint 1",
                    "provider": "bandplan",
                },
                {
                    "name": "amateur LEO satellite downlinks",
                    "category": "sat",
                    "confidence": 0.5,
                    "reason": "hint 2",
                    "provider": "bandplan",
                },
            ]

        monkeypatch.setattr(service, "_identify_for_survey", fake_identify)
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 435_000_000})
        assert resp.status_code == 200
        evidence = resp.json()["evidence"]
        bandplan = [e for e in evidence if e["kind"] == "bandplan"]
        assert [e["title"] for e in bandplan] == ["70cm amateur", "amateur LEO satellite downlinks"]
        [allocation] = [e for e in evidence if e["kind"] == "allocation"]
        # 420-450 MHz: RADIOLOCATION (Federal, primary) + Amateur (Non-Federal, secondary).
        assert allocation["title"] == "RADIOLOCATION; Amateur (secondary)"
        assert allocation["evidence_type"] == "allocation"
        assert allocation["state"] == "context" and allocation["source_group"] == "context"
        assert allocation["source"] == "allocations" and allocation["score"] is None
        detail = allocation["detail"]
        assert detail["status"] == "ok" and detail["national"]["shared"] is True
        assert len(detail["national"]["federal"]["services"]) + len(
            detail["national"]["non_federal"]["services"]
        ) > 1
        assert "applications" in detail and "channels" in detail


async def test_classify_find_carries_allocation_and_notes_list_every_service():
    async with running_app(survey=None) as (app, client, _adapter):
        service = app.state.service
        classification = await service._classify_find(5_264_000)
        allocation = classification["allocation"]
        assert (allocation["lo_hz"], allocation["hi_hz"]) == (5_250_000, 5_275_000)
        assert allocation["jurisdiction"] == "US" and allocation["status"] == "ok"
        assert {(s["name"], s["priority"], s["table"]) for s in allocation["services"]} >= {
            ("FIXED", "primary", "federal"),
            ("MOBILE", "primary", "non_federal"),
            ("RADIOLOCATION", "primary", "federal"),
        }
        notes = service._classification_notes(classification)
        assert "allocation: FIXED, MOBILE except aeronautical mobile, RADIOLOCATION" in notes

        shared = service._classification_notes(
            {"allocation": service.allocation_summary_at(435_000_000)}
        )
        assert shared == "allocation: RADIOLOCATION; Amateur (secondary)"
