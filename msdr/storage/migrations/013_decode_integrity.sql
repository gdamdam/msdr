-- Decode event integrity and provenance (S4, 0.29.0). Additive only.
-- integrity comes from the decoder's capability-matrix policy and the
-- backend's own check result -- never guessed (see
-- docs/epics/listening-workbench/S4-decode-contract.md section 1). Rows from
-- before this migration keep NULL here, i.e. "not recorded", not "passed".
-- source_id/generation/sample_index/analysis_run_id tie a replay or
-- re-analysis event to its input so it never inflates live counts.
-- NB: db._split_statements splits on semicolons, so none may appear in
-- comments below.

ALTER TABLE decode_events ADD COLUMN integrity TEXT;
ALTER TABLE decode_events ADD COLUMN integrity_note TEXT;
ALTER TABLE decode_events ADD COLUMN source_id TEXT;
ALTER TABLE decode_events ADD COLUMN generation INTEGER;
ALTER TABLE decode_events ADD COLUMN sample_index INTEGER;
ALTER TABLE decode_events ADD COLUMN analysis_run_id TEXT;
