#!/usr/bin/env python3
"""Reproduce a CPU comparison of the current and newer TorchSig XCiT models.

The script uses exact XCiT class nodes from local upstream source snapshots,
loads both Lightning checkpoints with ``weights_only=True`` and strict state
dict checks, and writes logits plus provenance to JSON. It does not estimate
accuracy: the three radio windows come from one known recording, and the
noise/DC/tone inputs are synthetic controls.

Default source files live beside this checkout at ``../ml-compare``. Override
paths with CLI flags when reproducing from another checkout.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CURRENT_CHECKPOINT = Path("~/.cache/msdr/models/xcit_narrowband.ckpt").expanduser()
CURRENT_MANIFEST = ROOT / "msdr/rfml/model.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_class_names(path: Path, variable: str = "CLASS_FAMILY_DICT") -> list[str]:
    """Read class ordering from a source-level dict without importing TorchSig."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != variable or not isinstance(node.value, ast.Dict):
            continue
        names = [ast.literal_eval(key) for key in node.value.keys]
        if any(not isinstance(name, str) for name in names):
            raise ValueError(f"{path}: class keys must be strings")
        return names
    raise ValueError(f"{path}: no annotated {variable} dictionary found")


def verify_new_label_order(labels_path: Path, inference_path: Path) -> None:
    """Check the source chain that maps output indices to label names."""
    labels_tree = ast.parse(labels_path.read_text(encoding="utf-8"), filename=str(labels_path))
    has_signal_list = False
    has_all_signals = False
    for node in labels_tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "SIGNALS_SHARED_LIST":
                value = node.value
                has_signal_list = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "list"
                    and len(value.args) == 1
                    and isinstance(value.args[0], ast.Call)
                    and isinstance(value.args[0].func, ast.Attribute)
                    and value.args[0].func.attr == "keys"
                    and isinstance(value.args[0].func.value, ast.Name)
                    and value.args[0].func.value.id == "CLASS_FAMILY_DICT"
                )
        if isinstance(node, ast.ClassDef) and node.name == "TorchSigSignalLists":
            has_all_signals = any(
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id == "all_signals"
                and isinstance(item.value, ast.Name)
                and item.value.id == "SIGNALS_SHARED_LIST"
                for item in node.body
            )
    pipeline_tree = ast.parse(
        inference_path.read_text(encoding="utf-8"), filename=str(inference_path)
    )
    pipeline_uses_labels = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "class_list" for target in node.targets
        )
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "all_signals"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "TorchSigSignalLists"
        for node in ast.walk(pipeline_tree)
    )
    if not (has_signal_list and has_all_signals and pipeline_uses_labels):
        raise ValueError(
            "could not verify v1.0 class order through its local labels and inference sources"
        )


def upstream_xcit_class(path: Path) -> Callable:
    """Compile unchanged upstream architecture class nodes, omitting trainers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"XCiT1d", "ConvDownSampler", "Chunker"}
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    found = {node.name for node in nodes}
    if found != wanted:
        raise ValueError(f"{path}: expected {sorted(wanted)}, found {sorted(found)}")

    import timm
    import torch
    from torch import Tensor, nn

    namespace = {"timm": timm, "torch": torch, "Tensor": Tensor, "nn": nn}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["XCiT1d"]


def extract_windows(iq: np.ndarray, sample_rate_hz: float, offsets_s: list[float]) -> list[dict]:
    data = np.asarray(iq)
    if data.ndim != 1 or not np.iscomplexobj(data) or not np.isfinite(data).all():
        raise ValueError("recording must be finite, one-dimensional complex IQ")
    result = []
    for offset_s in offsets_s:
        start = int(round(offset_s * sample_rate_hz))
        end = start + 4096
        if start < 0 or end > data.size:
            raise ValueError(
                f"offset {offset_s:g}s needs samples [{start}:{end}], file has {data.size}"
            )
        window = np.asarray(data[start:end], dtype=np.complex64)
        rms = float(np.sqrt(np.mean(np.abs(window.astype(np.complex128)) ** 2)))
        peak = float(np.max(np.abs(window)))
        result.append(
            {
                "offset_seconds": offset_s,
                "offset_samples": start,
                "duration_seconds": len(window) / sample_rate_hz,
                "iq": window,
                "rms": rms,
                "peak": peak,
            }
        )
    return result


def logits_for(model, torch, value: np.ndarray, *, normalize_linf: bool) -> np.ndarray:
    iq = np.asarray(value, dtype=np.complex64)
    if iq.shape != (4096,) or not np.isfinite(iq).all():
        raise ValueError("model input window must contain 4096 finite complex samples")
    if normalize_linf:
        peak = float(np.max(np.abs(iq)))
        if peak <= 1e-12:
            raise ValueError("current model cannot classify a silent window")
        iq = iq / peak
    tensor = torch.from_numpy(np.stack((iq.real, iq.imag)).astype(np.float32)[None])
    with torch.inference_mode():
        result = model(tensor).detach().cpu().numpy().reshape(-1)
    if not np.isfinite(result).all():
        raise ValueError("model produced non-finite logits")
    return result.astype(np.float64)


def load_checkpoint_model(torch, model_class, checkpoint_path: Path, expected: dict) -> tuple:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    hyper = checkpoint.get("hyper_parameters", {})
    for name, value in expected.items():
        if hyper.get(name) != value:
            raise ValueError(
                f"{checkpoint_path}: hyperparameter {name!r} is {hyper.get(name)!r}; "
                f"expected {value!r}"
            )
    state = checkpoint.get("state_dict")
    if not isinstance(state, dict) or not state:
        raise ValueError(f"{checkpoint_path}: missing state_dict")
    model_state = {key.removeprefix("model."): value for key, value in state.items()}
    model = model_class(
        input_channels=2,
        n_features=expected["num_classes"],
        xcit_version=expected["xcit_version"],
        ds_method=expected["ds_method"],
        ds_rate=expected["ds_rate"],
    )
    model.load_state_dict(model_state, strict=True)
    model.eval()
    return model, hyper


def top_candidates(logits: np.ndarray, labels: list[str], count: int = 3) -> list[dict]:
    if len(logits) != len(labels):
        raise ValueError(f"logit count {len(logits)} does not match {len(labels)} labels")
    shifted = logits - float(np.max(logits))
    probabilities = np.exp(shifted)
    probabilities /= probabilities.sum()
    indices = np.argsort(-probabilities, kind="stable")[:count]
    return [
        {
            "rank": rank,
            "label": labels[index],
            "raw_softmax_score": float(probabilities[index]),
            "logit": float(logits[index]),
        }
        for rank, index in enumerate(indices, 1)
    ]


def synthetic_controls(rng: np.random.Generator, count: int, rms: float) -> list[dict]:
    noise = (rng.normal(size=count) + 1j * rng.normal(size=count)).astype(np.complex64)
    noise *= np.float32(rms / np.sqrt(np.mean(np.abs(noise) ** 2)))
    index = np.arange(count, dtype=np.float64)
    tone = (rms * np.exp(2j * np.pi * 5_000 * index / 50_000)).astype(np.complex64)
    dc = np.full(count, rms + 0j, dtype=np.complex64)
    return [
        {"name": "complex_gaussian_noise", "iq": noise},
        {"name": "dc_offset", "iq": dc},
        {"name": "single_5khz_tone", "iq": tone},
    ]


def build_report(args) -> dict:
    import torch

    from msdr.rfml.classifier import prepare_iq
    from msdr.rfml.xcit import XCiT1d as CurrentXCiT1d

    torch.set_num_threads(2)
    source_dir = args.source_dir.resolve()
    files = {
        "upstream_xcit_v061": source_dir / "upstream_xcit.py",
        "new_xcit": source_dir / "new_xcit.py",
        "new_labels": source_dir / "new_labels.py",
        "new_pipeline": source_dir / "new_pipeline.py",
        "new_example_config": source_dir / "new_config.yaml",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing {name} source file: {path}")

    manifest = json.loads(CURRENT_MANIFEST.read_text(encoding="utf-8"))
    current_classes = manifest["classes"]
    new_classes = read_class_names(files["new_labels"])
    verify_new_label_order(files["new_labels"], files["new_pipeline"])
    old_source_class = upstream_xcit_class(files["upstream_xcit_v061"])
    new_source_class = upstream_xcit_class(files["new_xcit"])

    old_checkpoint = args.current_checkpoint.expanduser().resolve()
    new_checkpoint = args.new_checkpoint.expanduser().resolve()
    if not old_checkpoint.is_file() or not new_checkpoint.is_file():
        raise FileNotFoundError("both current and new checkpoints must exist")
    old_hash = sha256_file(old_checkpoint)
    new_hash = sha256_file(new_checkpoint)
    if old_hash != manifest["sha256"]:
        raise ValueError("current checkpoint does not match msdr/rfml/model.json SHA-256")
    if new_hash != args.expected_new_sha256:
        raise ValueError("new checkpoint SHA-256 does not match the requested pinned artifact")

    expected_old = {
        "input_channels": 2,
        "num_classes": len(current_classes),
        "xcit_version": "tiny_12_p16_224",
        "ds_method": "downsample",
        "ds_rate": 16,
    }
    expected_new = {
        "input_channels": 2,
        "num_classes": len(new_classes),
        "xcit_version": "tiny_12_p16_224",
        "ds_method": "downsample",
        "ds_rate": 16,
    }
    current_model, old_hyper = load_checkpoint_model(
        torch, CurrentXCiT1d, old_checkpoint, expected_old
    )
    upstream_model, _ = load_checkpoint_model(torch, old_source_class, old_checkpoint, expected_old)
    new_model, new_hyper = load_checkpoint_model(
        torch, new_source_class, new_checkpoint, expected_new
    )

    iq = np.load(args.iq.resolve(), allow_pickle=False, mmap_mode="r")
    windows = extract_windows(iq, args.sample_rate_hz, args.offset_seconds)
    session_rows = []
    max_old_diff = 0.0
    parity_ok = True
    for row in windows:
        # This is msdr's exact 4096-sample preprocessing, shared by current
        # and upstream v0.6.1 implementations to isolate code/model parity.
        prepared = prepare_iq(row["iq"])[0]
        old_tensor = torch.from_numpy(prepared[None].astype(np.float32))
        with torch.inference_mode():
            current_logits = (
                current_model(old_tensor).detach().cpu().numpy().reshape(-1).astype(np.float64)
            )
            upstream_logits = (
                upstream_model(old_tensor).detach().cpu().numpy().reshape(-1).astype(np.float64)
            )
        diff = float(np.max(np.abs(current_logits - upstream_logits)))
        same_logits = bool(np.allclose(current_logits, upstream_logits, rtol=1e-5, atol=1e-6))
        max_old_diff = max(max_old_diff, diff)
        parity_ok &= same_logits

        # The v1.0.0 inference source applies ComplexTo2D only. It does not
        # apply the old L-infinity normalization or random-phase training
        # augmentation. Preserve the recording's raw complex amplitude.
        new_logits = logits_for(new_model, torch, row["iq"], normalize_linf=False)
        normalized_new_logits = logits_for(new_model, torch, row["iq"], normalize_linf=True)
        session_rows.append(
            {
                "offset_seconds": row["offset_seconds"],
                "offset_samples": row["offset_samples"],
                "duration_seconds": row["duration_seconds"],
                "input_rms": row["rms"],
                "input_peak": row["peak"],
                "current_v061": {
                    "top3": top_candidates(current_logits, current_classes),
                },
                "upstream_v061": {
                    "top3": top_candidates(upstream_logits, current_classes),
                },
                "new_v100": {
                    "top3": top_candidates(new_logits, new_classes),
                    "preprocessing": "upstream ComplexTo2D only; raw amplitude",
                },
                "new_v100_linf_ablation": {
                    "top3": top_candidates(normalized_new_logits, new_classes),
                    "preprocessing": "ComplexTo2D after per-window L-infinity normalization",
                },
                "current_vs_upstream": {
                    "max_absolute_logit_delta": diff,
                    "allclose_rtol_1e-5_atol_1e-6": same_logits,
                },
            }
        )

    real_rms = float(np.mean([row["rms"] for row in windows]))
    controls = []
    for control in synthetic_controls(np.random.default_rng(args.synthetic_seed), 4096, real_rms):
        current_logits = logits_for(current_model, torch, control["iq"], normalize_linf=True)
        upstream_logits = logits_for(upstream_model, torch, control["iq"], normalize_linf=True)
        new_logits = logits_for(new_model, torch, control["iq"], normalize_linf=False)
        normalized_new_logits = logits_for(new_model, torch, control["iq"], normalize_linf=True)
        diff = float(np.max(np.abs(current_logits - upstream_logits)))
        controls.append(
            {
                "name": control["name"],
                "synthetic": True,
                "input_rms": float(np.sqrt(np.mean(np.abs(control["iq"]) ** 2))),
                "current_v061": {
                    "top3": top_candidates(current_logits, current_classes),
                },
                "upstream_v061": {
                    "top3": top_candidates(upstream_logits, current_classes),
                },
                "new_v100": {
                    "top3": top_candidates(new_logits, new_classes),
                    "preprocessing": "upstream ComplexTo2D only; raw amplitude",
                },
                "new_v100_linf_ablation": {
                    "top3": top_candidates(normalized_new_logits, new_classes),
                    "preprocessing": "per-window L-infinity normalization",
                },
                "current_vs_upstream_max_absolute_logit_delta": diff,
            }
        )

    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "purpose": (
            "Offline checkpoint/source parity and compatibility probe; not an accuracy evaluation."
        ),
        "models": {
            "current_and_upstream": {
                "current_id": manifest["id"],
                "checkpoint_sha256": old_hash,
                "checkpoint_bytes": old_checkpoint.stat().st_size,
                "classes": current_classes,
                "class_count": len(current_classes),
                "checkpoint_hyperparameters": old_hyper,
                "input_preprocessing": (
                    "msdr IQ4096 L-infinity normalize then ComplexTo2D; "
                    "same tensor for both implementations"
                ),
                "upstream_class_source": str(files["upstream_xcit_v061"]),
                "upstream_class_source_sha256": sha256_file(files["upstream_xcit_v061"]),
            },
            "new": {
                "id": "torchsig-models-v1.0.0-xcit",
                "checkpoint_sha256": new_hash,
                "checkpoint_bytes": new_checkpoint.stat().st_size,
                "classes": new_classes,
                "class_count": len(new_classes),
                "class_order_status": "inferred_from_local_sources_not_checkpoint_metadata",
                "class_order_note": (
                    "Order follows the local new_labels.py dict keys through "
                    "SIGNALS_SHARED_LIST and new_pipeline.py's TorchSigSignalLists.all_signals. "
                    "The checkpoint does not include class-name metadata."
                ),
                "checkpoint_hyperparameters": new_hyper,
                "input_preprocessing": (
                    "ComplexTo2D only; raw complex amplitude retained; "
                    "no old-model L-infinity normalization"
                ),
                "class_source": str(files["new_labels"]),
                "class_source_sha256": sha256_file(files["new_labels"]),
                "inference_source": str(files["new_pipeline"]),
                "inference_source_sha256": sha256_file(files["new_pipeline"]),
                "example_dataset_config": str(files["new_example_config"]),
                "example_dataset_config_sha256": sha256_file(files["new_example_config"]),
                "example_dataset_config_relevant": {
                    "schema_version": "2.1.1",
                    "sample_rate_hz": 10_000_000,
                    "num_iq_samples": 4096,
                    "bandwidth_hz": [2_500_000, 3_333_333],
                    "bandwidth_fraction_of_sample_rate": [0.25, 0.3333333],
                    "impairment_level": 0,
                },
                "training_distribution_note": (
                    "The local YAML is tests/test_narrowband_config.yaml, an example/test "
                    "dataset config, not verified checkpoint training metadata. The "
                    "released checkpoint's actual training distribution is unknown."
                ),
            },
        },
        "input": {
            "path": str(args.iq.resolve()),
            "sha256": sha256_file(args.iq.resolve()),
            "dtype": str(iq.dtype),
            "samples": int(iq.size),
            "duration_seconds": float(iq.size / args.sample_rate_hz),
            "sample_rate_hz": args.sample_rate_hz,
            "known_signal": (
                "DMR, independently decoded with DSD; one channel recording, one session."
            ),
            "new_model_domain_context": {
                "recording_sample_rate_hz": args.sample_rate_hz,
                "example_config_sample_rate_hz": 10_000_000,
                "recording_sample_rate_is_example_config_rate": args.sample_rate_hz == 10_000_000,
                "note": (
                    "Absolute sample rate alone does not establish incompatibility. "
                    "The example config has 0.25-0.333 fractional bandwidth; compare "
                    "normalized bandwidth and samples per symbol against the actual "
                    "training distribution, which is not available. Any mismatch claim "
                    "based on this example config is an inference, not checkpoint metadata."
                ),
            },
        },
        "real_recording_windows": session_rows,
        "current_vs_upstream_parity": {
            "same_prepared_iq_tensors": True,
            "allclose_rtol_1e-5_atol_1e-6": parity_ok,
            "max_absolute_logit_delta": max_old_diff,
        },
        "synthetic_controls": {
            "seed": args.synthetic_seed,
            "amplitude_rms": real_rms,
            "purpose": "Exercise model response to simple controls only; not labeled RF examples.",
            "accuracy_estimate": None,
            "controls": controls,
        },
        "evaluation_limits": [
            ("Three windows come from one 10.1-second capture; they are not independent sessions."),
            "Synthetic noise, DC, and tone outputs are controls, not accuracy samples.",
            (
                "No independent accuracy, calibration, generalization, or "
                "false-positive estimate is produced."
            ),
            "The supplied v1.0.0 YAML is an example/test config, not training metadata.",
            "A rate or domain mismatch with checkpoint training data is not established.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iq", type=Path, required=True, help="Complex IQ recording (.npy).")
    parser.add_argument("--sample-rate-hz", type=float, required=True)
    parser.add_argument("--offset-seconds", type=float, nargs="+", default=[5.0, 7.0, 9.0])
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Directory containing the upstream source snapshots.",
    )
    parser.add_argument("--current-checkpoint", type=Path, default=DEFAULT_CURRENT_CHECKPOINT)
    parser.add_argument(
        "--new-checkpoint", type=Path, help="Defaults to xcit-v1.ckpt inside --source-dir."
    )
    parser.add_argument(
        "--expected-new-sha256",
        default="e9b9e7b9324602a78babcbbe01e1b6d8cdb995a0a5e55cb3b7d6c23556d06fec",
    )
    parser.add_argument("--synthetic-seed", type=int, default=20260929)
    parser.add_argument("--output", type=Path, required=True, help="Path for the JSON result.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.source_dir = args.source_dir.expanduser().resolve()
    if args.new_checkpoint is None:
        args.new_checkpoint = args.source_dir / "xcit-v1.ckpt"
    report = build_report(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "current_vs_upstream_allclose": report["current_vs_upstream_parity"][
                    "allclose_rtol_1e-5_atol_1e-6"
                ],
                "current_vs_upstream_max_delta": report["current_vs_upstream_parity"][
                    "max_absolute_logit_delta"
                ],
                "window_offsets_s": [
                    row["offset_seconds"] for row in report["real_recording_windows"]
                ],
                "independent_accuracy_estimate": report["synthetic_controls"]["accuracy_estimate"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
