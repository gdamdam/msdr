"""Hash-pinned RF inference adapter. Torch is imported only when explicitly loaded."""

from __future__ import annotations

import hashlib
import importlib
import json
import time
from pathlib import Path

import numpy as np

MANIFEST = json.loads(Path(__file__).with_name("model.json").read_text())
SAMPLES = MANIFEST["input_samples"]
MAX_WINDOWS = 3
PREPROCESSING_VERSION = f'{MANIFEST["preprocessing_version"]}+contiguous-consensus-v2'


def select_device(torch, requested: str) -> str:
    if requested not in {"auto", "cpu", "mps"}:
        raise ValueError("device must be auto, cpu or mps")
    if requested != "cpu" and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def prepare_iq(iq: np.ndarray) -> np.ndarray:
    """Contiguous complex IQ -> (1,2,4096), normalized by complex L-infinity."""
    data = np.asarray(iq)
    if data.ndim != 1 or not np.iscomplexobj(data) or data.size < SAMPLES:
        raise ValueError("need at least 4096 contiguous complex IQ samples")
    data = data[:SAMPLES].astype(np.complex64, copy=True)
    if not np.isfinite(data).all():
        raise ValueError("non-finite IQ samples")
    peak = float(np.max(np.abs(data)))
    if peak <= 1e-12:
        raise ValueError("silent IQ window")
    data /= peak
    return np.stack((data.real, data.imag), axis=0)[None].astype(np.float32)


def family(label: str) -> str:
    if "fsk" in label or "msk" in label:
        return "FSK"
    if "psk" in label:
        return "PSK"
    if "qam" in label:
        return "QAM"
    if label.startswith("ofdm"):
        return "OFDM"
    if label.startswith("am-"):
        return "AM"
    if label == "fm":
        return "FM"
    if label.startswith("lfm") or label == "chirpss":
        return "CHIRP"
    if label == "ook" or "ask" in label or "pam" in label:
        return "ASK/PAM"
    return "UNKNOWN"


def summarize_logits(logits: np.ndarray, threshold: float) -> dict:
    values = np.asarray(logits, dtype=np.float64).reshape(-1)
    if len(values) != len(MANIFEST["classes"]) or not np.isfinite(values).all():
        raise ValueError("model returned invalid logits")
    scores = np.exp(values - values.max())
    scores /= scores.sum()
    order = np.argsort(-scores, kind="stable")[:3]
    candidates = [{"label": MANIFEST["classes"][i], "score": float(scores[i])} for i in order]
    top = candidates[0]
    accepted = top["score"] >= threshold
    return {
        "status": "ok" if accepted else "abstained",
        "classification": family(top["label"]) if accepted else "UNKNOWN",
        "score": top["score"],
        "candidates": candidates,
        "reason": "uncalibrated model preference" if accepted else "below score threshold",
    }


class RFMLClassifier:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.torch = None
        self.backend = "cpu"
        self.fallback_reason = None

    def load(self) -> None:
        if self.model is not None:
            return
        path = Path(self.settings.model_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError("RF model missing; run msdr-rfml download first")
        if path.stat().st_size != MANIFEST["size_bytes"]:
            raise ValueError("RF checkpoint size does not match the supported model")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != MANIFEST["sha256"]:
            raise ValueError("RF checkpoint checksum mismatch; refusing unknown weights")
        torch = importlib.import_module("torch")
        from msdr.rfml.xcit import XCiT1d

        self.torch = torch
        torch.set_num_threads(2)
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        expected = {
            "input_channels": 2,
            "num_classes": 61,
            "xcit_version": "tiny_12_p16_224",
            "ds_method": "downsample",
            "ds_rate": 16,
        }
        if any(checkpoint.get("hyper_parameters", {}).get(k) != v for k, v in expected.items()):
            raise ValueError("RF model hyperparameters do not match the pinned architecture")
        model = XCiT1d(2, 61, xcit_version="tiny_12_p16_224", ds_rate=16)
        state = {k.removeprefix("model."): v for k, v in checkpoint["state_dict"].items()}
        model.load_state_dict(state, strict=True)
        del state, checkpoint
        self.model = model.eval()
        self.backend = select_device(torch, self.settings.device)
        if self.settings.device == "mps" and self.backend == "cpu":
            self.fallback_reason = "MPS unavailable; using CPU"
        try:
            self.model.to(self.backend)
            with torch.inference_mode():
                self.model(torch.zeros(1, 2, SAMPLES, device=self.backend))
                if self.backend == "mps":
                    torch.mps.synchronize()
        except (RuntimeError, NotImplementedError) as exc:
            if self.backend != "mps":
                self.model = None
                raise
            self._fallback(exc)
            with torch.inference_mode():
                self.model(torch.zeros(1, 2, SAMPLES))

    def _fallback(self, exc: Exception) -> None:
        self.backend = "cpu"
        self.fallback_reason = f"MPS failed; using CPU: {type(exc).__name__}: {exc}"
        self.model.to("cpu")

    def predict(self, iq: np.ndarray, sample_rate_hz: float) -> dict:
        base = {
            "model_id": MANIFEST["id"],
            "model_version": MANIFEST["sha256"],
            "preprocessing_version": PREPROCESSING_VERSION,
            "calibration": "uncalibrated",
            "shadow": True,
            "sample_rate_hz": float(sample_rate_hz),
            "analyzed_at": time.time(),
            "backend": self.backend,
        }
        if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
            raise ValueError("invalid IQ sample rate")
        data = np.asarray(iq)
        if data.ndim != 1 or not np.iscomplexobj(data) or data.size < SAMPLES:
            return {
                **base,
                "status": "abstained",
                "classification": "UNKNOWN",
                "score": None,
                "candidates": [],
                "windows": [],
                "reason": "need at least 4096 contiguous complex IQ samples",
            }
        if self.model is None:
            raise RuntimeError("RF model has not been loaded")
        torch = self.torch
        started = time.perf_counter()
        windows = []
        count = min(MAX_WINDOWS, data.size // SAMPLES)
        for index in range(count):
            try:
                prepared = prepare_iq(data[index * SAMPLES : (index + 1) * SAMPLES])
            except ValueError as exc:
                windows.append({"offset_samples": index * SAMPLES, "status": "abstained",
                                "classification": "UNKNOWN", "score": None,
                                "candidates": [], "reason": str(exc)})
                return {
                    **base,
                    "status": "abstained",
                    "classification": "UNKNOWN",
                    "score": None,
                    "candidates": [],
                    "windows": windows,
                    "window_count": count,
                    "reason": f"invalid IQ window: {exc}",
                    "backend": self.backend,
                    "fallback_reason": self.fallback_reason,
                    "inference_ms": (time.perf_counter() - started) * 1000,
                    "window_s": count * SAMPLES / sample_rate_hz,
                }
            with torch.inference_mode():
                try:
                    logits = self.model(torch.from_numpy(prepared).to(self.backend)).cpu().numpy()
                except (RuntimeError, NotImplementedError) as exc:
                    if self.backend != "mps":
                        raise
                    self._fallback(exc)
                    logits = self.model(torch.from_numpy(prepared)).cpu().numpy()
            windows.append({"offset_samples": index * SAMPLES,
                            **summarize_logits(logits, self.settings.score_threshold)})
        weakest = min(windows, key=lambda window: window["score"])
        families = {window["classification"] for window in windows}
        accepted = (
            all(window["status"] == "ok" for window in windows)
            and len(families) == 1
            and "UNKNOWN" not in families
        )
        if accepted:
            reason = "uncalibrated model preference; unanimous window family"
        elif any(window["status"] != "ok" for window in windows):
            reason = "below score threshold in one or more windows"
        elif "UNKNOWN" in families:
            reason = "unsupported modulation family"
        else:
            reason = "window family disagreement"
        return {
            **base,
            "status": "ok" if accepted else "abstained",
            "classification": weakest["classification"] if accepted else "UNKNOWN",
            "score": weakest["score"],
            "candidates": weakest["candidates"],
            "reason": reason,
            "windows": windows,
            "window_count": count,
            "backend": self.backend,
            "fallback_reason": self.fallback_reason,
            "inference_ms": (time.perf_counter() - started) * 1000,
            "window_s": count * SAMPLES / sample_rate_hz,
        }
