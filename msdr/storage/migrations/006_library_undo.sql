-- Undo log for LIBRARY edits: each row is a label plus the JSON before-image
-- of every signal one operation touched, so the latest one can be popped and
-- replayed backwards. No triggers -- the migration runner splits this file
-- naively on ';'.

CREATE TABLE library_undo (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    payload TEXT NOT NULL
);
