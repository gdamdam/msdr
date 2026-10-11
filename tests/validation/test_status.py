"""msdr.validation.status.validation_status (S5 plan §2): a pure snapshot,
never regenerating the scorecard or running a decoder."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from msdr.decoder.registry import DECODERS
from msdr.settings import Settings
from msdr.validation.status import validation_status


def test_top_level_keys():
    status = validation_status(Settings())
    assert set(status) == {"corpus_cases", "decoders", "scorecard", "outstanding_gates"}


def test_corpus_cases_reflect_the_manifest():
    status = validation_status(Settings())
    by_id = {c["id"]: c for c in status["corpus_cases"]}
    assert by_id  # non-empty

    pending = by_id["aprs-144390"]
    assert pending["status"] == "pending_intake"
    assert pending["files_located"] is None  # no files listed at all


def test_decoders_cover_the_registry_plus_rds():
    status = validation_status(Settings())
    ids = {d["id"] for d in status["decoders"]}
    assert ids == set(DECODERS) | {"rds"}
    by_id = {d["id"]: d for d in status["decoders"]}
    ais = by_id["ais"]
    assert ais["validation_state"] == "fixture_tested"
    assert isinstance(ais["validation_stale"], bool)
    assert any("test_ais.py" in t for t in ais["tests"])
    rds = by_id["rds"]
    assert rds["validation_state"] == "fixture_tested"
    assert any("test_rds.py" in t for t in rds["tests"])


def test_decoders_never_claim_untested_decoders_are_validated():
    status = validation_status(Settings())
    flex = next(d for d in status["decoders"] if d["id"] == "flex")
    assert flex["validation_state"] == "untested"
    assert flex["tests"] == []


def test_scorecard_summary_parses_committed_markdown():
    status = validation_status(Settings())
    scorecard = status["scorecard"]
    assert scorecard["generated_at"] is not None
    assert scorecard["generated_at"].startswith("2026-")
    assert scorecard["decoders_by_status"].get("available", 0) > 0
    assert sum(scorecard["decoders_by_status"].values()) > 0
    assert sum(scorecard["detectors_by_outcome"].values()) > 0


def test_outstanding_gates_always_list_hf_field_validation():
    status = validation_status(Settings())
    gates = status["outstanding_gates"]
    assert gates, "outstanding_gates must never be empty"
    assert any("HF field validation" in g for g in gates)
    assert any("8-hour soak" in g for g in gates)
    assert any("AIS" in g for g in gates)


def test_pure_function_is_deterministic():
    settings = Settings()
    first = validation_status(settings)
    second = validation_status(settings)
    assert first == second


def test_installed_package_reads_bundled_validation_resources_outside_repo(tmp_path):
    package_root = Path(__file__).resolve().parents[2] / "msdr"
    site_packages = tmp_path / "site-packages"
    shutil.copytree(package_root, site_packages / "msdr")
    code = (
        "from msdr.settings import Settings; "
        "from msdr.validation.status import validation_status; "
        "s = validation_status(Settings()); "
        "print(len(s['corpus_cases']), s['scorecard']['generated_at'], "
        "sum(s['scorecard']['decoders_by_status'].values()))"
    )
    env = {**os.environ, "PYTHONPATH": str(site_packages)}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    cases, generated_at, decoder_rows = result.stdout.strip().split()
    assert int(cases) > 0
    assert generated_at.startswith("2026-")
    assert int(decoder_rows) > 0
