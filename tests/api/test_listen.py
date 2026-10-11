"""LISTEN presets: GET /api/listen?preset=<name>."""

import datetime as dt
import json

from msdr.api.service import beacon_schedule
from msdr.catalog.parsers.eibi import parse_eibi
from msdr.providers.tracking import SatTracker, TleStore
from msdr.settings import Location, Settings
from msdr.storage.repos import JournalEntry
from tests.api.test_app import running_app

_SAN_FRANCISCO = Location(lat=37.77, lon=-122.42, altitude_m=15.0, timezone="America/Los_Angeles")

ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-09-22T06:30:37.496448",
    "MEAN_MOTION": 15.49224498,
    "ECCENTRICITY": 0.00047657,
    "INCLINATION": 51.6312,
    "RA_OF_ASC_NODE": 179.6046,
    "ARG_OF_PERICENTER": 167.6102,
    "MEAN_ANOMALY": 192.5004,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 58680,
    "BSTAR": 0.0001364276,
    "MEAN_MOTION_DOT": 7.132e-05,
    "MEAN_MOTION_DDOT": 0,
}


def _hhmm(at: dt.datetime) -> str:
    return at.strftime("%H%M")


def _eibi_line(khz: float, time_range: str, station: str, lang: str, remarks: str = "") -> str:
    return f"{khz};{time_range};;D;{station};{lang};Europe;{remarks};;;"


def _seed_eibi(app, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    entries = parse_eibi(text, filename="sked-a26.csv")
    app.state.service.catalog.replace_source("eibi", entries)


async def test_unknown_preset_returns_422():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/listen", params={"preset": "not-a-real-preset"})
        assert resp.status_code == 422


async def test_on_air_my_languages_filters_by_language_and_sorts_by_vfo_distance():
    async with running_app() as (app, client, adapter):
        always_on = "0000-2359"
        # Default seeded VFO is 145_800_000 Hz -- the closer English station wins.
        _seed_eibi(
            app,
            [
                _eibi_line(145_790, always_on, "Near English", "E"),
                _eibi_line(9_410, always_on, "Far English", "E"),
                _eibi_line(6_070, always_on, "German Station", "G"),
            ],
        )
        await client.patch("/api/settings", json={"catalog_languages": ["E"]})

        resp = await client.get("/api/listen", params={"preset": "on_air_my_languages"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["preset"] == "on_air_my_languages"
        titles = [i["title"] for i in body["items"]]
        assert titles == ["Near English (E)", "Far English (E)"]
        assert all(i["kind"] == "catalog" for i in body["items"])


async def test_starting_soon_includes_only_entries_within_15_minutes_and_not_on_air():
    async with running_app() as (app, client, adapter):
        now = dt.datetime.now(dt.UTC)
        soon_start = _hhmm(now + dt.timedelta(minutes=5))
        soon_stop = _hhmm(now + dt.timedelta(minutes=65))
        later_start = _hhmm(now + dt.timedelta(minutes=45))
        later_stop = _hhmm(now + dt.timedelta(minutes=105))
        _seed_eibi(
            app,
            [
                _eibi_line(6_070, f"{soon_start}-{soon_stop}", "Starting Soon", "E"),
                _eibi_line(6_100, f"{later_start}-{later_stop}", "Starting Later", "E"),
                _eibi_line(6_150, "0000-2359", "Already On Air", "E"),
            ],
        )

        resp = await client.get("/api/listen", params={"preset": "starting_soon"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        titles = [i["title"] for i in items]
        assert titles == ["Starting Soon (E)"]
        # `when` is the entry's next actual UTC start, not left blank.
        expected_start = (now + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
        assert items[0]["when"] == expected_start.strftime("%Y-%m-%dT%H:%M:00Z")


async def test_unheard_excludes_stations_seen_in_the_journal():
    async with running_app() as (app, client, adapter):
        _seed_eibi(
            app,
            [
                _eibi_line(6_070, "0000-2359", "Heard Station", "E"),
                _eibi_line(6_100, "0000-2359", "Unheard Station", "E"),
            ],
        )
        create_resp = await client.post(
            "/api/signals",
            json={"name": "Logged", "frequency_hz": 6_070_000, "station": "Heard Station"},
        )
        signal_id = create_resp.json()["id"]
        service = app.state.service
        service.journal_repo.add(JournalEntry(kind="observed", signal_id=signal_id))

        resp = await client.get("/api/listen", params={"preset": "unheard"})
        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["items"]]
        assert titles == ["Unheard Station (E)"]


async def test_heard_this_hour_ranks_by_count_excluding_today():
    async with running_app() as (app, client, adapter):
        create_resp = await client.post(
            "/api/signals", json={"name": "Regular", "frequency_hz": 7_100_000}
        )
        signal_id = create_resp.json()["id"]
        service = app.state.service
        yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        at = yesterday.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        for _ in range(3):
            service.journal_repo.add(JournalEntry(kind="observed", signal_id=signal_id, at=at))
        # A today entry at the same hour must NOT count (it's "today", not a previous day).
        today_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        service.journal_repo.add(JournalEntry(kind="observed", signal_id=signal_id, at=today_at))

        resp = await client.get("/api/listen", params={"preset": "heard_this_hour"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Regular"
        assert items[0]["score"] == 3.0


async def test_decodable_matches_notes_tokens_for_on_air_entries():
    async with running_app() as (app, client, adapter):
        _seed_eibi(
            app,
            [
                _eibi_line(
                    4_610, "0000-2359", "Radiofax Station", "E", remarks="fax weather charts"
                ),
                _eibi_line(6_070, "0000-2359", "Plain Broadcast", "E", remarks="light music"),
            ],
        )

        resp = await client.get("/api/listen", params={"preset": "decodable"})
        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["items"]]
        assert titles == ["Radiofax Station (E)"]


def test_beacon_schedule_rotates_one_slot_per_frequency():
    cycle_start = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.UTC)

    at_start = beacon_schedule(cycle_start)
    assert at_start[14_100_000] == "4U1UN"
    assert at_start[18_110_000] == "YV5B"  # the callsign that closes out the prior cycle

    ten_seconds_later = beacon_schedule(cycle_start + dt.timedelta(seconds=10))
    assert ten_seconds_later[14_100_000] == "VE8AT"
    # The beacon that just aired on 14.100 shows up on 18.110 ten seconds later.
    assert ten_seconds_later[18_110_000] == at_start[14_100_000]

    forty_seconds_later = beacon_schedule(cycle_start + dt.timedelta(seconds=40))
    assert forty_seconds_later[28_200_000] == at_start[14_100_000]


async def test_beacons_preset_lists_all_five_frequencies():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/listen", params={"preset": "beacons"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        freqs = sorted(i["frequency_hz"] for i in items)
        assert freqs == [14_100_000, 18_110_000, 21_150_000, 24_930_000, 28_200_000]
        assert all(i["subtitle"] for i in items)


async def test_beacons_preset_tune_target_is_a_cw_narrow_bandwidth_recipe():
    # Beacons have no catalog/signal row to tune *from*, so the item must
    # carry its own recipe -- CW, not whatever mode the receiver happens to
    # be in (this was the "tune-signal to a synthetic beacon: id -> 404" bug).
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/listen", params={"preset": "beacons"})
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["tune"] == {
            "kind": "recipe",
            "id": None,
            "frequency_hz": item["frequency_hz"],
            "demodulator": "CW",
            "filter_bandwidth_hz": 500,
        }


async def test_on_air_my_languages_tune_target_uses_catalog_tune_recipe():
    async with running_app() as (app, client, adapter):
        _seed_eibi(app, [_eibi_line(6_070, "0000-2359", "No Mode Station", "E")])
        await client.patch("/api/settings", json={"catalog_languages": ["E"]})

        resp = await client.get("/api/listen", params={"preset": "on_air_my_languages"})
        item = resp.json()["items"][0]
        # tune_recipe() defaults an unset mode to AM/9000 Hz -- same recipe
        # POST /api/tune/catalog/{id} would apply.
        assert item["tune"] == {
            "kind": "catalog",
            "id": item["id"],
            "frequency_hz": 6_070_000,
            "demodulator": "AM",
            "filter_bandwidth_hz": 9000,
        }


async def test_passes_compatible_lists_upcoming_pass_for_bound_satellite(tmp_path):
    settings = Settings(dwell_seconds=0.05, db_path=":memory:", location=_SAN_FRANCISCO)
    async with running_app(settings=settings) as (app, client, adapter):
        create_resp = await client.post(
            "/api/signals",
            json={"name": "ISS Memory", "frequency_hz": 145_800_000, "satellite": "ISS"},
        )
        assert create_resp.status_code == 200
        signal_id = create_resp.json()["id"]
        service = app.state.service
        tle_dir = tmp_path / "tle"
        tle_dir.mkdir()
        (tle_dir / "25544.json").write_text(json.dumps([ISS_OMM]))
        service.tle_store = TleStore([tle_dir])
        service.tracker = SatTracker(service.tle_store, _SAN_FRANCISCO)

        resp = await client.get("/api/listen", params={"preset": "passes_compatible"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        assert items[0]["kind"] == "pass"
        assert items[0]["title"] == "ISS"
        assert "ISS Memory" in items[0]["subtitle"]
        assert items[0]["frequency_hz"] == 145_800_000
        # A pass's item id is the synthetic "<sat>:<rise>" key, but its tune
        # target is the bound memory's own signal id -- that's what
        # POST /api/tune/signal/{id} needs (not the synthetic key).
        assert items[0]["tune"] == {
            "kind": "signal",
            "id": signal_id,
            "frequency_hz": 145_800_000,
            "demodulator": None,
            "filter_bandwidth_hz": None,
        }


async def test_passes_compatible_empty_when_passes_feature_off():
    async with running_app() as (app, client, adapter):
        await client.post(
            "/api/signals",
            json={"name": "ISS Memory", "frequency_hz": 145_800_000, "satellite": "ISS"},
        )
        await client.patch("/api/settings", json={"features": {"passes": False}})

        resp = await client.get("/api/listen", params={"preset": "passes_compatible"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []


async def test_catalog_presets_empty_when_catalog_feature_off():
    async with running_app() as (app, client, adapter):
        _seed_eibi(app, [_eibi_line(6_070, "0000-2359", "Always On", "E")])
        await client.patch("/api/settings", json={"features": {"catalog": False}})

        for preset in ("on_air_my_languages", "starting_soon", "unheard", "decodable"):
            resp = await client.get("/api/listen", params={"preset": preset})
            assert resp.status_code == 200
            assert resp.json()["items"] == [], preset


async def test_tune_frequency_with_demodulator_and_bandwidth_restores_full_recipe():
    # `hz` alone must keep working (journal/palette "tune here" rows), but a
    # LISTEN recipe item (e.g. a beacon) needs mode/bandwidth restored too.
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/tune/frequency",
            json={"hz": 14_100_000, "demodulator": "CW", "filter_bandwidth_hz": 500},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"

        state = (await client.get("/api/state")).json()
        assert state["props"]["device_vfo_frequency"] == 14_100_000
        assert state["props"]["demodulator"] == "CW"
        assert state["props"]["filter_bandwidth"] == 500


async def test_tune_frequency_without_recipe_fields_still_works():
    async with running_app() as (app, client, adapter):
        resp = await client.post("/api/tune/frequency", json={"hz": 7_100_000})
        assert resp.status_code == 200
        assert resp.json()["status"] == "confirmed"
        state = (await client.get("/api/state")).json()
        assert state["props"]["device_vfo_frequency"] == 7_100_000
