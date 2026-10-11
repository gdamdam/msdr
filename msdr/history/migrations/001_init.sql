-- Device history (docs/history.md). A separate database from msdr.db:
-- bulk, regenerable observation data that does not belong in backups.
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE buckets (
    receiver TEXT NOT NULL,
    ident TEXT NOT NULL,
    minute INTEGER NOT NULL,
    reports INTEGER NOT NULL,
    sig_min REAL,
    sig_max REAL,
    sig_sum REAL NOT NULL DEFAULT 0,
    sig_n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (receiver, ident, minute)
) WITHOUT ROWID;

-- Timeline reads scan a time range across every device.
CREATE INDEX buckets_by_minute ON buckets (minute);

CREATE TABLE devices (
    receiver TEXT NOT NULL,
    ident TEXT NOT NULL,
    name TEXT,
    kind TEXT,
    signal_kind TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (receiver, ident)
) WITHOUT ROWID;

CREATE TABLE coverage (
    receiver TEXT NOT NULL,
    minute INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    seconds REAL NOT NULL,
    PRIMARY KEY (receiver, minute, channel)
) WITHOUT ROWID;
