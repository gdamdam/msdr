"""Radiotap header parser.

Handles the chained (extended) presence bitmaps, per-field alignment
relative to the start of the radiotap header, and the Vendor Namespace
escape (bit 30 of any presence word): OUI + sub-namespace + skip_length,
payload returned keyed by `(oui, sub_ns)` so callers never confuse vendor
bytes with a calibrated reading.

Only the fields msdr actually consumes are decoded to typed values (TSFT,
Flags, Rate, Channel, Antenna signal); every other known field is skipped by
its declared (alignment, size) so the header length still lines up. A
presence bit we have no size for stops parsing and returns a `ParseError`
rather than guessing and misreading the rest of the header.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MORE_BITMAPS = 1 << 31
NAMESPACE_NEXT_BIT = 29
VENDOR_NS_BIT = 30
MAX_PRESENCE_WORDS = 8

FLAG_FCS_AT_END = 0x10
FLAG_BAD_FCS = 0x40

# bit -> (alignment, size in bytes) for fields whose layout we know. Bits not
# listed here (e.g. 27 TLV, 28 reserved) are intentionally absent: hitting a
# set bit without a table entry is reported as an error, not guessed at.
_FIELD_TABLE: dict[int, tuple[int, int]] = {
    0: (8, 8),  # TSFT
    1: (1, 1),  # Flags
    2: (1, 1),  # Rate
    3: (2, 4),  # Channel (freq u16 + flags u16)
    4: (2, 2),  # FHSS
    5: (1, 1),  # Antenna signal (dBm)
    6: (1, 1),  # Antenna noise (dBm)
    7: (2, 2),  # Lock quality
    8: (2, 2),  # TX attenuation
    9: (2, 2),  # dB TX attenuation
    10: (1, 1),  # dBm TX power
    11: (1, 1),  # Antenna
    12: (1, 1),  # dB antenna signal
    13: (1, 1),  # dB antenna noise
    14: (2, 2),  # RX flags
    15: (2, 2),  # TX flags
    16: (1, 1),  # RTS retries
    17: (1, 1),  # Data retries
    18: (4, 8),  # XChannel
    19: (1, 3),  # MCS
    20: (4, 8),  # A-MPDU status
    21: (2, 12),  # VHT
    22: (8, 12),  # Frame timestamp
    23: (2, 12),  # HE
    24: (2, 12),  # HE-MCS
    25: (1, 1),  # Zero-length PSDU
    26: (2, 4),  # L-SIG
}

# The helper's "msdr-raw" vendor namespace (docs/wifi.md): version 1 is eleven bytes, an
# unsigned packet power code (upstream reads it unsigned, 0..255) and signed bytes for the
# rest, -128 meaning absent. Older helpers wrote raw descriptor bytes there instead; the exact
# length and version tell the two apart.
MSDR_RAW_KEY = (b"\x00\x00\x00", 0)
MSDR_RAW_V1_LEN = 11
MSDR_RAW_ABSENT = -128


@dataclass(frozen=True)
class ParseError:
    kind: str
    detail: str


@dataclass(frozen=True)
class RadiotapFlags:
    raw: int
    fcs_present: bool
    bad_fcs: bool


@dataclass(frozen=True)
class Radiotap:
    version: int
    header_length: int
    present_words: tuple[int, ...]
    tsft: int | None = None
    flags: RadiotapFlags | None = None
    rate_500kbps: int | None = None
    channel_mhz: int | None = None
    channel_flags: int | None = None
    dbm_antsignal: int | None = None
    vendor: dict[tuple[bytes, int], bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class MsdrRawChain:
    gain_code: int | None
    snr_code: int | None
    evm_code: int | None


@dataclass(frozen=True)
class MsdrRaw:
    """Raw, uncalibrated PHY codes the helper writes per frame. Never dBm."""

    version: int
    packet_power_code: int | None
    signal_evm_code: int | None
    antenna_a: int | None
    antenna_b: int | None
    chains: tuple[MsdrRawChain, ...]


def msdr_raw(rt: Radiotap | ParseError | None) -> MsdrRaw | None:
    """Decode the msdr-raw vendor payload, or None when absent or not version 1."""
    if rt is None or isinstance(rt, ParseError):
        return None
    payload = rt.vendor.get(MSDR_RAW_KEY)
    if payload is None or len(payload) != MSDR_RAW_V1_LEN or payload[0] != 1:
        return None
    v = [None if x == MSDR_RAW_ABSENT else x for x in struct.unpack("<bB9b", payload)]
    chains = tuple(MsdrRawChain(*v[i:i + 3]) for i in (5, 8)
                   if any(x is not None for x in v[i:i + 3]))
    return MsdrRaw(1, v[1], v[2], v[3], v[4], chains)


def _align(offset: int, alignment: int) -> int:
    if alignment <= 1:
        return offset
    rem = offset % alignment
    return offset if rem == 0 else offset + (alignment - rem)


def parse(data: bytes) -> Radiotap | ParseError:
    if len(data) < 8:
        return ParseError("truncated", "radiotap header shorter than 8 bytes")

    version = data[0]
    if version != 0:
        return ParseError("bad_version", f"unsupported radiotap version {version}")

    header_length = struct.unpack_from("<H", data, 2)[0]
    if header_length > len(data):
        return ParseError(
            "truncated", f"declared length {header_length} exceeds buffer {len(data)}"
        )
    if header_length < 8:
        return ParseError("truncated", f"declared length {header_length} too short")

    present_words: list[int] = []
    offset = 4
    while True:
        if len(present_words) >= MAX_PRESENCE_WORDS:
            return ParseError("malformed", "too many chained presence bitmaps")
        if offset + 4 > header_length:
            return ParseError("truncated", "presence bitmap runs past header length")
        word = struct.unpack_from("<I", data, offset)[0]
        present_words.append(word)
        offset += 4
        if not (word & MORE_BITMAPS):
            break

    tsft: int | None = None
    flags: RadiotapFlags | None = None
    rate_500kbps: int | None = None
    channel_mhz: int | None = None
    channel_flags: int | None = None
    dbm_antsignal: int | None = None
    vendor: dict[tuple[bytes, int], bytes] = {}

    # Bit 30 of a word switches the NEXT presence word to a vendor namespace
    # and bit 29 switches it back to radiotap (radiotap.org "Vendor Namespace").
    # Fields of a vendor word are never interpreted here: their data lies inside
    # the vendor block already skipped via skip_length. Reading them as radiotap
    # fields misparsed every frame from the msdr-wifi-capture helper.
    next_in_vendor = False
    for word in present_words:
        in_vendor = next_in_vendor
        if (word >> VENDOR_NS_BIT) & 1:
            next_in_vendor = True
        elif (word >> NAMESPACE_NEXT_BIT) & 1:
            next_in_vendor = False
        for bit in range(0, 31):
            if not (word >> bit) & 1:
                continue
            if bit == NAMESPACE_NEXT_BIT:
                continue  # no payload; just resets field numbering context
            if in_vendor and bit != VENDOR_NS_BIT:
                continue  # vendor-defined field, covered by the vendor block's skip_length
            if bit == VENDOR_NS_BIT:
                offset = _align(offset, 2)
                if offset + 6 > header_length:
                    return ParseError("truncated", "vendor namespace header runs past length")
                oui = bytes(data[offset : offset + 3])
                sub_ns = data[offset + 3]
                skip_length = struct.unpack_from("<H", data, offset + 4)[0]
                offset += 6
                if offset + skip_length > header_length:
                    return ParseError("truncated", "vendor namespace payload runs past length")
                vendor[(oui, sub_ns)] = bytes(data[offset : offset + skip_length])
                offset += skip_length
                continue

            sizing = _FIELD_TABLE.get(bit)
            if sizing is None:
                return ParseError("unknown_field", f"no size/alignment known for bit {bit}")
            alignment, size = sizing
            offset = _align(offset, alignment)
            if offset + size > header_length:
                return ParseError("truncated", f"field for bit {bit} runs past header length")

            # A later radiotap-namespace presence word repeats fields per antenna
            # chain; keep the first word's value (chain 0) instead of letting the
            # last chain overwrite it.
            if bit == 0:
                if tsft is None:
                    tsft = struct.unpack_from("<Q", data, offset)[0]
            elif bit == 1:
                raw = data[offset]
                if flags is None:
                    flags = RadiotapFlags(
                        raw=raw,
                        fcs_present=bool(raw & FLAG_FCS_AT_END),
                        bad_fcs=bool(raw & FLAG_BAD_FCS),
                    )
            elif bit == 2:
                if rate_500kbps is None:
                    rate_500kbps = data[offset]
            elif bit == 3:
                if channel_mhz is None:
                    channel_mhz, channel_flags = struct.unpack_from("<HH", data, offset)
            elif bit == 5:
                if dbm_antsignal is None:
                    dbm_antsignal = struct.unpack_from("<b", data, offset)[0]
            # other known bits: already advanced past, no typed field kept

            offset += size

    return Radiotap(
        version=version,
        header_length=header_length,
        present_words=tuple(present_words),
        tsft=tsft,
        flags=flags,
        rate_500kbps=rate_500kbps,
        channel_mhz=channel_mhz,
        channel_flags=channel_flags,
        dbm_antsignal=dbm_antsignal,
        vendor=vendor,
    )
