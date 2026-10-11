"""Conservative semantic parsing for DSC symbols.

The field positions and symbol values follow SDRangel's DSCMessage decoder
(Jon Beniston, M7RCE, 2023; GPL-3.0-or-later). Only well-bounded call layouts
are decoded here; unsupported layouts retain their raw symbols upstream.
"""

from __future__ import annotations

_FORMAT = {
    102: "geographic_call",
    112: "distress_alert",
    114: "group_call",
    116: "all_ships",
    120: "selective_call",
    123: "automatic_call",
}
_CATEGORY = {100: "routine", 108: "safety", 110: "urgency", 112: "distress"}
_NATURE = {
    100: "fire_explosion",
    101: "flooding",
    102: "collision",
    103: "grounding",
    104: "listing",
    105: "sinking",
    106: "adrift",
    107: "undesignated",
    108: "abandoning_ship",
    109: "piracy_armed_attack",
    110: "man_overboard",
    112: "epirb",
}
_EOS = {117: "request_ack", 122: "ack", 127: "eos"}


def _digits(symbols: list[int]) -> str | None:
    if any(not 0 <= s <= 99 for s in symbols):
        return None
    return "".join(f"{s:02d}" for s in symbols)


def _mmsi(symbols: list[int]) -> str | None:
    digits = _digits(symbols)
    if digits is None or not digits.endswith("0"):
        return None
    # A 10th zero is the normal DSC representation of a 9-digit MMSI. A
    # nonzero equipment suffix is not promoted to an MMSI identity.
    return digits[:9]


def _position(symbols: list[int]) -> tuple[float, float] | None:
    digits = _digits(symbols)
    if digits is None or len(digits) != 10 or digits == "9999999999":
        return None
    quadrant = int(digits[0])
    lat_d, lat_m = int(digits[1:3]), int(digits[3:5])
    lon_d, lon_m = int(digits[5:8]), int(digits[8:10])
    if (
        quadrant > 3
        or lat_d > 90
        or lon_d > 180
        or lat_m > 59
        or lon_m > 59
        or (lat_d == 90 and lat_m)
        or (lon_d == 180 and lon_m)
    ):
        return None
    lat = lat_d + lat_m / 60
    lon = lon_d + lon_m / 60
    if quadrant & 2:
        lat = -lat
    if quadrant & 1:
        lon = -lon
    return lat, lon


def parse_dsc_symbols(symbols: list[int], *, ecc_ok: bool) -> dict:
    """Return safe, display-ready fields; no semantic claims without ECC."""
    out: dict = {}
    if not ecc_ok or len(symbols) < 9 or any(not 0 <= int(x) <= 127 for x in symbols):
        return out
    # The first format specifier is sent twice; the symbol stream contains one
    # copy, as the framing decoder consumes the diversity copy in phasing.
    fmt = symbols[0]
    if symbols[-2] not in _EOS:
        return out
    out.update(format_code=fmt, format=_FORMAT.get(fmt), end_of_signal=_EOS[symbols[-2]])
    end = len(symbols) - 2  # EOS and XOR ECC are the final two symbols.
    if fmt == 112:  # Distress alert: sender, nature, position, UTC, comms mode.
        if end < 15:
            return {}
        sender = _mmsi(symbols[1:6])
        nature = _NATURE.get(symbols[6])
        pos = _position(symbols[7:12])
        if sender is None or nature is None:
            return {}
        out.update(sender_mmsi=sender, distress_nature=nature)
        if pos is not None:
            out.update(latitude=pos[0], longitude=pos[1])
        utc = _digits(symbols[12:14])
        if utc and utc != "8888":
            hh, mm = int(utc[:2]), int(utc[2:])
            if hh < 24 and mm < 60:
                out["utc_time"] = f"{hh:02d}:{mm:02d}"
        out["subsequent_communications_code"] = symbols[14]
        return out
    # Selective/group/all-ships routine/safety/urgency calls. Other formats
    # have different optional fields and are intentionally left raw.
    if fmt not in (114, 116, 120):
        return out
    if fmt == 116:
        if end < 9:
            return out
        category = _CATEGORY.get(symbols[1])
        sender = _mmsi(symbols[2:7])
        command_idx = 7
    else:
        if end < 14:
            return out
        target = _mmsi(symbols[1:6])
        category = _CATEGORY.get(symbols[6])
        sender = _mmsi(symbols[7:12])
        command_idx = 12
    if sender is None or category not in ("routine", "safety", "urgency"):
        return out
    if end < command_idx + 2:
        return {}
    if fmt != 116:
        if target is None:
            return out
        out["group_mmsi" if fmt == 114 else "recipient_mmsi"] = target
    out.update(
        sender_mmsi=sender,
        category=category,
        telecommand=symbols[command_idx],
        telecommand2=symbols[command_idx + 1],
    )
    # ITU-R M.493-16 Table A1-4.7 encodes an individual safety position
    # request as TC1=121, TC2=126, followed by six 126 symbols. The response
    # uses the same two telecommands; Pos4 is either five Pos1 symbols plus
    # 126 (agree) or six 126 symbols (decline), followed by UTC HHMM.
    if fmt == 120 and category == "safety" and symbols[command_idx : command_idx + 2] == [121, 126]:
        message = symbols[command_idx + 2 : end]
        if message == [126] * 6:
            out["position_request"] = True
        elif len(message) >= 8 and message[:6] == [126] * 6:
            out["position_acknowledgement"] = "declined"
            utc = _digits(message[6:8])
            if utc and utc != "8888":
                hh, mm = int(utc[:2]), int(utc[2:])
                if hh < 24 and mm < 60:
                    out["utc_time"] = f"{hh:02d}:{mm:02d}"
        elif len(message) >= 8 and message[5] == 126:
            pos = _position(message[:5])
            utc = _digits(message[6:8])
            if pos is not None:
                out["position_acknowledgement"] = "agreed"
                out.update(latitude=pos[0], longitude=pos[1])
            if utc and utc != "8888":
                hh, mm = int(utc[:2]), int(utc[2:])
                if hh < 24 and mm < 60:
                    out["utc_time"] = f"{hh:02d}:{mm:02d}"
    return out
