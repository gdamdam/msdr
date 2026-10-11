-- Colors for signals and tags/categories.

ALTER TABLE tags ADD COLUMN color TEXT;
ALTER TABLE signals ADD COLUMN color TEXT;
