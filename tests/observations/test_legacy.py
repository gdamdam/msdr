import inspect
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from msdr.observations.legacy import backup_db, import_legacy, legacy_import_done
from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database
from msdr.storage.repos import DecodeEventRepo, FindRepo

_MIGRATIONS = Path(inspect.getfile(Database)).parent / "migrations"


def _event(ts, freq, decoder="pocsag"):
    return SimpleNamespace(ts=ts, frequency_hz=freq, decoder=decoder, kind="msg", text="x")


@pytest.fixture
def legacy_db(tmp_path):
    """A DB at schema 011 holding finds + decode_events, then reopened so
    012 applies over existing data (as on a real upgrade)."""
    old = tmp_path / "old_migrations"
    old.mkdir()
    for p in sorted(_MIGRATIONS.glob("*.sql")):
        if int(p.name.split("_", 1)[0]) <= 11:
            shutil.copy(p, old / p.name)
    path = tmp_path / "msdr.db"
    pre = Database(path, migrations_dir=old)
    pre.connect()
    finds = FindRepo(pre)
    finds.add_or_merge({"ts": 100.0, "frequency_hz": 162_400_000, "peak_snr_db": 20.0})
    finds.add_or_merge({"ts": 400.0, "frequency_hz": 162_401_000, "peak_snr_db": 25.0})
    finds.add_or_merge({"ts": 50.0, "frequency_hz": 146_520_000})
    events = DecodeEventRepo(pre)
    # pocsag: two events 100 s apart (one group), one 1000 s later (new
    # group), one on another channel at the same time (new group).
    for ts, freq in ((1_000.0, 152_007_500), (1_100.0, 152_010_000), (2_100.0, 152_007_500)):
        events.add(_event(ts, freq))
    events.add(_event(1_050.0, 157_450_000))
    events.add(_event(1_000.0, 144_390_000, decoder="aprs"))
    pre.close()

    db = Database(path)
    db.connect()
    yield db
    db.close()


def test_migration_applies_over_existing_data(legacy_db):
    cols = {r["name"] for r in legacy_db.conn.execute("PRAGMA table_info(finds)")}
    assert "observation_id" in cols
    assert legacy_db.conn.execute("SELECT COUNT(*) FROM decode_events").fetchone()[0] == 5


def test_import_legacy_counts_links_idempotent(legacy_db):
    repo = ObservationRepo(legacy_db)
    counts = import_legacy(legacy_db, repo)
    assert counts == {
        "finds": 2,
        "decode_events": 5,
        "decode_groups": 4,
        "claims": 4,
        "skipped": False,
    }
    assert legacy_import_done(legacy_db)
    conn = legacy_db.conn
    assert (
        conn.execute("SELECT COUNT(*) FROM finds WHERE observation_id IS NULL").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM decode_events WHERE observation_id IS NULL").fetchone()[
            0
        ]
        == 0
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    again = import_legacy(legacy_db, repo)
    assert again["skipped"] is True and again["finds"] == 0 and again["decode_events"] == 0
    assert len(repo.list(limit=100)) == 6


def test_import_uses_only_row_times(legacy_db):
    repo = ObservationRepo(legacy_db)
    import_legacy(legacy_db, repo)
    wx = repo.list(frequency_hz=162_400_000, tolerance_hz=2000)
    [obs] = wx
    assert obs.legacy and obs.closed
    assert (obs.start_ts, obs.end_ts) == (100.0, 400.0)
    assert obs.provenance == "migrated: finds (frequency aggregate, seen_count=2)"
    [pager] = [o for o in repo.list(frequency_hz=152_008_000) if o.start_ts == 1_000.0]
    assert pager.end_ts == 1_100.0
    assert pager.provenance == "migrated: decode_events (2 events)"
    assert len(repo.decode_event_ids(pager.id)) == 2
    [claim] = repo.claims(pager.id)
    # pocsag's backend exposes no integrity flag: never "validated".
    assert (claim.evidence_type, claim.integrity) == ("decoder_output", "unavailable")
    [aprs] = repo.list(frequency_hz=144_390_000)
    assert repo.claims(aprs.id)[0].evidence_type == "validated_decode"


def test_backup_db(tmp_path):
    assert backup_db(":memory:") is None
    path = tmp_path / "msdr.db"
    db = Database(path)
    db.connect()
    db.conn.execute("INSERT INTO settings (key, value_json) VALUES ('k', '1')")
    db.conn.commit()
    dest = backup_db(path, now=datetime(2026, 9, 27, 8, 30, 0))
    db.close()
    assert dest == tmp_path / "msdr.db.bak-20260927-083000"
    # WAL-resident commits must be in the copy.
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT value_json FROM settings WHERE key = 'k'").fetchone() == ("1",)
    copy.close()
