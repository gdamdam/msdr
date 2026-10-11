"""msdr/remoteid/transport.py: pulling Remote ID message bytes out of
already-parsed Wi-Fi frames and BLE advertisements, and offline decode of a
recorded Wi-Fi pcap session (docs/remote-id.md)."""

from __future__ import annotations

import json
from pathlib import Path

from msdr.ble import adv as ble_adv
from msdr.remoteid import messages, transport
from msdr.wifi import dot11, pcap
from msdr.wifi.pcap import Record
from msdr.wifi.sessions import WifiSessionStore
from tests.wifi import fixtures

VECTORS = {
    v["name"]: v
    for v in json.loads((Path(__file__).parent / "data" / "vectors.json").read_text())["vectors"]
}


def _vendor_ie_payload(counter: int, message_pack_hex: str) -> bytes:
    return (
        transport.WIFI_OUI
        + bytes([transport.WIFI_OUI_TYPE, counter])
        + bytes.fromhex(message_pack_hex)
    )


def _beacon_management_frame(
    *, counter: int = 1, message_pack_hex: str | None = None
) -> dot11.ManagementFrame:
    message_pack_hex = message_pack_hex or VECTORS["message_pack_7"]["hex"]
    frame_bytes = fixtures.build_beacon_frame(
        bssid="02:11:22:33:44:55",
        extra_ies=[(221, _vendor_ie_payload(counter, message_pack_hex))],
    )
    result = dot11.parse(frame_bytes)
    assert isinstance(result, dot11.ManagementFrame), result
    return result


def test_extract_wifi_remote_id_from_vendor_ie():
    frame = _beacon_management_frame(counter=42)
    wifi_rid = transport.extract_wifi_remote_id(frame)
    assert wifi_rid is not None
    assert wifi_rid.message_counter == 42
    assert isinstance(wifi_rid.pack, messages.MessagePack)
    assert wifi_rid.pack.message_count == 7
    assert not wifi_rid.pack.errors


def test_extract_wifi_remote_id_absent_is_none():
    frame_bytes = fixtures.build_beacon_frame(bssid="02:11:22:33:44:66")
    result = dot11.parse(frame_bytes)
    assert isinstance(result, dot11.ManagementFrame)
    assert transport.extract_wifi_remote_id(result) is None


def test_extract_wifi_remote_id_wrong_oui_or_type_is_none():
    frame_bytes = fixtures.build_beacon_frame(
        bssid="02:11:22:33:44:77",
        extra_ies=[(221, b"\x00\x50\xf2\x0d" + bytes(4))],  # Microsoft OUI, not ASD-STAN
    )
    result = dot11.parse(frame_bytes)
    assert isinstance(result, dot11.ManagementFrame)
    assert transport.extract_wifi_remote_id(result) is None


def test_extract_wifi_remote_id_truncated_vendor_ie():
    frame_bytes = fixtures.build_beacon_frame(
        bssid="02:11:22:33:44:88",
        extra_ies=[(221, transport.WIFI_OUI + bytes([transport.WIFI_OUI_TYPE]))],  # no counter byte
    )
    result = dot11.parse(frame_bytes)
    assert isinstance(result, dot11.ManagementFrame)
    wifi_rid = transport.extract_wifi_remote_id(result)
    assert wifi_rid is not None
    assert isinstance(wifi_rid.pack, messages.RemoteIdError)
    assert wifi_rid.pack.kind == "truncated"


def test_extract_ble_remote_id_legacy():
    basic_id_hex = VECTORS["basic_id_serial"]["hex"]
    service_data = bytes([ble_adv.REMOTE_ID_APP_CODE, 7]) + bytes.fromhex(basic_id_hex)
    payload = (
        bytes([1 + 2 + len(service_data), 0x16])  # AD length, AD type = service data 16-bit UUID
        + bytes.fromhex("faff")  # UUID 0xFFFA, little-endian
        + service_data
    )
    advertisement = ble_adv.parse(payload)
    assert advertisement.remote_id

    ble_rid = transport.extract_ble_remote_id(advertisement)
    assert ble_rid is not None
    assert ble_rid.message_counter == 7
    assert isinstance(ble_rid.message, messages.BasicId)
    assert ble_rid.message.uas_id_text == VECTORS["basic_id_serial"]["decoded"]["uas_id"]


def test_extract_ble_remote_id_absent_is_none():
    advertisement = ble_adv.parse(bytes.fromhex("020106"))  # flags only, no service data
    assert transport.extract_ble_remote_id(advertisement) is None


def test_extract_ble_remote_id_truncated():
    service_data = bytes([ble_adv.REMOTE_ID_APP_CODE])  # app code, no counter byte at all
    payload = bytes([1 + 2 + len(service_data), 0x16]) + bytes.fromhex("faff") + service_data
    advertisement = ble_adv.parse(payload)
    ble_rid = transport.extract_ble_remote_id(advertisement)
    assert ble_rid is not None
    assert isinstance(ble_rid.message, messages.RemoteIdError)
    assert ble_rid.message.kind == "truncated"


def test_extract_ble_extended_remote_id_message_pack():
    # Not reachable from any capture backend yet (docs/remote-id.md), but the
    # parser supports it for the future Sniffle backend: same service-data
    # framing, a full Message Pack instead of one message.
    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    service_data = bytes([ble_adv.REMOTE_ID_APP_CODE, 3]) + bytes.fromhex(message_pack_hex)
    ble_rid = transport.extract_ble_extended_remote_id(service_data)
    assert ble_rid is not None
    assert ble_rid.message_counter == 3
    assert isinstance(ble_rid.message, messages.MessagePack)
    assert ble_rid.message.message_count == 7


# --------------------------------------------------------------------------
# Offline decode of a Wi-Fi pcap session.
# --------------------------------------------------------------------------


def _wifi_record(ts: float, *, counter: int, message_pack_hex: str, bssid: str) -> Record:
    rt = fixtures.build_radiotap(channel_mhz=2437, flags=0x10)
    frame = fixtures.append_fcs(
        fixtures.build_beacon_frame(
            bssid=bssid, extra_ies=[(221, _vendor_ie_payload(counter, message_pack_hex))]
        )
    )
    data = rt + frame
    return Record(ts=ts, linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP, data=data, orig_len=len(data))


def test_decode_wifi_records_builds_tracker():
    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    records = [
        _wifi_record(
            1_700_000_000.0, counter=1, message_pack_hex=message_pack_hex, bssid="02:aa:bb:cc:dd:ee"
        )
    ]
    t = transport.decode_wifi_records(records)
    groups = t.groups(now=1_700_000_010.0)
    assert len(groups) == 1
    track = groups[0].tracks[0]
    assert "wifi" in track.sources
    assert track.uas_id_text == VECTORS["message_pack_7"]["decoded"]["basic_id_uas_id"]
    assert track.drone_position is not None
    assert track.drone_position.received_at == 1_700_000_000.0  # record.ts, not wall-clock


def test_decode_wifi_session_reproducible(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    session = store.new_session(
        channel=6, receiver_id="wifi:test", helper_commit=None, max_bytes=10**9, max_seconds=3600
    )
    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    record = _wifi_record(
        1_700_000_100.0, counter=5, message_pack_hex=message_pack_hex, bssid="02:cc:dd:ee:ff:00"
    )
    session.write_record(record)
    meta = session.finalize()

    first = transport.decode_wifi_session(store, meta["id"])
    second = transport.decode_wifi_session(store, meta["id"])

    def _snapshot(t):
        groups = t.groups(now=1_700_000_200.0)
        return [
            (g.key, g.conflict, g.tracks[0].uas_id_text, g.tracks[0].drone_position.received_at)
            for g in groups
        ]

    assert _snapshot(first) == _snapshot(second)


def test_decode_wifi_session_missing_raises_keyerror(tmp_path):
    store = WifiSessionStore(tmp_path / "wifi")
    try:
        transport.decode_wifi_session(store, "does-not-exist")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for a missing session")


def _nan_frame(counter: int, message_pack_hex: str, *, service_id: bytes | None = None,
               control: int = 0x10, fcs: bool = False) -> bytes:
    """A NAN Service Discovery Frame (Public Action, WFA OUI type 0x13)."""
    header = (
        bytes([0xD0, 0x00, 0x00, 0x00])
        + bytes.fromhex("516f9a010000")  # NAN broadcast address
        + bytes.fromhex("02aabbccdd01")  # transmitter: the drone
        + bytes.fromhex("506f9a01aa55")  # cluster id
        + b"\x00\x00"
    )
    info = bytes([counter]) + bytes.fromhex(message_pack_hex)
    sda = (service_id or transport.NAN_SERVICE_ID) + bytes([0x01, 0x00, control])
    if control & 0x04:  # matching filter present: skipped by the parser
        sda += bytes([2, 0xAB, 0xCD])
    sda += bytes([len(info)]) + info
    other_sda = bytes(6) + bytes([0x02, 0x00, 0x10, 1, 0x99])  # another service
    attrs = (
        bytes([0x00, 2, 0, 0x80, 0x01])  # Master Indication attribute
        + bytes([0x03]) + len(other_sda).to_bytes(2, "little") + other_sda
        + bytes([0x03]) + len(sda).to_bytes(2, "little") + sda
    )
    action = bytes([0x04, 0x09]) + transport.WFA_OUI + bytes([transport.NAN_OUI_TYPE])
    frame = header + action + attrs
    return fixtures.append_fcs(frame) if fcs else frame


def test_nan_service_id_is_the_spec_hash():
    # First 48 bits of SHA-256("org.opendroneid.remoteid") -- the constant
    # opendroneid-core-c uses.
    assert transport.NAN_SERVICE_ID.hex() == "8869199d9209"


def test_extract_nan_remote_id():
    pack_hex = VECTORS["message_pack_7"]["hex"]
    source, rid = transport.extract_nan_remote_id(_nan_frame(7, pack_hex, control=0x14))
    assert source == "02:aa:bb:cc:dd:01"
    assert rid.message_counter == 7
    assert isinstance(rid.pack, messages.MessagePack) and rid.pack.message_count == 7
    # FCS trailer stripped when radiotap says it's present.
    assert transport.extract_nan_remote_id(_nan_frame(7, pack_hex, fcs=True), fcs_present=True)


def test_extract_nan_ignores_other_services_and_frames():
    pack_hex = VECTORS["message_pack_7"]["hex"]
    assert transport.extract_nan_remote_id(_nan_frame(1, pack_hex, service_id=bytes(6))) is None
    assert transport.extract_nan_remote_id(_nan_frame(1, pack_hex, control=0x00)) is None
    beacon = fixtures.build_beacon_frame(bssid="02:11:22:33:44:77")
    assert transport.extract_nan_remote_id(beacon) is None
    assert transport.extract_nan_remote_id(b"\xd0\x00") is None


def test_decode_wifi_records_includes_nan():
    pack_hex = VECTORS["message_pack_7"]["hex"]
    rt = fixtures.build_radiotap(channel_mhz=2437, flags=0x10)
    data = rt + _nan_frame(3, pack_hex, fcs=True)
    record = Record(ts=1_700_000_000.0, linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP,
                    data=data, orig_len=len(data))
    groups = transport.decode_wifi_records([record]).groups(now=1_700_000_005.0)
    track = groups[0].tracks[0]
    assert track.uas_id_text == VECTORS["message_pack_7"]["decoded"]["basic_id_uas_id"]


def test_live_network_table_feeds_nan_to_tracker():
    from msdr.remoteid import tracker
    from msdr.wifi import radiotap
    from msdr.wifi.observations import NetworkTable

    pack_hex = VECTORS["message_pack_7"]["hex"]
    data = fixtures.build_radiotap(channel_mhz=2437, flags=0x10) + _nan_frame(4, pack_hex, fcs=True)
    record = Record(ts=1_700_000_000.0, linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP,
                    data=data, orig_len=len(data))
    table = NetworkTable(configured_channel=6)
    table.remoteid = tracker.Tracker()
    table.ingest(record, radiotap.parse(record.data))
    groups = table.remoteid.groups(now=1_700_000_005.0)
    assert groups and groups[0].tracks[0].uas_id_text == (
        VECTORS["message_pack_7"]["decoded"]["basic_id_uas_id"]
    )
    assert table.rows()[1] == 0  # an action frame never becomes a "network"
