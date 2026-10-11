import json
import zipfile
from pathlib import Path

import pytest

from msdr.contracts.evidence import Claim
from msdr.library.models import Signal
from msdr.observations.models import Observation, RecordingReference, Session
from msdr.observations.repo import ObservationRepo
from msdr.storage.backup import create_backup, create_evidence_bundle, restore_backup
from msdr.storage.db import Database
from msdr.storage.repos import SignalRepo


@pytest.fixture
def dbs():
    opened = []

    def make():
        db = Database(":memory:")
        db.connect()
        opened.append(db)
        return db

    yield make
    for db in opened:
        db.close()


def test_full_backup_restores_observation_history_and_scan_rows(tmp_path, dbs):
    source = dbs()
    signal = Signal(name="Weather", frequency_hz=162_550_000)
    SignalRepo(source).add(signal)
    repo = ObservationRepo(source)
    session = repo.open_session(Session(source_id="live:test", mode="live", started_at=1.0))
    observation = repo.add(
        Observation(
            signal_id=signal.id,
            session_id=session.id,
            reported_hz=signal.frequency_hz,
            notes="storm warning",
        )
    )
    repo.add_claim(
        Claim(
            observation_id=observation.id,
            subject="protocol",
            value=" SAME",
            evidence_type="decoder_output",
            integrity="unknown",
            producer="decoder.same",
            producer_version="2.4",
        )
    )
    source.conn.execute(
        "INSERT INTO finds (id, first_ts, last_ts, frequency_hz) VALUES ('f1', 1, 2, 162550000)"
    )
    source.conn.execute(
        "INSERT INTO decode_events (id, ts, decoder, kind, observation_id) "
        "VALUES ('d1', 1, 'same', 'message', ?)",
        (observation.id,),
    )
    source.conn.execute(
        "INSERT INTO survey_band_scans (id, config_json, state, started_at, updated_at) "
        "VALUES ('b1', '{}', 'done', 1, 2)"
    )
    source.conn.commit()

    path = tmp_path / "backup.msdr"
    manifest = create_backup(source, path)
    assert manifest["format"] == "msdr/backup/1"
    target = dbs()
    report = restore_backup(target, path)
    assert report.verified
    assert report.table_counts["observations"] == 1
    assert target.conn.execute("SELECT notes FROM observations").fetchone()[0] == "storm warning"
    assert target.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    assert target.conn.execute("SELECT id FROM finds").fetchone()[0] == "f1"
    assert target.conn.execute("SELECT id FROM decode_events").fetchone()[0] == "d1"
    assert target.conn.execute("SELECT id FROM survey_band_scans").fetchone()[0] == "b1"


def test_backup_rejects_tampering_before_mutating_target(tmp_path, dbs):
    source = dbs()
    path = tmp_path / "backup.zip"
    create_backup(source, path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(tampered, "w") as archive:
        for entry in original.infolist():
            archive.writestr(
                entry.filename,
                b"{}" if entry.filename == "database.json" else original.read(entry.filename),
            )
    path = tampered
    target = dbs()
    target.conn.execute("INSERT INTO settings (key, value_json) VALUES ('keep', '1')")
    target.conn.commit()
    with pytest.raises(ValueError, match="checksum"):
        restore_backup(target, path)
    assert target.conn.execute("SELECT key FROM settings").fetchone()[0] == "keep"


def test_invalid_foreign_key_restore_rolls_back_existing_database(tmp_path, dbs):
    source = dbs()
    source.conn.execute("PRAGMA foreign_keys=OFF")
    source.conn.execute(
        "INSERT INTO claims (id, observation_id, subject, value, evidence_type, integrity, "
        "producer, created_at) VALUES ('bad', 'missing-observation', 'protocol', 'x', "
        "'decoder_output', 'unknown', 'decoder.test', 1)"
    )
    source.conn.commit()
    path = tmp_path / "invalid-fk.zip"
    create_backup(source, path)

    target = dbs()
    target.conn.execute("INSERT INTO settings (key, value_json) VALUES ('keep', '1')")
    target.conn.commit()
    with pytest.raises(ValueError, match="foreign-key"):
        restore_backup(target, path)
    assert target.conn.execute("SELECT key FROM settings").fetchone()[0] == "keep"


def test_restore_rejects_malicious_media_remap_before_mutating_database(tmp_path, dbs):
    source = dbs()
    signal = Signal(name="Media", frequency_hz=10)
    SignalRepo(source).add(signal)
    obs = ObservationRepo(source).add(Observation(signal_id=signal.id))
    media = tmp_path / "clip.bin"
    media.write_bytes(b"recording")
    ObservationRepo(source).add_ref(
        RecordingReference(observation_id=obs.id, path=str(media), name="clip")
    )
    original = tmp_path / "original.zip"
    create_backup(source, original, include_media=True, media_roots=[tmp_path])

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(malicious, "w") as output:
        manifest = json.loads(source_zip.read("manifest.json"))
        manifest["path_remaps"][str(media.resolve())] = "../../outside"
        output.writestr(
            "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
        for entry in source_zip.infolist():
            if entry.filename != "manifest.json":
                output.writestr(entry.filename, source_zip.read(entry.filename))

    target = dbs()
    target.conn.execute("INSERT INTO settings (key, value_json) VALUES ('keep', '1')")
    target.conn.commit()
    with pytest.raises(ValueError, match="path remap"):
        restore_backup(target, malicious, media_root=tmp_path / "restored")
    assert target.conn.execute("SELECT key FROM settings").fetchone()[0] == "keep"


def test_backup_media_is_limited_to_explicit_roots(tmp_path, dbs):
    source = dbs()
    signal = Signal(name="Media", frequency_hz=10)
    SignalRepo(source).add(signal)
    obs = ObservationRepo(source).add(Observation(signal_id=signal.id))
    recording = tmp_path / "recording"
    recording.mkdir()
    inside = recording / "run-00.sigmf-data"
    (recording / "run-00.sigmf-meta").write_text("{}")
    outside = tmp_path.parent / "outside.sigmf-data"
    inside.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    ObservationRepo(source).add_ref(
        RecordingReference(observation_id=obs.id, path=str(recording), name="inside")
    )
    ObservationRepo(source).add_ref(
        RecordingReference(observation_id=obs.id, path=str(outside), name="outside")
    )
    path = tmp_path / "with-media.zip"
    manifest = create_backup(source, path, include_media=True, media_roots=[tmp_path])
    assert len(manifest["media"]) == 2
    target = dbs()
    report = restore_backup(target, path, media_root=tmp_path / "restored")
    assert report.media_count == 2
    restored_dir = Path(
        target.conn.execute("SELECT path FROM recording_refs WHERE name='inside'").fetchone()[0]
    )
    assert (restored_dir / inside.name).read_bytes() == b"inside"
    assert (restored_dir / "run-00.sigmf-meta").is_file()


def test_evidence_bundle_contains_notes_decoder_version_and_bounded_iq(tmp_path, dbs):
    db = dbs()
    signal = Signal(name="Beacon", frequency_hz=144_800_000)
    SignalRepo(db).add(signal)
    repo = ObservationRepo(db)
    obs = repo.add(
        Observation(signal_id=signal.id, notes="operator note", reported_hz=signal.frequency_hz)
    )
    repo.add_claim(
        Claim(
            observation_id=obs.id,
            subject="protocol",
            value="test",
            evidence_type="decoder_output",
            integrity="unknown",
            producer="decoder.test",
            producer_version="1.2",
        )
    )
    iq = tmp_path / "sample.sigmf-data"
    iq.write_bytes(bytes(range(80)))
    iq.with_suffix(".sigmf-meta").write_text(
        json.dumps(
            {
                "global": {"core:datatype": "ci16_le", "core:sample_rate": 250_000},
                "captures": [
                    {
                        "core:sample_start": 0,
                        "core:frequency": 144_800_000,
                        "core:datetime": "2026-01-01T00:00:00Z",
                    },
                    {"core:sample_start": 4, "core:frequency": 145_000_000},
                    {"core:sample_start": 8, "core:frequency": 146_000_000},
                ],
                "annotations": [
                    {"core:sample_start": 3, "core:label": "inside"},
                    {"core:sample_start": 9, "core:label": "outside"},
                ],
            }
        )
    )
    repo.add_ref(
        RecordingReference(
            observation_id=obs.id, path=str(iq), name="clip", sample_start=2, sample_end=10
        )
    )
    path = tmp_path / "evidence.zip"
    payload = create_evidence_bundle(db, signal.id, path, max_iq_samples=3)
    assert payload["observations"][0]["notes"] == "operator note"
    assert payload["decoder_versions"] == [{"producer": "decoder.test", "version": "1.2"}]
    with zipfile.ZipFile(path) as archive:
        exported = json.loads(archive.read("evidence.json"))
        clip = archive.read("iq/clip.sigmf-data")
        clip_meta = json.loads(archive.read("iq/clip.sigmf-meta"))
    assert exported["iq_clip"]["bytes"] == 12
    assert clip == iq.read_bytes()[8:20]
    assert clip_meta["global"]["core:sample_rate"] == 250_000
    assert clip_meta["captures"][0]["core:frequency"] == 144_800_000
    assert clip_meta["captures"][0]["core:datetime"] == "2026-01-01T00:00:00.000008Z"
    assert [c["core:frequency"] for c in clip_meta["captures"]] == [144_800_000, 145_000_000]
    assert [a["core:label"] for a in clip_meta["annotations"]] == ["inside"]
    assert exported["iq_clip"]["source_ref_id"]
    assert exported["iq_clip"]["sample_start"] == 2
