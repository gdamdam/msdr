"""802.11 management-frame parser: beacons (subtype 8) and probe responses
(subtype 5) only. Every failure mode -- truncated header, malformed IE
lengths, an unsupported frame type -- is returned as a `Dot11Error` value;
this module never raises on attacker- or corruption-controlled input.

Callers are expected to strip trailing bytes that aren't the 802.11 frame
themselves (radiotap's `header_length`) before calling `parse`; `parse`
strips the 4-byte FCS when told the capture includes one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

FRAME_HEADER_LEN = 24  # frame control, duration, addr1-3, seq ctrl
FIXED_FIELDS_LEN = 12  # timestamp(8) + beacon interval(2) + capability(2)
FCS_LEN = 4

TYPE_MANAGEMENT = 0
SUBTYPE_PROBE_RESPONSE = 5
SUBTYPE_BEACON = 8

CAP_ESS = 0x0001
CAP_IBSS = 0x0002
CAP_PRIVACY = 0x0010

IE_SSID = 0
IE_SUPPORTED_RATES = 1
IE_DS_PARAMETER_SET = 3
IE_COUNTRY = 7
IE_HT_CAPABILITIES = 45
IE_RSN = 48
IE_EXTENDED_SUPPORTED_RATES = 50
IE_VHT_CAPABILITIES = 191
IE_VENDOR_SPECIFIC = 221
IE_EXTENSION = 255
EXT_ID_HE_CAPABILITIES = 35
EXT_ID_HE_OPERATION = 36

OUI_IEEE80211 = b"\x00\x0f\xac"
OUI_MICROSOFT = b"\x00\x50\xf2"
WPA1_OUI_TYPE = 1

_CIPHER_NAMES = {
    1: "WEP-40",
    2: "TKIP",
    4: "CCMP",
    5: "WEP-104",
    6: "BIP-CMAC-128",
    8: "GCMP-128",
    9: "GCMP-256",
    10: "CCMP-256",
    11: "BIP-GMAC-128",
    12: "BIP-GMAC-256",
    13: "BIP-CMAC-256",
}

_WPA1_CIPHER_NAMES = {1: "WEP-40", 2: "TKIP", 4: "CCMP", 5: "WEP-104"}
_WPA1_AKM_NAMES = {1: "802.1X", 2: "PSK"}


@dataclass(frozen=True)
class Dot11Error:
    kind: str
    detail: str


@dataclass(frozen=True)
class InformationElement:
    id: int
    ext_id: int | None
    data: bytes


@dataclass(frozen=True)
class RsnInfo:
    version: int
    group_cipher: str
    pairwise_ciphers: tuple[str, ...]
    akms: tuple[str, ...]
    mfpc: bool
    mfpr: bool


@dataclass(frozen=True)
class Wpa1Info:
    multicast_cipher: str
    unicast_ciphers: tuple[str, ...]
    akms: tuple[str, ...]


@dataclass(frozen=True)
class ManagementFrame:
    subtype: int
    bssid: str
    source: str
    destination: str
    frame_timestamp: int
    beacon_interval: int
    capability_raw: int
    cap_ess: bool
    cap_ibss: bool
    cap_privacy: bool
    ssid_bytes: bytes
    ssid_text: str
    ssid_hidden: bool
    ssid_utf8_ok: bool
    ds_channel: int | None
    supported_rates_mbps: tuple[float, ...]
    country: str | None
    rsn: RsnInfo | None
    wpa1: Wpa1Info | None
    ht_present: bool
    vht_present: bool
    he_present: bool
    security_summary: str
    ies: tuple[InformationElement, ...] = field(default_factory=tuple)


def _mac(data: bytes) -> str:
    return ":".join(f"{b:02x}" for b in data)


def _suite_name(
    oui: bytes, suite_type: int, names: dict[int, str], expected_oui: bytes = OUI_IEEE80211
) -> str:
    if oui == expected_oui and suite_type in names:
        return names[suite_type]
    return f"{_mac(oui).replace(':', '')}:{suite_type}"


def _parse_ies(buf: bytes) -> list[InformationElement] | Dot11Error:
    ies: list[InformationElement] = []
    i = 0
    n = len(buf)
    while i < n:
        if i + 2 > n:
            return Dot11Error("malformed_ie", "truncated IE header")
        ie_id = buf[i]
        ie_len = buf[i + 1]
        start = i + 2
        end = start + ie_len
        if end > n:
            return Dot11Error("malformed_ie", f"IE id={ie_id} length {ie_len} runs past frame")
        payload = buf[start:end]
        ext_id = None
        if ie_id == IE_EXTENSION:
            if not payload:
                return Dot11Error("malformed_ie", "extension IE with no ext id")
            ext_id = payload[0]
            payload = payload[1:]
        ies.append(InformationElement(id=ie_id, ext_id=ext_id, data=bytes(payload)))
        i = end
    return ies


def _parse_rsn(data: bytes) -> RsnInfo | Dot11Error:
    try:
        if len(data) < 2 + 4 + 2:
            return Dot11Error("malformed_ie", "RSN IE too short")
        version = struct.unpack_from("<H", data, 0)[0]
        off = 2
        group_oui, group_type = data[off : off + 3], data[off + 3]
        group_cipher = _suite_name(group_oui, group_type, _CIPHER_NAMES)
        off += 4
        pairwise_count = struct.unpack_from("<H", data, off)[0]
        off += 2
        pairwise: list[str] = []
        for _ in range(pairwise_count):
            oui, suite_type = data[off : off + 3], data[off + 3]
            pairwise.append(_suite_name(oui, suite_type, _CIPHER_NAMES))
            off += 4
        akm_count = struct.unpack_from("<H", data, off)[0]
        off += 2
        akms: list[str] = []
        for _ in range(akm_count):
            oui, suite_type = data[off : off + 3], data[off + 3]
            akms.append(_suite_name(oui, suite_type, _AKM_DISPLAY_NAMES))
            off += 4
        mfpc = mfpr = False
        if off + 2 <= len(data):
            rsn_cap = struct.unpack_from("<H", data, off)[0]
            mfpr = bool(rsn_cap & 0x0040)
            mfpc = bool(rsn_cap & 0x0080)
        return RsnInfo(
            version=version,
            group_cipher=group_cipher,
            pairwise_ciphers=tuple(pairwise),
            akms=tuple(akms),
            mfpc=mfpc,
            mfpr=mfpr,
        )
    except (struct.error, IndexError):
        return Dot11Error("malformed_ie", "RSN IE truncated mid-field")


def _parse_wpa1(vendor_payload: bytes) -> Wpa1Info | Dot11Error:
    # vendor_payload excludes the 4-byte OUI+type header; starts at version.
    try:
        if len(vendor_payload) < 2 + 4:
            # Tuple, like the truncation path below: the caller unpacks it.
            return Dot11Error("malformed_ie", "WPA1 IE too short"), []
        off = 2  # skip version
        mcast_oui, mcast_type = vendor_payload[off : off + 3], vendor_payload[off + 3]
        multicast_cipher = _suite_name(
            mcast_oui, mcast_type, _WPA1_CIPHER_NAMES, expected_oui=OUI_MICROSOFT
        )
        off += 4
        unicast_count = struct.unpack_from("<H", vendor_payload, off)[0]
        off += 2
        unicast: list[str] = []
        for _ in range(unicast_count):
            oui, suite_type = vendor_payload[off : off + 3], vendor_payload[off + 3]
            unicast.append(
                _suite_name(oui, suite_type, _WPA1_CIPHER_NAMES, expected_oui=OUI_MICROSOFT)
            )
            off += 4
        akm_count = struct.unpack_from("<H", vendor_payload, off)[0]
        off += 2
        akms: list[str] = []
        akm_types: list[int] = []
        for _ in range(akm_count):
            oui, suite_type = vendor_payload[off : off + 3], vendor_payload[off + 3]
            akms.append(_suite_name(oui, suite_type, _WPA1_AKM_NAMES, expected_oui=OUI_MICROSOFT))
            akm_types.append(suite_type if oui == OUI_MICROSOFT else -1)
            off += 4
        return Wpa1Info(
            multicast_cipher=multicast_cipher, unicast_ciphers=tuple(unicast), akms=tuple(akms)
        ), akm_types
    except (struct.error, IndexError):
        return Dot11Error("malformed_ie", "WPA1 IE truncated mid-field"), []


def _decode_ssid(raw: bytes) -> tuple[str, bool, bool]:
    """Returns (ssid_text, hidden, utf8_ok)."""
    hidden = len(raw) == 0 or all(b == 0 for b in raw)
    if hidden:
        return "", True, True
    try:
        return raw.decode("utf-8"), False, True
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="backslashreplace"), False, False


def _security_summary(
    cap_privacy: bool, rsn: RsnInfo | None, wpa1: Wpa1Info | None, wpa1_akm_types: list[int]
) -> str:
    if rsn is not None:
        # Classify by the human display names _parse_rsn already produced;
        # a name that isn't in the known table is an unrecognised suite,
        # shown by its raw "OUI:type" fallback string instead of guessed at.
        has_psk = any(n in ("PSK", "PSK-SHA256") for n in _akm_display_names(rsn.akms))
        has_sae = any(n in ("SAE", "FT-SAE") for n in _akm_display_names(rsn.akms))
        has_owe = any(n == "OWE" for n in _akm_display_names(rsn.akms))
        has_enterprise = any(
            n
            in (
                "802.1X",
                "FT-802.1X",
                "802.1X-SHA256",
                "802.1X-Suite-B",
                "802.1X-Suite-B-192",
                "FT-802.1X-SHA384",
                "FILS-SHA256",
                "FILS-SHA384",
                "FT-FILS-SHA256",
                "FT-FILS-SHA384",
            )
            for n in _akm_display_names(rsn.akms)
        )
        unknown = [n for n in rsn.akms if ":" in n and n not in _AKM_DISPLAY_NAMES_VALUES]
        if has_sae and has_psk:
            return "WPA2/WPA3"
        if has_sae:
            return "WPA3-SAE"
        if has_owe:
            return "OWE"
        if has_psk:
            return "WPA2-PSK"
        if has_enterprise:
            return "WPA2-Enterprise"
        if unknown:
            return "/".join(unknown)
        return "WPA2/WPA3"  # RSN present but no AKM we could classify at all
    if wpa1 is not None:
        if any(t == 2 for t in wpa1_akm_types):
            return "WPA1-PSK"
        if any(t == 1 for t in wpa1_akm_types):
            return "WPA1-Enterprise"
        return "WPA1"
    if cap_privacy:
        return "WEP"
    return "Open"


# AKM suite-type -> human display name, built once from the tables above so
# `_security_summary` can classify by name without re-deriving raw types.
_AKM_DISPLAY_NAMES = {
    1: "802.1X",
    2: "PSK",
    3: "FT-802.1X",
    4: "FT-PSK",
    5: "802.1X-SHA256",
    6: "PSK-SHA256",
    7: "TDLS",
    8: "SAE",
    9: "FT-SAE",
    11: "802.1X-Suite-B",
    12: "802.1X-Suite-B-192",
    13: "FT-802.1X-SHA384",
    14: "FILS-SHA256",
    15: "FILS-SHA384",
    16: "FT-FILS-SHA256",
    17: "FT-FILS-SHA384",
    18: "OWE",
}
_AKM_DISPLAY_NAMES_VALUES = set(_AKM_DISPLAY_NAMES.values())


def _akm_display_names(akms: tuple[str, ...]) -> list[str]:
    return [n for n in akms if n in _AKM_DISPLAY_NAMES_VALUES]


def parse(data: bytes, fcs_present: bool = False) -> ManagementFrame | Dot11Error:
    if fcs_present:
        if len(data) < FCS_LEN:
            return Dot11Error("truncated", "frame shorter than the FCS it's said to carry")
        data = data[:-FCS_LEN]

    # Frame type/subtype must be checked before any length requirement tied
    # to the management-frame shape: control frames (ACK/CTS are 10 bytes,
    # RTS 16) are legitimately shorter than FRAME_HEADER_LEN (24) and are not
    # truncated beacons/probe-responses -- they're a frame type this parser
    # doesn't (and never will) decode. Checking length first miscounted every
    # short control frame as "truncated" instead of "unsupported_frame"
    # (confirmed against a real capture: 75 good-FCS ACK/CTS/RTS frames).
    if len(data) < 2:
        return Dot11Error("truncated", "frame shorter than the frame control field")

    frame_control = struct.unpack_from("<H", data, 0)[0]
    ftype = (frame_control >> 2) & 0x3
    subtype = (frame_control >> 4) & 0xF
    if ftype != TYPE_MANAGEMENT or subtype not in (SUBTYPE_BEACON, SUBTYPE_PROBE_RESPONSE):
        return Dot11Error("unsupported_frame", f"type={ftype} subtype={subtype}")

    if len(data) < FRAME_HEADER_LEN:
        return Dot11Error("truncated", "frame shorter than the 802.11 header")

    destination = _mac(data[4:10])
    source = _mac(data[10:16])
    bssid = _mac(data[16:22])

    body = data[FRAME_HEADER_LEN:]
    if len(body) < FIXED_FIELDS_LEN:
        return Dot11Error("truncated", "missing beacon/probe-response fixed fields")

    frame_timestamp = struct.unpack_from("<Q", body, 0)[0]
    beacon_interval = struct.unpack_from("<H", body, 8)[0]
    capability_raw = struct.unpack_from("<H", body, 10)[0]

    ies_or_error = _parse_ies(body[FIXED_FIELDS_LEN:])
    if isinstance(ies_or_error, Dot11Error):
        return ies_or_error
    ies = ies_or_error

    ssid_bytes = b""
    ds_channel: int | None = None
    rates: list[float] = []
    country: str | None = None
    rsn: RsnInfo | None = None
    wpa1: Wpa1Info | None = None
    wpa1_akm_types: list[int] = []
    ht_present = False
    vht_present = False
    he_present = False

    for ie in ies:
        if ie.id == IE_SSID:
            ssid_bytes = ie.data
        elif ie.id in (IE_SUPPORTED_RATES, IE_EXTENDED_SUPPORTED_RATES):
            rates.extend((b & 0x7F) * 0.5 for b in ie.data)
        elif ie.id == IE_DS_PARAMETER_SET:
            if len(ie.data) >= 1:
                ds_channel = ie.data[0]
        elif ie.id == IE_COUNTRY:
            # First two bytes are the alpha-2 code; the third (regulatory
            # environment indicator) is dropped -- not useful for display.
            if len(ie.data) >= 2:
                country = ie.data[:2].decode("ascii", errors="replace")
        elif ie.id == IE_RSN:
            result = _parse_rsn(ie.data)
            if isinstance(result, Dot11Error):
                return result
            rsn = result
        elif ie.id == IE_HT_CAPABILITIES:
            ht_present = True
        elif ie.id == IE_VHT_CAPABILITIES:
            vht_present = True
        elif ie.id == IE_EXTENSION and ie.ext_id in (EXT_ID_HE_CAPABILITIES, EXT_ID_HE_OPERATION):
            he_present = True
        elif ie.id == IE_VENDOR_SPECIFIC and len(ie.data) >= 4:
            oui, vendor_type = ie.data[:3], ie.data[3]
            if oui == OUI_MICROSOFT and vendor_type == WPA1_OUI_TYPE:
                result = _parse_wpa1(ie.data[4:])
                if isinstance(result[0], Dot11Error):
                    return result[0]
                wpa1, wpa1_akm_types = result

    ssid_text, ssid_hidden, ssid_utf8_ok = _decode_ssid(ssid_bytes)
    cap_privacy = bool(capability_raw & CAP_PRIVACY)
    security_summary = _security_summary(cap_privacy, rsn, wpa1, wpa1_akm_types)

    return ManagementFrame(
        subtype=subtype,
        bssid=bssid,
        source=source,
        destination=destination,
        frame_timestamp=frame_timestamp,
        beacon_interval=beacon_interval,
        capability_raw=capability_raw,
        cap_ess=bool(capability_raw & CAP_ESS),
        cap_ibss=bool(capability_raw & CAP_IBSS),
        cap_privacy=cap_privacy,
        ssid_bytes=ssid_bytes,
        ssid_text=ssid_text,
        ssid_hidden=ssid_hidden,
        ssid_utf8_ok=ssid_utf8_ok,
        ds_channel=ds_channel,
        supported_rates_mbps=tuple(rates),
        country=country,
        rsn=rsn,
        wpa1=wpa1,
        ht_present=ht_present,
        vht_present=vht_present,
        he_present=he_present,
        security_summary=security_summary,
        ies=tuple(ies),
    )
