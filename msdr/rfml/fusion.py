"""Deterministic fusion of DSP and ML signal candidates.

Scores are retained exactly as supplied and are never calibrated or combined.
Matching uses frequency-time rectangle IoU; a match requires positive overlap on
both axes. One-to-one assignment prevents a broad candidate from joining several
nearby detections. DSP bounds remain authoritative for matched results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Candidate:
    """A raw DSP or ML candidate in Hz and seconds.

    ``score`` is a source-native, uncalibrated score. ``evidence`` carries
    optional source details such as labels, bins, or classifier outputs.
    Frequency bounds must be positive and increasing; time bounds must be
    finite and increasing.
    """

    source: Literal["dsp", "ml"]
    low_hz: float
    high_hz: float
    start_s: float
    end_s: float
    score: float
    evidence: Any = None

    def __post_init__(self) -> None:
        if self.source not in ("dsp", "ml"):
            raise ValueError("source must be 'dsp' or 'ml'")
        values = (self.low_hz, self.high_hz, self.start_s, self.end_s, self.score)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("candidate bounds and score must be finite")
        if self.low_hz <= 0 or self.high_hz <= self.low_hz:
            raise ValueError("frequency bounds must be positive and increasing")
        if self.start_s >= self.end_s:
            raise ValueError("time bounds must be increasing")


@dataclass(frozen=True)
class FusedCandidate:
    """A matched DSP/ML pair or an unmatched candidate.

    ``dsp``, ``ml``, and ``extra_ml`` retain complete source candidates,
    including separate raw scores and original bounds. ``extra_ml`` contains
    additional overlapping ML evidence attached to the matched DSP result. For
    a matched result, callers should use ``dsp`` bounds as reported bounds.
    """

    dsp: Candidate | None
    ml: Candidate | None
    overlap_iou: float | None
    extra_ml: tuple[Candidate, ...] = ()


def _rectangle_iou(left: Candidate, right: Candidate) -> float:
    freq_overlap = min(left.high_hz, right.high_hz) - max(left.low_hz, right.low_hz)
    time_overlap = min(left.end_s, right.end_s) - max(left.start_s, right.start_s)
    if freq_overlap <= 0 or time_overlap <= 0:
        return 0.0
    intersection = freq_overlap * time_overlap
    left_area = (left.high_hz - left.low_hz) * (left.end_s - left.start_s)
    right_area = (right.high_hz - right.low_hz) * (right.end_s - right.start_s)
    return intersection / (left_area + right_area - intersection)


def fuse_candidates(
    candidates: list[Candidate] | tuple[Candidate, ...],
    *,
    iou_threshold: float = 0.1,
    ml_only_threshold: float = 0.0,
) -> list[FusedCandidate]:
    """Fuse candidates with deterministic one-to-one frequency-time IoU matching.

    Pairs are considered by descending rectangle IoU, with input order breaking
    ties. Further ML candidates meeting the overlap gate attach as ``extra_ml``
    evidence to the best matching DSP result. Remaining ML candidates are
    emitted alone only when their raw score is strictly greater than
    ``ml_only_threshold``. Every unmatched DSP candidate survives.
    The thresholds are finite IoU/score values; scores are not presumed to be
    probabilities.
    """

    if not math.isfinite(iou_threshold) or not 0 <= iou_threshold <= 1:
        raise ValueError("iou_threshold must be finite and between 0 and 1")
    if not math.isfinite(ml_only_threshold):
        raise ValueError("ml_only_threshold must be finite")
    items = tuple(candidates)
    if any(not isinstance(item, Candidate) for item in items):
        raise TypeError("candidates must contain Candidate instances")

    dsps = [(index, item) for index, item in enumerate(items) if item.source == "dsp"]
    mls = [(index, item) for index, item in enumerate(items) if item.source == "ml"]
    possible = []
    for dsp_index, dsp in dsps:
        for ml_index, ml in mls:
            iou = _rectangle_iou(dsp, ml)
            if iou > 0 and iou >= iou_threshold:
                possible.append((-iou, dsp_index, ml_index, dsp, ml, iou))

    used_dsp: set[int] = set()
    used_ml: set[int] = set()
    paired: list[tuple[int, FusedCandidate]] = []
    matched_by_dsp: dict[int, tuple[int, Candidate, Candidate, float]] = {}
    for _negative_iou, dsp_index, ml_index, dsp, ml, iou in sorted(possible):
        if dsp_index in used_dsp or ml_index in used_ml:
            continue
        used_dsp.add(dsp_index)
        used_ml.add(ml_index)
        matched_by_dsp[dsp_index] = (min(dsp_index, ml_index), dsp, ml, iou)

    # Treat additional ML candidates that independently clear the overlap gate
    # as evidence for the best matched DSP rectangle, rather than emitting the
    # same detected signal again as an ML-only result. Each alternative attaches
    # to only one DSP candidate, preventing a broad result from bridging signals.
    extras: dict[int, list[tuple[int, Candidate]]] = {index: [] for index in matched_by_dsp}
    for ml_index, ml in mls:
        if ml_index in used_ml:
            continue
        overlaps = [
            (_rectangle_iou(matched[1], ml), dsp_index, ml_index, ml)
            for dsp_index, matched in matched_by_dsp.items()
            if _rectangle_iou(matched[1], ml) > 0
            and _rectangle_iou(matched[1], ml) >= iou_threshold
        ]
        if overlaps:
            _iou, dsp_index, candidate_index, candidate = sorted(
                overlaps, key=lambda item: (-item[0], item[1], item[2])
            )[0]
            extras[dsp_index].append((candidate_index, candidate))
            used_ml.add(candidate_index)

    for dsp_index, (order, dsp, ml, iou) in matched_by_dsp.items():
        extra_ml = tuple(candidate for _, candidate in sorted(extras[dsp_index]))
        paired.append((order, FusedCandidate(dsp, ml, iou, extra_ml)))

    for index, dsp in dsps:
        if index not in used_dsp:
            paired.append((index, FusedCandidate(dsp, None, None)))
    for index, ml in mls:
        if index not in used_ml and ml.score > ml_only_threshold:
            paired.append((index, FusedCandidate(None, ml, None)))
    return [result for _index, result in sorted(paired, key=lambda item: item[0])]
