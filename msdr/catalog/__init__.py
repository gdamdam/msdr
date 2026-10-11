"""Searchable catalog of known frequencies, fed by shortwave schedules
(EiBi, Aoki, HFCC) and user frequency-manager imports. Wiring into the API
and settings is a later task; this package is the parsers + storage only."""

from msdr.catalog.parsers.aoki import parse_aoki
from msdr.catalog.parsers.eibi import parse_eibi
from msdr.catalog.parsers.faa_nasr import parse_faa_nasr
from msdr.catalog.parsers.hfcc import parse_hfcc
from msdr.catalog.parsers.ioos_hfradar import default_url as ioos_hfradar_default_url
from msdr.catalog.parsers.ioos_hfradar import parse_ioos_hfradar
from msdr.catalog.parsers.ndxc import default_url as ndxc_default_url
from msdr.catalog.parsers.ndxc import parse_ndxc
from msdr.catalog.parsers.nist_time import parse_nist_time
from msdr.catalog.parsers.priyom import default_url as priyom_default_url
from msdr.catalog.parsers.priyom import parse_priyom
from msdr.catalog.parsers.radiofax import parse_radiofax
from msdr.catalog.parsers.satnogs import default_url as satnogs_default_url
from msdr.catalog.parsers.satnogs import parse_satnogs, url_for_norad
from msdr.catalog.parsers.sigid import parse_sigid
from msdr.catalog.parsers.superdarn import parse_superdarn
from msdr.catalog.parsers.userlists import parse_gqrx_csv, parse_sdrpp_json

PARSERS = {
    "eibi": parse_eibi,
    "aoki": parse_aoki,
    "hfcc": parse_hfcc,
    "ndxc": parse_ndxc,
    "noaa_radiofax": parse_radiofax,
    "sigidwiki": parse_sigid,
    "priyom": parse_priyom,
    "satnogs": parse_satnogs,
    "nist_time": parse_nist_time,
    "superdarn": parse_superdarn,
    "faa_nasr": parse_faa_nasr,
    "ioos_hfradar": parse_ioos_hfradar,
}

# How each source is kept current (docs/reference-catalogs.md, "API and UI"):
# "static" is a bundled, hand-reviewed snapshot; "refreshable" re-downloads
# a dataset on demand; "live" carries a data-recency signal from the source.
# Every PARSERS key must appear here (tests/catalog/test_registration.py).
CATALOG_KINDS = {
    "eibi": "refreshable",
    "aoki": "refreshable",
    "hfcc": "refreshable",
    "ndxc": "refreshable",
    "noaa_radiofax": "static",
    "sigidwiki": "static",
    "priyom": "refreshable",
    "satnogs": "refreshable",
    "nist_time": "static",
    "superdarn": "static",
    "faa_nasr": "refreshable",
    "ioos_hfradar": "live",
}

# Default fetch URLs for sources that have a stable one (blank/absent means
# "left disabled" -- see msdr.settings.Settings.catalog_sources). Static
# sources (noaa_radiofax, sigidwiki) have none.
DEFAULT_URLS = {
    "ndxc": ndxc_default_url,
    "priyom": priyom_default_url,
    "satnogs": satnogs_default_url,
    "ioos_hfradar": ioos_hfradar_default_url,
    # faa_nasr has no single stable URL here: it needs two per-cycle zips
    # (NAV_CSV + ILS_CSV) computed from the current 28-day cycle date, not a
    # fixed string -- see msdr/catalog/parsers/faa_nasr.py's fetch_faa_nasr().
}


def catalog_sources_help() -> dict[str, str]:
    """One-line description per source, for the SETUP UI."""
    return {
        "eibi": (
            "EiBi is a worldwide shortwave broadcast schedule, with station, frequency, "
            "language and airtime listings."
        ),
        "aoki": (
            "Aoki (JSEA) lists international shortwave broadcast schedules, including "
            "frequencies, languages and target areas."
        ),
        "hfcc": (
            "HFCC/ASBU publishes coordinated seasonal shortwave broadcast schedules "
            "from participating broadcasters."
        ),
        "ndxc": (
            "NDXC (ndxc.info) maintains a shortwave broadcast schedule with station, "
            "frequency and airtime listings."
        ),
        "noaa_radiofax": (
            "Bundled NOAA/USCG HF weather radiofax (WEFAX) station frequencies; "
            "a static reference list."
        ),
        "sigidwiki": (
            "Bundled Signal Identification Wiki reference examples of well-known "
            "signal types; not a live schedule."
        ),
        "priyom": (
            "Priyom tracks scheduled numbers-station transmissions. Refresh reads its "
            "calendar API; legacy iCalendar feeds are supported."
        ),
        "satnogs": (
            "SatNOGS DB lists satellite transmitters and frequencies; "
            "optionally filter by NORAD ID."
        ),
        "nist_time": (
            "Bundled NIST time-signal stations WWV, WWVH and WWVB: one record per "
            "frequency with antenna coordinates; a dated, hand-reviewed snapshot."
        ),
        "superdarn": (
            "Bundled SuperDARN radar sites from the public hardware tables: "
            "identifiers and coordinates only, no operating frequencies."
        ),
        "faa_nasr": (
            "FAA NASR navaid and ILS localizer reference data (VOR/VOR-DME/VORTAC, NDB, "
            "DME, TACAN, localizers), refreshed on the 28-day NASR cycle."
        ),
        "ioos_hfradar": (
            "IOOS HFRNet coastal HF radar site list: nominal operating frequency and "
            "manufacturer per site, with a data-recency status."
        ),
    }


# Display label per source (SETUP UI's CATALOG group); keys match PARSERS.
CATALOG_LABELS = {
    "eibi": "EiBi",
    "aoki": "Aoki",
    "hfcc": "HFCC",
    "ndxc": "NDXC",
    "noaa_radiofax": "NOAA Radiofax",
    "sigidwiki": "SigIDWiki",
    "priyom": "Priyom",
    "satnogs": "SatNOGS",
    "nist_time": "NIST time",
    "superdarn": "SuperDARN",
    "faa_nasr": "FAA NASR",
    "ioos_hfradar": "IOOS HFRNet",
}


__all__ = [
    "PARSERS",
    "DEFAULT_URLS",
    "CATALOG_LABELS",
    "CATALOG_KINDS",
    "catalog_sources_help",
    "parse_eibi",
    "parse_aoki",
    "parse_hfcc",
    "parse_ndxc",
    "parse_nist_time",
    "parse_superdarn",
    "parse_faa_nasr",
    "parse_ioos_hfradar",
    "parse_radiofax",
    "parse_sigid",
    "parse_priyom",
    "parse_satnogs",
    "url_for_norad",
    "parse_sdrpp_json",
    "parse_gqrx_csv",
]
