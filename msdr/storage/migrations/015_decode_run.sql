-- Replay sample positions are local to a run, not the whole recording.
-- Older events retain NULL: their original run cannot be reconstructed safely.
ALTER TABLE decode_events ADD COLUMN run_index INTEGER;
