"""Synchronous repositories over a single `Database` connection.

All queries are parameterised. States and profiles are stored as whole-model
JSON (schema survives registry changes); signals are columnar because they
are searched and filtered.
"""

# SignalRepo.list() (a method, per the brief's naming) shadows the builtin
# `list` inside that class body; defer annotation evaluation so `list[Signal]`
# elsewhere still means the builtin generic, not the method.
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from msdr.engine.history import HistoryStack
from msdr.engine.state import ReceiverState
from msdr.ids import uuid7
from msdr.library.frequency import parse_frequency
from msdr.library.models import CATEGORY_TAGS, ReceiverProfile, Signal, Tag, now_iso
from msdr.storage.db import Database


class JournalEntry(BaseModel):
    id: str = Field(default_factory=uuid7)
    at: str = Field(default_factory=now_iso)
    kind: Literal[
        "capture", "tune", "back", "forward", "profile", "observed", "identified", "antenna"
    ]
    state_id: str | None = None
    signal_id: str | None = None
    profile_id: str | None = None
    signal_power_db: float | None = None
    signal_snr_db: float | None = None
    note: str | None = None


class StateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, state: ReceiverState) -> None:
        # OR IGNORE: states are content-addressed by id, re-adding one already
        # persisted (e.g. a history entry loaded from disk) is a no-op.
        self.db.conn.execute(
            "INSERT OR IGNORE INTO receiver_states (id, json, captured_at, source) "
            "VALUES (?, ?, ?, ?)",
            (state.id, state.model_dump_json(), state.captured_at, state.source),
        )
        self.db.conn.commit()

    def get(self, id: str) -> ReceiverState | None:
        row = self.db.conn.execute(
            "SELECT json FROM receiver_states WHERE id = ?", (id,)
        ).fetchone()
        return ReceiverState.model_validate_json(row["json"]) if row else None

    def get_many(self, ids: list[str]) -> list[ReceiverState]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.conn.execute(
            f"SELECT id, json FROM receiver_states WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {row["id"]: ReceiverState.model_validate_json(row["json"]) for row in rows}
        return [by_id[i] for i in ids if i in by_id]  # preserve requested order


def _signal_from_row(row, tag_ids: list[str]) -> Signal:
    return Signal(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        frequency_hz=row["frequency_hz"],
        demodulator=row["demodulator"],
        filter_bandwidth_hz=row["filter_bandwidth_hz"],
        profile_id=row["profile_id"],
        station=row["station"] or "",
        location=row["location"] or "",
        latitude=row["latitude"],
        longitude=row["longitude"],
        urls=json.loads(row["urls_json"]) if row["urls_json"] else [],
        notes=row["notes"] or "",
        tag_ids=tag_ids,
        decoders=json.loads(row["decoders_json"]) if row["decoders_json"] else [],
        decoder_preset=row["decoder_preset"],
        first_heard_at=row["first_heard_at"],
        last_heard_at=row["last_heard_at"],
        heard_count=row["heard_count"] or 0,
        origin_state_id=row["origin_state_id"],
        color=row["color"],
        satellite=row["satellite"],
        antenna=row["antenna"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# Shared by SignalRepo.add() and LibraryUndoRepo.pop_and_restore(): the latter
# can't call SignalRepo.add() itself (its own `with conn:` would commit the
# undo restore's transaction early), so the INSERT is factored out here
# instead of duplicated inline.
_SIGNAL_INSERT_SQL = (
    "INSERT INTO signals ("
    "id, name, status, frequency_hz, demodulator, filter_bandwidth_hz, profile_id, "
    "station, location, latitude, longitude, urls_json, notes, first_heard_at, last_heard_at, "
    "heard_count, origin_state_id, color, satellite, created_at, updated_at, decoders_json, "
    "antenna, decoder_preset"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _signal_insert_values(signal: Signal) -> tuple:
    return (
        signal.id,
        signal.name,
        signal.status,
        signal.frequency_hz,
        signal.demodulator,
        signal.filter_bandwidth_hz,
        signal.profile_id,
        signal.station,
        signal.location,
        signal.latitude,
        signal.longitude,
        json.dumps(signal.urls),
        signal.notes,
        signal.first_heard_at,
        signal.last_heard_at,
        signal.heard_count,
        signal.origin_state_id,
        signal.color,
        signal.satellite,
        signal.created_at,
        signal.updated_at,
        json.dumps(signal.decoders),
        signal.antenna,
        signal.decoder_preset,
    )


# sort= values GET /api/library accepts, mapped to their signals column.
_LIBRARY_SORT_COLUMNS = {
    "name": "name",
    "frequency": "frequency_hz",
    "last_heard": "last_heard_at",
    "heard_count": "heard_count",
    "created": "created_at",
}


class SignalRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, signal: Signal) -> None:
        conn = self.db.conn
        with conn:  # row write and tag replacement commit (or roll back) together
            conn.execute(_SIGNAL_INSERT_SQL, _signal_insert_values(signal))
            self._replace_tags(signal.id, signal.tag_ids)

    def update(self, signal: Signal) -> None:
        signal.updated_at = now_iso()
        conn = self.db.conn
        with conn:  # row write and tag replacement commit (or roll back) together
            conn.execute(
                "UPDATE signals SET name=?, status=?, frequency_hz=?, demodulator=?, "
                "filter_bandwidth_hz=?, profile_id=?, station=?, location=?, latitude=?, "
                "longitude=?, urls_json=?, "
                "notes=?, first_heard_at=?, last_heard_at=?, heard_count=?, origin_state_id=?, "
                "color=?, satellite=?, updated_at=?, decoders_json=?, antenna=?, "
                "decoder_preset=? WHERE id=?",
                (
                    signal.name,
                    signal.status,
                    signal.frequency_hz,
                    signal.demodulator,
                    signal.filter_bandwidth_hz,
                    signal.profile_id,
                    signal.station,
                    signal.location,
                    signal.latitude,
                    signal.longitude,
                    json.dumps(signal.urls),
                    signal.notes,
                    signal.first_heard_at,
                    signal.last_heard_at,
                    signal.heard_count,
                    signal.origin_state_id,
                    signal.color,
                    signal.satellite,
                    signal.updated_at,
                    json.dumps(signal.decoders),
                    signal.antenna,
                    signal.decoder_preset,
                    signal.id,
                ),
            )
            self._replace_tags(signal.id, signal.tag_ids)

    def delete(self, id: str) -> None:
        self.db.conn.execute("DELETE FROM signals WHERE id = ?", (id,))
        self.db.conn.commit()

    def _tag_ids(self, signal_id: str) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT tag_id FROM signal_tags WHERE signal_id = ?", (signal_id,)
        ).fetchall()
        return [row["tag_id"] for row in rows]

    def get(self, id: str) -> Signal | None:
        row = self.db.conn.execute("SELECT * FROM signals WHERE id = ?", (id,)).fetchone()
        return _signal_from_row(row, self._tag_ids(id)) if row else None

    def get_many(self, ids: list[str]) -> list[Signal]:
        """Like StateRepo.get_many: preserves `ids`' order, silently drops ids
        that don't exist -- used for GET /api/lists/{id}/items."""
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.conn.execute(
            f"SELECT * FROM signals WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        tags_by_id = self._tag_ids_many(list(by_id))
        return [_signal_from_row(by_id[i], tags_by_id.get(i, [])) for i in ids if i in by_id]

    def list(
        self, *, status: str | None = None, q: str | None = None, limit: int = 500
    ) -> list[Signal]:
        clauses = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if q is not None:
            clauses.append("(lower(name) LIKE ? OR lower(station) LIKE ?)")
            pattern = f"%{q.lower()}%"
            params.extend([pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.conn.execute(
            f"SELECT * FROM signals {where} ORDER BY created_at ASC, rowid ASC LIMIT ?", params
        ).fetchall()
        return [_signal_from_row(row, self._tag_ids(row["id"])) for row in rows]

    def query(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        tag_id: str | None = None,
        list_id: str | None = None,
        lo_hz: int | None = None,
        hi_hz: int | None = None,
        sort: str | None = None,
        order: str = "asc",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Signal], int]:
        """LIBRARY view: filtered/sorted/paged signals plus the total match
        count (for pagination), tags loaded in one extra query rather than
        one per row. `sort`/`order` are whitelisted here too (not just at the
        route) since this is a public repo method other callers could hit
        directly. `sort=None` means "no sort requested": plain "created", or
        (when `list_id` is also given) the list's own item order -- the
        route's default per the /api/library contract.
        """
        use_list_order = list_id is not None and sort is None
        sort = sort or "created"
        column = _LIBRARY_SORT_COLUMNS.get(sort)
        if column is None:
            raise ValueError(f"unknown sort: {sort!r}")
        if order not in ("asc", "desc"):
            raise ValueError(f"unknown order: {order!r}")

        join = ""
        join_params: list[object] = []
        if list_id is not None:
            join = "JOIN list_items li ON li.signal_id = signals.id AND li.list_id = ?"
            join_params.append(list_id)

        clauses = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if q:
            pattern = f"%{q.lower()}%"
            like = (
                "(lower(name) LIKE ? OR lower(station) LIKE ? OR lower(notes) LIKE ? "
                "OR lower(location) LIKE ?"
            )
            like_params = [pattern, pattern, pattern, pattern]
            freq = parse_frequency(q)
            if freq is not None:
                like += " OR frequency_hz = ?"
                like_params.append(freq)
            clauses.append(like + ")")
            params.extend(like_params)
        if tag_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM signal_tags st "
                "WHERE st.signal_id = signals.id AND st.tag_id = ?)"
            )
            params.append(tag_id)
        if lo_hz is not None:
            clauses.append("frequency_hz >= ?")
            params.append(lo_hz)
        if hi_hz is not None:
            clauses.append("frequency_hz <= ?")
            params.append(hi_hz)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        if use_list_order:
            # The list's own item order, not a signals column -- li is only
            # in scope when list_id (and thus the join) is set.
            order_by = "li.position ASC, id ASC"
        else:
            # `(col IS NULL)` sorts first so NULLs land last regardless of
            # direction (only last_heard_at can be NULL, but this is harmless
            # for the other columns); `id` breaks ties so paging is stable.
            order_by = f"({column} IS NULL), {column} {order.upper()}, id ASC"
        rows = self.db.conn.execute(
            f"SELECT signals.* FROM signals {join} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            [*join_params, *params, limit, offset],
        ).fetchall()
        total = self.db.conn.execute(
            f"SELECT COUNT(*) AS n FROM signals {join} {where}", [*join_params, *params]
        ).fetchone()["n"]

        tags_by_id = self._tag_ids_many([row["id"] for row in rows])
        signals = [_signal_from_row(row, tags_by_id.get(row["id"], [])) for row in rows]
        return signals, total

    def _tag_ids_many(self, signal_ids: list[str]) -> dict[str, list[str]]:
        if not signal_ids:
            return {}
        placeholders = ",".join("?" for _ in signal_ids)
        rows = self.db.conn.execute(
            f"SELECT signal_id, tag_id FROM signal_tags WHERE signal_id IN ({placeholders})",
            signal_ids,
        ).fetchall()
        out: dict[str, list[str]] = {sid: [] for sid in signal_ids}
        for row in rows:
            out[row["signal_id"]].append(row["tag_id"])
        return out

    def near(self, frequency_hz: int, radius_hz: int) -> list[Signal]:
        """Signals within radius_hz of frequency_hz, nearest first.

        Filtered in SQL (idx_signals_frequency_hz) rather than by scanning a
        LIMIT-ed page in Python, so an exact match in a large library can't
        fall off the end of the page before anyone looks at it.
        """
        rows = self.db.conn.execute(
            "SELECT * FROM signals WHERE frequency_hz BETWEEN ? AND ? "
            "ORDER BY abs(frequency_hz - ?) ASC, created_at ASC",
            (frequency_hz - radius_hz, frequency_hz + radius_hz, frequency_hz),
        ).fetchall()
        return [_signal_from_row(row, self._tag_ids(row["id"])) for row in rows]

    def search(self, q: str, limit: int = 1000) -> list[Signal]:
        """Signals matching `q` on name, station or tag name -- matched in SQL
        before LIMIT, so the limit caps the matches rather than the rows that
        get to be considered. When `q` parses as a frequency, signals on
        exactly that frequency are always included.
        """
        pattern = f"%{q.lower()}%"
        params: list[object] = [pattern, pattern, pattern]
        freq_clause = ""
        freq = parse_frequency(q)
        if freq is not None:
            freq_clause = " OR s.frequency_hz = ?"
            params.append(freq)
        params.append(limit)
        rows = self.db.conn.execute(
            "SELECT DISTINCT s.* FROM signals s "
            "LEFT JOIN signal_tags st ON st.signal_id = s.id "
            "LEFT JOIN tags t ON t.id = st.tag_id "
            "WHERE lower(s.name) LIKE ? OR lower(s.station) LIKE ? OR lower(t.name) LIKE ?"
            f"{freq_clause} ORDER BY s.created_at ASC LIMIT ?",
            params,
        ).fetchall()
        return [_signal_from_row(row, self._tag_ids(row["id"])) for row in rows]

    def inbox(self) -> list[Signal]:
        rows = self.db.conn.execute(
            "SELECT * FROM signals WHERE status = 'unknown' ORDER BY created_at DESC"
        ).fetchall()
        return [_signal_from_row(row, self._tag_ids(row["id"])) for row in rows]

    def recent(self, limit: int = 20) -> list[Signal]:
        rows = self.db.conn.execute(
            "SELECT * FROM signals ORDER BY last_heard_at DESC NULLS LAST LIMIT ?", (limit,)
        ).fetchall()
        return [_signal_from_row(row, self._tag_ids(row["id"])) for row in rows]

    def _replace_tags(self, signal_id: str, tag_ids: list[str]) -> None:
        # No commit here: callers (add/update, and set_tags below) own the transaction.
        conn = self.db.conn
        conn.execute("DELETE FROM signal_tags WHERE signal_id = ?", (signal_id,))
        conn.executemany(
            "INSERT INTO signal_tags (signal_id, tag_id) VALUES (?, ?)",
            [(signal_id, tag_id) for tag_id in tag_ids],
        )

    def set_tags(self, signal_id: str, tag_ids: list[str]) -> None:
        with self.db.conn:
            self._replace_tags(signal_id, tag_ids)


class ProfileRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, profile: ReceiverProfile) -> None:
        self.db.conn.execute(
            "INSERT INTO profiles (id, json, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                profile.id,
                profile.model_dump_json(),
                profile.name,
                profile.created_at,
                profile.updated_at,
            ),
        )
        self.db.conn.commit()

    def update(self, profile: ReceiverProfile) -> None:
        profile.updated_at = now_iso()
        self.db.conn.execute(
            "UPDATE profiles SET json=?, name=?, updated_at=? WHERE id=?",
            (profile.model_dump_json(), profile.name, profile.updated_at, profile.id),
        )
        self.db.conn.commit()

    def delete(self, id: str) -> None:
        self.db.conn.execute("DELETE FROM profiles WHERE id = ?", (id,))
        self.db.conn.commit()

    def get(self, id: str) -> ReceiverProfile | None:
        row = self.db.conn.execute("SELECT json FROM profiles WHERE id = ?", (id,)).fetchone()
        return ReceiverProfile.model_validate_json(row["json"]) if row else None

    def list(self) -> list[ReceiverProfile]:
        rows = self.db.conn.execute("SELECT json FROM profiles ORDER BY name").fetchall()
        return [ReceiverProfile.model_validate_json(row["json"]) for row in rows]


# Legacy category names -> current short codes. Shortwave and Broadcast both
# land on "bc": whichever is processed second (Broadcast, per this mapping's
# order) merges into the tag the first rename produced.
_LEGACY_CATEGORY_RENAMES = {
    "Favorites": "fav",
    "Satellites": "sat",
    "Aviation": "air",
    "Shortwave": "bc",
    "Amateur": "ham",
    "Marine": "marine",
    "Broadcast": "bc",
    "Digital": "digital",
    "Experiments": "exp",
    "Unidentified": "unid",
}


def _tag_from_row(row) -> Tag:
    return Tag(id=row["id"], name=row["name"], kind=row["kind"], color=row["color"])


class TagRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, tag: Tag) -> None:
        self.db.conn.execute(
            "INSERT INTO tags (id, name, kind, color) VALUES (?, ?, ?, ?)",
            (tag.id, tag.name, tag.kind, tag.color),
        )
        self.db.conn.commit()

    def update(self, tag: Tag) -> None:
        self.db.conn.execute(
            "UPDATE tags SET name=?, color=? WHERE id=?",
            (tag.name, tag.color, tag.id),
        )
        self.db.conn.commit()

    def list(self) -> list[Tag]:
        rows = self.db.conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
        return [_tag_from_row(row) for row in rows]

    def get(self, id: str) -> Tag | None:
        row = self.db.conn.execute("SELECT * FROM tags WHERE id = ?", (id,)).fetchone()
        return _tag_from_row(row) if row else None

    def get_by_name(self, name: str) -> Tag | None:
        row = self.db.conn.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
        return _tag_from_row(row) if row else None

    def ensure_categories(self) -> None:
        conn = self.db.conn
        with conn:  # renames, merges and inserts commit (or roll back) together
            for old_name, new_name in _LEGACY_CATEGORY_RENAMES.items():
                old = self.get_by_name(old_name)
                if old is None or old_name == new_name:
                    continue
                target = self.get_by_name(new_name)
                if target is None:
                    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, old.id))
                else:
                    # Merge: move old's signal_tags rows onto target (ignore
                    # duplicates), then drop the now-redundant old tag.
                    conn.execute(
                        "INSERT OR IGNORE INTO signal_tags (signal_id, tag_id) "
                        "SELECT signal_id, ? FROM signal_tags WHERE tag_id = ?",
                        (target.id, old.id),
                    )
                    conn.execute("DELETE FROM tags WHERE id = ?", (old.id,))

            for name, color in CATEGORY_TAGS:
                if self.get_by_name(name) is None:
                    conn.execute(
                        "INSERT INTO tags (id, name, kind, color) VALUES (?, ?, ?, ?)",
                        (uuid7(), name, "category", color),
                    )
                # Only fill in a color where one isn't already set (a legacy
                # row carried forward with color NULL, or a user's own custom
                # color on an existing category tag must survive untouched).
                conn.execute(
                    "UPDATE tags SET color = ? WHERE name = ? AND kind = 'category' "
                    "AND color IS NULL",
                    (color, name),
                )


class HistoryRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, stack: HistoryStack) -> None:
        conn = self.db.conn
        with conn:
            conn.execute("DELETE FROM history")
            conn.executemany(
                "INSERT INTO history (position, state_id) VALUES (?, ?)",
                [(i, state.id) for i, state in enumerate(stack.entries)],
            )
            conn.execute("DELETE FROM history_meta WHERE key = 'cursor'")
            conn.execute(
                "INSERT INTO history_meta (key, value) VALUES ('cursor', ?)",
                (str(stack.position),),
            )

    def load(self) -> tuple[list[str], int]:
        conn = self.db.conn
        rows = conn.execute("SELECT state_id FROM history ORDER BY position").fetchall()
        state_ids = [row["state_id"] for row in rows]
        cursor_row = conn.execute("SELECT value FROM history_meta WHERE key = 'cursor'").fetchone()
        cursor = int(cursor_row["value"]) if cursor_row else -1
        return state_ids, cursor


class JournalRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, entry: JournalEntry) -> None:
        self.db.conn.execute(
            "INSERT INTO journal_entries ("
            "id, at, kind, state_id, signal_id, profile_id, signal_power_db, signal_snr_db, note"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.at,
                entry.kind,
                entry.state_id,
                entry.signal_id,
                entry.profile_id,
                entry.signal_power_db,
                entry.signal_snr_db,
                entry.note,
            ),
        )
        self.db.conn.commit()

    def list_day(self, day: str) -> list[JournalEntry]:
        rows = self.db.conn.execute(
            "SELECT * FROM journal_entries WHERE substr(at, 1, 10) = ? ORDER BY at ASC", (day,)
        ).fetchall()
        return [
            JournalEntry(
                id=row["id"],
                at=row["at"],
                kind=row["kind"],
                state_id=row["state_id"],
                signal_id=row["signal_id"],
                profile_id=row["profile_id"],
                signal_power_db=row["signal_power_db"],
                signal_snr_db=row["signal_snr_db"],
                note=row["note"],
            )
            for row in rows
        ]

    def signal_entry_ids(self, signal_id: str) -> list[str]:
        """Entry ids naming `signal_id` -- merge_signals uses this to capture
        which entries it's about to repoint, so undo can point them back."""
        rows = self.db.conn.execute(
            "SELECT id FROM journal_entries WHERE signal_id = ?", (signal_id,)
        ).fetchall()
        return [row["id"] for row in rows]

    def repoint_signal(self, old_signal_id: str, new_signal_id: str) -> None:
        """Re-point every entry naming `old_signal_id` at `new_signal_id` --
        merge_signals calls this for each signal it deletes into the kept one."""
        self.db.conn.execute(
            "UPDATE journal_entries SET signal_id = ? WHERE signal_id = ?",
            (new_signal_id, old_signal_id),
        )
        self.db.conn.commit()

    def days(self, limit: int = 30) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT DISTINCT substr(at, 1, 10) AS day FROM journal_entries "
            "ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["day"] for row in rows]

    def heard_station_names(self) -> set[str]:
        """Distinct station identities (case-folded) that have ever appeared
        in the journal, joined through the signal each entry names -- LISTEN's
        "unheard" preset uses this to find catalog entries never logged."""
        rows = self.db.conn.execute(
            "SELECT DISTINCT lower(CASE WHEN s.station != '' THEN s.station ELSE s.name END) "
            "AS heard FROM journal_entries j JOIN signals s ON s.id = j.signal_id "
            "WHERE j.signal_id IS NOT NULL"
        ).fetchall()
        return {row["heard"] for row in rows if row["heard"]}

    def hour_of_day_counts(self, hour: int, *, exclude_day: str) -> dict[str, int]:
        """signal_id -> count of journal entries (any kind) at this UTC
        hour-of-day on days other than `exclude_day` (today), ordered by count
        descending -- LISTEN's "heard_this_hour" preset ranks signals by this."""
        rows = self.db.conn.execute(
            "SELECT signal_id, COUNT(*) AS n FROM journal_entries "
            "WHERE signal_id IS NOT NULL AND substr(at, 12, 2) = ? "
            "AND substr(at, 1, 10) != ? GROUP BY signal_id ORDER BY n DESC",
            (f"{hour:02d}", exclude_day),
        ).fetchall()
        return {row["signal_id"]: row["n"] for row in rows}


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: object = None) -> object:
        row = self.db.conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set(self, key: str, value: object) -> None:
        conn = self.db.conn
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (key, json.dumps(value)),
            )


# Undo log rows kept -- oldest are pruned on every record() beyond this count.
_LIBRARY_UNDO_KEEP = 50


class LibraryUndoRepo:
    """Undo log for LIBRARY edits: each row is a human label plus the
    before-image of every signal one operation touched, so the latest entry
    can be popped and replayed backwards."""

    def __init__(self, db: Database) -> None:
        self.db = db
        # Redo is session-scoped: the durable undo log remains authoritative
        # after a restart, while a new edit invalidates this forward history.
        self._redo: list[dict] = []

    def record(
        self,
        label: str,
        before: list[tuple[str, Signal | None]],
        *,
        journal_repoint: list[tuple[str, str]] | None = None,
    ) -> None:
        # None means "this signal didn't exist before the op" (a create) --
        # pop_and_restore() deletes it rather than upserting a row for it.
        # Each entry also captures that signal's *current* (pre-mutation)
        # list_items rows, so a caller that records before it mutates (every
        # caller does) gets those memberships back on undo for free -- a
        # deleted signal cascades its list_items, and a list add/remove is
        # itself nothing but a list_items change with the signal row untouched.
        conn = self.db.conn
        entries = []
        for signal_id, signal in before:
            rows = conn.execute(
                "SELECT list_id, position, skip FROM list_items WHERE signal_id = ?",
                (signal_id,),
            ).fetchall()
            memberships = [[row["list_id"], row["position"], row["skip"]] for row in rows]
            entries.append(
                [
                    signal_id,
                    signal.model_dump(mode="json") if signal is not None else None,
                    memberships,
                ]
            )
        payload = json.dumps(
            {
                "signals": entries,
                # merge_signals only: entries whose signal_id it repointed
                # away from old_signal_id, restored on undo.
                "journal_repoint": [list(pair) for pair in (journal_repoint or [])],
            }
        )
        with conn:
            conn.execute(
                "INSERT INTO library_undo (id, created_at, label, payload) VALUES (?, ?, ?, ?)",
                (uuid7(), now_iso(), label, payload),
            )
            conn.execute(
                "DELETE FROM library_undo WHERE id NOT IN ("
                "SELECT id FROM library_undo ORDER BY created_at DESC, id DESC LIMIT ?)",
                (_LIBRARY_UNDO_KEEP,),
            )
        self._redo.clear()

    def latest(self) -> dict | None:
        row = self.db.conn.execute(
            "SELECT id, label, created_at FROM library_undo "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "label": row["label"], "created_at": row["created_at"]}

    def latest_redo(self) -> dict | None:
        if not self._redo:
            return None
        row = self._redo[-1]["row"]
        return {"id": row["id"], "label": row["label"]}

    def _snapshot(self, raw: dict | list, signal_repo: SignalRepo) -> dict:
        entries = raw if isinstance(raw, list) else raw["signals"]
        journal_repoint = [] if isinstance(raw, list) else raw.get("journal_repoint", [])
        conn = self.db.conn
        signals = []
        for entry in entries:
            signal_id = entry[0]
            signal = signal_repo.get(signal_id)
            memberships = [
                [row["list_id"], row["position"], row["skip"]]
                for row in conn.execute(
                    "SELECT list_id, position, skip FROM list_items WHERE signal_id = ?",
                    (signal_id,),
                ).fetchall()
            ]
            signals.append([
                signal_id,
                signal.model_dump(mode="json") if signal is not None else None,
                memberships,
            ])
        journal = []
        for entry_id, _ in journal_repoint:
            row = conn.execute(
                "SELECT signal_id FROM journal_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is not None:
                journal.append([entry_id, row["signal_id"]])
        return {"signals": signals, "journal_repoint": journal}

    def _restore_payload(self, raw: dict | list, signal_repo: SignalRepo) -> None:
        # Payloads written before list undo support are bare [id, before]
        # pairs. Their missing memberships mean "leave empty" as before.
        if isinstance(raw, list):
            entries, journal_repoint = raw, []
        else:
            entries, journal_repoint = raw["signals"], raw.get("journal_repoint", [])
        conn = self.db.conn
        live_list_ids = {
            row["id"] for row in conn.execute("SELECT id FROM lists").fetchall()
        }
        for entry in entries:
            signal_id, before = entry[0], entry[1]
            memberships = entry[2] if len(entry) > 2 else None
            if before is None:
                conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
                continue
            signal = Signal.model_validate(before)
            conn.execute("DELETE FROM signals WHERE id = ?", (signal.id,))
            conn.execute(_SIGNAL_INSERT_SQL, _signal_insert_values(signal))
            signal_repo._replace_tags(signal.id, signal.tag_ids)
            if memberships is not None:
                conn.executemany(
                    "INSERT INTO list_items (list_id, signal_id, position, skip, added_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (m[0], signal.id, m[1], m[2] if len(m) > 2 else 0, now_iso())
                        for m in memberships if m[0] in live_list_ids
                    ],
                )
        for entry_id, signal_id in journal_repoint:
            conn.execute(
                "UPDATE journal_entries SET signal_id = ? WHERE id = ?",
                (signal_id, entry_id),
            )

    def pop_and_restore(self, signal_repo: SignalRepo) -> str:
        """Restore the latest undo entry's before-images and drop it. Returns
        its label. Raises ValueError if the log is empty (callers with an
        HTTP contract should check latest() first and 409 instead)."""
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM library_undo ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("no undo entries")
        raw = json.loads(row["payload"])
        with conn:  # every restore plus dropping this row commit (or roll back) together
            redo_payload = self._snapshot(raw, signal_repo)
            self._restore_payload(raw, signal_repo)
            undone_payload = self._snapshot(raw, signal_repo)
            conn.execute("DELETE FROM library_undo WHERE id = ?", (row["id"],))
        self._redo.append({"row": dict(row), "payload": redo_payload, "undone": undone_payload})
        return row["label"]

    def redo(self, signal_repo: SignalRepo, undo_id: str) -> str:
        if not self._redo or self._redo[-1]["row"]["id"] != undo_id:
            raise ValueError("nothing to redo")
        item = self._redo[-1]
        row = item["row"]
        with self.db.conn:
            # Refuse to overwrite an unrelated edit made after Undo, including
            # one that bypassed the Library undo recorder.
            if self._snapshot(json.loads(row["payload"]), signal_repo) != item["undone"]:
                raise ValueError("library changed since undo; redo is no longer safe")
            self._restore_payload(item["payload"], signal_repo)
            self.db.conn.execute(
                "INSERT INTO library_undo (id, created_at, label, payload) VALUES (?, ?, ?, ?)",
                (row["id"], row["created_at"], row["label"], row["payload"]),
            )
        self._redo.pop()
        return row["label"]


class ListRepo:
    """CRUD for user lists (e.g. built-in "Favorites") and their signal
    memberships (list_items), ordered by an explicit `position` column."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def all(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT l.id, l.name, l.builtin, l.position, "
            "(SELECT COUNT(*) FROM list_items li WHERE li.list_id = l.id) AS count "
            "FROM lists l ORDER BY l.position"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "builtin": bool(row["builtin"]),
                "position": row["position"],
                "count": row["count"],
            }
            for row in rows
        ]

    def get(self, list_id: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "builtin": bool(row["builtin"]),
            "position": row["position"],
        }

    def get_by_name(self, name: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT id FROM lists WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        return self.get(row["id"]) if row else None

    def create(self, name: str) -> dict:
        conn = self.db.conn
        list_id = uuid7()
        with conn:
            next_position = conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM lists"
            ).fetchone()["p"]
            conn.execute(
                "INSERT INTO lists (id, name, position, builtin, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (list_id, name, next_position, now_iso()),
            )
        result = self.get(list_id)
        assert result is not None
        return result

    def update(self, list_id: str, *, name: str | None = None, position: int | None = None) -> dict:
        conn = self.db.conn
        with conn:
            if name is not None:
                conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name, list_id))
            if position is not None:
                conn.execute("UPDATE lists SET position = ? WHERE id = ?", (position, list_id))
        result = self.get(list_id)
        assert result is not None
        return result

    def delete(self, list_id: str) -> None:
        self.db.conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        self.db.conn.commit()

    def item_ids(self, list_id: str) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT signal_id FROM list_items WHERE list_id = ? ORDER BY position", (list_id,)
        ).fetchall()
        return [row["signal_id"] for row in rows]

    def item_rows(self, list_id: str) -> list[dict]:
        """Ordered list items with their `skip` flag -- the scanner's list
        source (v0.12.0) needs this since item_ids() alone can't say which
        items to leave out of the sweep."""
        rows = self.db.conn.execute(
            "SELECT signal_id, skip FROM list_items WHERE list_id = ? ORDER BY position",
            (list_id,),
        ).fetchall()
        return [{"signal_id": row["signal_id"], "skip": bool(row["skip"])} for row in rows]

    def set_item_skip(self, list_id: str, signal_id: str, skip: bool) -> bool:
        """Returns False if `signal_id` isn't a member of `list_id` (route
        maps that to 404)."""
        conn = self.db.conn
        cur = conn.execute(
            "UPDATE list_items SET skip = ? WHERE list_id = ? AND signal_id = ?",
            (1 if skip else 0, list_id, signal_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def add_items(self, list_id: str, signal_ids: list[str]) -> int:
        """Append `signal_ids` not already in the list, in the given order
        (duplicates within `signal_ids` are collapsed). Returns count added."""
        conn = self.db.conn
        existing = set(self.item_ids(list_id))
        with conn:
            next_position = conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) AS p FROM list_items WHERE list_id = ?",
                (list_id,),
            ).fetchone()["p"]
            added = 0
            for signal_id in signal_ids:
                if signal_id in existing:
                    continue
                conn.execute(
                    "INSERT INTO list_items (list_id, signal_id, position, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (list_id, signal_id, next_position, now_iso()),
                )
                existing.add(signal_id)
                next_position += 1
                added += 1
        return added

    def remove_items(self, list_id: str, signal_ids: list[str]) -> int:
        if not signal_ids:
            return 0
        placeholders = ",".join("?" for _ in signal_ids)
        conn = self.db.conn
        cur = conn.execute(
            f"DELETE FROM list_items WHERE list_id = ? AND signal_id IN ({placeholders})",
            [list_id, *signal_ids],
        )
        conn.commit()
        return cur.rowcount

    def set_order(self, list_id: str, signal_ids: list[str]) -> None:
        """Reassign 0..n-1 positions per `signal_ids`' order. Caller (the
        route/service) is responsible for checking it's a permutation of the
        list's current items first."""
        conn = self.db.conn
        with conn:
            for position, signal_id in enumerate(signal_ids):
                conn.execute(
                    "UPDATE list_items SET position = ? WHERE list_id = ? AND signal_id = ?",
                    (position, list_id, signal_id),
                )

    def list_ids_for(self, signal_ids: list[str]) -> dict[str, list[str]]:
        """signal_id -> the ids of every list it belongs to -- LIBRARY (and
        other signal-listing views) attach this in one batched query."""
        if not signal_ids:
            return {}
        placeholders = ",".join("?" for _ in signal_ids)
        rows = self.db.conn.execute(
            f"SELECT signal_id, list_id FROM list_items WHERE signal_id IN ({placeholders})",
            signal_ids,
        ).fetchall()
        out: dict[str, list[str]] = {sid: [] for sid in signal_ids}
        for row in rows:
            out[row["signal_id"]].append(row["list_id"])
        return out

    def memberships_for(self, signal_id: str) -> list[tuple[str, int]]:
        rows = self.db.conn.execute(
            "SELECT list_id, position FROM list_items WHERE signal_id = ?", (signal_id,)
        ).fetchall()
        return [(row["list_id"], row["position"]) for row in rows]

    def set_memberships(self, signal_id: str, memberships: dict[str, int]) -> None:
        """Replace `signal_id`'s list memberships wholesale -- merge_signals
        uses this to give the kept signal the union of both sides' lists."""
        conn = self.db.conn
        with conn:
            conn.execute("DELETE FROM list_items WHERE signal_id = ?", (signal_id,))
            conn.executemany(
                "INSERT INTO list_items (list_id, signal_id, position, added_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (list_id, signal_id, position, now_iso())
                    for list_id, position in memberships.items()
                ],
            )


class SavedSearchRepo:
    """Named, reusable LIBRARY queries (GET /api/library's own params)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT id, name, params, created_at FROM saved_searches ORDER BY created_at"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "params": json.loads(row["params"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create(self, name: str, params: dict) -> dict:
        saved_id = uuid7()
        created_at = now_iso()
        conn = self.db.conn
        conn.execute(
            "INSERT INTO saved_searches (id, name, params, created_at) VALUES (?, ?, ?, ?)",
            (saved_id, name, json.dumps(params), created_at),
        )
        conn.commit()
        return {"id": saved_id, "name": name, "params": params, "created_at": created_at}

    def delete(self, id: str) -> None:
        self.db.conn.execute("DELETE FROM saved_searches WHERE id = ?", (id,))
        self.db.conn.commit()


# S4 integrity/provenance columns (013_decode_integrity.sql), same names on
# the DecodeEvent dataclass and in the returned row dict.
_DECODE_PROVENANCE_KEYS = (
    "integrity",
    "integrity_note",
    "source_id",
    "generation",
    "sample_index",
    "run_index",
    "analysis_run_id",
)


def _decode_event_from_row(row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "frequency_hz": row["frequency_hz"],
        "signal_id": row["signal_id"],
        "decoder": row["decoder"],
        "kind": row["kind"],
        "text": row["text"],
        "fields": json.loads(row["fields_json"]) if row["fields_json"] else {},
        "image_path": row["image_path"],
        # S4 (migration 013): None on rows stored before integrity existed
        # (or read through a pre-013 schema, e.g. a legacy-upgrade fixture).
        **{key: (row[key] if key in row.keys() else None) for key in _DECODE_PROVENANCE_KEYS},
    }


class DecodeEventRepo:
    """History of decoded messages (POCSAG/FLEX/EAS/APRS/DTMF/CW/RTTY, P1).
    `add()` accepts anything with the `DecodeEvent` dataclass's attributes
    (msdr.decoder.backends.DecodeEvent, or a test double) -- this module
    doesn't import msdr.decoder (optional package, see msdr/api/service.py's
    import guard)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._provenance: tuple[int, tuple[str, ...]] | None = None

    def _provenance_columns(self) -> tuple[str, ...]:
        """Available provenance columns, including older upgrade fixtures.
        Cached per schema_version (a header read, cheap per add()) so a
        migration applied after this repo's first use is picked up."""
        version = self.db.conn.execute("PRAGMA schema_version").fetchone()[0]
        if self._provenance is None or self._provenance[0] != version:
            cols = {r["name"] for r in self.db.conn.execute("PRAGMA table_info(decode_events)")}
            self._provenance = (
                version, tuple(key for key in _DECODE_PROVENANCE_KEYS if key in cols)
            )
        return self._provenance[1]

    def add(self, event) -> dict:
        row = {
            "id": uuid7(),
            "ts": event.ts,
            "frequency_hz": getattr(event, "frequency_hz", None),
            "signal_id": getattr(event, "signal_id", None),
            "decoder": event.decoder,
            "kind": event.kind,
            "text": event.text,
            "fields": dict(getattr(event, "fields", None) or {}),
            "image_path": getattr(event, "image_path", None),
            # A test double without the S4 attributes stores the dataclass
            # default ("unknown"), never an invented "passed".
            "integrity": getattr(event, "integrity", "unknown"),
            "integrity_note": getattr(event, "integrity_note", ""),
            "source_id": getattr(event, "source_id", None),
            "generation": getattr(event, "generation", None),
            "sample_index": getattr(event, "sample_index", None),
            "run_index": getattr(event, "run_index", None),
            "analysis_run_id": getattr(event, "analysis_run_id", None),
        }
        columns = ["id", "ts", "frequency_hz", "signal_id", "decoder", "kind", "text"]
        values = [row[c] for c in columns]
        columns += ["fields_json", "image_path"]
        values += [json.dumps(row["fields"]), row["image_path"]]
        provenance = self._provenance_columns()
        columns += list(provenance)
        values += [row[key] for key in provenance]
        self.db.conn.execute(
            f"INSERT INTO decode_events ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        self.db.conn.commit()
        return row

    def list(
        self,
        *,
        limit: int = 200,
        decoder: str | None = None,
        q: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        clauses = []
        params: list[object] = []
        if decoder is not None:
            clauses.append("decoder = ?")
            params.append(decoder)
        if q is not None:
            clauses.append("text LIKE ?")
            params.append(f"%{q}%")
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.conn.execute(
            f"SELECT * FROM decode_events {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
        return [_decode_event_from_row(row) for row in rows]

    def get(self, event_id: str) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM decode_events WHERE id = ?", (event_id,)
        ).fetchone()
        return _decode_event_from_row(row) if row is not None else None

    def update_event(
        self, event_id: str, *, text: str | None = None, fields: dict | None = None
    ) -> dict | None:
        """Merge `fields` into the stored fields_json (keys not mentioned are
        kept) and optionally replace `text` -- wefax (S7-S10 round 1) uses
        this to update the "fax started" event in place as lines arrive and
        again when the image finishes. Returns the updated row, or None if
        `event_id` doesn't exist."""
        existing = self.get(event_id)
        if existing is None:
            return None
        merged_fields = dict(existing["fields"])
        if fields:
            merged_fields.update(fields)
        new_text = existing["text"] if text is None else text
        self.db.conn.execute(
            "UPDATE decode_events SET text = ?, fields_json = ? WHERE id = ?",
            (new_text, json.dumps(merged_fields), event_id),
        )
        self.db.conn.commit()
        return self.get(event_id)

    def count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) AS n FROM decode_events").fetchone()["n"]

    def trim(self, limit: int) -> int:
        """Delete the oldest rows beyond `limit`. Returns the number deleted."""
        conn = self.db.conn
        excess = self.count() - limit
        if excess <= 0:
            return 0
        conn.execute(
            "DELETE FROM decode_events WHERE id IN "
            "(SELECT id FROM decode_events ORDER BY ts ASC LIMIT ?)",
            (excess,),
        )
        conn.commit()
        return excess

    def clear(self) -> int:
        conn = self.db.conn
        n = self.count()
        conn.execute("DELETE FROM decode_events")
        conn.commit()
        return n


def _beacon_hit_from_row(row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "beacon": row["beacon"],
        "frequency_hz": row["frequency_hz"],
        "snr_db": row["snr_db"],
        "decoded": bool(row["decoded"]),
        "probable": bool(row["probable"]),
        "mode": row["mode"],
    }


class BeaconHitRepo:
    """Beacon monitor session hits (S10): one row per recorded slot (SNR
    threshold or CW-decoded call-sign match) -- see msdr.survey.beacons's
    BeaconMonitor, this feature's engine."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, hit: dict) -> dict:
        row = {
            "id": uuid7(),
            "ts": hit["ts"],
            "beacon": hit["beacon"],
            "frequency_hz": hit["frequency_hz"],
            "snr_db": hit.get("snr_db"),
            "decoded": bool(hit.get("decoded", False)),
            "probable": bool(hit.get("probable", False)),
            "mode": hit["mode"],
        }
        self.db.conn.execute(
            "INSERT INTO beacon_hits "
            "(id, ts, beacon, frequency_hz, snr_db, decoded, probable, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["ts"],
                row["beacon"],
                row["frequency_hz"],
                row["snr_db"],
                int(row["decoded"]),
                int(row["probable"]),
                row["mode"],
            ),
        )
        self.db.conn.commit()
        return row

    def list(
        self,
        *,
        limit: int = 200,
        since: float | None = None,
        beacon: str | None = None,
        frequency_hz: int | None = None,
    ) -> list[dict]:
        clauses = []
        params: list[object] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if beacon is not None:
            clauses.append("beacon = ?")
            params.append(beacon)
        if frequency_hz is not None:
            clauses.append("frequency_hz = ?")
            params.append(frequency_hz)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.conn.execute(
            f"SELECT * FROM beacon_hits {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
        return [_beacon_hit_from_row(row) for row in rows]

    def summary(self) -> list[dict]:
        """Per beacon x band: last_heard, last_snr_db (from that latest hit),
        decoded (whether any hit for the pair was CW-confirmed) and hits
        count -- the BEACONS grid's own per-cell data (msdr.survey.beacons's
        status()["summary"])."""
        rows = self.db.conn.execute(
            "SELECT beacon, frequency_hz, MAX(ts) AS last_heard, COUNT(*) AS hits, "
            "MAX(decoded) AS decoded FROM beacon_hits "
            "GROUP BY beacon, frequency_hz ORDER BY beacon, frequency_hz"
        ).fetchall()
        summaries = []
        for row in rows:
            latest = self.db.conn.execute(
                "SELECT snr_db FROM beacon_hits WHERE beacon = ? AND frequency_hz = ? "
                "ORDER BY ts DESC LIMIT 1",
                (row["beacon"], row["frequency_hz"]),
            ).fetchone()
            summaries.append(
                {
                    "beacon": row["beacon"],
                    "band_hz": row["frequency_hz"],
                    "last_heard": row["last_heard"],
                    "last_snr_db": latest["snr_db"] if latest else None,
                    "decoded": bool(row["decoded"]),
                    "hits": row["hits"],
                }
            )
        return summaries

    def clear(self) -> int:
        conn = self.db.conn
        n = conn.execute("SELECT COUNT(*) AS n FROM beacon_hits").fetchone()["n"]
        conn.execute("DELETE FROM beacon_hits")
        conn.commit()
        return n


def _find_from_row(row) -> dict:
    return {
        "id": row["id"],
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "seen_count": row["seen_count"],
        "frequency_hz": row["frequency_hz"],
        "label": row["label"],
        "signal_id": row["signal_id"],
        "peak_snr_db": row["peak_snr_db"],
        "last_duration_s": row["last_duration_s"],
        "demodulator": row["demodulator"],
        "bandwidth_hz": row["bandwidth_hz"],
        "classification": (
            json.loads(row["classification_json"]) if row["classification_json"] else None
        ),
        "status": row["status"],
        "note": row["note"],
    }


class FindRepo:
    """Scanner finds log (S11): one row per (roughly) distinct frequency seen
    across scan runs with `log_finds` enabled, merged within a tolerance --
    see msdr.engine.scanner's on_find hook and
    docs/superpowers/plans/2026-09-25-scanner-finds.md."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add_or_merge(self, find: dict, tolerance_hz: int = 6000) -> tuple[dict, bool]:
        """Insert `find` (the on_find payload: ts, frequency_hz, label,
        signal_id, peak_snr_db, duration_s, demodulator, bandwidth_hz,
        classification) as a new row, or merge it into the closest existing
        row within `tolerance_hz`. Returns (row, created)."""
        conn = self.db.conn
        freq = find["frequency_hz"]
        existing_row = conn.execute(
            "SELECT * FROM finds WHERE frequency_hz BETWEEN ? AND ? "
            "ORDER BY ABS(frequency_hz - ?) LIMIT 1",
            (freq - tolerance_hz, freq + tolerance_hz, freq),
        ).fetchone()

        if existing_row is None:
            row = {
                "id": uuid7(),
                "first_ts": find["ts"],
                "last_ts": find["ts"],
                "seen_count": 1,
                "frequency_hz": freq,
                "label": find.get("label"),
                "signal_id": find.get("signal_id"),
                "peak_snr_db": find.get("peak_snr_db"),
                "last_duration_s": find.get("duration_s"),
                "demodulator": find.get("demodulator"),
                "bandwidth_hz": find.get("bandwidth_hz"),
                "classification": find.get("classification"),
                "status": "new",
                "note": None,
            }
            classification_json = (
                json.dumps(row["classification"]) if row["classification"] is not None else None
            )
            conn.execute(
                "INSERT INTO finds (id, first_ts, last_ts, seen_count, frequency_hz, label, "
                "signal_id, peak_snr_db, last_duration_s, demodulator, bandwidth_hz, "
                "classification_json, status, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["first_ts"],
                    row["last_ts"],
                    row["seen_count"],
                    row["frequency_hz"],
                    row["label"],
                    row["signal_id"],
                    row["peak_snr_db"],
                    row["last_duration_s"],
                    row["demodulator"],
                    row["bandwidth_hz"],
                    classification_json,
                    row["status"],
                    row["note"],
                ),
            )
            conn.commit()
            return row, True

        existing = _find_from_row(existing_row)
        merged_classification = dict(existing["classification"] or {})
        for key, value in (find.get("classification") or {}).items():
            if value is not None:
                merged_classification[key] = value
        snr_candidates = [
            v for v in (existing["peak_snr_db"], find.get("peak_snr_db")) if v is not None
        ]

        def _latest(key: str):
            value = find.get(key)
            return value if value is not None else existing[key]

        updated = {
            "last_ts": find["ts"],
            "seen_count": existing["seen_count"] + 1,
            "label": _latest("label"),
            "signal_id": _latest("signal_id"),
            "peak_snr_db": max(snr_candidates) if snr_candidates else None,
            "last_duration_s": find.get("duration_s"),
            "demodulator": _latest("demodulator"),
            "bandwidth_hz": _latest("bandwidth_hz"),
            "classification": merged_classification or None,
        }
        classification_json = (
            json.dumps(updated["classification"]) if updated["classification"] is not None else None
        )
        conn.execute(
            "UPDATE finds SET last_ts = ?, seen_count = ?, label = ?, signal_id = ?, "
            "peak_snr_db = ?, last_duration_s = ?, demodulator = ?, bandwidth_hz = ?, "
            "classification_json = ? WHERE id = ?",
            (
                updated["last_ts"],
                updated["seen_count"],
                updated["label"],
                updated["signal_id"],
                updated["peak_snr_db"],
                updated["last_duration_s"],
                updated["demodulator"],
                updated["bandwidth_hz"],
                classification_json,
                existing["id"],
            ),
        )
        conn.commit()
        return self.get(existing["id"]), False

    def list(
        self, *, status: str | None = None, limit: int = 200, q: str | None = None
    ) -> list[dict]:
        clauses = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if q is not None:
            clauses.append("label LIKE ?")
            params.append(f"%{q}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.conn.execute(
            f"SELECT * FROM finds {where} ORDER BY last_ts DESC LIMIT ?", params
        ).fetchall()
        return [_find_from_row(row) for row in rows]

    def get(self, find_id: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM finds WHERE id = ?", (find_id,)).fetchone()
        return _find_from_row(row) if row is not None else None

    def set_status(
        self, find_id: str, status: str, note: str | None = None, signal_id: str | None = None
    ) -> dict | None:
        """`signal_id` is set on promote (find -> saved); omitted otherwise,
        in which case the existing value is kept."""
        existing = self.get(find_id)
        if existing is None:
            return None
        new_note = existing["note"] if note is None else note
        new_signal_id = existing["signal_id"] if signal_id is None else signal_id
        self.db.conn.execute(
            "UPDATE finds SET status = ?, note = ?, signal_id = ? WHERE id = ?",
            (status, new_note, new_signal_id, find_id),
        )
        self.db.conn.commit()
        return self.get(find_id)

    def delete(self, find_id: str) -> bool:
        conn = self.db.conn
        existing = conn.execute("SELECT id FROM finds WHERE id = ?", (find_id,)).fetchone()
        if existing is None:
            return False
        conn.execute("DELETE FROM finds WHERE id = ?", (find_id,))
        conn.commit()
        return True

    def ignored_frequencies(self) -> list[int]:
        rows = self.db.conn.execute(
            "SELECT frequency_hz FROM finds WHERE status = 'ignored'"
        ).fetchall()
        return [row["frequency_hz"] for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM finds GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}
