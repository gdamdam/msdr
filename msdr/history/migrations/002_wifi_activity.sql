-- Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): one row per
-- (second, channel) actually observed, drained from ActivityAccumulator by
-- the same 30 s history flush as `buckets`. Per-second rows are bulky
-- (~86,400/day per channel) so this table gets its own, shorter retention
-- (settings.history.activity_retention_hours) rather than sharing
-- retention_days with `buckets`/`coverage`.
CREATE TABLE wifi_activity (
    second INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    packets INTEGER NOT NULL DEFAULT 0,
    listened_s REAL NOT NULL DEFAULT 0,
    sig_sum REAL NOT NULL DEFAULT 0,
    sig_n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (second, channel)
) WITHOUT ROWID;

-- Range reads scan by time across every channel.
CREATE INDEX wifi_activity_by_second ON wifi_activity (second);
