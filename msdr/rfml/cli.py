"""Install the pinned RF checkpoint or benchmark native inference without an SDR."""

import argparse
import contextlib
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from msdr.rfml.classifier import MANIFEST, RFMLClassifier

DEFAULT_PATH = "~/.cache/msdr/models/xcit_narrowband.ckpt"


def download(path: Path, manifest=None) -> dict:
    manifest = MANIFEST if manifest is None else manifest
    path = path.expanduser()
    if path.exists():
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["sha256"]:
                raise ValueError("destination exists with different contents; choose another path")
        return {"path": str(path), "sha256": manifest["sha256"], "downloaded": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(manifest["url"], timeout=30) as response:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                tmp = Path(stream.name)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > manifest["size_bytes"]:
                        raise ValueError("download exceeded expected checkpoint size")
                    digest.update(chunk)
                    stream.write(chunk)
        if total != manifest["size_bytes"] or digest.hexdigest() != manifest["sha256"]:
            raise ValueError("checkpoint download checksum/size mismatch")
        os.replace(tmp, path)
        return {"path": str(path), "sha256": manifest["sha256"], "downloaded": True}
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def benchmark(args) -> dict:
    settings = SimpleNamespace(
        model_path=args.model_path, device=args.device, score_threshold=args.score_threshold
    )
    classifier = RFMLClassifier(settings)
    started = time.perf_counter()
    classifier.load()
    load_s = time.perf_counter() - started
    if args.iq:
        iq = np.load(Path(args.iq).expanduser(), allow_pickle=False, mmap_mode="r")
        input_kind = "provided complex IQ; labels not independently verified"
    else:
        rng = np.random.default_rng(7)
        iq = (rng.normal(size=4096) + 1j * rng.normal(size=4096)).astype(np.complex64)
        input_kind = "synthetic noise: throughput diagnostic, not RF accuracy validation"
    # Includes input validation/normalization and transfers. cpu().numpy()
    # synchronizes results, so MPS scheduling latency is not mistaken for inference.
    for _ in range(3):
        classifier.predict(iq, args.sample_rate)
    latencies = []
    result = None
    for _ in range(args.iterations):
        start = time.perf_counter()
        result = classifier.predict(iq, args.sample_rate)
        latencies.append((time.perf_counter() - start) * 1000)
    return {
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "model_id": MANIFEST["id"],
        "sha256": MANIFEST["sha256"],
        "backend_requested": args.device,
        "backend_actual": classifier.backend,
        "fallback_reason": classifier.fallback_reason,
        "torch": classifier.torch.__version__,
        "load_s": load_s,
        "input": input_kind,
        "channel_sample_rate_hz": args.sample_rate,
        "window_samples": 4096,
        "window_s": 4096 / args.sample_rate,
        "iterations": args.iterations,
        "p50_ms": float(np.median(latencies)),
        "p95_ms": float(np.percentile(latencies, 95)),
        "prediction": result,
        "detector": "not run by this classifier benchmark; use the separate offline detect command",
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("download", help="download and verify the supported 76 MB RF checkpoint")
    fetch.add_argument("--model-path", default=DEFAULT_PATH)
    bench = sub.add_parser("benchmark", help="benchmark classification; no SDR required")
    bench.add_argument("--model-path", default=DEFAULT_PATH)
    bench.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    bench.add_argument("--iterations", type=int, default=20)
    bench.add_argument("--sample-rate", type=float, default=50_000)
    bench.add_argument("--score-threshold", type=float, default=0.70)
    bench.add_argument("--iq", help="optional .npy containing one-dimensional complex IQ")
    evaluate_parser = sub.add_parser("evaluate", help="evaluate manually labeled IQ JSONL corpus")
    evaluate_parser.add_argument("manifest", type=Path)
    evaluate_parser.add_argument("--model-path", default=DEFAULT_PATH)
    evaluate_parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    evaluate_parser.add_argument("--score-threshold", type=float, default=0.70)
    baseline_parser = sub.add_parser(
        "baseline", help="score paired DSP, ML and fusion detections against annotated recordings"
    )
    baseline_parser.add_argument("manifest", type=Path)
    gate_parser = sub.add_parser("release-gate", help="evaluate provisional RFML evidence gate")
    gate_parser.add_argument("manifest", type=Path, help="detection baseline manifest")
    gate_parser.add_argument("--classification-report", required=True, type=Path,
                             help="JSON output from msdr-rfml evaluate")
    detector_fetch = sub.add_parser("download-detector", help="fetch pinned experimental detector")
    detector_fetch.add_argument("--model-path", default="~/.cache/msdr/models/yolo-wideband.pt")
    detect = sub.add_parser("detect", help="offline experimental wideband IQ detection")
    detect.add_argument("--iq", required=True, type=Path)
    detect.add_argument("--sample-rate", required=True, type=float)
    detect.add_argument("--center-frequency", required=True, type=float)
    detect.add_argument("--model-path", default="~/.cache/msdr/models/yolo-wideband.pt")
    detect.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    detect.add_argument("--score-threshold", type=float, default=0.50)
    detect.add_argument("--target-bin-hz", type=float,
                        help="opt into adaptive tiled resolution, e.g. 1500 Hz/bin")
    detect.add_argument("--max-tiles", type=int, default=4)
    detect.add_argument(
        "--dsp-json",
        type=Path,
        help="optional JSON array of DSP Candidate fields on the same IQ time base",
    )
    args = parser.parse_args(argv)
    if args.command == "benchmark" and (
        args.iterations < 1
        or args.iterations > 1000
        or not np.isfinite(args.sample_rate)
        or args.sample_rate <= 0
        or not 0 <= args.score_threshold <= 1
    ):
        parser.error("iterations must be 1..1000, sample rate positive, score threshold 0..1")
    if args.command in {"evaluate", "detect"} and not 0 <= args.score_threshold <= 1:
        parser.error("score threshold must be 0..1")
    try:
        # Optional libraries may print setup notices; stdout remains machine-readable JSON.
        with contextlib.redirect_stdout(sys.stderr):
            if args.command == "download":
                result = download(Path(args.model_path))
            elif args.command == "download-detector":
                manifest = json.loads(Path(__file__).with_name("detector.json").read_text())
                result = download(Path(args.model_path), manifest)
            elif args.command == "detect":
                from msdr.rfml.detector import RFMLDetector
                from msdr.rfml.fusion import Candidate, fuse_candidates

                options = {}
                if args.target_bin_hz is not None:
                    options = {"target_bin_hz": args.target_bin_hz, "max_tiles": args.max_tiles}
                detector = RFMLDetector(
                    args.model_path, device=args.device, score_threshold=args.score_threshold,
                    **options,
                )
                iq = np.load(args.iq.expanduser(), allow_pickle=False, mmap_mode="r")
                started = time.perf_counter()
                result = detector.predict(
                    iq, sample_rate_hz=args.sample_rate, center_frequency_hz=args.center_frequency
                )
                result["elapsed_s_including_load"] = time.perf_counter() - started
                candidates = result.pop("candidates")
                dsp = []
                if args.dsp_json:
                    dsp = [Candidate(**row) for row in json.loads(args.dsp_json.read_text())]
                    if any(c.source != "dsp" for c in dsp):
                        raise ValueError("--dsp-json must contain only DSP candidates")
                result["candidates"] = [asdict(c) for c in candidates]
                result["fusion"] = [
                    asdict(c)
                    for c in fuse_candidates(
                        [*dsp, *candidates], ml_only_threshold=args.score_threshold
                    )
                ]
            elif args.command == "evaluate":
                from msdr.rfml.corpus import evaluate

                classifier = RFMLClassifier(args)
                classifier.load()
                result = evaluate(args.manifest.expanduser(), classifier)
            elif args.command == "baseline":
                from msdr.rfml.baseline import evaluate_baseline

                result = evaluate_baseline(args.manifest.expanduser())
            elif args.command == "release-gate":
                from msdr.rfml.baseline import evaluate_baseline, release_gate

                baseline = evaluate_baseline(args.manifest.expanduser())
                classification = json.loads(args.classification_report.expanduser().read_text())
                result = release_gate(baseline, classification)
            else:
                result = benchmark(args)
    except Exception as exc:
        parser.exit(1, f"RF ML unavailable: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.command == "release-gate" and not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
