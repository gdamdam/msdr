"""Scanner finds log API (msdr/api/**): GET /api/finds,
PATCH/DELETE /api/finds/{id}, POST .../promote, POST .../tune, the on_find ->
FindRepo.add_or_merge + ws push wiring, the classify() callback (survey label
snapshot vs identify()-only fallback) and the is_ignored cache."""

import contextlib

import httpx

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_app import FakeSurvey


@contextlib.asynccontextmanager
async def running_app(*, survey=None, settings=None):
    adapter = FakeAdapter()
    settings = settings or Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db, survey=survey)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, adapter


def _find(**overrides) -> dict:
    find = {
        "ts": 1000.0,
        "frequency_hz": 146_520_000,
        "label": "APRS",
        "signal_id": None,
        "peak_snr_db": 10.0,
        "duration_s": 2.5,
        "demodulator": "NFM",
        "bandwidth_hz": 12_500,
        "classification": {"source": "identify", "bandplan": {"name": "2m"}},
    }
    find.update(overrides)
    return find


# --- routes ------------------------------------------------------------------


async def test_get_finds_returns_finds_and_counts():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.finds.add_or_merge(_find())
        service.finds.add_or_merge(_find(frequency_hz=440_000_000, label="DMR"))

        resp = await client.get("/api/finds")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["finds"]) == 2
        assert body["counts"] == {"new": 2}


async def test_get_finds_filters_by_status_and_q():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        a, _ = service.finds.add_or_merge(_find(label="APRS"))
        service.finds.add_or_merge(_find(frequency_hz=440_000_000, label="DMR"))
        service.finds.set_status(a["id"], "reviewed")

        resp = await client.get("/api/finds", params={"status": "reviewed"})
        assert [f["id"] for f in resp.json()["finds"]] == [a["id"]]

        resp = await client.get("/api/finds", params={"q": "DMR"})
        assert len(resp.json()["finds"]) == 1
        assert resp.json()["finds"][0]["label"] == "DMR"


async def test_patch_find_updates_status_and_note():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find())

        resp = await client.patch(
            f"/api/finds/{row['id']}", json={"status": "reviewed", "note": "hm"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "reviewed"
        assert resp.json()["note"] == "hm"


async def test_patch_find_missing_returns_404():
    async with running_app() as (app, client, _adapter):
        resp = await client.patch("/api/finds/nope", json={"status": "reviewed"})
        assert resp.status_code == 404


async def test_delete_find_removes_row():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find())

        resp = await client.delete(f"/api/finds/{row['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert service.finds.get(row["id"]) is None


async def test_delete_find_missing_returns_404():
    async with running_app() as (app, client, _adapter):
        resp = await client.delete("/api/finds/nope")
        assert resp.status_code == 404


async def test_promote_find_creates_signal_and_sets_saved():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(
            _find(
                classification={
                    "source": "survey",
                    "family": {"name": "DMR", "suggested_decoders": ["dsd-fme"]},
                    "bandplan": {"name": "2m"},
                }
            )
        )

        resp = await client.post(f"/api/finds/{row['id']}/promote", json={"name": None})
        assert resp.status_code == 200
        signal = resp.json()
        assert signal["name"] == "APRS"
        assert signal["frequency_hz"] == 146_520_000
        assert signal["demodulator"] == "NFM"
        assert signal["filter_bandwidth_hz"] == 12_500
        assert signal["decoders"] == ["dsd-fme"]
        assert "bandplan: 2m" in signal["notes"]

        updated = service.finds.get(row["id"])
        assert updated["status"] == "saved"
        assert updated["signal_id"] == signal["id"]
        assert service.signals.get(signal["id"]) is not None


async def test_promote_find_uses_given_name_and_falls_back_frequency_label():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find(label=None))

        resp = await client.post(f"/api/finds/{row['id']}/promote", json={"name": "My channel"})
        assert resp.json()["name"] == "My channel"

        row2, _ = service.finds.add_or_merge(_find(frequency_hz=433_000_000, label=None))
        resp2 = await client.post(f"/api/finds/{row2['id']}/promote", json={})
        assert resp2.json()["name"] == "433.0000 find"


async def test_promote_find_missing_returns_404():
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/finds/nope/promote", json={})
        assert resp.status_code == 404


async def test_tune_find_calls_tune_frequency_with_finds_demod_and_bandwidth():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find(demodulator="AM", bandwidth_hz=6_000))

        resp = await client.post(f"/api/finds/{row['id']}/tune")
        assert resp.status_code == 200
        props = service.confirmed.props
        assert props["device_vfo_frequency"] == 146_520_000
        assert props["demodulator"] == "AM"
        assert props["filter_bandwidth"] == 6_000


async def test_tune_find_missing_returns_404():
    async with running_app() as (app, client, _adapter):
        resp = await client.post("/api/finds/nope/tune")
        assert resp.status_code == 404


async def test_finds_routes_404_when_feature_disabled():
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    settings.features.finds = False
    async with running_app(settings=settings) as (app, client, _adapter):
        assert (await client.get("/api/finds")).status_code == 404
        assert (await client.delete("/api/finds/nope")).status_code == 404


# --- on_find wiring: repo merge + ws push -------------------------------------


async def test_on_find_merges_and_publishes_find_message():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        q = service.broadcaster.subscribe()

        await service._on_find(_find(ts=1.0))
        await service._on_find(_find(ts=2.0, frequency_hz=146_522_000, peak_snr_db=20.0))

        assert len(service.finds.list()) == 1  # merged, not duplicated
        published = [q.get_nowait(), q.get_nowait()]
        assert [m["type"] for m in published] == ["find", "find"]
        assert published[0]["created"] is True
        assert published[1]["created"] is False
        assert published[1]["find"]["seen_count"] == 2


# --- classify(): survey label snapshot vs identify()-only fallback -----------


async def test_classify_falls_back_to_identify_when_no_survey():
    async with running_app(survey=None) as (app, client, _adapter):
        service = app.state.service
        classification = await service._classify_find(146_520_000)
        assert classification["source"] == "identify"
        assert classification["activity"] is None
        assert classification["confirmed"] is None
        assert "bandplan" in classification
        assert "catalog" in classification


async def test_classify_falls_back_to_identify_when_survey_inactive():
    fake = FakeSurvey()
    fake.active = False
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        classification = await service._classify_find(146_520_000)
        assert classification["source"] == "identify"


async def test_classify_uses_survey_label_within_tolerance():
    fake = FakeSurvey()
    fake.active = True

    def status_with_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance = 2e6/512 = ~3906 Hz
        base["labels"] = [
            {
                "frequency_hz": 146_521_000,  # 1 kHz away -- within tolerance
                "activity": {"kind": "busy", "duty": 0.5},
                "bw_class": "narrow",
                "modulation": {"kind": "fm"},
                "symbol_rate_hz": None,
                "family": {"name": "APRS", "suggested_decoders": ["multimon-ng"]},
                "voice": None,
                "confirmed": None,
            }
        ]
        return base

    fake.status = status_with_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        classification = await service._classify_find(146_520_000)
        assert classification["source"] == "survey"
        assert classification["activity"] == {"kind": "busy", "duty": 0.5}
        assert classification["family"] == {"name": "APRS", "suggested_decoders": ["multimon-ng"]}
        assert "bandplan" in classification
        assert "catalog" in classification


async def test_classify_ignores_survey_label_outside_tolerance():
    fake = FakeSurvey()
    fake.active = True

    def status_with_far_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance ~3906 Hz
        base["labels"] = [{"frequency_hz": 146_600_000, "activity": {"kind": "busy", "duty": 0.5}}]
        return base

    fake.status = status_with_far_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        classification = await service._classify_find(146_520_000)
        assert classification["source"] == "identify"


# --- is_ignored cache, refreshed on status changes ----------------------------


async def test_is_ignored_cache_refreshes_on_patch_and_delete():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find())
        assert service._is_find_ignored(146_520_000) is False

        await client.patch(f"/api/finds/{row['id']}", json={"status": "ignored"})
        assert service._is_find_ignored(146_520_000) is True
        # within the +-6 kHz tolerance
        assert service._is_find_ignored(146_524_000) is True
        assert service._is_find_ignored(146_600_000) is False

        await client.patch(f"/api/finds/{row['id']}", json={"status": "reviewed"})
        assert service._is_find_ignored(146_520_000) is False


async def test_is_ignored_cache_refreshes_on_delete():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        row, _ = service.finds.add_or_merge(_find())
        await client.patch(f"/api/finds/{row['id']}", json={"status": "ignored"})
        assert service._is_find_ignored(146_520_000) is True

        await client.delete(f"/api/finds/{row['id']}")
        assert service._is_find_ignored(146_520_000) is False
