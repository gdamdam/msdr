"""msdr/remoteid/messages.py against tests/remoteid/data/vectors.json
(independently generated from opendroneid-core-c -- see that directory's
README) plus hand-built malformed/truncated/boundary cases."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from msdr.remoteid import messages as m

VECTORS_PATH = Path(__file__).parent / "data" / "vectors.json"
VECTORS = {v["name"]: v for v in json.loads(VECTORS_PATH.read_text())["vectors"]}

# vectors.json field name -> (attribute name or callable(decoded) -> value)
_FIELD_MAP = {
    "message_type": lambda d: d.message_type,
    "protocol_version": lambda d: d.protocol_version,
    "ua_type": lambda d: d.ua_type,
    "id_type": lambda d: d.id_type,
    "uas_id": lambda d: d.uas_id_text,
    "uas_id_hex": lambda d: d.uas_id_bytes.hex(),
    "status": lambda d: d.status,
    "direction_deg": lambda d: d.direction_deg,
    "speed_horizontal_ms": lambda d: d.speed_horizontal_ms,
    "speed_vertical_ms": lambda d: d.speed_vertical_ms,
    "latitude": lambda d: d.latitude_deg,
    "longitude": lambda d: d.longitude_deg,
    "altitude_baro_m": lambda d: d.altitude_baro_m,
    "altitude_geo_m": lambda d: d.altitude_geo_m,
    "height_type": lambda d: d.height_type,
    "height_m": lambda d: d.height_m,
    "horiz_accuracy_enum": lambda d: d.horiz_accuracy_enum,
    "vert_accuracy_enum": lambda d: d.vert_accuracy_enum,
    "baro_accuracy_enum": lambda d: d.baro_accuracy_enum,
    "speed_accuracy_enum": lambda d: d.speed_accuracy_enum,
    "ts_accuracy_enum": lambda d: d.timestamp_accuracy_enum,
    "timestamp_s": lambda d: d.timestamp_s,
    "auth_type": lambda d: d.auth_type,
    "data_page": lambda d: d.data_page,
    "last_page_index": lambda d: d.last_page_index,
    "length": lambda d: d.length,
    "timestamp_s_since_2019": lambda d: d.timestamp_s_since_2019,
    "auth_data_hex": lambda d: d.auth_data.hex(),
    "desc_type": lambda d: d.desc_type,
    "description": lambda d: d.description,
    "operator_location_type": lambda d: d.operator_location_type,
    "classification_type": lambda d: d.classification_type,
    "operator_latitude": lambda d: d.operator_latitude_deg,
    "operator_longitude": lambda d: d.operator_longitude_deg,
    "area_count": lambda d: d.area_count,
    "area_radius_m": lambda d: d.area_radius_m,
    "area_ceiling_m": lambda d: d.area_ceiling_m,
    "area_floor_m": lambda d: d.area_floor_m,
    "category_eu": lambda d: d.category_eu,
    "class_eu": lambda d: d.class_eu,
    "operator_altitude_geo_m": lambda d: d.operator_altitude_geo_m,
    "operator_id_type": lambda d: d.operator_id_type,
    "operator_id": lambda d: d.operator_id,
}


def _assert_field(decoded, key: str, expected) -> None:
    actual = _FIELD_MAP[key](decoded)
    if expected is None:
        assert actual is None, f"{key}: expected None (unknown/invalid), got {actual!r}"
    elif isinstance(expected, float):
        assert actual is not None, f"{key}: expected {expected}, got None"
        assert actual == pytest.approx(expected, abs=1e-3), (
            f"{key}: expected {expected}, got {actual}"
        )
    else:
        assert actual == expected, f"{key}: expected {expected!r}, got {actual!r}"


MESSAGE_VECTOR_NAMES = [v["name"] for v in VECTORS.values() if v["type"] == "message"]


@pytest.mark.parametrize("name", MESSAGE_VECTOR_NAMES)
def test_decode_message_vector(name):
    vector = VECTORS[name]
    data = bytes.fromhex(vector["hex"])
    decoded = m.decode_message(data)
    assert not isinstance(decoded, m.RemoteIdError), f"{name}: {decoded}"
    for key, expected in vector["decoded"].items():
        _assert_field(decoded, key, expected)


def test_decode_message_pack_vector():
    vector = VECTORS["message_pack_7"]
    pack = m.decode_message_pack(bytes.fromhex(vector["hex"]))
    assert not isinstance(pack, m.RemoteIdError)
    assert pack.message_count == 7
    assert len(pack.messages) == 7
    assert not pack.errors

    by_type = {
        msg.message_type: msg for msg in pack.messages if msg.message_type != m.MESSAGETYPE_AUTH
    }
    expected = vector["decoded"]
    assert by_type[m.MESSAGETYPE_BASIC_ID].uas_id_text == expected["basic_id_uas_id"]
    assert by_type[m.MESSAGETYPE_LOCATION].status == expected["location_status"]
    assert by_type[m.MESSAGETYPE_SELF_ID].description == expected["self_id_description"]
    assert by_type[m.MESSAGETYPE_OPERATOR_ID].operator_id == expected["operator_id"]
    auth_pages = {msg.data_page for msg in pack.messages if msg.message_type == m.MESSAGETYPE_AUTH}
    assert auth_pages == {0, 1}


# --------------------------------------------------------------------------
# Malformed / truncated input: typed errors, never a raise.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 24, 26, 100])
def test_decode_message_wrong_length_is_truncated_error(length):
    result = m.decode_message(bytes(length))
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "truncated"


def test_decode_message_unsupported_type():
    # High nibble 6-0xE are not assigned message types (0xF is the pack type).
    data = bytes([0x60]) + bytes(24)
    result = m.decode_message(data)
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "unsupported_type"


def test_decode_auth_malformed_length_exceeds_pages():
    # DataPage 0, AuthType=1, LastPageIndex=0 (only page 0 exists, max 17
    # bytes of data) but Length claims 200 bytes -- must fail, not raise.
    byte0 = (m.MESSAGETYPE_AUTH << 4) | 0
    byte1 = (1 << 4) | 0  # AuthType=1, DataPage=0
    data = bytes([byte0, byte1, 0, 200]) + bytes(21)
    assert len(data) == m.MESSAGE_SIZE
    result = m.decode_message(data)
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "malformed"


def test_decode_message_pack_truncated_header():
    result = m.decode_message_pack(bytes(2))
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "truncated"


def test_decode_message_pack_truncated_body():
    # Header claims 2 messages but only one is present.
    header = bytes([m.MESSAGETYPE_PACKED << 4, m.MESSAGE_SIZE, 2])
    one_message = bytes([m.MESSAGETYPE_SELF_ID << 4]) + bytes(24)
    result = m.decode_message_pack(header + one_message)
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "truncated"


def test_decode_message_pack_reports_inner_errors_not_hidden():
    # A well-formed pack header wrapping one valid message and one message
    # of a legal type (Authentication) that is itself malformed (Length
    # exceeds what LastPageIndex can hold): the pack still decodes, the bad
    # message is reported in .errors, not silently dropped. (A message whose
    # *type* is illegal fails the whole pack -- checkPackContent()'s own
    # behaviour in opendroneid-core-c -- covered by
    # test_decode_message_pack_too_many_of_one_type and its sibling below.)
    good = bytes([m.MESSAGETYPE_SELF_ID << 4, 0]) + bytes(23)
    bad_auth_byte1 = (1 << 4) | 0  # AuthType=1, DataPage=0
    bad = bytes([m.MESSAGETYPE_AUTH << 4, bad_auth_byte1, 0, 200]) + bytes(21)
    header = bytes([m.MESSAGETYPE_PACKED << 4, m.MESSAGE_SIZE, 2])
    pack = m.decode_message_pack(header + good + bad)
    assert isinstance(pack, m.MessagePack)
    assert len(pack.messages) == 1
    assert len(pack.errors) == 1
    assert pack.errors[0].kind == "malformed"


def test_decode_message_pack_illegal_inner_type_fails_whole_pack():
    # checkPackContent(): any message whose type isn't one of the six
    # ordinary types fails the entire pack up front (never partially
    # decoded), matching opendroneid-core-c.
    good = bytes([m.MESSAGETYPE_SELF_ID << 4, 0]) + bytes(23)
    illegal_type = bytes([0x60]) + bytes(24)
    header = bytes([m.MESSAGETYPE_PACKED << 4, m.MESSAGE_SIZE, 2])
    result = m.decode_message_pack(header + good + illegal_type)
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "malformed"


def test_decode_message_pack_too_many_of_one_type():
    # Two Location messages: checkPackContent() allows at most one.
    location = bytes([m.MESSAGETYPE_LOCATION << 4, 0]) + bytes(23)
    header = bytes([m.MESSAGETYPE_PACKED << 4, m.MESSAGE_SIZE, 2])
    result = m.decode_message_pack(header + location + location)
    assert isinstance(result, m.RemoteIdError)
    assert result.kind == "malformed"


# --------------------------------------------------------------------------
# Timestamp wrap at the hour.
# --------------------------------------------------------------------------


def _location_bytes(timestamp_enc: int) -> bytes:
    byte0 = m.MESSAGETYPE_LOCATION << 4
    byte1 = 0
    body = struct.pack(
        "<BBbiiHHH", 0, 0, 0, 0, 0, 2000, 2000, 2000
    )  # alt enc 2000 -> 0.0 m, not unknown
    accuracy_bytes = bytes(
        [0, 0]
    )  # byte19 (horiz/vert acc), byte20 (speed/baro acc): all "unknown" (0)
    tail = struct.pack("<H", timestamp_enc) + bytes([0, 0])  # byte23 (ts acc), byte24 (reserved)
    data = bytes([byte0, byte1]) + body + accuracy_bytes + tail
    assert len(data) == m.MESSAGE_SIZE
    return data


def test_timestamp_zero_is_valid_top_of_hour():
    decoded = m.decode_message(_location_bytes(0))
    assert decoded.timestamp_s == 0.0


def test_timestamp_just_before_wrap_is_valid():
    decoded = m.decode_message(
        _location_bytes(35999)
    )  # 3599.9s, one tick before the hour rolls over
    assert decoded.timestamp_s == pytest.approx(3599.9, abs=1e-6)


def test_timestamp_wrap_does_not_imply_going_backward_in_this_module():
    # decode_message has no notion of "previous" timestamp -- each call is
    # independent -- so the wrap (3599.9 -> 0.0 on the next message) is just
    # two valid, unrelated values. (Tracker-level freshness ordering is
    # covered in test_tracker.py and never uses this field.)
    before = m.decode_message(_location_bytes(35999))
    after = m.decode_message(_location_bytes(0))
    assert before.timestamp_s == pytest.approx(3599.9, abs=1e-6)
    assert after.timestamp_s == 0.0


def test_timestamp_sentinel_is_unknown():
    decoded = m.decode_message(_location_bytes(0xFFFF))
    assert decoded.timestamp_s is None


# --------------------------------------------------------------------------
# Accuracy enums: unknown (0) never decodes to the worst-case default number.
# --------------------------------------------------------------------------


def test_accuracy_unknown_enum_decodes_to_none():
    assert m.decode_horizontal_accuracy_m(0) is None
    assert m.decode_vertical_accuracy_m(0) is None
    assert m.decode_speed_accuracy_ms(0) is None
    assert m.decode_timestamp_accuracy_s(0) is None


def test_accuracy_known_enum_decodes_to_meters():
    assert m.decode_horizontal_accuracy_m(12) == pytest.approx(1.0)
    assert m.decode_vertical_accuracy_m(6) == pytest.approx(1.0)
    assert m.decode_speed_accuracy_ms(4) == pytest.approx(0.3)
    assert m.decode_timestamp_accuracy_s(1) == pytest.approx(0.1)
