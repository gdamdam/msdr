CREATE TABLE survey_scan_spans (
  scan_id TEXT NOT NULL REFERENCES survey_band_scans(id) ON DELETE CASCADE,
  span_index INTEGER NOT NULL,
  data_json TEXT NOT NULL,
  PRIMARY KEY (scan_id, span_index)
);
CREATE TABLE survey_scan_results (
  scan_id TEXT NOT NULL REFERENCES survey_band_scans(id) ON DELETE CASCADE,
  frequency_hz INTEGER NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  observation_count INTEGER NOT NULL DEFAULT 0,
  snr_min_db REAL,
  snr_max_db REAL,
  snr_sum_db REAL NOT NULL DEFAULT 0,
  snr_count INTEGER NOT NULL DEFAULT 0,
  span_centers_json TEXT NOT NULL DEFAULT '[]',
  identities_json TEXT NOT NULL DEFAULT '[]',
  latest_json TEXT NOT NULL DEFAULT '{}',
  strongest_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (scan_id, frequency_hz)
);
CREATE TABLE survey_scan_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id TEXT NOT NULL REFERENCES survey_band_scans(id) ON DELETE CASCADE,
  frequency_hz INTEGER NOT NULL,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 1,
  snr_min_db REAL,
  snr_max_db REAL,
  snr_sum_db REAL NOT NULL DEFAULT 0,
  snr_count INTEGER NOT NULL DEFAULT 0,
  signature TEXT NOT NULL,
  first_json TEXT NOT NULL,
  latest_json TEXT NOT NULL,
  strongest_json TEXT NOT NULL
);
CREATE INDEX survey_scan_observations_page ON survey_scan_observations(scan_id, id);
CREATE INDEX survey_scan_observations_frequency ON survey_scan_observations(scan_id, frequency_hz, id);
CREATE TABLE survey_scan_spectrum (
  scan_id TEXT PRIMARY KEY REFERENCES survey_band_scans(id) ON DELETE CASCADE,
  data_json TEXT NOT NULL
);

CREATE INDEX survey_scan_observations_recent ON survey_scan_observations(scan_id, last_seen DESC, id DESC);
CREATE INDEX survey_scan_observations_frequency_recent ON survey_scan_observations(scan_id, frequency_hz, last_seen DESC, id DESC);
