"""Conservative decoders for binary SDRangel UDP payloads.

Adapted from SDRangel's GPL-3.0-or-later implementation:
RS41 parsing by Jon Beniston (2022), EOT parsing by Jon Beniston (2024)
and Eric Reuter (2018). Keep this adaptation under GPL-3.0-or-later.
CRC source copyright maintech GmbH (2012), Christian Daniel,
Edouard Griffiths (2015-2020), and Jon Beniston (2020).
Source: /plugins/channelrx/demodradiosonde and /plugins/channelrx/demodendoftrain.
"""

from __future__ import annotations

import math


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _reverse_bits(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _eot_crc(packet: bytes) -> tuple[int, int]:
    # SDRangel crc(18, 0x39A0F, MSB-first, init 0, xor 0x2b770),
    # followed by its 18-bit bit reversal for the transmitted field.
    crc = 0
    for value, width in [(packet[5] & 0x1F, 5), *[(packet[i], 8) for i in range(4, -1, -1)]]:
        crc ^= value << (18 - width)
        for _ in range(width):
            crc = ((crc << 1) ^ 0x39A0F) & 0x3FFFF if crc & 0x20000 else (crc << 1) & 0x3FFFF
    calculated = _reverse_bits(crc ^ 0x2B770, 18)
    received = (packet[7] << 11) | (packet[6] << 3) | ((packet[5] >> 5) & 7)
    return received, calculated


def _rs41(data: bytes):
    if len(data) not in (320, 518):
        raise ValueError("RS41 payload must be a 320-byte or 518-byte frame")
    expected_type = 0x0F if len(data) == 320 else 0xF0
    if data[0x38] != expected_type:
        raise ValueError("RS41 frame length marker does not match payload")

    fields: dict[str, object] = {"payload_hex": data.hex(), "frame_length": len(data)}
    blocks: dict[int, bytes] = {}
    checks_ok = True
    offset = 0x39
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("truncated RS41 sub-block header")
        block_id, size = data[offset], data[offset + 1]
        end = offset + 2 + size
        if end + 2 > len(data):
            raise ValueError("truncated RS41 sub-block")
        block = data[offset + 2 : end]
        received_crc = int.from_bytes(data[end : end + 2], "little")
        checks_ok &= _crc16_ccitt(block) == received_crc
        blocks[block_id] = block
        offset = end + 2

    status = blocks.get(0x79)
    if status is not None:
        if len(status) < 0x0B:
            raise ValueError("truncated RS41 status block")
        serial_bytes = status[2:10].split(b"\0", 1)[0]
        try:
            serial = serial_bytes.decode("ascii")
        except UnicodeDecodeError:
            serial = ""
        if serial and all(ch.isprintable() for ch in serial):
            fields["serial"] = serial
        fields["frame_number"] = int.from_bytes(status[:2], "little")
        fields["battery_v"] = status[0x0A] / 10.0
        if len(status) > 0x0D:
            fields["flight_phase_code"] = status[0x0D] & 3
        if len(status) > 0x10:
            fields["pcb_temperature_raw_c"] = status[0x10]

    gps = blocks.get(0x7B)
    if gps is not None:
        if len(gps) < 0x13:
            raise ValueError("truncated RS41 GPSPOS block")
        fields["gps_satellites"] = gps[0x12]
        if gps[0x12] > 0:
            fields["ecef_position_m"] = [
                int.from_bytes(gps[i : i + 4], "little", signed=True) / 100.0 for i in (0, 4, 8)
            ]
            fields["ecef_velocity_m_s"] = [
                int.from_bytes(gps[i : i + 2], "little", signed=True) / 100.0
                for i in (0x0C, 0x0E, 0x10)
            ]

    text = "RS41 " + " ".join(
        f"{key}={fields[key]}" for key in ("serial", "frame_number", "battery_v") if key in fields
    )
    return (
        text,
        fields,
        "text",
        "passed" if checks_ok else "failed",
        (
            "RS41 sub-block CRC-16 checks passed; SDRangel forwards after Reed-Solomon correction"
            if checks_ok
            else "RS41 sub-block CRC-16 check failed"
        ),
    )


def _eot(data: bytes):
    if len(data) != 8:
        raise ValueError("EOT payload must be exactly 8 bytes")
    received, calculated = _eot_crc(data)
    battery = (data[0] >> 2) & 3
    address = ((data[2] & 0xFF) << 9) | ((data[1] & 0xFF) << 1) | ((data[0] >> 7) & 1)
    fields = {
        "payload_hex": data.hex(),
        "address": address,
        "type": (data[0] >> 4) & 7,
        "battery_condition": ("N/A", "very low", "low", "ok")[battery],
        "pressure_psig": data[3] & 0x7F,
        "battery_charge_percent": round((data[4] & 0x7F) * 100 / 127, 1),
        "valve_circuit_ok": bool(data[4] & 0x80),
        "confirmation": bool(data[5] & 1),
        "turbine": bool(data[5] & 2),
        "motion": bool(data[5] & 4),
        "marker_light_battery_low": bool(data[5] & 8),
        "marker_light_on": bool(data[5] & 0x10),
        "crc_received": received,
        "crc_calculated": calculated,
    }
    valid = received == calculated
    text = (
        f"EOT address={address} pressure={fields['pressure_psig']} psig "
        f"battery={fields['battery_condition']} motion={fields['motion']}"
    )
    return (
        text,
        fields,
        "text",
        "passed" if valid else "failed",
        ("SDRangel EOT CRC-18 verified" if valid else "SDRangel EOT CRC-18 mismatch"),
    )


def decode_payload(decoder_id: str, data: bytes) -> tuple[str, dict, str, str, str]:
    """Return display text, structured fields, kind, integrity, and evidence note."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("payload must be non-empty bytes")
    if decoder_id == "rs41":
        return _rs41(data)
    if decoder_id == "endoftrain":
        return _eot(data)
    if decoder_id == "ils":
        try:
            ddm = float(data.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("ILS DDM payload must be ASCII numeric text") from exc
        if not math.isfinite(ddm):
            raise ValueError("ILS DDM must be finite")
        return (
            f"ILS DDM {ddm:.3f}",
            {"ddm": ddm},
            "measurement",
            "unavailable",
            ("ILS DDM measurement; not a validated message"),
        )
    if decoder_id == "dsc":
        hex_payload = data.hex()
        return (
            f"DSC symbols {hex_payload}",
            {"payload_hex": hex_payload},
            "text",
            "unknown",
            ("SDRangel DSC symbols retained as hex; message ECC was not parsed"),
        )
    raise ValueError(f"unsupported SDRangel payload decoder: {decoder_id}")
