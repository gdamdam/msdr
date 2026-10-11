-- Searchable catalog of known shortwave/broadcast frequencies, fed by the
-- EiBi/Aoki/HFCC schedule parsers and user frequency-manager imports.
--
-- FTS5 (catalog_fts) and its sync triggers are deliberately NOT created
-- here. The migration runner in storage/db.py (_split_statements) strips
-- comments and splits each file naively on ';' before executing statements
-- one at a time; that breaks a `CREATE TRIGGER ... BEGIN ... END;` body,
-- whose own internal ';' terminators get split apart from the trailing
-- END. CatalogRepo creates the FTS5 table and triggers itself, via
-- executescript() (which runs the whole multi-statement script at once,
-- unlike the migration runner), guarded by "IF NOT EXISTS" and by a
-- PRAGMA compile_options capability check -- so catalog search still works
-- (falling back to LIKE) on SQLite builds without FTS5.

CREATE TABLE catalog_entries (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
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
    imported_at TEXT NOT NULL
);

CREATE INDEX idx_catalog_entries_frequency_hz ON catalog_entries(frequency_hz);
CREATE INDEX idx_catalog_entries_source ON catalog_entries(source);

CREATE TABLE catalog_sources (
    source TEXT PRIMARY KEY,
    label TEXT,
    url TEXT,
    season TEXT,
    fetched_at TEXT,
    entry_count INTEGER
);
