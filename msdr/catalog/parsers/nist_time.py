"""NIST time-signal stations WWV, WWVH and WWVB from a bundled, hand-reviewed
snapshot (`msdr/catalog/data/nist_time_stations.json`). NIST publishes these
facts as HTML prose and tables with no structured feed, so the snapshot is
built by hand from the station pages and carries its `snapshot_date` plus a
per-station `source_url`; see docs/reference-catalogs.md.

One entry per station per frequency (`ref_type` "station", continuous
service, no schedule window): WWV 2.5/5/10/15/20 MHz plus the experimental
25 MHz, WWVH 2.5/5/10/15 MHz only, and WWVB's two 60 kHz antenna arrays."""

from __future__ import annotations

import json
from pathlib import Path

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

SOURCE = "nist_time"
_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "nist_time_stations.json"


def snapshot_date(path: Path | None = None) -> str:
    return json.loads((path or _DATA_PATH).read_text(encoding="utf-8"))["snapshot_date"]


def parse_nist_time(path: Path | None = None) -> list[CatalogEntry]:
    """No network fetch -- the bundled snapshot is the whole source."""
    data = json.loads((path or _DATA_PATH).read_text(encoding="utf-8"))
    edition = data["snapshot_date"]
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for station in data["stations"]:
        for comp in station["components"]:
            entries.append(
                CatalogEntry(
                    source=SOURCE,
                    ref_type="station",
                    network=data.get("network", "NIST"),
                    station_id=station["station_id"],
                    component=comp["component"],
                    station=f"{station['name']} {comp['component']}",
                    frequency_hz=comp["frequency_hz"],
                    mode=comp.get("mode"),
                    bandwidth_hz=comp.get("bandwidth_hz"),
                    power_kw=comp.get("power_kw"),
                    operator=data.get("operator", "NIST"),
                    country=data.get("country", "USA"),
                    location=station.get("location"),
                    latitude=comp.get("latitude"),
                    longitude=comp.get("longitude"),
                    status=comp.get("status", "operational"),
                    status_at=edition,
                    source_url=station["source_url"],
                    edition=edition,
                    decoder=station.get("decoder"),
                    notes=comp.get("notes"),
                    imported_at=imported_at,
                )
            )
    return entries
