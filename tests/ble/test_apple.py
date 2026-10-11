"""Apple Continuity decoding (msdr/ble/apple.py) on synthetic payloads built
from the documented (reverse-engineered) layouts -- no captured bytes from
real nearby devices."""

from msdr.ble.apple import parse_continuity, parse_proximity_pairing


def _proximity(model: int, status: int, pods: int, flags_case: int) -> bytes:
    body = bytes([0x01, model >> 8, model & 0xFF, status, pods, flags_case, 0x00, 0x00])
    return bytes([0x07, len(body)]) + body


def test_message_types_named_and_unknown_kept_as_hex():
    data = bytes([0x0C, 2, 0xAA, 0xBB, 0x10, 1, 0x00, 0x16, 1, 0x00])
    parsed = parse_continuity(data)
    assert [t["name"] for t in parsed["types"]] == ["Handoff", "Nearby Info", None]
    assert parsed["types"][2]["type"] == "0x16"
    assert parsed["audio"] is None


def test_homekit_length_byte_carries_a_subtype():
    # 0x31 = subtype 1, length 17 -- read as 49 it would overrun and drop it.
    data = bytes([0x06, 0x31]) + bytes(17) + bytes([0x11, 0x36]) + bytes(22)
    assert [t["name"] for t in parse_continuity(data)["types"]] == [
        "HomeKit", "HomeKit notification"
    ]


def test_overflow_area_has_no_length_byte():
    data = bytes([0x01]) + bytes(15) + bytes([0x40])
    assert [t["type"] for t in parse_continuity(data)["types"]] == ["0x01"]


def test_truncated_message_keeps_complete_ones_and_garbage_is_none():
    parsed = parse_continuity(bytes([0x0C, 1, 0x00, 0x10, 9, 0x00]))
    assert [t["name"] for t in parsed["types"]] == ["Handoff"]
    assert parse_continuity(bytes([0x0C, 5, 0x00])) is None


def test_airpods_model_battery_and_swap():
    # status bit 0x20 set: low nibble is left. 0x49 -> left 90 %, right 40 %.
    audio = parse_continuity(_proximity(0x1420, 0x20, 0x49, 0x2F))["audio"]
    assert audio["model"] == "AirPods Pro (2nd generation)"
    assert (audio["battery_left"], audio["battery_right"], audio["battery_case"]) == (90, 40, None)
    assert audio["charging_right"] and not audio["charging_left"] and not audio["charging_case"]
    # bit clear: swapped -- high nibble is left.
    audio = parse_proximity_pairing(_proximity(0x0E20, 0x00, 0x49, 0x45)[2:])
    assert (audio["battery_left"], audio["battery_right"], audio["battery_case"]) == (40, 90, 50)
    assert audio["charging_case"] and audio["model"] == "AirPods Pro"
    assert parse_proximity_pairing(_proximity(0x7720, 0, 0, 0)[2:])["model"] is None
