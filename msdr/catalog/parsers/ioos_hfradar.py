"""IOOS / HFRNet coastal HF radar site list -- see docs/reference-catalogs.md
for the contract.

Primary source: `stationList.php?output=CSV` (header `Station,"Station
Name",Network,Frequency,Bandwidth,SweepRate,"Date Added","Last Radial
File",Latitude,Longitude,Manufacturer`; `Frequency` is MHz, confirmed from
the live feed's magnitudes (4.45-43.7 MHz, matching known HF radar
allocations) and the doc's worked examples (ACI1 13.475 MHz, AGL1 4.4505
MHz). `Bandwidth` is *not* a usable span: live samples include negative
values and a `-9999` not-available sentinel on ~90% of rows and a max
around 600 on the rest -- far outside any plausible kHz occupied-bandwidth
for this service -- so `freq_lo_hz`/`freq_hi_hz` are always left null here
rather than guessed from it.

Optional secondary source: ERDDAP `allDatasets.csv`, whose `datasetID`
follows `<Network>_<Station>` (e.g. `UCSB_ACI1`); when it's reachable and a
row's `institution` adds information beyond the station list's own
`Network` column, that's folded into `notes`. ERDDAP being unreachable (or
malformed) must not fail the import -- see `_apply_erddap`.

`CatalogEntry` doesn't yet carry the new reference-catalog fields -- as in
`faa_nasr.py`, the real parsing logic lives in the pure `parse_station_rows()`
function, which returns plain dicts; `to_entries()` is a thin pass-through
that becomes real `CatalogEntry` objects once the model lands."""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

_FETCH_TIMEOUT_S = 30
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB cap

_STALE_AFTER_DAYS = 30

_SNAPSHOT_DATE = "2026-10-02"
_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / f"ioos_hfradar_{_SNAPSHOT_DATE}.json"
)

default_url = "https://hfradar.ioos.us/hfrnet/sitediag/stationList.php?output=CSV"
erddap_url = "https://hfradar.ioos.us/erddap/tabledap/allDatasets.csv"


def _parse_float(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _component_from_manufacturer(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    upper = text.upper()
    if "CODAR" in upper:
        return "CODAR SeaSonde"
    if "WERA" in upper:
        return "WERA"
    return text  # unrecognized manufacturer string: keep as published


def _status_from_last_radial(last_radial: str, now: date) -> tuple[str, str | None]:
    last_radial = last_radial.strip()
    if not last_radial:
        return "unknown", None
    try:
        when = date.fromisoformat(last_radial)
    except ValueError:
        return "unknown", last_radial  # malformed date: keep the raw text, status unknown
    status = "data_recent" if abs((now - when).days) <= _STALE_AFTER_DAYS else "unknown"
    return status, last_radial


def parse_station_rows(
    text: str, *, now: date | None = None, source_url: str = "", edition: str = ""
) -> list[dict]:
    """Pure parse layer for the station list CSV. `now` is the retrieval
    date used to judge "Last Radial File" staleness (fixed in tests)."""
    if now is None:
        now = date.today()
    imported_at = now_iso()
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for raw in reader:
        station = (raw.get("Station") or "").strip()
        if not station:
            continue  # no stable identifier to key the entry on
        network = (raw.get("Network") or "").strip() or None
        name = (raw.get("Station Name") or "").strip() or None

        freq_raw = (raw.get("Frequency") or "").strip()
        mhz = _parse_float(freq_raw)
        frequency_hz = round(mhz * 1_000_000) if mhz is not None else None

        lat = _parse_float(raw.get("Latitude"))
        lon = _parse_float(raw.get("Longitude"))
        if (lat is None) != (lon is None):
            lat = lon = None

        last_radial = (raw.get("Last Radial File") or "").strip()
        status, status_at = _status_from_last_radial(last_radial, now)

        note_parts = ["Last Radial File is a data-availability date, not a transmission indicator"]
        if freq_raw and mhz is None:
            note_parts.append(f"unparseable Frequency {freq_raw!r}")

        rows.append(
            {
                "source": "ioos_hfradar",
                "ref_type": "radar_site",
                "network": "IOOS HFRNet",
                "station_id": station,
                "component": _component_from_manufacturer(raw.get("Manufacturer")),
                "operator": network,
                "frequency_hz": frequency_hz,
                "freq_lo_hz": None,
                "freq_hi_hz": None,
                "current_freq_hz": None,
                "current_freq_at": None,
                "status": status,
                "status_at": status_at,
                "source_url": source_url,
                "edition": edition,
                "decoder": None,
                "station": f"{station} {name}" if name else station,
                "mode": None,
                "bandwidth_hz": None,
                "country": "USA",
                "location": name,
                "latitude": lat,
                "longitude": lon,
                "notes": "; ".join(note_parts),
                "imported_at": imported_at,
            }
        )
    return rows


def _erddap_institutions(erddap_text: str) -> dict[str, str]:
    """`datasetID` -> `institution`, for enrichment keyed on
    `<Network>_<Station>` (e.g. `UCSB_ACI1`)."""
    out: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(erddap_text))
    for row in reader:
        dataset_id = (row.get("datasetID") or "").strip()
        institution = (row.get("institution") or "").strip()
        if dataset_id and institution:
            out[dataset_id] = institution
    return out


def _apply_erddap(rows: list[dict], erddap_text: str | None) -> None:
    """Attach ERDDAP institution names in place. Any failure parsing ERDDAP
    (unreachable, malformed, unexpected columns) is swallowed -- per the
    contract, ERDDAP is a nice-to-have enrichment, never a reason to fail
    the whole import."""
    if not erddap_text:
        return
    try:
        institutions = _erddap_institutions(erddap_text)
    except Exception:
        return
    for row in rows:
        operator = row.get("operator")
        if not operator:
            continue
        institution = institutions.get(f"{operator}_{row['station_id']}")
        if institution and institution.strip().upper() != operator.strip().upper():
            extra = f"ERDDAP institution: {institution}"
            row["notes"] = f"{row['notes']}; {extra}" if row.get("notes") else extra


def parse_ioos_hfradar_rows(
    station_text: str,
    erddap_text: str | None = None,
    *,
    now: date | None = None,
    source_url: str = "",
    edition: str = "",
) -> list[dict]:
    rows = parse_station_rows(station_text, now=now, source_url=source_url, edition=edition)
    _apply_erddap(rows, erddap_text)
    return rows


def to_entries(rows: Iterable[dict]) -> list[CatalogEntry]:
    """Thin pass-through -- see faa_nasr.to_entries() for why this needs no
    changes once ref_type/network/... land on CatalogEntry."""
    return [CatalogEntry(**row) for row in rows]


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


def fetch_ioos_hfradar() -> list[CatalogEntry]:
    """Network path: station list CSV (required) plus ERDDAP allDatasets.csv
    (best-effort, never fatal)."""
    station_text = _download(default_url).decode("utf-8", errors="replace")
    try:
        erddap_text: str | None = _download(erddap_url).decode("utf-8", errors="replace")
    except Exception:
        erddap_text = None
    today = date.today()
    rows = parse_ioos_hfradar_rows(
        station_text,
        erddap_text,
        now=today,
        source_url=default_url,
        edition=today.isoformat(),
    )
    return to_entries(rows)


def parse_ioos_hfradar() -> list[CatalogEntry]:
    """No network fetch -- loads the bundled dated snapshot
    (msdr/catalog/data/ioos_hfradar_<date>.json), matching the other static
    parsers' no-arg signature."""
    with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
        rows = json.load(f)
    return to_entries(rows)
