import pytest

from msdr.rfml.fusion import Candidate, fuse_candidates


def _candidate(source, low=100.0, high=200.0, start=0.0, end=1.0, score=0.5, evidence=None):
    return Candidate(source, low, high, start, end, score, evidence)


def test_duplicate_candidates_pair_and_keep_source_data_separate():
    dsp = _candidate("dsp", 100, 200, 0, 1, 7.5, {"method": "fft"})
    ml = _candidate("ml", 110, 190, 0.1, 0.9, 0.83, {"label": "fm"})

    fused = fuse_candidates([dsp, ml])

    assert len(fused) == 1
    assert fused[0].dsp is dsp
    assert fused[0].ml is ml
    assert fused[0].dsp.score == 7.5
    assert fused[0].ml.score == 0.83
    assert fused[0].dsp.evidence == {"method": "fft"}
    assert fused[0].ml.evidence == {"label": "fm"}
    assert (fused[0].dsp.low_hz, fused[0].dsp.high_hz) == (100, 200)


def test_adjacent_signals_remain_separate_and_matching_is_one_to_one():
    dsp_left = _candidate("dsp", 100, 150)
    dsp_right = _candidate("dsp", 150, 200)
    broad_ml = _candidate("ml", 100, 200, score=0.9)

    fused = fuse_candidates([dsp_left, dsp_right, broad_ml], iou_threshold=0.1)

    assert len(fused) == 2
    assert sum(item.ml is not None for item in fused) == 1
    assert sum(item.dsp is not None and item.ml is None for item in fused) == 1


def test_additional_overlapping_ml_evidence_is_attached_without_duplicate_output():
    dsp = _candidate("dsp", 100, 200)
    selected = _candidate("ml", 105, 195, score=0.9, evidence="selected")
    duplicate = _candidate("ml", 110, 190, score=0.8, evidence="duplicate")
    separate = _candidate("ml", 300, 400, score=0.9, evidence="separate")

    fused = fuse_candidates([dsp, selected, duplicate, separate])

    assert len(fused) == 2
    assert fused[0].dsp is dsp
    assert fused[0].ml is selected
    assert fused[0].extra_ml == (duplicate,)
    assert fused[1].dsp is None
    assert fused[1].ml is separate


def test_frequency_overlap_without_time_overlap_does_not_match():
    dsp = _candidate("dsp", 100, 200, 0, 1)
    ml = _candidate("ml", 100, 200, 1, 2, score=0.8)

    fused = fuse_candidates([dsp, ml])

    assert [(item.dsp, item.ml) for item in fused] == [(dsp, None), (None, ml)]


def test_threshold_controls_pairing_and_ml_only_retention():
    dsp = _candidate("dsp")
    weak_ml = _candidate("ml", 150, 250, score=0.4)
    strong_ml = _candidate("ml", 150, 250, score=0.8)

    fused = fuse_candidates([dsp, weak_ml, strong_ml], iou_threshold=0.6, ml_only_threshold=0.5)

    assert len(fused) == 2
    assert fused[0] == type(fused[0])(dsp, None, None)
    assert fused[1].dsp is None
    assert fused[1].ml is strong_ml


@pytest.mark.parametrize(
    "kwargs",
    [
        {"low": 0, "high": 10},
        {"low": 10, "high": 10},
        {"low": 20, "high": 10},
        {"start": 1, "end": 1},
        {"start": 2, "end": 1},
        {"score": float("nan")},
        {"high": float("inf")},
    ],
)
def test_candidate_rejects_malformed_or_non_finite_values(kwargs):
    values = {"low": 100.0, "high": 200.0, "start": 0.0, "end": 1.0, "score": 0.5}
    values.update(kwargs)
    with pytest.raises(ValueError):
        _candidate("dsp", **values)


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan")])
def test_iou_threshold_must_be_in_unit_interval(threshold):
    with pytest.raises(ValueError, match="iou_threshold"):
        fuse_candidates([], iou_threshold=threshold)


def test_unknown_source_and_non_candidate_input_are_rejected():
    with pytest.raises(ValueError, match="source"):
        _candidate("radio")
    with pytest.raises(TypeError, match="Candidate"):
        fuse_candidates([object()])
