"""Live decoding: the `NetworkTable`/`AdvertiserTable` optional `.remoteid`
sink feeds a `Tracker` from the same trusted ingest path as device history
(docs/remote-id.md "Live")."""

from __future__ import annotations

import json
from pathlib import Path

from msdr.ble import adv as ble_adv
from msdr.ble.observations import AdvertiserTable
from msdr.remoteid import transport
from msdr.remoteid.tracker import Tracker
from msdr.wifi import radiotap
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import LINKTYPE_IEEE802_11_RADIOTAP, Record
from tests.wifi import fixtures

VECTORS = {
    v["name"]: v
    for v in json.loads((Path(__file__).parent / "data" / "vectors.json").read_text())["vectors"]
}


def _wifi_record(ts, rt_bytes, frame_bytes) -> Record:
    return Record(
        ts=ts, linktype=LINKTYPE_IEEE802_11_RADIOTAP, data=rt_bytes + frame_bytes, orig_len=0
    )


def _vendor_ie_payload(counter: int, message_pack_hex: str) -> bytes:
    return (
        transport.WIFI_OUI
        + bytes([transport.WIFI_OUI_TYPE, counter])
        + bytes.fromhex(message_pack_hex)
    )


def test_wifi_table_feeds_tracker_on_good_fcs_beacon():
    table = NetworkTable(configured_channel=6)
    tracker = Tracker()
    table.remoteid = tracker

    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    frame = fixtures.build_beacon_frame(
        bssid="02:aa:bb:cc:dd:ee",
        extra_ies=[(221, _vendor_ie_payload(3, message_pack_hex))],
    )
    rt_bytes = fixtures.build_radiotap(flags=radiotap.FLAG_FCS_AT_END)
    frame = fixtures.append_fcs(frame)
    rt = radiotap.parse(rt_bytes)
    table.ingest(_wifi_record(1_700_000_000.0, rt_bytes, frame), rt)

    groups = tracker.groups(now=1_700_000_000.0)
    assert len(groups) == 1
    track = groups[0].tracks[0]
    assert track.address == "wifi:02:aa:bb:cc:dd:ee"
    assert "wifi" in track.sources
    assert track.uas_id_text == VECTORS["message_pack_7"]["decoded"]["basic_id_uas_id"]
    assert track.drone_position is not None
    assert track.drone_position.received_at == 1_700_000_000.0


def test_wifi_table_without_remote_id_ie_does_not_touch_tracker():
    table = NetworkTable(configured_channel=6)
    tracker = Tracker()
    table.remoteid = tracker

    frame = fixtures.append_fcs(fixtures.build_beacon_frame(bssid="02:11:22:33:44:55"))
    rt_bytes = fixtures.build_radiotap(flags=radiotap.FLAG_FCS_AT_END)
    table.ingest(_wifi_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))

    assert tracker.groups() == []


def test_wifi_table_bad_fcs_frame_never_reaches_tracker():
    table = NetworkTable(configured_channel=6)
    tracker = Tracker()
    table.remoteid = tracker

    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    frame = fixtures.append_bad_fcs(
        fixtures.build_beacon_frame(
            bssid="02:aa:bb:cc:dd:ee",
            extra_ies=[(221, _vendor_ie_payload(1, message_pack_hex))],
        )
    )
    rt_bytes = fixtures.build_radiotap(flags=radiotap.FLAG_FCS_AT_END | radiotap.FLAG_BAD_FCS)
    table.ingest(_wifi_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))

    assert tracker.groups() == []


def test_wifi_table_no_sink_set_is_a_noop():
    table = NetworkTable(configured_channel=6)  # table.remoteid left at its None default
    message_pack_hex = VECTORS["message_pack_7"]["hex"]
    frame = fixtures.append_fcs(
        fixtures.build_beacon_frame(
            bssid="02:aa:bb:cc:dd:ee",
            extra_ies=[(221, _vendor_ie_payload(1, message_pack_hex))],
        )
    )
    rt_bytes = fixtures.build_radiotap(flags=radiotap.FLAG_FCS_AT_END)
    table.ingest(_wifi_record(1.0, rt_bytes, frame), radiotap.parse(rt_bytes))  # must not raise
    rows, total = table.rows()
    assert total == 1


# --------------------------------------------------------------------------
# BLE legacy service-data advertisement -> AdvertiserTable.ingest -> Tracker.
# --------------------------------------------------------------------------


def _ble_remote_id_payload(counter: int, message_hex: str) -> bytes:
    service_data = bytes([ble_adv.REMOTE_ID_APP_CODE, counter]) + bytes.fromhex(message_hex)
    return (
        bytes([1 + 2 + len(service_data), 0x16])  # AD length, type = service data 16-bit UUID
        + bytes.fromhex("faff")  # UUID 0xFFFA, little-endian
        + service_data
    )


def test_ble_table_feeds_tracker_on_remote_id_service_data():
    table = AdvertiserTable()
    tracker = Tracker()
    table.remoteid = tracker

    basic_id_hex = VECTORS["basic_id_serial"]["hex"]
    data = _ble_remote_id_payload(5, basic_id_hex)
    table.ingest(
        {"ts": 1_700_000_000.0, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public",
         "evt": "ADV_NONCONN_IND", "rssi": -60, "data": data.hex()}
    )

    groups = tracker.groups(now=1_700_000_000.0)
    assert len(groups) == 1
    track = groups[0].tracks[0]
    assert track.address == "ble:aa:bb:cc:dd:ee:ff"
    assert "ble" in track.sources
    assert track.uas_id_text == VECTORS["basic_id_serial"]["decoded"]["uas_id"]


def test_ble_extended_table_dispatches_message_pack_to_tracker():
    table = AdvertiserTable()
    tracker = Tracker()
    table.remoteid = tracker

    # BLE extended advertising can carry the full pack in one service-data AD
    # structure. Keep the AD envelope identical to legacy service data; the
    # Remote ID message-type nibble selects message-pack decoding. The extended
    # metadata here exercises the real report shape without controlling dispatch.
    pack = bytes.fromhex(VECTORS["message_pack_7"]["hex"])
    service_data = bytes([ble_adv.REMOTE_ID_APP_CODE, 7]) + pack
    data = bytes([1 + 2 + len(service_data), 0x16]) + bytes.fromhex("faff") + service_data
    table.ingest(
        {"ts": 1_700_000_000.0, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public",
         "evt": "ADV_EXT_IND", "rssi": -60, "data": data.hex(), "extended": True}
    )

    groups = tracker.groups(now=1_700_000_000.0)
    assert len(groups) == 1
    track = groups[0].tracks[0]
    assert track.address == "ble:aa:bb:cc:dd:ee:ff"
    assert track.uas_id_text == VECTORS["message_pack_7"]["decoded"]["basic_id_uas_id"]
    assert track.drone_position is not None


def test_ble_table_without_remote_id_service_data_does_not_touch_tracker():
    table = AdvertiserTable()
    tracker = Tracker()
    table.remoteid = tracker

    table.ingest(
        {"ts": 1.0, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public",
         "evt": "ADV_IND", "rssi": -50, "data": "020106"}  # flags only
    )

    assert tracker.groups() == []


def test_ble_table_no_sink_set_is_a_noop():
    table = AdvertiserTable()  # table.remoteid left at its None default
    basic_id_hex = VECTORS["basic_id_serial"]["hex"]
    data = _ble_remote_id_payload(1, basic_id_hex)
    table.ingest(
        {"ts": 1.0, "addr": "aa:bb:cc:dd:ee:ff", "addr_type": "public",
         "evt": "ADV_NONCONN_IND", "rssi": -60, "data": data.hex()}
    )  # must not raise
    assert len(table) == 1
