"""AD structure parsing and address classification (docs/ble.md)."""

from __future__ import annotations

from msdr.ble import adv


def test_apple_continuity_payload():
    a = adv.parse(bytes.fromhex("02011a0cff4c001007361f84b53cae28020a00"))
    assert a.flags == 0x1A
    assert a.company_id == 0x004C and adv.company_name(a.company_id) == "Apple"
    assert a.manufacturer_data[0x004C] == bytes.fromhex("1007361f84b53cae28")
    assert a.tx_power == 0
    assert a.name is None and a.service_uuids == [] and not a.remote_id and not a.malformed


def test_names_uuids_service_data_and_appearance():
    payload = (
        bytes.fromhex("020106")
        + bytes.fromhex("0808")
        + b"TestBea"  # shortened name
        + bytes.fromhex("0b09")
        + b"TestBeacon"  # complete name wins
        + bytes.fromhex("03030f18")  # 16-bit Battery
        + bytes.fromhex("1107")
        + bytes.fromhex("9ecadc240ee5a9e093f3a3b50100406e")
        + bytes.fromhex("0516aafe0010")  # Eddystone service data
        + bytes.fromhex("03194003")  # appearance
    )
    a = adv.parse(payload)
    assert a.name == "TestBeacon" and a.name_complete is True
    assert a.service_uuids == ["0x180f", "6e400001-b5a3-f393-e0a9-e50e24dcca9e"]
    assert a.service_data == {"0xfeaa": bytes.fromhex("0010")}
    assert a.appearance == 0x0340
    assert adv.SERVICE_NAMES["0x180f"] == "Battery"


def test_remote_id_flag_requires_uuid_fffa_and_app_code_0d():
    yes = adv.parse(bytes.fromhex("1e16faff0d00") + b"\x00" * 25)
    assert yes.remote_id and yes.service_uuids == []
    no_code = adv.parse(bytes.fromhex("0416faff01"))
    assert not no_code.remote_id
    other_uuid = adv.parse(bytes.fromhex("0516f9fa0d00"))
    assert not other_uuid.remote_id


def test_malformed_payload_keeps_what_was_readable():
    a = adv.parse(bytes.fromhex("020106") + bytes.fromhex("0a09") + b"Tes")
    assert a.flags == 6 and a.name is None and a.malformed
    short = adv.parse(bytes.fromhex("0116"))  # service data shorter than a UUID
    assert short.service_data == {} and not short.malformed
    assert adv.parse(b"").malformed is False


def test_classify_address():
    assert adv.classify_address("00:11:22:33:44:66", "public") == "public"
    assert adv.classify_address("c0:11:22:33:44:55", "random") == "static"
    assert adv.classify_address("4a:34:3f:50:52:88", "random") == "rpa"
    assert adv.classify_address("3f:00:00:00:00:01", "random") == "nrpa"
    assert adv.classify_address("80:00:00:00:00:01", "random") == "random"
    assert adv.classify_address("garbage", "random") == "random"
