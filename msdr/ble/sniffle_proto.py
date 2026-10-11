# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal, receive-only port of the Sniffle serial protocol (nccgroup/Sniffle
v1.11.0, commit 18252f2c5a05f9ad906865b776652e31c61471c4, GPL-3.0; see
THIRD_PARTY_NOTICES.md "Passive BLE capture (Sniffle)"). Framing and command
encoding are ported from `sniffle_hw.py` (`SniffleHW._send_cmd`/`_recv_msg`);
advertising decode is ported from `packet_decoder.py`'s `AdvertMessage`/
`AdvExtIndMessage`/`AuxAdvIndMessage` family and `constants.py`/
`measurements.py`'s `VersionMeasurement`.

msdr is receive-only by construction: this module implements only the
commands needed for passive advertising sniffing (channel/PHY select, RSSI
floor, CRC validation, the marker and version-probe commands) and always
disables connection-following (`cmd_follow(False)`, never re-enabled). It
does not implement `cmd_scan` (active scan), `cmd_connect`, `cmd_advertise`/
`cmd_advertise_ext`, `cmd_transmit`, `cmd_setaddr`, `cmd_irk`, `cmd_setmap`,
`cmd_interval_preload`, `cmd_phy_preload`, `cmd_instahop` or `cmd_tx_power` at
all -- those code paths do not exist in msdr.

Decoding is also simplified to what receive-only advertising sniffing needs:
upstream's `SniffleDecoderState` additionally tracks pending AUX_SCAN_RSP/
AUX_CHAIN_IND/CONNECT_IND continuations to support connection-following and
active scanning. Since msdr never does either, every received advertising
PDU (including each AUX_CHAIN_IND continuation of a long extended
advertisement) is decoded and reported independently; `msdr.ble.observations`
merges same-address reports as usual. Directed fields (TargetA), CTEInfo,
AdvDataInfo (DID/SID) and SyncInfo (periodic advertising) are parsed past but
not surfaced -- out of scope for Phase 1.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as B64Error
from dataclasses import dataclass
from enum import IntEnum
from struct import pack, unpack

# constants.py
BLE_ADV_AA = 0x8E89BED6
BLE_ADV_CRCI = 0x555555


class PhyMode(IntEnum):
    PHY_1M = 0
    PHY_2M = 1
    PHY_CODED_S8 = 2
    PHY_CODED_S2 = 3


#: msdr's own CLI/settings PHY names -> Sniffle's wire value. Coded PHY
#: always requests S=8 (long range); Sniffle reports whichever coding rate
#: was actually used per packet via `PHY_NAME_BY_VALUE`.
PHY_BY_NAME: dict[str, PhyMode] = {
    "1m": PhyMode.PHY_1M,
    "2m": PhyMode.PHY_2M,
    "coded": PhyMode.PHY_CODED_S8,
}
PHY_NAME_BY_VALUE: dict[int, str] = {0: "1m", 1: "2m", 2: "coded", 3: "coded"}

# Command opcodes msdr sends (subset of SniffleHW.cmd_*; see module docstring
# for what is deliberately not implemented).
CMD_CHAN_AA_PHY = 0x10
CMD_PAUSE_DONE = 0x11
CMD_RSSI = 0x12
CMD_MAC = 0x13
CMD_FOLLOW = 0x15
CMD_AUXADV = 0x16
CMD_MARKER = 0x18
CMD_VERSION = 0x24
CMD_CRC_VALID = 0x26

# Message types the device sends (SniffleHW.recv_and_decode).
MSG_PACKET = 0x10
MSG_DEBUG = 0x11
MSG_MARKER = 0x12
MSG_STATE = 0x13
MSG_MEASUREMENT = 0x14

MEASUREMENT_TYPE_VERSION = 5


class FramingError(Exception):
    """A line did not decode as a well-formed Sniffle message: garbage, most
    often the wrong baud rate for this dongle's bridge chip."""


# --- wire framing (sniffle_hw.py _send_cmd / _recv_msg) ---------------------


def frame(payload: bytes) -> bytes:
    """Base64, word-count-prefixed, CRLF-terminated line -- the one message
    shape used in both directions. `payload` is `[opcode-or-msg_type,
    *rest]`. Every Sniffle message is exactly one line (base64 never
    produces a raw newline byte), so line-based reads are a complete framer,
    not just a resync fallback."""
    b0 = (len(payload) + 3) // 3
    return b64encode(bytes([b0, *payload])) + b"\r\n"


def encode_command(opcode: int, args: bytes = b"") -> bytes:
    return frame(bytes([opcode, *args]))


def decode_line(line: bytes) -> tuple[int, bytes] | None:
    """One CRLF-terminated line -> `(msg_type, body)`, or None for a blank
    line. Raises `FramingError` for anything else malformed."""
    raw = line.rstrip(b"\r\n")
    if not raw.strip():
        return None
    try:
        data = b64decode(raw, validate=True)
    except (B64Error, ValueError) as exc:
        raise FramingError(f"not valid base64: {exc}") from exc
    if len(data) < 2:
        raise FramingError("frame too short")
    return data[1], data[2:]


# --- commands msdr sends -----------------------------------------------------


def cmd_marker(data: bytes = b"") -> bytes:
    """Sync ping; the device echoes `data` back in a MarkerMessage."""
    return encode_command(CMD_MARKER, data)


def cmd_version() -> bytes:
    return encode_command(CMD_VERSION)


def cmd_chan_aa_phy(
    chan: int, phy: PhyMode, aa: int = BLE_ADV_AA, crci: int = BLE_ADV_CRCI
) -> bytes:
    if not 0 <= chan <= 39:
        raise ValueError("channel must be between 0 and 39")
    return encode_command(CMD_CHAN_AA_PHY, pack("<BLBL", chan, aa, int(phy), crci))


def cmd_rssi(rssi_min: int = -128) -> bytes:
    return encode_command(CMD_RSSI, bytes([rssi_min & 0xFF]))


def cmd_mac_clear() -> bytes:
    """Clear any advertiser MAC filter. msdr never sets one: no per-target
    filter means no `hop3` channel-hop command either (that mode requires a
    MAC/IRK target) -- msdr's own `--channel hop` instead dwells and resends
    `cmd_chan_aa_phy`, see `sniffle_capture.py`."""
    return encode_command(CMD_MAC)


def cmd_pause_done(pause: bool = False) -> bytes:
    return encode_command(CMD_PAUSE_DONE, bytes([1 if pause else 0]))


def cmd_follow_disabled() -> bytes:
    """Always sent with enable=False. msdr never follows a connection; no
    other value is ever encoded."""
    return encode_command(CMD_FOLLOW, bytes([0]))


def cmd_auxadv(enable: bool = True) -> bytes:
    return encode_command(CMD_AUXADV, bytes([1 if enable else 0]))


def cmd_crc_valid(validate: bool = True) -> bytes:
    return encode_command(CMD_CRC_VALID, bytes([1 if validate else 0]))


# --- measurement (version probe) --------------------------------------------


def decode_measurement(body: bytes) -> dict | None:
    """A MSG_MEASUREMENT body -> `{major, minor, revision, api_level}` for a
    VersionMeasurement, else None (a different measurement type, or malformed
    -- both read as "not a recognisable Sniffle version reply")."""
    if len(body) < 6 or body[1] != MEASUREMENT_TYPE_VERSION:
        return None
    major, minor, revision, api_level = body[2:6]
    return {"major": major, "minor": minor, "revision": revision, "api_level": api_level}


# --- advertising packet decode (packet_decoder.py, advertising PDUs only) --


@dataclass
class AuxPtr:
    chan: int
    phy: str
    offset_us: int

    @staticmethod
    def parse(raw: bytes) -> AuxPtr:
        chan = raw[0] & 0x3F
        phy_value = raw[2] >> 5
        offset_mult = 300 if raw[0] & 0x80 else 30
        aux_offset = raw[1] + ((raw[2] & 0x1F) << 8)
        return AuxPtr(
            chan=chan,
            phy=PHY_NAME_BY_VALUE.get(phy_value, "coded"),
            offset_us=aux_offset * offset_mult,
        )


#: Legacy (non-extended) PDU type nibble -> event name, primary channels only
#: (packet_decoder.py `AdvertMessage.decode`'s `type_classes` for chan>=37).
#: 3 (SCAN_REQ) and 5 (CONNECT_IND) are intentionally absent: msdr never
#: surfaces a scan or connection request/response, it only receives
#: advertisements.
_LEGACY_EVENTS = {
    0: "ADV_IND",
    1: "ADV_DIRECT_IND",
    2: "ADV_NONCONN_IND",
    4: "SCAN_RSP",
    6: "ADV_SCAN_IND",
}


def decode_packet_message(body: bytes) -> dict | None:
    """One MSG_PACKET body -> a receive-only advertising report shaped like
    `msdr.ble.capture`'s HCI reports plus `phy`/`channel`/`extended`/`aux`,
    or None when the PDU is not an advertisement msdr surfaces (a CRC
    failure, or a scan/connect/data-channel PDU -- msdr does not follow or
    initiate connections, so those are simply dropped here, never decoded
    further)."""
    if len(body) < 10:
        return None
    _ts, length_dir, _event_ctr, rssi, chan_phy = unpack("<LHHbB", body[:10])
    pkt_body = body[10:]
    crc_err = bool(length_dir & 0x4000)
    length = length_dir & 0x3FFF
    if crc_err or len(pkt_body) != length or not pkt_body:
        return None

    phy_value = chan_phy >> 6
    chan = chan_phy & 0x3F
    pdu_type = pkt_body[0] & 0xF
    tx_add = (pkt_body[0] >> 6) & 1
    extended = pdu_type == 7

    if chan >= 37:
        if pdu_type in _LEGACY_EVENTS:
            evt = _LEGACY_EVENTS[pdu_type]
        elif extended:
            evt = "ADV_EXT_IND"
        else:
            return None  # SCAN_REQ / CONNECT_IND
    else:
        if extended:
            evt = "AUX_ADV_IND"
        else:
            return None  # AUX_SCAN_REQ / AUX_CONNECT_REQ / AUX_CONNECT_RSP

    addr: bytes | None = None
    adv_data = b""
    aux: AuxPtr | None = None

    if extended:
        if len(pkt_body) < 4:
            return None
        hdr_body_len = pkt_body[2] & 0x3F
        # The extended header spans pkt_body[3 : 3 + hdr_body_len]; a shorter
        # frame would make the field slices below silently return short
        # (e.g. a 4-byte AdvA) rather than rejecting the packet.
        if len(pkt_body) < 3 + hdr_body_len:
            return None
        hdr_flags = pkt_body[3]
        pos = 4
        if hdr_flags & 0x01:  # AdvA
            addr = pkt_body[pos : pos + 6]
            pos += 6
        if hdr_flags & 0x02:  # TargetA -- not surfaced
            pos += 6
        if hdr_flags & 0x04:  # CTEInfo -- not surfaced
            pos += 1
        if hdr_flags & 0x08:  # AdvDataInfo (DID/SID) -- not surfaced
            pos += 2
        if hdr_flags & 0x10:  # AuxPtr
            if pos + 3 <= len(pkt_body):
                aux = AuxPtr.parse(pkt_body[pos : pos + 3])
            pos += 3
        if hdr_flags & 0x20:  # SyncInfo (periodic advertising) -- not surfaced
            pos += 18
        if hdr_flags & 0x40:  # TxPower -- not surfaced
            pos += 1
        # The extended header (flags + fields + any trailing ACAD) spans
        # pkt_body[3 : 3 + hdr_body_len]; AD structures start after it, not
        # after the last field this port knows (upstream AdvExtIndMessage).
        pos = max(pos, 3 + hdr_body_len)
        adv_data = pkt_body[pos:]
    elif pdu_type == 1:  # ADV_DIRECT_IND: AdvA + TargetA, no AD payload
        if len(pkt_body) < 14:
            return None
        addr = pkt_body[2:8]
    else:
        if len(pkt_body) < 8:
            return None
        addr = pkt_body[2:8]
        adv_data = pkt_body[8:]

    if addr is None:
        return None

    return {
        "addr": ":".join(f"{b:02x}" for b in reversed(addr)),
        "addr_type": "random" if tx_add else "public",
        "evt": evt,
        "rssi": rssi,
        "data": adv_data.hex(),
        "phy": PHY_NAME_BY_VALUE.get(phy_value, "coded"),
        "channel": chan,
        "extended": extended,
        "aux": (
            {"chan": aux.chan, "phy": aux.phy, "offset_us": aux.offset_us}
            if aux is not None
            else None
        ),
    }
