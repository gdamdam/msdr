# SPDX-License-Identifier: GPL-3.0-or-later
"""Native (pure numpy, no GNU Radio) receive-only LoRa chirp-spread-spectrum PHY.

Provenance (both upstreams GPL-3.0-or-later; msdr is GPL-3.0-only):

- Structure (dechirp + FFT symbol demodulation, preamble -> network id ->
  SFD -> header -> payload pipeline, the coding-chain stages as separate
  helpers) follows SDRangel's ChirpChat demodulator,
  `plugins/channelrx/demodchirpchat/` at commit
  030963609f7a667c62259dbcf3cee7522880885c (Copyright (C) 2019-2020 Edouard
  Griffiths, F4EXB): chirpchatdemodsink.cpp/.h, chirpchatdemoddecoderlora.
  cpp/.h, chirpchatdemoddecoder.cpp, chirpchatdemodsettings.*.
- Bit conventions and frame synchronisation are ported from gr-lora_sdr
  (Joachim Tapparel et al., EPFL, https://github.com/tapparelj/gr-lora_sdr)
  at commit fbbf6ea6684d00363096929c7ef015cabcfaeb6d -- the same build that
  generated the reference vectors in tests/decoder/data/lora/. Files ported
  from, by stage:
    lib/frame_sync_impl.cc   preamble detection over consecutive up-chirps,
                             k_hat alignment, Bernier fractional CFO,
                             integer CFO from the down-chirps, network-id
                             check with its +-2 bin timing correction,
                             2.25 down-chirps, SFO tracking
    lib/fft_demod_impl.cc    symbol = ((bin - 1) mod 2**sf), divided by 4
                             for the header block and for LDRO
    lib/gray_mapping_impl.cc s ^ (s >> 1)
    lib/deinterleaver_impl.cc diagonal deinterleave, sf_app = sf-2 for the
                             first (header) block and for LDRO, 8-bit
                             codewords in the first block
    lib/hamming_dec_impl.cc  hard-decision Hamming decode
    lib/header_decoder_impl.cc header checksum, length/CR/CRC fields; the
                             first block's codewords after the 5 header
                             nibbles are payload nibbles
    lib/dewhitening_impl.cc  + lib/tables.h whitening sequence, payload
                             bytes only (the CRC bytes are not whitened)
    lib/crc_verif_impl.cc    CRC-16 over payload[:len-2] XOR the last two
                             payload bytes, compared with the 2 CRC bytes
  The TX side (whitening, add_crc, hamming_enc, interleaver, gray_demap,
  modulate, header) was read to confirm each RX convention's inverse and
  is mirrored only by tests/helpers/lora.py.

Where this module departs from a literal port of frame_sync it says so at
the point of use: it works on a buffered stream with batched FFTs instead
of a per-symbol state machine, averages the preamble instead of single
symbols where that costs nothing, and resolves the integer CFO/timing split
from the preamble and both SFD down-chirps together.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

# -- Gray code -----------------------------------------------------------------


def gray_encode(value: int) -> int:
    """Binary-reflected Gray code. gray_mapping_impl.cc applies exactly this
    (`in ^ (in >> 1)`) to the demodulated symbol on the receive side."""
    return value ^ (value >> 1)


def gray_decode(value: int) -> int:
    """Inverse of `gray_encode` (gray_demap_impl.cc's TX-side loop)."""
    value ^= value >> 1
    value ^= value >> 2
    value ^= value >> 4
    value ^= value >> 8
    value ^= value >> 16
    return value


# -- Diagonal deinterleaver ----------------------------------------------------
#
# Codewords are held LSB-first: bit 0 is the first bit gr-lora_sdr's int2bool
# yields (data bit d0), bits 4.. are parity. With that layout
# deinterleaver_impl.cc's `deinter_bin[(i - j - 1) mod sf_app][i] =
# inter_bin[i][j]` (inter_bin MSB-first) reduces to the formula below.


def deinterleave(block: list[int] | np.ndarray, sf_app: int, cr: int) -> list[int]:
    """One diagonal-interleaver block: `4+cr` Gray-mapped symbols (each
    < 2**sf_app) -> `sf_app` codewords of `4+cr` bits. The first block of a
    frame always uses cr=4 and sf_app=sf-2."""
    n_sym = 4 + cr
    if len(block) != n_sym:
        raise ValueError(f"deinterleave: block must hold {n_sym} symbols for cr={cr}")
    codewords = [0] * sf_app
    for k in range(n_sym):
        sym = int(block[k])
        for m in range(sf_app):
            codewords[(m + k) % sf_app] |= ((sym >> m) & 1) << k
    return codewords


def interleave(codewords: list[int], sf_app: int, cr: int) -> list[int]:
    """Inverse of `deinterleave` (interleaver_impl.cc, TX side); used by the
    test-suite's synthetic transmitter only."""
    n_sym = 4 + cr
    if len(codewords) != sf_app:
        raise ValueError(f"interleave: need {sf_app} codewords for sf_app={sf_app}")
    symbols = [0] * n_sym
    for m in range(sf_app):
        for k in range(n_sym):
            symbols[k] |= ((int(codewords[(m + k) % sf_app]) >> k) & 1) << m
    return symbols


# -- Hamming FEC ---------------------------------------------------------------
#
# hamming_enc_impl.cc: p0 = d0^d1^d2, p1 = d1^d2^d3, p2 = d0^d1^d3,
# p3 = d0^d2^d3 for CR 4/6..4/8 (keeping the first `cr` of them); CR 4/5
# carries a single overall parity bit d0^d1^d2^d3 instead.


def _parity_bits(d: int) -> tuple[int, int, int, int]:
    d0, d1, d2, d3 = d & 1, (d >> 1) & 1, (d >> 2) & 1, (d >> 3) & 1
    return (d0 ^ d1 ^ d2, d1 ^ d2 ^ d3, d0 ^ d1 ^ d3, d0 ^ d2 ^ d3)


def hamming_encode(nibble: int, cr: int) -> int:
    """TX-side encode (synthetic transmitter/tests): data in bits 0..3,
    parity in bits 4.."""
    if not 1 <= cr <= 4:
        raise ValueError("cr must be 1..4 (4/5 .. 4/8)")
    nibble &= 0xF
    if cr == 1:
        return nibble | (bin(nibble).count("1") & 1) << 4
    p = _parity_bits(nibble)
    codeword = nibble
    for i in range(cr):
        codeword |= p[i] << (4 + i)
    return codeword


# hamming_dec_impl.cc syndrome (s0 + 2*s1 + 4*s2) -> data bit to flip.
_SYNDROME_FLIP = {5: 0, 7: 1, 3: 2, 6: 3}


def hamming_decode(codeword: int, cr: int) -> tuple[int, bool]:
    """Hard-decision decode, as hamming_dec_impl.cc: -> (data_nibble, ok).

    4/7 corrects one error. 4/8 corrects one error only when the codeword
    weight is odd (an even weight with a non-zero syndrome is a detected
    double error, left uncorrected). 4/5 and 4/6 only detect. `ok` is False
    when an error was detected but not corrected."""
    if not 1 <= cr <= 4:
        raise ValueError("cr must be 1..4 (4/5 .. 4/8)")
    c = [(codeword >> i) & 1 for i in range(4 + cr)]
    data = codeword & 0xF
    if cr == 1:
        return data, sum(c) % 2 == 0
    s0 = c[0] ^ c[1] ^ c[2] ^ c[4]
    s1 = c[1] ^ c[2] ^ c[3] ^ c[5]
    if cr == 2:
        return data, not (s0 | s1)
    s2 = c[0] ^ c[1] ^ c[3] ^ c[6]
    syndrome = s0 | (s1 << 1) | (s2 << 2)
    if cr == 4 and sum(c) % 2 == 0:
        return data, syndrome == 0
    if syndrome in _SYNDROME_FLIP:
        data ^= 1 << _SYNDROME_FLIP[syndrome]
    return data, True


# -- Whitening ---------------------------------------------------------------
#
# gr-lora_sdr lib/tables.h `whitening_seq` (255 bytes). dewhitening_impl.cc
# XORs payload byte i with whitening_seq[i] (low nibble first); the two CRC
# bytes that follow are not whitened.
_WHITENING = bytes.fromhex(
    "fffefcf8f0e1c2850b172f5ebc78f1e3c68d1a3468d0a04080010204081123478e1c3871e2c48912"
    "254b972e5cb870e0c08103060c193264c992244993264d9b376edcb972e4c890204182050a152b56"
    "ad5bb66ddab56bd6ac59b265cb962c58b061c3870f1f3e7dfbf6eddbb76fdebd7af5ebd7ae5dba74"
    "e8d1a24488102143860d1b366cd8b163c78f1e3c79f3e7ce9c3973e6cc983162c58b162d5ab469d2"
    "a4489122458a142952a54a952a54a953a74e9d3b77eeddbb76ecd9b367cf9e3d7bf7efdfbf7efdfa"
    "f4e9d3a64c993366cd9a356ad4a851a3468c183060c183070e1d3a75ead5aa55ab57af5fbe7cf9f2"
    "e5ca942850a142840913274f9f3f7f"
)


def dewhiten(data: bytes) -> bytes:
    """XOR `data` (payload bytes only) with the LoRa whitening sequence.
    Symmetric: also whitens at TX (whitening_impl.cc)."""
    if len(data) > len(_WHITENING):
        raise ValueError(f"dewhiten: whitening sequence has {len(_WHITENING)} bytes")
    return bytes(b ^ w for b, w in zip(data, _WHITENING, strict=False))


# -- Header -----------------------------------------------------------------


def _header_checksum(length_byte: int, cr_crc_nibble: int) -> int:
    """5-bit explicit-header checksum, header_decoder_impl.cc / header_impl.cc."""
    a, b, c = (length_byte >> 4) & 0xF, length_byte & 0xF, cr_crc_nibble & 0xF

    def bit(v: int, i: int) -> int:
        return (v >> i) & 1

    c4 = bit(a, 3) ^ bit(a, 2) ^ bit(a, 1) ^ bit(a, 0)
    c3 = bit(a, 3) ^ bit(b, 3) ^ bit(b, 2) ^ bit(b, 1) ^ bit(c, 0)
    c2 = bit(a, 2) ^ bit(b, 3) ^ bit(b, 0) ^ bit(c, 3) ^ bit(c, 1)
    c1 = bit(a, 1) ^ bit(b, 2) ^ bit(b, 0) ^ bit(c, 2) ^ bit(c, 1) ^ bit(c, 0)
    c0 = bit(a, 0) ^ bit(b, 1) ^ bit(c, 3) ^ bit(c, 2) ^ bit(c, 1) ^ bit(c, 0)
    return (c4 << 4) | (c3 << 3) | (c2 << 2) | (c1 << 1) | c0


def header_decode(nibbles: list[int]) -> dict | None:
    """Explicit header: 5 post-Hamming nibbles -> length/CR/CRC fields, or
    None if the checksum fails. As header_decoder_impl.cc, the checksum is
    `((n3 & 1) << 4) + n4`. (A zero length is rejected by the receiver,
    not here.)"""
    if len(nibbles) != 5:
        raise ValueError("header_decode needs exactly 5 nibbles")
    n0, n1, n2, n3, n4 = (int(v) for v in nibbles)
    length = (n0 << 4) | n1
    checksum = ((n3 & 1) << 4) | n4
    if _header_checksum(length, n2) != checksum:
        return None
    return {"length": length, "cr": n2 >> 1, "has_crc": bool(n2 & 1), "checksum": checksum}


def header_encode(length: int, cr: int, has_crc: bool) -> list[int]:
    """TX-side encode (header_impl.cc): fields -> 5 pre-Hamming nibbles."""
    n2 = ((cr & 0x7) << 1) | int(has_crc)
    checksum = _header_checksum(length & 0xFF, n2)
    return [(length >> 4) & 0xF, length & 0xF, n2, checksum >> 4, checksum & 0xF]


# -- CRC-16 -------------------------------------------------------------------


def _crc16_ccitt(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def payload_crc16(payload: bytes) -> int:
    """LoRa payload CRC as crc_verif_impl.cc / add_crc_impl.cc compute it:
    CRC-16 (poly 0x1021, init 0) over payload[:len-2], then XOR
    payload[len-1] and (payload[len-2] << 8). Transmitted after the
    payload as 2 bytes, low byte first. Undefined below 2 bytes (gr-lora_sdr
    reads out of bounds on TX and emits nothing on RX)."""
    if len(payload) < 2:
        raise ValueError("LoRa payload CRC is undefined for payloads shorter than 2 bytes")
    crc = _crc16_ccitt(payload[:-2])
    return (crc ^ payload[-1] ^ (payload[-2] << 8)) & 0xFFFF


# -- Parameters / packet ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LoRaParams:
    sf: int
    bw: int
    sync_word: int = 0x12
    ldro: str = "auto"  # "auto" | "on" | "off"
    implicit_header: bool = False
    implicit_length: int | None = None
    implicit_cr: int | None = None
    implicit_crc: bool | None = None
    # RF centre frequency of the channel. When set, sample-clock drift is
    # tracked from the measured CFO (frame_sync_impl.cc sfo_hat = CFO / f_c,
    # which assumes LO and sample clock share one reference on each side).
    center_freq_hz: float | None = None

    def __post_init__(self) -> None:
        if not 7 <= self.sf <= 12:
            raise ValueError("LoRa spreading factor must be 7..12")
        if self.ldro not in ("auto", "on", "off"):
            raise ValueError('ldro must be "auto", "on", or "off"')
        if self.implicit_header and (
            self.implicit_length is None or self.implicit_cr is None or self.implicit_crc is None
        ):
            raise ValueError("implicit_header needs implicit_length/implicit_cr/implicit_crc")

    @property
    def ldro_enabled(self) -> bool:
        """frame_sync_impl.cc AUTO rule: symbol time 2**sf / bw > 16 ms."""
        if self.ldro == "on":
            return True
        if self.ldro == "off":
            return False
        return (2**self.sf / self.bw) > 16e-3


@dataclasses.dataclass(frozen=True)
class LoRaPacket:
    payload: bytes
    crc_valid: bool | None  # None: frame carried no CRC (has_crc=False)
    header: dict | None  # None for an implicit-header frame
    cr: int
    sf: int
    cfo_hz: float
    snr_db: float | None
    sample_index: int


# -- Receiver ------------------------------------------------------------------

_N_UP_REQ = 5  # frame_sync: preamble_len (8) - 3 consecutive matching up-chirps
_MAX_PREAMBLE_SYMS = 256  # stop walking a "preamble" after this many symbols
_WALK_CHUNK = 8  # symbols requested at a time while walking a long preamble
# frame_sync accepts consecutive detection bins within +-1. Detection windows
# are not yet symbol-aligned and the sample phase is not yet chosen, so each
# window holds a chirp fold with a phase step of 2*pi*(sub-chip timing); near
# half a chip that splits the peak in two and consecutive argmaxes can land 2
# bins apart (measured: ~1.5% of SF7 frames missed at 0 dB with +-1).
_DETECT_TOL = 2


def _ref_upchirp(sf: int) -> np.ndarray:
    """utilities.h build_upchirp(id=0) at 1 sample/chip."""
    n = np.arange(1 << sf, dtype=np.float64)
    return np.exp(2j * np.pi * (n * n / (2 * (1 << sf)) - 0.5 * n))


class _Incomplete(Exception):
    """More samples are needed; `need` is the absolute sample index."""

    def __init__(self, need: int):
        self.need = need


class _Reject(Exception):
    """Not a frame (or not ours); resume detection at absolute `resume`."""

    def __init__(self, resume: int):
        self.resume = resume


class LoRaReceiver:
    """Receive-only LoRa demodulator.

    `feed(samples)` takes complex IQ at `os_factor * params.bw` samples/s and
    returns the packets completed by this call; packets spanning feed calls
    complete later, and the buffer is bounded by the longest possible frame.

    As in gr-lora_sdr's frame_sync, symbols are demodulated from one sample
    per chip, picked at the sample phase that the fractional timing
    estimate selects. At `os_factor=1` that choice does not exist and a
    fractional timing offset near half a chip costs most of the link
    margin (the chirp's frequency fold then puts a phase step inside the
    symbol), so callers that resample (LoRaBackend) use `os_factor=4` and
    low-pass the input to the LoRa bandwidth first: picking samples does
    not filter, so wider-band noise would alias in.
    """

    def __init__(self, params: LoRaParams, os_factor: int = 1):
        if os_factor < 1:
            raise ValueError("os_factor must be >= 1")
        self.params = params
        self.sf = params.sf
        self.bw = params.bw
        self.os = os = int(os_factor)
        self.n = n = 1 << params.sf
        self.ns = n * os  # input samples per symbol
        up = _ref_upchirp(params.sf)
        self._up = up.astype(np.complex64)
        self._down = np.conj(up).astype(np.complex64)
        self._ramp = np.arange(n, dtype=np.float64) / n
        self._ar = np.arange(n, dtype=np.int64) * os
        sw = params.sync_word & 0xFF
        # frame_sync_impl.cc: a one-byte sync word becomes two up-chirps of
        # id (nibble << 3), independent of SF.
        self._net_ids = ((sw >> 4) << 3, (sw & 0xF) << 3)
        self._buf = np.zeros(0, dtype=np.complex64)
        self._base = 0  # absolute index of _buf[0]
        self._scan = 0  # absolute start of the next detection window
        self._det_bins = np.zeros(0, dtype=np.int64)  # bins of windows before _scan
        self._cand: dict | None = None
        self._need = 0
        # Longest frame: preamble cap + sync/SFD + 8 header symbols + 255
        # bytes and CRC at CR 4/8 with LDRO.
        max_payload_syms = math.ceil((2 * 255 + 4) / (self.sf - 2)) * 8
        self._max_frame = (_MAX_PREAMBLE_SYMS + 16 + max_payload_syms) * self.ns

    # -- public ---------------------------------------------------------------

    def feed(self, samples: np.ndarray) -> list[LoRaPacket]:
        samples = np.asarray(samples)
        if samples.size:
            chunk = samples.astype(np.complex64, copy=False)
            self._buf = np.concatenate([self._buf, chunk]) if self._buf.size else chunk.copy()
        packets: list[LoRaPacket] = []
        end = self._base + self._buf.size
        while True:
            if self._cand is None and not self._detect():
                break
            if end < self._need:
                break
            try:
                packets.append(self._decode_candidate())
            except _Incomplete as exc:
                self._need = exc.need
                if exc.need - self._cand["w0"] > self._max_frame:
                    self._reset_to(self._cand["w0"] + self.ns)
                    continue
                break
            except _Reject as exc:
                self._reset_to(exc.resume)
        self._trim()
        return packets

    # -- streaming bookkeeping ------------------------------------------------

    def _reset_to(self, resume: int) -> None:
        self._cand = None
        self._need = 0
        self._scan = max(resume, self._base)
        self._det_bins = np.zeros(0, dtype=np.int64)

    def _trim(self) -> None:
        # Keep the windows whose bins are carried in _det_bins: a detection
        # completed by the next feed starts there.
        if self._cand is None:
            keep_from = self._scan - (self._det_bins.size + 1) * self.ns
        else:
            keep_from = self._cand["w0"] - self.os
        drop = keep_from - self._base
        if drop > 0:
            self._buf = self._buf[drop:]
            self._base += drop

    def _spectra(self, starts: np.ndarray, ref: np.ndarray, nfft: int | None = None) -> np.ndarray:
        """Complex spectra of one-sample-per-chip windows starting at the
        absolute sample indices `starts`, each multiplied by `ref` (one
        batched FFT)."""
        idx = (np.asarray(starts, dtype=np.int64) - self._base)[:, None] + self._ar
        return np.fft.fft(self._buf[idx] * ref, n=nfft, axis=1)

    def _windows(self, starts: np.ndarray, ref: np.ndarray) -> np.ndarray:
        spec = self._spectra(starts, ref)
        return spec.real**2 + spec.imag**2

    def _have(self, start_abs: int) -> None:
        """Raise _Incomplete unless the symbol starting at `start_abs` is
        fully buffered."""
        end = start_abs + (self.n - 1) * self.os + 1
        if end > self._base + self._buf.size:
            raise _Incomplete(end)

    # -- DETECT ---------------------------------------------------------------

    def _detect(self) -> bool:
        """frame_sync_impl.cc DETECT: dechirp consecutive symbol-long windows
        and look for _N_UP_REQ of them whose peak bins differ by at most
        _DETECT_TOL from one window to the next (batched over every whole window
        available). Sample phase os/2 within each chip, as frame_sync."""
        n, ns = self.n, self.ns
        k = (self._base + self._buf.size - self._scan) // ns
        if k <= 0:
            return False
        starts = self._scan + self.os // 2 + ns * np.arange(k)
        power = self._windows(starts, self._down)
        bins = np.argmax(power, axis=1).astype(np.int64)
        bins[power.sum(axis=1) <= 0] = -1  # gr's get_symbol_val: no energy -> -1
        prev = self._det_bins
        all_bins = np.concatenate([prev, bins])
        first = int(starts[0]) - prev.size * ns
        d = np.abs(all_bins[1:] - all_bins[:-1]) % n
        close = (np.minimum(d, n - d) <= _DETECT_TOL) & (all_bins[1:] >= 0) & (all_bins[:-1] >= 0)
        need = _N_UP_REQ - 1
        if close.size >= need:
            run = np.convolve(close.astype(np.int64), np.ones(need, dtype=np.int64), "valid")
            hits = np.flatnonzero(run == need)
            if hits.size:
                i = int(hits[0])
                self._cand = {"w0": first + i * ns, "bins": all_bins[i : i + _N_UP_REQ]}
                self._need = 0
                self._det_bins = np.zeros(0, dtype=np.int64)
                return True
        self._det_bins = all_bins[-need:] if all_bins.size > need else all_bins
        self._scan += k * ns
        return False

    # -- SYNC / header / payload ---------------------------------------------

    def _cfo_ref(self, cfo_bins: float, chirp: np.ndarray) -> np.ndarray:
        return (chirp * np.exp(-2j * np.pi * cfo_bins * self._ramp)).astype(np.complex64)

    def _sto_frac(self, starts: np.ndarray, ref: np.ndarray) -> float:
        """frame_sync_impl.cc estimate_STO_frac: 2N-point zero-padded
        spectra of the (fractional-CFO-corrected) preamble up-chirps, summed
        in power, peak refined with Cui Yang's interpolator (eq. 15). The
        fractional part of the peak is the fractional timing offset in
        chips."""
        n = self.n
        spec = self._spectra(starts, ref, nfft=2 * n)
        power = (spec.real**2 + spec.imag**2).sum(axis=0)
        k0 = int(np.argmax(power))
        y_1, y0, y1 = power[(k0 - 1) % (2 * n)], power[k0], power[(k0 + 1) % (2 * n)]
        u = 64 * n / 406.5506497
        v = u * 2.4674
        wa = (y1 - y_1) / (u * (y1 + y_1) + v * y0)
        ka = wa * n / math.pi
        k_residual = ((k0 + ka) / 2) % 1.0
        return float(k_residual - (1.0 if k_residual > 0.5 else 0.0))

    def _sync(self, cand: dict) -> None:
        """frame_sync_impl.cc SYNC, on the buffered stream."""
        n, ns, os = self.n, self.ns, self.os
        w0 = cand["w0"]
        if "cfo_frac" not in cand:
            # k_hat: most frequent preamble bin; the up-chirp boundary (offset
            # by the integer CFO, resolved below) is then N - k_hat chips
            # into a detection window.
            k_hat = int(np.bincount(cand["bins"] % n, minlength=n).argmax())
            g1 = w0 + ns - k_hat * os
            starts = g1 + ns * np.arange(_N_UP_REQ - 1)
            self._have(int(starts[-1]))
            # Bernier fractional CFO (estimate_CFO_frac_Bernier): phase
            # advance of the strongest bin between consecutive up-chirps.
            spec = self._spectra(starts, self._down)
            power = spec.real**2 + spec.imag**2
            row = int(np.argmax(power.max(axis=1)))
            k0 = int(np.argmax(power[row]))
            four_cum = np.sum(spec[:-1, k0] * np.conj(spec[1:, k0]))
            cfo_frac = float(-np.angle(four_cum) / (2 * np.pi))
            # Fractional STO picks the sample phase (frame_sync's
            # in_down[ii] = in[os/2 + os*ii - round(sto_frac*os)]).
            sto = self._sto_frac(starts, self._cfo_ref(cfo_frac, self._down))
            cand.update(cfo_frac=cfo_frac, g1=g1 - round(sto * os))
        g1, cfo_frac = cand["g1"], cand["cfo_frac"]
        down_frac = self._cfo_ref(cfo_frac, self._down)

        # Walk the aligned grid to the first symbol that is not a preamble
        # up-chirp (frame_sync's NET_ID1 test: bin outside {N-1, 0, 1}),
        # which also absorbs preambles longer than 8 (Meshtastic sends 16).
        m = cand.get("walk", _N_UP_REQ - 1)
        while True:
            if m > _MAX_PREAMBLE_SYMS:
                raise _Reject(g1 + m * ns)
            avail = (self._base + self._buf.size - g1 - (n - 1) * os - 1) // ns + 1
            stop = min(m + _WALK_CHUNK, avail)
            if stop <= m:
                raise _Incomplete(g1 + (m + _WALK_CHUNK) * ns)
            bins = np.argmax(self._windows(g1 + ns * np.arange(m, stop), down_frac), axis=1)
            outside = np.flatnonzero(np.minimum(bins, n - bins) > 1)
            if outside.size:
                m1 = m + int(outside[0])
                break
            m = stop
            cand["walk"] = m
        pre_starts = g1 + ns * np.arange(0, m1)
        if os > 1:
            # Refine the sample phase over the whole preamble (frame_sync
            # re-estimates it once CFO is known, for the same reason).
            g1 -= round(self._sto_frac(pre_starts, down_frac) * os)
            pre_starts = g1 + ns * np.arange(0, m1)
        pre_power = self._windows(pre_starts, down_frac)
        u_raw = int(np.bincount(np.argmax(pre_power, axis=1), minlength=n).argmax())
        u = u_raw - n if u_raw > n // 2 else u_raw
        # Both SFD down-chirps dechirped with the up-chirp (frame_sync uses
        # DOWNCHIRP2 only; summing both costs nothing here).
        self._have(g1 + (m1 + 4) * ns + (n // 4) * os)
        dn_power = self._windows(
            g1 + ns * np.arange(m1 + 2, m1 + 4), self._cfo_ref(cfo_frac, self._up)
        )
        dn = int(np.argmax(dn_power.sum(axis=0)))
        dn = dn - n if dn >= n // 2 else dn
        # An up-chirp read d chips late with c bins of CFO reads d + c; a
        # down-chirp reads c - d. frame_sync assumes u == 0 and takes
        # floor(dn / 2); using the measured u keeps the split exact.
        cfo_int = (u + dn) // 2
        delta = u - cfo_int
        t_net = g1 + m1 * ns - delta * os
        cfo = cfo_int + cfo_frac
        ref = self._cfo_ref(cfo, self._down)
        net = np.argmax(self._windows(t_net + ns * np.arange(-1, 2), ref), axis=1)
        net_off = self._check_net_ids(int(net[1]), int(net[2]), int(net[0]))
        if net_off is None:
            raise _Reject(g1 + (m1 + 1) * ns)
        shift, sym_off = net_off
        # frame_sync: consume -net_id_off samples (chips there) as a timing
        # correction; a one-symbol-late NET_ID1 moves back one symbol.
        t_net -= shift * os + sym_off * ns
        # Preamble symbols before g1 still in the buffer, only to report
        # where the frame starts.
        back = (g1 - self._base) // ns
        lead = 0
        if back > 0:
            pw = self._windows(g1 - ns * np.arange(1, back + 1), down_frac)
            d = np.abs(np.argmax(pw, axis=1) - u_raw) % n
            far = np.flatnonzero((np.minimum(d, n - d) > 1) | (pw.sum(axis=1) <= 0))
            lead = int(far[0]) if far.size else int(back)
        cand.update(
            t_net=t_net,
            pre_start=t_net - (m1 + lead) * ns - os // 2,
            cfo=cfo,
            snr_db=self._snr_db(pre_power),
            ref=ref,
            header_start=t_net + 4 * ns + (n // 4) * os,
        )

    def _check_net_ids(self, id1: int, id2: int, prev: int) -> tuple[int, int] | None:
        """frame_sync_impl.cc network-identifier test. Returns (timing
        shift in chips, symbol offset) or None when the sync word does not
        match."""
        n = self.n
        s0, s1 = self._net_ids
        if s0 == 0:
            return 0, 0
        if abs(id1 - s0) > 2:
            # One symbol late: this is NET_ID2 and the previous one NET_ID1.
            if abs(id1 - s1) <= 2:
                off = id1 - s1
                if prev + off == s0:
                    return off, 1
            return None
        off = id1 - s0
        if s1 != 0 and (id2 - off) % n != s1:
            return None
        return off, 0

    def _snr_db(self, power: np.ndarray) -> float | None:
        """SNR in the LoRa bandwidth from dechirped preamble spectra
        (frame_sync determine_snr's peak-vs-rest, rescaled to signal/noise
        power per chip)."""
        if power.size == 0:
            return None
        peak = power.max(axis=1)
        noise_bin = (power.sum(axis=1) - peak) / (self.n - 1)
        sig = float(np.mean(np.maximum(peak - noise_bin, 0.0)))
        noise = float(np.mean(noise_bin)) * self.n
        if sig <= 0 or noise <= 0:
            return None
        return round(10 * math.log10(sig / noise), 1)

    def _symbol_starts(self, cand: dict, first: int, count: int) -> np.ndarray:
        """Absolute starts of header/payload symbols first..first+count-1,
        with frame_sync's SFO compensation (one sample dropped or repeated
        whenever the accumulated drift passes half a sample)."""
        k = np.arange(first, first + count, dtype=np.int64)
        starts = cand["header_start"] + k * self.ns
        sfo = cand.get("sfo", 0.0)
        if sfo:
            # Drift accumulates from the network-id symbols where timing was
            # measured, 4.25 symbols before the header.
            starts -= np.rint((k + 4.25) * sfo * self.os).astype(np.int64)
        return starts

    def _demod(self, cand: dict, starts: np.ndarray, reduced: bool) -> list[int]:
        """fft_demod_impl.cc + gray_mapping_impl.cc for a batch of symbols."""
        n = self.n
        self._have(int(starts[-1]))
        bins = np.argmax(self._windows(starts, cand["ref"]), axis=1)
        vals = (bins - 1) % n
        if reduced:
            vals //= 4
        return [int(v) ^ (int(v) >> 1) for v in vals]

    def _decode_candidate(self) -> LoRaPacket:
        cand = self._cand
        assert cand is not None
        sf, p = self.sf, self.params
        if "t_net" not in cand:
            self._sync(cand)
            if p.center_freq_hz:
                # frame_sync: sfo_hat = (cfo_int + cfo_frac) * bw / f_c, chips
                # of drift per symbol.
                cand["sfo"] = cand["cfo"] * self.bw / float(p.center_freq_hz)
        header_start = cand["header_start"]

        if "first_block" not in cand:
            # First block: 8 symbols at reduced rate, sf-2 codewords at 4/8.
            syms = self._demod(cand, self._symbol_starts(cand, 0, 8), reduced=True)
            nibbles = [hamming_decode(cw, 4)[0] for cw in deinterleave(syms, sf - 2, 4)]
            if p.implicit_header:
                header = None
                length, cr, has_crc = p.implicit_length, p.implicit_cr, p.implicit_crc
                payload_nibbles = nibbles
            else:
                header = header_decode(nibbles[:5])
                if header is None or header["length"] == 0 or not 1 <= header["cr"] <= 4:
                    raise _Reject(header_start)
                length, cr, has_crc = header["length"], header["cr"], header["has_crc"]
                # header_decoder_impl.cc: codewords after the 5 header
                # nibbles are the first payload nibbles.
                payload_nibbles = nibbles[5:]
            cand["first_block"] = (header, int(length), int(cr), bool(has_crc), payload_nibbles)
        header, length, cr, has_crc, nibbles = cand["first_block"]

        ldro = p.ldro_enabled
        sf_app = sf - 2 if ldro else sf
        total = 2 * length + (4 if has_crc else 0)
        n_blocks = math.ceil(max(total - len(nibbles), 0) / sf_app)
        cw_len = 4 + cr
        n_syms = n_blocks * cw_len
        nibbles = list(nibbles)
        if n_syms:
            syms = self._demod(cand, self._symbol_starts(cand, 8, n_syms), reduced=ldro)
            for b in range(n_blocks):
                for cw in deinterleave(syms[b * cw_len : (b + 1) * cw_len], sf_app, cr):
                    nibbles.append(hamming_decode(cw, cr)[0])
        nibbles = nibbles[:total]
        raw = bytes(nibbles[2 * i] | (nibbles[2 * i + 1] << 4) for i in range(total // 2))
        # dewhitening_impl.cc: payload bytes only; crc_verif_impl.cc then
        # compares the CRC of the dewhitened payload with the raw CRC bytes.
        payload = dewhiten(raw[:length])
        crc_valid: bool | None = None
        if has_crc:
            tx_crc = raw[length] | (raw[length + 1] << 8)
            crc_valid = length >= 2 and payload_crc16(payload) == tx_crc
        last = int(self._symbol_starts(cand, 8 + n_syms - 1, 1)[0])
        packet = LoRaPacket(
            payload=payload,
            crc_valid=crc_valid,
            header=header,
            cr=cr,
            sf=sf,
            cfo_hz=cand["cfo"] * self.bw / self.n,
            snr_db=cand["snr_db"],
            sample_index=int(cand["pre_start"]),
        )
        self._reset_to(last + self.ns)
        return packet
