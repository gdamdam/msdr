import struct

from msdr.decoder.pydecoders.lora import decode_mesh_payload
from msdr.decoder.pydecoders.lorawan import LORAWAN_SYNC, devaddr_network, parse_lorawan

MIC = bytes.fromhex("DEADBEEF")


def _data(mhdr, devaddr, fctrl, fcnt, fopts=b"", fport=None, payload=b""):
    body = bytes([mhdr]) + struct.pack("<IBH", devaddr, fctrl, fcnt) + fopts
    if fport is not None:
        body += bytes([fport]) + payload
    return body + MIC


def test_join_request():
    frame = (
        bytes([0x00])
        + bytes.fromhex("0807060504030201")
        + bytes.fromhex("1817161514131211")
        + struct.pack("<H", 0x1234)
        + MIC
    )
    r = parse_lorawan(frame, sync_word=LORAWAN_SYNC)
    assert r["lorawan_mtype"] == "Join Request"
    assert r["lorawan_join_eui"] == "0102030405060708"
    assert r["lorawan_dev_eui"] == "1112131415161718"
    assert r["lorawan_dev_nonce"] == 0x1234
    assert r["lorawan_mic"] == "deadbeef"
    assert r["lorawan_evidence"] == "sync_word"


def test_join_accept_lengths():
    assert parse_lorawan(bytes([0x20]) + bytes(16))["lorawan_mtype"] == "Join Accept"
    assert parse_lorawan(bytes([0x20]) + bytes(32))["lorawan_cflist"] is True
    assert parse_lorawan(bytes([0x20]) + bytes(20)) is None


def test_unconfirmed_uplink_ttn_with_fopts():
    # ADR + ADRACKReq, FOptsLen 1 (LinkCheckReq), FPort 1, 3 payload bytes.
    frame = _data(0x40, 0x26011234, 0xC1, 258, b"\x02", 1, b"abc")
    r = parse_lorawan(frame, sync_word=LORAWAN_SYNC)
    assert r["lorawan_mtype"] == "Unconfirmed Data Up"
    assert r["lorawan_dev_addr"] == "26011234"
    assert r["lorawan_network_operator"] == "The Things Network"
    assert r["lorawan_adr"] and r["lorawan_adr_ack_req"] and not r["lorawan_ack"]
    assert r["lorawan_fcnt"] == 258
    assert r["lorawan_mac_commands"] == ["LinkCheckReq"]
    assert r["lorawan_fport"] == 1 and r["lorawan_payload_bytes"] == 3
    assert r["lorawan_payload_kind"] == "application"
    assert r["lorawan_mic"] == "deadbeef"


def test_confirmed_downlink_flags_and_mac_commands():
    # ACK + FPending, FOptsLen 5: LinkADRReq (4 bytes) after DevStatusReq... order: 06 then 03+4.
    fopts = b"\x06" + b"\x03\x00\x00\x00\x00"[:5]
    frame = _data(0xA0, 0xFC014C01, 0x20 | 0x10 | len(fopts), 7, fopts)
    r = parse_lorawan(frame)
    assert r["lorawan_mtype"] == "Confirmed Data Down"
    assert r["lorawan_ack"] and r["lorawan_fpending"]
    assert r["lorawan_mac_commands"] == ["DevStatusReq", "LinkADRReq"]
    assert r["lorawan_network_operator"] == "Helium"
    assert "lorawan_fport" not in r and r["lorawan_payload_bytes"] == 0
    assert r["lorawan_evidence"] == "plausible_header"


def test_fport_zero_is_mac_payload():
    r = parse_lorawan(_data(0x80, 0x00000001, 0, 1, b"", 0, b"\x01\x02"))
    assert r["lorawan_mtype"] == "Confirmed Data Up"
    assert r["lorawan_payload_kind"] == "mac_commands"
    assert r["lorawan_network_operator"] == "Private / experimental"


def test_proprietary_needs_sync_word():
    frame = bytes([0xE0]) + b"xyz12"
    assert parse_lorawan(frame) is None
    assert parse_lorawan(frame, sync_word=LORAWAN_SYNC)["lorawan_mtype"] == "Proprietary"


def test_devaddr_types():
    assert devaddr_network(0x27FFFFFF)["nwkid"] == 0x13
    assert devaddr_network(0x80000000)["netid_type"] == 1


def test_garbage_and_truncated():
    assert parse_lorawan(b"") is None
    assert parse_lorawan(b"\x40\x01\x02") is None
    assert parse_lorawan(bytes([0x43]) + bytes(14)) is None  # major 3
    assert parse_lorawan(bytes([0x44]) + bytes(14)) is None  # RFU bit
    assert parse_lorawan(bytes([0x40]) + bytes(10)) is None  # shorter than 12
    # FOptsLen 15 cannot fit in a 12-byte frame.
    assert parse_lorawan(_data(0x40, 1, 0x0F, 1)) is None


def test_mesh_payload_integration_and_other_protocols_unchanged():
    frame = _data(0x40, 0x26000001, 0, 1, b"", 1, b"hi")
    f = decode_mesh_payload(frame, "lora", LORAWAN_SYNC)
    assert f["protocol"] == "LoRaWAN" and f["encrypted"] is True
    assert f["lorawan_dev_addr"] == "26000001"
    assert decode_mesh_payload(b"\x01" * 5, "lora")["protocol"] == "LoRa"
    assert decode_mesh_payload(frame, "meshtastic")["protocol"] == "Meshtastic"
