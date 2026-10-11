# SPDX-License-Identifier: GPL-3.0-or-later
"""S0 reuse-before-rebuild PoC: synthetic AIS burst -> numpy GMSK demod -> HDLC/CRC.

SYNTHETIC ONLY. Nothing here was measured on field recordings.

Receiver structure mirrors the SDRangel AIS demodulator's discriminator path
(plugins/channelrx/demodais/aisdemodsink.cpp @ 6627a5cf7058, GPL-3.0-or-later,
(C) Jon Beniston M7RCE, Edouard Griffiths F4EXB): phase discriminator ->
Gaussian pulse filter -> clip -> training-sequence DC (frequency-offset)
removal -> preamble correlation for alignment -> hard slice at symbol centres
-> NRZI -> HDLC flag search / de-stuffing -> CRC-16/X.25. The deframer
(`deframe`) follows the same state machine as AISDemodSink::deframe().
It does NOT port SDRangel's newer coherent GMSK MLSE (sdrbase/dsp/gmskmlse.h),
which upstream says gains "several dB" over this discriminator baseline.

Run: .venv/bin/python docs/decisions/poc/gmsk_ais_poc.py
"""

from __future__ import annotations

import time

import numpy as np

FS = 48_000
BAUD = 9_600
SPS = FS // BAUD  # 5 samples/symbol
BT = 0.4
H = 0.5  # modulation index -> 2400 Hz peak deviation
GEN_OS = 10  # generate at 480 kS/s so bursts get a random fractional timing
TRAIN_BITS = 24
FLAG = 0x7E
PAYLOAD_BITS = 168
FREQ_OFFSET_HZ = 600.0


# ---------------------------------------------------------------- framing
def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


assert crc16_x25(b"123456789") == 0x906E  # standard CRC-16/X.25 check value


def byte_bits_lsb(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def build_bits(payload: bytes) -> list[int]:
    crc = crc16_x25(payload)
    body = byte_bits_lsb(payload + bytes([crc & 0xFF, crc >> 8]))
    stuffed, ones = [], 0
    for bit in body:
        stuffed.append(bit)
        ones = ones + 1 if bit else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    train = [i & 1 for i in range(TRAIN_BITS)]  # 0101...
    flag = byte_bits_lsb(bytes([FLAG]))
    return train + flag + stuffed + flag + [0] * 4  # 4-bit tail/ramp


def nrzi(bits: list[int]) -> np.ndarray:
    # AIS/HDLC NRZI: 0 -> transition, 1 -> no change (matches deframe()).
    level, out = 0, []
    for bit in bits:
        if bit == 0:
            level ^= 1
        out.append(level)
    return np.array(out, dtype=np.int8)


def gaussian_taps(bt: float, sps: int, span: int) -> np.ndarray:
    t = np.arange(-span * sps / 2, span * sps / 2 + 1) / sps
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-(t**2) / (2 * sigma**2))
    return g / g.sum()


def gmsk_modulate(levels: np.ndarray, frac: int) -> np.ndarray:
    """Complex baseband at FS; `frac` in [0, GEN_OS) sets fractional timing."""
    sps_hi = SPS * GEN_OS
    nrz = np.repeat(2.0 * levels - 1.0, sps_hi)
    freq = np.convolve(nrz, gaussian_taps(BT, sps_hi, 4), mode="same")
    phase = np.cumsum(freq) * (np.pi * H / sps_hi)  # +-pi/2 per symbol
    return np.exp(1j * phase[frac::GEN_OS])


# ---------------------------------------------------------------- receiver
def lowpass(num_taps: int, cutoff_hz: float) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / FS * n) * np.hamming(num_taps)
    return h / h.sum()


CHAN_LPF = lowpass(33, 7_500.0)
PULSE = gaussian_taps(BT, SPS, 3)
TEMPLATE_BITS = [i & 1 for i in range(TRAIN_BITS)] + byte_bits_lsb(bytes([FLAG]))
# Expected filtered-discriminator waveform for training+flag (one NRZI polarity;
# the other polarity is just -template, so |corr| covers both).
_tmpl_levels = 2.0 * nrzi(TEMPLATE_BITS) - 1.0
TEMPLATE = np.convolve(np.repeat(_tmpl_levels, SPS), PULSE, mode="same")
TEMPLATE -= TEMPLATE.mean()
TEMPLATE /= np.linalg.norm(TEMPLATE)
CORR_THRESHOLD = 0.6
MAX_FRAME_SYMS = TRAIN_BITS + 8 + (PAYLOAD_BITS + 16) * 6 // 5 + 8 + 8


def deframe(symbols: np.ndarray) -> bytes | None:
    """HDLC deframer, same state machine as AISDemodSink::deframe()."""
    got_sop = False
    bits = bit_count = ones = 0
    out = bytearray()
    prev = 0
    for s in symbols:
        bit = 1 if s == prev else 0
        prev = s
        bits |= bit << bit_count
        bit_count += 1
        if bit:
            ones += 1
            if ones == 7 and got_sop:
                return None
        else:
            if ones == 5:
                bit_count -= 1  # de-stuff
            elif ones == 6:
                if got_sop and bit_count == 8 and bits == FLAG and len(out) >= 3:
                    rx = out[-2] | (out[-1] << 8)
                    return bytes(out[:-2]) if crc16_x25(bytes(out[:-2])) == rx else None
                if got_sop:
                    return None
                got_sop, bits, bit_count, out = True, 0, 0, bytearray()
            ones = 0
        if bit_count == 8:
            if got_sop:
                out.append(bits)
            bits = bit_count = 0
        elif not got_sop and bit_count > 8:
            bits >>= 1
            bit_count -= 1
    return None


DIFF_LPF = lowpass(49, 6_000.0)


def detector_output(iq: np.ndarray, detector: str) -> np.ndarray:
    if detector == "disc":
        # SDRangel-mirroring path: per-sample phase discriminator + Gaussian filter.
        x = np.convolve(iq, CHAN_LPF, mode="same")
        disc = np.angle(x[1:] * np.conj(x[:-1])) / (np.pi * H / SPS)  # ~ +-1 at peak dev
        return np.clip(np.convolve(disc, PULSE, mode="same"), -1.4, 1.4)
    if detector == "diff1":
        # One-symbol differential detector: arg(x[n+SPS] x*[n]) is +-pi/2 for
        # h=0.5, with no per-sample unwrap clicks. Not in SDRangel; a cheap
        # alternative measured here to size the gap to the discriminator.
        x = np.convolve(iq, DIFF_LPF, mode="same")
        return np.angle(x[SPS:] * np.conj(x[:-SPS])) / (np.pi / 2)
    raise ValueError(detector)


def demodulate(iq: np.ndarray, detector: str = "disc") -> list[bytes]:
    filt = detector_output(iq, detector)
    n = len(TEMPLATE)
    if len(filt) < n:
        return []
    raw = np.correlate(filt, TEMPLATE, mode="valid")
    # Normalise by local energy after removing the local mean (the mean is the
    # frequency-offset DC term SDRangel subtracts via the training-sequence sum).
    c1 = np.concatenate(([0.0], np.cumsum(filt)))
    c2 = np.concatenate(([0.0], np.cumsum(filt * filt)))
    s1 = c1[n:] - c1[:-n]
    s2 = c2[n:] - c2[:-n]
    energy = np.sqrt(np.maximum(s2 - s1 * s1 / n, 1e-12))
    metric = np.abs(raw) / energy
    frames: list[bytes] = []
    idx = np.flatnonzero(metric > CORR_THRESHOLD)
    skip_until = -1
    for i in idx:
        if i < skip_until:
            continue
        lo, hi = i, min(i + n, len(metric))
        best = lo + int(np.argmax(metric[lo:hi]))  # local peak (NMS)
        # DC (frequency offset) from the training sequence only, as SDRangel does;
        # the NRZI-coded flag is not zero-mean.
        dc = float(filt[best : best + TRAIN_BITS * SPS].mean())
        got = None
        for d in (0, -1, 1, -2, 2):  # SDRangel also tries neighbouring alignments
            start = best + d + SPS // 2
            pos = start + SPS * np.arange(MAX_FRAME_SYMS)
            pos = pos[(pos >= 0) & (pos < len(filt))]
            syms = (filt[pos] - dc >= 0).astype(np.int8)
            got = deframe(syms)
            if got is not None:
                break
        if got is not None:
            frames.append(got)
            skip_until = best + MAX_FRAME_SYMS * SPS
        else:
            skip_until = best + n
    return frames


# ---------------------------------------------------------------- harness
def add_noise(sig: np.ndarray, ebn0_db: float, rng: np.random.Generator) -> np.ndarray:
    # Unit-power signal; Eb = SPS samples of power 1 per bit -> N0 = SPS / (Eb/N0).
    n0 = SPS / 10 ** (ebn0_db / 10)
    noise = (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig))) * np.sqrt(n0 / 2)
    return sig + noise


def make_burst(rng: np.random.Generator) -> tuple[np.ndarray, bytes]:
    payload = rng.integers(0, 256, PAYLOAD_BITS // 8, dtype=np.uint8).tobytes()
    sig = gmsk_modulate(nrzi(build_bits(payload)), int(rng.integers(GEN_OS)))
    return sig, payload


def rotate(sig: np.ndarray, offset_hz: float, phase: float, t0: int = 0) -> np.ndarray:
    t = np.arange(t0, t0 + len(sig))
    return sig * np.exp(1j * (2 * np.pi * offset_hz * t / FS + phase))


def monte_carlo(ebn0_list, trials: int, rng, detectors=("disc", "diff1")) -> None:
    pad = np.zeros(40 * SPS, dtype=complex)
    print(f"Frame success (synthetic, {trials} bursts/point, +{FREQ_OFFSET_HZ:.0f} Hz offset,")
    print("random carrier phase + fractional timing). SNR columns are Es/N0 referred")
    print("to a 48 kHz and a 25 kHz noise bandwidth.")
    print(f"{'Eb/N0 dB':>8} {'SNR@48k':>8} {'SNR@25k':>8}" + "".join(f"{d:>8}" for d in detectors))
    for ebn0 in ebn0_list:
        ok = dict.fromkeys(detectors, 0)
        for _ in range(trials):
            sig, payload = make_burst(rng)
            burst = np.concatenate((pad, sig, pad))
            burst = add_noise(rotate(burst, FREQ_OFFSET_HZ, rng.uniform(0, 2 * np.pi)), ebn0, rng)
            for d in detectors:  # same noisy burst through each detector
                ok[d] += any(f == payload for f in demodulate(burst, d))
        snr48 = ebn0 - 10 * np.log10(FS / BAUD)
        snr25 = ebn0 - 10 * np.log10(25_000 / BAUD)
        cols = "".join(f"{ok[d] / trials:8.3f}" for d in detectors)
        print(f"{ebn0:8.1f} {snr48:8.1f} {snr25:8.1f}" + cols)


def throughput(seconds: float, ebn0: float, rng, detector: str) -> None:
    slot = int(FS * 0.02667)
    total = int(FS * seconds)
    stream = np.zeros(total, dtype=complex)
    sent: list[bytes] = []
    for start in range(slot, total - 2 * slot, 4 * slot):  # one burst every 4 slots
        sig, payload = make_burst(rng)
        stream[start : start + len(sig)] += rotate(sig, FREQ_OFFSET_HZ, rng.uniform(0, 6.3), start)
        sent.append(payload)
    stream = add_noise(stream, ebn0, rng).astype(np.complex64)
    t0 = time.perf_counter()
    frames = demodulate(stream.astype(complex), detector)
    dt = time.perf_counter() - t0
    good = sum(1 for f in frames if f in set(sent))
    noise = demodulate(add_noise(np.zeros(total, dtype=complex), 0.0, rng), detector)
    print(
        f"\nThroughput [{detector}]: {seconds:.0f} s of 48 kS/s input, "
        f"{len(sent)} bursts @ Eb/N0 {ebn0} dB"
    )
    print(
        f"  processing {dt * 1000 / seconds:.1f} ms per second of input "
        f"({seconds / dt:.0f}x real time), decoded {good}/{len(sent)}, "
        f"false CRC passes on {seconds:.0f} s noise-only: {len(noise)}"
    )


if __name__ == "__main__":
    rng = np.random.default_rng(20260927)
    monte_carlo([4, 6, 8, 10, 12, 14, 16, 20], trials=200, rng=rng)
    for det in ("disc", "diff1"):
        throughput(10.0, 14.0, rng, det)
