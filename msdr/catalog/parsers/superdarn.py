"""SuperDARN radar sites from a bundled snapshot of the public hardware
tables (`hdw.dat.<code>` in https://github.com/SuperDARN/hdw) joined with the
names/institutions table on https://superdarn.ca/radar-info. See
docs/reference-catalogs.md.

Only facts are stored: identifiers, coordinates, boresight, beam/gate counts
and the date the current hardware configuration took effect. No primary
source publishes an operating frequency, so `frequency_hz`, `freq_lo_hz` and
`freq_hi_hz` stay null and the secondary-source 8-22 MHz band is only
mentioned in `notes` as unverified. The hdw repo carries no licence and the
site is "all rights reserved"; radar *data* needs a PI agreement, which the
notes say too.

`parse_superdarn()` reads the newest bundled `superdarn_<date>.json`;
`parse_hdw_dat()`, `parse_radar_info()` and `build_snapshot()` are the
pieces that produced it (and that a refresh would reuse)."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

SOURCE = "superdarn"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HDW_RAW_BASE = "https://raw.githubusercontent.com/SuperDARN/hdw/main/"
RADAR_INFO_URL = "https://superdarn.ca/radar-info"

LICENCE_CAVEAT = (
    "Hardware tables from github.com/SuperDARN/hdw (no licence stated) and names from "
    "superdarn.ca/radar-info (all rights reserved); only identifiers and coordinates are "
    "stored. Data use is governed by a PI agreement."
)
_NOTES = (
    "No operating frequency is published in the hdw tables or radar-info; secondary "
    "sources cite 8–22 MHz (unverified). Data use is governed by a PI agreement."
)

_ROW_RE = re.compile(
    r"<tr[^>]*id=['\"](?P<code>[a-z]{3})-table-row['\"][^>]*>(?P<body>.*?)</tr>", re.S
)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_FLAG_RE = re.compile(r"flags/\d+-(?P<slug>[a-z0-9-]+?)_[a-z0-9]+\.svg")


def parse_hdw_dat(text: str) -> dict[str, Any] | None:
    """The current hardware configuration from one `hdw.dat.<code>` file:
    the data line with the latest valid-from date. Columns (from the file
    header): station id, status, YYYYMMDD, HH:MM:SS, lat, lon, alt,
    boresight, ... , max range gates, max beams. Returns None when the file
    has no data lines."""
    latest: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 10:
            continue
        key = f"{fields[2]} {fields[3]}"
        if latest is None or key > latest[0]:
            latest = (key, fields)
    if latest is None:
        return None
    _, f = latest
    date, time = f[2], f[3]
    return {
        "station_id": int(f[0]),
        "lat": float(f[4]),
        "lon": float(f[5]),
        "boresight": float(f[7]),
        "gates": int(float(f[-2])),
        "beams": int(float(f[-1])),
        "config_valid_from": f"{date[:4]}-{date[4:6]}-{date[6:8]} {time}",
    }


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def _country(cell: str) -> str | None:
    m = _FLAG_RE.search(cell)
    if not m:
        return None
    return m.group("slug").replace("-", " ").title()


def parse_radar_info(page: str) -> dict[str, dict[str, Any]]:
    """code -> {name, pi, institution, country} from the radar-info summary
    rows (one `<tr id="<code>-table-row">` per radar: chevron, code, old
    code, station id, name + flag, PI, institution + flag). The country is
    the flag icon next to the name; the page prints no country text."""
    radars: dict[str, dict[str, Any]] = {}
    for m in _ROW_RE.finditer(page):
        body = m.group("body").split("<table")[0]
        cells = _CELL_RE.findall(body)
        if len(cells) < 7:
            continue
        name_cell = cells[4]
        radars[m.group("code")] = {
            "name": _clean(name_cell),
            "pi": _clean(cells[5]) or None,
            "institution": _clean(cells[6]) or None,
            "country": _country(name_cell),
        }
    return radars


def build_snapshot(
    hdw_texts: dict[str, str],
    radar_info_page: str | None,
    *,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Join `hdw.dat` texts (keyed by radar code) with the radar-info table
    into the bundled snapshot document."""
    names = parse_radar_info(radar_info_page) if radar_info_page else {}
    radars = []
    for code in sorted(hdw_texts):
        hw = parse_hdw_dat(hdw_texts[code])
        if hw is None:
            continue
        info = names.get(code, {})
        radars.append(
            {
                "code": code,
                "station_id": hw["station_id"],
                "name": info.get("name"),
                "institution": info.get("institution"),
                "pi": info.get("pi"),
                "country": info.get("country"),
                "lat": hw["lat"],
                "lon": hw["lon"],
                "boresight": hw["boresight"],
                "beams": hw["beams"],
                "gates": hw["gates"],
                "config_valid_from": hw["config_valid_from"],
                "source_urls": [HDW_RAW_BASE + f"hdw.dat.{code}", RADAR_INFO_URL],
            }
        )
    return {
        "retrieved_at": retrieved_at or now_iso(),
        "sources": {"hdw": HDW_RAW_BASE, "radar_info": RADAR_INFO_URL},
        "licence": LICENCE_CAVEAT,
        "radars": radars,
    }


def bundled_snapshot_path() -> Path:
    paths = sorted(_DATA_DIR.glob("superdarn_*.json"))
    if not paths:
        raise FileNotFoundError(f"no superdarn_<date>.json under {_DATA_DIR}")
    return paths[-1]


def entries_from_snapshot(data: dict[str, Any]) -> list[CatalogEntry]:
    edition = data["retrieved_at"][:10]
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for r in data["radars"]:
        name = r.get("name") or r["code"].upper()
        details = (
            f"SuperDARN radar {r['code']} (station id {r['station_id']}); boresight "
            f"{r['boresight']:g} deg, {r['beams']} beams x {r['gates']} gates, hardware "
            f"configuration valid from {r['config_valid_from']}"
        )
        if r.get("pi"):
            details += f"; PI {r['pi']}"
        entries.append(
            CatalogEntry(
                source=SOURCE,
                ref_type="radar_site",
                network="SuperDARN",
                station_id=r["code"],
                component=None,
                station=f"SuperDARN {name}",
                frequency_hz=None,
                mode=None,
                operator=r.get("institution"),
                country=r.get("country"),
                location=name,
                latitude=r["lat"],
                longitude=r["lon"],
                status="unknown",
                status_at=edition,
                source_url=r["source_urls"][0],
                edition=edition,
                decoder=None,
                notes=f"{_NOTES} {details}.",
                imported_at=imported_at,
            )
        )
    return entries


def parse_superdarn(path: Path | None = None) -> list[CatalogEntry]:
    """No network fetch -- reads the newest bundled snapshot."""
    data = json.loads((path or bundled_snapshot_path()).read_text(encoding="utf-8"))
    return entries_from_snapshot(data)
