import pytest

from msdr.contracts.evidence import Claim, Measurement
from msdr.observations.models import Observation, RecordingReference, Session
from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    return ObservationRepo(db)


def _claim(obs_id, **kw):
    base = {
        "subject": "modulation",
        "value": "fm_fsk",
        "evidence_type": "iq_estimate",
        "producer": "survey.analyzer",
        "observation_id": obs_id,
    }
    return Claim(**(base | kw))


def test_session_roundtrip(repo):
    s = repo.open_session(
        Session(source_id="live:sdrconnect", mode="live", started_at=10.0, receiver={"g": 1})
    )
    repo.close_session(s.id, 20.0)
    got = repo.get_session(s.id)
    assert got.ended_at == 20.0 and got.receiver == {"g": 1}
    assert repo.get_session("nope") is None


def test_observation_roundtrip_update_close(repo):
    m = Measurement(name="snr", value=12.0, unit="dB", method="spectrum_display_snr")
    obs = repo.add(Observation(reported_hz=146_520_000, measurements=[m], start_ts=5.0))
    assert obs.created_at is not None
    got = repo.get(obs.id)
    assert got.measurements[0].value == 12.0 and not got.closed
    repo.update(got.model_copy(update={"capture_quality": ["quiet"], "measured_hz": 146_520_100}))
    got = repo.get(obs.id)
    assert got.capture_quality == ["quiet"] and got.measured_hz == 146_520_100
    assert got.reported_hz == 146_520_000
    repo.close(obs.id, 9.0, None)
    repo.close(obs.id, None, None)  # a time-less close keeps the recorded end
    got = repo.get(obs.id)
    assert got.closed and got.end_ts == 9.0


def test_link_signal_and_list_filters_and_order(repo):
    a = repo.add(Observation(reported_hz=100_000_000, start_ts=1.0, end_ts=2.0))
    b = repo.add(Observation(reported_hz=100_004_000, start_ts=5.0, legacy=True))
    repo.add(Observation(reported_hz=200_000_000, start_ts=9.0))
    repo.link_signal(a.id, "sig1")
    near = repo.list(frequency_hz=100_000_000)
    assert [o.id for o in near] == [b.id, a.id]  # newest first
    assert [o.id for o in repo.list(frequency_hz=100_000_000, include_legacy=False)] == [a.id]
    assert [o.id for o in repo.list(signal_id="sig1")] == [a.id]
    repo.link_signal(a.id, None)
    assert repo.list(signal_id="sig1") == []
    assert len(repo.list(limit=2)) == 2


def test_open_near_newest_open_in_session(repo):
    s = repo.open_session(Session(source_id="x", mode="live", started_at=0.0))
    old = repo.add(Observation(session_id=s.id, reported_hz=1_000_000, start_ts=1.0))
    new = repo.add(Observation(session_id=s.id, reported_hz=1_003_000, start_ts=2.0))
    repo.add(Observation(reported_hz=1_000_000, start_ts=3.0))  # other (no) session
    assert repo.open_near(1_000_000, 6000, s.id).id == new.id
    repo.close(new.id, 4.0, None)
    assert repo.open_near(1_000_000, 6000, s.id).id == old.id
    assert repo.open_near(2_000_000, 6000, s.id) is None


def test_claims_append_only_supersede(repo):
    obs = repo.add(Observation(reported_hz=1))
    c1 = repo.add_claim(_claim(obs.id))
    c2 = repo.supersede(c1.claim_id, _claim(None, value="ook"))
    assert c2.observation_id == obs.id
    allc = repo.claims(obs.id)
    assert [c.claim_id for c in allc] == [c1.claim_id, c2.claim_id]
    assert allc[0].superseded_by == c2.claim_id
    assert [c.value for c in repo.claims(obs.id, include_historical=False)] == ["ook"]
    with pytest.raises(ValueError):
        repo.add_claim(_claim(None))


def test_refs_and_mark_unavailable(repo):
    obs = repo.add(Observation(reported_hz=1))
    repo.add_ref(RecordingReference(observation_id=obs.id, path="/r/a.wav", name="a"))
    assert repo.mark_unavailable("/r/a.wav") == 1
    assert repo.mark_unavailable("/r/a.wav") == 0
    assert repo.refs(obs.id)[0].available is False


def test_decode_event_links(db, repo):
    obs = repo.add(Observation(reported_hz=1))
    db.conn.execute(
        "INSERT INTO decode_events (id, ts, decoder, kind) VALUES ('e1', 1.0, 'aprs', 'msg')"
    )
    repo.link_decode_event("e1", obs.id)
    assert repo.decode_event_ids(obs.id) == ["e1"]


def test_summary_context_only_is_not_identity(repo):
    obs = repo.add(Observation(reported_hz=162_400_000))
    repo.add_claim(
        _claim(
            obs.id,
            subject="service",
            value="NOAA WX",
            evidence_type="allocation",
            integrity="not_applicable",
        )
    )
    repo.add_claim(
        _claim(
            obs.id,
            subject="station",
            value="KEC49",
            evidence_type="schedule",
            integrity="not_applicable",
        )
    )
    s = repo.summary(obs)
    assert s["evidence_state"] == "context"
    assert s["identity_established"] is False
    assert len(s["claims"]) == 2 and all(c["historical"] is False for c in s["claims"])
    assert s["refs"] == [] and s["decode_event_ids"] == []


def test_observation_ids_by_ref_path_filters_by_recording_and_provenance(repo):
    a = repo.add(Observation(provenance="bookmark", reported_hz=1, start_ts=10.0))
    b = repo.add(Observation(provenance="bookmark", reported_hz=1, start_ts=20.0))
    other = repo.add(Observation(provenance="survey label", reported_hz=1, start_ts=30.0))
    for obs in (a, b, other):
        repo.add_ref(RecordingReference(observation_id=obs.id, path="/rec/x", name="x"))
    elsewhere = repo.add(Observation(provenance="bookmark", reported_hz=1, start_ts=40.0))
    repo.add_ref(RecordingReference(observation_id=elsewhere.id, path="/rec/y", name="y"))

    assert repo.observation_ids_by_ref_path("/rec/x", provenance="bookmark") == [b.id, a.id]
    assert set(repo.observation_ids_by_ref_path("/rec/x")) == {a.id, b.id, other.id}
    assert repo.observation_ids_by_ref_path("/rec/none") == []


def test_notes_default_set_and_update_does_not_touch_them(repo):
    obs = repo.add(Observation(reported_hz=1))
    assert repo.get(obs.id).notes == ""
    repo.set_notes(obs.id, "NOAA test")
    # update() persists reception fields only; a stale copy keeps the note.
    repo.update(obs.model_copy(update={"closed": True}))
    got = repo.get(obs.id)
    assert (got.notes, got.closed) == ("NOAA test", True)


def test_update_ref_and_export_helpers(db, repo):
    obs = repo.add(Observation(reported_hz=1))
    ref = repo.add_ref(RecordingReference(observation_id=obs.id, path="/r/x", name="x"))
    repo.update_ref(ref.model_copy(update={"path": "/r/y", "name": "y", "sample_end": 9}))
    [got] = repo.refs(obs.id)
    assert (got.path, got.name, got.sample_end) == ("/r/y", "y", 9)

    db.conn.execute(
        "INSERT INTO decode_events (id, ts, decoder, kind, fields_json, observation_id) "
        "VALUES ('e1', 1.0, 'aprs', 'msg', '{\"a\": 1}', ?)",
        (obs.id,),
    )
    [event] = repo.decode_events(obs.id)
    assert event["id"] == "e1" and event["fields"] == {"a": 1}

    assert [o.id for o in repo.list_updated_since(None, 10)] == [obs.id]
    assert repo.list_updated_since(4e9, 10) == []
