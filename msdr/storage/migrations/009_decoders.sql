-- Signal decoders (P1): a memory can carry a list of decoder ids
-- (Signal.decoders, e.g. ["pocsag", "flex"]) that start automatically when
-- it's tuned (see AppService.tune_signal); decoded messages are logged here
-- for the DECODE messages view and history. See
-- docs/superpowers/specs/2026-09-25-decoders-design.md.

ALTER TABLE signals ADD COLUMN decoders_json TEXT;

CREATE TABLE decode_events (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    frequency_hz INTEGER,
    signal_id TEXT,
    decoder TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    fields_json TEXT,
    image_path TEXT
);

CREATE INDEX idx_decode_events_ts ON decode_events(ts);
