"""Experimental, hash-pinned wideband RF object detector.

This is inference-only. The upstream TorchSig release publishes a YOLO11n
wideband checkpoint but does not fully document its training preprocessing;
our spectrogram image conversion is therefore explicitly experimental and
requires local validation before interpreting detections.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from msdr.rfml.fusion import Candidate
from msdr.rfml.spectrogram import SpectrogramMetadata

MANIFEST = json.loads(Path(__file__).with_name("detector.json").read_text())
MAX_IQ_SAMPLES = 1_048_576


def _device(torch: Any, requested: str) -> str:
    if requested not in {"auto", "cpu", "mps"}:
        raise ValueError("device must be auto, cpu, or mps")
    if requested == "cpu":
        return "cpu"
    try:
        available = bool(torch.backends.mps.is_available())
    except (AttributeError, RuntimeError):
        available = False
    return "mps" if available else "cpu"


def prepare_spectrogram_image(
    iq: np.ndarray,
    sample_rate_hz: float,
    center_frequency_hz: float,
    *,
    n_fft: int = 256,
    hop_length: int | None = None,
) -> tuple[np.ndarray, Any]:
    """Return a TorchSig v2.1.1-style frequency-row, time-column BGR image.

    The released checkpoint's exact training FFT settings remain unpublished.
    This follows ``compute_spectrogram`` and the YOLO image adapter, while the
    returned metadata keeps msdr's frequency-bin and time-frame coordinate
    contract for inverse box mapping.
    """
    data = np.asarray(iq)
    if data.ndim != 1 or not np.iscomplexobj(data) or not np.isfinite(data).all():
        raise ValueError("IQ must be a one-dimensional finite complex array")
    if isinstance(n_fft, bool) or not isinstance(n_fft, int) or n_fft < 2 or n_fft % 2:
        raise ValueError("n_fft must be an even integer of at least 2")
    if hop_length is None:
        hop_length = n_fft // 2
    if isinstance(hop_length, bool) or not isinstance(hop_length, int) or hop_length < 1:
        raise ValueError("hop_length must be a positive integer")
    if data.size < n_fft:
        raise ValueError("IQ is shorter than one complete FFT window")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and positive")
    if not math.isfinite(center_frequency_hz):
        raise ValueError("center_frequency_hz must be finite")

    starts = np.arange(0, data.size - n_fft + 1, hop_length)
    frames = np.asarray(data[starts[:, None] + np.arange(n_fft)], dtype=np.complex64)
    # torch.blackman_window uses the periodic form. torch.stft defaults to an
    # unnormalized FFT; Spectrogram(power=2) squares its magnitude.
    window = np.blackman(n_fft + 1)[:-1].astype(np.float32)
    spectrum = np.fft.fft(frames * window, axis=1)
    power = np.abs(spectrum).astype(np.float32) ** 2
    power = np.fft.fftshift(power, axes=1).T[::-1, :]
    epsilon = float(np.max(power)) * 1e-10
    if epsilon > 0:
        power[power == 0] = epsilon
        grid = (10 * np.log10(power)).astype(np.float32)
    else:
        # Upstream log10(0) is -inf for silent IQ; a constant finite image
        # preserves the detector's defined min/max conversion.
        grid = np.zeros(power.shape, dtype=np.float32)
    low = float(np.min(grid))
    high = float(np.max(grid))
    if high <= low:
        gray = np.zeros(grid.shape, dtype=np.uint8)
    else:
        gray = ((grid - low) * (255.0 / (high - low))).clip(0, 255).astype(np.uint8)
    black_hot = 255 - gray
    image = np.repeat(black_hot[:, :, None], 3, axis=2)
    metadata = SpectrogramMetadata(
        sample_rate_hz=float(sample_rate_hz),
        center_frequency_hz=float(center_frequency_hz),
        n_fft=n_fft,
        hop_length=hop_length,
        input_samples=data.size,
        frame_count=len(starts),
        frequency_bins=n_fft,
        dynamic_range_db=high - low,
    )
    return np.ascontiguousarray(image), metadata


class RFMLDetector:
    """Lazy Ultralytics adapter that emits raw ML ``Candidate`` detections.

    ``model_path`` must name the pinned checkpoint; arbitrary external weights
    are refused. The checkpoint is hashed before loading because Ultralytics
    loads a PyTorch pickle. Importing this module itself needs only NumPy.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        score_threshold: float = 0.25,
        n_fft: int = 256,
        hop_length: int | None = None,
        target_bin_hz: float | None = None,
        max_tiles: int = 4,
        tile_bins: int = 512,
    ):
        if device not in {"auto", "cpu", "mps"}:
            raise ValueError("device must be auto, cpu, or mps")
        if not math.isfinite(score_threshold) or not 0 <= score_threshold <= 1:
            raise ValueError("score_threshold must be between 0 and 1")
        if isinstance(n_fft, bool) or not isinstance(n_fft, int) or n_fft < 2 or n_fft % 2:
            raise ValueError("n_fft must be an even integer of at least 2")
        if hop_length is not None and (
            isinstance(hop_length, bool) or not isinstance(hop_length, int) or hop_length < 1
        ):
            raise ValueError("hop_length must be a positive integer")
        if target_bin_hz is not None and (not math.isfinite(target_bin_hz) or target_bin_hz <= 0):
            raise ValueError("target_bin_hz must be positive and finite")
        if not 1 <= max_tiles <= 32 or not 64 <= tile_bins <= 512:
            raise ValueError("max_tiles must be 1..32 and tile_bins 64..512")
        self.target_bin_hz = target_bin_hz
        self.max_tiles = max_tiles
        self.tile_bins = tile_bins
        self._tile_cursor = 0
        self.model_path = Path(model_path).expanduser()
        self.requested_device = device
        self.score_threshold = score_threshold
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.model = None
        self.backend = "unloaded"
        self.fallback_reason = None
        self.load_duration_ms = None

    def _verify_checkpoint(self) -> None:
        if not self.model_path.is_file():
            raise FileNotFoundError(
                "wideband RF checkpoint missing; download the pinned model first"
            )
        if self.model_path.stat().st_size != MANIFEST["size_bytes"]:
            raise ValueError("wideband checkpoint size mismatch; refusing unknown weights")
        with self.model_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != MANIFEST["sha256"]:
            raise ValueError("wideband checkpoint checksum mismatch; refusing unknown weights")

    def load(self) -> None:
        if self.model is not None:
            return
        self._verify_checkpoint()
        load_started = time.perf_counter()
        try:
            torch = importlib.import_module("torch")
            ultralytics = importlib.import_module("ultralytics")
        except ImportError as exc:
            raise RuntimeError(
                "wideband detection requires the optional torch and ultralytics packages"
            ) from exc
        YOLO = ultralytics.YOLO
        model = YOLO(str(self.model_path))
        self.backend = _device(torch, self.requested_device)
        self.model = model
        self.load_duration_ms = (time.perf_counter() - load_started) * 1000

    def _predict(self, image: np.ndarray):
        self.load()
        try:
            return self.model.predict(
                source=image,
                imgsz=512,
                conf=self.score_threshold,
                device=self.backend,
                verbose=False,
            )
        except (RuntimeError, NotImplementedError) as exc:
            if self.backend != "mps":
                raise
            self.backend = "cpu"
            self.fallback_reason = f"MPS inference failed; using CPU: {type(exc).__name__}: {exc}"
            # Ultralytics caches a predictor between calls; recreate it so the
            # retry cannot retain an MPS-selected backend.
            self.model.predictor = None
            return self.model.predict(
                source=image,
                imgsz=512,
                conf=self.score_threshold,
                device="cpu",
                verbose=False,
            )

    def predict(
        self, iq: np.ndarray, *, sample_rate_hz: float, center_frequency_hz: float,
        focus_frequencies_hz: list[float] | None = None,
    ) -> dict[str, Any]:
        """Detect spectrogram regions and map boxes into physical coordinates."""
        if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and positive")
        if not math.isfinite(center_frequency_hz):
            raise ValueError("center_frequency_hz must be finite")
        iq_array = np.asarray(iq)
        if iq_array.ndim != 1 or iq_array.size > MAX_IQ_SAMPLES:
            raise ValueError(
                f"IQ must be one-dimensional and no longer than {MAX_IQ_SAMPLES} samples"
            )
        n_fft = self.n_fft
        hop = self.hop_length
        if self.target_bin_hz is not None:
            # Bound memory to <=512 time frames and 8192 frequency bins.
            wanted = 2 ** math.ceil(math.log2(max(2, sample_rate_hz / self.target_bin_hz)))
            n_fft = min(8192, wanted)
            if iq_array.size < n_fft:
                raise ValueError("IQ window too short for requested frequency resolution")
            hop = max(1, math.ceil((iq_array.size - n_fft + 1) / 512))
        image, metadata = prepare_spectrogram_image(
            iq_array,
            sample_rate_hz,
            center_frequency_hz,
            n_fft=n_fft,
            hop_length=hop,
        )
        self.load()
        started = time.perf_counter()
        candidates: list[Candidate] = []
        tile_width = min(self.tile_bins, metadata.frequency_bins)
        starts = self._tile_starts(metadata, focus_frequencies_hz or [])
        for tile_start in starts:
            # TorchSig rows descend from high to low frequency, while tile
            # starts and shared metadata count bins from low to high.
            image_row_start = metadata.frequency_bins - tile_start - tile_width
            tile = np.ascontiguousarray(image[image_row_start:image_row_start + tile_width, :])
            results = self._predict(tile)
            for result in results or ():
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue
                xyxy = boxes.xyxy.detach().cpu().numpy()
                scores = boxes.conf.detach().cpu().numpy()
                classes = boxes.cls.detach().cpu().numpy().astype(int)
                names = getattr(result, "names", getattr(self.model, "names", {}))
                for coords, score, class_id in zip(xyxy, scores, classes, strict=True):
                    x0, y0, x1, y1 = (float(value) for value in coords)
                    if not np.isfinite([x0, y0, x1, y1, score]).all():
                        continue
                    # Ultralytics returns pixel coordinates in the original image.
                    x0 = max(0, min(metadata.frame_count, int(math.floor(x0))))
                    x1 = max(0, min(metadata.frame_count, int(math.ceil(x1))))
                    y0 = max(0, min(tile_width, int(math.floor(y0))))
                    y1 = max(0, min(tile_width, int(math.ceil(y1))))
                    if x0 >= x1 or y0 >= y1:
                        continue
                    frequency_start_bin = metadata.frequency_bins - image_row_start - y1
                    frequency_end_bin = metadata.frequency_bins - image_row_start - y0
                    box = metadata.box_to_coordinates(
                        (frequency_start_bin, x0, frequency_end_bin, x1)
                    )
                    # Candidate requires positive absolute Hz bounds. An RF capture
                    # centered at/near DC cannot represent negative frequency there.
                    if box.frequency_start_hz <= 0:
                        continue
                    label = (
                        names.get(int(class_id), str(class_id))
                        if isinstance(names, dict)
                        else str(class_id)
                    )
                    candidates.append(
                        Candidate(
                            source="ml",
                            low_hz=box.frequency_start_hz,
                            high_hz=box.frequency_end_hz,
                            start_s=box.time_start_s,
                            end_s=box.time_end_s,
                            score=float(score),
                            evidence={
                                "class_id": int(class_id),
                                "label": str(label),
                                "box_px": [x0, y0 + image_row_start, x1, y1 + image_row_start],
                                "tile_start_bin": tile_start,
                            },
                        )
                    )
        # Count every model box, while presenting one representative and a
        # duplicate count for clearly repeated views of the same region.
        raw_candidate_count = len(candidates)
        kept = _aggregate_duplicate_candidates(candidates)
        covered = set()
        for tile_start in starts:
            covered.update(range(tile_start, tile_start + tile_width))
        return {
            "status": "ok",
            "model_id": MANIFEST["id"],
            "model_version": MANIFEST["sha256"],
            "preprocessing_version": ("experimental-torchsig21-adaptive-tiled-v3"
                                      if self.target_bin_hz is not None
                                      else MANIFEST["preprocessing_version"]),
            "preprocessing_status": "experimental_unvalidated",
            "field_validated": False,
            "backend": self.backend,
            "fallback_reason": self.fallback_reason,
            "sample_rate_hz": float(sample_rate_hz),
            "center_frequency_hz": float(center_frequency_hz),
            "spectrogram": asdict(metadata),
            "score_threshold": self.score_threshold,
            "score_semantics": "uncalibrated detector confidence; not a probability",
            "model_load_ms": self.load_duration_ms,
            "inference_ms": (time.perf_counter() - started) * 1000,
            "candidates": kept,
            "raw_candidate_count": raw_candidate_count,
            "bin_width_hz": metadata.bin_width_hz,
            "frequency_coverage": len(covered) / metadata.frequency_bins,
            "tiles": [{"start_bin": start, "bins": tile_width} for start in starts],
            "target_bin_hz": self.target_bin_hz,
            "resolution_target_met": (self.target_bin_hz is None or
                                      metadata.bin_width_hz <= self.target_bin_hz),
        }


    def _tile_starts(self, metadata, focus: list[float]) -> list[int]:
        width = metadata.frequency_bins
        size = min(self.tile_bins, width)
        if width <= size:
            return [0]
        stride = max(1, size * 3 // 4)
        starts = list(range(0, width - size + 1, stride))
        if starts[-1] != width - size:
            starts.append(width - size)
        # Choose the blind-search tile before focused tiles. Its position must
        # advance on every inference, even when a focus frequency lands on it.
        blind_start = starts[self._tile_cursor % len(starts)]
        self._tile_cursor = (self._tile_cursor + 1) % len(starts)
        selected = [blind_start]
        low = metadata.center_frequency_hz - metadata.sample_rate_hz / 2
        for hz in focus:
            if len(selected) >= self.max_tiles:
                break
            if not math.isfinite(hz) or not low <= hz < low + metadata.sample_rate_hz:
                continue
            x = (hz - low) / metadata.bin_width_hz
            nearest = min(starts, key=lambda start: abs(start + size / 2 - x))
            if nearest not in selected and len(selected) < self.max_tiles:
                selected.append(nearest)
        for offset in range(len(starts)):
            if len(selected) >= self.max_tiles:
                break
            start = starts[(self._tile_cursor + offset) % len(starts)]
            if start not in selected:
                selected.append(start)
        return selected



def _aggregate_duplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse only near-identical, same-class detections with shared time.

    The 0.8 overlap thresholds are measured against the smaller box on each
    axis, allowing modest edge jitter from adjacent spectrogram tiles. Boxes
    from separate bursts never join without positive time overlap; nearby
    channels never join without near-complete frequency overlap.
    """
    kept: list[Candidate] = []
    counts: list[int] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        match = next((i for i, other in enumerate(kept)
                      if _is_duplicate_box(candidate, other)), None)
        if match is None:
            kept.append(candidate)
            counts.append(1)
            continue
        counts[match] += 1
        evidence = dict(kept[match].evidence)
        evidence["duplicate_count"] = counts[match]
        kept[match] = Candidate(
            source=kept[match].source,
            low_hz=kept[match].low_hz,
            high_hz=kept[match].high_hz,
            start_s=kept[match].start_s,
            end_s=kept[match].end_s,
            score=kept[match].score,
            evidence=evidence,
        )
    return kept


def _is_duplicate_box(a: Candidate, b: Candidate) -> bool:
    if a.source != b.source or a.evidence.get("class_id") != b.evidence.get("class_id"):
        return False
    freq_intersection = max(0.0, min(a.high_hz, b.high_hz) - max(a.low_hz, b.low_hz))
    time_intersection = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    freq_smaller = min(a.high_hz - a.low_hz, b.high_hz - b.low_hz)
    freq_larger = max(a.high_hz - a.low_hz, b.high_hz - b.low_hz)
    time_smaller = min(a.end_s - a.start_s, b.end_s - b.start_s)
    return (freq_smaller > 0 and time_smaller > 0
            and freq_smaller / freq_larger >= 0.5
            and freq_intersection / freq_smaller >= 0.8
            and time_intersection / time_smaller >= 0.8)
