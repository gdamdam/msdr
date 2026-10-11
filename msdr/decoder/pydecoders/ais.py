# SPDX-License-Identifier: GPL-3.0-or-later
"""AIS (GMSK 9600 baud, BT 0.4, h 0.5) decoder on flat NFM discriminator audio.

Input: float32 mono at 48 kHz (5 samples/symbol) from the NFM flat tap
(VirtualReceiver.flat_tap / the band channelizer) -- *not* IQ, so the
receiver's own discriminator is the front end here.

Receiver structure ported from the SDRangel AIS demodulator's discriminator
path, plugins/channelrx/demodais/aisdemodsink.cpp @ 6627a5cf7058
(GPL-3.0-or-later, (C) Jon Beniston M7RCE, Edouard Griffiths F4EXB), via the
S0 PoC docs/decisions/poc/gmsk_ais_poc.py: Gaussian pulse filter ->
training-sequence preamble correlation for alignment -> DC (frequency-offset)
estimate from the training sequence only -> hard slice at symbol centres,
trying neighbouring alignments -> NRZI -> HDLC flag search / de-stuffing /
abort -> CRC-16/X.25. `_deframe` follows the same state machine as
AISDemodSink::deframe(). SDRangel's newer coherent GMSK MLSE is not ported.

Integrity: a frame becomes an event only when its CRC-16/X.25 matches *and*
it is long enough to carry an MMSI; CRC failures are counted in stats(),
never emitted. Validation is synthetic-only (tests/decoder/test_ais.py);
field validation on 161.975/162.025 MHz is pending.
"""

from __future__ import annotations

import dataclasses
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent

_FS = 48_000
_BAUD = 9_600
_SPS = _FS // _BAUD  # 5 samples/symbol
# Receive pulse filter: Gaussian at BT 0.5 rather than the transmitter's 0.4
# (the PoC/SDRangel value) -- measured on synthetic bursts at 14 dB Eb/N0,
# 1000 bursts: 0.854 -> 0.880 frame success.
_BT_RX = 0.5
_TRAIN_BITS = 24
_FLAG = 0x7E
# Longest AIS transmission is 5 slots; 1008 data bits + CRC, stuffed (x6/5),
# covers every message type. The deframer stops at the closing flag, so the
# bound only costs time on candidates that never close.
_MAX_DATA_BITS = 1008
_MAX_FRAME_SYMS = _TRAIN_BITS + 8 + (_MAX_DATA_BITS + 16) * 6 // 5 + 8 + 8
# The start flag sits right after the training sequence; a candidate that
# hasn't seen it a few symbols later is not a burst (saves walking the whole
# window per false preamble on noise).
_SOP_SEARCH_SYMS = _TRAIN_BITS + 8 + 8
_CORR_THRESHOLD = 0.6
_CLIP = 1.4  # x peak deviation, as the PoC's clip(-1.4, 1.4)
# Sample offsets tried around the correlation peak, nearest first (SDRangel
# tries neighbouring alignments too); +-2.5 samples spans a whole symbol.
_ALIGN_TRIES = tuple(sorted((k / 2 for k in range(-5, 6)), key=abs))
_MIN_MMSI_BYTES = 5  # type(6) + repeat(2) + MMSI(30) = 38 bits

_NAV_STATUS = {
    0: "under way using engine",
    1: "at anchor",
    2: "not under command",
    3: "restricted manoeuvrability",
    4: "constrained by draught",
    5: "moored",
    6: "aground",
    7: "engaged in fishing",
    8: "under way sailing",
    14: "AIS-SART active",
    15: "not defined",
}


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _gaussian_taps(bt: float, sps: int, span: int) -> np.ndarray:
    t = np.arange(-span * sps / 2, span * sps / 2 + 1) / sps
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-(t**2) / (2 * sigma**2))
    return g / g.sum()


def _nrzi_levels(bits: list[int]) -> np.ndarray:
    level, out = 0, []
    for bit in bits:
        if bit == 0:
            level ^= 1
        out.append(level)
    return np.array(out, dtype=np.float64)


_PULSE = _gaussian_taps(_BT_RX, _SPS, 3)
_TEMPLATE_BITS = [i & 1 for i in range(_TRAIN_BITS)] + [(_FLAG >> i) & 1 for i in range(8)]
# Expected filtered-discriminator waveform for training + start flag (one NRZI
# polarity; the other is -template, so |corr| covers both).
_TEMPLATE = np.convolve(np.repeat(2.0 * _nrzi_levels(_TEMPLATE_BITS) - 1.0, _SPS), _PULSE, "same")
_TEMPLATE -= _TEMPLATE.mean()
# Norm of the unit-deviation template: raw correlation / this = the burst's
# peak-deviation amplitude in input units (scale-free clip, below).
_TEMPLATE_NORM = float(np.linalg.norm(_TEMPLATE))
_TEMPLATE /= _TEMPLATE_NORM
_TMPL_LEN = _TEMPLATE.size
_MARGIN = _PULSE.size  # filter edge samples never searched
_NEED = _TMPL_LEN + _MAX_FRAME_SYMS * _SPS + 2 * _MARGIN


def _deframe(symbols: np.ndarray) -> tuple[str, bytes]:
    """HDLC deframer, same state machine as AISDemodSink::deframe().

    Returns ("ok", data) with the CRC stripped, or (reason, b"") where reason
    is "crc" (closing flag reached, CRC mismatch) or "framing" (no start
    flag, abort, or no closing flag)."""
    got_sop = False
    bits = bit_count = ones = 0
    out = bytearray()
    prev = 0
    for n, s in enumerate(symbols):
        bit = 1 if s == prev else 0
        prev = s
        bits |= bit << bit_count
        bit_count += 1
        if bit:
            ones += 1
            if ones == 7 and got_sop:
                return "framing", b""  # HDLC abort
        else:
            if ones == 5:
                bit_count -= 1  # de-stuff
            elif ones == 6:
                if got_sop and bit_count == 8 and bits == _FLAG and len(out) >= 3:
                    rx = out[-2] | (out[-1] << 8)
                    data = bytes(out[:-2])
                    return ("ok", data) if crc16_x25(data) == rx else ("crc", b"")
                if got_sop:
                    return "framing", b""
                got_sop, bits, bit_count, out = True, 0, 0, bytearray()
            ones = 0
        if bit_count == 8:
            if got_sop:
                out.append(bits)
            bits = bit_count = 0
        elif not got_sop and bit_count > 8:
            bits >>= 1
            bit_count -= 1
        if not got_sop and n > _SOP_SEARCH_SYMS:
            return "framing", b""
    return "framing", b""


# -- payload -------------------------------------------------------------------


def _bits(data: bytes, start: int, width: int, *, signed: bool = False) -> int | None:
    """MSB-first field at bit `start` of the payload, or None if truncated."""
    if (start + width) > 8 * len(data):
        return None
    value = int.from_bytes(data, "big") >> (8 * len(data) - start - width)
    value &= (1 << width) - 1
    if signed and value & (1 << (width - 1)):
        value -= 1 << width
    return value


def _position_fields(data: bytes, sog_at: int, lon_at: int) -> dict[str, object]:
    """SOG/lon/lat/COG/heading for types 1/2/3 and 18 (same widths; only the
    offsets differ). "Not available" sentinels come back as None."""
    sog = _bits(data, sog_at, 10)
    lon = _bits(data, lon_at, 28, signed=True)
    lat = _bits(data, lon_at + 28, 27, signed=True)
    cog = _bits(data, lon_at + 55, 12)
    heading = _bits(data, lon_at + 67, 9)
    if heading is None:
        return {}
    lon_deg = lon / 600_000.0
    lat_deg = lat / 600_000.0
    return {
        "sog_kn": None if sog == 1023 else sog / 10.0,
        "lon_deg": None if lon == 181 * 600_000 or abs(lon_deg) > 180.0 else round(lon_deg, 6),
        "lat_deg": None if lat == 91 * 600_000 or abs(lat_deg) > 90.0 else round(lat_deg, 6),
        "cog_deg": None if cog == 3600 or cog > 3600 else cog / 10.0,
        "heading_deg": None if heading == 511 or heading > 359 else heading,
    }


def decode_payload(data: bytes) -> dict[str, object] | None:
    """Payload fields of a CRC-valid frame, or None when it can't carry an MMSI."""
    if len(data) < _MIN_MMSI_BYTES:
        return None
    msg_type = _bits(data, 0, 6)
    fields: dict[str, object] = {
        "type": msg_type,
        "repeat": _bits(data, 6, 2),
        "mmsi": _bits(data, 8, 30),
        "payload_hex": data.hex(),
    }
    if msg_type in (1, 2, 3):
        status = _bits(data, 38, 4)
        if status is not None:
            fields["nav_status"] = status
            fields["nav_status_text"] = _NAV_STATUS.get(status, "reserved")
        fields.update(_position_fields(data, sog_at=50, lon_at=61))
    elif msg_type == 18:
        fields.update(_position_fields(data, sog_at=46, lon_at=57))
    # Types 5/24 (static/voyage data, class B static): type + MMSI only in S4.
    return fields


def _text(fields: dict[str, object]) -> str:
    parts = [f"AIS type {fields['type']} MMSI {fields['mmsi']:09d}"]
    if fields.get("lat_deg") is not None and fields.get("lon_deg") is not None:
        parts.append(f"{fields['lat_deg']:.5f},{fields['lon_deg']:.5f}")
    if fields.get("sog_kn") is not None:
        parts.append(f"SOG {fields['sog_kn']:.1f} kn")
    if fields.get("cog_deg") is not None:
        parts.append(f"COG {fields['cog_deg']:.1f}")
    if fields.get("heading_deg") is not None:
        parts.append(f"HDG {fields['heading_deg']}")
    if "nav_status_text" in fields:
        parts.append(str(fields["nav_status_text"]))
    return " ".join(parts)


# DecodeEvent gains `integrity`/`integrity_note` in S4 (W2); set them only when
# the running dataclass has them so this module works before and after.
_EVENT_FIELDS = {f.name for f in dataclasses.fields(DecodeEvent)}


def _event(fields: dict[str, object]) -> DecodeEvent:
    kwargs: dict[str, object] = {
        "ts": time.time(),
        "decoder": "ais",
        "kind": "text",
        "text": _text(fields),
        "fields": fields,
    }
    if "integrity" in _EVENT_FIELDS:
        kwargs["integrity"] = "passed"
    if "integrity_note" in _EVENT_FIELDS:
        kwargs["integrity_note"] = "CRC-16/X.25 verified"
    return DecodeEvent(**kwargs)


# -- decoder -------------------------------------------------------------------


class AisDecoder:
    """PyDecoder: flat NFM discriminator audio (float32, 48 kHz) -> AIS frames.

    Scale-free: preamble correlation is energy-normalised and slicing is
    against the training-sequence mean, so the flat tap's deviation scaling
    or an upstream AGC doesn't matter."""

    input_rate = _FS

    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.float64)
        self._base = 0  # absolute sample index of _buf[0]
        self._skip_until = 0  # absolute: don't search before this
        self._first = True
        self._stats = {
            "preambles": 0,
            "frames_ok": 0,
            "crc_fail": 0,
            "framing_fail": 0,
            "no_mmsi": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size:
            self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float64)])
        if self._buf.size < 2 * _NEED:
            return []
        return self._process(final=False)

    def flush(self) -> list[DecodeEvent]:
        if self._buf.size < _TMPL_LEN + _MARGIN:
            return []
        return self._process(final=True)

    def _process(self, *, final: bool) -> list[DecodeEvent]:
        filt = np.convolve(self._buf, _PULSE, mode="same")
        lo = 0 if self._first else _MARGIN
        hi = filt.size - _TMPL_LEN if final else filt.size - _NEED
        events = [self._event_for(f) for f in self._search(filt, lo, hi)]
        events = [e for e in events if e is not None]
        if final:
            self._buf = np.zeros(0, dtype=np.float64)
            self._base += filt.size
        else:
            # Keep the unsearched tail plus a filter margin before it.
            drop = hi - _MARGIN
            self._buf = self._buf[drop:]
            self._base += drop
        self._first = False
        return events

    def _search(self, filt: np.ndarray, lo: int, hi: int) -> list[bytes]:
        n = _TMPL_LEN
        if hi <= lo or filt.size < n:
            return []
        raw = np.correlate(filt, _TEMPLATE, mode="valid")
        # Normalise by local energy after removing the local mean (the mean is
        # the frequency-offset DC term SDRangel subtracts via the training sum).
        c1 = np.concatenate(([0.0], np.cumsum(filt)))
        c2 = np.concatenate(([0.0], np.cumsum(filt * filt)))
        s1 = c1[n:] - c1[:-n]
        s2 = c2[n:] - c2[:-n]
        energy = np.sqrt(np.maximum(s2 - s1 * s1 / n, 1e-24))
        metric = np.abs(raw) / energy
        hi = min(hi, metric.size)
        frames: list[bytes] = []
        for i in lo + np.flatnonzero(metric[lo:hi] > _CORR_THRESHOLD):
            i = int(i)
            if self._base + i < self._skip_until:
                continue
            best = i + int(np.argmax(metric[i : min(i + n, metric.size)]))  # local peak
            self._stats["preambles"] += 1
            # DC (frequency offset) from the training sequence only, as SDRangel
            # does; the NRZI-coded flag is not zero-mean. SDRangel/the PoC clip
            # the filtered discriminator at 1.4x peak deviation so FM clicks
            # can't drag that estimate; the input scale is unknown here
            # (flat-tap scaling, band-path AGC), so the clip is relative to
            # this burst's own amplitude, read off the correlation peak.
            amp = abs(float(raw[best])) / _TEMPLATE_NORM
            seg = filt[best : best + (_MAX_FRAME_SYMS + 1) * _SPS]
            train = seg[: _TRAIN_BITS * _SPS]
            mean = float(train.mean())
            dc = float(np.clip(train, mean - _CLIP * amp, mean + _CLIP * amp).mean())
            outcome, data = "framing", b""
            for d in _ALIGN_TRIES:
                # Fractional (half-sample) timing tries: at 5 samples/symbol
                # the integer grid alone left ~3 points of frame success at
                # 14 dB (synthetic, 1000 bursts).
                pos = d + _SPS // 2 + _SPS * np.arange(_MAX_FRAME_SYMS, dtype=float)
                pos = pos[(pos >= 0) & (pos <= seg.size - 1)]
                vals = np.interp(pos, np.arange(seg.size), seg)
                result, got = _deframe((vals - dc >= 0).astype(np.int8))
                if result == "ok":
                    outcome, data = result, got
                    break
                if result == "crc":
                    outcome = "crc"
            if outcome == "ok":
                frames.append(data)
                self._stats["frames_ok"] += 1
                self._skip_until = self._base + best + (_TRAIN_BITS + 16 + 8 * len(data)) * _SPS
            else:
                self._stats["crc_fail" if outcome == "crc" else "framing_fail"] += 1
                self._skip_until = self._base + best + n
        return frames

    def _event_for(self, data: bytes) -> DecodeEvent | None:
        fields = decode_payload(data)
        if fields is None:
            # CRC-valid but too short to carry an MMSI: no identity, no event.
            self._stats["no_mmsi"] += 1
            return None
        return _event(fields)
