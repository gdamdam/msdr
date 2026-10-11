"""Validates tests/corpus/manifest.json's shape and its promises about the
repo: every "located" file must actually exist with a matching sha256, and
no raw IQ capture over 2 MB may be committed under tests/ or docs/ (this
manifest describes captures -- most of them off-device -- it must never
become a smuggled way to check one in)."""

import hashlib
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = json.loads((_REPO_ROOT / "tests/corpus/manifest.json").read_text())

_REQUIRED_CASE_KEYS = {
    "id",
    "class",
    "reported_frequencies_hz",
    "label_notes",
    "reference_type",
    "status",
    "files",
    "validation_question",
    "expected_outcome",
    "provenance",
}
_REQUIRED_FILE_KEYS = {"path", "sha256", "format", "rate", "center"}
_VALID_REFERENCE_TYPES = {"operator_report", "repo_fixture", "private_capture"}
# synthetic_validated_field_pending (S4): the decode/classification is
# validated on synthetic fixtures (named tests), but no real off-air capture
# backs it yet -- distinct from "located" (a real repo fixture already
# checked in) and "pending_intake" (nothing checked at all).
_VALID_STATUSES = {"located", "pending_intake", "inaccessible", "synthetic_validated_field_pending"}

# Guard against ever committing a raw capture: anything over this, under
# tests/ or docs/, fails the suite (manifest entries point at captures --
# most living outside the repo entirely -- they must not smuggle one in).
_MAX_COMMITTED_BYTES = 2 * 1024 * 1024
# Anything that could be a raw capture (plan: no raw IQ in Git).
_CAPTURE_SUFFIXES = (".npy", ".wav", ".sigmf-data", ".iq", ".raw", ".cs16", ".cf32", ".cu8", ".bin")


def _cases() -> list[dict]:
    return _MANIFEST["cases"]


def test_manifest_has_cases():
    assert len(_cases()) > 0


def test_every_case_has_required_keys_and_valid_ids():
    ids = [case["id"] for case in _cases()]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for case in _cases():
        missing = _REQUIRED_CASE_KEYS - set(case)
        assert not missing, f"{case.get('id')}: missing keys {missing}"
        assert case["reference_type"] in _VALID_REFERENCE_TYPES, case["id"]
        assert case["status"] in _VALID_STATUSES, case["id"]
        assert isinstance(case["reported_frequencies_hz"], list), case["id"]
        assert all(isinstance(f, int) for f in case["reported_frequencies_hz"]), case["id"]
        assert isinstance(case["files"], list), case["id"]
        for file_entry in case["files"]:
            missing_file_keys = _REQUIRED_FILE_KEYS - set(file_entry)
            assert not missing_file_keys, f"{case['id']}: file missing {missing_file_keys}"


def test_located_files_exist_with_matching_sha256():
    # No case ships a repo fixture today (off-air captures are kept out of the
    # tree); the check still guards any case that gains one.
    located = [case for case in _cases() if case["status"] == "located"]
    for case in located:
        assert case["files"], f"{case['id']}: status=located but no files listed"
        for file_entry in case["files"]:
            path = _REPO_ROOT / file_entry["path"]
            assert path.is_file(), f"{case['id']}: {file_entry['path']} does not exist"
            assert file_entry["sha256"], f"{case['id']}: located file must have a sha256"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == file_entry["sha256"], f"{case['id']}: sha256 mismatch for {path}"


def test_inaccessible_files_have_no_invented_sha256():
    # A capture the test environment cannot read keeps sha256 null rather
    # than a guessed/invented value.
    for case in _cases():
        if case["status"] != "inaccessible":
            continue
        for file_entry in case["files"]:
            assert file_entry["sha256"] is None, (
                f"{case['id']}: inaccessible file must not carry an invented sha256"
            )


def test_no_raw_capture_over_size_limit_committed_under_tests_or_docs():
    for base in (_REPO_ROOT / "tests", _REPO_ROOT / "docs"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if (
                path.suffix.lower() in _CAPTURE_SUFFIXES
                and path.stat().st_size > _MAX_COMMITTED_BYTES
            ):
                raise AssertionError(f"raw-looking capture over size limit committed: {path}")


def test_ais_case_never_identifies_from_frequency_alone():
    case = next(c for c in _cases() if c["id"] == "ais-161975-162025")
    # S4: synthetic-validated (fixtures + named tests), field validation on
    # the real 161.975/162.025 MHz capture still pending -- never "located"
    # (no repo fixture backs this) and never silently upgraded past what the
    # synthetic tests actually proved.
    assert case["status"] == "synthetic_validated_field_pending"
    assert "GMSK" in case["expected_outcome"]
    assert "test_" in case["expected_outcome"], "expected_outcome must name the tests"
    assert "frequency" in case["validation_question"].lower()
