"""Opt-in, bounded local RF examples and explicit operator-label evaluation.

Predictions are never used as ground truth. Files use NumPy's non-pickle format.
"""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np

FAMILIES = {"AM", "FM", "FSK", "PSK", "QAM", "OFDM", "ASK/PAM", "CHIRP"}


def comparison(dsp: dict, prediction: dict) -> str:
    """Compare only compatible broad concepts, not voice or service identities."""
    if prediction.get("status") != "ok" or prediction.get("classification") == "UNKNOWN":
        return "ml_abstained"
    options = {
        "am": {"AM"},
        "am_like": {"AM"},
        "psk_like": {"PSK"},
        "fm_fsk": {"FM", "FSK"},
        "ook": {"ASK/PAM"},
    }.get((dsp.get("modulation") or {}).get("kind"))
    if not options:
        return "not_comparable"
    if prediction.get("classification") not in options:
        return "disagreement"
    return "compatible"  # Not independent confirmation or calibrated agreement.


class ExampleWriter:
    """One worker owns this writer; quota includes existing completed examples.

    Does not delete old examples. Concurrent application instances must use
    separate directories. No writes happen when export_dir is empty.
    """

    def __init__(self, settings):
        value = getattr(settings, "export_dir", "")
        self.directory = Path(value).expanduser() if value else None
        self.limit = int(getattr(settings, "export_max_bytes", 64 * 1024 * 1024))
        self.mode = getattr(settings, "export_mode", "disagreements")
        self.interval = float(getattr(settings, "export_interval_s", 10.0))
        self.last_write = -math.inf
        self.saved = 0
        self.used = None

    def save(self, iq, rate: float, metadata: dict, result: dict) -> str | None:
        if self.directory is None:
            return None
        relation = comparison(metadata.get("dsp", {}), result)
        if self.mode == "disagreements" and relation not in {"disagreement", "ml_abstained"}:
            return None
        if time.monotonic() - self.last_write < self.interval:
            return None
        data = np.asarray(iq)
        if (
            data.ndim != 1
            or not 4096 <= data.size <= 3 * 4096
            or not np.iscomplexobj(data)
            or not np.isfinite(data).all()
            or not math.isfinite(rate)
            or rate <= 0
        ):
            raise ValueError(
                "example requires 4096–12288 finite complex IQ samples and a positive rate"
            )
        from msdr.rfml.spectrogram import iq_spectrogram

        spec = iq_spectrogram(
            data, sample_rate_hz=rate, center_frequency_hz=float(metadata.get("frequency_hz", 0))
        )
        payload = {
            "spectrogram": asdict(spec.metadata),
            "schema_version": 1,
            "recorded_at": time.time(),
            "sample_rate_hz": rate,
            "input": metadata,
            "prediction": result,
            "comparison": relation,
            "ground_truth": None,
            "label_note": "operator annotation required; predictions are not truth",
        }
        buffer = io.BytesIO()
        np.savez_compressed(
            buffer,
            iq=data.astype(np.complex64),
            metadata=np.array(json.dumps(payload, allow_nan=False)),
            spectrogram=spec.dbfs,
        )
        size = buffer.tell()
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.used is None:
            self.used = sum(p.stat().st_size for p in self.directory.glob("*.npz"))
        if self.used + size > self.limit:
            return None
        path = self.directory / f"{uuid.uuid4().hex}.npz"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(buffer.getvalue())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.used += size
        self.saved += 1
        self.last_write = time.monotonic()
        return str(path)


def evaluate(manifest: Path, classifier) -> dict:
    """JSONL rows: path, label, sample_rate_hz; label is manually supplied.

    NOISE/UNKNOWN are rejection examples, excluded from modulation accuracy.
    Invalid rows fail explicitly rather than silently changing denominators.
    """
    rows = []
    provenance_complete = True
    evaluation_sessions = set()
    evaluation_splits = set()
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("label") not in FAMILIES | {"NOISE", "UNKNOWN"}:
            raise ValueError("unsupported ground-truth label")
        rate = float(row["sample_rate_hz"])
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("sample_rate_hz must be positive and finite")
        session_id = row.get("session_id")
        split = row.get("split")
        if session_id is None and split is None:
            provenance_complete = False
        elif (not isinstance(session_id, str) or not session_id.strip()
              or split not in {"development", "test"}):
            raise ValueError("session_id and split must be valid when supplied")
        else:
            evaluation_sessions.add(session_id)
            evaluation_splits.add(split)
        rows.append(row)
    if not rows:
        raise ValueError("evaluation manifest is empty")
    confusion = {}
    counts = Counter()
    records = []
    for row in rows:
        path = Path(row["path"]).expanduser()
        if not path.is_absolute():
            path = manifest.parent / path
        loaded = np.load(path, allow_pickle=False)
        if isinstance(loaded, np.lib.npyio.NpzFile):
            with loaded:
                iq = loaded["iq"]
        else:
            iq = loaded
        if iq.ndim != 1 or iq.size < 4096 or not np.iscomplexobj(iq) or not np.isfinite(iq).all():
            raise ValueError(f"invalid evaluation IQ: {path}")
        truth = row["label"]
        rejection = truth in {"NOISE", "UNKNOWN"}
        counts["rejection_examples" if rejection else "known_examples"] += 1
        started = time.perf_counter()
        try:
            prediction = classifier.predict(iq[:3 * 4096], float(row["sample_rate_hz"]))
            accepted = prediction.get("status") == "ok"
            label = prediction.get("classification") if accepted else "UNKNOWN"
            if accepted and label not in FAMILIES:
                raise ValueError("classifier returned unsupported family")
            counts["accepted"] += int(accepted)
            if rejection:
                counts["false_accepts"] += int(accepted)
            else:
                counts["known_accepted"] += int(accepted)
                counts["correct"] += int(label == truth)
        except Exception as exc:
            label = "ERROR"
            prediction = {"status": "error", "reason": str(exc)}
            counts["errors"] += 1
            if rejection:
                counts["rejection_errors"] += 1
        confusion.setdefault(truth, {})[label] = confusion.get(truth, {}).get(label, 0) + 1
        records.append(
            {
                "path": str(path),
                "session_id": row.get("session_id"),
                "split": row.get("split"),
                "truth": truth,
                "prediction": prediction,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        )
    return {
        "examples": len(rows),
        "provenance_complete": provenance_complete,
        "evaluation_sessions": sorted(evaluation_sessions),
        "evaluation_splits": sorted(evaluation_splits),
        # Counter omits untouched keys; explicit zeros are required by the
        # release gate to distinguish measured zero false accepts from absent
        # rejection evidence.
        "counts": {**dict.fromkeys(("errors", "accepted", "known_examples",
                                     "known_accepted", "correct", "rejection_examples",
                                     "rejection_errors", "false_accepts"), 0),
                   **counts},
        "confusion": confusion,
        "coverage": (
            counts["accepted"] / (len(rows) - counts["errors"])
            if len(rows) > counts["errors"]
            else None
        ),
        "known_accuracy_including_abstentions": (
            counts["correct"] / counts["known_examples"] if counts["known_examples"] else None
        ),
        "selective_accuracy": (
            counts["correct"] / counts["known_accepted"] if counts["known_accepted"] else None
        ),
        "rejection_false_accept_rate": (
            counts["false_accepts"]
            / (counts["rejection_examples"] - counts["rejection_errors"])
            if counts["rejection_examples"] > counts["rejection_errors"]
            else None
        ),
        "results": records,
        "limitations": "User-supplied labels; corpus representativeness is not established. "
        "Inference errors are reported separately, not successful rejections.",
    }
