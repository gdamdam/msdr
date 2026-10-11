"""Apple Continuity advertisements (manufacturer data, company id 0x004C):
message *types*, plus the model and battery levels of AirPods/Beats
"proximity pairing" messages. See docs/ble.md "Apple Continuity".

Apple does not publish this format. The type list and the proximity-pairing
byte layout come from public reverse-engineering: the furiousMAC
"continuity" project (https://github.com/furiousMAC/continuity) and Martin et
al., "Handoff All Your Privacy" (PoPETs 2019) for the types; the OpenPods app
(https://github.com/adolfintel/OpenPods) for proximity pairing. Treat every
decoded value as best effort -- Apple can change it in any firmware.

Deliberately not decoded: anything describing what a person is doing (Nearby
Info activity/screen/call state, Nearby Action details) or keys that exist to
locate a device (Find My public keys). Only the message type is named, which
is what any BLE scanner app already shows.
"""

from __future__ import annotations

#: Continuity message type -> name. iBeacon (0x02) is decoded separately as a
#: sensor beacon (msdr/ble/sensors.py); it is listed so it still gets a name.
CONTINUITY_TYPES: dict[int, str] = {
    0x01: "Overflow area (iOS background app)",
    0x02: "iBeacon",
    0x03: "AirPrint",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "AirPods / proximity pairing",
    0x08: "Hey Siri",
    0x09: "AirPlay target",
    0x0A: "AirPlay source",
    0x0B: "Magic Switch",
    0x0C: "Handoff",
    0x0D: "Instant Hotspot (target)",
    0x0E: "Instant Hotspot (source)",
    0x0F: "Nearby Action",
    0x10: "Nearby Info",
    0x11: "HomeKit notification",
    0x12: "Find My",
}

#: HomeKit (HAP) messages pack a 3-bit subtype into the length byte's top
#: bits; only the low 5 bits are the length (HAP specification, BLE
#: advertisement format).
_HAP_TYPES = frozenset({0x06, 0x11})
_LENGTH_MASK_HAP = 0x1F

#: The overflow area has no length byte: type 0x01 then a fixed 16-byte
#: bitmask of background-app service UUIDs.
OVERFLOW_AREA = 0x01

APPLE_COMPANY = 0x004C
PROXIMITY_PAIRING = 0x07

#: Proximity-pairing model code (bytes 1-2 of the message, big-endian) ->
#: product, per OpenPods. Unknown codes are shown as their hex value.
AUDIO_MODELS: dict[int, str] = {
    0x0220: "AirPods (1st generation)",
    0x0F20: "AirPods (2nd generation)",
    0x1320: "AirPods (3rd generation)",
    0x0E20: "AirPods Pro",
    0x1420: "AirPods Pro (2nd generation)",
    0x2420: "AirPods Pro (2nd generation, USB-C)",
    0x0A20: "AirPods Max",
    0x0320: "Powerbeats3",
    0x0B20: "Powerbeats Pro",
    0x0520: "BeatsX",
    0x0620: "Beats Solo3",
    0x0920: "Beats Studio3",
    0x1020: "Beats Flex",
    0x1120: "Beats Studio Buds",
    0x1220: "Beats Fit Pro",
}


def _battery(nibble: int) -> int | None:
    """0-10 is tens of percent; anything else (0xF) means not reported."""
    return nibble * 10 if nibble <= 10 else None


def parse_proximity_pairing(body: bytes) -> dict | None:
    """Model and battery from a type-0x07 message body (OpenPods layout):
    byte 1-2 model, byte 3 status (bit 0x20 set = left/right not swapped),
    byte 4 left/right battery nibbles, byte 5 charging flags (high nibble)
    and case battery (low nibble)."""
    if len(body) < 6:
        return None
    model = (body[1] << 8) | body[2]
    status, pods, flags_case = body[3], body[4], body[5]
    flip = (status & 0x20) == 0
    high, low = pods >> 4, pods & 0x0F
    left, right = (high, low) if flip else (low, high)
    charging = flags_case >> 4
    return {
        "model_code": f"0x{model:04x}",
        "model": AUDIO_MODELS.get(model),
        "battery_left": _battery(left),
        "battery_right": _battery(right),
        "battery_case": _battery(flags_case & 0x0F),
        "charging_left": bool(charging & (0b10 if flip else 0b01)),
        "charging_right": bool(charging & (0b01 if flip else 0b10)),
        "charging_case": bool(charging & 0b100),
    }


def parse_continuity(data: bytes) -> dict | None:
    """`data` is the manufacturer-data value for company 0x004C (company id
    already stripped). Returns {"types": [...], "audio": {...} | None}, or
    None when it isn't a well-formed type/length list."""
    types: list[dict] = []
    audio = None
    i = 0
    if data[:1] == bytes([OVERFLOW_AREA]):
        return {"types": [{"type": "0x01", "name": CONTINUITY_TYPES[OVERFLOW_AREA]}], "audio": None}
    while i + 2 <= len(data):
        mtype, length = data[i], data[i + 1]
        if mtype in _HAP_TYPES:
            length &= _LENGTH_MASK_HAP
        body = data[i + 2 : i + 2 + length]
        if len(body) < length:
            break  # truncated: keep what was complete
        name = CONTINUITY_TYPES.get(mtype)
        types.append({"type": f"0x{mtype:02x}", "name": name})
        if mtype == PROXIMITY_PAIRING and audio is None:
            audio = parse_proximity_pairing(body)
        i += 2 + length
    if not types:
        return None
    return {"types": types, "audio": audio}
