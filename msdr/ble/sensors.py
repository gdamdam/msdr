"""BLE sensor beacon decoders (docs/ble.md "Sensors"): explicit, per-protocol
parsers for published formats only. Nothing here infers the contents of an
encrypted payload -- an encrypted BTHome packet is reported as
`{"encrypted": true}` with no readings, never guessed. Unrecognised object
ids stop decoding of the remaining objects in that packet rather than
guessing a length and misaligning everything after.

Fixtures (tests/ble/test_sensors.py) are taken from each format's published
specification, fetched 2026-10-03 (retrying `http.client.IncompleteRead`,
which several of these hosts returned on the first attempt):

- BTHome v2: https://bthome.io/format/ (worked object-table examples, e.g.
  temperature object `0x02 C409` / `0x02 CA09`, humidity `0x03 BF13`, the
  device-information byte `0x40` = unencrypted v2, bit 0 = encryption flag).
- Ruuvi RAWv2 (data format 5, manufacturer id 0x0499):
  https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
  (byte layout) and the test vectors in
  https://github.com/ruuvi/ruuvi-sensor-protocols/blob/master/dataformat_05.md
  ("valid data", "maximum values", "minimum values", "Invalid values" cases).
- iBeacon: https://developer.apple.com/ibeacon/Getting-Started-with-iBeacon.pdf
  (UUID/major/minor semantics, the worked
  `D9B9EC1F-3925-43D0-80A9-1E39D4CEA95C` example). Apple's document does not
  itself publish the raw manufacturer-data byte layout (company id 0x004C,
  type 0x02, length 0x15) -- that packet format requires a paid iBeacon
  license to obtain from Apple directly, but it is the layout implemented
  (and published) by essentially every third-party iBeacon scanner/library,
  and is treated here as the de-facto published wire format.
- Eddystone (service data UUID 0xFEAA):
  https://github.com/google/eddystone/blob/master/protocol-specification.md,
  .../eddystone-uid/README.md, .../eddystone-url/README.md,
  .../eddystone-tlm/tlm-plain.md (UID/URL/TLM frame layouts).
- pvvx/ATC custom thermometer format (service data UUID 0x181A):
  https://github.com/pvvx/ATC_MiThermometer#custom-format-all-data-little-endian
  (pvvx 15-byte little-endian layout) and
  https://github.com/atc1441/ATC_MiThermometer#advertising-format-of-the-custom-firmware
  (ATC1441 13-byte big-endian layout). The two are disambiguated by length
  alone (13 vs 15 bytes of service data after the UUID), which never overlaps.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

#: 16-bit service UUIDs these parsers key off (docs/ble.md / msdr/ble/adv.py
#: already strip the UUID itself out of `Advertisement.service_data`).
BTHOME_UUID = "0xfcd2"
EDDYSTONE_UUID = "0xfeaa"
ATC_UUID = "0x181a"
RUUVI_COMPANY = 0x0499
APPLE_COMPANY = 0x004C


@dataclass
class Reading:
    value: float | int | bool | str | None
    unit: str | None = None

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit}


@dataclass
class SensorReport:
    protocol: str
    readings: dict[str, Reading] = field(default_factory=dict)
    encrypted: bool = False
    malformed: bool = False

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "readings": {k: v.to_dict() for k, v in self.readings.items()},
            "encrypted": self.encrypted,
        }


# ---------------------------------------------------------------------------
# BTHome v2 -- https://bthome.io/format/
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BtHomeObject:
    name: str
    length: int
    factor: float | None  # None for binary objects
    unit: str | None
    signed: bool = False
    binary: bool = False


#: Only the object ids this module exposes as readings (docs/ble.md Phase 5:
#: "temperature, humidity, battery, pressure, illuminance, motion/door
#: binary"), plus packet id (0x00, common in real packets, consumed but not
#: exposed). An id outside this table halts decoding of later objects in the
#: same packet -- see module docstring.
_BTHOME_OBJECTS: dict[int, _BtHomeObject] = {
    0x00: _BtHomeObject("packet_id", 1, 1, None),
    0x01: _BtHomeObject("battery", 1, 1, "%"),
    0x02: _BtHomeObject("temperature", 2, 0.01, "°C", signed=True),
    0x03: _BtHomeObject("humidity", 2, 0.01, "%"),
    0x04: _BtHomeObject("pressure", 3, 0.01, "hPa"),
    0x05: _BtHomeObject("illuminance", 3, 0.01, "lx"),
    0x1A: _BtHomeObject("door", 1, None, None, binary=True),
    0x21: _BtHomeObject("motion", 1, None, None, binary=True),
}
_BTHOME_HIDDEN = frozenset({"packet_id"})


def parse_bthome(service_data: bytes) -> SensorReport | None:
    """`service_data` is the BTHome service-data value, i.e. everything after
    the 0xFCD2 UUID: the device-information byte followed by TLV objects."""
    if len(service_data) < 1:
        return None
    info = service_data[0]
    report = SensorReport(protocol="bthome", encrypted=bool(info & 0x01))
    if report.encrypted:
        return report  # ciphertext -- reported as encrypted, never guessed
    idx = 1
    while idx < len(service_data):
        obj_id = service_data[idx]
        defn = _BTHOME_OBJECTS.get(obj_id)
        if defn is None:
            report.malformed = True
            break
        idx += 1
        raw = service_data[idx : idx + defn.length]
        if len(raw) < defn.length:
            report.malformed = True
            break
        idx += defn.length
        if defn.name in _BTHOME_HIDDEN:
            continue
        if defn.binary:
            report.readings[defn.name] = Reading(bool(raw[0]), None)
            continue
        value = int.from_bytes(raw, "little", signed=defn.signed)
        report.readings[defn.name] = Reading(round(value * defn.factor, 2), defn.unit)
    return report


# ---------------------------------------------------------------------------
# Ruuvi RAWv2 (data format 5) --
# https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
# ---------------------------------------------------------------------------

_RUUVI_LEN = 24


def parse_ruuvi(data: bytes) -> SensorReport | None:
    """`data` is the manufacturer-specific-data value for company id 0x0499,
    i.e. everything after the 2-byte company id."""
    if len(data) < _RUUVI_LEN or data[0] != 5:
        return None
    report = SensorReport(protocol="ruuvi_rawv2")
    temp_raw = struct.unpack(">h", data[1:3])[0]
    if temp_raw != -32768:
        report.readings["temperature"] = Reading(round(temp_raw * 0.005, 3), "°C")
    hum_raw = struct.unpack(">H", data[3:5])[0]
    if hum_raw != 0xFFFF:
        report.readings["humidity"] = Reading(round(hum_raw * 0.0025, 4), "%")
    pres_raw = struct.unpack(">H", data[5:7])[0]
    if pres_raw != 0xFFFF:
        report.readings["pressure"] = Reading(round((pres_raw + 50000) / 100, 2), "hPa")
    power = struct.unpack(">H", data[13:15])[0]
    batt_field = power >> 5
    if batt_field != 0x7FF:
        report.readings["battery"] = Reading(round((batt_field + 1600) / 1000, 3), "V")
    txp_idx = power & 0x1F
    if txp_idx != 0x1F:
        report.readings["tx_power"] = Reading(txp_idx * 2 - 40, "dBm")
    move = data[15]
    if move != 0xFF:
        report.readings["movement_counter"] = Reading(move, "count")
    return report


# ---------------------------------------------------------------------------
# iBeacon -- Apple company id 0x004C, type 0x02
# (https://developer.apple.com/ibeacon/Getting-Started-with-iBeacon.pdf)
# ---------------------------------------------------------------------------

_IBEACON_LEN = 23  # type(1) + length(1) + uuid(16) + major(2) + minor(2) + power(1)


def parse_ibeacon(data: bytes) -> SensorReport | None:
    """`data` is the manufacturer-specific-data value for company id 0x004C."""
    if len(data) < _IBEACON_LEN or data[0] != 0x02 or data[1] != 0x15:
        return None
    u = data[2:18]
    major, minor = struct.unpack(">HH", data[18:22])
    measured_power = struct.unpack(">b", data[22:23])[0]
    uuid = (
        f"{u[0:4].hex()}-{u[4:6].hex()}-{u[6:8].hex()}-{u[8:10].hex()}-{u[10:16].hex()}"
    )
    report = SensorReport(protocol="ibeacon")
    report.readings["uuid"] = Reading(uuid, None)
    report.readings["major"] = Reading(major, None)
    report.readings["minor"] = Reading(minor, None)
    report.readings["measured_power"] = Reading(measured_power, "dBm")
    return report


# ---------------------------------------------------------------------------
# Eddystone -- service data UUID 0xFEAA
# (https://github.com/google/eddystone, protocol-specification.md +
# eddystone-uid/eddystone-url/eddystone-tlm READMEs)
# ---------------------------------------------------------------------------

_EDDYSTONE_URL_SCHEMES = {0: "http://www.", 1: "https://www.", 2: "http://", 3: "https://"}
_EDDYSTONE_URL_EXPANSIONS = {
    0x00: ".com/", 0x01: ".org/", 0x02: ".edu/", 0x03: ".net/", 0x04: ".info/",
    0x05: ".biz/", 0x06: ".gov/", 0x07: ".com", 0x08: ".org", 0x09: ".edu",
    0x0A: ".net", 0x0B: ".info", 0x0C: ".biz", 0x0D: ".gov",
}


def _decode_eddystone_url(encoded: bytes) -> str:
    if not encoded:
        return ""
    out = [_EDDYSTONE_URL_SCHEMES.get(encoded[0], "")]
    for b in encoded[1:]:
        if b in _EDDYSTONE_URL_EXPANSIONS:
            out.append(_EDDYSTONE_URL_EXPANSIONS[b])
        elif 0x20 < b < 0x7F:
            out.append(chr(b))
        # Reserved/invalid bytes (0x0E-0x20, 0x7F-0xFF per spec) are dropped
        # rather than guessed.
    return "".join(out)


def parse_eddystone(data: bytes) -> SensorReport | None:
    """`data` is the Eddystone service-data value, i.e. everything after the
    0xFEAA UUID: the frame-type byte followed by the frame's own layout."""
    if len(data) < 1:
        return None
    frame = data[0]
    report = SensorReport(protocol="eddystone")
    if frame == 0x00:  # UID
        if len(data) < 18:
            report.malformed = True
            return report
        report.readings["tx_power"] = Reading(struct.unpack(">b", data[1:2])[0], "dBm")
        report.readings["namespace"] = Reading(data[2:12].hex(), None)
        report.readings["instance"] = Reading(data[12:18].hex(), None)
    elif frame == 0x10:  # URL
        if len(data) < 2:
            report.malformed = True
            return report
        report.readings["tx_power"] = Reading(struct.unpack(">b", data[1:2])[0], "dBm")
        report.readings["url"] = Reading(_decode_eddystone_url(data[2:]), None)
    elif frame == 0x20:  # TLM (unencrypted only; encrypted TLM is a separate,
        # unpublished-here frame layout -- treated as unrecognised below)
        if len(data) < 14 or data[1] != 0x00:
            report.malformed = True
            return report
        vbatt = struct.unpack(">H", data[2:4])[0]
        temp = struct.unpack(">h", data[4:6])[0] / 256.0
        adv_cnt = int.from_bytes(data[6:10], "big")
        sec_cnt = int.from_bytes(data[10:14], "big")
        report.readings["battery_voltage"] = Reading(vbatt, "mV")
        report.readings["temperature"] = Reading(round(temp, 2), "°C")
        report.readings["adv_count"] = Reading(adv_cnt, "count")
        report.readings["uptime"] = Reading(round(sec_cnt / 10.0, 1), "s")
    else:
        return None  # not a recognised Eddystone frame type
    return report


# ---------------------------------------------------------------------------
# pvvx/ATC custom thermometer format -- service data UUID 0x181A
# ---------------------------------------------------------------------------

# MAC(6) + temp(2) + humidity(1) + battery%(1) + battery_mV(2) + counter(1)
_ATC1441_LEN = 13
# MAC(6) + temp(2) + humidity(2) + battery_mV(2) + battery%(1) + counter(1) + flags(1)
_PVVX_LEN = 15


def _parse_atc1441(data: bytes) -> SensorReport:
    """ATC1441 format, big-endian
    (https://github.com/atc1441/ATC_MiThermometer#advertising-format-of-the-custom-firmware)."""
    mac = data[0:6].hex(":")
    temp = struct.unpack(">h", data[6:8])[0]
    humidity = data[8]
    battery_pct = data[9]
    battery_mv = struct.unpack(">H", data[10:12])[0]
    counter = data[12]
    report = SensorReport(protocol="atc1441")
    report.readings["mac"] = Reading(mac, None)
    report.readings["temperature"] = Reading(round(temp * 0.1, 1), "°C")
    report.readings["humidity"] = Reading(humidity, "%")
    report.readings["battery"] = Reading(battery_pct, "%")
    report.readings["battery_voltage"] = Reading(battery_mv, "mV")
    report.readings["counter"] = Reading(counter, "count")
    return report


def _parse_pvvx(data: bytes) -> SensorReport:
    """pvvx custom extended format, little-endian
    (https://github.com/pvvx/ATC_MiThermometer#custom-format-all-data-little-endian)."""
    mac = data[0:6][::-1].hex(":")
    temp = struct.unpack("<h", data[6:8])[0]
    humidity = struct.unpack("<H", data[8:10])[0]
    battery_mv = struct.unpack("<H", data[10:12])[0]
    battery_pct = data[12]
    counter = data[13]
    flags = data[14]
    report = SensorReport(protocol="pvvx")
    report.readings["mac"] = Reading(mac, None)
    report.readings["temperature"] = Reading(round(temp * 0.01, 2), "°C")
    report.readings["humidity"] = Reading(round(humidity * 0.01, 2), "%")
    report.readings["battery_voltage"] = Reading(battery_mv, "mV")
    report.readings["battery"] = Reading(battery_pct, "%")
    report.readings["counter"] = Reading(counter, "count")
    report.readings["flags"] = Reading(flags, None)
    return report


def parse_atc(data: bytes) -> SensorReport | None:
    """`data` is the 0x181A service-data value. The two layouts never share a
    length (13 vs 15 bytes), so length alone disambiguates them."""
    if len(data) == _ATC1441_LEN:
        return _parse_atc1441(data)
    if len(data) == _PVVX_LEN:
        return _parse_pvvx(data)
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def decode(
    service_data: dict[str, bytes], manufacturer_data: dict[int, bytes]
) -> SensorReport | None:
    """Try each known protocol against one advertisement's service/manufacturer
    data (`adv.Advertisement.service_data`/`.manufacturer_data`). Returns the
    first match -- a real device advertises one sensor format, not several."""
    bthome = service_data.get(BTHOME_UUID)
    if bthome is not None:
        parsed = parse_bthome(bthome)
        if parsed is not None:
            return parsed
    eddystone = service_data.get(EDDYSTONE_UUID)
    if eddystone is not None:
        parsed = parse_eddystone(eddystone)
        if parsed is not None:
            return parsed
    atc = service_data.get(ATC_UUID)
    if atc is not None:
        parsed = parse_atc(atc)
        if parsed is not None:
            return parsed
    ruuvi = manufacturer_data.get(RUUVI_COMPANY)
    if ruuvi is not None:
        parsed = parse_ruuvi(ruuvi)
        if parsed is not None:
            return parsed
    apple = manufacturer_data.get(APPLE_COMPANY)
    if apple is not None:
        parsed = parse_ibeacon(apple)
        if parsed is not None:
            return parsed
    return None
