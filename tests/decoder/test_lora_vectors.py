"""Integrity checks for the gr-lora_sdr reference IQ fixtures.

This only validates the *fixture files* (manifest <-> archive consistency,
dtypes, shapes, non-zero energy, payload hex parsing). It intentionally does
not import GNU Radio or gr-lora_sdr -- it runs in the normal msdr venv -- and
it does not exercise any decoder. Regenerate the fixtures with:

    .venv-lora/bin/python scripts/make_lora_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

DATA_DIR = Path(__file__).parent / "data" / "lora"
ARCHIVE_PATH = DATA_DIR / "vectors.npz"
MANIFEST_PATH = DATA_DIR / "manifest.json"

pytestmark = pytest.mark.skipif(
    not (ARCHIVE_PATH.exists() and MANIFEST_PATH.exists()),
    reason=(
        "LoRa reference vectors not generated; run "
        ".venv-lora/bin/python scripts/make_lora_vectors.py"
    ),
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def archive() -> dict[str, np.ndarray]:
    with np.load(ARCHIVE_PATH) as data:
        return {key: data[key] for key in data.files}


def test_manifest_has_cases(manifest: dict) -> None:
    assert isinstance(manifest.get("cases"), list)
    assert len(manifest["cases"]) > 0


def test_manifest_and_archive_keys_match(manifest: dict, archive: dict) -> None:
    manifest_ids = {case["id"] for case in manifest["cases"]}
    dropped_ids = set(manifest.get("dropped_cases", []))
    archive_ids = set(archive.keys())

    assert manifest_ids == archive_ids, (
        f"manifest/archive id mismatch: manifest only={manifest_ids - archive_ids}, "
        f"archive only={archive_ids - manifest_ids}"
    )
    # Dropped cases must not silently remain in the manifest's case list.
    assert not (dropped_ids & manifest_ids)


def test_each_case_is_well_formed(manifest: dict, archive: dict) -> None:
    for case in manifest["cases"]:
        cid = case["id"]
        assert cid in archive, f"{cid}: missing from archive"
        vec = archive[cid]

        # dtype and shape: flat int8, interleaved I/Q -> even length.
        assert vec.dtype == np.int8, f"{cid}: expected int8, got {vec.dtype}"
        assert vec.ndim == 1, f"{cid}: expected a 1-D interleaved array"
        assert vec.size % 2 == 0, f"{cid}: interleaved I/Q array must have even length"

        n_complex = vec.size // 2
        assert n_complex == case["sample_count"], (
            f"{cid}: manifest sample_count={case['sample_count']} != archive {n_complex}"
        )

        # Non-zero energy: the recorded waveform must not be silence.
        assert np.any(vec != 0), f"{cid}: archive vector is all zero"

        # iq_scale must be a finite positive number so the vector can be
        # dequantized back to a physically meaningful complex waveform.
        scale = case["iq_scale"]
        assert isinstance(scale, (int, float)) and scale > 0 and np.isfinite(scale)

        # payload hex must parse to exactly payload_len bytes.
        payload_hex = case["payload_hex"]
        payload = bytes.fromhex(payload_hex)
        assert len(payload) == case["payload_len"], (
            f"{cid}: payload_hex is {len(payload)} bytes, manifest {case['payload_len']}"
        )

        # Required TX parameter fields are present and sane.
        assert case["sf"] in range(5, 13)
        assert case["bw_hz"] > 0
        assert case["samp_rate_hz"] == case["bw_hz"], (
            f"{cid}: samp_rate must equal bw (1 sample/chip)"
        )
        assert case["cr"] in (1, 2, 3, 4)
        assert case["header"] in ("explicit", "implicit")
        assert isinstance(case["has_crc"], bool)
        assert case["ldro_mode"] in (0, 1, 2)
        assert isinstance(case["sync_word"], list) and len(case["sync_word"]) >= 1
        assert isinstance(case["reference_decode"], bool)


def test_manifest_records_environment(manifest: dict) -> None:
    env = manifest.get("environment", {})
    assert "gnuradio_version" in env
    assert "lora_sdr_version" in env
    assert manifest.get("generation_command")


def test_at_least_one_case_reference_decoded(manifest: dict) -> None:
    # Cases gr-lora_sdr itself cannot decode are kept (not tuned away), but we
    # still expect the fixture set as a whole to prove the TX path is sane.
    assert any(case.get("reference_decode") for case in manifest["cases"])
