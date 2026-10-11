-- Adds a one-off calendar date to catalog entries, for schedules like a
-- Priyom event with no RRULE (or one whose FREQ we can't safely apply)
-- that occur on a single day rather than recurring per `days`. See
-- msdr/catalog/models.py's CatalogEntry.date, on_air() and next_start().
ALTER TABLE catalog_entries ADD COLUMN date TEXT;
