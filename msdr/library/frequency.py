"""Frequency text parsing and display, per design spec §8."""

import re

_SUFFIX_MULT = {
    "ghz": 1_000_000_000.0,
    "mhz": 1_000_000.0,
    "khz": 1_000.0,
    "hz": 1.0,
    "g": 1_000_000_000.0,
    "m": 1_000_000.0,
    "k": 1_000.0,
}

# Optional space before the suffix is absorbed by stripping whitespace up front.
_PATTERN = re.compile(r"^(?P<num>\d+\.?\d*)(?P<suffix>ghz|mhz|khz|hz|g|m|k)?$", re.IGNORECASE)


def parse_frequency(text: str) -> int | None:
    """Parse a user-typed frequency. Explicit suffix wins; otherwise a decimal
    point means MHz, a bare integer > 30_000 means Hz, and <= 30_000 means MHz."""
    cleaned = re.sub(r"[\s_,]", "", text)
    if not cleaned:
        return None
    match = _PATTERN.match(cleaned)
    if match is None:
        return None
    num_str = match.group("num")
    suffix = match.group("suffix")
    try:
        num = float(num_str)
    except ValueError:
        return None
    if suffix is not None:
        multiplier = _SUFFIX_MULT[suffix.lower()]
    elif "." in num_str:
        multiplier = 1_000_000.0
    elif int(num_str) > 30_000:
        multiplier = 1.0
    else:
        multiplier = 1_000_000.0
    hz = round(num * multiplier)
    return hz if hz > 0 else None


def format_mhz(hz: int) -> str:
    """e.g. 137_900_000 -> "137.900000 MHz"."""
    return f"{hz / 1_000_000:.6f} MHz"


def format_readout(hz: int) -> str:
    """e.g. 137_900_000 -> "137.900.000" (digit groups of three from the right)."""
    digits = str(int(hz))
    groups = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return ".".join(reversed(groups))
