# SPDX-License-Identifier: GPL-3.0-or-later
"""Native ADS-B / Mode S demodulator and message decoder (no dump1090).

Provenance: preamble chip correlation, PPM bit-slicing and the CRC-24/ICAO
address-recovery scheme are ported from SDRangel's ADS-B demodulator,
`plugins/channelrx/demodadsb/adsbdemodsinkworker.cpp` and `adsb.h` at commit
030963609f7a667c62259dbcf3cee7522880885c (GPL-3.0-or-later, Copyright (C)
2020 Jon Beniston, M7RCE): the preamble is correlated as the ratio of power
summed at the four "one" chip positions (0, 2, 7, 9) over power summed at
the twelve "zero" chip positions, each message bit is sliced by comparing
the power of its two PPM chips, and -- the error-correction policy this
ports -- extended squitter (DF17/18) frames are accepted only on an *exact*
CRC-24 match (`adsbdemodsinkworker.cpp` lines ~427-458: `if (parity ==
m_crc.get())`), with no bit-flip correction attempted. SDRangel does not
implement dump1090's optional brute-force single-bit CRC correction for
ADS-B, so neither does this port: a corrupted frame is simply rejected.
Other Mode S downlink formats recover the ICAO address by XORing the
parity field with the computed CRC remainder (same file, lines ~516-567);
this is address recovery, not error correction, and is likewise exact-match
only. `adsbdemodsink.cpp` confirms the correlator and slicer operate on
power (`magsq = I^2+Q^2`), not amplitude or dB.

The CRC-24 generator polynomial (0xFFF409) and the per-message-type field
decode (identification 6-bit charset, airborne position CPR, airborne
velocity) are not SDRangel-specific -- they follow the public ICAO Annex 10
Volume IV Mode S specification, as documented in "The 1090 MHz Riddle" by
Junzi Sun (mode-s.org, MIT-licensed) and the same author's pyModeS. Formulas
were checked against that book's own worked examples (see
tests/decoder/test_adsb_native.py).

Scope: DF17/18 (ADS-B extended squitter: identification, airborne position,
airborne velocity) and DF11 (all-call reply: ICAO address only). Other Mode
S downlink formats (TCAS RA, Comm-A/B, Mode S altitude/identity replies,
Gillham-coded altitude) are not decoded.

Input: complex64 IQ at exactly 2,000,000 samples/second (2 chips/bit, 1
sample/chip -- `adsb.h`'s ADS_B_CHIPS_PER_BIT=2 at the native rate). The
backend wrapper (adsb.py) resamples msdr's IQ stream to this rate first,
the same role `_StreamingIQResampler` plays for dump1090's 2.4 MHz input in
standalone_adsb.py.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

_SAMPLE_RATE = 2_000_000
_PREAMBLE_CHIPS = 16
_PREAMBLE_ONES_CHIPS = (0, 2, 7, 9)
_PREAMBLE_ZEROS_CHIPS = (1, 3, 4, 5, 6, 8, 10, 11, 12, 13, 14, 15)
_BITS_LONG = 112  # DF 17/18 extended squitter
_BITS_SHORT = 56  # DF 11 all-call reply
_CRC24_POLY = 0xFFF409
_DEFAULT_THRESHOLD = 2.0  # preambleCorrelation ratio (SDRangel's is dB; this is linear)

_CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def crc24_remainder(data: bytes) -> int:
    """CRC-24 remainder of `data`, as if 24 zero bits were appended.

    This is the value a correctly-formed Mode S/ADS-B frame's trailing
    24-bit parity field holds (ICAO Annex 10 Vol IV; generator polynomial
    0xFFF409, i.e. x^24+x^23+...+x^12+x^10+x^3+1).
    """
    reg = 0
    for byte in data:
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            bit = (byte >> shift) & 1
            msb = (reg >> 23) & 1
            reg = ((reg << 1) & 0xFFFFFF) | bit
            if msb:
                reg ^= _CRC24_POLY
    for _ in range(24):
        msb = (reg >> 23) & 1
        reg = (reg << 1) & 0xFFFFFF
        if msb:
            reg ^= _CRC24_POLY
    return reg


def _crc24_batch(bits: np.ndarray) -> np.ndarray:
    """Vectorized crc24_remainder: bits is (n, k) of MSB-first 0/1 values;
    returns (n,) int64 remainders, one per row, identical to calling
    crc24_remainder on each row's packed bytes.
    """
    n = bits.shape[0]
    k = bits.shape[1]
    reg = np.zeros(n, dtype=np.int64)
    for idx in range(k + 24):
        bit = bits[:, idx].astype(np.int64) if idx < k else 0
        msb = (reg >> 23) & 1
        reg = ((reg << 1) & 0xFFFFFF) | bit
        reg = np.where(msb == 1, reg ^ _CRC24_POLY, reg)
    return reg


def _bits_to_int(bits) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


@dataclasses.dataclass
class AdsbMessage:
    df: int
    icao: int
    sample_index: int
    preamble_correlation: float
    crc_ok: bool
    fields: dict  # SBS-like keys: callsign, altitude_ft, latitude, longitude, ...


def _pack_bytes(bits: np.ndarray) -> np.ndarray:
    """bits: (n, k) bool/0-1 array, k a multiple of 8, MSB-first per byte."""
    n, k = bits.shape
    nbytes = k // 8
    reshaped = bits[:, : nbytes * 8].reshape(n, nbytes, 8).astype(np.uint8)
    weights = (1 << np.arange(7, -1, -1)).astype(np.uint8)
    return (reshaped * weights).sum(axis=2).astype(np.uint8)


def _boxcar(power: np.ndarray, width: int) -> np.ndarray:
    """boxcar[k] = sum(power[k : k + width]) for every valid k -- the energy
    SDRangel's correlator/slicer sums "for (i = 0; i < samplesPerChip; i++)"
    per chip, vectorized. width=1 (native 2 MS/s, 1 sample/chip) is just
    `power` itself.
    """
    if width == 1:
        return power
    # A strong burst can make subsequent weak samples smaller than one
    # float32 accumulator ULP. Preserve their energy before differencing.
    c = np.cumsum(np.insert(power, 0, 0.0), dtype=np.float64)
    return c[width:] - c[:-width]


def _find_candidates(
    power: np.ndarray, threshold: float, chip_width: int = 1
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized preamble correlation over the whole buffer. Returns
    (candidate start indices, their preamble correlation ratio), sorted by
    start index ascending. A candidate start index is one sample position
    before the first preamble chip, matching SDRangel's `startIdx`.
    `chip_width` samples/chip (SDRangel's `samplesPerChip`): 1 at the
    native 2 MS/s rate; >1 when the backend oversamples instead of
    decimating to exactly 1 sample/chip (see adsb.py), which -- like
    SDRangel summing over `samplesPerChip` real samples -- recovers the
    chip-phase alignment a plain decimation to 2 MS/s can land on badly.
    """
    boxcar = _boxcar(power, chip_width)
    margin = (_PREAMBLE_CHIPS + _BITS_LONG * 2) * chip_width
    # Candidate starts are chip-start sample indices; the last chip of the
    # longest supported frame must still fit within `power`.
    span = len(power) - margin
    if span <= 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
    ones = np.zeros(span, dtype=np.float32)
    zeros = np.zeros(span, dtype=np.float32)
    for c in _PREAMBLE_ONES_CHIPS:
        off = c * chip_width
        ones += boxcar[off : off + span]
    for c in _PREAMBLE_ZEROS_CHIPS:
        off = c * chip_width
        zeros += boxcar[off : off + span]
    with np.errstate(divide="ignore", invalid="ignore"):
        # zeros == 0 only happens with no noise floor at all (never true of
        # real ADC samples); SDRangel's C++ leaves the ratio at 0 in that
        # case, which would wrongly reject a perfect noiseless preamble, so
        # treat it as an unambiguous match instead.
        ratio = np.where(zeros > 0, ones / zeros, np.where(ones > 0, np.inf, 0.0)).astype(
            np.float32
        )
    mask = (ratio > threshold) & (ones > 0)
    idx = np.nonzero(mask)[0]
    return idx, ratio[idx]


def _demod_bits(
    power: np.ndarray, starts: np.ndarray, nbits: int, chip_width: int = 1
) -> np.ndarray:
    boxcar = _boxcar(power, chip_width)
    bit_idx = np.arange(nbits)
    one_pos = starts[:, None] + (_PREAMBLE_CHIPS + 2 * bit_idx[None, :]) * chip_width
    zero_pos = one_pos + chip_width
    return boxcar[one_pos] > boxcar[zero_pos]


def _icao_valid(icao: int) -> bool:
    return 0 < icao < 0xFFFFFF


def decode_identification(me: np.ndarray) -> dict:
    """me: 56-bit array (TC 1-4 identification/category)."""
    category = _bits_to_int(me[5:8])
    chars = []
    for i in range(8):
        chars.append(_CHARSET[_bits_to_int(me[8 + 6 * i : 14 + 6 * i])])
    callsign = "".join(chars).rstrip(" #")
    return {"callsign": callsign, "flight_id": callsign, "category": category}


def _decode_alt12(bits) -> float | None:
    """12-bit AC altitude field used in airborne position messages. Q=1
    (metric-step binary coding, standard for modern transponders) only;
    Q=0 (legacy Gillham/Gray code) is not decoded.
    """
    q = int(bits[7])
    if q != 1:
        return None
    n = _bits_to_int(list(bits[0:7]) + list(bits[8:12]))
    return float(n * 25 - 1000)


def cpr_nl(lat: float) -> int:
    """Number of longitude zones at latitude `lat` (CPR NL function)."""
    if lat == 0:
        return 59
    if abs(lat) == 87.0:
        return 2
    if abs(lat) > 87.0:
        return 1
    nz = 15
    a = 1 - math.cos(math.pi / (2 * nz))
    b = math.cos(math.radians(abs(lat))) ** 2
    return math.floor(2 * math.pi / math.acos(1 - a / b))


def cpr_resolve_global(
    lat_cpr_even: int, lon_cpr_even: int, lat_cpr_odd: int, lon_cpr_odd: int, odd_is_latest: bool
) -> tuple[float, float] | None:
    """Global CPR position decode from one even + one odd frame (ICAO Annex
    10 Vol IV / "The 1090 MHz Riddle" ch. 3). Returns None if the pair
    straddles a latitude-zone boundary (NL mismatch) and cannot be resolved.
    """
    lat_e = lat_cpr_even / 131072.0
    lon_e = lon_cpr_even / 131072.0
    lat_o = lat_cpr_odd / 131072.0
    lon_o = lon_cpr_odd / 131072.0
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    lat_even = (360.0 / 60.0) * ((j % 60) + lat_e)
    lat_odd = (360.0 / 59.0) * ((j % 59) + lat_o)
    if lat_even >= 270:
        lat_even -= 360
    if lat_odd >= 270:
        lat_odd -= 360
    nl_even = cpr_nl(lat_even)
    nl_odd = cpr_nl(lat_odd)
    if nl_even != nl_odd:
        return None
    if odd_is_latest:
        lat = lat_odd
        ni = max(nl_even - 1, 1)
        m = math.floor(lon_e * (nl_even - 1) - lon_o * nl_even + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_o)
    else:
        lat = lat_even
        ni = max(nl_even, 1)
        m = math.floor(lon_e * (nl_even - 1) - lon_o * nl_even + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_e)
    if lon > 180:
        lon -= 360
    return lat, lon


def decode_airborne_position(me: np.ndarray) -> dict:
    """me: 56-bit array (TC 9-18, barometric-altitude airborne position)."""
    alt_bits = me[8:20]
    f_odd = int(me[21])
    lat_cpr = _bits_to_int(me[22:39])
    lon_cpr = _bits_to_int(me[39:56])
    fields: dict = {"cpr_odd": bool(f_odd), "cpr_lat": lat_cpr, "cpr_lon": lon_cpr}
    altitude_ft = _decode_alt12(alt_bits)
    if altitude_ft is not None:
        fields["altitude_ft"] = altitude_ft
    return fields


def decode_airborne_velocity(me: np.ndarray) -> dict:
    """me: 56-bit array (TC 19, airborne velocity)."""
    subtype = _bits_to_int(me[5:8])
    fields: dict = {}
    if subtype in (1, 2):
        scale = 4 if subtype == 2 else 1
        s_ew = int(me[13])
        v_ew = _bits_to_int(me[14:24])
        s_ns = int(me[24])
        v_ns = _bits_to_int(me[25:35])
        ew = (v_ew - 1) * scale * (-1 if s_ew else 1)
        ns = (v_ns - 1) * scale * (-1 if s_ns else 1)
        if v_ew and v_ns:
            speed = math.hypot(ew, ns)
            heading = math.degrees(math.atan2(ew, ns)) % 360.0
            fields["ground_speed_kt"] = speed
            fields["track_deg"] = heading
    elif subtype in (3, 4):
        s_hdg = int(me[13])
        if s_hdg:
            hdg_raw = _bits_to_int(me[14:24])
            fields["track_deg"] = hdg_raw / 1024.0 * 360.0
    s_vr = int(me[36])
    vr_raw = _bits_to_int(me[37:46])
    if vr_raw:
        fields["vertical_rate_fpm"] = float((vr_raw - 1) * 64 * (-1 if s_vr else 1))
    return fields


def decode_me(tc: int, me: np.ndarray) -> dict:
    if 1 <= tc <= 4:
        return decode_identification(me)
    if 9 <= tc <= 18:
        return decode_airborne_position(me)
    if tc == 19:
        return decode_airborne_velocity(me)
    return {}


class CprTracker:
    """Pairs the most recent even/odd airborne-position frame per ICAO
    address and resolves a global CPR position once both are present.
    """

    def __init__(self, max_age_s: float = 10.0):
        self._max_age_s = max_age_s
        self._even: dict[int, tuple[int, int, float]] = {}
        self._odd: dict[int, tuple[int, int, float]] = {}

    def update(self, icao: int, fields: dict, now: float) -> dict:
        if "cpr_lat" not in fields:
            return {}
        slot = self._odd if fields["cpr_odd"] else self._even
        slot[icao] = (fields["cpr_lat"], fields["cpr_lon"], now)
        even = self._even.get(icao)
        odd = self._odd.get(icao)
        if even is None or odd is None:
            return {}
        if abs(even[2] - odd[2]) > self._max_age_s:
            return {}
        resolved = cpr_resolve_global(
            even[0], even[1], odd[0], odd[1], odd_is_latest=fields["cpr_odd"]
        )
        if resolved is None:
            return {}
        lat, lon = resolved
        return {"latitude": lat, "longitude": lon}


class AdsbReceiver:
    """Streaming demodulator: feed(complex64 IQ at 2 MS/s) -> list[AdsbMessage].

    Bounded internal buffer: only the samples needed to complete a frame
    that might start near the end of the previous call are retained.
    """

    def __init__(
        self, correlation_threshold: float = _DEFAULT_THRESHOLD, samples_per_chip: int = 1
    ):
        self._threshold = correlation_threshold
        # SDRangel's samplesPerChip: 1 at the native 2 MS/s rate; adsb.py
        # oversamples (2) instead of decimating to exactly 1 sample/chip
        # when the input isn't already 2 MS/s, trading some CPU for immunity
        # to a bad chip-phase landing from the resample (see _find_candidates).
        self._chip_width = int(samples_per_chip)
        self._tail = np.empty(0, dtype=np.complex64)
        self._sample_index = 0
        self._cpr = CprTracker()

    def stats(self) -> dict:
        return {}

    def feed(self, iq: np.ndarray, now: float = 0.0) -> list[AdsbMessage]:
        if len(iq):
            buf = np.concatenate((self._tail, iq)) if len(self._tail) else iq
        else:
            buf = self._tail
        base_index = self._sample_index - len(self._tail)
        messages: list[AdsbMessage] = []
        margin = (_PREAMBLE_CHIPS + _BITS_LONG * 2) * self._chip_width
        if len(buf) >= margin:
            power = (buf.real.astype(np.float32) ** 2 + buf.imag.astype(np.float32) ** 2)
            starts, ratios = _find_candidates(power, self._threshold, self._chip_width)
            messages = self._decode_candidates(power, starts, ratios, base_index, now)
        # Retain enough tail for a frame whose preamble started near the end.
        keep_from = max(0, len(buf) - margin)
        self._tail = buf[keep_from:].copy()
        self._sample_index += len(iq)
        return messages

    def _decode_candidates(
        self,
        power: np.ndarray,
        starts: np.ndarray,
        ratios: np.ndarray,
        base_index: int,
        now: float,
    ) -> list[AdsbMessage]:
        if not len(starts):
            return []
        bits112 = _demod_bits(power, starts, _BITS_LONG, self._chip_width)
        bytes112 = _pack_bytes(bits112)
        df = (bytes112[:, 0] >> 3) & 0x1F

        results: list[tuple[int, float, AdsbMessage]] = []  # (start, ratio, msg)

        es_mask = (df == 17) | (df == 18)
        if es_mask.any():
            es_bits = bits112[es_mask][:, :88]
            es_bytes = bytes112[es_mask]
            crc = _crc24_batch(es_bits)
            parity = (
                (es_bytes[:, 11].astype(np.int64) << 16)
                | (es_bytes[:, 12].astype(np.int64) << 8)
                | es_bytes[:, 13].astype(np.int64)
            )
            valid = crc == parity
            es_starts = starts[es_mask]
            es_ratios = ratios[es_mask]
            es_df = df[es_mask]
            for i in np.nonzero(valid)[0]:
                row = es_bytes[i]
                icao = (int(row[1]) << 16) | (int(row[2]) << 8) | int(row[3])
                if not _icao_valid(icao):
                    continue
                tc = _bits_to_int(bits112[es_mask][i][32:37])
                me = bits112[es_mask][i][32:88]
                fields = decode_me(tc, me)
                if "cpr_lat" in fields:
                    fields = {**fields, **self._cpr.update(icao, fields, now)}
                    fields.pop("cpr_odd", None)
                    fields.pop("cpr_lat", None)
                    fields.pop("cpr_lon", None)
                msg = AdsbMessage(
                    df=int(es_df[i]),
                    icao=icao,
                    sample_index=base_index + int(es_starts[i]),
                    preamble_correlation=float(es_ratios[i]),
                    crc_ok=True,
                    fields=fields,
                )
                results.append((int(es_starts[i]), float(es_ratios[i]), msg))

        df11_mask = df == 11
        if df11_mask.any():
            d11_bits = bits112[df11_mask][:, :32]
            d11_bytes = bytes112[df11_mask]
            crc = _crc24_batch(d11_bits)
            parity = (
                (d11_bytes[:, 4].astype(np.int64) << 16)
                | (d11_bytes[:, 5].astype(np.int64) << 8)
                | d11_bytes[:, 6].astype(np.int64)
            )
            valid = (crc & 0xFFFF80) == (parity & 0xFFFF80)
            d11_starts = starts[df11_mask]
            d11_ratios = ratios[df11_mask]
            for i in np.nonzero(valid)[0]:
                row = d11_bytes[i]
                icao = (int(row[1]) << 16) | (int(row[2]) << 8) | int(row[3])
                if not _icao_valid(icao):
                    continue
                msg = AdsbMessage(
                    df=11,
                    icao=icao,
                    sample_index=base_index + int(d11_starts[i]),
                    preamble_correlation=float(d11_ratios[i]),
                    crc_ok=True,
                    fields={},
                )
                results.append((int(d11_starts[i]), float(d11_ratios[i]), msg))

        # Greedy non-overlapping selection in time order, matching
        # SDRangel's readIdx skip-ahead past a successfully decoded frame.
        results.sort(key=lambda r: r[0])
        out: list[AdsbMessage] = []
        last_end = -1
        for start, _ratio, msg in results:
            if start <= last_end:
                continue
            out.append(msg)
            nbits = _BITS_SHORT if msg.df == 11 else _BITS_LONG
            frame_span = (_PREAMBLE_CHIPS + nbits * 2) * self._chip_width
            last_end = start + frame_span - 1
        return out
