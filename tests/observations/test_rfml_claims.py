"""RFML shadow evidence remains separate from the live DSP analyzer lane."""

from msdr.contracts.evidence import evidence_state
from msdr.observations.recorder import ObservationRecorder
from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database


def _label(**changes):
    return {
        "id": "rfml-1",
        "frequency_hz": 145_500_000,
        "modulation": {"kind": "fm_fsk", "confidence": 0.7},
        "family": None,
        "rfml": {
            "status": "ok",
            "classification": "QAM",
            "score": 0.62,
            "calibration": "uncalibrated",
            "model_id": "torchsig-xcit-v0.6.1",
            "model_version": "sha-a",
            "preprocessing_version": "iq4096-linf-v1",
            "candidates": [{"label": "QAM", "score": 0.62}, {"label": "PSK", "score": 0.2}],
            "reason": "",
            "backend": "cpu",
            "shadow": True,
            "analyzed_at": 100.0,
            "sample_rate_hz": 1_000_000.0,
        },
    } | changes


def _recorder():
    db = Database(":memory:")
    db.connect()
    rec = ObservationRecorder(ObservationRepo(db), now=lambda: 100.0)
    rec.start_session("live:test", "live")
    return db, rec


def test_rfml_claim_is_independent_and_explicitly_uncalibrated():
    db, rec = _recorder()
    try:
        rec.sync_labels([_label()])
        [obs] = rec.repo.list(session_id=rec.session.id)
        claims = rec.repo.claims(obs.id)
        dsp = next(c for c in claims if c.producer == "survey.analyzer")
        ml = next(c for c in claims if c.producer == "survey.rfml")
        assert (dsp.value, dsp.producer) == ("fm_fsk", "survey.analyzer")
        assert (ml.subject, ml.value, ml.evidence_type, ml.integrity) == (
            "modulation",
            "QAM",
            "iq_estimate",
            "unknown",
        )
        assert ml.producer_version == "sha-a"
        assert evidence_state([ml]) == "none"
        assert "torchsig-xcit" in ml.support[0]
        assert ml.score == 0.62 and ml.alternatives == ["PSK"]
        assert "uncalibrated" in " ".join(ml.limitations)
        assert "iq4096-linf-v1" in " ".join(ml.limitations)
        assert ml.next_action == "independent RF modulation validation"
    finally:
        db.close()


def test_rfml_version_or_preprocessing_change_supersedes_own_slot():
    db, rec = _recorder()
    try:
        rec.sync_labels([_label()])
        rec.sync_labels([_label(rfml=_label()["rfml"] | {"model_version": "sha-b"})])
        [obs] = rec.repo.list(session_id=rec.session.id)
        ml = [c for c in rec.repo.claims(obs.id) if c.producer == "survey.rfml"]
        assert len(ml) == 2
        assert ml[0].historical and ml[0].superseded_by == ml[1].claim_id
        assert ml[1].producer_version == "sha-b"
        assert len([c for c in rec.repo.claims(obs.id) if c.producer == "survey.analyzer"]) == 1
    finally:
        db.close()


def test_alternatives_reorder_alone_does_not_append_a_claim():
    db, rec = _recorder()
    try:
        rec.sync_labels([_label()])
        with_third = _label(
            rfml=_label()["rfml"]
            | {
                "candidates": [
                    {"label": "QAM", "score": 0.62},
                    {"label": "PSK", "score": 0.2},
                    {"label": "FSK", "score": 0.15},
                ]
            }
        )
        rec.sync_labels([with_third])  # a genuinely new alternative: appends
        [obs] = rec.repo.list(session_id=rec.session.id)
        ml = [c for c in rec.repo.claims(obs.id) if c.producer == "survey.rfml"]
        assert len(ml) == 2

        reshuffled = _label(
            rfml=_label()["rfml"]
            | {
                "candidates": [
                    {"label": "QAM", "score": 0.62},
                    {"label": "FSK", "score": 0.30},  # same alternatives, runner-up
                    {"label": "PSK", "score": 0.10},  # order and scores jittered
                ]
            }
        )
        rec.sync_labels([reshuffled])
        ml = [c for c in rec.repo.claims(obs.id) if c.producer == "survey.rfml"]
        assert len(ml) == 2  # order/score jitter alone must not append a claim
    finally:
        db.close()


def test_abstention_alone_does_not_create_modulation_claim():
    db, rec = _recorder()
    try:
        abstained = _label(rfml={"status": "abstained", "reason": "low signal"})
        rec.sync_labels([abstained])
        [obs] = rec.repo.list(session_id=rec.session.id)
        assert not [c for c in rec.repo.claims(obs.id) if c.producer == "survey.rfml"]
    finally:
        db.close()


def test_abstention_after_estimate_appends_unknown_state():
    db, rec = _recorder()
    try:
        rec.sync_labels([_label()])
        rec.sync_labels([_label(rfml={"status": "abstained", "reason": "low signal"})])
        [obs] = rec.repo.list(session_id=rec.session.id)
        ml = [c for c in rec.repo.claims(obs.id) if c.producer == "survey.rfml"]
        assert [c.value for c in ml] == ["QAM", "UNKNOWN"]
        assert ml[0].historical and ml[1].score is None
        assert "abstained" in " ".join(ml[1].limitations)
    finally:
        db.close()


def test_detector_proposal_is_persisted_without_promoting_evidence():
    db, rec = _recorder()
    try:
        rec.sync_labels([{
            "id": "proposal", "frequency_hz": 160000000, "width_hz": 12000,
            "rfml_detection": {"model_id": "test-detector", "model_version": "sha",
                               "low_hz": 159994000, "high_hz": 160006000, "score": 0.8},
        }])
        [obs] = rec.repo.list(session_id=rec.session.id)
        [claim] = rec.repo.claims(obs.id)
        assert claim.producer == "survey.rfml.detector"
        assert claim.subject == "presence"
        assert evidence_state([claim]) == "none"
        assert '159994000' in claim.support[0]
    finally:
        db.close()
