"""NDXC (ndxc.info) shortwave schedule: a semicolon-separated CSV with no
header row. Column order: kHz;HHMM-HHMM;Days;ITU;Station;Language;Azimuth
(degrees, or 'ND' for non-directional, or blank);Broadcaster;(trailing
empty field). Days are Sunday-first digits, optionally with '.' filler for
an inactive weekday in its own position (same positional convention as
Aoki's xta26 layout), e.g. '.234567' (Sunday off)."""

from __future__ import annotations

import re

from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers._days import digits_to_mask
from msdr.library.models import now_iso

_NUM_FIELDS = 8
_TIME_RE = re.compile(r"^(\d{4})-(\d{4})$")

default_url = "https://ndxc.info/wordpress/wp-content/uploads/freqdb/sked.csv"


def parse_ndxc(text: str) -> list[CatalogEntry]:
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in text.splitlines():
        line = line.strip("\r\n")
        if not line.strip():
            continue
        fields = line.split(";")
        if len(fields) < _NUM_FIELDS:
            fields += [""] * (_NUM_FIELDS - len(fields))
        khz, time_raw, days_raw, itu, station, lang, azimuth, broadcaster = fields[:_NUM_FIELDS]

        khz = khz.strip()
        try:
            frequency_hz = round(float(khz) * 1000)
        except ValueError:
            continue  # not a data row (stray blank/malformed line)

        tm = _TIME_RE.match(time_raw.strip())
        start_utc, stop_utc = (tm.group(1), tm.group(2)) if tm else (None, None)

        digits = "".join(c for c in days_raw if c.isdigit())
        days = digits_to_mask(digits, first_day="sunday") if digits else None

        azimuth = azimuth.strip()
        broadcaster = broadcaster.strip()
        notes_parts = []
        if azimuth:
            notes_parts.append("non-directional" if azimuth.upper() == "ND" else f"az {azimuth}")
        if broadcaster:
            notes_parts.append(broadcaster)

        entries.append(
            CatalogEntry(
                source="ndxc",
                frequency_hz=frequency_hz,
                station=station.strip() or "Unknown",
                mode="AM",
                bandwidth_hz=9000,
                start_utc=start_utc,
                stop_utc=stop_utc,
                days=days,
                language=lang.strip() or None,
                country=itu.strip() or None,
                notes="; ".join(notes_parts) or None,
                imported_at=imported_at,
            )
        )
    return entries
