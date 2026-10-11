-- Optional, explicitly supplied station locations used by Discover maps.
ALTER TABLE signals ADD COLUMN latitude REAL;
ALTER TABLE signals ADD COLUMN longitude REAL;
ALTER TABLE catalog_entries ADD COLUMN latitude REAL;
ALTER TABLE catalog_entries ADD COLUMN longitude REAL;
