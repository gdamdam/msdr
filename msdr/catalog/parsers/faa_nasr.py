"""FAA NASR (National Airspace System Resources) navaid and ILS localizer
reference data -- see docs/reference-catalogs.md for the contract.

Two CSV files per 28-day NASR cycle, downloaded as separate ZIPs:

- `NAV_BASE.csv`: one row per navaid (VOR, VOR/DME, VORTAC, VOT, NDB,
  NDB/DME, MARINE NDB, DME-only, TACAN-only, FAN MARKER). `FREQ` is MHz
  for the VOR family and kHz for NDB family; DME-only/TACAN-only rows
  carry a channel (`CHAN`), not a usable VHF frequency, and FAN MARKER
  carries neither -- none of those three get a derived `frequency_hz`.
- `ILS_BASE.csv`: one row per runway-end localizer. Glideslope/DME/marker
  components for the same approach live in ILS_GS.csv/ILS_DME.csv/
  ILS_MKR.csv, out of scope here (see docs/reference-catalogs.md).

`CatalogEntry` doesn't yet carry the new reference-catalog fields
(`ref_type`, `network`, `station_id`, `component`, ...) -- a sibling change
adds them. Until then, the real parsing logic lives in the pure
`parse_nav_rows()` / `parse_ils_rows()` functions below, which return
plain dicts with every field the eventual model will have; `to_entries()`
is a thin pass-through that becomes real `CatalogEntry` objects once the
model lands (pydantic already ignores dict keys a model doesn't declare,
so no changes will be needed here when it does)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import urllib.request
import zipfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

_EPOCH = date(2026, 10, 1)
_PERIOD_DAYS = 28

_FETCH_TIMEOUT_S = 30
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB cap per zip

_SNAPSHOT_CYCLE = "2026-10-01"
_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / f"faa_nasr_{_SNAPSHOT_CYCLE}.json.gz"
)

# NAV_TYPE values whose FREQ is a VHF frequency in MHz. Only the first three
# get the "vor" decoder (VOT is a VOR *test* facility, not itself tunable as
# a navigation aid); all four are AM-modulated idents.
_VOR_DECODER_TYPES = {"VOR", "VOR/DME", "VORTAC"}
_VOR_FAMILY_MHZ = _VOR_DECODER_TYPES | {"VOT"}

# NAV_TYPE values whose FREQ is in kHz (no msdr decoder for NDB).
_NDB_TYPES = {"NDB", "NDB/DME", "MARINE NDB"}


def current_cycle_date(today: date) -> date:
    """The NASR 28-day cycle `today` falls in, anchored at the 2026-10-01
    epoch cycle (next cycle 2026-10-29, per docs/reference-catalogs.md)."""
    offset = (today - _EPOCH).days
    cycle_index = offset // _PERIOD_DAYS
    return _EPOCH + timedelta(days=cycle_index * _PERIOD_DAYS)


def _cycle_token(cycle: date) -> str:
    return f"{cycle.day:02d}_{cycle.strftime('%b')}_{cycle.year}"


def nav_csv_url(cycle: date) -> str:
    """NAV_BASE.csv ZIP for `cycle`. Note: the real download lives under
    .../28DaySub/extra/..., not .../28DaySub/... directly (confirmed against
    the live FAA subscription index for the 2026-10-01 cycle)."""
    return f"https://nfdc.faa.gov/webContent/28DaySub/extra/{_cycle_token(cycle)}_NAV_CSV.zip"


def ils_csv_url(cycle: date) -> str:
    return f"https://nfdc.faa.gov/webContent/28DaySub/extra/{_cycle_token(cycle)}_ILS_CSV.zip"


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
        chunks = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError(f"{url}: download exceeds {_MAX_DOWNLOAD_BYTES} byte cap")
            chunks.append(chunk)
        return b"".join(chunks)


def _read_zip_member(data: bytes, filename: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(filename).decode("utf-8", errors="replace")


def _parse_float(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _operator(raw: str | None) -> str:
    """OWNER is an ownership-type code + name, e.g. "F-FEDERAL AVIATION
    ADMIN" or "A-US AIR FORCE". Strip the code prefix and normalize the
    several FAA spellings seen in NAV_BASE/ILS_BASE to "FAA"; anything else
    (military, state, foreign) is kept as published rather than guessed."""
    text = (raw or "").strip()
    if not text:
        return "FAA"
    if len(text) > 1 and text[1] == "-":
        text = text[2:].strip()
    if text.upper().startswith("FEDERAL AVIATION ADMIN"):
        return "FAA"
    return text or "FAA"


def _status_from_raw(raw: str | None) -> tuple[str, str | None]:
    """(status, raw text). The real 2026-10-01 cycle has no bare
    "OPERATIONAL" value -- NAV_STATUS/COMPONENT_STATUS read "OPERATIONAL
    IFR", "OPERATIONAL VFR ONLY" or "OPERATIONAL RESTRICTED" -- so any
    "OPERATIONAL..." not also containing "RESTRICTED" maps to operational;
    the raw wording is always returned for notes (source wording kept in
    notes, per docs/reference-catalogs.md's Data model table)."""
    text = (raw or "").strip()
    if not text:
        return "unknown", None
    upper = text.upper()
    if "SHUTDOWN" in upper or "DECOMMISSION" in upper:
        return "shutdown", text
    if "RESTRICTED" in upper:
        return "operational_restricted", text
    if upper.startswith("OPERATIONAL"):
        return "operational", text
    return "unknown", text


def _base_row(
    *,
    station_id: str,
    station: str,
    component: str,
    operator: str,
    frequency_hz: int | None,
    status: str,
    status_at: str | None,
    source_url: str,
    edition: str,
    decoder: str | None,
    mode: str | None,
    location: str | None,
    latitude: float | None,
    longitude: float | None,
    notes: str | None,
    imported_at: str,
) -> dict:
    return {
        "source": "faa_nasr",
        "ref_type": "navaid",
        "network": "FAA NASR",
        "station_id": station_id,
        "component": component,
        "operator": operator,
        "frequency_hz": frequency_hz,
        "freq_lo_hz": None,
        "freq_hi_hz": None,
        "current_freq_hz": None,
        "current_freq_at": None,
        "status": status,
        "status_at": status_at,
        "source_url": source_url,
        "edition": edition,
        "decoder": decoder,
        "station": station,
        "mode": mode,
        "bandwidth_hz": None,
        "country": "USA",
        "location": location,
        "latitude": latitude,
        "longitude": longitude,
        "notes": notes,
        "imported_at": imported_at,
    }


def parse_nav_rows(text: str, *, source_url: str = "", edition: str = "") -> tuple[list[dict], int]:
    """Parse NAV_BASE.csv rows. Returns (rows, skipped_count) -- a row is
    only skipped (and counted) when it has no NAV_ID at all, since that's
    the stable identifier the catalog keys entries on; every other row is
    kept even when a field is malformed (null + a note), per the "no silent
    drops" rule in docs/reference-catalogs.md."""
    imported_at = now_iso()
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    skipped = 0
    for raw in reader:
        nav_id = (raw.get("NAV_ID") or "").strip()
        if not nav_id:
            skipped += 1
            continue
        nav_type = (raw.get("NAV_TYPE") or "").strip()
        city = (raw.get("CITY") or "").strip()
        state = (raw.get("STATE_CODE") or "").strip()
        location = ", ".join(p for p in (city, state) if p) or None

        lat = _parse_float(raw.get("LAT_DECIMAL"))
        lon = _parse_float(raw.get("LONG_DECIMAL"))
        if (lat is None) != (lon is None):
            lat = lon = None

        freq_raw = (raw.get("FREQ") or "").strip()
        chan = (raw.get("CHAN") or "").strip()
        frequency_hz: int | None = None
        mode: str | None = None
        decoder: str | None = None
        component = nav_type
        note_parts: list[str] = []

        if nav_type == "DME":
            component = f"DME channel {chan}" if chan else "DME"
        elif nav_type == "TACAN":
            component = f"TACAN channel {chan}" if chan else "TACAN"
        elif nav_type in _VOR_FAMILY_MHZ:
            mhz = _parse_float(freq_raw)
            if mhz is None:
                if freq_raw:
                    note_parts.append(f"unparseable FREQ {freq_raw!r}")
            else:
                frequency_hz = round(mhz * 1_000_000)
            mode = "AM"
            if nav_type in _VOR_DECODER_TYPES and frequency_hz is not None:
                decoder = "vor"
        elif nav_type in _NDB_TYPES:
            khz = _parse_float(freq_raw)
            if khz is None:
                if freq_raw:
                    note_parts.append(f"unparseable FREQ {freq_raw!r}")
            else:
                frequency_hz = round(khz * 1000)
            mode = "AM"
        else:
            # FAN MARKER and any other/future NAV_TYPE: no documented
            # frequency convention, so leave it null rather than guess.
            if freq_raw:
                note_parts.append(f"unmapped NAV_TYPE {nav_type!r} FREQ {freq_raw!r}")

        status, status_text = _status_from_raw(raw.get("NAV_STATUS"))
        if status_text:
            note_parts.append(f"status: {status_text}")

        rows.append(
            _base_row(
                station_id=nav_id,
                station=f"{nav_id} {nav_type}".strip(),
                component=component,
                operator=_operator(raw.get("OWNER")),
                frequency_hz=frequency_hz,
                status=status,
                status_at=(raw.get("EFF_DATE") or "").strip() or None,
                source_url=source_url,
                edition=edition,
                decoder=decoder,
                mode=mode,
                location=location,
                latitude=lat,
                longitude=lon,
                notes="; ".join(note_parts) or None,
                imported_at=imported_at,
            )
        )
    return rows, skipped


def parse_ils_rows(text: str, *, source_url: str = "", edition: str = "") -> tuple[list[dict], int]:
    """Parse ILS_BASE.csv rows (localizers only -- see module docstring).
    Returns (rows, skipped_count); a row is skipped only when it has no
    ILS_LOC_ID."""
    imported_at = now_iso()
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    skipped = 0
    for raw in reader:
        loc_id = (raw.get("ILS_LOC_ID") or "").strip()
        if not loc_id:
            skipped += 1
            continue
        rwy = (raw.get("RWY_END_ID") or "").strip()
        arpt = (raw.get("ARPT_ID") or "").strip()
        city = (raw.get("CITY") or "").strip()
        state = (raw.get("STATE_CODE") or "").strip()
        location = ", ".join(p for p in (city, state) if p) or None

        lat = _parse_float(raw.get("LAT_DECIMAL"))
        lon = _parse_float(raw.get("LONG_DECIMAL"))
        if (lat is None) != (lon is None):
            lat = lon = None

        freq_raw = (raw.get("LOC_FREQ") or "").strip()
        note_parts: list[str] = []
        frequency_hz: int | None = None
        mhz = _parse_float(freq_raw)
        if mhz is None:
            if freq_raw:
                note_parts.append(f"unparseable LOC_FREQ {freq_raw!r}")
        else:
            frequency_hz = round(mhz * 1_000_000)

        status, status_text = _status_from_raw(raw.get("COMPONENT_STATUS"))
        if status_text:
            note_parts.append(f"status: {status_text}")

        rows.append(
            _base_row(
                station_id=loc_id,
                station=f"{loc_id} {arpt} RWY {rwy}".strip(),
                component=f"LOC RWY {rwy}" if rwy else "LOC",
                operator=_operator(raw.get("OWNER")),
                frequency_hz=frequency_hz,
                status=status,
                status_at=(raw.get("COMPONENT_STATUS_DATE") or "").strip() or None,
                source_url=source_url,
                edition=edition,
                decoder="ils",
                mode="AM",
                location=location,
                latitude=lat,
                longitude=lon,
                notes="; ".join(note_parts) or None,
                imported_at=imported_at,
            )
        )
    return rows, skipped


def parse_faa_nasr_rows(
    nav_text: str,
    ils_text: str,
    *,
    cycle: date,
    nav_url: str = "",
    ils_url: str = "",
) -> list[dict]:
    """Pure parse layer: NAV_BASE.csv + ILS_BASE.csv text -> plain dicts
    (see module docstring for why dicts rather than CatalogEntry)."""
    edition = cycle.isoformat()
    nav_rows, _ = parse_nav_rows(nav_text, source_url=nav_url, edition=edition)
    ils_rows, _ = parse_ils_rows(ils_text, source_url=ils_url, edition=edition)
    return nav_rows + ils_rows


def parse_report(nav_text: str, ils_text: str) -> dict:
    """Row counts for the SETUP UI / import log: totals, malformed rows
    skipped (missing identifier), and a NAV_TYPE breakdown."""
    nav_rows, nav_skipped = parse_nav_rows(nav_text)
    ils_rows, ils_skipped = parse_ils_rows(ils_text)
    by_component: dict[str, int] = {}
    for row in nav_rows:
        key = row["component"].split(" channel ")[0] if " channel " in row["component"] else (
            row["component"] or "unknown"
        )
        # Group by the underlying NAV_TYPE-ish bucket, not the per-channel text.
        by_component[key] = by_component.get(key, 0) + 1
    return {
        "nav_rows": len(nav_rows),
        "nav_skipped": nav_skipped,
        "ils_rows": len(ils_rows),
        "ils_skipped": ils_skipped,
        "nav_by_type": by_component,
    }


def to_entries(rows: Iterable[dict]) -> list[CatalogEntry]:
    """Thin pass-through: builds real CatalogEntry objects from
    parse_*_rows() dicts. pydantic ignores dict keys the current model
    doesn't declare, so this needs no changes once ref_type/network/...
    land on CatalogEntry -- it just starts carrying them."""
    return [CatalogEntry(**row) for row in rows]


def fetch_faa_nasr(cycle: date | None = None) -> list[CatalogEntry]:
    """Network path: download the current (or given) cycle's NAV_CSV and
    ILS_CSV zips and parse them. Bounded by `_FETCH_TIMEOUT_S` and the 20 MB
    per-file cap in `_download`."""
    if cycle is None:
        cycle = current_cycle_date(date.today())
    nav_url = nav_csv_url(cycle)
    ils_url = ils_csv_url(cycle)
    nav_text = _read_zip_member(_download(nav_url), "NAV_BASE.csv")
    ils_text = _read_zip_member(_download(ils_url), "ILS_BASE.csv")
    rows = parse_faa_nasr_rows(nav_text, ils_text, cycle=cycle, nav_url=nav_url, ils_url=ils_url)
    return to_entries(rows)


def parse_faa_nasr() -> list[CatalogEntry]:
    """No network fetch -- loads the bundled dated snapshot
    (msdr/catalog/data/faa_nasr_<cycle>.json.gz), matching the other static
    parsers' no-arg signature."""
    with gzip.open(_SNAPSHOT_PATH, "rt", encoding="utf-8") as f:
        rows = json.load(f)
    return to_entries(rows)
