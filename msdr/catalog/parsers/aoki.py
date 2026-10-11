"""Aoki/Nagoya shortwave "bi" (broadcasting information) list. Two real-world
layouts are supported, detected from the header/ruler present in each file:

- xta26 layout (e.g. xta26.txt): fixed-width, one header line, columns
  FRE/STATION/UTC/Su-W-Sa/Language/Pow/Azi/Location/ADM/L/L/Remarks. The
  DAYS column is itself positional (Sunday-first, one character per
  weekday); inactive days are blanked with a space or '.', e.g. '123.567'
  (Wed off) or ' 23456  ' (Sun+Sat off).
- userlist1 layout (e.g. userlist1.txt): fixed-width with an explicit
  '+---+' ruler line, columns kHz/Time(UTC)/ITU/Station/Lang./Location/Days.
  DAYS here is concatenated digits and/or 'a-b' ranges with no separator,
  e.g. '1-7', '124-7' (1,2,4-7), '5-3' (a range wrapping past 7 back to 1).

Both files carry a "Day 1 = Sunday" banner -- Aoki is Sunday-first.
"""

from __future__ import annotations

import re

from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers._days import digit_ranges_to_mask, digits_to_mask
from msdr.library.models import now_iso

_TIME_RE = re.compile(r"(\d{4})-(\d{4})")
_FREQ_RE = re.compile(r"^\d+(?:\.\d+)?")
_COORD_RE = re.compile(r"\d{6}[NS]\d{7}[EW]")

# xta26: fixed-width column bounds, derived from the header line's token
# positions (the file ships no ruler line). The trailing Remarks column is
# left open-ended since it's free text.
_XTA_FREQ = (0, 6)
_XTA_STATION = (6, 38)
_XTA_UTC = (38, 48)
_XTA_DAYS = (48, 56)
_XTA_LANG_POW = (56, 81)  # Language + Pow share a boundary that drifts with
# power's digit count (e.g. a 4-digit "1000" kW value shifts one column
# left of the nominal 3-digit width) -- split from the right instead.
_XTA_AZIMUTH = (81, 85)
_XTA_LOCATION = (85, 109)
_XTA_ADM = (109, 113)
_XTA_REMARKS = (129, None)


def _slice(line: str, seg: tuple[int, int | None]) -> str:
    s, e = seg
    return (line[s:] if e is None else line[s:e]).strip()


def parse_aoki(text: str) -> list[CatalogEntry]:
    lines = text.splitlines()
    for line in lines[:4]:
        if "STATION" in line and "Su-W-Sa" in line:
            return _parse_xta26(lines)
        if "ITU" in line and "Days" in line and "Station" in line:
            return _parse_userlist1(lines)
    return []


def _parse_xta26(lines: list[str]) -> list[CatalogEntry]:
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in lines:
        if not line.strip():
            continue
        freq_raw = _slice(line, _XTA_FREQ)
        fm = _FREQ_RE.match(freq_raw)
        if not fm:
            continue  # banner/header line, or not a data row
        tm = _TIME_RE.search(_slice(line, _XTA_UTC))
        if not tm:
            continue

        station = _slice(line, _XTA_STATION)

        days_raw = _slice(line, _XTA_DAYS)
        digits = "".join(c for c in days_raw if c.isdigit())
        days = digits_to_mask(digits, first_day="sunday") if digits else None

        # Language and Pow are packed into one chunk with a variable-width
        # boundary (see _XTA_LANG_POW); split off Pow's trailing digits.
        chunk = _slice(line, _XTA_LANG_POW)
        pm = re.search(r"([\d.]+)$", chunk)
        if pm:
            language = chunk[: pm.start()].strip() or None
            try:
                power_kw = float(pm.group(1))
            except ValueError:
                power_kw = None
        else:
            language = chunk or None
            power_kw = None

        azimuth = _slice(line, _XTA_AZIMUTH)
        location = _COORD_RE.sub("", _slice(line, _XTA_LOCATION)).strip() or None
        country = _slice(line, _XTA_ADM) or None
        remarks = _slice(line, _XTA_REMARKS)

        notes_parts = []
        if azimuth:
            notes_parts.append(f"az {azimuth}")
        if remarks:
            notes_parts.append(remarks)

        entries.append(
            CatalogEntry(
                source="aoki",
                frequency_hz=round(float(fm.group()) * 1000),
                station=station or "Unknown",
                mode="AM",
                bandwidth_hz=9000,
                start_utc=tm.group(1),
                stop_utc=tm.group(2),
                days=days,
                language=language,
                location=location,
                country=country,
                power_kw=power_kw,
                notes="; ".join(notes_parts) or None,
                imported_at=imported_at,
            )
        )
    return entries


def _plus_ruler_segments(ruler: str) -> list[tuple[int, int | None]]:
    """userlist1's ruler ('+-------------+---------+...') leads with a
    border '+' that isn't echoed in the header/data lines, so strip it
    before splitting -- otherwise every column reads one char short."""
    body = ruler[1:] if ruler.startswith("+") else ruler
    segs: list[tuple[int, int | None]] = []
    start = 0
    for i, ch in enumerate(body):
        if ch == "+":
            segs.append((start, i))
            start = i + 1
    segs.append((start, None))  # last column (Days) is variable-width
    return segs


def _parse_userlist1(lines: list[str]) -> list[CatalogEntry]:
    # kHz, Time(UTC), <gap>, ITU, Station, Lang., Location, Days
    ruler_idx = next((i for i, ln in enumerate(lines) if ln.startswith("+")), None)
    if ruler_idx is None:
        return []
    segs = _plus_ruler_segments(lines[ruler_idx])
    if len(segs) < 8:
        return []
    khz_seg, time_seg, _gap_seg, itu_seg, station_seg, lang_seg, loc_seg, days_seg = segs[:8]

    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in lines[ruler_idx + 1 :]:
        if not line.strip():
            continue
        khz_raw = _slice(line, khz_seg)
        if not _FREQ_RE.match(khz_raw):
            continue
        tm = _TIME_RE.search(_slice(line, time_seg))
        if not tm:
            continue

        days_raw = _slice(line, days_seg)
        days = digit_ranges_to_mask(days_raw, first_day="sunday")

        entries.append(
            CatalogEntry(
                source="aoki",
                frequency_hz=round(float(khz_raw) * 1000),
                station=_slice(line, station_seg) or "Unknown",
                mode="AM",
                bandwidth_hz=9000,
                start_utc=tm.group(1),
                stop_utc=tm.group(2),
                days=days,
                language=_slice(line, lang_seg) or None,
                location=_slice(line, loc_seg) or None,
                country=_slice(line, itu_seg) or None,
                imported_at=imported_at,
            )
        )
    return entries
