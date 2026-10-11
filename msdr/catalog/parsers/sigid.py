"""A small curated reference table of well-known signal types, keyed to
their Signal Identification Wiki (sigidwiki.com) page. Static data -- no
fetcher: this is a hand-picked "what am I looking at" cheat sheet, not a
schedule, so there is nothing to download or keep in sync with a source file.

CatalogEntry has no notion of a frequency *range*, only a single
`frequency_hz` plus an optional `bandwidth_hz`. The convention used here:
`frequency_hz` is the centre of the signal's typical range and `bandwidth_hz`
is the range's width (hi - lo). A signal pinned to one exact frequency is a
zero-width range (lo == hi), so its `bandwidth_hz` is 0 -- which conveniently
also means `tune_recipe()` falls back to its own default bandwidth for it,
since `0 or 9000` is `9000`."""

from __future__ import annotations

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso


def _wiki(page: str) -> str:
    return f"https://www.sigidwiki.com/wiki/{page}"


# (name, lo_hz, hi_hz, mode, wiki_page)
_SIGNALS: list[tuple[str, int, int, str | None, str]] = [
    ("WEFAX / Radiofax (HF)", 4_000_000, 13_600_000, "USB", "Radiofax"),
    ("NAVTEX (international, 518 kHz)", 518_000, 518_000, "USB", "NAVTEX"),
    ("NAVTEX (national language, 490 kHz)", 490_000, 490_000, "USB", "NAVTEX"),
    ("DSC (Digital Selective Calling)", 2_187_500, 2_187_500, "USB", "Digital_Selective_Calling"),
    ("STANAG 4285", 3_000_000, 30_000_000, "USB", "STANAG_4285"),
    ("ALE 2G (MIL-STD-188-141A)", 2_000_000, 30_000_000, "USB", "2G_ALE"),
    ("RTTY", 3_000_000, 30_000_000, "USB", "RTTY"),
    ("Olivia", 1_800_000, 30_000_000, "USB", "Olivia"),
    ("FT8 (160m)", 1_840_000, 1_840_000, "USB", "FT8"),
    ("FT8 (80m)", 3_573_000, 3_573_000, "USB", "FT8"),
    ("FT8 (40m)", 7_074_000, 7_074_000, "USB", "FT8"),
    ("FT8 (30m)", 10_136_000, 10_136_000, "USB", "FT8"),
    ("FT8 (20m)", 14_074_000, 14_074_000, "USB", "FT8"),
    ("FT8 (17m)", 18_100_000, 18_100_000, "USB", "FT8"),
    ("FT8 (15m)", 21_074_000, 21_074_000, "USB", "FT8"),
    ("FT8 (12m)", 24_915_000, 24_915_000, "USB", "FT8"),
    ("FT8 (10m)", 28_074_000, 28_074_000, "USB", "FT8"),
    ("WSPR (80m)", 3_568_600, 3_568_600, "USB", "WSPR"),
    ("WSPR (40m)", 7_038_600, 7_038_600, "USB", "WSPR"),
    ("WSPR (20m)", 14_095_600, 14_095_600, "USB", "WSPR"),
    ("APRS", 144_390_000, 144_390_000, "NFM", "APRS"),
    ("POCSAG", 929_000_000, 932_000_000, "NFM", "POCSAG"),
    ("P25 (land mobile)", 764_000_000, 806_000_000, "NFM", "P25"),
    ("DMR (land mobile)", 400_000_000, 470_000_000, "NFM", "DMR"),
    ("NXDN (land mobile)", 150_000_000, 174_000_000, "NFM", "NXDN"),
    ("ACARS (131.550 MHz)", 131_550_000, 131_550_000, "AM", "ACARS"),
    ("ACARS (130.025 MHz)", 130_025_000, 130_025_000, "AM", "ACARS"),
    ("ACARS (129.125 MHz)", 129_125_000, 129_125_000, "AM", "ACARS"),
    ("VDL Mode 2", 136_975_000, 136_975_000, "AM", "VDL_Mode_2"),
    ("HFDL", 2_000_000, 22_000_000, "USB", "HFDL"),
    ("ADS-B", 1_090_000_000, 1_090_000_000, None, "ADS-B"),
    ("AIS (channel A, 161.975 MHz)", 161_975_000, 161_975_000, "NFM", "AIS"),
    ("AIS (channel B, 162.025 MHz)", 162_025_000, 162_025_000, "NFM", "AIS"),
    ("NOAA APT (137.1 MHz)", 137_100_000, 137_100_000, "NFM", "NOAA_APT"),
    ("NOAA APT (137.9125 MHz)", 137_912_500, 137_912_500, "NFM", "NOAA_APT"),
    ("NOAA APT (137.62 MHz)", 137_620_000, 137_620_000, "NFM", "NOAA_APT"),
    ("Meteor LRPT (137.1 MHz)", 137_100_000, 137_100_000, "NFM", "Meteor_LRPT"),
    ("Meteor LRPT (137.9 MHz)", 137_900_000, 137_900_000, "NFM", "Meteor_LRPT"),
    ("GOES LRIT/HRIT", 1_694_100_000, 1_694_100_000, None, "LRIT-HRIT"),
    ("CW beacons (20m beacon band)", 14_100_000, 14_112_000, "CW", "CW_Beacon"),
]


def parse_sigid() -> list[CatalogEntry]:
    """No network fetch -- this is the whole (static) source."""
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    for name, lo, hi, mode, page in _SIGNALS:
        entries.append(
            CatalogEntry(
                source="sigidwiki",
                frequency_hz=round((lo + hi) / 2),
                station=name,
                mode=mode,
                bandwidth_hz=hi - lo,
                days=None,
                start_utc="0000",
                stop_utc="2400",
                notes=_wiki(page),
                imported_at=imported_at,
            )
        )
    return entries
