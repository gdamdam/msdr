-- Scanner finds log: one row per (roughly) distinct frequency seen by a
-- scan run with log_finds enabled, merged across runs within tolerance. See
-- docs/superpowers/plans/2026-09-25-scanner-finds.md.

CREATE TABLE finds (id TEXT PRIMARY KEY, first_ts REAL NOT NULL, last_ts REAL NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  frequency_hz INTEGER NOT NULL, label TEXT, signal_id TEXT, peak_snr_db REAL, last_duration_s REAL,
  demodulator TEXT, bandwidth_hz INTEGER, classification_json TEXT, status TEXT NOT NULL DEFAULT 'new', note TEXT);
CREATE INDEX idx_finds_freq ON finds(frequency_hz); CREATE INDEX idx_finds_status ON finds(status);
