-- Per-signal antenna memory (TODO: Antenna control and per-signal antenna
-- memory). None = leave the current antenna unchanged on tune; existing rows
-- get NULL, same meaning.
ALTER TABLE signals ADD COLUMN antenna TEXT;
