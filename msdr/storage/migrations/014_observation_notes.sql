-- Operator notes on an observation (S5, 0.30.0). Additive only.
-- NB: db._split_statements splits on semicolons, so none may appear in
-- comments below.

ALTER TABLE observations ADD COLUMN notes TEXT NOT NULL DEFAULT '';
