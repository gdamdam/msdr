"""Fixtures for msdr/ble/sensors.py, taken from each format's published spec
(see that module's docstring for the fetched source URLs and dates).
"""

import struct

from msdr.ble import sensors as s

# --- BTHome v2 (https://bthome.io/format/) ---------------------------------


def test_bthome_temperature_humidity_worked_example():
    # Spec's own worked example: 0xD2FC 40 02C409 03BF13 (device info 0x40 =
    # unencrypted v2; temperature object 0x02 C409 = 25.00 C; humidity
    # object 0x03 BF13 = 50.55 %).
    data = bytes.fromhex("4002c40903bf13")
    r = s.parse_bthome(data)
    assert r.protocol == "bthome"
    assert not r.encrypted and not r.malformed
    assert r.readings["temperature"].value == 25.0
    assert r.readings["temperature"].unit == "°C"
    assert r.readings["humidity"].value == 50.55


def test_bthome_battery_pressure_illuminance_door_motion():
    # Each object's bytes are the spec table's own worked examples:
    # battery 0x0161=97%, pressure 0x04138A01=1008.83 hPa,
    # illuminance 0x05138A14=13460.67 lx, door 0x1A00=closed, motion
    # 0x2101=detected.
    data = bytes.fromhex("40" + "0161" + "04138A01" + "05138A14" + "1A00" + "2101")
    r = s.parse_bthome(data)
    assert r.readings["battery"].value == 97
    assert r.readings["battery"].unit == "%"
    assert r.readings["pressure"].value == 1008.83
    assert r.readings["pressure"].unit == "hPa"
    assert r.readings["illuminance"].value == 13460.67
    assert r.readings["illuminance"].unit == "lx"
    assert r.readings["door"].value is False
    assert r.readings["motion"].value is True


def test_bthome_encrypted_reported_as_encrypted_not_guessed():
    # Device info byte 0x41: bit 0 set -> encrypted (bthome.io "Encryption
    # flag"). The trailing bytes are ciphertext and must never be decoded.
    data = bytes.fromhex("41" + "aabbccddeeff")
    r = s.parse_bthome(data)
    assert r.encrypted is True
    assert r.readings == {}


def test_bthome_malformed_truncated_object():
    # Temperature object (0x02) declares 2 value bytes but only 1 is present.
    data = bytes.fromhex("40" + "02" + "c4")
    r = s.parse_bthome(data)
    assert r.malformed is True


def test_bthome_unknown_object_id_stops_without_guessing():
    data = bytes.fromhex("40" + "0161" + "ff0102")  # 0xff is not in our table
    r = s.parse_bthome(data)
    assert r.readings["battery"].value == 97
    assert r.malformed is True


def test_bthome_too_short_is_none():
    assert s.parse_bthome(b"") is None


# --- Ruuvi RAWv2 (data format 5) --------------------------------------------
# Test vectors from
# https://github.com/ruuvi/ruuvi-sensor-protocols/blob/master/dataformat_05.md


def test_ruuvi_rawv2_valid_data_vector():
    raw = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")
    r = s.parse_ruuvi(raw)
    assert r.protocol == "ruuvi_rawv2"
    assert r.readings["temperature"].value == 24.3
    assert r.readings["humidity"].value == 53.49
    assert r.readings["pressure"].value == 1000.44
    assert r.readings["battery"].value == 2.977
    assert r.readings["tx_power"].value == 4
    assert r.readings["movement_counter"].value == 66


def test_ruuvi_rawv2_maximum_values_vector():
    raw = bytes.fromhex("057FFFFFFEFFFE7FFF7FFF7FFFFFDEFEFFFECBB8334C884F")
    r = s.parse_ruuvi(raw)
    assert r.readings["temperature"].value == 163.835
    assert r.readings["pressure"].value == 1155.34
    assert r.readings["battery"].value == 3.646
    assert r.readings["tx_power"].value == 20
    assert r.readings["movement_counter"].value == 254


def test_ruuvi_rawv2_minimum_values_vector():
    raw = bytes.fromhex("058001000000008001800180010000000000CBB8334C884F")
    r = s.parse_ruuvi(raw)
    assert r.readings["temperature"].value == -163.835
    assert r.readings["pressure"].value == 500.0
    assert r.readings["battery"].value == 1.6
    assert r.readings["tx_power"].value == -40
    assert r.readings["movement_counter"].value == 0


def test_ruuvi_rawv2_invalid_values_vector_omits_readings():
    # "Invalid values" case: every field is at its not-available sentinel.
    raw = bytes.fromhex("058000FFFFFFFF800080008000FFFFFFFFFFFFFFFFFFFFFF")
    r = s.parse_ruuvi(raw)
    assert "humidity" not in r.readings
    assert "pressure" not in r.readings
    assert "battery" not in r.readings
    assert "tx_power" not in r.readings
    assert "movement_counter" not in r.readings


def test_ruuvi_rejects_wrong_data_format():
    assert s.parse_ruuvi(bytes([3]) + bytes(23)) is None


def test_ruuvi_rejects_too_short():
    assert s.parse_ruuvi(bytes.fromhex("0512")) is None


# --- iBeacon (Apple company id 0x004C) --------------------------------------


def test_ibeacon_apple_worked_example():
    # UUID/major/minor from Apple's own retail example (Getting Started with
    # iBeacon, "San Francisco / Clothing"): D9B9EC1F-3925-43D0-80A9-
    # 1E39D4CEA95C, major 1, minor 10.
    uuid_hex = "D9B9EC1F392543D080A91E39D4CEA95C"
    data = bytes.fromhex("0215" + uuid_hex + "0001" + "000A" + "C5")
    r = s.parse_ibeacon(data)
    assert r.protocol == "ibeacon"
    assert r.readings["uuid"].value == "d9b9ec1f-3925-43d0-80a9-1e39d4cea95c"
    assert r.readings["major"].value == 1
    assert r.readings["minor"].value == 10
    assert r.readings["measured_power"].value == -59


def test_ibeacon_wrong_type_is_none():
    uuid_hex = "D9B9EC1F392543D080A91E39D4CEA95C"
    data = bytes.fromhex("0315" + uuid_hex + "0001000AC5")
    assert s.parse_ibeacon(data) is None


def test_ibeacon_too_short_is_none():
    assert s.parse_ibeacon(bytes.fromhex("0215")) is None


# --- Eddystone (0xFEAA) -----------------------------------------------------


def test_eddystone_uid_frame():
    data = bytes.fromhex("00") + bytes.fromhex("EC") + bytes([0x01] * 10) + bytes([0x02] * 6)
    r = s.parse_eddystone(data)
    assert r.protocol == "eddystone"
    assert r.readings["tx_power"].value == -20
    assert r.readings["namespace"].value == "01" * 10
    assert r.readings["instance"].value == "02" * 6


def test_eddystone_url_frame_goo_gl_example():
    # Spec's own worked example: https://goo.gl/Aq18zF
    data = bytes([0x10, 0x00, 0x03]) + b"goo.gl/Aq18zF"
    r = s.parse_eddystone(data)
    assert r.readings["url"].value == "https://goo.gl/Aq18zF"


def test_eddystone_tlm_frame():
    data = bytes.fromhex("20" + "00" + "0C80" + "1500" + "00000010" + "00000578")
    r = s.parse_eddystone(data)
    assert r.readings["battery_voltage"].value == 0x0C80
    assert r.readings["temperature"].value == 21.0
    assert r.readings["adv_count"].value == 16
    assert r.readings["uptime"].value == 140.0


def test_eddystone_uid_truncated_is_malformed():
    data = bytes.fromhex("00EC")
    r = s.parse_eddystone(data)
    assert r.malformed is True


def test_eddystone_unknown_frame_type_is_none():
    assert s.parse_eddystone(bytes([0x30, 0x00])) is None


# --- pvvx/ATC custom thermometer format (0x181A) ----------------------------


def test_atc1441_format_structural_example_from_readme():
    # atc1441's own README example bytes (placeholder values demonstrating
    # byte offsets, not realistic sensor readings):
    # 0x0e,0x16,0x1a,0x18, <mac 00*6>, 0xaa,0xaa, 0xbb, 0xcc, 0xdd,0xdd, 0x00
    data = bytes.fromhex("000000000000" + "aaaa" + "bb" + "cc" + "dddd" + "00")
    r = s.parse_atc(data)
    assert r.protocol == "atc1441"
    assert r.readings["mac"].value == "00:00:00:00:00:00"
    assert r.readings["humidity"].value == 0xBB
    assert r.readings["battery"].value == 0xCC
    assert r.readings["battery_voltage"].value == 0xDDDD


def test_atc1441_format_realistic_values():
    mac = bytes.fromhex("c4a2b1001122")
    payload = (
        mac
        + struct.pack(">h", 215)
        + bytes([55])
        + bytes([88])
        + struct.pack(">H", 2950)
        + bytes([42])
    )
    r = s.parse_atc(payload)
    assert r.protocol == "atc1441"
    assert r.readings["mac"].value == "c4:a2:b1:00:11:22"
    assert r.readings["temperature"].value == 21.5
    assert r.readings["humidity"].value == 55
    assert r.readings["battery"].value == 88
    assert r.readings["battery_voltage"].value == 2950
    assert r.readings["counter"].value == 42


def test_pvvx_custom_format_realistic_values():
    mac_le = bytes.fromhex("221100b1a2c4")  # lo..hi per pvvx docs
    payload = (
        mac_le
        + struct.pack("<h", 2150)
        + struct.pack("<H", 6050)
        + struct.pack("<H", 3050)
        + bytes([91])
        + bytes([7])
        + bytes([0b00010])
    )
    r = s.parse_atc(payload)
    assert r.protocol == "pvvx"
    assert r.readings["mac"].value == "c4:a2:b1:00:11:22"
    assert r.readings["temperature"].value == 21.5
    assert r.readings["humidity"].value == 60.5
    assert r.readings["battery_voltage"].value == 3050
    assert r.readings["battery"].value == 91
    assert r.readings["counter"].value == 7


def test_atc_unrecognised_length_is_none():
    assert s.parse_atc(bytes(9)) is None


# --- dispatch ----------------------------------------------------------------


def test_decode_prefers_bthome_service_data():
    bthome = bytes.fromhex("4002c40903bf13")
    report = s.decode({s.BTHOME_UUID: bthome}, {})
    assert report.protocol == "bthome"


def test_decode_ruuvi_from_manufacturer_data():
    raw = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")
    report = s.decode({}, {s.RUUVI_COMPANY: raw})
    assert report.protocol == "ruuvi_rawv2"


def test_decode_returns_none_when_nothing_matches():
    assert s.decode({}, {}) is None
    assert s.decode({"0x180f": b"\x64"}, {0x0001: b"\x00\x00"}) is None
