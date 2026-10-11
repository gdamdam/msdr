"""Explicit operator captures, independent of ML predictions and dependencies."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import numpy as np

_LOCK = threading.Lock()
_MAX_BYTES = 512 * 1024 * 1024


def save_capture(directory: Path, iq, rate, metadata, label, notes) -> dict:
    data = np.asarray(iq)
    if (data.ndim != 1 or not np.iscomplexobj(data) or not 4096 <= data.size <= 64_000_000
            or not np.isfinite(data).all() or not np.isfinite(rate) or rate <= 0):
        raise ValueError("Capture needs 4096–64000000 finite complex samples and a positive rate")
    if label not in {"unlabeled", "voice", "digital", "noise", "interference", "uncertain"}:
        raise ValueError("Unsupported operator label")
    from msdr.rfml.spectrogram import iq_spectrogram

    # Sparse overview for long recordings; retain every IQ sample for replay.
    hop = max(128, (data.size + 1023) // 1024)
    spec = iq_spectrogram(data, sample_rate_hz=rate, hop_length=hop,
                          center_frequency_hz=metadata["frequency_hz"])
    payload = {
        "schema_version": 1, "recorded_at": time.time(), "sample_rate_hz": rate,
        "duration_s": data.size / rate, "input": metadata,
        "spectrogram": asdict(spec.metadata),
        "operator_annotation": {"label": label, "notes": notes},
        "ground_truth": None,
        "limitations": "Operator description is not a verified modulation or protocol label. "
                        "Wideband captures require time/frequency annotations for evaluation.",
    }
    with _LOCK:
        directory.mkdir(parents=True, exist_ok=True)
        used = sum(p.stat().st_size for p in directory.glob("*.npz"))
        if used + data.size * 8 + spec.dbfs.nbytes + 65536 > _MAX_BYTES:
            raise ValueError("Validation capture quota reached (512 MiB); archive examples first")
        path = directory / f"{uuid.uuid4().hex}.npz"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                np.savez_compressed(stream, iq=data.astype(np.complex64, copy=False),
                                    spectrogram=spec.dbfs,
                                    metadata=np.array(json.dumps(payload, allow_nan=False)))
            if used + temporary.stat().st_size > _MAX_BYTES:
                raise ValueError("Validation quota reached (512 MiB); archive examples first")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"path": str(path), "samples": int(data.size), "sample_rate_hz": rate,
            "duration_s": data.size / rate, "label": label}
