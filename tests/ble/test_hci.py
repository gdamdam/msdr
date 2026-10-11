"""HCI encoding/parsing against bytes captured from the BCM20702A0 on
2026-10-02 (docs/ble.md): no hardware involved."""

from __future__ import annotations

import struct

from msdr.ble import hci


def test_commands_are_passive_and_well_formed():
    params = hci.le_set_scan_parameters()
    assert params == bytes.fromhex("0b2007") + struct.pack("<BHHBB", 0x00, 0x0010, 0x0010, 0, 0)
    assert params[3] == hci.SCAN_TYPE_PASSIVE
    assert hci.le_set_scan_enable(True) == bytes.fromhex("0c2002") + b"\x01\x00"
    assert hci.le_set_scan_enable(False) == bytes.fromhex("0c2002") + b"\x00\x00"
    assert hci.command(hci.OP_RESET) == bytes.fromhex("030c00")


def test_command_complete_and_local_version():
    # Command Complete for Read_Local_Version_Information as the dongle sent it.
    ev = hci.Event(0x0E, bytes.fromhex("01 0110 00 06 0020 06 0f00 0e41".replace(" ", "")))
    done = hci.parse_command_complete(ev)
    assert done is not None and done.opcode == hci.OP_READ_LOCAL_VERSION and done.status == 0
    lv = hci.parse_local_version(done.returned)
    assert (
        lv.hci_version,
        lv.hci_revision,
        lv.lmp_version,
        lv.manufacturer,
        lv.lmp_subversion,
    ) == (6, 0x2000, 6, 15, 0x410E)
    assert lv.manufacturer_name == "Broadcom"
    # Command Status carries the opcode at a different offset.
    status = hci.parse_command_complete(
        hci.Event(0x0F, bytes.fromhex("00 01 030c".replace(" ", "")))
    )
    assert status is not None and status.opcode == hci.OP_RESET and status.status == 0
    assert hci.parse_command_complete(hci.Event(0x3E, b"\x02\x00")) is None


def _adv_report_event(*reports: tuple[int, int, str, bytes, int]) -> hci.Event:
    body = bytes([hci.SUBEVT_LE_ADVERTISING_REPORT, len(reports)])
    for evt, atype, addr, data, rssi in reports:
        raw_addr = bytes.fromhex(addr.replace(":", ""))[::-1]
        body += bytes([evt, atype]) + raw_addr + bytes([len(data)]) + data + struct.pack("<b", rssi)
    return hci.Event(hci.EVT_LE_META, body)


def test_advertising_reports_parse_including_multi_report_events():
    data = bytes.fromhex("02011a0cff4c001007361f84b53cae28020a00")
    ev = _adv_report_event(
        (0, 1, "4a:34:3f:50:52:88", data, -66), (3, 0, "00:11:22:33:44:66", b"\x02\x01\x06", 127)
    )
    reports = hci.parse_advertising_reports(ev)
    assert [r.address for r in reports] == ["4a:34:3f:50:52:88", "00:11:22:33:44:66"]
    assert reports[0].event_name == "ADV_IND" and reports[0].address_type_name == "random"
    assert reports[0].data == data and reports[0].rssi == -66
    assert reports[1].event_name == "ADV_NONCONN_IND" and reports[1].address_type_name == "public"
    assert reports[1].rssi is None  # 127 = not available


def test_truncated_report_yields_what_was_complete():
    ev = _adv_report_event((0, 1, "4a:34:3f:50:52:88", b"\x02\x01\x06", -60))
    truncated = hci.Event(ev.code, ev.params[:-3] + b"\x00")  # claims one report, cuts its data
    assert hci.parse_advertising_reports(hci.Event(ev.code, ev.params[:1])) == []
    assert hci.parse_advertising_reports(truncated) == []
    assert hci.parse_advertising_reports(hci.Event(0x0E, b"\x01\x03\x0c\x00")) == []


def test_reassembler_handles_split_and_concatenated_events():
    e1 = bytes([0x0E, 4, 1, 0x03, 0x0C, 0]).hex()
    e2 = _adv_report_event((3, 0, "00:11:22:33:44:66", b"\x02\x01\x06", -40))
    e2_bytes = bytes([e2.code, len(e2.params)]) + e2.params
    stream = bytes.fromhex(e1) + e2_bytes
    r = hci.EventReassembler()
    got = r.feed(stream[:3])
    assert got == []
    got += r.feed(stream[3:9])
    got += r.feed(stream[9:])
    assert [e.code for e in got] == [0x0E, 0x3E]
    assert got[1].params == e2.params


def test_bluetooth_class_detection():
    assert hci.is_bluetooth_class((0xE0, 1, 1), None)
    assert hci.is_bluetooth_class((0, 0, 0), (0xE0, 1, 1))
    assert not hci.is_bluetooth_class((0xFF, 0, 0), (0xFF, 0xFF, 0xFF))
    # Branded BCM20702A0 units: vendor-specific class, matched like Linux btusb does.
    assert hci.is_bluetooth_class((0xFF, 1, 1), (0xFF, 1, 1))
    assert hci.is_bluetooth_class((0xFF, 0, 0), None, (0x19FF, 0x0239))
    assert not hci.is_bluetooth_class((0xFF, 0, 0), None, (0x1234, 0x5678))
    assert (0x19FF, 0x0239) in hci.KNOWN_DONGLES
