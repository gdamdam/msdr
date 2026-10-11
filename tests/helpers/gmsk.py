# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic AIS (GMSK 9600, BT 0.4, h 0.5) burst generator for tests.

Copied from docs/decisions/poc/gmsk_ais_poc.py (the PoC stays standalone as
documentation) and generalised to any output sample rate so the survey
analyzer tests can build 25 kHz channels from the same generator as the
48 kHz decoder tests. SYNTHETIC ONLY: nothing here reflects a field capture.
"""

from __future__ import annotations

import numpy as np

BAUD = 9_600
BT = 0.4
H = 0.5  # modulation index -> 2400 Hz peak deviation
TRAIN_BITS = 24
FLAG = 0x7E
# NBFM discriminator scale used by msdr.source.dsp (flat tap): 5 kHz -> 1.0.
NFM_DEVIATION_HZ = 5_000.0
_FINE_OS = 40  # phase is generated at 40 samples/symbol, then interpolated


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def byte_bits_lsb(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def build_bits(payload: bytes, *, corrupt_crc: bool = False) -> list[int]:
    """Training + flag + bit-stuffed (payload + CRC, LSB first) + flag + tail."""
    crc = crc16_x25(payload)
    if corrupt_crc:
        crc ^= 0x0001
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
    return train + flag + stuffed + flag + [0] * 4


def nrzi(bits: list[int]) -> np.ndarray:
    # AIS/HDLC NRZI: 0 -> transition, 1 -> no change.
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


def gmsk_iq(levels: np.ndarray, fs: float, timing: float = 0.0) -> np.ndarray:
    """Unit-amplitude complex baseband at `fs`; `timing` in [0, 1) symbols
    shifts the sampling instants (fractional symbol timing)."""
    nrz = np.repeat(2.0 * levels - 1.0, _FINE_OS)
    freq = np.convolve(nrz, gaussian_taps(BT, _FINE_OS, 4), mode="same")
    phase = np.cumsum(freq) * (np.pi * H / _FINE_OS)  # +-pi/2 per symbol
    t_fine = np.arange(phase.size) / (BAUD * _FINE_OS)
    n_out = int((t_fine[-1] - timing / BAUD) * fs)
    t_out = timing / BAUD + np.arange(n_out) / fs
    return np.exp(1j * np.interp(t_out, t_fine, phase))


def make_burst(
    payload: bytes, fs: float, rng: np.random.Generator, *, corrupt_crc: bool = False
) -> np.ndarray:
    return gmsk_iq(nrzi(build_bits(payload, corrupt_crc=corrupt_crc)), fs, float(rng.random()))


def add_noise(sig: np.ndarray, ebn0_db: float, fs: float, rng: np.random.Generator) -> np.ndarray:
    # Unit-power signal; Eb = fs/BAUD samples of power 1 per bit.
    n0 = (fs / BAUD) / 10 ** (ebn0_db / 10)
    noise = (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig))) * np.sqrt(n0 / 2)
    return sig + noise


def rotate(sig: np.ndarray, offset_hz: float, phase: float, fs: float, t0: int = 0) -> np.ndarray:
    t = np.arange(t0, t0 + len(sig))
    return sig * np.exp(1j * (2 * np.pi * offset_hz * t / fs + phase))


def _lowpass(num_taps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / fs * n) * np.hamming(num_taps)
    return h / h.sum()


def iq_to_flat(iq: np.ndarray, fs: float = 48_000.0, cutoff_hz: float = 7_500.0) -> np.ndarray:
    """Channel filter + phase discriminator scaled like the NFM flat tap
    (5 kHz deviation -> 1.0), as float32 -- what AisDecoder is fed."""
    x = np.convolve(iq, _lowpass(33, cutoff_hz, fs), mode="same")
    disc = np.angle(x[1:] * np.conj(x[:-1])) * fs / (2 * np.pi * NFM_DEVIATION_HZ)
    return disc.astype(np.float32)


def pack_bits(fields: list[tuple[int, int]]) -> bytes:
    """[(value, nbits), ...] MSB-first (two's complement for negatives),
    zero-padded to a whole byte -- the AIS payload bit order."""
    bits: list[int] = []
    for value, nbits in fields:
        value &= (1 << nbits) - 1
        bits.extend((value >> (nbits - 1 - i)) & 1 for i in range(nbits))
    bits += [0] * (-len(bits) % 8)
    return bytes(
        sum(bit << (7 - j) for j, bit in enumerate(bits[i : i + 8])) for i in range(0, len(bits), 8)
    )


def ais_type1(
    mmsi: int,
    *,
    nav_status: int = 0,
    sog_tenths: int = 123,
    lon_deg: float = -122.4194,
    lat_deg: float = 37.8081,
    cog_tenths: int = 2710,
    heading: int = 270,
    msg_type: int = 1,
) -> bytes:
    """168-bit position report (types 1/2/3), ITU-R M.1371 field layout."""
    return pack_bits(
        [
            (msg_type, 6),
            (0, 2),  # repeat
            (mmsi, 30),
            (nav_status, 4),
            (0, 8),  # ROT
            (sog_tenths, 10),
            (1, 1),  # position accuracy
            (round(lon_deg * 600_000), 28),
            (round(lat_deg * 600_000), 27),
            (cog_tenths, 12),
            (heading, 9),
            (30, 6),  # timestamp
            (0, 2),  # manoeuvre
            (0, 3),  # spare
            (0, 1),  # RAIM
            (0, 19),  # radio status
        ]
    )


def random_payload(rng: np.random.Generator, nbytes: int = 21) -> bytes:
    return rng.integers(0, 256, nbytes, dtype=np.uint8).tobytes()
