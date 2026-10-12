-- Observations and honest evidence (0.27.0). Additive only: nothing is
-- renamed or dropped. A Session is one live run or one replay; an
-- Observation is one reception inside it; Claims are append-only,
-- inspectable assertions about an Observation (msdr.contracts.evidence).
-- NB: db._split_statements splits on semicolons, so none may appear in
-- comments below.

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,          -- "live:sdrconnect" | "replay:<recording name>"
  mode TEXT NOT NULL,               -- "live" | "replay"
  origin TEXT NOT NULL DEFAULT '',  -- adapter host or recording path
  started_at REAL NOT NULL,         -- epoch s (processing clock)
  ended_at REAL,
  center_hz INTEGER, sample_rate_hz INTEGER,
  receiver_json TEXT,               -- antenna/gain/profile snapshot
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE observations (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id),
  signal_id TEXT,                   -- optional memory link, may stay NULL
  find_id TEXT,                     -- scanner find this came from (legacy import or live hit)
  start_ts REAL, end_ts REAL,       -- original capture time when known, else NULL
  sample_start INTEGER, sample_end INTEGER,  -- replay: sample range on the source
  reported_hz INTEGER,              -- asserted/tuned frequency
  measured_hz INTEGER,              -- centroid, kept separate, never overwrites reported
  bandwidth_hz INTEGER,
  receiver_json TEXT,               -- demodulator/filter/antenna/gain snapshot
  measurements_json TEXT NOT NULL DEFAULT '[]',
  capture_quality_json TEXT NOT NULL DEFAULT '[]',
  legacy INTEGER NOT NULL DEFAULT 0, -- 1 = summary imported from finds/decode_events
  provenance TEXT NOT NULL DEFAULT '', -- e.g. "survey label", "scanner hit", "migrated: finds"
  closed INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX idx_obs_freq ON observations(reported_hz);
CREATE INDEX idx_obs_signal ON observations(signal_id);
CREATE INDEX idx_obs_session ON observations(session_id);

CREATE TABLE claims (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id),
  subject TEXT NOT NULL, value TEXT NOT NULL,
  evidence_type TEXT NOT NULL, integrity TEXT NOT NULL,
  producer TEXT NOT NULL, producer_version TEXT NOT NULL DEFAULT '',
  observed_start REAL, observed_end REAL, sample_start INTEGER, sample_end INTEGER,
  support_json TEXT NOT NULL DEFAULT '[]', measurements_json TEXT NOT NULL DEFAULT '[]',
  alternatives_json TEXT NOT NULL DEFAULT '[]', limitations_json TEXT NOT NULL DEFAULT '[]',
  score REAL, next_action TEXT NOT NULL DEFAULT '',
  analysis_run_id TEXT,             -- review reruns carry a run id and never inflate live counts
  superseded_by TEXT,               -- append-only: newer claim id
  created_at REAL NOT NULL
);
CREATE INDEX idx_claims_obs ON claims(observation_id);

CREATE TABLE recording_refs (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id),
  path TEXT NOT NULL, name TEXT NOT NULL,
  run_index INTEGER, sample_start INTEGER, sample_end INTEGER,
  available INTEGER NOT NULL DEFAULT 1,   -- 0 = media missing/deleted: explicit unavailable
  note TEXT NOT NULL DEFAULT ''
);

ALTER TABLE decode_events ADD COLUMN observation_id TEXT;
ALTER TABLE finds ADD COLUMN observation_id TEXT;

-- Summary rows look up an observation's decode events, so this must not be
-- a scan of the whole (large, long-lived) decode history per row.
CREATE INDEX idx_decode_events_obs ON decode_events(observation_id);
