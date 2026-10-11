import pytest

from msdr.decoder.payloads import _crc16_ccitt, decode_payload


def _rs41_block(block_id: int, payload: bytes) -> bytes:
    return bytes([block_id, len(payload)]) + payload + _crc16_ccitt(payload).to_bytes(2, "little")


def _rs41_frame() -> bytes:
    status = bytearray(0x18)
    status[0:2] = (321).to_bytes(2, "little")
    status[2:10] = b"K1234567"
    status[0x0A] = 28
    gps = bytearray(0x13)
    gps[0:4] = (637813700).to_bytes(4, "little", signed=True)
    gps[0x0C:0x12] = b"\x64\x00\x9c\xff\x32\x00"
    gps[0x12] = 8
    blocks = _rs41_block(0x79, bytes(status)) + _rs41_block(0x7B, bytes(gps))
    remainder = 320 - 0x39 - len(blocks) - 4
    blocks += _rs41_block(0x76, bytes(remainder))
    return bytes(0x38) + b"\x0f" + blocks


def test_rs41_extracts_status_gps_and_checks_subblock_crc():
    text, fields, kind, integrity, note = decode_payload("rs41", _rs41_frame())
    assert kind == "text"
    assert integrity == "passed"
    assert "CRC-16 checks passed" in note
    assert fields["serial"] == "K1234567"
    assert fields["frame_number"] == 321
    assert fields["battery_v"] == 2.8
    assert fields["gps_satellites"] == 8
    assert fields["ecef_position_m"] == [6378137.0, 0.0, 0.0]
    assert fields["ecef_velocity_m_s"] == [1.0, -1.0, 0.5]
    assert text.startswith("RS41 serial=K1234567")


def test_rs41_bad_crc_is_failed_and_truncated_frame_raises():
    frame = bytearray(_rs41_frame())
    frame[0x39 + 4] ^= 1
    assert decode_payload("rs41", bytes(frame))[3] == "failed"
    with pytest.raises(ValueError, match="320-byte or 518-byte"):
        decode_payload("rs41", bytes(frame[:-1]))


def test_eot_decodes_fields_and_verifies_crc18():
    packet = bytes.fromhex("3c561245617da143")
    text, fields, kind, integrity, note = decode_payload("endoftrain", packet)
    assert kind == "text"
    assert integrity == "passed"
    assert "CRC-18 verified" in note
    assert fields["address"] == 0x24AC
    assert fields["pressure_psig"] == 0x45
    assert fields["motion"] is True
    assert text.startswith("EOT address=")
    corrupted = bytearray(packet)
    corrupted[3] ^= 1
    assert decode_payload("endoftrain", bytes(corrupted))[3] == "failed"
    with pytest.raises(ValueError, match="exactly 8 bytes"):
        decode_payload("endoftrain", packet[:-1])


def test_ils_is_measurement_and_dsc_binary_never_becomes_utf8_garbage():
    ils = decode_payload("ils", b"-0.125\n")
    assert ils[1] == {"ddm": -0.125}
    assert ils[2:4] == ("measurement", "unavailable")
    dsc = decode_payload("dsc", b"\xff\x00\x81")
    assert dsc[0] == "DSC symbols ff0081"
    assert dsc[1]["payload_hex"] == "ff0081"
    assert dsc[2:4] == ("text", "unknown")
    with pytest.raises(ValueError, match="ASCII numeric"):
        decode_payload("ils", b"\xff")
