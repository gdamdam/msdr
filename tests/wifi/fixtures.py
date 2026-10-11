"""Builders for synthetic radiotap + 802.11 management-frame bytes, so
tests/wifi don't need real capture files. Every builder produces bytes that
match what msdr.wifi.{pcap,radiotap,dot11} are expected to parse; deviations
used to test error paths are constructed explicitly in each test.
"""

from __future__ import annotations

import struct
import zlib

OUI_IEEE80211 = b"\x00\x0f\xac"
OUI_MICROSOFT = b"\x00\x50\xf2"
OUI_MSDR_RAW = b"\x00\x00\x00"

FLAG_FCS_AT_END = 0x10
FLAG_BAD_FCS = 0x40


def mac(text: str) -> bytes:
    return bytes(int(h, 16) for h in text.split(":"))


def _pad(offset: int, alignment: int) -> int:
    return (-offset) % alignment


def build_radiotap(
    *,
    tsft: int | None = None,
    flags: int | None = None,
    rate_500kbps: int | None = None,
    channel_mhz: int | None = None,
    channel_flags: int = 0,
    antsignal: int | None = None,
    vendor: tuple[bytes, int, bytes] | None = None,
    vendor_in_extended: bool = False,
) -> bytes:
    """A valid radiotap header carrying exactly the requested fields, laid
    out in ascending bit order with alignment relative to header start. When
    `vendor_in_extended` the vendor namespace bit is set in a *second*
    presence word (bit 31 of word 0 set to chain to it), exercising the
    extended-presence-bitmap path."""

    has_ext_word = vendor is not None and vendor_in_extended
    offset = 8 + (4 if has_ext_word else 0)
    body = bytearray()
    present0 = 0
    present1 = 0

    if tsft is not None:
        body += b"\x00" * _pad(offset, 8)
        offset += _pad(offset, 8)
        body += struct.pack("<Q", tsft)
        offset += 8
        present0 |= 1 << 0
    if flags is not None:
        body += struct.pack("<B", flags)
        offset += 1
        present0 |= 1 << 1
    if rate_500kbps is not None:
        body += struct.pack("<B", rate_500kbps)
        offset += 1
        present0 |= 1 << 2
    if channel_mhz is not None:
        pad = _pad(offset, 2)
        body += b"\x00" * pad
        offset += pad
        body += struct.pack("<HH", channel_mhz, channel_flags)
        offset += 4
        present0 |= 1 << 3
    if antsignal is not None:
        body += struct.pack("<b", antsignal)
        offset += 1
        present0 |= 1 << 5

    if vendor is not None:
        oui, sub_ns, payload = vendor
        pad = _pad(offset, 2)
        body += b"\x00" * pad
        offset += pad
        body += oui + bytes([sub_ns]) + struct.pack("<H", len(payload)) + payload
        offset += 6 + len(payload)
        if vendor_in_extended:
            present1 |= 1 << 30
        else:
            present0 |= 1 << 30

    if has_ext_word:
        present0 |= 1 << 31
        presents = struct.pack("<II", present0, present1)
    else:
        presents = struct.pack("<I", present0)

    header_length = 8 + (4 if has_ext_word else 0) + len(body)
    header = struct.pack("<BBH", 0, 0, header_length) + presents + bytes(body)
    assert len(header) == header_length
    return header


def build_rsn_ie(
    group_cipher: int = 4,
    pairwise: tuple[int, ...] = (4,),
    akms: tuple[int, ...] = (2,),
    mfpc: bool = False,
    mfpr: bool = False,
    version: int = 1,
    pairwise_oui: bytes = OUI_IEEE80211,
    akm_oui: bytes = OUI_IEEE80211,
) -> bytes:
    out = struct.pack("<H", version)
    out += OUI_IEEE80211 + bytes([group_cipher])
    out += struct.pack("<H", len(pairwise))
    for c in pairwise:
        out += pairwise_oui + bytes([c])
    out += struct.pack("<H", len(akms))
    for a in akms:
        out += akm_oui + bytes([a])
    cap = (0x0040 if mfpr else 0) | (0x0080 if mfpc else 0)
    out += struct.pack("<H", cap)
    return out


def build_wpa1_ie_body(
    unicast: tuple[int, ...] = (2,), akms: tuple[int, ...] = (2,), multicast: int = 2
) -> bytes:
    """Full IE 221 payload, including the OUI+type vendor header."""
    out = OUI_MICROSOFT + bytes([1])  # WPA1 vendor IE type
    out += struct.pack("<H", 1)  # version
    out += OUI_MICROSOFT + bytes([multicast])
    out += struct.pack("<H", len(unicast))
    for c in unicast:
        out += OUI_MICROSOFT + bytes([c])
    out += struct.pack("<H", len(akms))
    for a in akms:
        out += OUI_MICROSOFT + bytes([a])
    return out


def build_beacon_frame(
    *,
    bssid: str = "aa:bb:cc:dd:ee:01",
    source: str | None = None,
    destination: str = "ff:ff:ff:ff:ff:ff",
    subtype: int = 8,
    frame_timestamp: int = 0,
    beacon_interval: int = 100,
    capability: int = 0x0001,
    ssid: bytes | None = b"TestNet",
    ds_channel: int | None = 6,
    rates: tuple[float, ...] | None = (1.0, 2.0, 5.5, 11.0),
    country: bytes | None = None,
    rsn: bytes | None = None,
    wpa1: bytes | None = None,
    ht: bool = False,
    vht: bool = False,
    he: bool = False,
    extra_ies: list[tuple[int, bytes]] | None = None,
) -> bytes:
    fc = ((subtype & 0xF) << 4) | (0 << 2) | 0  # type = management (0)
    header = (
        struct.pack("<H", fc)
        + struct.pack("<H", 0)  # duration
        + mac(destination)
        + mac(source or bssid)
        + mac(bssid)
        + struct.pack("<H", 0)  # seq ctrl
    )
    fixed = (
        struct.pack("<Q", frame_timestamp)
        + struct.pack("<H", beacon_interval)
        + struct.pack("<H", capability)
    )

    ies = bytearray()
    if ssid is not None:
        ies += bytes([0, len(ssid)]) + ssid
    if rates:
        rate_bytes = bytes(int(round(r * 2)) & 0x7F for r in rates)
        ies += bytes([1, len(rate_bytes)]) + rate_bytes
    if ds_channel is not None:
        ies += bytes([3, 1, ds_channel])
    if country is not None:
        ies += bytes([7, len(country)]) + country
    if rsn is not None:
        ies += bytes([48, len(rsn)]) + rsn
    if ht:
        ies += bytes([45, 2]) + b"\x00\x00"
    if vht:
        ies += bytes([191, 2]) + b"\x00\x00"
    if he:
        ies += bytes([255, 1, 35])  # extension IE, ext_id=35 (HE Capabilities)
    if wpa1 is not None:
        ies += bytes([221, len(wpa1)]) + wpa1
    for ie_id, data in extra_ies or []:
        ies += bytes([ie_id, len(data)]) + data

    return header + fixed + bytes(ies)


def append_fcs(frame: bytes) -> bytes:
    """A real (good) FCS appended, for tests that exercise fcs_present
    without caring whether the CRC itself validates against anything."""
    return frame + struct.pack("<I", zlib.crc32(frame) & 0xFFFFFFFF)


def append_bad_fcs(frame: bytes) -> bytes:
    return frame + b"\xde\xad\xbe\xef"
