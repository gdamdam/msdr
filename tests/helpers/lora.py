"""Synthetic LoRa transmitter for msdr's own test suite: builds the IQ the
gr-lora_sdr transmitter (commit fbbf6ea6684d00363096929c7ef015cabcfaeb6d)
produces at 1 sample/chip -- whitening (payload bytes only), CRC appended
unwhitened, Hamming encoding with the first sf-2 codewords at 4/8,
diagonal interleaving with the first block at sf-2 (header block) and
reduced-rate symbols carrying a parity bit, Gray demap + 1, preamble,
sync word as two up-chirps of id nibble*8, 2.25 down-chirps.

It reuses lora_phy's coding helpers, so it only proves self-consistency;
tests/decoder/data/lora/ (real gr-lora_sdr TX output) is what proves the
receive chain bit-exact. Use this for noise/CFO/timing sweeps.
"""

from __future__ import annotations

import math

import numpy as np

from msdr.decoder.pydecoders import lora_phy as lp


def _chirp_id(symbol: int, sf: int, reduced: bool) -> int:
    """interleaver_impl.cc + gray_demap_impl.cc: a reduced-rate symbol is
    sent as (data << 2 | parity << 1), and the parity makes the two low bits
    of the Gray-demapped value zero; then +1."""
    value = lp.gray_decode(symbol) << 2 if reduced else lp.gray_decode(symbol)
    return (value + 1) % (1 << sf)


def frame_symbols(
    payload: bytes,
    *,
    sf: int,
    cr: int = 1,
    has_crc: bool = True,
    ldro: bool = False,
    implicit_header: bool = False,
) -> list[int]:
    """Chirp ids of the header + payload symbols of one frame."""
    nibbles: list[int] = []
    if not implicit_header:
        nibbles += lp.header_encode(len(payload), cr, has_crc)
    for byte in lp.dewhiten(payload):
        nibbles += [byte & 0xF, byte >> 4]
    if has_crc:
        crc = lp.payload_crc16(payload)
        nibbles += [crc & 0xF, (crc >> 4) & 0xF, (crc >> 8) & 0xF, crc >> 12]
    ids: list[int] = []
    first = nibbles[: sf - 2]
    first += [0] * (sf - 2 - len(first))
    for s in lp.interleave([lp.hamming_encode(nb, 4) for nb in first], sf - 2, 4):
        ids.append(_chirp_id(s, sf, reduced=True))
    rest = nibbles[sf - 2 :]
    sf_app = sf - 2 if ldro else sf
    for b in range(math.ceil(len(rest) / sf_app)):
        block = rest[b * sf_app : (b + 1) * sf_app]
        codewords = [lp.hamming_encode(nb, cr) for nb in block] + [0] * (sf_app - len(block))
        for s in lp.interleave(codewords, sf_app, cr):
            ids.append(_chirp_id(s, sf, reduced=ldro))
    return ids


def make_frame(
    payload: bytes,
    *,
    sf: int,
    bw: int = 125_000,
    sync_word: int = 0x12,
    cr: int = 1,
    has_crc: bool = True,
    ldro: bool | None = None,
    implicit_header: bool = False,
    preamble_len: int = 8,
    leading_zeros: int = 0,
    trailing_zeros: int = 0,
    os_factor: int = 1,
    phase: float = 0.0,
    clock_ppm: float = 0.0,
) -> np.ndarray:
    """One complex128 frame at `os_factor` samples/chip with silence
    padding (`leading_zeros`/`trailing_zeros` in output samples).

    Output sample k is the transmitted waveform at chip time
    (k / os_factor + phase) * (1 + clock_ppm * 1e-6): `phase` is a
    sub-sample timing offset, `clock_ppm` a transmitter clock running fast
    (symbols shorter in receiver time). The matching carrier offset,
    clock_ppm * 1e-6 * f_c, is the caller's to apply."""
    n = 1 << sf
    if ldro is None:
        ldro = (2**sf / bw) > 16e-3
    ids = [0] * preamble_len + [((sync_word >> 4) & 0xF) * 8, (sync_word & 0xF) * 8]
    down = [False] * len(ids) + [True, True, True]
    ids += [0, 0, 0]
    lengths = [float(n)] * (len(ids) - 1) + [n / 4]
    data = frame_symbols(
        payload, sf=sf, cr=cr, has_crc=has_crc, ldro=ldro, implicit_header=implicit_header
    )
    ids += data
    down += [False] * len(data)
    lengths += [float(n)] * len(data)
    starts = np.concatenate([[0.0], np.cumsum(lengths)])
    scale = 1.0 + clock_ppm * 1e-6
    count = int(np.ceil((starts[-1] / scale - phase) * os_factor))
    t = (np.arange(count) / os_factor + phase) * scale
    t = t[t < starts[-1]]
    sym = np.searchsorted(starts, t, side="right") - 1
    tau = t - starts[sym]
    sid = np.asarray(ids, dtype=np.float64)[sym]
    rate = np.where(tau < n - sid, sid / n - 0.5, sid / n - 1.5)
    sig = np.exp(2j * np.pi * (tau * tau / (2 * n) + rate * tau))
    sig = np.where(np.asarray(down)[sym], np.conj(sig), sig)
    pad = (np.zeros(leading_zeros, dtype=complex), sig, np.zeros(trailing_zeros, dtype=complex))
    return np.concatenate(pad).astype(np.complex128)
