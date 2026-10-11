"""Compare two detector adapters on identical, local IQ without fetching weights.

Example:
    .venv/bin/python scripts/rfml_detector_compare.py \
      --legacy-checkout /path/to/original/msdr \
      --iq-sc16 /path/to/preserved.sc16 \
      --model /path/to/yolo-wideband.pt \
      --output /path/to/detector-comparison.json

The script runs each checkout in a fresh Python process. Its output is a
behavior comparison, not a truth-scored detector evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve()
CURRENT_CHECKOUT = SCRIPT.parent.parent
OFFSETS_SECONDS = (5, 7, 9)
SAMPLE_RATE_HZ = 2_000_000
CENTER_FREQUENCY_HZ = 152_889_000
FOCUS_FREQUENCY_HZ = 152_879_000
WINDOW_SAMPLES = 65_536
SCORE_THRESHOLD = 0.5
TARGET_BIN_HZ = 1500
MAX_TILES = 4
NOISE_SEED = 28


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inputs(path: Path):
    raw = np.memmap(path, mode="r", dtype="<i2")
    if raw.size % 2:
        raise ValueError("SC16 input has an odd number of int16 values")
    raw = raw.reshape(-1, 2)
    for seconds in OFFSETS_SECONDS:
        offset = seconds * SAMPLE_RATE_HZ
        section = raw[offset:offset + WINDOW_SAMPLES]
        if len(section) != WINDOW_SAMPLES:
            raise ValueError(f"SC16 input lacks complete {seconds}s window")
        values = section.astype(np.float32)
        iq = ((values[:, 0] + 1j * values[:, 1]) / 32768).astype(np.complex64)
        yield f"capture_{seconds}s", iq
    rng = np.random.default_rng(NOISE_SEED)
    yield "synthetic_noise", ((rng.normal(size=WINDOW_SAMPLES) +
                               1j * rng.normal(size=WINDOW_SAMPLES)) * .01).astype(np.complex64)
    yield "synthetic_dc", np.full(WINDOW_SAMPLES, .01 + 0j, dtype=np.complex64)


def worker(args):
    from msdr.rfml.detector import RFMLDetector

    model = RFMLDetector(args.model, device="cpu", score_threshold=SCORE_THRESHOLD,
                         target_bin_hz=TARGET_BIN_HZ, max_tiles=MAX_TILES)
    windows = {}
    for name, iq in inputs(args.iq_sc16):
        result = model.predict(
            iq, sample_rate_hz=SAMPLE_RATE_HZ,
            center_frequency_hz=CENTER_FREQUENCY_HZ,
            focus_frequencies_hz=([FOCUS_FREQUENCY_HZ] if name.startswith("capture_") else None),
        )
        candidates = [asdict(candidate) for candidate in result["candidates"]]
        windows[name] = {
            "candidate_count": len(candidates),
            "focus_frequency_box_count": sum(
                item["low_hz"] <= FOCUS_FREQUENCY_HZ <= item["high_hz"]
                for item in candidates
            ),
            "candidates": candidates,
            "frequency_coverage": result["frequency_coverage"],
            "tiles": result["tiles"],
            "preprocessing_version": result["preprocessing_version"],
            "backend": result["backend"],
        }
    print(json.dumps(windows))


def run_checkout(checkout: Path, args) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(checkout)
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--worker", "--iq-sc16", str(args.iq_sc16),
         "--model", str(args.model)],
        cwd=tempfile.gettempdir(), env=env, check=True, text=True, capture_output=True,
    )
    return json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-checkout", type=Path)
    parser.add_argument("--iq-sc16", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.iq_sc16 = args.iq_sc16.expanduser().resolve()
    args.model = args.model.expanduser().resolve()
    if args.worker:
        worker(args)
        return
    if args.legacy_checkout is None or args.output is None:
        parser.error("--legacy-checkout and --output are required")
    legacy = args.legacy_checkout.resolve()
    current = CURRENT_CHECKOUT.resolve()
    if not (legacy / "msdr/rfml/detector.py").is_file():
        parser.error("--legacy-checkout must contain msdr/rfml/detector.py")
    for required in (args.iq_sc16, args.model):
        if not required.is_file():
            parser.error(f"missing input: {required}")
    report = {
        "purpose": "Adapter behavior comparison on identical IQ; no operator truth supplied",
        "settings": {
            "offsets_seconds": OFFSETS_SECONDS,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "center_frequency_hz": CENTER_FREQUENCY_HZ,
            "focus_frequency_hz": FOCUS_FREQUENCY_HZ,
            "window_samples": WINDOW_SAMPLES,
            "score_threshold": SCORE_THRESHOLD,
            "target_bin_hz": TARGET_BIN_HZ,
            "max_tiles": MAX_TILES,
            "device": "cpu",
            "noise_seed": NOISE_SEED,
            "controls": "complex Gaussian noise at 0.01 amplitude; constant complex 0.01 DC",
        },
        "inputs": {
            "iq_sc16": {"path": str(args.iq_sc16.resolve()), "sha256": sha256(args.iq_sc16)},
            "model": {"path": str(args.model.resolve()), "sha256": sha256(args.model)},
        },
        "checkouts": {
            "legacy": {"path": str(legacy),
                       "detector_py_sha256": sha256(legacy / "msdr/rfml/detector.py")},
            "corrected": {"path": str(current),
                          "detector_py_sha256": sha256(current / "msdr/rfml/detector.py")},
        },
        "results": {
            "legacy": run_checkout(legacy, args),
            "corrected": run_checkout(current, args),
        },
        "interpretation": (
            "Proposal count is not accuracy. Frequency-overlapping boxes at different "
            "times are separate model proposals, including repeated regions. Neither "
            "captured nor synthetic windows have full time/frequency annotations; "
            "unmatched boxes cannot be called false positives here."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({name: {kind: report["results"][kind][name]["candidate_count"]
                             for kind in ("legacy", "corrected")}
                      for name in report["results"]["legacy"]}, indent=2))


if __name__ == "__main__":
    main()
