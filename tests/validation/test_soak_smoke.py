"""scripts/soak.py smoke test: a ~20 s run with a small config,
asserting a summary is produced with no exception. Not skipped/opt-in --
runs by default, but kept well under the 40 s budget."""

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.soak import run_soak  # noqa: E402


async def test_soak_smoke_runs_and_produces_a_summary(tmp_path):
    t0 = time.monotonic()
    # 0.33 min ~= 20 s; sample every 5 s so a handful of samples land.
    summary = await run_soak(minutes=0.33, sample_s=5.0, out_dir=tmp_path)
    elapsed = time.monotonic() - t0
    assert elapsed < 40.0, f"soak smoke took {elapsed:.1f}s, budget is 40s"

    assert summary["samples"] >= 1
    assert "rss_start_kb" in summary
    assert "rss_end_kb" in summary
    assert "lag_p50_ms" in summary and "lag_p99_ms" in summary
    assert summary["lag_p99_ms"] >= 0.0
    assert "final_drops" in summary
    assert isinstance(summary["band_decoders"], list)
    assert isinstance(summary["band_decoders_skipped"], list)
    assert set(summary["band_decoders"]) | set(summary["band_decoders_skipped"]) == {
        "pocsag",
        "aprs",
    }

    jsonl_path = Path(summary["jsonl_path"])
    summary_path = Path(summary["summary_path"])
    assert jsonl_path.is_file()
    assert summary_path.is_file()
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == summary["samples"]


async def test_soak_smoke_handles_a_run_shorter_than_one_sample_interval(tmp_path):
    # Duration shorter than sample_s: the loop still takes exactly one final
    # sample before exiting, rather than raising or hanging.
    summary = await run_soak(minutes=0.01, sample_s=5.0, out_dir=tmp_path)
    assert summary["samples"] == 1
    jsonl_path = Path(summary["jsonl_path"])
    assert len(jsonl_path.read_text().strip().splitlines()) == 1
