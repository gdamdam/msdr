import pytest

from msdr.observations.recorder import ObservationRecorder
from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database
from msdr.survey.families import KEYED_CARRIER


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rec(db, clock):
    r = ObservationRecorder(ObservationRepo(db), now=clock)
    r.start_session("live:sdrconnect", "live", "host", 146_000_000, 2_000_000, {})
    return r


def _label(**kw):
    base = {
        "id": "t1",
        "frequency_hz": 146_520_000,
        "width_hz": 12_500.0,
        "snr_db": 18.0,
        "activity": {"kind": "steady", "duty": 0.5},
        "bandplan": None,
        "catalog": [],
        "modulation": None,
        "symbol_rate_hz": None,
        "family": None,
        "voice": None,
        "confirmed": None,
        "rds": None,
    }
    return base | kw


def _obs(rec):
    return rec.repo.list(session_id=rec.session.id)


def _changes(db):
    return db.conn.total_changes


def test_label_opens_observation_with_measurements(rec):
    rec.sync_labels([_label()])
    [obs] = _obs(rec)
    assert obs.provenance == "survey label" and obs.reported_hz == 146_520_000
    assert obs.bandwidth_hz == 12_500 and obs.start_ts == 1_000.0
    snr, duty = obs.measurements
    assert snr.method == "spectrum_display_snr" and snr.unit == "dB"
    assert duty.unit == "%" and duty.value == 50.0 and duty.denominator == "occupancy window"
    assert duty.window_s == 600.0


def test_identical_labels_twice_zero_writes(db, rec):
    labels = [
        _label(
            bandplan={"name": "2m amateur"},
            modulation={"kind": "fm_fsk", "confidence": 0.7},
            voice={"kind": "silence", "score": 0.0},
        )
    ]
    rec.sync_labels(labels)
    before = _changes(db)
    rec.sync_labels([dict(labels[0])])
    assert _changes(db) == before


def test_changed_value_supersedes_append_only(rec, clock):
    rec.sync_labels([_label(modulation={"kind": "unknown", "confidence": 0.4})])
    # A jittering score alone must not append a claim.
    rec.sync_labels([_label(modulation={"kind": "unknown", "confidence": 0.5})])
    clock.t += 5
    rec.sync_labels([_label(modulation={"kind": "fm_fsk", "confidence": 0.7})])
    [obs] = _obs(rec)
    claims = rec.repo.claims(obs.id)
    assert [c.value for c in claims] == ["unknown", "fm_fsk"]
    assert claims[0].superseded_by == claims[1].claim_id and claims[1].superseded_by is None


def test_retired_label_closes(rec, clock):
    rec.sync_labels([_label()])
    clock.t += 30
    rec.sync_labels([], sample_index=None)
    [obs] = _obs(rec)
    assert obs.closed and obs.end_ts == 1_030.0


def test_replay_session_has_no_capture_times(db, clock):
    r = ObservationRecorder(ObservationRepo(db), now=clock)
    r.start_session("replay:x.wav", "replay", "/r/x.wav")
    r.sync_labels([_label(modulation={"kind": "fm_fsk", "confidence": 0.7})], sample_index=100)
    r.sync_labels([], sample_index=900)
    [obs] = r.repo.list(session_id=r.session.id)
    assert obs.start_ts is None and obs.end_ts is None
    assert (obs.sample_start, obs.sample_end) == (100, 900)
    [c] = r.repo.claims(obs.id)
    assert c.observed_start is None and c.sample_start == 100


def test_quiet_voice_is_capture_quality_not_claim(rec):
    rec.sync_labels([_label(voice={"kind": "quiet", "score": 0.0})])
    [obs] = _obs(rec)
    assert obs.capture_quality == ["quiet"]
    assert rec.repo.claims(obs.id) == []


def test_voice_speech_and_data(rec):
    rec.sync_labels([_label(voice={"kind": "voice", "score": 0.8})])
    [obs] = _obs(rec)
    assert [c.value for c in rec.repo.claims(obs.id)] == ["speech-like audio"]


def test_keyed_carrier_is_presence_never_cw(rec):
    rec.sync_labels(
        [
            _label(
                modulation={"kind": "ook", "confidence": 0.75},
                symbol_rate_hz=20.0,
                family={"name": "CW", "suggested_decoders": ["cw"], "confidence": 0.6},
            )
        ]
    )
    [obs] = _obs(rec)
    claims = rec.repo.claims(obs.id)
    assert [(c.subject, c.value) for c in claims] == [("presence", "keyed carrier")]
    assert claims[0].evidence_type == "iq_estimate"
    assert claims[0].next_action == "decode cw"
    assert not any("CW" in c.value for c in claims)


def test_keyed_activity_alone_is_presence(rec):
    rec.sync_labels([_label(activity={"kind": "keyed", "duty": 0.3})])
    [obs] = _obs(rec)
    assert [(c.subject, c.value) for c in rec.repo.claims(obs.id)] == [
        ("presence", "keyed carrier")
    ]


def test_family_claim_support_and_next_action(rec):
    rec.sync_labels(
        [
            _label(
                modulation={"kind": "fm_fsk", "confidence": 0.8},
                symbol_rate_hz=1200.0,
                family={"name": "POCSAG1200", "suggested_decoders": ["pocsag"], "confidence": 0.6},
            )
        ]
    )
    [obs] = _obs(rec)
    fam = [c for c in rec.repo.claims(obs.id) if c.subject == "protocol"][0]
    assert fam.evidence_type == "iq_estimate" and fam.next_action == "decode pocsag"
    assert "1200" in fam.support[0] and fam.score == 0.6


@pytest.mark.parametrize(
    ("decoder", "etype", "integrity"),
    [
        ("pocsag", "decoder_output", "unknown"),  # event did not record a check
        ("aprs", "decoder_output", "unknown"),  # FCS capability is not event evidence
    ],
)
def test_confirmed_decoder_follows_integrity_policy(rec, decoder, etype, integrity):
    rec.sync_labels(
        [_label(confirmed={"decoder": decoder, "sample": "hi", "at": 1.0, "events": 3})]
    )
    [obs] = _obs(rec)
    [c] = rec.repo.claims(obs.id)
    assert (c.subject, c.evidence_type, c.integrity) == ("protocol", etype, integrity)
    if integrity == "unavailable":
        assert "backend exposes no per-message integrity flag" in c.limitations


def test_confirmed_decoder_uses_event_integrity_and_upgrades(rec):
    label = _label(
        frequency_hz=77_500_000,
        confirmed={
            "decoder": "dcf77",
            "sample": "UTC frame",
            "at": 1.0,
            "events": 1,
            "integrity": "failed",
        },
    )
    rec.sync_labels([label])
    [obs] = _obs(rec)
    [failed] = rec.repo.claims(obs.id)
    assert (failed.evidence_type, failed.integrity) == ("decoder_output", "failed")

    # Same decoder/value, stronger subsequent event: preserve history and
    # append an upgraded claim rather than treating the value as unchanged.
    rec.sync_labels(
        [
            {
                **label,
                "confirmed": {
                    **label["confirmed"],
                    "sample": "passing frame",
                    "events": 2,
                    "integrity": "passed",
                    "integrity_sample": "passing frame",
                },
            }
        ]
    )
    claims = rec.repo.claims(obs.id)
    assert len(claims) == 2
    assert claims[0].superseded_by == claims[1].claim_id
    assert (claims[1].evidence_type, claims[1].integrity) == ("validated_decode", "passed")
    assert "passing frame" in claims[1].support[0]


def test_rds_pi_validated_ps_decoder_output(rec):
    rds = {"pi": "0x1234", "ps": "KOIT", "pty_name": "Soft", "groups": 40, "block_error_rate": 0.1}
    rec.sync_labels(
        [
            _label(
                frequency_hz=96_500_000,
                rds=rds,
                family={"name": "FM broadcast · KOIT", "confidence": 0.9, "suggested_decoders": []},
                confirmed={"decoder": "rds", "sample": "KOIT", "at": 1.0, "events": 40},
            )
        ]
    )
    [obs] = _obs(rec)
    claims = rec.repo.claims(obs.id)
    assert [(c.value, c.evidence_type, c.integrity) for c in claims] == [
        ("PI 0x1234", "validated_decode", "passed"),
        ("KOIT", "decoder_output", "unknown"),
    ]
    assert claims[1].limitations == ["PS text can change"]
    s = rec.repo.summary(obs)
    assert s["evidence_state"] == "validated" and s["identity_established"] is True


def test_allocation_and_schedule_only_is_context(rec):
    rec.sync_labels(
        [
            _label(
                bandplan={"name": "NOAA weather"},
                catalog=[{"name": "KEC49", "confidence": 0.5, "reason": "x"}],
            )
        ]
    )
    [obs] = _obs(rec)
    s = rec.repo.summary(obs)
    assert s["evidence_state"] == "context" and s["identity_established"] is False
    station = [c for c in s["claims"] if c["subject"] == "station"][0]
    assert station["limitations"] == ["schedule predicts a transmitter; not reception"]


def test_allocation_claim_lists_every_service_beside_bandplan(rec):
    rec.sync_labels(
        [
            _label(
                bandplan={"name": "NOAA weather"},
                allocation={
                    "lo_hz": 162_000_000,
                    "hi_hz": 174_000_000,
                    "jurisdiction": "US",
                    "status": "ok",
                    "services": [
                        {"name": "FIXED", "priority": "primary", "table": "federal"},
                        {
                            "name": "MOBILE",
                            "qualifier": "except aeronautical mobile",
                            "priority": "primary",
                            "table": "federal",
                        },
                        {"name": "Radiolocation", "priority": "secondary", "table": "non_federal"},
                    ],
                },
            )
        ]
    )
    [obs] = _obs(rec)
    by_producer = {c.producer: c for c in rec.repo.claims(obs.id)}
    assert by_producer["survey.bandplan"].value == "NOAA weather"
    allocation = by_producer["survey.allocation"]
    assert allocation.evidence_type == "allocation" and allocation.subject == "service"
    assert allocation.value == "FIXED; MOBILE except aeronautical mobile; Radiolocation (secondary)"


def test_decode_event_links_and_claims_once(db, rec):
    for i, ts in enumerate((1_000.0, 1_010.0)):
        db.conn.execute(
            "INSERT INTO decode_events (id, ts, frequency_hz, decoder, kind) "
            "VALUES (?, ?, 144390000, 'aprs', 'msg')",
            (f"e{i}", ts),
        )
        oid = rec.on_decode_event(
            {"id": f"e{i}", "ts": ts, "frequency_hz": 144_390_000 + i, "decoder": "aprs"}
        )
    obs = rec.repo.get(oid)
    assert obs.provenance == "decode event" and obs.end_ts == 1_010.0
    assert sorted(rec.repo.decode_event_ids(oid)) == ["e0", "e1"]
    [c] = rec.repo.claims(oid)
    assert (c.evidence_type, c.integrity) == ("decoder_output", "unknown")
    assert rec.on_decode_event({"id": "x", "ts": 1.0, "frequency_hz": None}) is None


@pytest.mark.parametrize(
    ("integrity", "etype"),
    [
        ("failed", "decoder_output"),  # bug repro (P1): must not become validated_decode
        ("passed", "validated_decode"),
        ("unknown", "decoder_output"),  # unchecked: capability alone is never a pass
    ],
)
def test_live_decode_event_integrity_overrides_capability_matrix(db, rec, integrity, etype):
    """DCF77's capability-matrix policy is "parity" (it *can* check), which
    used to be enough on its own to grade the live claim `validated_decode`/
    `passed` regardless of this event's actual result. The event's own
    integrity must win, live same as replay."""
    db.conn.execute(
        "INSERT INTO decode_events (id, ts, frequency_hz, decoder, kind) "
        "VALUES ('e', 1.0, 77500000, 'dcf77', 'msg')"
    )
    oid = rec.on_decode_event(
        {
            "id": "e",
            "ts": 1.0,
            "frequency_hz": 77_500_000,
            "decoder": "dcf77",
            "integrity": integrity,
        }
    )
    [c] = rec.repo.claims(oid)
    assert (c.evidence_type, c.integrity) == (etype, integrity)


def test_replay_decode_event_failed_integrity_is_not_validated(db, rec):
    """Same rule on the replay/analysis_run_id path: a capability-matrix
    "parity" decoder (DCF77) whose event failed its parity check must not be
    persisted as validated_decode/passed."""
    _insert_decode_event(db, "e0", frequency_hz=77_500_000, decoder="dcf77")
    oid = rec.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 77_500_000,
            "decoder": "dcf77",
            "integrity": "failed",
            "analysis_run_id": "run-1",
        }
    )
    [claim] = rec.repo.claims(oid)
    assert (claim.evidence_type, claim.integrity) == ("decoder_output", "failed")


def test_decode_event_on_label_obs_adds_no_claim(db, rec):
    rec.sync_labels([_label(frequency_hz=144_390_000)])
    db.conn.execute(
        "INSERT INTO decode_events (id, ts, frequency_hz, decoder, kind) "
        "VALUES ('e', 1.0, 144390000, 'aprs', 'msg')"
    )
    oid = rec.on_decode_event(
        {"id": "e", "ts": 1.0, "frequency_hz": 144_390_000, "decoder": "aprs"}
    )
    assert rec.repo.get(oid).provenance == "survey label"
    assert rec.repo.claims(oid) == []


# --- S4: review/replay decode events (analysis_run_id) ----------------------


def _insert_decode_event(db, event_id, *, frequency_hz, decoder="aprs"):
    db.conn.execute(
        "INSERT INTO decode_events (id, ts, frequency_hz, decoder, kind) "
        "VALUES (?, 1.0, ?, ?, 'msg')",
        (event_id, frequency_hz, decoder),
    )


@pytest.mark.parametrize("replay", [False, True])
def test_event_claim_upgrades_to_later_pass_without_downgrade(db, rec, replay):
    if replay:
        rec.start_session("replay:test.wav", "replay", "/r/test.wav")
    events = [
        ("e0", "failed", "bad frame"),
        ("e1", "passed", "good frame"),
        ("e2", "failed", "later bad frame"),
    ]
    obs_id = None
    for index, (event_id, integrity, text) in enumerate(events):
        _insert_decode_event(db, event_id, frequency_hz=144_390_000)
        event = {
            "id": event_id,
            "ts": float(index + 1),
            "frequency_hz": 144_390_000,
            "decoder": "aprs",
            "integrity": integrity,
            "text": text,
        }
        if replay:
            event.update(
                source_id="replay:test.wav",
                analysis_run_id="run-1",
                generation=0,
                run_index=0,
                sample_index=index * 100,
            )
        obs_id = rec.on_decode_event(event)

    claims = rec.repo.claims(obs_id)
    assert len(claims) == 2
    assert claims[0].superseded_by == claims[1].claim_id
    assert (claims[1].evidence_type, claims[1].integrity) == ("validated_decode", "passed")
    assert "good frame" in claims[1].support[-1]


def test_replay_decode_events_link_to_one_observation_per_channel(db, rec):
    """A live session is open (the `rec` fixture's own start_session), but an
    analysis_run_id-tagged event must never be treated as live -- no
    capture-time start/end, no bump to a live encounter count. Same
    source_id/generation/frequency -> the same observation."""
    _insert_decode_event(db, "e0", frequency_hz=144_390_000)
    _insert_decode_event(db, "e1", frequency_hz=144_390_000, decoder="aprs")
    oid0 = rec.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 144_390_000,
            "decoder": "aprs",
            "integrity": "passed",
            "source_id": "replay:x.wav",
            "analysis_run_id": "run-1",
            "generation": 0,
            "sample_index": 100,
        }
    )
    oid1 = rec.on_decode_event(
        {
            "id": "e1",
            "ts": 2.0,
            "frequency_hz": 144_390_000,  # same channel, same run/generation
            "decoder": "aprs",
            "integrity": "passed",
            "source_id": "replay:x.wav",
            "analysis_run_id": "run-1",
            "generation": 0,
            "sample_index": 900,
        }
    )
    assert oid0 == oid1
    obs = rec.repo.get(oid0)
    assert obs.start_ts is None and obs.end_ts is None  # never live-looking
    assert (obs.sample_start, obs.sample_end) == (100, 900)
    assert obs.provenance == "replay decode"
    assert sorted(rec.repo.decode_event_ids(oid0)) == ["e0", "e1"]


def test_replay_decode_events_partition_by_channel_and_seek_generation(db, clock):
    """Bug repro (P1): a run visiting two frequencies, then a seek backward
    (a new generation, lower sample_index), must not merge into one
    observation labelled by the first channel with a backwards interval --
    each (source_id, generation, frequency) gets its own observation, with
    correct (increasing) sample intervals and a RecordingReference onto the
    recording being replayed."""
    r = ObservationRecorder(ObservationRepo(db), now=clock)
    r.start_session("replay:x.wav", "replay", "/r/x.wav")
    for event_id, freq in (("e0", 144_390_000), ("e1", 145_825_000), ("e2", 145_825_000)):
        _insert_decode_event(db, event_id, frequency_hz=freq)
    common = {"decoder": "aprs", "source_id": "replay:x.wav", "analysis_run_id": "run-1"}
    o0 = r.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 144_390_000,
            "generation": 0,
            "run_index": 0,
            "sample_index": 10_000,
            **common,
        }
    )
    o1 = r.on_decode_event(
        {
            "id": "e1",
            "ts": 2.0,
            "frequency_hz": 145_825_000,
            "generation": 0,
            "run_index": 0,
            "sample_index": 20_000,
            **common,
        }
    )
    # seek backward: new generation, lower sample_index, same frequency as e1
    o2 = r.on_decode_event(
        {
            "id": "e2",
            "ts": 3.0,
            "frequency_hz": 145_825_000,
            "generation": 1,
            "run_index": 1,
            "sample_index": 100,
            **common,
        }
    )
    assert len({o0, o1, o2}) == 3  # all separate observations

    obs0 = r.repo.get(o0)
    assert (obs0.reported_hz, obs0.sample_start, obs0.sample_end) == (144_390_000, 10_000, 10_000)
    obs1 = r.repo.get(o1)
    assert (obs1.reported_hz, obs1.sample_start, obs1.sample_end) == (145_825_000, 20_000, 20_000)
    obs2 = r.repo.get(o2)
    assert (obs2.reported_hz, obs2.sample_start, obs2.sample_end) == (145_825_000, 100, 100)

    for oid in (o0, o1, o2):
        [ref] = r.repo.refs(oid)
        assert ref.path == "/r/x.wav" and ref.name == "x.wav"
    assert r.repo.refs(o2)[0].run_index == 1


def test_replay_decode_event_claim_carries_analysis_run_id_and_event_integrity(db, rec):
    _insert_decode_event(db, "e0", frequency_hz=144_390_000)
    oid = rec.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 144_390_000,
            "decoder": "aprs",
            "integrity": "passed",
            "source_id": "replay:x.wav",
            "analysis_run_id": "run-1",
            "sample_index": 100,
        }
    )
    [claim] = rec.repo.claims(oid)
    assert claim.analysis_run_id == "run-1"
    assert (claim.evidence_type, claim.integrity) == ("validated_decode", "passed")
    assert claim.sample_start == 100 and claim.sample_end == 100


def test_replay_decode_event_unavailable_integrity_is_decoder_output(db, rec):
    _insert_decode_event(db, "e0", frequency_hz=152_840_000, decoder="pocsag")
    oid = rec.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 152_840_000,
            "decoder": "pocsag",
            "integrity": "unavailable",
            "analysis_run_id": "run-2",
        }
    )
    [claim] = rec.repo.claims(oid)
    assert (claim.evidence_type, claim.integrity) == ("decoder_output", "unavailable")


def test_replay_decode_events_from_different_runs_get_different_observations(db, rec):
    _insert_decode_event(db, "e0", frequency_hz=144_390_000)
    _insert_decode_event(db, "e1", frequency_hz=144_390_000)
    oid0 = rec.on_decode_event(
        {
            "id": "e0",
            "ts": 1.0,
            "frequency_hz": 144_390_000,
            "decoder": "aprs",
            "analysis_run_id": "run-1",
        }
    )
    oid1 = rec.on_decode_event(
        {
            "id": "e1",
            "ts": 1.0,
            "frequency_hz": 144_390_000,
            "decoder": "aprs",
            "analysis_run_id": "run-2",
        }
    )
    assert oid0 != oid1


# --- S4: voice-wording families are a modulation claim, not protocol --------


@pytest.mark.parametrize(
    ("family_name", "modulation_value"),
    [
        ("NFM voice (analog)", "NFM voice"),
        ("speech-like NFM audio (tentative)", "speech-like NFM audio (tentative)"),
        ("speech-like audio (demodulation untested)", "speech-like audio"),
    ],
)
def test_voice_family_wording_is_a_modulation_claim_not_protocol(
    rec, family_name, modulation_value
):
    rec.sync_labels(
        [
            _label(
                modulation={"kind": "fm_fsk", "confidence": 0.8},
                family={
                    "name": family_name,
                    "suggested_decoders": [],
                    "alternatives": ["NFM voice (analog)"],
                    "next_action": "listen (NFM)",
                    "confidence": 0.8,
                },
            )
        ]
    )
    [obs] = _obs(rec)
    claims = rec.repo.claims(obs.id)
    assert not any(c.subject == "protocol" and c.value == family_name for c in claims)
    modulation_claims = [
        c for c in claims if c.subject == "modulation" and c.value == modulation_value
    ]
    assert len(modulation_claims) == 1
    assert modulation_claims[0].evidence_type == "iq_estimate"


def test_unconfirmed_keyed_carrier_family_is_presence_never_protocol(rec):
    """msdr.survey.families.KEYED_CARRIER ("keyed carrier (unknown code)") is
    an *unconfirmed* keyed carrier -- families.family_for names it this when
    the on/off timing doesn't fit Morse (features.keying_timing). Like "CW",
    it must never become a protocol claim."""
    rec.sync_labels(
        [
            _label(
                modulation={"kind": "ook", "confidence": 0.75},
                symbol_rate_hz=150.0,
                family={
                    "name": KEYED_CARRIER,
                    "suggested_decoders": [],
                    "alternatives": ["CW (dot/dash timing not confirmed)"],
                    "next_action": "listen; on/off keying without confirmed Morse timing",
                    "confidence": 0.6,
                },
            )
        ]
    )
    [obs] = _obs(rec)
    claims = rec.repo.claims(obs.id)
    assert [(c.subject, c.value) for c in claims] == [("presence", "keyed carrier")]
    assert not any(c.value == KEYED_CARRIER for c in claims)


def test_scanner_hit_and_end_session(rec, clock):
    obs = rec.on_scanner_hit(
        {"ts": 500.0, "duration_s": 4.0, "frequency_hz": 162_400_000, "peak_snr_db": 20.0},
        "find1",
    )
    got = rec.repo.get(obs.id)
    assert got.closed and (got.start_ts, got.end_ts) == (496.0, 500.0)
    assert got.find_id == "find1" and got.provenance == "scanner hit"
    rec.sync_labels([_label()])
    sid = rec.session.id
    clock.t += 1
    rec.end_session()
    assert all(o.closed for o in rec.repo.list(session_id=sid))
    assert rec.repo.get_session(sid).ended_at == 1_001.0
