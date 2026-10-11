-- User-defined lists (e.g. "Favorites") grouping signals, plus saved LIBRARY
-- searches. No triggers -- the migration runner splits this file naively on
-- ';' (see 004_catalog.sql). PRAGMA foreign_keys is ON (storage/db.py), so
-- list_items cascades on either side exactly like signal_tags already does.

CREATE TABLE lists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

INSERT INTO lists (id, name, position, builtin, created_at)
VALUES ('favorites', 'Favorites', 0, 1, '2026-09-23T00:00:00Z');

CREATE TABLE list_items (
    list_id TEXT REFERENCES lists(id) ON DELETE CASCADE,
    signal_id TEXT REFERENCES signals(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, signal_id)
);

CREATE TABLE saved_searches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
