"""Offline, paired detection evaluation against explicit operator annotations.

The manifest contains already-generated predictions from the *same* recordings.
It never treats a DSP or ML prediction as ground truth and runs no model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from msdr.rfml.fusion import Candidate, _rectangle_iou, fuse_candidates

MATCH_IOU = 0.1
MIN_TEST_SESSIONS = 5
MIN_TEST_TRUTHS = 30
MIN_TEST_DSP_FALSE_POSITIVES = 10
MIN_TEST_NEGATIVE_SESSIONS = 2
MIN_TEST_NEGATIVE_MINUTES = 10.0
MIN_CLASSIFIER_KNOWN_ACCEPTED = 30
MIN_CLASSIFIER_REJECTION_EXAMPLES = 100
MAX_CLASSIFIER_REJECTION_FALSE_ACCEPT_RATE = 0.05


def _number(value, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric")
    result = float(value)
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _regions(rows, source: str, session: dict) -> list[Candidate]:
    if not isinstance(rows, list):
        raise ValueError(f"{source} must be an array")
    low, high = session["frequency_range_hz"]
    duration = session["duration_s"]
    regions = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{source}[{index}] must be an object")
        if source != "truth" and row.get("source") != source:
            raise ValueError(f"{source}[{index}] must declare source={source}")
        region = Candidate(
            source=source if source in {"dsp", "ml"} else "dsp",
            low_hz=_number(row.get("low_hz"), "low_hz"),
            high_hz=_number(row.get("high_hz"), "high_hz"),
            start_s=_number(row.get("start_s"), "start_s"),
            end_s=_number(row.get("end_s"), "end_s"),
            score=1.0 if source == "truth" else _number(row.get("score"), "score"),
        )
        if not (low <= region.low_hz < region.high_hz <= high):
            raise ValueError(f"{source}[{index}] exceeds session frequency range")
        if not (0 <= region.start_s < region.end_s <= duration):
            raise ValueError(f"{source}[{index}] exceeds session duration")
        regions.append(region)
    return regions


def load_manifest(path: Path) -> list[dict]:
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("baseline manifest requires schema_version 1")
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("baseline manifest requires nonempty sessions")
    seen = set()
    recordings = set()
    normalized = []
    for session in sessions:
        if not isinstance(session, dict):
            raise ValueError("session must be an object")
        identifier = session.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ValueError("session IDs must be unique nonempty strings")
        seen.add(identifier)
        if session.get("split") not in {"development", "test"}:
            raise ValueError("split must be development or test")
        if session.get("annotation_complete") is not True:
            raise ValueError("each session requires annotation_complete=true")
        if not isinstance(session.get("recording"), str) or not session["recording"].strip():
            raise ValueError("recording identifier is required")
        if session["recording"] in recordings:
            raise ValueError("recording identifiers must be unique across sessions")
        recordings.add(session["recording"])
        duration = _number(session.get("duration_s"), "duration_s", positive=True)
        bounds = session.get("frequency_range_hz")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("frequency_range_hz must be [low, high]")
        low, high = (_number(value, "frequency_range_hz", positive=True) for value in bounds)
        if low >= high:
            raise ValueError("frequency_range_hz must increase")
        data = {"id": identifier, "recording": session["recording"],
                "split": session["split"], "duration_s": duration,
                "frequency_range_hz": [low, high]}
        for source in ("truth", "dsp", "ml"):
            data[source] = _regions(session.get(source), source, data)
        data["combined"] = (_regions(session["combined"], "combined", data)
                            if "combined" in session else None)
        normalized.append(data)
    return normalized


def _score(sessions: list[dict], source: str) -> dict:
    tp = fp = fn = 0
    ious = []
    matches = []
    for session in sessions:
        truth = session["truth"]
        if source == "fusion":
            predictions = [item.dsp or item.ml for item in fuse_candidates(
                [*session["dsp"], *session["ml"]]
            )]
        else:
            predictions = session[source]
        edges = sorted(
            ((-_rectangle_iou(target, predicted), ti, pi)
             for ti, target in enumerate(truth)
             for pi, predicted in enumerate(predictions)
             if _rectangle_iou(target, predicted) >= MATCH_IOU),
        )
        used_truth, used_prediction = set(), set()
        for negative_iou, ti, pi in edges:
            if ti in used_truth or pi in used_prediction:
                continue
            used_truth.add(ti)
            used_prediction.add(pi)
            iou = -negative_iou
            ious.append(iou)
            matches.append({"session_id": session["id"], "truth_index": ti,
                            "prediction_index": pi, "boundary_iou": iou})
        tp += len(used_truth)
        fp += len(predictions) - len(used_prediction)
        fn += len(truth) - len(used_truth)
    minutes = sum(session["duration_s"] for session in sessions) / 60
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "false_positives_per_minute": fp / minutes if minutes else None,
            "mean_boundary_iou": sum(ious) / len(ious) if ious else None,
            "min_boundary_iou": min(ious) if ious else None,
            "matches": matches}


def evaluate_baseline(path: Path) -> dict:
    sessions = load_manifest(path)
    splits = {}
    for split in ("development", "test"):
        subset = [session for session in sessions if session["split"] == split]
        splits[split] = {
            "sessions": len(subset), "truth_regions": sum(len(s["truth"]) for s in subset),
            "negative_sessions": sum(not s["truth"] for s in subset),
            "negative_minutes": sum(s["duration_s"] for s in subset if not s["truth"]) / 60,
            "duration_minutes": sum(s["duration_s"] for s in subset) / 60,
            "recordings": [s["recording"] for s in subset],
            "dsp": _score(subset, "dsp"), "ml": _score(subset, "ml"),
            "fusion": _score(subset, "fusion"),
            "combined": (_score(subset, "combined")
                         if all(s["combined"] is not None for s in subset) and subset else None),
        }
    return {"schema_version": 1, "manifest": str(path), "match_iou_threshold": MATCH_IOU,
            "splits": splits, "limitations": (
                "Operator annotations and prediction provenance are supplied by the manifest; "
                "representativeness and annotation quality are not independently established.")}


def release_gate(baseline: dict, classification: dict) -> dict:
    """Fail closed on missing support, errors, and missing comparative metrics."""
    if baseline.get("schema_version") != 1 or not isinstance(baseline.get("splits"), dict):
        raise ValueError("invalid baseline report")
    test = baseline["splits"].get("test", {})
    development = baseline["splits"].get("development", {})
    dsp = test.get("dsp", {})
    combined = test.get("combined") or {}
    counts = classification.get("counts") if isinstance(classification, dict) else None
    counts = counts if isinstance(counts, dict) else {}
    reasons = []
    def require(condition, message):
        if not condition:
            reasons.append(message)
    require(development.get("sessions", 0) >= 1, "development split requires at least one session")
    require(test.get("sessions", 0) >= MIN_TEST_SESSIONS,
            f"test split requires at least {MIN_TEST_SESSIONS} sessions")
    require(not set(development.get("recordings", [])) & set(test.get("recordings", [])),
            "development and test recordings overlap")
    require(test.get("truth_regions", 0) >= MIN_TEST_TRUTHS,
            f"test split requires at least {MIN_TEST_TRUTHS} truth regions")
    require(test.get("negative_sessions", 0) >= MIN_TEST_NEGATIVE_SESSIONS,
            f"test split requires at least {MIN_TEST_NEGATIVE_SESSIONS} negative sessions")
    require(test.get("negative_minutes", 0) >= MIN_TEST_NEGATIVE_MINUTES,
            f"test split requires at least {MIN_TEST_NEGATIVE_MINUTES:g} negative minutes")
    require(dsp.get("false_positives", 0) >= MIN_TEST_DSP_FALSE_POSITIVES,
            f"DSP must have at least {MIN_TEST_DSP_FALSE_POSITIVES} test false positives")
    reduction = None
    require(test.get("combined") is not None,
            "all test sessions require explicit combined predictions")
    if (isinstance(dsp.get("false_positives"), int) and dsp["false_positives"] > 0
            and isinstance(combined.get("false_positives"), int)):
        reduction = 1 - combined["false_positives"] / dsp["false_positives"]
    require(reduction is not None and reduction >= 0.30,
            "combined false-positive reduction must be at least 30% versus DSP")
    recall_loss = None
    if (isinstance(dsp.get("recall"), (int, float))
            and isinstance(combined.get("recall"), (int, float))):
        recall_loss = dsp["recall"] - combined["recall"]
    require(recall_loss is not None and recall_loss <= 0.05,
            "combined recall loss must be at most 5 percentage points versus DSP")
    require(classification.get("provenance_complete") is True
            if isinstance(classification, dict) else False,
            "classifier evaluation requires provenance on every row")
    require(classification.get("evaluation_splits") == ["test"]
            if isinstance(classification, dict) else False,
            "classifier evaluation must contain only held-out test rows")
    session_ids = (classification.get("evaluation_sessions")
                   if isinstance(classification, dict) else None)
    require(isinstance(session_ids, list) and len(session_ids) >= 2
            and all(isinstance(item, str) and item.strip() for item in session_ids)
            and len(set(session_ids)) == len(session_ids),
            "classifier evaluation requires at least two independent test sessions")
    require(type(counts.get("errors")) is int and counts["errors"] == 0
            and type(counts.get("accepted")) is int and counts["accepted"] > 0,
            "classifier evaluation must contain accepted predictions and no errors")
    require(type(counts.get("known_accepted")) is int
            and counts["known_accepted"] >= MIN_CLASSIFIER_KNOWN_ACCEPTED,
            f"classifier requires at least {MIN_CLASSIFIER_KNOWN_ACCEPTED} accepted known examples")
    known_accepted = counts.get("known_accepted", 0)
    correct = counts.get("correct", -1)
    known_examples = counts.get("known_examples", -1)
    valid_counts = all(type(value) is int for value in (known_accepted, correct, known_examples))
    require(valid_counts and 0 <= correct <= known_accepted <= known_examples,
            "classifier counts must be consistent")
    known_coverage = (known_accepted / known_examples
                      if valid_counts and known_examples > 0 else None)
    require(known_coverage is not None and known_coverage >= 0.50,
            "accepted known-class coverage must be at least 50%")
    rejection_examples = counts.get("rejection_examples")
    false_accepts = counts.get("false_accepts")
    rejection_errors = counts.get("rejection_errors")
    accepted = counts.get("accepted")
    valid_rejection_counts = (
        all(type(value) is int for value in
            (rejection_examples, false_accepts, rejection_errors, accepted, counts.get("errors")))
        and rejection_examples >= 0 and rejection_errors >= 0
        and 0 <= false_accepts <= rejection_examples - rejection_errors
        and rejection_errors <= counts["errors"]
        and valid_counts and counts["errors"] - rejection_errors <= known_examples
        and known_accepted <= known_examples - (counts["errors"] - rejection_errors)
        and accepted == known_accepted + false_accepts
    )
    require(valid_rejection_counts, "classifier rejection and acceptance counts must be consistent")
    examples = classification.get("examples") if isinstance(classification, dict) else None
    require(type(examples) is int and valid_counts and valid_rejection_counts
            and examples == known_examples + rejection_examples,
            "classifier example count must agree with known and rejection counts")
    require(type(rejection_examples) is int
            and rejection_examples >= MIN_CLASSIFIER_REJECTION_EXAMPLES,
            f"classifier requires at least {MIN_CLASSIFIER_REJECTION_EXAMPLES} rejection examples")
    rejection_false_accept_rate = (
        classification.get("rejection_false_accept_rate")
        if isinstance(classification, dict) else None
    )
    measured_rejection_false_accept_rate = (
        false_accepts / (rejection_examples - rejection_errors)
        if valid_rejection_counts and rejection_examples > rejection_errors else None
    )
    require(measured_rejection_false_accept_rate is not None
            and type(rejection_false_accept_rate) in (int, float)
            and math.isfinite(rejection_false_accept_rate)
            and math.isclose(rejection_false_accept_rate,
                             measured_rejection_false_accept_rate,
                             rel_tol=1e-9, abs_tol=1e-9),
            "classifier rejection false-accept rate must agree with counts")
    require(measured_rejection_false_accept_rate is not None
            and measured_rejection_false_accept_rate <= MAX_CLASSIFIER_REJECTION_FALSE_ACCEPT_RATE,
            "classifier rejection false-accept rate must be at most 5%")
    accuracy = (classification.get("selective_accuracy")
                if isinstance(classification, dict) else None)
    require(valid_counts and known_accepted > 0 and type(accuracy) in (int, float)
            and math.isfinite(accuracy) and math.isclose(accuracy, correct / known_accepted,
                                                       rel_tol=1e-9, abs_tol=1e-9),
            "classifier selective accuracy must agree with counts")
    require(type(accuracy) in (int, float) and math.isfinite(accuracy) and accuracy >= 0.90,
            "accepted classification accuracy must be at least 90%")
    return {"schema_version": 1, "passed": not reasons,
            "status": "provisional_pass" if not reasons else "insufficient_or_failed",
            "thresholds": {"fp_reduction_min": 0.30, "recall_loss_max": 0.05,
                           "accepted_classification_accuracy_min": 0.90,
                           "accepted_known_coverage_min": 0.50,
                           "rejection_false_accept_rate_max":
                               MAX_CLASSIFIER_REJECTION_FALSE_ACCEPT_RATE,
                           "rejection_examples_min": MIN_CLASSIFIER_REJECTION_EXAMPLES},
            "observed": {"fp_reduction": reduction, "recall_loss": recall_loss,
                         "accepted_classification_accuracy": accuracy,
                         "accepted_known_coverage": known_coverage,
                         "rejection_false_accept_rate": measured_rejection_false_accept_rate},
            "reasons": reasons,
            "limitations": (
                "A pass is provisional and depends on operator truth, independent recording "
                "splits, and representative recordings. It is not field validation.")}
