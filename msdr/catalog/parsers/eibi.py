"""EiBi (Eike Bierwirth) shortwave schedule: a semicolon-separated CSV,
published per season as sked-a25.csv / sked-b25.csv at eibispace.de.

Column order (per season file): kHz; Time(UTC) HHMM-HHMM or '24h'; Days;
ITU country code; Station; Lng (language); Target; Remarks; P; Start(date);
Stop(date). The trailing Start/Stop are validity dates within the season,
not a daily time window, and aren't represented in CatalogEntry -- out of
scope here (the season code itself already captures the ~6-month validity).
"""

from __future__ import annotations

import datetime as dt
import re

from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers._days import eibi_days_to_mask
from msdr.library.models import now_iso

_NUM_FIELDS = 11
_FILENAME_RE = re.compile(r"sked-([ab]\d{2})", re.IGNORECASE)


def _parse_time(raw: str) -> tuple[str | None, str | None]:
    raw = raw.strip()
    if not raw or raw.lower() == "24h" or "-" not in raw:
        return None, None
    start, stop = raw.split("-", 1)
    start, stop = start.strip(), stop.strip()
    if len(start) != 4 or len(stop) != 4 or not (start.isdigit() and stop.isdigit()):
        return None, None
    return start, stop


def season_from_filename(filename: str) -> str | None:
    """'sked-b25.csv' -> 'B25'."""
    m = _FILENAME_RE.search(filename.lower())
    return m.group(1).upper() if m else None


def parse_eibi(text: str, *, filename: str | None = None) -> list[CatalogEntry]:
    season = season_from_filename(filename) if filename else None
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in text.splitlines():
        line = line.strip("\n")
        if not line.strip() or line.lower().startswith("khz"):
            continue  # blank line or header
        fields = line.split(";")
        if len(fields) < _NUM_FIELDS:
            fields += [""] * (_NUM_FIELDS - len(fields))
        khz, time_raw, days_raw, itu, station, lang, target, remarks = fields[:8]
        # fields[8:11] are P, Start, Stop -- not modelled, see module docstring.
        try:
            frequency_hz = round(float(khz.strip()) * 1000)
        except ValueError:
            continue  # not a data row (stray blank/comment line)
        start_utc, stop_utc = _parse_time(time_raw)
        entries.append(
            CatalogEntry(
                source="eibi",
                frequency_hz=frequency_hz,
                station=station.strip() or "Unknown",
                mode="AM",
                bandwidth_hz=9000,
                start_utc=start_utc,
                stop_utc=stop_utc,
                days=eibi_days_to_mask(days_raw),
                language=lang.strip() or None,
                target=target.strip() or None,
                country=itu.strip() or None,
                notes=remarks.strip() or None,
                season=season,
                imported_at=imported_at,
            )
        )
    return entries


def season_code(date: dt.date) -> str:
    """EiBi season for `date`: 'A' from the last Sunday of March, 'B' from
    the last Sunday of October, e.g. A26 / B26 (two-digit year)."""
    if isinstance(date, dt.datetime):
        date = date.date()
    boundary_a = _last_sunday(date.year, 3)
    boundary_b = _last_sunday(date.year, 10)
    if date >= boundary_b:
        return f"B{date.year % 100:02d}"
    if date >= boundary_a:
        return f"A{date.year % 100:02d}"
    return f"B{(date.year - 1) % 100:02d}"  # still in last October's B season


def _last_sunday(year: int, month: int) -> dt.date:
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = next_month - dt.timedelta(days=1)
    offset = (last_day.weekday() - 6) % 7  # date.weekday(): Mon=0 .. Sun=6
    return last_day - dt.timedelta(days=offset)


def default_url(season: str) -> str:
    return f"http://www.eibispace.de/dx/sked-{season.lower()}.csv"
