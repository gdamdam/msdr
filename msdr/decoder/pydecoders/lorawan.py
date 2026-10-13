"""Receive-only LoRaWAN (1.0.x) MAC header parsing for CRC-valid LoRa frames.

Parses the cleartext framing only: MHDR, DevAddr, FCtrl, FCnt, FOpts MAC command
names, FPort, payload length, MIC (as hex) and join-request identifiers. It
never decrypts FRMPayload/join-accept and never verifies the MIC (that needs
the session keys, which a passive receiver does not and must not have).

LoRaWAN uses the public sync word 0x34, but a header is also structurally
checkable on its own (major version 0, RFU bits clear, exact lengths for join
frames, FOpts fitting the frame), so frames received with another sync word are
still labelled when they pass those checks -- tagged "plausible_header" rather
than "sync_word" so the weaker evidence is visible.
"""

from __future__ import annotations

from typing import Any

LORAWAN_SYNC = 0x34

_MTYPES = {
    0: "Join Request",
    1: "Join Accept",
    2: "Unconfirmed Data Up",
    3: "Unconfirmed Data Down",
    4: "Confirmed Data Up",
    5: "Confirmed Data Down",
    6: "Rejoin Request (1.1)",
    7: "Proprietary",
}

# (bits of leading prefix incl. terminating 0, NwkID width) per DevAddr/NetID type
# 0..7, from the LoRaWAN Backend Interfaces DevAddr allocation table.
_NWKID_BITS = (6, 6, 9, 11, 12, 13, 15, 17)

# (netid type, NwkID) -> operator. Only allocations that are well documented.
_OPERATORS = {
    (0, 0x00): "Private / experimental",
    (0, 0x13): "The Things Network",
    (6, 0x53): "Helium",
}

# CID -> (name, payload length) for LoRaWAN 1.0 MAC commands.
_MAC_UP = {
    0x02: ("LinkCheckReq", 0),
    0x03: ("LinkADRAns", 1),
    0x04: ("DutyCycleAns", 0),
    0x05: ("RXParamSetupAns", 1),
    0x06: ("DevStatusAns", 2),
    0x07: ("NewChannelAns", 1),
    0x08: ("RXTimingSetupAns", 0),
}
_MAC_DOWN = {
    0x02: ("LinkCheckAns", 2),
    0x03: ("LinkADRReq", 4),
    0x04: ("DutyCycleReq", 1),
    0x05: ("RXParamSetupReq", 4),
    0x06: ("DevStatusReq", 0),
    0x07: ("NewChannelReq", 5),
    0x08: ("RXTimingSetupReq", 1),
}


def devaddr_network(devaddr: int) -> dict[str, Any]:
    """NetID type, NwkID and operator name (if known) from a DevAddr's prefix."""
    net_type = 0
    while net_type < 7 and devaddr & (0x80000000 >> net_type):
        net_type += 1
    width = _NWKID_BITS[net_type]
    # type n has n leading 1s, one 0, then the NwkID.
    shift = 32 - (net_type + 1) - width
    nwkid = (devaddr >> shift) & ((1 << width) - 1)
    return {
        "netid_type": net_type,
        "nwkid": nwkid,
        "network_operator": _OPERATORS.get((net_type, nwkid)),
    }


def _eui(raw: bytes) -> str:
    # EUIs go over the air little-endian; operators print them big-endian.
    return raw[::-1].hex().upper()


def _mac_commands(opts: bytes, uplink: bool) -> list[str]:
    table = _MAC_UP if uplink else _MAC_DOWN
    names: list[str] = []
    i = 0
    while i < len(opts):
        entry = table.get(opts[i])
        if entry is None or i + 1 + entry[1] > len(opts):
            # Unknown/proprietary CID (0x80+) or truncated: lengths past it are
            # unknowable, so stop rather than mislabel the remaining bytes.
            names.append(f"unknown(0x{opts[i]:02x})")
            break
        names.append(entry[0])
        i += 1 + entry[1]
    return names


def parse_lorawan(payload: bytes, *, sync_word: int | None = None) -> dict[str, Any] | None:
    """Return LoRaWAN fields for `payload`, or None if it is not a plausible frame."""
    if len(payload) < 5:
        return None
    mhdr = payload[0]
    mtype = mhdr >> 5
    if (mhdr >> 2) & 0x07 or mhdr & 0x03:
        return None  # RFU bits set or major != 0 (LoRaWAN R1)
    n = len(payload)
    out: dict[str, Any] = {
        "lorawan_mtype": _MTYPES[mtype],
        "lorawan_major": mhdr & 0x03,
        "lorawan_evidence": "sync_word" if sync_word == LORAWAN_SYNC else "plausible_header",
    }
    if mtype == 0:
        if n != 23:
            return None
        out.update(
            lorawan_join_eui=_eui(payload[1:9]),
            lorawan_dev_eui=_eui(payload[9:17]),
            lorawan_dev_nonce=int.from_bytes(payload[17:19], "little"),
            lorawan_mic=payload[19:23].hex(),
        )
        out["lorawan_summary"] = f"Join Request DevEUI {out['lorawan_dev_eui']}"
        return out
    if mtype == 1:
        if n not in (17, 33):
            return None
        out.update(lorawan_encrypted_bytes=n - 1, lorawan_cflist=n == 33)
        out["lorawan_summary"] = "Join Accept (encrypted)"
        return out
    if mtype in (2, 3, 4, 5):
        if n < 12:
            return None
        uplink = mtype in (2, 4)
        fctrl = payload[5]
        fopts_len = fctrl & 0x0F
        header_end = 8 + fopts_len
        if header_end + 4 > n:
            return None
        devaddr = int.from_bytes(payload[1:5], "little")
        net = devaddr_network(devaddr)
        out.update(
            lorawan_dev_addr=f"{devaddr:08X}",
            lorawan_netid_type=net["netid_type"],
            lorawan_nwkid=net["nwkid"],
            lorawan_network_operator=net["network_operator"],
            lorawan_adr=bool(fctrl & 0x80),
            lorawan_ack=bool(fctrl & 0x20),
            lorawan_fopts_len=fopts_len,
            lorawan_fcnt=int.from_bytes(payload[6:8], "little"),
            lorawan_mic=payload[-4:].hex(),
        )
        if uplink:
            out["lorawan_adr_ack_req"] = bool(fctrl & 0x40)
            out["lorawan_class_b"] = bool(fctrl & 0x10)
        else:
            out["lorawan_fpending"] = bool(fctrl & 0x10)
        if fopts_len:
            out["lorawan_mac_commands"] = _mac_commands(payload[8:header_end], uplink)
        body = payload[header_end:-4]
        if body:
            out["lorawan_fport"] = body[0]
            out["lorawan_payload_bytes"] = len(body) - 1
            # FPort 0 carries encrypted MAC commands instead of application data.
            out["lorawan_payload_kind"] = "mac_commands" if body[0] == 0 else "application"
        else:
            out["lorawan_payload_bytes"] = 0
        out["lorawan_summary"] = f"{_MTYPES[mtype]} DevAddr {out['lorawan_dev_addr']} FCnt " + str(
            out["lorawan_fcnt"]
        )
        return out
    if mtype == 7:
        # Proprietary has no defined layout; only trust it when the sync word says LoRaWAN.
        if sync_word != LORAWAN_SYNC:
            return None
        out["lorawan_payload_bytes"] = n - 1
        out["lorawan_summary"] = "Proprietary"
        return out
    return None  # Rejoin Request (1.1) is not parsed
