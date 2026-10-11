"""NOAA / US Coast Guard HF radiofax (WEFAX) transmitter schedule. Static
data -- no fetcher, no fixture: NOAA publishes these assigned carrier
frequencies as a stable reference table (they change rarely and only by
NOTAM), so there is nothing to download or parse.

The frequencies below are the assigned (dial + 1.9 kHz) carrier frequencies;
the actual USB dial frequency to tune is 1.9 kHz *lower* than the assigned
carrier (standard convention for HF fax broadcasts). We store the assigned
frequency in `frequency_hz` and note the dial offset, rather than silently
shifting it, so the table matches what's printed on NOAA's own station
pages and NTIA frequency lists."""

from __future__ import annotations

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

# (callsign, city/state, list of assigned carrier frequencies in kHz)
_STATIONS: list[tuple[str, str, list[float]]] = [
    ("NMC", "Point Reyes, CA", [4346, 8682, 12786, 17151.2]),
    ("NMF", "Boston, MA", [4235, 6340.5, 9110, 12750]),
    ("NMG", "New Orleans, LA", [4317.9, 8503.9, 12789.9, 17146.4]),
    ("NOJ", "Kodiak, AK", [2054, 4298, 8459, 12412.5]),
    ("KVM70", "Honolulu, HI", [9982.5, 11090, 16135]),
]

_NOTES = "fax; dial -1.9 kHz for USB; radiofax 120 lpm IOC 576"


def parse_radiofax() -> list[CatalogEntry]:
    """No network fetch -- this is the whole (static) source."""
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for callsign, city, khz_list in _STATIONS:
        for khz in khz_list:
            entries.append(
                CatalogEntry(
                    source="noaa_radiofax",
                    frequency_hz=round(khz * 1000),
                    station=f"{callsign} {city}",
                    mode="USB",
                    bandwidth_hz=2400,
                    start_utc="0000",
                    stop_utc="2400",
                    days=None,  # daily
                    country="USA",
                    location=city,
                    notes=_NOTES,
                    imported_at=imported_at,
                )
            )
    return entries
