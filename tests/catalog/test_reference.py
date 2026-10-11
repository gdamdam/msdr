"""Reference-catalog extensions (docs/reference-catalogs.md): nullable
frequency, stable ids, on_air_state, and migration 019's table rebuild."""

import datetime as dt
import shutil
from pathlib import Path

import pytest

from msdr.catalog.models import (
    ON_AIR_STATES,
    CatalogEntry,
    on_air,
    on_air_state,
    stable_entry_id,
    tune_recipe,
)
from msdr.catalog.store import CatalogRepo
from msdr.storage.db import _MIGRATIONS_DIR, Database

AT = dt.datetime(2026, 9, 22, 12, 0)


def _schedule(**overrides) -> CatalogEntry:
    base = dict(source="eibi", frequency_hz=6070_000, station="Test")
    base.update(overrides)
    return CatalogEntry(**base)


def _reference(**overrides) -> CatalogEntry:
    base = dict(
        source="nist_time", ref_type="station", network="NIST", station_id="WWV",
        component="10 MHz", station="WWV 10 MHz", frequency_hz=10_000_000, status="operational",
    )
    base.update(overrides)
    return CatalogEntry(**base)


# --- model -----------------------------------------------------------------


def test_schedule_rows_keep_defaults_and_uuid7_ids():
    a, b = _schedule(), _schedule()
    assert a.ref_type == "schedule"
    assert a.network is None and a.station_id is None and a.decoder is None
    assert a.id != b.id  # time-based, not derived


def test_stable_id_is_deterministic_for_reference_records():
    a = _reference()
    b = _reference()
    assert a.id == b.id == stable_entry_id("nist_time", "WWV", "10 MHz")
    assert _reference(component="15 MHz").id != a.id
    assert _reference(source="other").id != a.id
    # A caller-supplied id still wins.
    assert _reference(id="explicit").id == "explicit"


def test_null_frequency_is_allowed_and_not_tunable():
    radar = CatalogEntry(
        source="superdarn", ref_type="radar_site", station_id="bks", station="SuperDARN Blackstone",
    )
    assert radar.frequency_hz is None
    with pytest.raises(ValueError):
        tune_recipe(radar)


# --- on-air states ---------------------------------------------------------


def test_on_air_state_schedule_rows():
    assert on_air_state(_schedule(), AT) == "scheduled_on"
    assert on_air_state(_schedule(start_utc="1000", stop_utc="1400"), AT) == "scheduled_on"
    assert on_air_state(_schedule(start_utc="1400", stop_utc="1600"), AT) == "scheduled_off"


def test_on_air_state_station_continuous_or_unknown():
    assert on_air_state(_reference(status="operational"), AT) == "continuous"
    assert on_air_state(_reference(status="experimental"), AT) == "continuous"
    assert on_air_state(_reference(status="shutdown"), AT) == "unknown"
    assert on_air_state(_reference(status=None), AT) == "unknown"


def test_on_air_state_navaid_published_active():
    navaid = _reference(
        source="faa_nasr", ref_type="navaid", network="FAA NASR", station_id="SFO",
        component="VOR", status="operational_restricted", frequency_hz=115_800_000,
    )
    assert on_air_state(navaid, AT) == "published_active"
    assert on_air_state(navaid.model_copy(update={"status": "operational"}), AT) == (
        "published_active"
    )
    assert on_air_state(navaid.model_copy(update={"status": "unknown"}), AT) == "unknown"


def test_on_air_state_radar_sites_are_unknown():
    radar = CatalogEntry(source="superdarn", ref_type="radar_site", station_id="bks", station="x")
    assert on_air_state(radar, AT) == "unknown"
    assert set(ON_AIR_STATES) == {
        "continuous", "scheduled_on", "scheduled_off", "published_active", "unknown"
    }


def test_on_air_is_defined_on_top_of_states():
    assert on_air(_schedule(), AT)  # NULL window = always, as before
    assert not on_air(_schedule(start_utc="1400", stop_utc="1600"), AT)
    assert on_air(_reference(), AT)
    assert not on_air(_reference(status=None), AT)
    radar = CatalogEntry(source="superdarn", ref_type="radar_site", station_id="bks", station="x")
    assert not on_air(radar, AT)


# --- store -----------------------------------------------------------------


@pytest.fixture
def repo():
    db = Database(":memory:")
    db.connect()
    yield CatalogRepo(db)
    db.close()


def test_round_trip_with_null_frequency_and_reference_fields(repo):
    radar = CatalogEntry(
        source="superdarn", ref_type="radar_site", network="SuperDARN", station_id="bks",
        station="SuperDARN Blackstone", operator="Virginia Tech", status="unknown",
        status_at="2026-10-01", source_url="https://example/hdw.dat.bks", edition="2026-10-01",
        latitude=37.1, longitude=-77.9,
    )
    repo.replace_source("superdarn", [radar])
    got = repo.get(radar.id)
    assert got == radar
    assert got.frequency_hz is None and got.ref_type == "radar_site"
    # A null frequency never matches a frequency lookup or a band filter.
    assert repo.near(10_000_000, tolerance_hz=10_000_000_000) == []
    assert repo.search("", band=(0, None)) == []
    assert repo.count_matches("", band=(None, 10**12)) == 0
    assert repo.count_matches("") == 1


def test_search_filters_by_network_station_id_and_ref_type(repo):
    repo.replace_source("nist_time", [_reference(), _reference(component="15 MHz",
                                                             frequency_hz=15_000_000)])
    repo.replace_source("eibi", [_schedule()])
    assert repo.count_matches("", network="nist") == 2
    assert repo.count_matches("", station_id="WWV") == 2
    assert repo.count_matches("", ref_type="schedule") == 1
    assert [e.component for e in repo.search("", network="NIST", on_air_at=AT)] == [
        "10 MHz", "15 MHz"
    ]


def test_query_with_station_id_matches_text_or_id(repo):
    # The catalog view sends a short word as both q and station_id; a word
    # that is not a call sign ("china") must still find its text matches.
    repo.replace_source("nist_time", [_reference()])
    repo.replace_source("eibi", [_schedule(station="China Radio Int."), _schedule(country="CHN")])
    assert [e.station for e in repo.search("china", station_id="china")] == ["China Radio Int."]
    assert repo.count_matches("china", station_id="china") == 1
    assert [e.station for e in repo.search("wwv", station_id="wwv")] == ["WWV 10 MHz"]
    assert repo.count_matches("WWV", station_id="WWV", source="eibi") == 0
    repo._fts = False  # LIKE fallback takes the same OR path
    assert repo.count_matches("china", station_id="china") == 1


def test_on_air_ranking_excludes_radar_sites_from_the_active_segment(repo):
    radar = CatalogEntry(source="superdarn", ref_type="radar_site", station_id="bks", station="x")
    repo.replace_source("superdarn", [radar])
    repo.replace_source("nist_time", [_reference()])
    assert [e.source for e in repo.search("", on_air_at=AT)] == ["nist_time", "superdarn"]


# --- migration 019 ---------------------------------------------------------


def test_migration_019_preserves_rows_and_fts(tmp_path: Path):
    old_dir = tmp_path / "migrations"
    old_dir.mkdir()
    for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if int(sql.name.split("_", 1)[0]) <= 18:
            shutil.copy(sql, old_dir / sql.name)
    db_path = tmp_path / "msdr.sqlite"

    db = Database(db_path, migrations_dir=old_dir)
    db.connect()
    db.conn.execute(
        "INSERT INTO catalog_entries (id, source, frequency_hz, station, language, start_utc, "
        "stop_utc, date, latitude, longitude, imported_at) "
        "VALUES ('old1', 'eibi', 6070000, 'Deutschlandfunk', 'German', '0400', '0600', "
        "'2026-09-22', 51.0, 9.0, '2026-09-01T00:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO catalog_sources (source, label, entry_count) VALUES ('eibi', 'EiBi', 1)"
    )
    db.conn.commit()
    CatalogRepo(db)  # builds the FTS index over the pre-migration table
    db.close()

    db = Database(db_path)  # full migration set: 019 applies
    db.connect()
    repo = CatalogRepo(db)
    entry = repo.get("old1")
    assert entry is not None
    assert (entry.frequency_hz, entry.station) == (6070000, "Deutschlandfunk")
    assert entry.language == "German"
    assert (entry.start_utc, entry.stop_utc, entry.date) == ("0400", "0600", "2026-09-22")
    assert (entry.latitude, entry.longitude) == (51.0, 9.0)
    assert entry.ref_type == "schedule" and entry.network is None
    assert repo.sources()[0].entry_count == 1
    # Text search still finds the old row (rowids preserved for the FTS index)
    # and a new row with a null frequency can be inserted.
    assert [e.id for e in repo.search("Deutschlandfunk")] == ["old1"]
    repo.replace_source(
        "superdarn",
        [CatalogEntry(source="superdarn", ref_type="radar_site", station_id="bks", station="x")],
    )
    assert repo.count() == 2
    indexes = {
        row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='catalog_entries'"
        )
    }
    assert {"idx_catalog_entries_frequency_hz", "idx_catalog_entries_source"} <= indexes
    assert db.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 19
    db.close()


def test_stable_id_uses_place_discriminator_for_repeated_idents():
    # Short FAA idents repeat nationally ("AA" NDB at Fargo ND and Thomson GA).
    fargo = stable_entry_id("faa_nasr", "AA", "NDB", "FARGO, ND", 47.009, -96.815)
    thomson = stable_entry_id("faa_nasr", "AA", "NDB", "THOMSON, GA", 33.53, -82.51)
    assert fargo != thomson
    # Location text wins over coordinates, so a survey nudge does not churn ids.
    assert fargo == stable_entry_id("faa_nasr", "AA", "NDB", "FARGO, ND", 47.0091, -96.8152)
    # Without a location the coordinates (4 decimals) discriminate instead.
    by_coord = stable_entry_id("faa_nasr", "AA", "NDB", None, 47.00905216, -96.8151835)
    assert by_coord == stable_entry_id("faa_nasr", "AA", "NDB", None, 47.00906, -96.81518)
    assert by_coord != stable_entry_id("faa_nasr", "AA", "NDB", None, 33.53, -82.51)
    assert by_coord != fargo
    # Neither: the id is still deterministic from (source, station_id, component).
    assert stable_entry_id("x", "A", None) == stable_entry_id("x", "A", None, None, None, None)
    # The model validator feeds the same inputs.
    entry = CatalogEntry(
        source="faa_nasr", ref_type="navaid", station_id="AA", component="NDB",
        station="AA NDB", location="FARGO, ND", latitude=47.009, longitude=-96.815,
    )
    assert entry.id == fargo


def test_round_trip_timestamped_current_frequency(repo):
    radar = CatalogEntry(
        source="test_radar", ref_type="radar_site", network="Test", station_id="r1",
        station="Synthetic radar", current_freq_hz=12_345_000,
        current_freq_at="2026-10-01T12:00:00Z", freq_lo_hz=8_000_000, freq_hi_hz=22_000_000,
    )
    repo.replace_source("test_radar", [radar])
    got = repo.get(radar.id)
    assert got == radar
    assert got.frequency_hz is None  # a current reading is not a tunable fixed frequency
    assert (got.current_freq_hz, got.current_freq_at) == (12_345_000, "2026-10-01T12:00:00Z")
    assert (got.freq_lo_hz, got.freq_hi_hz) == (8_000_000, 22_000_000)
