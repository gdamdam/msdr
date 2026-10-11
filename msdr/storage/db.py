"""SQLite connection and schema migration.

One connection per process; the asyncio thread that owns it is the only
caller (`check_same_thread=False` lets it be created off that thread, not
shared across threads).
"""

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path | str, *, migrations_dir: Path | None = None) -> None:
        self.path = path
        self._migrations_dir = migrations_dir or _MIGRATIONS_DIR
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return  # idempotent: repeated calls (app startup + tests) are cheap no-ops
        in_memory = str(self.path) == ":memory:"
        if not in_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not in_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        self._conn = conn
        try:
            self._migrate()
        except Exception:
            # A failed migration must not leave the instance looking
            # "connected" (idempotency check above would then skip retrying
            # it forever); close and reset so the next connect() call retries.
            self.close()
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    def _migrate(self) -> None:
        conn = self.conn
        # schema_version itself is created by 001_init.sql, so it may not
        # exist yet on a brand new database.
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if exists:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
        else:
            current = 0
        for path in sorted(self._migrations_dir.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version <= current:
                continue
            statements = _split_statements(path.read_text())
            # conn.executescript() commits any pending transaction before it
            # runs and does not itself run the script as one transaction, so
            # a mid-script failure previously left earlier CREATE TABLEs in
            # this file committed with schema_version not bumped -- every
            # later connect() then re-ran the same (partially applied) file
            # and hit "table already exists". Explicit BEGIN + per-statement
            # execute + rollback-on-error makes the whole file atomic.
            conn.execute("BEGIN")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            current = version


def _split_statements(script: str) -> list[str]:
    # Migration files are plain DDL with no semicolons inside string literals,
    # so stripping `--` line comments and splitting on `;` is sufficient.
    lines = (line for line in script.splitlines() if not line.strip().startswith("--"))
    cleaned = "\n".join(lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]
