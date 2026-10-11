"""Import a user's own frequency lists: SDR++ frequency manager JSON, and
GQRX's bookmark CSV. Explicit coordinates may be supplied as ``latitude`` and
``longitude`` fields in SDR++ bookmarks, or as two extra trailing GQRX CSV
columns after the existing tags column. Source is tagged "user:<filename>"
so multiple imported lists coexist and can each be replaced independently."""

from __future__ import annotations

import json

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

# SDR++ bookmark "mode" is an index into this fixed list.
_SDRPP_MODES = ["NFM", "WFM", "AM", "DSB", "USB", "CW", "LSB", "RAW"]


def _coordinate_pair(latitude, longitude) -> tuple[float | None, float | None]:
    """Accept only a complete, in-range pair of explicit decimal values."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def parse_sdrpp_json(text: str, *, filename: str = "import") -> list[CatalogEntry]:
    data = json.loads(text)
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for name, bm in data.get("bookmarks", {}).items():
        mode_idx = bm.get("mode")
        mode = (
            _SDRPP_MODES[mode_idx]
            if isinstance(mode_idx, int) and 0 <= mode_idx < len(_SDRPP_MODES)
            else None
        )
        latitude, longitude = _coordinate_pair(bm.get("latitude"), bm.get("longitude"))
        entries.append(
            CatalogEntry(
                source=f"user:{filename}",
                frequency_hz=int(bm["frequency"]),
                station=name,
                mode=mode,
                bandwidth_hz=int(bm["bandwidth"]) if bm.get("bandwidth") is not None else None,
                latitude=latitude,
                longitude=longitude,
                imported_at=imported_at,
            )
        )
    return entries


def parse_gqrx_csv(text: str, *, filename: str = "import") -> list[CatalogEntry]:
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue  # GQRX header line starts with '#'
        fields = [f.strip() for f in raw.split(";")]
        if len(fields) < 5:
            fields += [""] * (5 - len(fields))
        freq, name, mode, bandwidth, tags = fields[:5]
        latitude, longitude = _coordinate_pair(
            fields[5] if len(fields) > 5 else None,
            fields[6] if len(fields) > 6 else None,
        )
        try:
            frequency_hz = int(float(freq))
        except ValueError:
            continue
        entries.append(
            CatalogEntry(
                source=f"user:{filename}",
                frequency_hz=frequency_hz,
                station=name or "Unknown",
                mode=mode or None,
                bandwidth_hz=int(float(bandwidth)) if bandwidth else None,
                notes=tags or None,
                latitude=latitude,
                longitude=longitude,
                imported_at=imported_at,
            )
        )
    return entries
