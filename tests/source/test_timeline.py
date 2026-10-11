"""RecordingTimeline: runs, gaps and the elapsed <-> (run, sample) mapping."""

import json
from pathlib import Path

import numpy as np
import pytest

from msdr.source.timeline import RecordingError, RecordingTimeline

# 3-run fixture: rate change (run 0 -> 1), centre change (1 -> 2), a known
# gap before run 1 (trailing annotation of run 0) and an unknown one before
# run 2 (nothing recorded about it).
RUNS = [
    # (sample_rate, center_hz, samples, start_wall)
    (10_000, 100_000_000, 3000, 1_700_000_000.0),
    (20_000, 100_000_000, 4000, 1_700_000_000.4),
    (20_000, 101_000_000, 2000, None),
]
KNOWN_GAP = 500  # samples at run 0's rate -> 0.05 s


def run_samples(run_index: int, n: int) -> np.ndarray:
    """I = local sample index, Q = run index: a skipped or misplaced sample
    is visible in the data itself."""
    out = np.empty(2 * n, dtype="<i2")
    out[0::2] = np.arange(n) % 30_000
    out[1::2] = run_index
    return out


def write_three_runs(root: Path, *, with_index: bool = True) -> Path:
    d = root / "rec"
    d.mkdir()
    entries = []
    for i, (rate, center, n, start_wall) in enumerate(RUNS):
        stem = f"run-{i + 1:02d}_{rate}SPS_{center}Hz"
        (d / f"{stem}.sigmf-data").write_bytes(run_samples(i, n).tobytes())
        annotations = [{"core:sample_start": 0, "core:label": "REC"}]
        if i == 0:
            annotations.append(
                {
                    "core:sample_start": n,
                    "core:label": "gap",
                    "core:comment": f"missing ~{KNOWN_GAP} samples",
                }
            )
        capture = {"core:sample_start": 0, "core:frequency": center}
        meta = {
            "global": {"core:datatype": "ci16_le", "core:sample_rate": rate},
            "captures": [capture],
            "annotations": annotations,
        }
        (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
        entries.append(
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": rate,
                "center_hz": center,
                "samples": n,
                "start_wall": start_wall,
                "settings": {},
            }
        )
    if with_index:
        index = {"start_wall": RUNS[0][3], "end_wall": None, "preroll_s": 0, "runs": entries}
        (d / "recording.json").write_text(json.dumps(index))
    return d


def test_three_run_timeline_from_recording_json(tmp_path):
    tl = RecordingTimeline.from_path(write_three_runs(tmp_path))
    assert [(r.sample_rate_hz, r.center_hz, r.samples) for r in tl.runs] == [
        (rate, center, n) for rate, center, n, _ in RUNS
    ]
    assert [r.start_wall for r in tl.runs] == [r[3] for r in RUNS]
    assert [r.gap_before_samples for r in tl.runs] == [0, KNOWN_GAP, None]
    assert [r.gap_unknown for r in tl.runs] == [False, False, True]
    # Starts: 0; 0.3 + 0.05 known gap; 0.35 + 0.2 (unknown gap adds nothing).
    assert tl.run_start_s(1) == pytest.approx(0.35)
    assert tl.run_start_s(2) == pytest.approx(0.55)
    assert tl.total_elapsed_s == pytest.approx(0.65)
    assert tl.total_samples == 9000


def test_elapsed_and_locate_round_trip(tmp_path):
    tl = RecordingTimeline.from_path(write_three_runs(tmp_path))
    for run, sample in [(0, 0), (0, 2999), (1, 0), (1, 1234), (2, 0), (2, 1999)]:
        assert tl.locate(tl.elapsed_s(run, sample)) == (run, sample)
    assert tl.elapsed_s(1, 2000) == pytest.approx(0.35 + 0.1)
    # Inside the known gap -> start of the run after it.
    assert tl.locate(0.32) == (1, 0)
    # Clamped at both ends.
    assert tl.locate(-5) == (0, 0)
    assert tl.locate(99) == (2, 1999)


def test_interior_gap_uses_elapsed_time_without_changing_stored_indices(tmp_path):
    d = write_three_runs(tmp_path)
    meta_path = sorted(d.glob("*.sigmf-meta"))[0]
    meta = json.loads(meta_path.read_text())
    meta["annotations"].extend(
        [
            {
                "core:sample_start": 1000,
                "core:label": "gap",
                "core:comment": "missing ~500 samples",
            },
            {
                "core:sample_start": 2000,
                "core:label": "gap",
                "core:comment": "missing ~250 samples",
            },
        ]
    )
    meta_path.write_text(json.dumps(meta))
    tl = RecordingTimeline.from_path(d)
    assert tl.runs[0].interior_gaps == ((1000, 500), (2000, 250))
    assert tl.elapsed_s(0, 999) == pytest.approx(0.0999)
    assert tl.elapsed_s(0, 1000) == pytest.approx(0.15)
    assert tl.locate(0.11) == (0, 1000)  # inside the gap -> first post-gap sample
    assert tl.locate(0.15) == (0, 1000)
    assert tl.elapsed_s(0, 2000) == pytest.approx(0.275)
    assert tl.locate(0.26) == (0, 2000)
    assert tl.local_sample_at(0, tl.elapsed_s(0, 3000)) == 3000
    assert tl.run_start_s(1) == pytest.approx(0.425)  # both gaps + boundary gap
    assert tl.total_elapsed_s == pytest.approx(0.725)
    assert tl.total_samples == 9000
    assert tl.as_dicts()[0]["interior_gaps"][0]["missing_samples"] == 500

    # The same SigMF metadata without recording.json has identical timing.
    (d / "recording.json").unlink()
    assert RecordingTimeline.from_path(d).runs[0].interior_gaps == tl.runs[0].interior_gaps


def test_sigmf_metas_without_index(tmp_path):
    d = write_three_runs(tmp_path, with_index=False)
    meta_path = sorted(d.glob("*.sigmf-meta"))[1]
    meta = json.loads(meta_path.read_text())
    meta["captures"][0]["core:datetime"] = "2023-11-14T22:13:20.400Z"
    meta_path.write_text(json.dumps(meta))
    tl = RecordingTimeline.from_path(d)
    assert len(tl) == 3
    assert [r.gap_before_samples for r in tl.runs] == [0, KNOWN_GAP, None]
    assert tl.runs[0].start_wall is None  # no core:datetime -> unknown
    assert tl.runs[1].start_wall == pytest.approx(1_700_000_000.4)
    assert tl.runs[2].center_hz == 101_000_000


def test_runs_as_dicts(tmp_path):
    runs = RecordingTimeline.from_path(write_three_runs(tmp_path)).as_dicts()
    assert runs[2]["gap_unknown"] is True
    assert runs[1]["start_s"] == pytest.approx(0.35)
    assert {"index", "sample_rate_hz", "center_hz", "samples", "start_wall"} <= set(runs[0])


def test_unknown_file_is_refused(tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\0" * 16)
    with pytest.raises(RecordingError, match="unsupported"):
        RecordingTimeline.from_path(tmp_path / "x.bin")
