-- Initial schema. States and profiles are stored as JSON so the schema
-- survives registry changes; signals are columnar because they are queried
-- (search, inbox, nearby-frequency lookups).

CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE receiver_states (
    id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    captured_at REAL,
    source TEXT
);

CREATE TABLE profiles (
    id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    name TEXT UNIQUE,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
    demodulator TEXT,
    filter_bandwidth_hz INTEGER,
    profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
    station TEXT,
    location TEXT,
    urls_json TEXT,
    notes TEXT,
    first_heard_at TEXT,
    last_heard_at TEXT,
    heard_count INTEGER,
    origin_state_id TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX idx_signals_frequency_hz ON signals(frequency_hz);

CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE,
    kind TEXT NOT NULL
);

CREATE TABLE signal_tags (
    signal_id TEXT REFERENCES signals(id) ON DELETE CASCADE,
    tag_id TEXT REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (signal_id, tag_id)
);

CREATE TABLE history (
    position INTEGER PRIMARY KEY,
    state_id TEXT
);

CREATE TABLE history_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE journal_entries (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    state_id TEXT,
    signal_id TEXT,
    profile_id TEXT,
    signal_power_db REAL,
    signal_snr_db REAL,
    note TEXT
);

CREATE INDEX idx_journal_entries_at ON journal_entries(at);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT
);
