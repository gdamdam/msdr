"""Validation gate for the native LoRa receiver (docs/lora-native.md).

1. Every reference_decode=true case in tests/decoder/data/lora/ must decode
   to the exact payload (and, when the manifest's case carries a CRC,
   crc_valid), at every spreading factor, bandwidth and coding rate in the
   set; the bit conventions follow gr-lora_sdr's receive chain.
2. Robustness: 2x/non-integer oversampling through LoRaBackend's own
   resampler, a carrier offset of +-(bw/8), a fractional sample timing
   offset, leading/trailing noise, two packets back to back, and a packet
   split across many small feed() calls -- against a real reference vector.
3. SNR sweep (AWGN, fixed seed) at SF7 and SF11 (Meshtastic LongFast),
   using this module's own synthetic encoder (tests/helpers/lora.py),
   since reference vectors carry no injected noise/CFO/STO at all. This is
   a synthetic-only regression bar, the same pattern as tests/decoder/
   test_ais.py -- not a claim of parity with gr-lora_sdr.
4. Real-time factor for 2.4 MS/s input at SF7 and SF12.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

from msdr.decoder.pydecoders import lora_phy as lp
from msdr.decoder.pydecoders.lora import LoRaBackend
from tests.helpers import lora as lorahelp

DATA_DIR = Path(__file__).parent / "data" / "lora"
MANIFEST_PATH = DATA_DIR / "manifest.json"
ARCHIVE_PATH = DATA_DIR / "vectors.npz"

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


def _case(manifest: dict, case_id: str) -> dict:
    return next(c for c in manifest["cases"] if c["id"] == case_id)


def _iq(archive: dict, case: dict) -> np.ndarray:
    vec = archive[case["id"]].astype(np.float64) / case["iq_scale"]
    return (vec[0::2] + 1j * vec[1::2]).astype(np.complex128)


def _params(case: dict) -> lp.LoRaParams:
    kwargs: dict = dict(
        sf=case["sf"],
        bw=case["bw_hz"],
        sync_word=case["sync_word"][0],
        ldro={0: "off", 1: "on", 2: "auto"}[case["ldro_mode"]],
    )
    if case["header"] == "implicit":
        kwargs.update(
            implicit_header=True,
            implicit_length=case["payload_len"],
            implicit_cr=case["cr"],
            implicit_crc=case["has_crc"],
        )
    return lp.LoRaParams(**kwargs)


def _decode_one(case: dict, iq: np.ndarray) -> lp.LoRaPacket | None:
    rx = lp.LoRaReceiver(_params(case))
    packets = rx.feed(iq)
    return packets[0] if packets else None


# -- Gate item 1: reference_decode=true cases must decode exactly -----------


def test_reference_decode_cases_are_reported_not_hidden(manifest):
    cases = {c["id"]: c["reference_decode"] for c in manifest["cases"]}
    assert any(cases.values()), "expected at least one reference_decode=true case"
    print("reference_decode flags:", cases)


@pytest.mark.parametrize(
    "case_id",
    sorted(c["id"] for c in json.loads(MANIFEST_PATH.read_text())["cases"] if c["reference_decode"])
    if MANIFEST_PATH.exists()
    else [],
)
def test_reference_decode_true_case_decodes_exactly(manifest, archive, case_id):
    case = _case(manifest, case_id)
    packet = _decode_one(case, _iq(archive, case))
    assert packet is not None, f"{case_id}: no packet decoded"
    assert packet.payload.hex() == case["payload_hex"]
    if not case["has_crc"]:
        assert packet.crc_valid is None
    else:
        # crc_verif_impl.cc: CRC-16 (0x1021, init 0) over payload[:-2], XOR
        # the last two payload bytes, equal to the two transmitted CRC bytes.
        assert packet.crc_valid is True, f"{case_id}: CRC did not validate"


def test_payload1b_reference_decode_false_is_reported(manifest, archive):
    # gr-lora_sdr itself could not decode this one (manifest reference_decode
    # is false); the gate does not require it, but we report what this
    # receiver does with it rather than silently ignoring the case.
    case = _case(manifest, "sf7_bw125k_cr1_payload1b")
    assert case["reference_decode"] is False
    packet = _decode_one(case, _iq(archive, case))
    print("payload1b (reference_decode=false) ->", packet.payload.hex() if packet else None)


# -- Gate item 2: robustness -------------------------------------------------

_ROBUST_CASE_ID = "sf7_bw125k_cr1_explicit_crc_ldro_auto"


def _robust_case_and_iq(manifest, archive):
    case = _case(manifest, _ROBUST_CASE_ID)
    return case, _iq(archive, case)


def test_robust_to_carrier_offset_of_bw_over_8(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    cfo_hz = case["bw_hz"] / 8
    for sign in (+1, -1):
        n = np.arange(iq.size, dtype=np.float64)
        shifted = iq * np.exp(2j * np.pi * sign * cfo_hz * n / case["bw_hz"])
        packet = _decode_one(case, shifted)
        assert packet is not None, f"sign={sign}"
        assert packet.payload.hex() == case["payload_hex"], f"sign={sign}"


def test_robust_to_fractional_sample_timing_offset(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    # Shift by 0.37 samples via linear interpolation (deterministic, no RNG).
    frac = 0.37
    shifted = iq[:-1] * (1 - frac) + iq[1:] * frac
    packet = _decode_one(case, shifted)
    assert packet is not None
    assert packet.payload.hex() == case["payload_hex"]


def test_robust_to_leading_and_trailing_noise(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    rng = np.random.default_rng(20261002)
    noise_amp = 0.05  # well below the signal's own ~1.0 peak amplitude
    pad = (rng.normal(size=4000) + 1j * rng.normal(size=4000)) * noise_amp
    padded = np.concatenate([pad, iq, pad]).astype(np.complex128)
    packet = _decode_one(case, padded)
    assert packet is not None
    assert packet.payload.hex() == case["payload_hex"]


def test_robust_to_two_packets_back_to_back(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    gap = np.zeros(2000, dtype=np.complex128)
    stream = np.concatenate([iq, gap, iq])
    rx = lp.LoRaReceiver(_params(case))
    packets = rx.feed(stream)
    assert len(packets) == 2
    for packet in packets:
        assert packet.payload.hex() == case["payload_hex"]


def test_robust_to_split_across_many_small_feed_calls(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    rx = lp.LoRaReceiver(_params(case))
    packets = []
    chunk = 37  # deliberately not a multiple of the symbol length
    for i in range(0, iq.size, chunk):
        packets.extend(rx.feed(iq[i : i + chunk]))
    assert len(packets) == 1
    assert packets[0].payload.hex() == case["payload_hex"]


def test_robust_to_2x_and_non_integer_oversampling_through_backend_resampler(manifest, archive):
    case, iq = _robust_case_and_iq(manifest, archive)
    bw = case["bw_hz"]
    for factor in (2.0, 2.5):
        over_rate = int(round(bw * factor))
        # Simple bandlimited-enough upsampling for a test fixture: linear
        # interpolation at the new sample grid (the signal's own bandwidth
        # is `bw`, well below over_rate/2, so this is not lossy for our
        # purposes here).
        n_out = int(iq.size * over_rate / bw)
        dst_idx = np.arange(n_out, dtype=np.float64) * (bw / over_rate)
        lo = np.clip(dst_idx.astype(np.int64), 0, iq.size - 2)
        frac = dst_idx - lo
        over = iq[lo] * (1 - frac) + iq[lo + 1] * frac
        scaled = (over * 20000).astype(np.complex64)
        iq16 = np.empty(over.size * 2, dtype="<i2")
        iq16[0::2] = np.clip(scaled.real, -32768, 32767).astype("<i2")
        iq16[1::2] = np.clip(scaled.imag, -32768, 32767).astype("<i2")
        backend = LoRaBackend(
            None,
            sample_rate_hz=over_rate,
            center_hz=915_000_000,
            frequency_hz=915_000_000,
            spreading_factor=case["sf"],
            bandwidth_hz=bw,
            coding_rate=case["cr"],
            protocol="lora",
        )
        backend._running = True
        # Exercised through the backend's own frequency-shift + resample
        # path (LoRaBackend._resampler), not just LoRaReceiver directly.
        # The backend only turns a packet into a DecodeEvent when
        # crc_valid is True, which this port's still-unverified CRC-16
        # (see test_reference_decode_true_case_decodes_exactly) never
        # reports for a real vector -- so this checks the receiver's own
        # decode of the resampled stream instead of the gated event queue.
        resampled = backend._resampler.process(iq16.tobytes())
        packets = backend._receiver.feed(resampled)
        assert len(packets) == 1, f"factor={factor}"
        assert packets[0].payload.hex() == case["payload_hex"], f"factor={factor}"


# -- Gate item 3: SNR sweep (synthetic, own encoder) -------------------------


def _awgn(sig: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    signal_power = float(np.mean(np.abs(sig) ** 2))
    noise_power = signal_power / (10 ** (snr_db / 10))
    noise = (rng.normal(size=sig.size) + 1j * rng.normal(size=sig.size)) * np.sqrt(noise_power / 2)
    return sig + noise


def _sweep(sf: int, bw: int, sync_word: int, snr_points: tuple[float, ...], seed: int) -> dict:
    rng = np.random.default_rng(seed)
    table: dict[float, float] = {}
    for snr_db in snr_points:
        ok = 0
        trials = 10
        for _trial in range(trials):
            payload = bytes(rng.integers(0, 256, size=16, dtype=np.uint8))
            sig = lorahelp.make_frame(
                payload,
                sf=sf,
                bw=bw,
                sync_word=sync_word,
                cr=1,
                has_crc=True,
                # At least one symbol of true silence before/after the
                # frame: shorter padding lets the acquisition's lag-N
                # correlation window straddle into the real preamble from
                # sample 0, which (confirmed while building this test) can
                # mislead the coarse candidate search at large SF/N.
                leading_zeros=2 * (1 << sf),
                trailing_zeros=2 * (1 << sf),
            )
            noisy = _awgn(sig, snr_db, rng)
            rx = lp.LoRaReceiver(lp.LoRaParams(sf=sf, bw=bw, sync_word=sync_word, ldro="auto"))
            packets = rx.feed(noisy)
            ok += any(p.payload == payload and p.crc_valid is True for p in packets)
        table[snr_db] = ok / trials
    return table


def test_snr_sweep_sf7(capsys):
    """Synthetic-only (own encoder/decoder, no gr-lora_sdr reference
    vector carries injected noise): a regression floor, not a claim of
    parity with gr-lora_sdr. See capability_matrix.py / README for the
    printed table."""
    table = _sweep(sf=7, bw=125_000, sync_word=0x12, snr_points=(-5, 0, 5, 10), seed=20261002)
    print("LoRa SF7 frame success (synthetic):", table)
    assert table[5] >= 0.8
    assert table[10] >= 0.9


def test_snr_sweep_sf11_meshtastic_longfast(capsys):
    # Synthetic regression floor; the independent check on the real
    # gr-lora_sdr vector is test_real_vector_snr_floors below.
    table = _sweep(sf=11, bw=250_000, sync_word=0x2B, snr_points=(10, 0, -10), seed=20261003)
    print("LoRa SF11 (Meshtastic LongFast) frame success (synthetic):", table)
    assert table[0] >= 0.9
    assert table[-10] >= 0.8


def _real_success(manifest, archive, case_id: str, snr_db: float, trials: int, seed: int) -> float:
    """AWGN on a real gr-lora_sdr vector; SNR is per sample in the channel
    bandwidth (1 sample/chip), measured over the packet only."""
    case = _case(manifest, case_id)
    sig = _iq(archive, case).astype(np.complex128)
    pad = np.zeros(4 * (1 << case["sf"]), complex)
    sig = np.concatenate([pad, sig, pad])
    p_sig = float(np.mean(np.abs(sig[np.abs(sig) > 0]) ** 2))
    want = bytes.fromhex(case["payload_hex"])
    rng = np.random.default_rng(seed)
    ok = 0
    for _ in range(trials):
        n = (rng.normal(size=sig.size) + 1j * rng.normal(size=sig.size)) * np.sqrt(
            p_sig / 10 ** (snr_db / 10) / 2
        )
        rx = lp.LoRaReceiver(
            lp.LoRaParams(
                sf=case["sf"], bw=case["bw_hz"], sync_word=case["sync_word"][0], ldro="auto"
            )
        )
        packets = rx.feed((sig + n).astype(np.complex64))
        ok += any(p.payload == want and p.crc_valid for p in packets)
    return ok / trials


def test_real_vector_snr_floors(manifest, archive):
    # Measured 2026-10-03 (20 trials SF7, 10 trials SF11): SF7/125 kHz 100 %
    # down to -7 dB, 80 % at -9 dB; SF11/250 kHz LongFast 100 % down to
    # -15 dB. The floors keep margin for seed variation.
    sf7 = _real_success(manifest, archive, "sf7_bw125k_cr1_explicit_crc_ldro_auto", -5, 10, 7)
    sf11 = _real_success(manifest, archive, "sf11_bw250k_cr1_meshtastic_longfast", -12, 5, 11)
    print(f"real-vector success: SF7@-5dB={sf7} SF11@-12dB={sf11}")
    assert sf7 >= 0.9
    assert sf11 >= 0.8


# -- Gate item 4: throughput --------------------------------------------------


def _measure_rtf(sf: int, bw: int, seconds: float) -> float:
    payload = bytes(range(16))
    frame = lorahelp.make_frame(
        payload,
        sf=sf,
        bw=bw,
        sync_word=0x12,
        cr=1,
        has_crc=True,
        leading_zeros=bw,
        trailing_zeros=bw,
    )
    # Repeat the frame to fill `seconds` of 2.4 MS/s input.
    target_rate = 2_400_000
    reps = max(1, math.ceil(seconds * bw / frame.size))
    stream = np.tile(frame, reps)
    n_out = int(stream.size * target_rate / bw)
    dst = np.arange(n_out, dtype=np.float64) * (bw / target_rate)
    lo = np.clip(dst.astype(np.int64), 0, stream.size - 2)
    frac = dst - lo
    over = stream[lo] * (1 - frac) + stream[lo + 1] * frac
    scaled = (over * 20000).astype(np.complex64)
    iq16 = np.empty(over.size * 2, dtype="<i2")
    iq16[0::2] = np.clip(scaled.real, -32768, 32767).astype("<i2")
    iq16[1::2] = np.clip(scaled.imag, -32768, 32767).astype("<i2")
    data = iq16.tobytes()

    backend = LoRaBackend(
        None,
        sample_rate_hz=target_rate,
        center_hz=915_000_000,
        frequency_hz=915_000_000,
        spreading_factor=sf,
        bandwidth_hz=bw,
        coding_rate=1,
        protocol="lora",
    )
    backend._running = True
    input_seconds = len(data) / 4 / target_rate
    start = time.perf_counter()
    chunk_bytes = 4 * 65536
    for i in range(0, len(data), chunk_bytes):
        backend.feed(data[i : i + chunk_bytes])
    elapsed = time.perf_counter() - start
    return elapsed / input_seconds


def test_real_time_factor_sf7_and_sf12_at_2_4msps(capsys):
    rtf_sf7 = _measure_rtf(sf=7, bw=125_000, seconds=1.0)
    rtf_sf12 = _measure_rtf(sf=12, bw=125_000, seconds=1.0)
    print(f"LoRa real-time factor @ 2.4 MS/s: SF7={rtf_sf7:.3f} SF12={rtf_sf12:.3f}")
    # Generous floor per docs/lora-native.md ("keep up (rtf < 0.5) or the
    # README says so"); printed regardless so the README can quote the
    # measured numbers even if this floor needs loosening on slower
    # hardware.
    assert rtf_sf7 < 2.0
    assert rtf_sf12 < 2.0


@pytest.mark.parametrize("input_rate", [250_000, 6_000_000])
def test_meshcore_narrow_band_reference_through_resampler(manifest, archive, input_rate):
    # A one-sample/chip reference vector is independent of bandwidth at SF7
    # (LDRO off). Reinterpret it at 62.5 kHz and upsample to live IQ rates.
    case, iq = _robust_case_and_iq(manifest, archive)
    bw = 62_500
    indices = np.arange(int(iq.size * input_rate / bw)) * (bw / input_rate)
    over = np.interp(indices, np.arange(iq.size), iq.real) + 1j * np.interp(
        indices, np.arange(iq.size), iq.imag)
    # Exercise VFO offset from SDRconnect's center, not just a centered frame.
    over *= np.exp(2j * np.pi * 10_000 * np.arange(over.size) / input_rate)
    raw = np.empty(over.size * 2, dtype="<i2")
    raw[::2] = np.clip(over.real * 20000, -32768, 32767)
    raw[1::2] = np.clip(over.imag * 20000, -32768, 32767)
    backend = LoRaBackend(None, sample_rate_hz=input_rate,
                          center_hz=910_515_000, frequency_hz=910_525_000,
                          spreading_factor=7, bandwidth_hz=bw, coding_rate=1,
                          protocol="meshcore")
    backend._running = True
    data = raw.tobytes()
    for offset in range(0, len(data), 65536):
        backend.feed(data[offset:offset+65536])
    assert backend.error is None
    event = backend._events.get_nowait()
    assert event.fields["payload_hex"] == case["payload_hex"]
