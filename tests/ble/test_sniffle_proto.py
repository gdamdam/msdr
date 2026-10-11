"""`msdr.ble.sniffle_proto`: wire framing and advertising-PDU decode (ported
from Sniffle v1.11.0, see THIRD_PARTY_NOTICES.md)."""

from __future__ import annotations

from struct import pack

from msdr.ble import sniffle_proto as proto


def test_command_framing_round_trips():
    line = proto.cmd_marker(b"MSDR")
    assert line.endswith(b"\r\n")
    opcode, args = proto.decode_line(line)
    assert opcode == proto.CMD_MARKER
    assert args == b"MSDR"


def test_decode_line_rejects_garbage():
    import pytest

    with pytest.raises(proto.FramingError):
        proto.decode_line(b"not-base64-!!!\r\n")


def test_decode_line_ignores_blank_lines():
    assert proto.decode_line(b"\r\n") is None


def test_version_measurement_decodes():
    body = bytes([4, proto.MEASUREMENT_TYPE_VERSION, 1, 11, 0, 10])
    assert proto.decode_measurement(body) == {
        "major": 1,
        "minor": 11,
        "revision": 0,
        "api_level": 10,
    }


def test_version_measurement_rejects_other_types():
    body = bytes([2, 0, 1, 2])  # MeasurementType.INTERVAL, not VERSION
    assert proto.decode_measurement(body) is None


def _legacy_body(pdu_type: int, rssi: int = -50, chan: int = 37) -> bytes:
    adva_wire = bytes.fromhex("554433221100")  # -> 00:11:22:33:44:55 displayed
    adv_data = bytes.fromhex("0201060909546573744265616e03ff5900")
    pdu_hdr = bytes([pdu_type])
    pkt_body = pdu_hdr + bytes([len(adva_wire) + len(adv_data)]) + adva_wire + adv_data
    chan_phy = (proto.PhyMode.PHY_1M << 6) | chan
    return pack("<LHHbB", 0, len(pkt_body), 0, rssi, chan_phy) + pkt_body


def test_decode_legacy_adv_nonconn_ind():
    report = proto.decode_packet_message(_legacy_body(0x02))
    assert report["addr"] == "00:11:22:33:44:55"
    assert report["addr_type"] == "public"
    assert report["evt"] == "ADV_NONCONN_IND"
    assert report["rssi"] == -50
    assert report["phy"] == "1m"
    assert report["channel"] == 37
    assert report["extended"] is False
    assert report["aux"] is None
    assert bytes.fromhex(report["data"]) == bytes.fromhex(
        "0201060909546573744265616e03ff5900"
    )


def test_decode_drops_scan_req_and_connect_ind():
    """msdr never surfaces a scan or connection request/response -- it only
    receives advertisements."""
    assert proto.decode_packet_message(_legacy_body(0x03)) is None  # SCAN_REQ
    assert proto.decode_packet_message(_legacy_body(0x05)) is None  # CONNECT_IND


def test_decode_drops_crc_errors():
    body = bytearray(_legacy_body(0x02))
    # length_dir is at offset 4 (uint16 LE); set the CRC-error bit (0x4000).
    length_dir = int.from_bytes(body[4:6], "little") | 0x4000
    body[4:6] = length_dir.to_bytes(2, "little")
    assert proto.decode_packet_message(bytes(body)) is None


def test_decode_extended_with_aux_ptr():
    hdr_flags = 0x01 | 0x10  # AdvA + AuxPtr
    addr = bytes.fromhex("554433221100")
    aux_raw = bytes([10, 0x00, proto.PhyMode.PHY_2M << 5])  # points to chan 10, 2M PHY
    ext_body = bytes([0x07, 0x00, len(addr) + len(aux_raw) + 1, hdr_flags]) + addr + aux_raw
    chan_phy = (proto.PhyMode.PHY_1M << 6) | 37
    msg_body = pack("<LHHbB", 0, len(ext_body), 0, -40, chan_phy) + ext_body

    report = proto.decode_packet_message(msg_body)
    assert report["evt"] == "ADV_EXT_IND"
    assert report["extended"] is True
    assert report["channel"] == 37
    assert report["aux"] == {"chan": 10, "phy": "2m", "offset_us": 0}


def test_decode_aux_adv_ind_on_secondary_channel():
    hdr_flags = 0x01  # AdvA only, no further aux chain
    addr = bytes.fromhex("554433221100")
    adv_data = bytes.fromhex("0201061a16faff0d00" + "00" * 20)
    ext_body = (
        bytes([0x07, 0x00, len(addr) + 1, hdr_flags]) + addr + adv_data
    )
    chan_phy = (proto.PhyMode.PHY_CODED_S8 << 6) | 6  # secondary channel 6, Coded PHY
    msg_body = pack("<LHHbB", 0, len(ext_body), 0, -70, chan_phy) + ext_body

    report = proto.decode_packet_message(msg_body)
    assert report["evt"] == "AUX_ADV_IND"
    assert report["extended"] is True
    assert report["channel"] == 6
    assert report["phy"] == "coded"
    assert report["aux"] is None


def test_decode_extended_skips_acad_before_ad_payload():
    """Trailing ACAD bytes inside the extended header must not leak into the
    AD payload (0.69.1; mirrors upstream AdvExtIndMessage)."""
    hdr_flags = 0x01  # AdvA
    addr = bytes.fromhex("554433221100")
    acad = bytes.fromhex("ff")
    adv_data = bytes.fromhex("020106" "0409" "6d736472")  # flags + name "msdr"
    ext_body = bytes([0x07, 0x00, 1 + len(addr) + len(acad), hdr_flags]) + addr + acad + adv_data
    chan_phy = (proto.PhyMode.PHY_1M << 6) | 6
    msg_body = pack("<LHHbB", 0, len(ext_body), 0, -60, chan_phy) + ext_body

    report = proto.decode_packet_message(msg_body)
    assert report["evt"] == "AUX_ADV_IND"
    assert report["data"] == adv_data.hex()


def test_decode_extended_rejects_truncated_header():
    """hdr_body_len claims AdvA (1 + 6 bytes) but only 3 address bytes follow:
    the packet must be dropped rather than reported with a 3-byte addr."""
    hdr_flags = 0x01  # AdvA
    ext_body = bytes([0x07, 0x00, 1 + 6, hdr_flags]) + bytes.fromhex("554433")
    chan_phy = (proto.PhyMode.PHY_1M << 6) | 6
    msg_body = pack("<LHHbB", 0, len(ext_body), 0, -60, chan_phy) + ext_body

    assert proto.decode_packet_message(msg_body) is None
