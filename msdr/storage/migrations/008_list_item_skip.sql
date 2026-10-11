-- Scanner (v0.12.0): a list item can be flagged to skip when the scanner's
-- list source sweeps through it (POST /api/scan/skip {persist: true}, or the
-- dedicated PATCH route). Boolean-as-integer like list_items' other columns
-- have no precedent for yet, but matches signals'/lists' own convention
-- elsewhere in this schema (e.g. lists.builtin).

ALTER TABLE list_items ADD COLUMN skip INTEGER NOT NULL DEFAULT 0;
