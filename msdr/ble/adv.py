"""Advertising-data (AD structure) parsing and address classification
(docs/ble.md). Payloads are neighbour-controlled bytes: nothing here raises
on malformed input, it marks the advertisement `malformed` and keeps what
could be read.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from msdr.ble import sig

AD_FLAGS = 0x01
AD_UUID16_INCOMPLETE = 0x02
AD_UUID16_COMPLETE = 0x03
AD_UUID32_INCOMPLETE = 0x04
AD_UUID32_COMPLETE = 0x05
AD_UUID128_INCOMPLETE = 0x06
AD_UUID128_COMPLETE = 0x07
AD_NAME_SHORT = 0x08
AD_NAME_COMPLETE = 0x09
AD_TX_POWER = 0x0A
AD_SERVICE_DATA16 = 0x16
AD_APPEARANCE = 0x19
AD_SERVICE_DATA32 = 0x20
AD_SERVICE_DATA128 = 0x21
AD_MANUFACTURER = 0xFF

#: ASTM F3411 / ASD-STAN direct Remote ID: 16-bit service UUID 0xFFFA, first
#: service-data byte is the application code 0x0D.
REMOTE_ID_UUID = "0xfffa"
REMOTE_ID_APP_CODE = 0x0D

#: Bluetooth SIG company identifiers that account for most legacy traffic.
COMPANY_NAMES: dict[int, str] = {
    0x0001: "Nokia",
    0x0006: "Microsoft",
    0x000A: "Cambridge Silicon Radio",
    0x000D: "Texas Instruments",
    0x000F: "Broadcom",
    0x001D: "Qualcomm",
    0x0030: "STMicroelectronics",
    0x004C: "Apple",
    0x0059: "Nordic Semiconductor",
    0x0075: "Samsung",
    0x0087: "Garmin",
    0x00D2: "Dialog Semiconductor",
    0x00E0: "Google",
    0x012D: "Sony",
    0x0131: "Cypress Semiconductor",
    0x0157: "Anhui Huami",
    0x0171: "Amazon",
    0x0181: "Fitbit",
    0x01DA: "Logitech",
    0x02FF: "Silicon Labs",
    0x0310: "SGL Italia",
    0x038F: "Xiaomi",
    0x0499: "Ruuvi Innovations",
    0x0822: "Adafruit",
}

#: 16-bit service UUIDs worth naming in the details pane.
SERVICE_NAMES: dict[str, str] = {
    "0x1800": "Generic Access",
    "0x180a": "Device Information",
    "0x180d": "Heart Rate",
    "0x180f": "Battery",
    "0x1812": "HID",
    "0x181a": "Environmental Sensing",
    "0xfe2c": "Google Fast Pair",
    "0xfe9f": "Google",
    "0xfeaa": "Eddystone",
    "0xfd6f": "Exposure Notification",
    "0xfffa": "ASTM Remote ID",
}


@dataclass
class Advertisement:
    flags: int | None = None
    name: str | None = None
    name_complete: bool | None = None
    service_uuids: list[str] = field(default_factory=list)
    service_data: dict[str, bytes] = field(default_factory=dict)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    tx_power: int | None = None
    appearance: int | None = None
    malformed: bool = False

    @property
    def remote_id(self) -> bool:
        data = self.service_data.get(REMOTE_ID_UUID)
        return bool(data) and data[0] == REMOTE_ID_APP_CODE

    @property
    def company_id(self) -> int | None:
        # The last manufacturer block wins; almost every payload has one.
        return next(reversed(self.manufacturer_data), None) if self.manufacturer_data else None


def uuid16(value: int) -> str:
    return f"0x{value:04x}"


def uuid32(value: int) -> str:
    return f"0x{value:08x}"


def uuid128(raw_le: bytes) -> str:
    h = raw_le[::-1].hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _uuids(data: bytes, width: int) -> list[str]:
    out = []
    for off in range(0, len(data) - width + 1, width):
        chunk = data[off : off + width]
        if width == 2:
            out.append(uuid16(struct.unpack("<H", chunk)[0]))
        elif width == 4:
            out.append(uuid32(struct.unpack("<I", chunk)[0]))
        else:
            out.append(uuid128(chunk))
    return out


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\x00", "").strip()


def parse(payload: bytes) -> Advertisement:
    """Legacy advertising payload (<= 31 bytes of length/type/value blocks)."""
    adv = Advertisement()
    off = 0
    while off < len(payload):
        length = payload[off]
        if length == 0:
            off += 1  # padding allowed by the spec
            continue
        if off + 1 + length > len(payload):
            adv.malformed = True
            break
        ad_type = payload[off + 1]
        data = payload[off + 2 : off + 1 + length]
        off += 1 + length
        try:
            _apply(adv, ad_type, data)
        except (struct.error, IndexError):
            adv.malformed = True
    return adv


def _apply(adv: Advertisement, ad_type: int, data: bytes) -> None:
    if ad_type == AD_FLAGS and data:
        adv.flags = data[0]
    elif ad_type in (AD_UUID16_INCOMPLETE, AD_UUID16_COMPLETE):
        _extend_unique(adv.service_uuids, _uuids(data, 2))
    elif ad_type in (AD_UUID32_INCOMPLETE, AD_UUID32_COMPLETE):
        _extend_unique(adv.service_uuids, _uuids(data, 4))
    elif ad_type in (AD_UUID128_INCOMPLETE, AD_UUID128_COMPLETE):
        _extend_unique(adv.service_uuids, _uuids(data, 16))
    elif ad_type == AD_NAME_COMPLETE:
        adv.name, adv.name_complete = _text(data), True
    elif ad_type == AD_NAME_SHORT:
        if adv.name_complete is not True:
            adv.name, adv.name_complete = _text(data), False
    elif ad_type == AD_TX_POWER and data:
        adv.tx_power = struct.unpack("<b", data[:1])[0]
    elif ad_type == AD_APPEARANCE and len(data) >= 2:
        adv.appearance = struct.unpack("<H", data[:2])[0]
    elif ad_type == AD_SERVICE_DATA16 and len(data) >= 2:
        adv.service_data[uuid16(struct.unpack("<H", data[:2])[0])] = data[2:]
    elif ad_type == AD_SERVICE_DATA32 and len(data) >= 4:
        adv.service_data[uuid32(struct.unpack("<I", data[:4])[0])] = data[4:]
    elif ad_type == AD_SERVICE_DATA128 and len(data) >= 16:
        adv.service_data[uuid128(data[:16])] = data[16:]
    elif ad_type == AD_MANUFACTURER and len(data) >= 2:
        adv.manufacturer_data[struct.unpack("<H", data[:2])[0]] = data[2:]


def _extend_unique(target: list[str], items: list[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def classify_address(address: str, address_type: str) -> str:
    """public | static | rpa | nrpa | random -- per Core Spec Vol 6 Part B
    1.3: the two most significant bits of a random address say whether it is
    static (11), resolvable private (01) or non-resolvable private (00)."""
    if address_type == "public":
        return "public"
    try:
        msb = int(address.split(":")[0], 16)
    except (ValueError, IndexError):
        return "random"
    top = msb >> 6
    return {0b11: "static", 0b01: "rpa", 0b00: "nrpa"}.get(top, "random")


def company_name(company_id: int | None) -> str | None:
    """Short built-in name first, then the downloaded SIG list (msdr/ble/sig.py)."""
    if company_id is None:
        return None
    return COMPANY_NAMES.get(company_id) or sig.company(company_id)


def service_name(uuid: str) -> str | None:
    return SERVICE_NAMES.get(uuid) or sig.service(uuid)


def appearance_name(value: int | None) -> str | None:
    return sig.appearance(value) if value is not None else None
