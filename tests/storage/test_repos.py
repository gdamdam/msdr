import inspect
import pathlib
import sqlite3

import pytest

from msdr.engine.history import HistoryStack
from msdr.engine.state import ReceiverState
from msdr.library.models import CATEGORY_TAGS, ReceiverProfile, Signal, Tag
from msdr.storage.db import Database
from msdr.storage.repos import (
    BeaconHitRepo,
    DecodeEventRepo,
    FindRepo,
    HistoryRepo,
    JournalEntry,
    JournalRepo,
    LibraryUndoRepo,
    ListRepo,
    ProfileRepo,
    SettingsRepo,
    SignalRepo,
    StateRepo,
    TagRepo,
)

# The schema version tracks the number of numbered migration files, so adding a
# migration never requires touching this test again.
EXPECTED_SCHEMA_VERSION = len(
    list((pathlib.Path(inspect.getfile(Database)).parent / "migrations").glob("*.sql"))
)

WIRE_PROPS = {
    "device_vfo_frequency": 145_800_000,
    "device_center_frequency": 145_800_000,
    "device_sample_rate": 2_000_000,
    "demodulator": "NFM",
    "filter_bandwidth": 12_500,
    "lna_state": 3,
    "active_antenna": "Antenna B",
    "squelch_enable": False,
    "squelch_threshold": -100.0,
    "agc_enable": True,
    "agc_threshold": -30.0,
    "noise_reduction_enable": False,
    "noise_reduction_strength": 0,
}


def _state(**overrides) -> ReceiverState:
    props = dict(WIRE_PROPS, **overrides)
    return ReceiverState.from_props(props, source="capture")


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


def test_migrate_creates_tables_and_records_schema_version(db):
    tables = {
        row["name"]
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "schema_version",
        "receiver_states",
        "profiles",
        "signals",
        "tags",
        "signal_tags",
        "history",
        "history_meta",
        "journal_entries",
        "settings",
    } <= tables
    row = db.conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == EXPECTED_SCHEMA_VERSION


def test_connect_is_idempotent(db):
    db.connect()  # second call must not re-run migrations or raise
    row = db.conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == EXPECTED_SCHEMA_VERSION


def test_migration_002_adds_color_columns(db):
    tag_cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(tags)").fetchall()}
    signal_cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(signals)").fetchall()}
    assert "color" in tag_cols
    assert "color" in signal_cols


def test_migration_003_adds_satellite_column(db):
    signal_cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(signals)").fetchall()}
    assert "satellite" in signal_cols


def test_migration_009_adds_decoders_column_and_decode_events_table(db):
    signal_cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(signals)").fetchall()}
    assert "decoders_json" in signal_cols
    event_cols = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(decode_events)").fetchall()
    }
    assert event_cols == {
        "id",
        "ts",
        "frequency_hz",
        "signal_id",
        "decoder",
        "kind",
        "text",
        "fields_json",
        "image_path",
        "observation_id",  # added by 012_observations
        # added by 013_decode_integrity
        "integrity",
        "integrity_note",
        "source_id",
        "generation",
        "sample_index",
        "run_index",  # added by 015_decode_run
        "analysis_run_id",
    }


def test_migration_file_is_atomic_on_failure(tmp_path):
    # A migration file that fails partway through must not leave its earlier
    # statements committed, and must not bump schema_version -- otherwise a
    # retry re-runs the same (partially applied) file and hits
    # "table already exists" forever.
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_ok.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
        "CREATE TABLE ok_table (id INTEGER);\n"
    )
    (migrations_dir / "002_bad.sql").write_text(
        "CREATE TABLE c (id INTEGER);\nCREATE TABL syntax_error (id INTEGER);\n"
    )
    db_path = tmp_path / "test.db"
    database = Database(db_path, migrations_dir=migrations_dir)

    with pytest.raises(sqlite3.OperationalError):
        database.connect()

    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    tables = {
        row["name"]
        for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "c" not in tables  # rolled back along with the rest of 002_bad.sql
    assert "ok_table" in tables  # 001_ok.sql committed fine
    assert raw.execute("SELECT version FROM schema_version").fetchone()["version"] == 1
    raw.close()

    # Fix 002 and retry on the same Database instance: connect() must not
    # have wedged itself into a permanently "connected but unmigrated" state.
    (migrations_dir / "002_bad.sql").write_text("CREATE TABLE c (id INTEGER);\n")
    database.connect()
    assert database.conn.execute("SELECT version FROM schema_version").fetchone()["version"] == 2
    database.close()


def test_signal_add_get_update_delete_with_tags(db):
    tag_repo = TagRepo(db)
    signal_repo = SignalRepo(db)
    tag = Tag(name="Aviation", kind="category")
    tag_repo.add(tag)

    signal = Signal(
        name="Metro Tower", frequency_hz=120_500_000, tag_ids=[tag.id],
        latitude=37.5, longitude=-122.2,
    )
    signal_repo.add(signal)

    fetched = signal_repo.get(signal.id)
    assert fetched.name == "Metro Tower"
    assert fetched.tag_ids == [tag.id]
    assert (fetched.latitude, fetched.longitude) == (37.5, -122.2)

    fetched.name = "Metro Tower ATC"
    fetched.tag_ids = []
    fetched.latitude = 38.0
    signal_repo.update(fetched)
    updated = signal_repo.get(signal.id)
    assert updated.name == "Metro Tower ATC"
    assert updated.tag_ids == []
    assert (updated.latitude, updated.longitude) == (38.0, -122.2)
    assert updated.updated_at >= signal.updated_at

    signal_repo.delete(signal.id)
    assert signal_repo.get(signal.id) is None


def test_inbox_orders_newest_created_first(db):
    signal_repo = SignalRepo(db)
    first = Signal(
        name="UNKNOWN 1",
        frequency_hz=100_000_000,
        status="unknown",
        created_at="2026-01-01T00:00:00Z",
    )
    signal_repo.add(first)
    second = Signal(
        name="UNKNOWN 2",
        frequency_hz=101_000_000,
        status="unknown",
        created_at="2026-01-02T00:00:00Z",
    )
    signal_repo.add(second)
    identified = Signal(name="Known", frequency_hz=102_000_000, status="identified")
    signal_repo.add(identified)

    inbox = signal_repo.inbox()
    assert [s.id for s in inbox] == [second.id, first.id]
    assert all(s.status == "unknown" for s in inbox)


def test_list_filters_by_name_or_station_substring(db):
    signal_repo = SignalRepo(db)
    signal_repo.add(Signal(name="Meteor Beacon", frequency_hz=137_100_000))
    signal_repo.add(Signal(name="Weather Fax", frequency_hz=137_500_000, station="Meteostation"))
    signal_repo.add(Signal(name="Aircraft Tower", frequency_hz=120_000_000))

    results = signal_repo.list(q="met")
    assert {s.name for s in results} == {"Meteor Beacon", "Weather Fax"}


def test_profile_json_round_trip(db):
    profile_repo = ProfileRepo(db)
    profile = ReceiverProfile(name="VHF Airband", antenna="Discone", lna_state=2)
    profile_repo.add(profile)

    fetched = profile_repo.get(profile.id)
    assert fetched == profile

    listed = profile_repo.list()
    assert listed == [profile]


def test_deleting_profile_nulls_signal_profile_id(db):
    profile_repo = ProfileRepo(db)
    signal_repo = SignalRepo(db)
    profile = ReceiverProfile(name="HF")
    profile_repo.add(profile)
    signal = Signal(name="Shortwave Station", frequency_hz=9_400_000, profile_id=profile.id)
    signal_repo.add(signal)

    profile_repo.delete(profile.id)

    assert signal_repo.get(signal.id).profile_id is None


def test_history_save_and_load_round_trip(db):
    history_repo = HistoryRepo(db)
    state_repo = StateRepo(db)
    stack = HistoryStack()
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack.push(a)
    stack.push(b)
    stack.back()
    state_repo.add(a)
    state_repo.add(b)

    history_repo.save(stack)
    state_ids, cursor = history_repo.load()

    assert state_ids == [a.id, b.id]
    assert cursor == 0


def test_journal_list_day_filters_by_day(db):
    journal_repo = JournalRepo(db)
    journal_repo.add(JournalEntry(kind="capture", at="2026-01-01T10:00:00Z"))
    journal_repo.add(JournalEntry(kind="tune", at="2026-01-01T11:00:00Z"))
    journal_repo.add(JournalEntry(kind="capture", at="2026-01-02T09:00:00Z"))

    day_entries = journal_repo.list_day("2026-01-01")
    assert [e.kind for e in day_entries] == ["capture", "tune"]
    assert journal_repo.days(limit=30) == ["2026-01-02", "2026-01-01"]


def test_settings_get_set(db):
    settings_repo = SettingsRepo(db)
    assert settings_repo.get("nearby_hz", default=5000) == 5000
    settings_repo.set("nearby_hz", 2500)
    assert settings_repo.get("nearby_hz") == 2500
    settings_repo.set("nearby_hz", 3000)
    assert settings_repo.get("nearby_hz") == 3000


def test_tag_ensure_categories_is_idempotent(db):
    tag_repo = TagRepo(db)
    tag_repo.ensure_categories()
    tag_repo.ensure_categories()
    names = {t.name for t in tag_repo.list()}
    assert "fav" in names
    assert len(tag_repo.list()) == len(set(names))


def test_tag_ensure_categories_renames_merges_and_sets_default_colors(db):
    # Seed the DB with the pre-short-code legacy names, as an install
    # upgrading from before this feature would have them: kind='category',
    # color NULL (the column didn't exist yet).
    tag_repo = TagRepo(db)
    signal_repo = SignalRepo(db)
    legacy_names = [
        "Favorites",
        "Satellites",
        "Aviation",
        "Shortwave",
        "Amateur",
        "Marine",
        "Broadcast",
        "Digital",
        "Experiments",
        "Unidentified",
    ]
    for name in legacy_names:
        tag_repo.add(Tag(name=name, kind="category"))
    legacy = {name: tag_repo.get_by_name(name).id for name in legacy_names}

    # A custom color set on "Aviation" (-> air, default "orange") must survive
    # the rename untouched.
    aviation = tag_repo.get_by_name("Aviation")
    aviation.color = "gray"
    tag_repo.update(aviation)

    # A signal tagged only with the legacy "Broadcast" id, to prove its
    # signal_tags row is preserved (remapped onto "bc") through the merge.
    signal = Signal(name="Shortwave station", frequency_hz=9_400_000, tag_ids=[legacy["Broadcast"]])
    signal_repo.add(signal)

    tag_repo.ensure_categories()

    by_name = {t.name: t for t in tag_repo.list()}
    assert set(by_name) >= {
        "sat", "ham", "mil", "air", "marine", "tv", "bc", "digital", "unid", "exp", "fav",
    }
    # Old names are gone; Shortwave+Broadcast merged into one "bc" tag.
    assert "Shortwave" not in by_name
    assert "Broadcast" not in by_name

    bc = by_name["bc"]
    assert signal_repo.get(signal.id).tag_ids == [bc.id]

    # Custom color survived; freshly-added/renamed categories got their
    # CATEGORY_TAGS default because color was NULL.
    assert by_name["air"].color == "gray"
    default_colors = dict(CATEGORY_TAGS)
    for name in ("sat", "ham", "mil", "marine", "tv", "bc", "digital", "unid", "exp", "fav"):
        assert by_name[name].color == default_colors[name]


def test_tag_repo_update_renames_and_recolors(db):
    tag_repo = TagRepo(db)
    tag = Tag(name="old-name", color="red")
    tag_repo.add(tag)

    tag.name = "new-name"
    tag.color = "blue"
    tag_repo.update(tag)

    fetched = tag_repo.get(tag.id)
    assert fetched.name == "new-name"
    assert fetched.color == "blue"


def test_signal_color_round_trip(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="Repeater", frequency_hz=146_940_000, color="cyan")
    signal_repo.add(signal)

    fetched = signal_repo.get(signal.id)
    assert fetched.color == "cyan"

    fetched.color = "magenta"
    signal_repo.update(fetched)
    assert signal_repo.get(signal.id).color == "magenta"


def test_signal_satellite_round_trip(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="Meteor pass", frequency_hz=137_900_000, satellite="M2-3")
    signal_repo.add(signal)

    fetched = signal_repo.get(signal.id)
    assert fetched.satellite == "M2-3"

    fetched.satellite = "ISS"
    signal_repo.update(fetched)
    assert signal_repo.get(signal.id).satellite == "ISS"


def test_signal_satellite_defaults_to_none(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="No sat", frequency_hz=146_520_000)
    signal_repo.add(signal)
    assert signal_repo.get(signal.id).satellite is None


def test_signal_decoders_round_trip(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="Pager", frequency_hz=152_840_000, decoders=["pocsag", "flex"])
    signal_repo.add(signal)

    fetched = signal_repo.get(signal.id)
    assert fetched.decoders == ["pocsag", "flex"]

    fetched.decoders = ["aprs"]
    signal_repo.update(fetched)
    assert signal_repo.get(signal.id).decoders == ["aprs"]


def test_signal_antenna_round_trip(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="Discone hit", frequency_hz=146_520_000, antenna="Antenna B")
    signal_repo.add(signal)

    fetched = signal_repo.get(signal.id)
    assert fetched.antenna == "Antenna B"

    fetched.antenna = "Antenna C"
    signal_repo.update(fetched)
    assert signal_repo.get(signal.id).antenna == "Antenna C"


def test_signal_antenna_defaults_to_none(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="No antenna", frequency_hz=146_520_000)
    signal_repo.add(signal)
    assert signal_repo.get(signal.id).antenna is None


def test_migration_020_adds_antenna_column_keeps_existing_rows_null(db):
    # Migration 020 ran as part of the db fixture's connect(); a signal added
    # through older code (no antenna kwarg -- simulated by direct SQL insert
    # on a pre-existing row) keeps reading back as a NULL antenna, not an
    # error, so an upgrade never breaks existing libraries.
    signal_cols = {row["name"] for row in db.conn.execute("PRAGMA table_info(signals)").fetchall()}
    assert "antenna" in signal_cols

    signal_repo = SignalRepo(db)
    signal = Signal(name="Pre-migration", frequency_hz=146_520_000)
    signal_repo.add(signal)
    db.conn.execute("UPDATE signals SET antenna = NULL WHERE id = ?", (signal.id,))
    db.conn.commit()
    assert signal_repo.get(signal.id).antenna is None


def test_signal_decoders_defaults_to_empty_list(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="No decoders", frequency_hz=146_520_000)
    signal_repo.add(signal)
    assert signal_repo.get(signal.id).decoders == []


class _FakeDecodeEvent:
    """Duck-types msdr.decoder.backends.DecodeEvent -- see DecodeEventRepo's
    own docstring on why this repo doesn't import that (optional) package."""

    def __init__(
        self,
        *,
        ts: float,
        decoder: str,
        kind: str = "text",
        text: str = "",
        fields: dict | None = None,
        frequency_hz: int | None = None,
        signal_id: str | None = None,
    ) -> None:
        self.ts = ts
        self.decoder = decoder
        self.kind = kind
        self.text = text
        self.fields = fields or {}
        self.frequency_hz = frequency_hz
        self.signal_id = signal_id


def test_decode_event_repo_add_returns_stored_row_with_decoded_fields(db):
    repo = DecodeEventRepo(db)
    event = _FakeDecodeEvent(
        ts=1000.0,
        decoder="pocsag",
        text="ALPHA - Test Page #00042",
        fields={"address": 100001, "function": 2, "demod": "POCSAG1200"},
        frequency_hz=152_840_000,
    )
    row = repo.add(event)
    assert row["id"]
    assert row["decoder"] == "pocsag"
    assert row["text"] == "ALPHA - Test Page #00042"
    assert row["fields"] == {"address": 100001, "function": 2, "demod": "POCSAG1200"}
    assert row["frequency_hz"] == 152_840_000
    assert row["signal_id"] is None


def test_decode_event_repo_round_trips_integrity_and_provenance(db):
    from msdr.decoder.backends import DecodeEvent

    repo = DecodeEventRepo(db)
    event = DecodeEvent(
        ts=5.0,
        decoder="aprs",
        kind="text",
        text="N0CALL>APRS:hi",
        fields={},
        integrity="passed",
        integrity_note="AX.25 FCS verified by direwolf",
        source_id="replay:rec",
        generation=3,
        sample_index=123_456,
        run_index=2,
        analysis_run_id="run-1",
    )
    row = repo.add(event)
    expected = {
        "integrity": "passed",
        "integrity_note": "AX.25 FCS verified by direwolf",
        "source_id": "replay:rec",
        "generation": 3,
        "sample_index": 123_456,
        "run_index": 2,
        "analysis_run_id": "run-1",
    }
    assert {k: row[k] for k in expected} == expected
    assert {k: repo.get(row["id"])[k] for k in expected} == expected
    assert {k: repo.list()[0][k] for k in expected} == expected


def test_decode_event_repo_duck_typed_event_defaults_to_unknown_integrity(db):
    repo = DecodeEventRepo(db)
    row = repo.add(_FakeDecodeEvent(ts=1.0, decoder="pocsag", text="x"))
    stored = repo.get(row["id"])
    assert stored["integrity"] == "unknown"  # never an invented "passed"
    assert stored["source_id"] is None and stored["analysis_run_id"] is None


def test_decode_run_migration_preserves_existing_events(tmp_path):
    migrations = pathlib.Path(inspect.getfile(Database)).parent / "migrations"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for path in migrations.glob("*.sql"):
        if int(path.name.split("_", 1)[0]) <= 14:
            (old_migrations / path.name).write_text(path.read_text())
    path = tmp_path / "upgrade.db"
    old = Database(path, migrations_dir=old_migrations)
    old.connect()
    row = DecodeEventRepo(old).add(_FakeDecodeEvent(ts=1.0, decoder="pocsag", text="keep me"))
    old.close()
    current = Database(path)
    current.connect()
    try:
        restored = DecodeEventRepo(current).get(row["id"])
        assert restored["text"] == "keep me"
        assert restored["run_index"] is None
    finally:
        current.close()


def test_decode_event_repo_list_filters_by_decoder_and_q_newest_first(db):
    repo = DecodeEventRepo(db)
    repo.add(_FakeDecodeEvent(ts=1.0, decoder="pocsag", text="alpha message"))
    repo.add(_FakeDecodeEvent(ts=2.0, decoder="flex", text="beta message"))
    repo.add(_FakeDecodeEvent(ts=3.0, decoder="pocsag", text="gamma"))

    all_events = repo.list()
    assert [e["ts"] for e in all_events] == [3.0, 2.0, 1.0]

    pocsag_only = repo.list(decoder="pocsag")
    assert [e["decoder"] for e in pocsag_only] == ["pocsag", "pocsag"]

    matches = repo.list(q="beta")
    assert [e["text"] for e in matches] == ["beta message"]

    since = repo.list(since=2.0)
    assert [e["ts"] for e in since] == [3.0, 2.0]


def test_decode_event_repo_update_event_merges_fields_and_keeps_others(db):
    repo = DecodeEventRepo(db)
    row = repo.add(
        _FakeDecodeEvent(
            ts=1.0, decoder="wefax", kind="image", text="fax started",
            fields={"lpm": 120, "ioc": 576},
        )
    )

    updated = repo.update_event(
        row["id"], text="fax 120 lpm IOC 576 · 40 lines", fields={"lines": 40}
    )
    assert updated["text"] == "fax 120 lpm IOC 576 · 40 lines"
    # lpm/ioc from the original insert are kept; only "lines" was merged in.
    assert updated["fields"] == {"lpm": 120, "ioc": 576, "lines": 40}
    assert repo.get(row["id"]) == updated

    # A fields-only update leaves text untouched.
    updated2 = repo.update_event(row["id"], fields={"lines": 80})
    assert updated2["text"] == "fax 120 lpm IOC 576 · 40 lines"
    assert updated2["fields"]["lines"] == 80

    assert repo.update_event("does-not-exist", text="x") is None


def test_decode_event_repo_count_trim_and_clear(db):
    repo = DecodeEventRepo(db)
    for i in range(5):
        repo.add(_FakeDecodeEvent(ts=float(i), decoder="cw", text=str(i)))
    assert repo.count() == 5

    trimmed = repo.trim(3)
    assert trimmed == 2
    assert repo.count() == 3
    # The two oldest (ts=0, ts=1) were the ones dropped.
    assert sorted(e["ts"] for e in repo.list()) == [2.0, 3.0, 4.0]

    assert repo.trim(3) == 0  # already at the cap: no-op

    cleared = repo.clear()
    assert cleared == 3
    assert repo.count() == 0


def test_beacon_hit_repo_add_returns_stored_row(db):
    repo = BeaconHitRepo(db)
    row = repo.add(
        {
            "ts": 1000.0,
            "beacon": "4U1UN",
            "frequency_hz": 14_100_000,
            "snr_db": 12.5,
            "decoded": True,
            "probable": False,
            "mode": "ncdxf",
        }
    )
    assert row["id"]
    assert row["beacon"] == "4U1UN"
    assert row["frequency_hz"] == 14_100_000
    assert row["snr_db"] == 12.5
    assert row["decoded"] is True
    assert row["probable"] is False
    assert row["mode"] == "ncdxf"


def test_beacon_hit_repo_add_defaults_decoded_and_probable_false(db):
    repo = BeaconHitRepo(db)
    row = repo.add({"ts": 1.0, "beacon": "VE8AT", "frequency_hz": 14_100_000, "mode": "ncdxf"})
    assert row["decoded"] is False
    assert row["probable"] is False
    assert row["snr_db"] is None


def test_beacon_hit_repo_list_filters_and_orders_newest_first(db):
    repo = BeaconHitRepo(db)
    repo.add({"ts": 1.0, "beacon": "4U1UN", "frequency_hz": 14_100_000, "mode": "ncdxf"})
    repo.add({"ts": 2.0, "beacon": "VE8AT", "frequency_hz": 14_100_000, "mode": "ncdxf"})
    repo.add({"ts": 3.0, "beacon": "4U1UN", "frequency_hz": 18_110_000, "mode": "ncdxf"})

    all_hits = repo.list()
    assert [h["ts"] for h in all_hits] == [3.0, 2.0, 1.0]

    by_beacon = repo.list(beacon="4U1UN")
    assert [h["ts"] for h in by_beacon] == [3.0, 1.0]

    by_freq = repo.list(frequency_hz=14_100_000)
    assert [h["ts"] for h in by_freq] == [2.0, 1.0]

    since = repo.list(since=2.0)
    assert [h["ts"] for h in since] == [3.0, 2.0]


def test_beacon_hit_repo_summary_per_beacon_and_band(db):
    repo = BeaconHitRepo(db)
    repo.add(
        {
            "ts": 1.0,
            "beacon": "4U1UN",
            "frequency_hz": 14_100_000,
            "snr_db": 5.0,
            "decoded": False,
            "mode": "ncdxf",
        }
    )
    repo.add(
        {
            "ts": 2.0,
            "beacon": "4U1UN",
            "frequency_hz": 14_100_000,
            "snr_db": 9.0,
            "decoded": True,
            "mode": "ncdxf",
        }
    )
    repo.add(
        {
            "ts": 3.0,
            "beacon": "VE8AT",
            "frequency_hz": 18_110_000,
            "snr_db": 3.0,
            "mode": "ncdxf",
        }
    )

    summary = repo.summary()
    by_key = {(s["beacon"], s["band_hz"]): s for s in summary}

    un = by_key[("4U1UN", 14_100_000)]
    assert un["hits"] == 2
    assert un["last_heard"] == 2.0
    assert un["last_snr_db"] == 9.0
    assert un["decoded"] is True  # any hit decoded=True counts

    ve = by_key[("VE8AT", 18_110_000)]
    assert ve["hits"] == 1
    assert ve["last_heard"] == 3.0
    assert ve["last_snr_db"] == 3.0
    assert ve["decoded"] is False


def test_beacon_hit_repo_clear(db):
    repo = BeaconHitRepo(db)
    for i in range(3):
        repo.add({"ts": float(i), "beacon": "4U1UN", "frequency_hz": 14_100_000, "mode": "ncdxf"})
    assert len(repo.list()) == 3

    cleared = repo.clear()
    assert cleared == 3
    assert repo.list() == []


def _bulk_signals(repo: SignalRepo, count: int, *, base_hz: int) -> None:
    conn = repo.db.conn
    with conn:
        conn.executemany(
            "INSERT INTO signals (id, name, status, frequency_hz, heard_count, "
            "created_at, updated_at) VALUES (?, ?, 'unknown', ?, 0, ?, ?)",
            [
                (f"bulk-{i:05d}", f"Filler {i}", base_hz + i * 100, f"2024-01-01T00:00:{i:05d}", "")
                for i in range(count)
            ],
        )


def test_near_and_search_find_matches_past_the_page_limit(db):
    """1,001 signals: the interesting one is created last, so any filtering
    that happens after a 500/1000-row page would never see it."""
    repo = SignalRepo(db)
    tags = TagRepo(db)
    _bulk_signals(repo, 1001, base_hz=100_000_000)

    tag = Tag(name="beacon")
    tags.add(tag)
    needle = Signal(
        name="Needle", frequency_hz=145_800_000, station="Lighthouse", tag_ids=[tag.id]
    )
    repo.add(needle)

    assert len(repo.list(limit=500)) == 500  # the page the old code filtered
    assert [s.id for s in repo.near(145_800_000, 1000)] == [needle.id]
    assert [s.id for s in repo.search("needle")] == [needle.id]
    assert [s.id for s in repo.search("lighthouse")] == [needle.id]
    assert [s.id for s in repo.search("beacon")] == [needle.id]
    # A query that parses as a frequency still matches that frequency exactly.
    assert needle.id in [s.id for s in repo.search("145.800")]


def test_near_orders_nearest_first_and_respects_radius(db):
    repo = SignalRepo(db)
    far = Signal(name="Far", frequency_hz=145_805_000)
    close = Signal(name="Close", frequency_hz=145_800_500)
    outside = Signal(name="Outside", frequency_hz=145_900_000)
    for signal in (far, close, outside):
        repo.add(signal)

    assert [s.id for s in repo.near(145_800_000, 10_000)] == [close.id, far.id]


def test_query_filters_sorts_pages_and_avoids_tag_n_plus_one(db, monkeypatch):
    repo = SignalRepo(db)
    tags = TagRepo(db)
    tag = Tag(name="beacon")
    tags.add(tag)
    a = Signal(name="Alpha", frequency_hz=100_000_000, station="Notes here", tag_ids=[tag.id])
    b = Signal(name="Bravo", frequency_hz=200_000_000, status="identified")
    c = Signal(name="Charlie", frequency_hz=300_000_000)
    for signal in (a, b, c):
        repo.add(signal)

    # text match across name/station, plus status/tag/band filters.
    results, total = repo.query(q="alpha")
    assert total == 1 and results[0].id == a.id

    results, total = repo.query(status="identified")
    assert total == 1 and results[0].id == b.id

    results, total = repo.query(tag_id=tag.id)
    assert total == 1 and results[0].id == a.id

    results, total = repo.query(lo_hz=150_000_000)
    assert {s.id for s in results} == {b.id, c.id}
    assert total == 2

    # sort/order plus paging: total reflects the whole filtered set, not the page.
    results, total = repo.query(sort="name", order="desc", limit=2, offset=0)
    assert [s.name for s in results] == ["Charlie", "Bravo"]
    assert total == 3
    results, total = repo.query(sort="name", order="desc", limit=2, offset=2)
    assert [s.name for s in results] == ["Alpha"]

    # Tags for the page come from one batched call (_tag_ids_many), not the
    # per-signal _tag_ids() that list()/get() use.
    calls = 0
    real_tag_ids = SignalRepo._tag_ids

    def counting_tag_ids(self, signal_id):
        nonlocal calls
        calls += 1
        return real_tag_ids(self, signal_id)

    monkeypatch.setattr(SignalRepo, "_tag_ids", counting_tag_ids)
    repo.query(limit=50)
    assert calls == 0


def test_query_rejects_unknown_sort_or_order(db):
    repo = SignalRepo(db)
    with pytest.raises(ValueError):
        repo.query(sort="bogus")
    with pytest.raises(ValueError):
        repo.query(order="bogus")


def test_library_undo_record_latest_and_cap_at_50(db):
    undo = LibraryUndoRepo(db)
    for i in range(55):
        undo.record(f"op {i}", [(f"sig-{i}", None)])

    assert undo.latest()["label"] == "op 54"
    rows = db.conn.execute("SELECT COUNT(*) AS n FROM library_undo").fetchone()["n"]
    assert rows == 50


def test_library_undo_pop_and_restore_recreates_delete_with_tags(db):
    signal_repo = SignalRepo(db)
    tags = TagRepo(db)
    tag = Tag(name="beacon")
    tags.add(tag)
    signal = Signal(name="Beacon", frequency_hz=145_800_000, tag_ids=[tag.id])
    signal_repo.add(signal)

    undo = LibraryUndoRepo(db)
    undo.record("delete Beacon", [(signal.id, signal)])
    signal_repo.delete(signal.id)
    assert signal_repo.get(signal.id) is None

    label = undo.pop_and_restore(signal_repo)
    assert label == "delete Beacon"
    restored = signal_repo.get(signal.id)
    assert restored.name == "Beacon"
    assert restored.tag_ids == [tag.id]
    assert undo.latest() is None


def test_library_undo_pop_and_restore_delete_keeps_journal_link(db):
    # journal_entries.signal_id has no FK to signals(id): a restored delete
    # must land back under the *same* id so pre-existing journal rows that
    # name it still resolve after the undo.
    signal_repo = SignalRepo(db)
    journal_repo = JournalRepo(db)
    signal = Signal(name="Beacon", frequency_hz=145_800_000)
    signal_repo.add(signal)
    journal_repo.add(JournalEntry(kind="capture", signal_id=signal.id))

    undo = LibraryUndoRepo(db)
    undo.record("delete Beacon", [(signal.id, signal)])
    signal_repo.delete(signal.id)

    day = journal_repo.days()[0]
    entries_mid_delete = journal_repo.list_day(day)
    assert any(e.signal_id == signal.id for e in entries_mid_delete)

    undo.pop_and_restore(signal_repo)
    assert signal_repo.get(signal.id).name == "Beacon"
    entries_after = journal_repo.list_day(day)
    assert any(e.signal_id == signal.id for e in entries_after)


def test_library_undo_pop_and_restore_create_deletes_signal(db):
    signal_repo = SignalRepo(db)
    signal = Signal(name="New", frequency_hz=145_800_000)
    signal_repo.add(signal)

    undo = LibraryUndoRepo(db)
    undo.record("create New", [(signal.id, None)])

    label = undo.pop_and_restore(signal_repo)
    assert label == "create New"
    assert signal_repo.get(signal.id) is None


def test_library_undo_pop_and_restore_preserves_scanner_skip_flag(db):
    # Claim: the before-image only carries (list_id, position), so restoring
    # a signal (delete-then-insert cascades list_items) always comes back
    # with skip=0 -- even when it was flagged "skip always" at record() time
    # and the op being undone (a colour change) never touched that flag.
    signal_repo = SignalRepo(db)
    lists = ListRepo(db)
    signal = Signal(name="Beacon", frequency_hz=145_800_000)
    signal_repo.add(signal)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [signal.id])
    lists.set_item_skip(lst["id"], signal.id, True)

    undo = LibraryUndoRepo(db)
    undo.record("set color Beacon", [(signal.id, signal)])
    signal.color = "red"
    signal_repo.update(signal)

    undo.pop_and_restore(signal_repo)
    assert lists.item_rows(lst["id"])[0]["skip"] is True


def test_library_undo_pop_and_restore_skips_membership_to_deleted_list(db):
    # Claim: a membership recorded against a list that's since been deleted
    # must not be re-inserted (FK violation) -- it should just be dropped so
    # the rest of the restore (and the pop off the undo stack) still succeeds.
    signal_repo = SignalRepo(db)
    lists = ListRepo(db)
    signal = Signal(name="Beacon", frequency_hz=145_800_000)
    signal_repo.add(signal)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [signal.id])

    undo = LibraryUndoRepo(db)
    undo.record("set color Beacon", [(signal.id, signal)])
    signal.color = "red"
    signal_repo.update(signal)
    lists.delete(lst["id"])

    label = undo.pop_and_restore(signal_repo)  # must not raise
    assert label == "set color Beacon"
    assert signal_repo.get(signal.id).color is None
    assert undo.latest() is None  # the entry was popped, not left wedged on top


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


def test_find_repo_add_or_merge_creates_new_row(db):
    repo = FindRepo(db)
    row, created = repo.add_or_merge(_find())
    assert created is True
    assert row["seen_count"] == 1
    assert row["first_ts"] == 1000.0
    assert row["last_ts"] == 1000.0
    assert row["frequency_hz"] == 146_520_000
    assert row["status"] == "new"
    assert row["classification"] == {"source": "identify", "bandplan": {"name": "2m"}}


def test_find_repo_add_or_merge_merges_within_tolerance(db):
    repo = FindRepo(db)
    first, _ = repo.add_or_merge(_find(ts=1000.0, peak_snr_db=8.0))
    # 3 kHz away -- within the default 6 kHz tolerance -- must merge, not create a new row.
    second, created = repo.add_or_merge(
        _find(ts=1010.0, frequency_hz=146_523_000, peak_snr_db=14.0, duration_s=1.0)
    )
    assert created is False
    assert second["id"] == first["id"]
    assert second["seen_count"] == 2
    assert second["last_ts"] == 1010.0
    assert second["peak_snr_db"] == 14.0  # max of the two
    assert second["last_duration_s"] == 1.0  # latest, not max
    assert repo.list()[0]["seen_count"] == 2  # one merged row, not two


def test_find_repo_add_or_merge_creates_separate_row_outside_tolerance(db):
    repo = FindRepo(db)
    repo.add_or_merge(_find(frequency_hz=146_520_000))
    _, created = repo.add_or_merge(_find(frequency_hz=146_600_000))
    assert created is True
    assert len(repo.list()) == 2


def test_find_repo_add_or_merge_merges_classification_latest_non_null_over_old(db):
    repo = FindRepo(db)
    repo.add_or_merge(
        _find(classification={"source": "identify", "bandplan": {"name": "2m"}, "activity": None})
    )
    merged, _ = repo.add_or_merge(
        _find(
            frequency_hz=146_520_500,
            classification={"source": "survey", "activity": {"level": "busy"}, "bandplan": None},
        )
    )
    # New non-null fields win (source, activity); fields the new find left
    # null keep their old value (bandplan) rather than being wiped out.
    assert merged["classification"] == {
        "source": "survey",
        "bandplan": {"name": "2m"},
        "activity": {"level": "busy"},
    }


def test_find_repo_add_or_merge_keeps_status_ignored_or_saved(db):
    repo = FindRepo(db)
    row, _ = repo.add_or_merge(_find())
    repo.set_status(row["id"], "ignored")
    merged, created = repo.add_or_merge(_find(ts=2000.0))
    assert created is False
    assert merged["status"] == "ignored"


def test_find_repo_list_filters_by_status_and_q_newest_first(db):
    repo = FindRepo(db)
    a, _ = repo.add_or_merge(_find(ts=1.0, frequency_hz=146_520_000, label="APRS"))
    b, _ = repo.add_or_merge(_find(ts=2.0, frequency_hz=440_000_000, label="DMR"))
    repo.set_status(a["id"], "reviewed")

    assert [f["id"] for f in repo.list()] == [b["id"], a["id"]]
    assert [f["id"] for f in repo.list(status="reviewed")] == [a["id"]]
    assert [f["id"] for f in repo.list(q="DMR")] == [b["id"]]


def test_find_repo_get_set_status_delete(db):
    repo = FindRepo(db)
    row, _ = repo.add_or_merge(_find())
    assert repo.get(row["id"])["id"] == row["id"]

    updated = repo.set_status(row["id"], "saved", note="kept")
    assert updated["status"] == "saved"
    assert updated["note"] == "kept"

    assert repo.delete(row["id"]) is True
    assert repo.get(row["id"]) is None
    assert repo.delete(row["id"]) is False


def test_find_repo_ignored_frequencies_and_counts(db):
    repo = FindRepo(db)
    a, _ = repo.add_or_merge(_find(frequency_hz=146_520_000))
    b, _ = repo.add_or_merge(_find(frequency_hz=440_000_000))
    repo.set_status(a["id"], "ignored")

    assert repo.ignored_frequencies() == [146_520_000]
    assert repo.counts() == {"ignored": 1, "new": 1}


def test_signal_list_breaks_created_at_ties_by_insertion_order(db):
    # created_at has one-second resolution, so signals saved together tie; the
    # list must then keep insertion order (SKY's "first bound satellite").
    repo = SignalRepo(db)
    repo.add(Signal(name="ISS packet", frequency_hz=145_825_000, satellite="ISS"))
    repo.add(Signal(name="AO-73 downlink", frequency_hz=145_960_000, satellite="AO-73"))
    db.conn.execute("UPDATE signals SET created_at = (SELECT MIN(created_at) FROM signals)")
    assert [s.name for s in repo.list()] == ["ISS packet", "AO-73 downlink"]


def test_decode_event_repo_picks_up_provenance_column_added_after_first_use(db):
    from msdr.decoder.backends import DecodeEvent

    repo = DecodeEventRepo(db)
    db.conn.execute("ALTER TABLE decode_events DROP COLUMN analysis_run_id")
    repo.add(_FakeDecodeEvent(ts=1.0, decoder="pocsag", text="before"))
    db.conn.execute("ALTER TABLE decode_events ADD COLUMN analysis_run_id TEXT")
    event = DecodeEvent(
        ts=2.0, decoder="aprs", kind="text", text="after", fields={}, analysis_run_id="run-2"
    )
    row = repo.add(event)
    assert repo.get(row["id"])["analysis_run_id"] == "run-2"
