"""Day-mask helpers shared by the schedule parsers. EiBi, Aoki and HFCC each
speak a different day notation, but all reduce to the same 'MTWTFSS' mask.

Two numbering conventions show up in the wild: HFCC and EiBi count days
Monday-first (1=Mon..7=Sun); Aoki and NDXC count Sunday-first (1=Sun..7=Sat,
per each file's own "Day 1 = Sunday" banner). `first_day` selects which."""

import re

_MASK_LETTERS = "MTWTFSS"
_DAY_INDEX = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}
_IRREGULAR = {"irr", "alt"}

DAYS_TOKEN_RE = re.compile(r"^[1-7]{1,7}$")


def _digit_to_index(digit: int, first_day: str) -> int:
    """Map a 1-7 day digit to a Monday-first mask index (0=Mon..6=Sun)."""
    if first_day == "sunday":
        return (digit - 2) % 7
    return digit - 1


def _mask_from_indices(indices: set[int]) -> str | None:
    if not indices:
        return None
    mask = ["."] * 7
    for idx in indices:
        mask[idx] = _MASK_LETTERS[idx]
    return "".join(mask)


def digits_to_mask(raw: str, *, first_day: str = "monday") -> str | None:
    """A run of digits 1-7, e.g. '1234567' (daily) or '135' (Mon/Wed/Fri
    only per `first_day`). Order and repeats don't matter."""
    if not raw or not raw.isdigit():
        return None
    # Out-of-range digits (0, 8, 9) are junk in a hand-typed cell: drop them
    # rather than raise and abort the whole import (or map 0 to Sunday).
    indices = {_digit_to_index(int(ch), first_day) for ch in raw if ch in "1234567"}
    return _mask_from_indices(indices)


def digit_ranges_to_mask(raw: str, *, first_day: str = "monday") -> str | None:
    """Concatenated day digits and/or 'a-b' ranges with no separator, e.g.
    userlist1's Days column: '1-7', '124-7' (-> 1,2,4-7), '5-3' (a range
    wrapping past 7 back around to 1, i.e. 5,6,7,1,2,3). Digits are 1-7 per
    `first_day`."""
    raw = (raw or "").strip()
    if not raw:
        return None
    indices: set[int] = set()
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if not ch.isdigit():
            i += 1
            continue
        if i + 2 < n and raw[i + 1] == "-" and raw[i + 2].isdigit():
            start, end = int(ch), int(raw[i + 2])
            # A digit outside 1-7 never matches the cycle below (1-9 would
            # loop forever), so skip the whole range.
            if 1 <= start <= 7 and 1 <= end <= 7:
                d = start
                while True:
                    indices.add(_digit_to_index(d, first_day))
                    if d == end:
                        break
                    d = d % 7 + 1
            i += 3
        else:
            if ch in "1234567":
                indices.add(_digit_to_index(int(ch), first_day))
            i += 1
    return _mask_from_indices(indices)


def eibi_days_to_mask(raw: str) -> str | None:
    """EiBi's Days column: empty = daily (None); 'irr'/'alt' (irregular /
    alternate days) has no fixed mask, so it also falls back to None; digit
    runs like '1234567'; or day lists/ranges like 'Mo-Fr', 'Sa,Su', 'Mo'.
    EiBi is Monday-first."""
    raw = raw.strip()
    if not raw or raw.lower() in _IRREGULAR:
        return None
    if raw.isdigit():
        return digits_to_mask(raw)
    days: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (p.strip() for p in part.split("-", 1))
            if a not in _DAY_INDEX or b not in _DAY_INDEX:
                continue
            ai, bi = _DAY_INDEX[a], _DAY_INDEX[b]
            if ai <= bi:
                days.update(range(ai, bi + 1))
            else:
                days.update(range(ai, 7))
                days.update(range(0, bi + 1))
        elif part in _DAY_INDEX:
            days.add(_DAY_INDEX[part])
    if not days:
        return None
    mask = ["."] * 7
    for idx in days:
        mask[idx] = _MASK_LETTERS[idx]
    return "".join(mask)
