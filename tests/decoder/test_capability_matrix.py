from msdr.contracts.readiness import Validation
from msdr.decoder.capability_matrix import MATRIX, RDS_ENTRY, CapabilityEntry
from msdr.decoder.registry import DECODERS


def test_matrix_covers_every_registry_decoder():
    # capability_matrix.py's own promise: one entry per DECODERS id, no more,
    # no less -- a new registry entry must get a matrix entry too.
    assert set(MATRIX) == set(DECODERS)


def test_every_entry_is_well_typed():
    for decoder_id, entry in MATRIX.items():
        assert isinstance(entry, CapabilityEntry), decoder_id
        assert entry.input_path in ("audio", "iq", "both"), decoder_id
        assert isinstance(entry.validation, Validation), decoder_id
        assert entry.replay_supported == (
            "raw_iq" if DECODERS[decoder_id].demodulators == ("IQ",) else "planned_s3"
        ), decoder_id
        assert entry.integrity_notes, decoder_id
        assert entry.field_notes, decoder_id


def test_iq_only_decoders_are_iq_input_path():
    for decoder_id, spec in DECODERS.items():
        if spec.iq_only:
            assert MATRIX[decoder_id].input_path == "iq", decoder_id


def test_band_path_matches_nfm_demodulator_gate():
    # Mirrors manager.py start_band's actual runtime gate: "NFM" in
    # spec.demodulators, regardless of iq_only.
    for decoder_id, spec in DECODERS.items():
        expected = "NFM" in spec.demodulators
        assert MATRIX[decoder_id].band_path == expected, decoder_id


def test_ax25_decoders_report_fcs_integrity():
    for decoder_id in ("aprs", "packet9600", "packet300"):
        assert MATRIX[decoder_id].integrity_policy == "fcs"


def test_navtex_reports_fec_integrity():
    assert MATRIX["navtex"].integrity_policy == "fec"


def test_dcf77_and_msf_report_parity_integrity():
    assert MATRIX["dcf77"].integrity_policy == "parity"
    assert MATRIX["msf"].integrity_policy == "parity"


def test_rds_entry_is_separate_from_matrix():
    assert "rds" not in MATRIX
    assert RDS_ENTRY.integrity_policy == "crc"
    assert RDS_ENTRY.validation.state == "fixture_tested"
    assert not RDS_ENTRY.validation.fixtures  # synthetic MPX only; no off-air IQ in the tree


def test_ais_entry_is_crc_iq_band_and_live_observed():
    assert set(MATRIX) == set(DECODERS)
    entry = MATRIX["ais"]
    assert entry.input_path == "iq"
    assert entry.band_path is True
    assert entry.integrity_policy == "crc"
    assert entry.validation.state == "live_output_observed"
    assert all(t.startswith("tests/decoder/test_ais.py::") for t in entry.validation.tests)
    assert "independent comparison pending" in entry.validation.notes


def test_confirmed_live_decoders_are_not_labeled_fixture_only():
    for decoder_id in ("pocsag", "aprs", "cw", "dmr"):
        entry = MATRIX[decoder_id]
        assert entry.validation.state == "live_output_observed"
        assert entry.validation.report == "docs/validation/live-vhf-uhf-2026-10-03.md"


def test_acars_is_crc_checked_and_synthetic_only():
    entry = MATRIX["acars"]
    assert entry.integrity_policy == "crc"
    assert entry.validation.state == "fixture_tested"


def test_jjy_chu_ale_report_their_integrity_policies():
    assert MATRIX["jjy"].integrity_policy == "parity"
    assert MATRIX["chu"].integrity_policy == "repeat_fields"
    assert MATRIX["ale"].integrity_policy == "fec"
