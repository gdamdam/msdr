#!/usr/bin/env python3
"""Generate independent LoRa reference IQ vectors with the real gr-lora_sdr TX.

These vectors exist to validate a *new*, independent native (numpy) LoRa
decoder against the real gr-lora_sdr implementation, so this script must only
ever produce data -- it must not import or exercise the new decoder.

Run with the GNU Radio 3.10 + gr-lora_sdr runtime:
    .venv-lora/bin/python scripts/make_lora_vectors.py

Output:
    tests/decoder/data/lora/vectors.npz    one int8-interleaved-IQ array per case
    tests/decoder/data/lora/manifest.json  per-case TX parameters + sanity-check status

Each case is transmitted with samp_rate == bw (one sample per chip, the
lowest rate gr-lora_sdr's hier blocks accept -- see the `(samp_rate/bw)
.is_integer()` assert in lora_sdr_lora_tx.block.yml). A packet is one
hierarchical TX block run per case; frame_zero_padd adds trailing zero
samples inside the flowgraph, and this script prepends an additional
~2 symbols of leading zero samples itself (frame_zero_padd only pads the
tail).

Payloads are derived deterministically from the case id via repeated SHA-256
(not Python's `random`, whose str-seeding is hash-randomized across
processes), so a regeneration run reproduces byte-identical payloads.

Payloads are printable ASCII (lowercase hex digits of the SHA-256 stream).
gr-lora_sdr's TX takes the payload as a PMT string on the whitening block's
"msg" port and transmits that text byte for byte; posting hex digits with the
intent of binary bytes was observed to transmit the hex characters
themselves (2x length). Bytes >= 0x80 cannot pass through that text path
unchanged, so they are not covered by these vectors; whitening, FEC and CRC
do not depend on the byte values, and the decoder unit tests cover them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "tests" / "decoder" / "data" / "lora"
ARCHIVE_PATH = DATA_DIR / "vectors.npz"
MANIFEST_PATH = DATA_DIR / "manifest.json"

QUANTIZED_PEAK = 100  # target peak magnitude for the int8 I/Q quantization
LEADING_SYMBOLS = 2  # extra all-zero symbols prepended before the frame

CR_LABELS = {1: "4/5", 2: "4/6", 3: "4/7", 4: "4/8"}


@dataclass
class Case:
    id: str
    sf: int
    bw: int
    cr: int
    impl_head: bool
    has_crc: bool
    ldro_mode: int  # 0=off, 1=on, 2=auto (gr-lora_sdr semantics)
    sync_word: list[int]
    payload_len: int
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _default_cases() -> list[Case]:
    cases: list[Case] = []

    # sf7..sf12 @ 125 kHz, cr 4/5, explicit header, CRC on, LDRO auto, sync 0x12
    for sf in range(7, 13):
        cases.append(
            Case(
                id=f"sf{sf}_bw125k_cr1_explicit_crc_ldro_auto",
                sf=sf,
                bw=125_000,
                cr=1,
                impl_head=False,
                has_crc=True,
                ldro_mode=2,
                sync_word=[0x12],
                payload_len=16,
                notes="baseline sweep; LDRO auto per LoRa Ts>16ms rule",
            )
        )

    # sf7 and sf9 @ 125 kHz, cr 4/6, 4/7, 4/8
    for sf in (7, 9):
        for cr in (2, 3, 4):
            cases.append(
                Case(
                    id=f"sf{sf}_bw125k_cr{cr}_explicit_crc_ldro_auto",
                    sf=sf,
                    bw=125_000,
                    cr=cr,
                    impl_head=False,
                    has_crc=True,
                    ldro_mode=2,
                    sync_word=[0x12],
                    payload_len=16,
                    notes="coding-rate sweep",
                )
            )

    # Bandwidth sweep, cr 4/5
    bw_sweep = [(250_000, 7), (250_000, 9), (250_000, 11), (500_000, 7), (500_000, 12)]
    for bw, sf in bw_sweep:
        if bw == 250_000 and sf == 11:
            # Meshtastic LongFast.
            cases.append(
                Case(
                    id="sf11_bw250k_cr1_meshtastic_longfast",
                    sf=sf,
                    bw=bw,
                    cr=1,
                    impl_head=False,
                    has_crc=True,
                    ldro_mode=0,
                    sync_word=[0x2B],
                    payload_len=32,
                    notes="Meshtastic LongFast preset: sync 0x2B, LDRO off",
                )
            )
        else:
            cases.append(
                Case(
                    id=f"sf{sf}_bw{bw // 1000}k_cr1_explicit_crc_ldro_auto",
                    sf=sf,
                    bw=bw,
                    cr=1,
                    impl_head=False,
                    has_crc=True,
                    ldro_mode=2,
                    sync_word=[0x12],
                    payload_len=16,
                    notes="bandwidth sweep",
                )
            )

    # Implicit header.
    cases.append(
        Case(
            id="sf8_bw125k_cr1_implicit_crc",
            sf=8,
            bw=125_000,
            cr=1,
            impl_head=True,
            has_crc=True,
            ldro_mode=2,
            sync_word=[0x12],
            payload_len=12,
            notes="implicit header mode",
        )
    )

    # CRC off.
    cases.append(
        Case(
            id="sf7_bw125k_cr1_explicit_crcoff",
            sf=7,
            bw=125_000,
            cr=1,
            impl_head=False,
            has_crc=False,
            ldro_mode=2,
            sync_word=[0x12],
            payload_len=16,
            notes="CRC disabled",
        )
    )

    # Payload-length extremes.
    for n in (1, 200):
        cases.append(
            Case(
                id=f"sf7_bw125k_cr1_payload{n}b",
                sf=7,
                bw=125_000,
                cr=1,
                impl_head=False,
                has_crc=True,
                ldro_mode=2,
                sync_word=[0x12],
                payload_len=n,
                notes="payload length extreme",
            )
        )

    return cases


def deterministic_payload(case_id: str, length: int) -> bytes:
    """`length` deterministic printable ASCII bytes from `case_id` (hex
    digits of a SHA-256 stream; see the module docstring for why ASCII)."""
    out = ""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"lora-vectors:{case_id}:{counter}".encode()).hexdigest()
        counter += 1
    return out[:length].encode("ascii")


def ldro_effective(sf: int, bw: int, ldro_mode: int) -> bool:
    """Mirror gr-lora_sdr's "Auto" rule: enable LDRO when the symbol period
    exceeds 16 ms."""
    if ldro_mode == 2:
        symbol_period_s = (2**sf) / bw
        return symbol_period_s > 16e-3
    return bool(ldro_mode)


def _lora_sdr_metadata() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        from gnuradio import gr  # noqa: PLC0415

        meta["gnuradio_version"] = gr.version()
    except Exception as exc:  # pragma: no cover - diagnostic path
        meta["gnuradio_version"] = f"<unavailable: {exc}>"

    conda_meta_dir = REPO_ROOT / ".venv-lora" / "conda-meta"
    lora_sdr_json = (
        sorted(conda_meta_dir.glob("gnuradio-lora_sdr-*.json")) if conda_meta_dir.is_dir() else []
    )
    if lora_sdr_json:
        try:
            pkg = json.loads(lora_sdr_json[-1].read_text())
            meta["lora_sdr_version"] = pkg.get("version")
            meta["lora_sdr_build"] = pkg.get("build")
            meta["lora_sdr_channel"] = pkg.get("channel")
        except Exception as exc:  # pragma: no cover - diagnostic path
            meta["lora_sdr_package_error"] = str(exc)
    else:
        meta["lora_sdr_version"] = "<conda-meta package record not found>"

    # Best-effort upstream commit, if the runtime was built from a git checkout
    # somewhere under the venv (not expected for a conda package, but cheap to
    # check and harmless if absent).
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%H", "--", ".venv-lora"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            meta["lora_sdr_repo_commit_in_msdr_git"] = result.stdout.strip()
    except Exception:
        pass

    meta["python_version"] = platform.python_version()
    return meta


def _run_tx(case: Case, payload: bytes) -> np.ndarray:
    """Run one gr-lora_sdr TX flowgraph for `case` and return the raw
    complex64 samples it produced (no padding applied yet)."""
    import pmt  # noqa: PLC0415
    from gnuradio import blocks, gr  # noqa: PLC0415
    from gnuradio.lora_sdr.lora_sdr_lora_tx import lora_sdr_lora_tx  # noqa: PLC0415

    samp_rate = case.bw  # one sample per chip, per task spec
    sps = samp_rate // case.bw  # == 1
    assert sps == 1
    symbol_samples = 2**case.sf
    frame_zero_padd = 2 * symbol_samples

    tx = lora_sdr_lora_tx(
        bw=case.bw,
        cr=case.cr,
        has_crc=case.has_crc,
        impl_head=case.impl_head,
        samp_rate=samp_rate,
        sf=case.sf,
        ldro_mode=case.ldro_mode,
        frame_zero_padd=frame_zero_padd,
        sync_word=case.sync_word,
    )
    sink = blocks.vector_sink_c()
    tb = gr.top_block(f"lora_tx_{case.id}")
    tb.connect(tx, sink)
    tb.start()

    # The whitening block transmits the posted text byte for byte.
    tx.lora_sdr_whitening_0.to_basic_block()._post(
        pmt.intern("msg"), pmt.intern(payload.decode("ascii"))
    )

    # Rough symbol-count estimate (Semtech formula) purely to size the wait;
    # correctness of the capture is decided by the "stopped growing" check
    # below, not by this estimate.
    de = 1 if ldro_effective(case.sf, case.bw, case.ldro_mode) else 0
    ih = 1 if case.impl_head else 0
    crc_bit = 1 if case.has_crc else 0
    numerator = 8 * case.payload_len - 4 * case.sf + 28 + 16 * crc_bit - 20 * ih
    payload_symbols = 8 + max(int(np.ceil(numerator / (4 * (case.sf - 2 * de)))) * (case.cr + 4), 0)
    est_symbols = 8 + 6 + payload_symbols  # preamble + sync/SFD + payload
    est_samples = est_symbols * symbol_samples + frame_zero_padd

    deadline = time.monotonic() + max(10.0, est_samples / samp_rate * 8 + 3.0)
    last_len = -1
    stable_polls = 0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        cur_len = len(sink.data())
        if cur_len == last_len and cur_len >= est_samples * 0.5:
            stable_polls += 1
            if stable_polls >= 10:
                break
        else:
            stable_polls = 0
        last_len = cur_len

    tb.stop()
    tb.wait()
    samples = np.asarray(sink.data(), dtype=np.complex64)
    if samples.size == 0:
        raise RuntimeError(f"case {case.id}: TX produced zero samples")
    return samples


def _rx_sanity_check(case: Case, samples: np.ndarray, expected_payload: bytes) -> dict[str, Any]:
    """Feed `samples` (already dequantized back to complex64) through
    gr-lora_sdr's own lora_sdr_lora_rx and report whether it reproduced the
    exact payload (and, when has_crc, a verified CRC)."""
    try:
        import pmt  # noqa: PLC0415
        from gnuradio import blocks, gr  # noqa: PLC0415
        from gnuradio.lora_sdr.lora_sdr_lora_rx import lora_sdr_lora_rx  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"reference_decode": False, "reference_decode_detail": f"import failed: {exc}"}

    try:
        rx = lora_sdr_lora_rx(
            center_freq=915_000_000,
            bw=case.bw,
            cr=case.cr,
            has_crc=case.has_crc,
            impl_head=case.impl_head,
            pay_len=case.payload_len,
            samp_rate=case.bw,
            sf=case.sf,
            sync_word=case.sync_word,
            soft_decoding=False,
            ldro_mode=case.ldro_mode,
            print_rx=[False, False],
        )
        # Trailing zeros let the receiver flush its last symbols.
        tail = [0j] * (8 * 2**case.sf)
        src = blocks.vector_source_c(samples.tolist() + tail, repeat=False)
        byte_sink = blocks.vector_sink_b()
        tb = gr.top_block(f"lora_rx_{case.id}")
        tb.connect(src, rx)
        tb.connect(rx, byte_sink)
        tb.start()
        tb.wait()  # the vector source ends the flowgraph

        # The stream output carries the payload bytes; its frame_info tag
        # carries crc_valid and the decoded header fields.
        payload_bytes = bytes(byte_sink.data())
        crc_valid = None
        for tag in byte_sink.tags():
            if pmt.symbol_to_string(tag.key) == "frame_info":
                info = pmt.to_python(tag.value)
                if isinstance(info, dict) and "crc_valid" in info:
                    crc_valid = bool(info["crc_valid"])
        ok = payload_bytes == expected_payload
        if case.has_crc:
            ok = ok and crc_valid is True
        detail = f"payload_match={payload_bytes == expected_payload} crc_valid={crc_valid}"
        return {"reference_decode": bool(ok), "reference_decode_detail": detail}
    except Exception as exc:  # pragma: no cover - diagnostic path
        return {"reference_decode": False, "reference_decode_detail": f"RX flowgraph error: {exc}"}


def build_manifest_entry(
    case: Case, payload: bytes, sample_count: int, scale: float
) -> dict[str, Any]:
    return {
        "id": case.id,
        "sf": case.sf,
        "bw_hz": case.bw,
        "samp_rate_hz": case.bw,
        "cr": case.cr,
        "cr_label": CR_LABELS[case.cr],
        "header": "implicit" if case.impl_head else "explicit",
        "has_crc": case.has_crc,
        "ldro_mode": case.ldro_mode,
        "ldro_mode_label": {0: "off", 1: "on", 2: "auto"}[case.ldro_mode],
        "ldro_effective": ldro_effective(case.sf, case.bw, case.ldro_mode),
        "sync_word": case.sync_word,
        "payload_len": case.payload_len,
        "payload_hex": payload.hex(),
        "frame_zero_padd_samples": 2 * (2**case.sf),
        "leading_zero_samples": LEADING_SYMBOLS * (2**case.sf),
        "sample_count": sample_count,
        "iq_dtype": "int8",
        "iq_layout": "interleaved [I0, Q0, I1, Q1, ...]",
        "iq_scale": scale,
        "notes": case.notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-archive-bytes",
        type=int,
        default=3 * 1024 * 1024,
        help="Soft cap for vectors.npz; drops the sf7/200-byte case first if exceeded.",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cases = _default_cases()

    arrays: dict[str, np.ndarray] = {}
    manifest_cases: list[dict[str, Any]] = []
    dropped: list[str] = []

    for case in cases:
        payload = deterministic_payload(case.id, case.payload_len)
        print(
            f"[{case.id}] sf={case.sf} bw={case.bw} cr={case.cr} payload={case.payload_len}B",
            file=sys.stderr,
        )

        raw = _run_tx(case, payload)
        symbol_samples = 2**case.sf
        leading = np.zeros(LEADING_SYMBOLS * symbol_samples, dtype=np.complex64)
        samples = np.concatenate([leading, raw])

        peak = float(max(np.max(np.abs(samples.real)), np.max(np.abs(samples.imag))))
        scale = (QUANTIZED_PEAK / peak) if peak > 0 else 1.0
        i_q = np.empty(2 * len(samples), dtype=np.int8)
        i_q[0::2] = np.clip(np.round(samples.real * scale), -128, 127).astype(np.int8)
        i_q[1::2] = np.clip(np.round(samples.imag * scale), -128, 127).astype(np.int8)

        # Dequantize for the RX sanity check, so it exercises exactly what is
        # stored on disk (quantization noise included).
        dequantized = (i_q[0::2].astype(np.float32) + 1j * i_q[1::2].astype(np.float32)) / scale
        dequantized = dequantized.astype(np.complex64)
        rx_result = _rx_sanity_check(case, dequantized, payload)

        entry = build_manifest_entry(case, payload, len(samples), scale)
        entry.update(rx_result)
        manifest_cases.append(entry)
        arrays[case.id] = i_q

    # Enforce the ~3 MB archive budget by dropping the heaviest case first if
    # needed (the 200-byte payload at sf7/125kHz is the slowest/largest case
    # named in the task spec).
    def _archive_size(arrs: dict[str, np.ndarray]) -> int:
        import io

        buf = io.BytesIO()
        np.savez_compressed(buf, **arrs)
        return buf.tell()

    drop_order = ["sf7_bw125k_cr1_payload200b"]
    size = _archive_size(arrays)
    for candidate in drop_order:
        if size <= args.max_archive_bytes:
            break
        if candidate in arrays:
            del arrays[candidate]
            manifest_cases = [c for c in manifest_cases if c["id"] != candidate]
            dropped.append(candidate)
            size = _archive_size(arrays)

    np.savez_compressed(ARCHIVE_PATH, **arrays)

    manifest = {
        "generated_by": "scripts/make_lora_vectors.py",
        "generation_command": "{} {}".format(sys.executable, " ".join(sys.argv)),
        "environment": _lora_sdr_metadata(),
        "archive_file": ARCHIVE_PATH.name,
        "archive_bytes": ARCHIVE_PATH.stat().st_size,
        "dropped_cases": dropped,
        "quantization": {
            "dtype": "int8",
            "layout": "interleaved I/Q per case, one array per case keyed by case id",
            "target_peak_magnitude": QUANTIZED_PEAK,
            "scale_field": (
                "iq_scale (float): stored int8 = complex64 sample x iq_scale; divide to reconstruct"
            ),
        },
        "cases": manifest_cases,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")

    n_ref_ok = sum(1 for c in manifest_cases if c.get("reference_decode"))
    print(
        f"Wrote {ARCHIVE_PATH} ({ARCHIVE_PATH.stat().st_size} bytes) and {MANIFEST_PATH} "
        f"with {len(manifest_cases)} cases ({n_ref_ok} reference-decoded OK); dropped={dropped}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
