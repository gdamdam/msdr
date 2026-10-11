CREATE TABLE survey_band_scans (
  id TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_count INTEGER NOT NULL DEFAULT 0,
  span_count INTEGER NOT NULL DEFAULT 0,
  covered_count INTEGER NOT NULL DEFAULT 0,
  uncovered_count INTEGER NOT NULL DEFAULT 0,
  started_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX survey_band_scans_started ON survey_band_scans(started_at DESC);
