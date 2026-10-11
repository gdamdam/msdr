import json

import pytest

from msdr.rfml import cli
from msdr.rfml.baseline import evaluate_baseline, release_gate


def box(source=None, low=100, high=110, start=0, end=1):
    value = {"low_hz": low, "high_hz": high, "start_s": start, "end_s": end}
    if source:
        value.update(source=source, score=0.8)
    return value


def session(identifier, split="test", truth=None, dsp=None, ml=None, combined=None, duration=600):
    result = {"id": identifier, "recording": identifier + ".npy", "split": split,
            "annotation_complete": True, "duration_s": duration,
            "frequency_range_hz": [90, 200], "truth": truth or [],
            "dsp": dsp or [], "ml": ml or []}
    if combined is not None:
        result["combined"] = combined
    return result


def write(tmp_path, sessions):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema_version": 1, "sessions": sessions}))
    return path


def test_one_to_one_matches_duplicate_prediction_and_reports_noise(tmp_path):
    path = write(tmp_path, [session("signal", truth=[box()],
                                    dsp=[box("dsp"), box("dsp")], ml=[box("ml")]),
                            session("noise", ml=[box("ml")])])
    report = evaluate_baseline(path)["splits"]["test"]
    assert report["dsp"]["true_positives"] == 1
    assert report["dsp"]["false_positives"] == 1
    assert report["ml"]["false_positives"] == 1
    assert report["ml"]["mean_boundary_iou"] == 1
    assert report["fusion"]["false_positives"] == 2
    assert report["combined"] is None
    assert report["negative_sessions"] == 1


@pytest.mark.parametrize("mutation", [
    {"annotation_complete": False}, {"duration_s": float("nan")},
    {"frequency_range_hz": [200, 90]},
    {"truth": [box(start=-1)]},
    {"dsp": [box("dsp", high=300)]},
    {"ml": [box("dsp")]},
])
def test_invalid_or_unverified_manifest_fails(tmp_path, mutation):
    sample = session("bad")
    sample.update(mutation)
    with pytest.raises(ValueError):
        evaluate_baseline(write(tmp_path, [sample]))


def test_empty_truth_is_valid_but_cannot_pass_gate(tmp_path):
    baseline = evaluate_baseline(write(tmp_path, [session("noise")]))
    assert baseline["splits"]["test"]["dsp"]["recall"] is None
    gate = release_gate(baseline, {})
    assert not gate["passed"]
    assert gate["status"] == "insufficient_or_failed"


def test_release_gate_requires_support_and_honors_comparative_thresholds(tmp_path):
    sessions = [session("dev", split="development")]
    for index in range(3):
        sessions.append(session(f"positive-{index}", truth=[box(start=j * 2, end=j * 2 + 1)
                                                              for j in range(10)],
                                dsp=[box("dsp", start=j * 2, end=j * 2 + 1)
                                     for j in range(10)] +
                                    [box("dsp", low=120, high=130, start=j * 2, end=j * 2 + 1)
                                     for j in range(4)],
                                ml=[box("ml", start=j * 2, end=j * 2 + 1)
                                    for j in range(10)],
                                combined=[box("combined", start=j * 2, end=j * 2 + 1)
                                          for j in range(10)]))
    sessions += [session("negative-1", combined=[]), session("negative-2", combined=[])]
    baseline = evaluate_baseline(write(tmp_path, sessions))
    classification = {"examples": 130, "counts": {"errors": 0, "accepted": 30,
                                 "known_examples": 30, "known_accepted": 30,
                                 "correct": 27, "rejection_examples": 100,
                                 "rejection_errors": 0, "false_accepts": 0},
                      "selective_accuracy": 0.9,
                      "rejection_false_accept_rate": 0.0,
                      "provenance_complete": True,
                      "evaluation_splits": ["test"],
                      "evaluation_sessions": ["classification-1", "classification-2"]}
    passed = release_gate(baseline, classification)
    assert passed["passed"] and passed["status"] == "provisional_pass"
    assert passed["observed"]["fp_reduction"] == 1
    assert passed["observed"]["rejection_false_accept_rate"] == 0
    classification["selective_accuracy"] = 0.89
    assert not release_gate(baseline, classification)["passed"]
    baseline["splits"]["test"]["combined"]["recall"] = 0.9
    assert not release_gate(baseline, {**classification, "selective_accuracy": 0.9})["passed"]
    baseline["splits"]["test"]["combined"] = None
    assert not release_gate(baseline, classification)["passed"]


def test_release_gate_rejects_false_accepts_and_missing_or_inconsistent_metrics(tmp_path):
    sessions = [session("dev", split="development")]
    sessions += [session(f"positive-{i}", truth=[box(start=j * 2, end=j * 2 + 1)
                                                 for j in range(10)],
                         dsp=[box("dsp", start=j * 2, end=j * 2 + 1)
                              for j in range(10)] +
                             [box("dsp", low=120, high=130, start=j * 2, end=j * 2 + 1)
                              for j in range(4)],
                         combined=[box("combined", start=j * 2, end=j * 2 + 1)
                                   for j in range(10)]) for i in range(3)]
    sessions += [session("negative-1", combined=[]), session("negative-2", combined=[])]
    baseline = evaluate_baseline(write(tmp_path, sessions))
    counts = {"errors": 0, "accepted": 30, "known_examples": 30,
              "known_accepted": 30, "correct": 27, "rejection_examples": 100,
              "rejection_errors": 0, "false_accepts": 0}
    report = {"examples": 130, "counts": counts, "selective_accuracy": 0.9,
              "rejection_false_accept_rate": 0.0, "provenance_complete": True,
              "evaluation_splits": ["test"], "evaluation_sessions": ["one", "two"]}
    assert release_gate(baseline, report)["passed"]
    for changed in (
        {"counts": {**counts, "false_accepts": 100, "accepted": 130},
         "rejection_false_accept_rate": 1.0},
        {"counts": {key: value for key, value in counts.items() if key != "false_accepts"}},
        {"rejection_false_accept_rate": None},
        {"rejection_false_accept_rate": 0.02},
        {"counts": {**counts, "false_accepts": 1}},
        {"examples": 129},
        {"counts": {**counts, "rejection_examples": 20}, "examples": 50},
    ):
        assert not release_gate(baseline, {**report, **changed})["passed"]


def test_release_gate_rejects_missing_classifier_provenance_or_coverage(tmp_path):
    baseline = evaluate_baseline(write(tmp_path, [session("noise", combined=[])]))
    classification = {"counts": {"known_examples": 100, "known_accepted": 30,
                                 "correct": 27, "accepted": 30, "rejection_examples": 10},
                      "selective_accuracy": .9, "provenance_complete": False,
                      "evaluation_splits": ["development", "test"],
                      "evaluation_sessions": ["one"]}
    reasons = release_gate(baseline, classification)["reasons"]
    assert any("provenance" in reason for reason in reasons)
    assert any("held-out" in reason for reason in reasons)
    assert any("50%" in reason for reason in reasons)


def test_cli_baseline_and_gate_are_json(tmp_path, capsys):
    path = write(tmp_path, [session("empty")])
    cli.main(["baseline", str(path)])
    assert json.loads(capsys.readouterr().out)["splits"]["test"]["sessions"] == 1
    classification = tmp_path / "classification.json"
    classification.write_text("{}")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["release-gate", str(path), "--classification-report", str(classification)])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["passed"] is False
