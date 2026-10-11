from msdr.catalog import CATALOG_KINDS, CATALOG_LABELS, DEFAULT_URLS, PARSERS, catalog_sources_help

_NEW_SOURCES = {"noaa_radiofax", "sigidwiki", "priyom", "satnogs", "nist_time", "superdarn"}


def test_parsers_contains_all_sources():
    assert _NEW_SOURCES <= PARSERS.keys()
    assert {"eibi", "aoki", "hfcc", "ndxc"} <= PARSERS.keys()


def test_default_urls_only_for_sources_with_a_stable_url():
    # The official Priyom JSON API and SatNOGS have stable endpoints;
    # static sources (noaa_radiofax, sigidwiki) do not need URLs.
    assert DEFAULT_URLS["priyom"] == "https://calendar.priyom.org/events"
    assert "noaa_radiofax" not in DEFAULT_URLS
    assert "sigidwiki" not in DEFAULT_URLS
    assert "nist_time" not in DEFAULT_URLS
    assert "superdarn" not in DEFAULT_URLS
    assert DEFAULT_URLS["satnogs"].startswith("https://db.satnogs.org/")


def test_catalog_sources_help_covers_every_parser():
    help_map = catalog_sources_help()
    assert set(PARSERS.keys()) <= help_map.keys()
    assert all(isinstance(v, str) and v for v in help_map.values())


def test_catalog_kinds_and_labels_cover_every_parser():
    assert set(PARSERS.keys()) <= CATALOG_KINDS.keys()
    assert set(PARSERS.keys()) <= CATALOG_LABELS.keys()
    assert set(CATALOG_KINDS.values()) <= {"static", "refreshable", "live"}
    assert CATALOG_KINDS["nist_time"] == "static"
    assert CATALOG_KINDS["superdarn"] == "static"
    assert CATALOG_KINDS["eibi"] == "refreshable"
    assert CATALOG_KINDS["ioos_hfradar"] == "live"
