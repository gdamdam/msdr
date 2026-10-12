"""Readiness contract: installed, validated and this-session are separate
facts; a backend version change makes validation stale."""

from datetime import date

from msdr.contracts.readiness import DecoderReadiness, Installation, Session, Validation


def test_installed_untested_is_usable_but_not_validated():
    r = DecoderReadiness(
        decoder_id="pocsag",
        installation=Installation(state="present", backend="multimon", version="1.3.0"),
    )
    assert r.usable
    assert r.validation.state == "untested"
    assert not r.validation_stale
    assert r.session.state == "idle"


def test_missing_backend_is_not_usable_even_if_validated_before():
    r = DecoderReadiness(
        decoder_id="dmr",
        installation=Installation(state="missing", backend="dsdfme"),
        validation=Validation(state="field_observed", backend_version="1.0"),
    )
    assert not r.usable
    assert r.validation.state == "field_observed"  # history is kept, not erased


def test_version_change_makes_validation_stale():
    r = DecoderReadiness(
        decoder_id="aprs",
        installation=Installation(state="present", backend="direwolf", version="1.8.1"),
        validation=Validation(
            state="fixture_tested", backend_version="1.7.0", validated_on=date(2026, 9, 25)
        ),
    )
    assert r.validation_stale
    fresh = r.model_copy(
        update={"installation": r.installation.model_copy(update={"version": "1.7.0"})}
    )
    assert not fresh.validation_stale


def test_unknown_version_cannot_prove_freshness():
    r = DecoderReadiness(
        decoder_id="cw",
        installation=Installation(state="present", backend="multimon", version=None),
        validation=Validation(state="fixture_tested", backend_version="1.3.0"),
    )
    assert r.validation_stale


def test_session_output_without_integrity_is_unverified():
    s = Session(state="unverified_output", events=3)
    assert s.valid_frames == 0
    r = DecoderReadiness(decoder_id="pocsag", installation=Installation(state="present"), session=s)
    api = r.as_api()
    assert api["session"]["state"] == "unverified_output"
    assert set(api) >= {"installation", "validation", "session", "validation_stale", "usable"}
