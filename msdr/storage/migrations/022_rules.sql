-- Watch rules (docs/rules.md, TODO "Phase 6 -- Watch rules"): beacon
-- presence/absence, BLE sensor thresholds and Remote ID drone geofences.
-- Kept in the main database, like device labels (migration 021), so backup
-- and restore carry user-authored rules and the recent fired-event log.
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('beacon_presence', 'sensor_threshold', 'drone_geofence')),
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL,
    cooldown_s REAL NOT NULL DEFAULT 60.0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE rule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES rules (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    fired_at REAL NOT NULL
);

CREATE INDEX rule_events_rule_id_fired_at ON rule_events (rule_id, fired_at);
CREATE INDEX rule_events_fired_at ON rule_events (fired_at);
