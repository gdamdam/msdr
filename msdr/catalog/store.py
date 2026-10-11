"""CatalogRepo: the searchable frequency catalog over `catalog_entries` /
`catalog_sources` (see migrations/004_catalog.sql)."""

from __future__ import annotations

import datetime as dt
import sqlite3

from msdr.catalog.languages import language_matches, language_parts
from msdr.catalog.models import CatalogEntry, Source, on_air
from msdr.library.models import now_iso
from msdr.storage.db import Database

# Candidate cap used by LISTEN's schedule prefilter. Catalog search pages scan
# matching schedule rows to preserve complete on-air ranking across pages.
ON_AIR_PREFILTER_LIMIT = 2000

_COLUMNS = (
    "id",
    "source",
    "frequency_hz",
    "station",
    "mode",
    "bandwidth_hz",
    "start_utc",
    "stop_utc",
    "days",
    "date",
    "language",
    "target",
    "country",
    "location",
    "latitude",
    "longitude",
    "power_kw",
    "notes",
    "season",
    "imported_at",
    # Reference-catalog columns (019_catalog_reference.sql).
    "ref_type",
    "network",
    "station_id",
    "component",
    "operator",
    "freq_lo_hz",
    "freq_hi_hz",
    "current_freq_hz",
    "current_freq_at",
    "status",
    "status_at",
    "source_url",
    "edition",
    "decoder",
)

# FTS5 external-content table + sync triggers, created lazily (see
# CatalogRepo._bootstrap_fts for why this isn't in the migration file).
_FTS_SCRIPT = """
CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
    station, language, target, country, notes,
    content='catalog_entries', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS catalog_fts_ai AFTER INSERT ON catalog_entries BEGIN
    INSERT INTO catalog_fts(rowid, station, language, target, country, notes)
    VALUES (new.rowid, new.station, new.language, new.target, new.country, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS catalog_fts_ad AFTER DELETE ON catalog_entries BEGIN
    INSERT INTO catalog_fts(catalog_fts, rowid, station, language, target, country, notes)
    VALUES ('delete', old.rowid, old.station, old.language, old.target, old.country, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS catalog_fts_au AFTER UPDATE ON catalog_entries BEGIN
    INSERT INTO catalog_fts(catalog_fts, rowid, station, language, target, country, notes)
    VALUES ('delete', old.rowid, old.station, old.language, old.target, old.country, old.notes);
    INSERT INTO catalog_fts(rowid, station, language, target, country, notes)
    VALUES (new.rowid, new.station, new.language, new.target, new.country, new.notes);
END;
"""


def _entry_from_row(row: sqlite3.Row) -> CatalogEntry:
    return CatalogEntry(**{col: row[col] for col in _COLUMNS})


class CatalogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.conn.create_function(
            "catalog_language_matches", 3, language_matches, deterministic=True
        )
        self._fts = self._bootstrap_fts()

    def _bootstrap_fts(self) -> bool:
        opts = {row[0] for row in self.db.conn.execute("PRAGMA compile_options").fetchall()}
        if "ENABLE_FTS5" not in opts:
            return False
        existed = (
            self.db.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_fts'"
            ).fetchone()
            is not None
        )
        # executescript() runs the whole multi-statement script atomically
        # (unlike the migration runner's naive per-statement split), and
        # IF NOT EXISTS makes this safe to call on every CatalogRepo(db).
        self.db.conn.executescript(_FTS_SCRIPT)
        if not existed and self.db.conn.execute("SELECT 1 FROM catalog_entries LIMIT 1").fetchone():
            # Rows imported before the index existed have no FTS entries --
            # the sync triggers only fire from here on -- so a catalog written
            # by an older build would be invisible to every text search.
            self.db.conn.execute("INSERT INTO catalog_fts(catalog_fts) VALUES('rebuild')")
        self.db.conn.commit()
        return True

    def replace_source(
        self,
        source: str,
        entries: list[CatalogEntry],
        *,
        label: str | None = None,
        url: str | None = None,
    ) -> None:
        """Atomically swap out all entries for `source`. Other sources are
        untouched (per-source isolation), and this is idempotent: replacing
        a source with the same entries again leaves the table unchanged."""
        conn = self.db.conn
        season = next((e.season for e in entries if e.season), None)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM catalog_entries WHERE source = ?", (source,))
            conn.executemany(
                f"INSERT INTO catalog_entries ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [tuple(getattr(e, col) for col in _COLUMNS) for e in entries],
            )
            conn.execute(
                "INSERT INTO catalog_sources (source, label, url, season, fetched_at, entry_count) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "label=excluded.label, url=excluded.url, season=excluded.season, "
                "fetched_at=excluded.fetched_at, entry_count=excluded.entry_count",
                (source, label, url, season, now_iso(), len(entries)),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _where(
        self,
        q: str,
        *,
        band: tuple[int | None, int | None] | None,
        source: str | None,
        sources: list[str] | None,
        languages: list[str] | None,
        has_coordinates: bool | None = None,
        network: str | None = None,
        station_id: str | None = None,
        ref_type: str | None = None,
    ) -> tuple[str, str, list]:
        """Returns (from_join, where, params) shared by search() and
        count_matches() so both see exactly the same set of rows."""
        params: list = []
        if q and station_id is not None:
            # The UI sends a short single word as both q and station_id
            # because it might be a call sign ("WWV") or plain text ("china").
            # Match either, so a word that is not an ID still finds text hits.
            frm = "catalog_entries e"
            if self._fts:
                text = "e.rowid IN (SELECT rowid FROM catalog_fts WHERE catalog_fts MATCH ?)"
                params.append('"' + q.replace('"', '""') + '"')
            else:
                text = (
                    "e.station LIKE ? OR e.language LIKE ? OR e.target LIKE ? "
                    "OR e.country LIKE ? OR e.notes LIKE ?"
                )
                params.extend([f"%{q}%"] * 5)
            where = f"({text} OR e.station_id = ? COLLATE NOCASE)"
            params.append(station_id)
            station_id = None
        elif q and self._fts:
            frm = "catalog_entries e JOIN catalog_fts f ON f.rowid = e.rowid"
            where = "catalog_fts MATCH ?"
            params.append('"' + q.replace('"', '""') + '"')  # phrase match, case-insensitive
        elif q:
            like = f"%{q}%"
            frm = "catalog_entries e"
            where = (
                "(e.station LIKE ? OR e.language LIKE ? OR e.target LIKE ? "
                "OR e.country LIKE ? OR e.notes LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        else:
            frm = "catalog_entries e"
            where = "1=1"
        if source is not None:
            where += " AND e.source = ?"
            params.append(source)
        if sources:
            where += f" AND e.source IN ({', '.join('?' for _ in sources)})"
            params.extend(sources)
        if band is not None:
            # NULL frequency_hz never satisfies either comparison, so
            # untunable reference rows drop out of any band filter.
            lo, hi = band
            if lo is not None:
                where += " AND e.frequency_hz >= ?"
                params.append(lo)
            if hi is not None:
                where += " AND e.frequency_hz <= ?"
                params.append(hi)
        if network is not None:
            where += " AND e.network = ? COLLATE NOCASE"
            params.append(network)
        if station_id is not None:
            where += " AND e.station_id = ? COLLATE NOCASE"
            params.append(station_id)
        if ref_type is not None:
            where += " AND e.ref_type = ?"
            params.append(ref_type)
        if languages:
            where += " AND (" + " OR ".join(
                "catalog_language_matches(e.source, e.language, ?) = 1" for _ in languages
            ) + ")"
            params.extend(languages)
        if has_coordinates is True:
            where += " AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL"
        elif has_coordinates is False:
            where += " AND (e.latitude IS NULL OR e.longitude IS NULL)"
        return frm, where, params

    def search(
        self,
        q: str,
        *,
        limit: int = 50,
        offset: int = 0,
        on_air_at: dt.datetime | None = None,
        band: tuple[int | None, int | None] | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        languages: list[str] | None = None,
        has_coordinates: bool | None = None,
        network: str | None = None,
        station_id: str | None = None,
        ref_type: str | None = None,
    ) -> list[CatalogEntry]:
        frm, where, params = self._where(
            q, band=band, source=source, sources=sources, languages=languages,
            has_coordinates=has_coordinates, network=network, station_id=station_id,
            ref_type=ref_type,
        )
        sql = f"SELECT e.* FROM {frm} WHERE {where}"
        if on_air_at is None:
            rows = self.db.conn.execute(
                sql + " LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()
            return [_entry_from_row(row) for row in rows]
        # On-air is a ranking preference, not an exclusion filter. Schedules
        # include weekdays, one-off dates and overnight windows, so count the
        # active segment first, then collect the requested page in a second
        # pass. Pagination stays exact with memory bounded to one page.
        page_end = offset + limit
        active_count = 0
        rows = self.db.conn.execute(sql, params)
        for row in rows:
            if on_air(_entry_from_row(row), on_air_at):
                active_count += 1
        page: list[CatalogEntry | None] = [None] * limit
        active_index = 0
        inactive_index = 0
        for row in self.db.conn.execute(sql, params):
            entry = _entry_from_row(row)
            if on_air(entry, on_air_at):
                position = active_index
                active_index += 1
            else:
                position = active_count + inactive_index
                inactive_index += 1
            if offset <= position < page_end:
                page[position - offset] = entry
        return [entry for entry in page if entry is not None]

    def count_matches(
        self,
        q: str,
        *,
        band: tuple[int | None, int | None] | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        languages: list[str] | None = None,
        has_coordinates: bool | None = None,
        network: str | None = None,
        station_id: str | None = None,
        ref_type: str | None = None,
    ) -> int:
        frm, where, params = self._where(
            q, band=band, source=source, sources=sources, languages=languages,
            has_coordinates=has_coordinates, network=network, station_id=station_id,
            ref_type=ref_type,
        )
        row = self.db.conn.execute(
            f"SELECT COUNT(*) AS n FROM {frm} WHERE {where}", params
        ).fetchone()
        return row["n"]

    def near(
        self, frequency_hz: int, tolerance_hz: int = 5000, at: dt.datetime | None = None
    ) -> list[CatalogEntry]:
        # BETWEEN is NULL for a NULL frequency_hz, so untunable reference
        # rows never match a frequency lookup.
        rows = self.db.conn.execute(
            "SELECT * FROM catalog_entries WHERE frequency_hz BETWEEN ? AND ? "
            "ORDER BY ABS(frequency_hz - ?)",
            (frequency_hz - tolerance_hz, frequency_hz + tolerance_hz, frequency_hz),
        ).fetchall()
        entries = [_entry_from_row(row) for row in rows]
        if at is not None:
            entries.sort(key=lambda e: not on_air(e, at))
        return entries

    def get(self, entry_id: str) -> CatalogEntry | None:
        row = self.db.conn.execute(
            "SELECT * FROM catalog_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    def sources(self) -> list[Source]:
        rows = self.db.conn.execute("SELECT * FROM catalog_sources ORDER BY source").fetchall()
        return [Source(**dict(row)) for row in rows]

    def languages(self) -> list[dict[str, str | int]]:
        """Languages present in imported entries, with counts for the UI filter."""
        counts: dict[str, dict[str, str | int]] = {}
        rows = self.db.conn.execute(
            "SELECT source, language FROM catalog_entries "
            "WHERE language IS NOT NULL AND TRIM(language) != ''"
        )
        for row in rows:
            # A compound entry contributes to each language it names.
            parts: dict[str, str] = {}
            for identity, display in language_parts(row["source"], row["language"]):
                parts.setdefault(identity, display)
            for identity, display in parts.items():
                item = counts.setdefault(
                    identity,
                    {
                        "language": display,
                        "language_filter": f"identity:{identity}",
                        "entry_count": 0,
                    },
                )
                item["entry_count"] = int(item["entry_count"]) + 1
                # Prefer the canonical English label; otherwise retain the
                # first source spelling, matching the historic display rule.
        return sorted(counts.values(), key=lambda item: str(item["language"]).casefold())

    def count(self) -> int:
        row = self.db.conn.execute("SELECT COUNT(*) AS n FROM catalog_entries").fetchone()
        return row["n"]
