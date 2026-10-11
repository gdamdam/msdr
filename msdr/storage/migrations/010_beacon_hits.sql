-- Beacon monitor (S10): NCDXF/IARU + NDB session hits logged here for the
-- BEACONS grid/strip/table. See
-- docs/superpowers/plans/2026-09-25-survey-s10-beacons.md.

CREATE TABLE beacon_hits (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    beacon TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
    snr_db REAL,
    decoded INTEGER NOT NULL DEFAULT 0,
    probable INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL
);

CREATE INDEX idx_beacon_hits_ts ON beacon_hits(ts);
