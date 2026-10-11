"""HFCC (High Frequency Coordination Conference) public schedule, published
per season as fixed-width text (e.g. A26all00.TXT / bNNall00.txt). The file
carries its own column ruler (a ';----+----+...' line) directly above the
header row, so columns are located by name rather than hard-coded offsets --
robust to the minor width drift seen between seasons/mirrors.

Reference tables (bundled under msdr/catalog/data/hfcc/, loaded lazily):
- broadcas.txt: BRC code -> broadcaster name
- site.txt: LOC code -> transmitter site name
- admin.txt: ADM code -> administration (country) English name
- languages.json: a compact code -> name map extracted from the (800 KB)
  LANGUAGE.TXT reference table -- the raw table isn't shipped.

Unmapped codes fall back to the raw code (never dropped).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from functools import lru_cache
from pathlib import Path

from msdr.catalog.models import CatalogEntry
from msdr.catalog.parsers._days import digits_to_mask
from msdr.library.models import now_iso

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "hfcc"

_RULER_RE = re.compile(r"^;[-+]+\s*$")


def _plus_ruler_segments(ruler: str) -> list[tuple[int, int | None]]:
    """Column boundaries from a '+'-delimited ruler line, e.g.
    ';----+----+----+...'. The last column is left open-ended since trailing
    fields (NOTES, and some right-justified numeric fields) can overflow
    their nominal ruler width."""
    segs: list[tuple[int, int | None]] = []
    start = 0
    for i, ch in enumerate(ruler):
        if ch == "+":
            segs.append((start, i))
            start = i + 1
    segs.append((start, None))
    return segs


def _slice(line: str, seg: tuple[int, int | None]) -> str:
    s, e = seg
    return (line[s:] if e is None else line[s:e]).strip()


def _find_columns(lines: list[str]) -> tuple[dict[str, tuple[int, int | None]], int]:
    """Locate the ruler+header pair and return (name -> column segment,
    index of the first data line)."""
    for i, line in enumerate(lines[:-1]):
        if _RULER_RE.match(line) and i + 1 < len(lines):
            header = lines[i + 1]
            segs = _plus_ruler_segments(line.rstrip())
            columns = {}
            for seg in segs:
                name = _slice(header, seg).lstrip(";").strip()
                if name:
                    columns[name] = seg
            # data starts after the (repeated) ruler line below the header
            data_start = i + 2
            if data_start < len(lines) and _RULER_RE.match(lines[data_start]):
                data_start += 1
            return columns, data_start
    return {}, 0


@lru_cache(maxsize=1)
def _load_admin() -> dict[str, str]:
    return _load_code_name_table(_DATA_DIR / "admin.txt", name_end=55)


@lru_cache(maxsize=1)
def _load_broadcasters() -> dict[str, str]:
    return _load_code_name_table(_DATA_DIR / "broadcas.txt", name_end=None)


@lru_cache(maxsize=1)
def _load_sites() -> dict[str, str]:
    return _load_code_name_table(_DATA_DIR / "site.txt", name_end=34)


@lru_cache(maxsize=1)
def _load_languages() -> dict[str, str]:
    path = _DATA_DIR / "languages.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_code_name_table(path: Path, *, name_end: int | None) -> dict[str, str]:
    """These reference tables share a simple layout: a 3-letter code in the
    first 3 columns, then the name from column 4 onward (optionally cut off
    before a second/third language column, per `name_end`)."""
    table: dict[str, str] = {}
    if not path.exists():
        return table
    for raw in path.read_text(encoding="latin-1").splitlines():
        if not raw or raw.startswith(";"):
            continue
        code = raw[0:3].strip()
        if not code:
            continue
        name = raw[4:name_end].strip() if name_end else raw[4:].strip()
        if name:
            table[code] = name
    return table


def _parse_hfcc_date(raw: str) -> dt.date | None:
    """FDATE/TDATE are DDMMYY."""
    raw = raw.strip()
    if len(raw) != 6 or not raw.isdigit():
        return None
    day, month, yy = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _explicit_coordinate(col, line: str, *names: str) -> float | None:
    """Read a decimal coordinate only when the source has an explicit field."""
    raw = next((col(name, line) for name in names if col(name, line)), "")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def parse_hfcc(text: str, *, today: dt.date | None = None) -> list[CatalogEntry]:
    lines = text.splitlines()
    columns, data_start = _find_columns(lines)
    if not columns:
        return []

    admin = _load_admin()
    broadcasters = _load_broadcasters()
    sites = _load_sites()
    languages = _load_languages()

    def col(name: str, line: str) -> str:
        seg = columns.get(name)
        return _slice(line, seg) if seg else ""

    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in lines[data_start:]:
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        freq_raw = col("FREQ", line)
        if not freq_raw or not freq_raw.isdigit():
            continue  # not a data row

        if today is not None:
            fdate = _parse_hfcc_date(col("FDATE", line))
            tdate = _parse_hfcc_date(col("TDATE", line))
            if fdate and tdate and not (fdate <= today <= tdate):
                continue

        days = digits_to_mask(col("DAYS", line), first_day="monday")

        loc_code = col("LOC", line)
        location = sites.get(loc_code, loc_code) or None

        power_raw = col("POWR", line)
        power_kw = float(power_raw) if power_raw else None

        azimuth = col("AZIMUTH", line)
        notes = f"az {azimuth}" if azimuth else None

        adm_code = col("ADM", line)
        country = admin.get(adm_code, adm_code) or None

        brc_code = col("BRC", line)
        station = broadcasters.get(brc_code, brc_code) or brc_code or "Unknown"

        lang_code = col("LANGUAGE", line)
        language = languages.get(lang_code, lang_code) or None

        target = col("CIRAF ZONES", line) or None
        latitude = _explicit_coordinate(col, line, "LATITUDE", "LAT")
        longitude = _explicit_coordinate(col, line, "LONGITUDE", "LON", "LONG")
        if latitude is not None and not -90 <= latitude <= 90:
            latitude = None
        if longitude is not None and not -180 <= longitude <= 180:
            longitude = None
        if latitude is None or longitude is None:
            latitude = longitude = None

        entries.append(
            CatalogEntry(
                source="hfcc",
                frequency_hz=int(freq_raw) * 1000,
                station=station,
                mode="AM",
                bandwidth_hz=9000,
                start_utc=col("STRT", line) or None,
                stop_utc=col("STOP", line) or None,
                days=days,
                language=language,
                target=target,
                country=country,
                location=location,
                latitude=latitude,
                longitude=longitude,
                power_kw=power_kw,
                notes=notes,
                imported_at=imported_at,
            )
        )
    return entries
