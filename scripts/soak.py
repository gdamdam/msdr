#!/usr/bin/env python3
"""Long-running soak (S5 plan §9): drives a real AppService against an
in-process FakeSDRconnect for `--minutes` (default 480 = 8 hours) on one
asyncio loop -- no uvicorn, no real network beyond the loopback websocket to
the fake. Exercises the three things an all-day session actually does:

- wide-view survey ON (spectrum -> occupancy/analyzer dimensions),
- BAND DECODE with pocsag+aprs on the live (fake) radio -- exactly the
  subprocess-lifecycle stress an 8-hour run needs, even though the fake
  never emits real IQ over the wire (SDRconnect's IQ/audio streaming
  protocols aren't reproduced by FakeSDRconnect, only spectrum is; see
  msdr/radio/fake_server.py),
- a replay loop of a synthetic 2-run recording with a review decode session
  (`ais`, a built-in Python decoder -- no external binary dependency) that's
  torn down and restarted every 60 s, with a periodic seek.

Samples every `--sample-s` seconds: RSS (resource.getrusage), event-loop lag
(a 100 ms timer's own drift, sampled continuously and summarised over the
whole run), queue/drop counters from the running services' own status
dicts, and the open asyncio task count. Writes newline-delimited JSON
samples plus a summary; exits non-zero if RSS grew more than 30% from the
2-minute mark to the end, or loop-lag p99 exceeded 250 ms.

Run from the repo root:

    .venv/bin/python scripts/soak.py --minutes 480 --out soak-runs
"""

import argparse
import asyncio
import contextlib
import json
import resource
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from msdr.api.service import AppService  # noqa: E402
from msdr.decoder.registry import DECODERS, effective_binaries, resolve_binaries  # noqa: E402
from msdr.radio.fake_server import FakeSDRconnect  # noqa: E402
from msdr.radio.sdrconnect import SDRconnectAdapter  # noqa: E402
from msdr.settings import DecoderSettings, Settings  # noqa: E402
from msdr.storage.db import Database  # noqa: E402

_LAG_INTERVAL_S = 0.1
_RESTART_EVERY_S = 60.0
_BAND_DECODERS = ("pocsag", "aprs")
_REPLAY_DECODER = "ais"  # built-in Python backend: no binary to skip.
_SOAK_FS = 2_000_000
_SOAK_CENTER_HZ = 462_562_500
_SOAK_RUN_SECONDS = 3.0
_RSS_GROWTH_GATE_PCT = 30.0
_LAG_P99_GATE_MS = 250.0
_RSS_BASELINE_AT_S = 120.0  # "minute 2" (plan §9).


def _rss_kb() -> float:
    """resource.getrusage's ru_maxrss: KB on Linux, bytes on macOS -- always
    the process's peak so far (never decreases), which is exactly what a
    growth-over-time leak check wants."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024.0 if sys.platform == "darwin" else float(rss)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100.0) * (len(sorted_values) - 1)
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


async def _lag_monitor(samples_ms: list[float]) -> None:
    """Event-loop lag (plan §2): a 100 ms timer's own drift. Runs for the
    whole soak; `run_soak` cancels it at shutdown."""
    try:
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(_LAG_INTERVAL_S)
            drift_s = (time.monotonic() - t0) - _LAG_INTERVAL_S
            samples_ms.append(max(0.0, drift_s) * 1000.0)
    except asyncio.CancelledError:
        raise


def _write_soak_recording(root: Path) -> Path:
    """A synthetic 2-run recording (plan §2) for the review replay loop --
    complex Gaussian noise at a realistic IQ rate (2 MS/s, same as
    tests/decoder/test_parity.py's fixtures), not a modulated signal: the
    soak cares about process/task lifecycle over hours, not decode content.
    """
    d = root / "soak-recording"
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260927)
    entries = []
    start_wall = time.time()
    n = int(_SOAK_RUN_SECONDS * _SOAK_FS)
    for i in range(2):
        noise = (0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(
            np.complex64
        )
        iq16 = np.empty(2 * n, dtype="<i2")
        scale = 32767.0
        iq16[0::2] = np.clip(noise.real * scale, -32768, 32767).astype("<i2")
        iq16[1::2] = np.clip(noise.imag * scale, -32768, 32767).astype("<i2")
        stem = f"run-{i + 1:02d}_{_SOAK_FS}SPS_{_SOAK_CENTER_HZ}Hz"
        (d / f"{stem}.sigmf-data").write_bytes(iq16.tobytes())
        meta = {
            "global": {
                "core:datatype": "ci16_le",
                "core:sample_rate": _SOAK_FS,
                "core:version": "1.0.0",
            },
            "captures": [{"core:sample_start": 0, "core:frequency": _SOAK_CENTER_HZ}],
            "annotations": [],
        }
        (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
        entries.append(
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": _SOAK_FS,
                "center_hz": _SOAK_CENTER_HZ,
                "samples": n,
                "start_wall": start_wall + i * _SOAK_RUN_SECONDS,
                "settings": {},
            }
        )
    index = {"start_wall": start_wall, "end_wall": None, "preroll_s": 0, "runs": entries}
    (d / "recording.json").write_text(json.dumps(index))
    return d


async def _wait_connected(adapter: SDRconnectAdapter, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not adapter.connected:
        if time.monotonic() > deadline:
            raise RuntimeError(
                "soak: SDRconnectAdapter never connected to the in-process FakeSDRconnect"
            )
        await asyncio.sleep(0.02)


def _band_decoders_available(bin_dirs: list[str]) -> tuple[list[str], list[str]]:
    binaries = resolve_binaries(bin_dirs)
    available, skipped = [], []
    for decoder_id in _BAND_DECODERS:
        spec = DECODERS[decoder_id]
        names = effective_binaries(spec, binaries)
        if names and all(binaries.get(name) for name in names):
            available.append(decoder_id)
        else:
            skipped.append(decoder_id)
    return available, skipped


def _summarize(samples: list[dict], lag_samples_ms: list[float], warnings: list[str]) -> dict:
    lag_sorted = sorted(lag_samples_ms)
    p50 = round(_percentile(lag_sorted, 50), 2)
    p99 = round(_percentile(lag_sorted, 99), 2)
    if not samples:
        return {
            "samples": 0,
            "lag_p50_ms": p50,
            "lag_p99_ms": p99,
            "warnings": warnings,
            "ok": False,
            "reason": "no samples collected",
        }
    baseline_idx = next(
        (i for i, s in enumerate(samples) if s["elapsed_s"] >= _RSS_BASELINE_AT_S), 0
    )
    rss_baseline = samples[baseline_idx]["rss_kb"]
    rss_end = samples[-1]["rss_kb"]
    rss_growth_pct = (
        round((rss_end - rss_baseline) / rss_baseline * 100.0, 2) if rss_baseline > 0 else 0.0
    )
    ok = rss_growth_pct <= _RSS_GROWTH_GATE_PCT and p99 <= _LAG_P99_GATE_MS
    return {
        "samples": len(samples),
        "duration_s": samples[-1]["elapsed_s"],
        "rss_start_kb": round(samples[0]["rss_kb"], 1),
        f"rss_at_{int(_RSS_BASELINE_AT_S)}s_kb": round(rss_baseline, 1),
        "rss_end_kb": round(rss_end, 1),
        "rss_growth_pct_from_baseline": rss_growth_pct,
        "lag_p50_ms": p50,
        "lag_p99_ms": p99,
        "open_tasks_end": samples[-1]["open_tasks"],
        "final_drops": samples[-1]["drops"],
        "warnings": warnings,
        "ok": ok,
    }


async def run_soak(minutes: float, sample_s: float, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = out_dir / f"soak-{ts}.jsonl"
    summary_path = out_dir / f"soak-{ts}.summary.json"

    default_bin_dirs = DecoderSettings().bin_dirs
    band_decoders, skipped_band = _band_decoders_available(default_bin_dirs)
    for decoder_id in skipped_band:
        print(f"soak: skipping band decoder {decoder_id!r}: binary missing", file=sys.stderr)

    warnings: list[str] = []
    samples: list[dict] = []
    lag_samples_ms: list[float] = []

    with tempfile.TemporaryDirectory(prefix="msdr-soak-") as tmp:
        tmp_path = Path(tmp)
        fake = FakeSDRconnect(port=0)
        await fake.start()
        settings = Settings(
            host="127.0.0.1",
            port=fake.port,
            web_port=0,
            db_path=tmp_path / "m.db",
            recorder_root=str(tmp_path / "recorder"),
            decoder=DecoderSettings(
                calls_dir=str(tmp_path / "decoder" / "calls"),
                fax_dir=str(tmp_path / "decoder" / "fax"),
            ),
        )
        db = Database(settings.db_path)
        # startup_grace_s=0.0: skip the real client's post-launch settle
        # delay (default 3 s) -- there's no real SDRconnect to (re)launch
        # here, just the in-process fake, already listening.
        adapter = SDRconnectAdapter(settings.host, settings.port, startup_grace_s=0.0)
        service = AppService(settings, adapter, db, tmp_path / "settings.json")
        await service.start()
        await _wait_connected(adapter)
        lag_task = asyncio.create_task(_lag_monitor(lag_samples_ms))
        rec_dir = _write_soak_recording(tmp_path)

        try:
            await service.survey_start()
            service.survey_dimensions(["activity", "bandwidth", "modulation", "voice"])
            if band_decoders:
                await service.decoder_band_start(band_decoders)
            await service.survey_replay(str(rec_dir), loop=True)
            await service.review_decode_start([_REPLAY_DECODER], mode="single")

            t_start = time.monotonic()
            deadline = t_start + minutes * 60.0
            next_restart_at = t_start + _RESTART_EVERY_S
            tick = 0
            jsonl_fh = jsonl_path.open("w")
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(sample_s, remaining))
                    now = time.monotonic()

                    if now >= next_restart_at:
                        try:
                            await service.review_decode_stop()
                            await service.review_decode_start([_REPLAY_DECODER], mode="single")
                        except Exception as exc:  # noqa: BLE001 - the loop must survive
                            warnings.append(f"restart failed: {exc}")
                        next_restart_at = now + _RESTART_EVERY_S

                    try:
                        service.review_seek(float(tick % 3) * (_SOAK_RUN_SECONDS / 2))
                    except Exception as exc:  # noqa: BLE001 - seek is best-effort
                        warnings.append(f"seek failed: {exc}")
                    tick += 1

                    survey_status = service.survey_status() or {}
                    band_status = (service.decoder_status() or {}).get("band") or {}
                    review = service.review_status()
                    replay_status = review.get("replay") or {}
                    receiver_status = review.get("receiver") or {}
                    decode_status = review.get("decode") or {}
                    lag_sorted = sorted(lag_samples_ms)
                    sample = {
                        "ts": time.time(),
                        "elapsed_s": round(now - t_start, 1),
                        "rss_kb": round(_rss_kb(), 1),
                        "open_tasks": len(asyncio.all_tasks()),
                        "lag_p50_ms": round(_percentile(lag_sorted, 50), 2),
                        "lag_p99_ms": round(_percentile(lag_sorted, 99), 2),
                        "survey_active": bool(survey_status.get("active")),
                        "drops": {
                            "band_iq_dropped": (band_status.get("iq") or {}).get("dropped", 0),
                            "band_backend_dropped_frames": band_status.get(
                                "backend_dropped_frames", 0
                            ),
                            "replay_dropped_frames": replay_status.get("dropped_frames", 0),
                            "replay_behind_s": replay_status.get("behind_s", 0.0),
                            "receiver_dropped_blocks": receiver_status.get("dropped_blocks", 0),
                            "decode_dropped_frames": decode_status.get("dropped_frames", 0),
                        },
                    }
                    samples.append(sample)
                    jsonl_fh.write(json.dumps(sample) + "\n")
                    jsonl_fh.flush()
            finally:
                jsonl_fh.close()
        finally:
            lag_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lag_task
            await service.stop()
            await fake.stop()

    summary = _summarize(samples, lag_samples_ms, warnings)
    summary["band_decoders"] = band_decoders
    summary["band_decoders_skipped"] = skipped_band
    summary["jsonl_path"] = str(jsonl_path)
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes", type=float, default=480.0, help="soak duration in minutes (default: 480 = 8h)"
    )
    parser.add_argument(
        "--sample-s", type=float, default=10.0, help="seconds between samples (default: 10)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("soak-runs"), help="output directory (default: soak-runs)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = asyncio.run(run_soak(args.minutes, args.sample_s, args.out))
    print(json.dumps(summary, indent=2))
    if not summary.get("ok"):
        print("soak: FAILED gate (RSS growth or event-loop lag out of bounds)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
