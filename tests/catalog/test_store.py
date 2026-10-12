import datetime as dt

import pytest

from msdr.catalog import store
from msdr.catalog.models import CatalogEntry
from msdr.catalog.store import CatalogRepo
from msdr.storage.db import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    return CatalogRepo(db)


def _entry(**overrides) -> CatalogEntry:
    base = dict(source="eibi", frequency_hz=6070_000, station="Deutschlandfunk")
    base.update(overrides)
    return CatalogEntry(**base)


def _count_entry_builds(monkeypatch) -> dict:
    """Counts row -> CatalogEntry conversions, so a test can assert a query
    never materializes the whole table."""
    counter = {"n": 0}
    real = store._entry_from_row

    def counting(row):
        counter["n"] += 1
        return real(row)

    monkeypatch.setattr(store, "_entry_from_row", counting)
    return counter


def test_replace_source_populates_entries_and_sources(repo):
    repo.replace_source("eibi", [_entry(), _entry(station="Radio Habana Cuba")], url="http://x")
    assert repo.count() == 2
    sources = repo.sources()
    assert len(sources) == 1
    assert sources[0].source == "eibi"
    assert sources[0].entry_count == 2
    assert sources[0].url == "http://x"


def test_replace_source_is_idempotent(repo):
    entries = [_entry(), _entry(station="Radio Habana Cuba")]
    repo.replace_source("eibi", entries)
    repo.replace_source("eibi", entries)  # re-import same data
    assert repo.count() == 2


def test_replace_source_is_isolated_per_source(repo):
    repo.replace_source("eibi", [_entry()])
    repo.replace_source("aoki", [_entry(source="aoki", station="Other")])
    assert repo.count() == 2
    repo.replace_source("eibi", [_entry(), _entry(station="Second EiBi Row")])
    # replacing "eibi" must not touch "aoki" rows
    assert repo.count() == 3
    assert {e.station for e in repo.search("", source="aoki")} == {"Other"}


def test_languages_lists_imported_values_and_counts(repo):
    repo.replace_source("eibi", [
        _entry(language="English"),
        _entry(station="Other", language=" english "),
        _entry(station="No language"),
    ])
    repo.replace_source("aoki", [_entry(source="aoki", language="French")])
    assert repo.languages() == [
        {"language": "English", "language_filter": "identity:english", "entry_count": 2},
        {"language": "French", "language_filter": "identity:french", "entry_count": 1},
    ]


def test_language_filters_group_source_aliases_and_compounds_without_rewriting_values(repo):
    repo.replace_source("eibi", [
        _entry(station="EiBi code", language="E"),
        _entry(station="EiBi compound", language="E/S"),
        _entry(station="EiBi abbreviation", language="Eng"),
    ])
    repo.replace_source("aoki", [
        _entry(source="aoki", station="Aoki full name", language="English"),
        _entry(source="aoki", station="Aoki one-letter value", language="E"),
    ])

    assert {item["language"]: item["entry_count"] for item in repo.languages()} == {
        "English": 4,
        "E": 1,
        "S": 1,
    }
    assert {entry.station for entry in repo.search("", languages=["E"])} == {
        "EiBi code", "EiBi compound", "EiBi abbreviation", "Aoki full name",
    }
    assert {entry.station for entry in repo.search("", languages=["identity:e"])} == {
        "Aoki one-letter value",
    }
    imported = next(e for e in repo.search("", source="eibi") if e.station == "EiBi code")
    assert imported.language == "E"


def test_hfcc_language_codes_use_cached_names_and_unknown_codes_stay_raw(repo, tmp_path):
    from msdr.catalog.languages import set_hfcc_languages_path

    path = tmp_path / "languages.json"
    path.write_text(
        '{"Aae":"Arbëreshë Albanian","Aue":"=/Kx\'au//\'ein"}', encoding="utf-8"
    )
    set_hfcc_languages_path(path)
    try:
        _check_hfcc_language_codes(repo)
    finally:
        set_hfcc_languages_path(None)


def _check_hfcc_language_codes(repo):
    repo.replace_source("hfcc", [
        _entry(source="hfcc", station="HFCC code", language="Aae"),
        _entry(source="hfcc", station="HFCC slash name", language="Aue"),
        _entry(source="hfcc", station="HFCC legacy mojibake", language="ArbÃ«reshÃ« Albanian"),
        _entry(source="hfcc", station="HFCC unknown", language="Xxx"),
    ])
    repo.replace_source("aoki", [
        _entry(source="aoki", station="Aoki name", language="Arbëreshë Albanian"),
        _entry(source="aoki", station="Aoki slash name", language="=/Kx'au//'ein"),
    ])

    assert {item["language"]: item["entry_count"] for item in repo.languages()} == {
        "Arbëreshë Albanian": 3,
        "=/Kx'au//'ein": 2,
        "Xxx": 1,
    }
    filters = {item["language"]: item["language_filter"] for item in repo.languages()}
    arb_entries = repo.search("", languages=[filters["Arbëreshë Albanian"]])
    assert {entry.station for entry in arb_entries} == {
        "HFCC code", "HFCC legacy mojibake", "Aoki name",
    }
    assert {entry.station for entry in repo.search("", languages=[filters["=/Kx'au//'ein"]])} == {
        "HFCC slash name", "Aoki slash name",
    }
    raw = next(entry for entry in repo.search("", source="hfcc") if entry.station == "HFCC code")
    assert raw.language == "Aae"
    legacy = next(
        entry for entry in repo.search("", source="hfcc")
        if entry.station == "HFCC legacy mojibake"
    )
    assert legacy.language == "ArbÃ«reshÃ« Albanian"


def test_compound_case_variants_count_once_per_language(repo):
    repo.replace_source("aoki", [
        _entry(source="aoki", station="Repeated language", language="French/french"),
    ])

    assert repo.languages() == [
        {"language": "French", "language_filter": "identity:french", "entry_count": 1},
    ]


def test_search_fts_finds_station_case_insensitively(repo):
    if not repo._fts:
        pytest.skip("FTS5 not available in this SQLite build")
    repo.replace_source("eibi", [_entry(station="Radio Habana Cuba")])
    results = repo.search("habana")
    assert any("Habana" in e.station for e in results)


def test_search_like_fallback_finds_station_case_insensitively(repo):
    repo.replace_source("eibi", [_entry(station="Radio Habana Cuba")])
    repo._fts = False  # force the LIKE path regardless of FTS5 availability
    results = repo.search("habana")
    assert any("Habana" in e.station for e in results)


def test_near_respects_tolerance(repo):
    repo.replace_source(
        "eibi",
        [
            _entry(frequency_hz=6070_000, station="close"),
            _entry(frequency_hz=6080_000, station="far"),
        ],
    )
    results = repo.near(6070_000, tolerance_hz=5000)
    assert [e.station for e in results] == ["close"]


def test_search_ranks_on_air_entries_first(repo):
    repo.replace_source(
        "eibi",
        [
            _entry(station="off-air", start_utc="1000", stop_utc="1100"),
            _entry(station="on-air", start_utc="0000", stop_utc="2359"),
        ],
    )
    at = dt.datetime(2026, 9, 22, 12, 0)  # inside "on-air"'s window, outside "off-air"'s
    results = repo.search("", on_air_at=at)
    assert results[0].station == "on-air"


def test_search_language_filter_runs_in_sql(repo):
    repo.replace_source(
        "eibi",
        [
            _entry(station="German one", language="G"),
            _entry(station="Spanish one", language="S"),
        ],
    )

    results = repo.search("", languages=["g"])

    assert [e.station for e in results] == ["German one"]


def test_coordinate_filter_returns_only_explicitly_located_entries(repo):
    repo.replace_source("eibi", [
        _entry(station="located", latitude=37.7, longitude=-122.4),
        _entry(station="unknown"),
    ])
    assert [e.station for e in repo.search("", has_coordinates=True)] == ["located"]
    assert repo.count_matches("", has_coordinates=True) == 1


def test_search_band_only_hi_excludes_frequencies_above_it(repo):
    # e.g. the UI's LW/MW filter, which sends only hi_hz.
    repo.replace_source(
        "eibi",
        [
            _entry(station="1 MHz", frequency_hz=1_000_000),
            _entry(station="7.1 MHz", frequency_hz=7_100_000),
            _entry(station="145.8 MHz", frequency_hz=145_800_000),
        ],
    )

    results = repo.search("", band=(None, 1_800_000))

    assert [e.station for e in results] == ["1 MHz"]
    assert repo.count_matches("", band=(None, 1_800_000)) == 1


def test_search_band_only_lo_excludes_frequencies_below_it(repo):
    repo.replace_source(
        "eibi",
        [
            _entry(station="1 MHz", frequency_hz=1_000_000),
            _entry(station="145.8 MHz", frequency_hz=145_800_000),
        ],
    )

    results = repo.search("", band=(30_000_000, None))

    assert [e.station for e in results] == ["145.8 MHz"]
    assert repo.count_matches("", band=(30_000_000, None)) == 1


def test_search_pushes_limit_into_sql_instead_of_materializing_the_table(repo, monkeypatch):
    repo.replace_source(
        "eibi",
        [_entry(station=f"Station {i}", frequency_hz=6_000_000 + i) for i in range(5_000)],
    )
    built = _count_entry_builds(monkeypatch)

    results = repo.search("", limit=10)

    assert len(results) == 10
    assert built["n"] == 10


def test_on_air_search_pages_across_the_full_filtered_catalog(repo, monkeypatch):
    # 5,000 rows, with the only on-air row after the old prefilter boundary.
    # It must still rank first and be discoverable through the complete result.
    entries = [
        _entry(station=f"off {i}", frequency_hz=6_000_000 + i, start_utc="1000", stop_utc="1100")
        for i in range(store.ON_AIR_PREFILTER_LIMIT - 1)
    ]
    entries.append(
        _entry(station="on-air", frequency_hz=9_000_000, start_utc="0000", stop_utc="2359")
    )
    entries += [
        _entry(station=f"tail {i}", frequency_hz=7_000_000 + i, start_utc="1000", stop_utc="1100")
        for i in range(5_000 - len(entries))
    ]
    repo.replace_source("eibi", entries)
    built = _count_entry_builds(monkeypatch)
    at = dt.datetime(2026, 9, 22, 12, 0)

    results = repo.search("", on_air_at=at, limit=5)

    assert results[0].station == "on-air"
    assert built["n"] == 10_000  # two streaming passes, bounded result memory


def test_count_matches_counts_in_sql_without_building_entries(repo, monkeypatch):
    repo.replace_source(
        "eibi", [_entry(station=f"Station {i}", frequency_hz=6_000_000 + i) for i in range(500)]
    )
    built = _count_entry_builds(monkeypatch)

    assert repo.count_matches("") == 500
    assert built["n"] == 0


def test_bootstrap_fts_rebuilds_index_for_rows_written_before_it_existed(db):
    repo = CatalogRepo(db)
    if not repo._fts:  # pragma: no cover - sqlite without FTS5
        pytest.skip("sqlite built without FTS5")
    repo.replace_source("eibi", [_entry(station="Radio Habana Cuba")])
    # Simulate a database written by a build that had no FTS table at all.
    db.conn.executescript(
        "DROP TRIGGER catalog_fts_ai; DROP TRIGGER catalog_fts_ad; "
        "DROP TRIGGER catalog_fts_au; DROP TABLE catalog_fts;"
    )
    db.conn.commit()

    rebuilt = CatalogRepo(db)

    assert [e.station for e in rebuilt.search("Habana")] == ["Radio Habana Cuba"]
