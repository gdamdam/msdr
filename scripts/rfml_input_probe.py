"""Offline preprocessing sensitivity check; never a field accuracy benchmark.

Example: python scripts/rfml_input_probe.py --iq channel.npy --sample-rate 50000 \
    --offset-seconds 5 7 9 > probe.json
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from msdr.rfml.classifier import MANIFEST, RFMLClassifier
from msdr.settings import RFMLSettings


def variants(iq, rate):
    """Change one input property at a time; no prediction-driven selection."""
    yield "original", iq, rate
    yield "remove_dc", iq - iq.mean(), rate
    for phase in (np.pi / 2, np.pi):
        yield f"phase_{phase:.4f}_rad", (iq * np.exp(1j * phase)).astype(np.complex64), rate
    for shift in (-1000, 1000):
        shifted = iq * np.exp(2j * np.pi * shift * np.arange(iq.size) / rate)
        yield f"offset_{shift:+d}_hz", shifted.astype(np.complex64), rate
    for up, down in ((1, 2), (2, 1)):
        samples = resample_poly(iq, up, down).astype(np.complex64)
        yield f"resample_{up}_{down}", samples, rate * up / down


def probe(path, rate, offsets, classifier):
    if not np.isfinite(rate) or rate <= 0:
        raise ValueError("sample rate must be positive and finite")
    data = np.load(path, mmap_mode="r", allow_pickle=False)
    if data.ndim != 1 or not np.iscomplexobj(data):
        raise ValueError("expected one-dimensional complex .npy IQ")
    records = []
    for offset in offsets:
        if not np.isfinite(offset) or offset < 0:
            raise ValueError("offsets must be nonnegative and finite")
        start = int(round(offset * rate))
        iq = np.asarray(data[start:start + int(rate)], dtype=np.complex64)
        if iq.size < max(int(rate), 24576) or not np.isfinite(iq).all():
            raise ValueError("each offset needs a full second and at least 24576 finite samples")
        for name, samples, sample_rate in variants(iq, rate):
            started = time.perf_counter()
            prediction = classifier.predict(samples, sample_rate)
            records.append({"offset_s": offset, "variant": name,
                            "latency_ms": (time.perf_counter() - started) * 1000,
                            "prediction": prediction})
    # Synthetic probes test pathological acceptance, not RF false-positive rate.
    rng = np.random.default_rng(28)
    n = 12288
    for name, iq in (("synthetic_noise", rng.normal(size=n) + 1j * rng.normal(size=n)),
                     ("synthetic_dc", np.ones(n, dtype=complex)),
                     ("synthetic_tone", np.exp(2j * np.pi * 1000 * np.arange(n) / rate))):
        records.append({"synthetic_control": name,
                        "prediction": classifier.predict(iq.astype(np.complex64), rate)})
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"schema_version": 1,
            "purpose": "development sensitivity probe; not accuracy validation",
            "input_sha256": digest, "sample_rate_hz": rate, "offsets_s": offsets,
            "model_id": MANIFEST["id"], "model_sha256": MANIFEST["sha256"],
            "backend": classifier.backend, "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iq", type=Path, required=True)
    parser.add_argument("--sample-rate", type=float, required=True)
    parser.add_argument("--offset-seconds", type=float, nargs="+", required=True)
    parser.add_argument("--model-path", default="~/.cache/msdr/models/xcit_narrowband.ckpt")
    args = parser.parse_args()
    model = RFMLClassifier(RFMLSettings(device="cpu", model_path=args.model_path))
    model.load()
    result = probe(args.iq, args.sample_rate, args.offset_seconds, model)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
