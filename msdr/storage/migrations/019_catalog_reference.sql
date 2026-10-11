-- Reference catalogs (docs/reference-catalogs.md): NIST time stations, FAA
-- navaids, IOOS HF radar and SuperDARN sites join the broadcast schedules in
-- catalog_entries. frequency_hz becomes nullable (DME/TACAN and radar sites
-- have no tunable fixed frequency), which SQLite cannot ALTER in place, so
-- the table is rebuilt. Rows are copied with their rowid preserved: the
-- external-content FTS5 index (catalog_fts, created by CatalogRepo) is keyed
-- by rowid, so existing text search keeps working without a rebuild; its
-- sync triggers are dropped with the old table and CatalogRepo recreates
-- them (IF NOT EXISTS) on next construction.
-- Column set = 004_catalog.sql + 005 (date) + 016 (latitude, longitude)
-- + the reference fields below.

CREATE TABLE catalog_entries_new (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    frequency_hz INTEGER,
    station TEXT NOT NULL,
    mode TEXT,
    bandwidth_hz INTEGER,
    start_utc TEXT,
    stop_utc TEXT,
    days TEXT,
    language TEXT,
    target TEXT,
    country TEXT,
    location TEXT,
    power_kw REAL,
    notes TEXT,
    season TEXT,
    imported_at TEXT NOT NULL,
    date TEXT,
    latitude REAL,
    longitude REAL,
    ref_type TEXT NOT NULL DEFAULT 'schedule',
    network TEXT,
    station_id TEXT,
    component TEXT,
    operator TEXT,
    freq_lo_hz INTEGER,
    freq_hi_hz INTEGER,
    current_freq_hz INTEGER,
    current_freq_at TEXT,
    status TEXT,
    status_at TEXT,
    source_url TEXT,
    edition TEXT,
    decoder TEXT
);

INSERT INTO catalog_entries_new (
    rowid, id, source, frequency_hz, station, mode, bandwidth_hz, start_utc, stop_utc,
    days, language, target, country, location, power_kw, notes, season, imported_at,
    date, latitude, longitude
)
SELECT
    rowid, id, source, frequency_hz, station, mode, bandwidth_hz, start_utc, stop_utc,
    days, language, target, country, location, power_kw, notes, season, imported_at,
    date, latitude, longitude
FROM catalog_entries;

DROP TABLE catalog_entries;

ALTER TABLE catalog_entries_new RENAME TO catalog_entries;

CREATE INDEX idx_catalog_entries_frequency_hz ON catalog_entries(frequency_hz);
CREATE INDEX idx_catalog_entries_source ON catalog_entries(source);
CREATE INDEX idx_catalog_entries_station_id ON catalog_entries(station_id);
CREATE INDEX idx_catalog_entries_network ON catalog_entries(network);
