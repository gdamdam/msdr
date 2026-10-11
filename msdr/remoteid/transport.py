# SPDX-License-Identifier: GPL-3.0-only
"""Pull Open Drone ID message bytes out of already-parsed Wi-Fi management
frames and BLE advertisements, and decode a recorded Wi-Fi pcap session into
`tracker.Tracker` output offline (docs/remote-id.md).

The ASD-STAN Wi-Fi vendor IE framing (OUI `FA:0B:BC`, OUI type `0x0D`,
message counter + Message Pack) is derived from opendroneid-core-c
(Apache-2.0, https://github.com/opendroneid/opendroneid-core-c) at commit
6484f26545d4f012682524e2d843fab0fbdc0b34: libopendroneid/wifi.c
(`odid_wifi_build_message_pack_beacon_frame`) and odid_wifi.h
(`struct ODID_service_info`). The BLE legacy service-data framing (UUID
0xFFFA, app code 0x0D, counter + one message) follows the same ASTM F3411 /
ASD-STAN specification; opendroneid-core-c's own Bluetooth encoder was not
among the files downloaded for this port (see docs/remote-id.md
"Provenance"), so it is cross-checked against `msdr/ble/adv.py`'s existing
`REMOTE_ID_UUID`/`REMOTE_ID_APP_CODE` constants (0.59.0) instead.

Wi-Fi NAN (Neighbor Awareness Networking / Wi-Fi Aware) transport: a
Public Action frame (category 0x04, action 0x09 vendor specific) with the
Wi-Fi Alliance OUI 50:6F:9A and OUI type 0x13, whose NAN attributes include
a Service Descriptor Attribute (id 0x03) for the Open Drone ID service. Its
service info is the same message counter + Message Pack as the Beacon IE.
The layout follows opendroneid-core-c's libopendroneid/wifi.c
(`odid_wifi_build_nan_sync_beacon_frame` / NAN action frame builder) and the
Wi-Fi Alliance NAN specification's attribute and service-control layout;
the service ID is computed as the NAN spec defines it (first 6 bytes of
SHA-256 of the lowercase service name) rather than copied.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from msdr.ble import adv as ble_adv
from msdr.remoteid import messages, tracker
from msdr.wifi import dot11, pcap, radiotap

#: ASD-STAN OUI for the Remote ID vendor-specific information element.
WIFI_OUI = b"\xfa\x0b\xbc"
WIFI_OUI_TYPE = 0x0D

#: NAN Service Discovery Frame framing (Public Action, Wi-Fi Alliance OUI).
_ACTION_FRAME_CONTROL0 = 0xD0  # management, subtype 13 (action)
_PUBLIC_ACTION = 0x04
_VENDOR_SPECIFIC_ACTION = 0x09
WFA_OUI = b"\x50\x6f\x9a"
NAN_OUI_TYPE = 0x13
NAN_SDA_ATTR = 0x03
#: Service ID = first 48 bits of SHA-256 of the service name (NAN spec).
NAN_SERVICE_ID = hashlib.sha256(b"org.opendroneid.remoteid").digest()[:6]
# Service Control bits (NAN spec, Service Descriptor Attribute).
_SC_MATCHING_FILTER = 0x04
_SC_RESPONSE_FILTER = 0x08
_SC_SERVICE_INFO = 0x10
_SC_BINDING_BITMAP = 0x40


@dataclass(frozen=True)
class WifiRemoteId:
    message_counter: int
    pack: messages.MessagePack | messages.RemoteIdError


@dataclass(frozen=True)
class BleRemoteId:
    message_counter: int
    message: messages.DecodedMessage | messages.MessagePack | messages.RemoteIdError


def extract_wifi_remote_id(frame: dot11.ManagementFrame) -> WifiRemoteId | None:
    """Scan a parsed Beacon/probe-response's information elements for the
    ASD-STAN Remote ID vendor IE. Returns None if the frame carries no such
    IE at all (an ordinary Wi-Fi network, not a drone) -- that is not an
    error. A present-but-broken IE decodes to a `WifiRemoteId` whose `pack`
    is a `RemoteIdError`, so callers can tell "no Remote ID here" apart from
    "Remote ID here, but malformed"."""
    for ie in frame.ies:
        if ie.id != dot11.IE_VENDOR_SPECIFIC or len(ie.data) < 4:
            continue
        if ie.data[:3] != WIFI_OUI or ie.data[3] != WIFI_OUI_TYPE:
            continue
        payload = ie.data[4:]
        if len(payload) < 1:
            return WifiRemoteId(
                message_counter=-1,
                pack=messages.RemoteIdError("truncated", "vendor IE missing message counter"),
            )
        counter = payload[0]
        pack = messages.decode_message_pack(payload[1:])
        return WifiRemoteId(message_counter=counter, pack=pack)
    return None


def extract_ble_remote_id(adv: ble_adv.Advertisement) -> BleRemoteId | None:
    """Decode UUID 0xFFFA service data from HCI or Sniffle advertisements.

    The message type selects a single legacy message or an extended
    Message Pack. Returns None when no Remote ID service data is present.
    """
    data = adv.service_data.get(ble_adv.REMOTE_ID_UUID)
    if not data or data[0] != ble_adv.REMOTE_ID_APP_CODE:
        return None
    payload = data[1:]
    if len(payload) < 1:
        return BleRemoteId(
            message_counter=-1,
            message=messages.RemoteIdError("truncated", "service data missing message counter"),
        )
    counter = payload[0]
    raw = payload[1:]
    message = (
        messages.decode_message_pack(raw)
        if raw and raw[0] >> 4 == messages.MESSAGETYPE_PACKED
        else messages.decode_message(raw)
    )
    return BleRemoteId(message_counter=counter, message=message)


def extract_ble_extended_remote_id(service_data: bytes) -> BleRemoteId | None:
    """BLE 5 extended advertising's larger PDU carries a full message
    counter + Message Pack in the same 0xFFFA service data, instead of one
    message. `service_data` is the raw AD value (app code byte included).
    The live advertisement entry point above also dispatches these packs."""
    if not service_data or service_data[0] != ble_adv.REMOTE_ID_APP_CODE:
        return None
    payload = service_data[1:]
    if len(payload) < 1:
        return BleRemoteId(
            message_counter=-1,
            message=messages.RemoteIdError("truncated", "service data missing message counter"),
        )
    counter = payload[0]
    pack = messages.decode_message_pack(payload[1:])
    return BleRemoteId(message_counter=counter, message=pack)


def _sda_service_info(body: bytes) -> bytes | None:
    """Service info of a Remote ID Service Descriptor Attribute body, or None
    when it's another service or carries no service info."""
    if len(body) < 9 or body[:6] != NAN_SERVICE_ID:
        return None
    control = body[8]
    i = 9
    if control & _SC_BINDING_BITMAP:
        i += 2
    for bit in (_SC_MATCHING_FILTER, _SC_RESPONSE_FILTER):
        if control & bit:
            if i >= len(body):
                return None
            i += 1 + body[i]
    if not control & _SC_SERVICE_INFO or i >= len(body):
        return None
    length = body[i]
    info = body[i + 1 : i + 1 + length]
    return info if len(info) == length else None


def extract_nan_remote_id(
    frame_bytes: bytes, *, fcs_present: bool = False
) -> tuple[str, WifiRemoteId] | None:
    """(transmitter MAC, Remote ID) from a raw 802.11 NAN Service Discovery
    Frame carrying the Open Drone ID service, else None -- any other action
    frame, or anything that isn't one, is not an error."""
    data = frame_bytes[:-4] if fcs_present else frame_bytes
    if len(data) < 30 or data[0] != _ACTION_FRAME_CONTROL0:
        return None
    body = data[24:]
    if (
        body[0] != _PUBLIC_ACTION
        or body[1] != _VENDOR_SPECIFIC_ACTION
        or body[2:5] != WFA_OUI
        or body[5] != NAN_OUI_TYPE
    ):
        return None
    source = ":".join(f"{b:02x}" for b in data[10:16])
    i = 6
    while i + 3 <= len(body):
        attr_id = body[i]
        length = int.from_bytes(body[i + 1 : i + 3], "little")
        attr = body[i + 3 : i + 3 + length]
        if len(attr) < length:
            return None
        if attr_id == NAN_SDA_ATTR:
            info = _sda_service_info(attr)
            if info is not None:
                if not info:
                    return source, WifiRemoteId(
                        message_counter=-1,
                        pack=messages.RemoteIdError("truncated", "NAN service info is empty"),
                    )
                return source, WifiRemoteId(
                    message_counter=info[0], pack=messages.decode_message_pack(info[1:])
                )
        i += 3 + length
    return None


def decode_wifi_records(records: Iterable[pcap.Record]) -> tracker.Tracker:
    """Replay pcap records (radiotap + 802.11) through the same parse
    pipeline `msdr/wifi/replay.py` and `NetworkTable.ingest` use, feeding
    every Remote ID report found into a fresh `Tracker`. Each record's own
    capture timestamp (`record.ts`) is used as the receive time -- never
    wall-clock `time.time()` -- so decoding the same capture twice is
    reproducible."""
    t = tracker.Tracker()
    for record in records:
        if record.linktype != pcap.LINKTYPE_IEEE802_11_RADIOTAP:
            continue
        rt = radiotap.parse(record.data)
        if isinstance(rt, radiotap.ParseError):
            continue
        fcs_present = rt.flags.fcs_present if rt.flags else False
        frame_bytes = record.data[rt.header_length :]
        result = dot11.parse(frame_bytes, fcs_present=fcs_present)
        if isinstance(result, dot11.Dot11Error):
            nan = extract_nan_remote_id(frame_bytes, fcs_present=fcs_present)
            if nan is not None and not isinstance(nan[1].pack, messages.RemoteIdError):
                t.ingest_wifi_pack(
                    f"wifi:{nan[0]}", nan[1].message_counter, nan[1].pack, received_at=record.ts
                )
            continue
        wifi_rid = extract_wifi_remote_id(result)
        if wifi_rid is None or isinstance(wifi_rid.pack, messages.RemoteIdError):
            continue
        t.ingest_wifi_pack(
            f"wifi:{result.source}",
            wifi_rid.message_counter,
            wifi_rid.pack,
            received_at=record.ts,
        )
    return t


def decode_wifi_session(store, session_id: str) -> tracker.Tracker:
    """Decode a recorded/imported Wi-Fi session (`msdr.wifi.sessions.
    WifiSessionStore`) offline. Raises `KeyError` if the session does not
    exist, matching `WifiSessionStore.export`/`replay_table`."""
    return decode_wifi_records(store.session_records(session_id))


def iter_vendor_ie_frames(frames: Iterable[dot11.ManagementFrame]) -> Iterator[WifiRemoteId]:
    """Convenience for tests/tools: every Remote ID vendor IE found across a
    sequence of already-parsed frames (skips frames with none)."""
    for frame in frames:
        wifi_rid = extract_wifi_remote_id(frame)
        if wifi_rid is not None:
            yield wifi_rid
