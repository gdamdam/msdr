"""Library-only JSON merge for device-to-device transfer.

For complete evidence/history backup and verified restore, use storage.backup.

Merge policy on import: signals and profiles merge by id, newer `updated_at`
wins (equal timestamps compare as ISO strings and count as `skipped`); tags
merge by id then by name; states and journal entries are content/id-addressed
and are inserted only if absent. Lists merge by id then by name (case-
insensitive); list memberships and saved searches are inserted only if absent.
Exports from before lists existed simply lack those keys and import as before.
Device labels (Wi-Fi/BLE) merge by (receiver, ident), newer `updated_at` wins;
invalid rows are skipped, not fatal. Older exports lack the key.
"""

import json

from pydantic import BaseModel

from msdr.engine.state import ReceiverState
from msdr.history.labels import CLASSES, LabelError, normalize_ident
from msdr.library.models import ReceiverProfile, Signal, Tag, now_iso
from msdr.storage.db import Database
from msdr.storage.repos import (
    JournalEntry,
    ProfileRepo,
    SignalRepo,
    StateRepo,
    TagRepo,
)

FORMAT = "msdr/1"

# Public API "list all" isn't in the repo interfaces for states/journal
# entries (they're accessed day-scoped / by id elsewhere), so export reads
# those two tables directly; signals/profiles/tags already expose list().
_ALL_SIGNALS_LIMIT = 1_000_000


def export_all(db: Database) -> dict:
    conn = db.conn
    profiles = ProfileRepo(db).list()
    tags = TagRepo(db).list()
    signals = SignalRepo(db).list(limit=_ALL_SIGNALS_LIMIT)

    state_rows = conn.execute("SELECT json FROM receiver_states ORDER BY captured_at").fetchall()
    states = [ReceiverState.model_validate_json(row["json"]) for row in state_rows]

    journal_rows = conn.execute("SELECT * FROM journal_entries ORDER BY at ASC").fetchall()
    journal_entries = [_journal_entry_from_row(row) for row in journal_rows]

    # Lists and saved searches are plain rows (no pydantic model), exported
    # as-is so a backup round-trip keeps them.
    lists = [dict(r) for r in conn.execute("SELECT * FROM lists ORDER BY position").fetchall()]
    list_items = [
        dict(r)
        for r in conn.execute("SELECT * FROM list_items ORDER BY list_id, position").fetchall()
    ]
    saved_searches = [
        {**dict(r), "params": json.loads(r["params"])}
        for r in conn.execute("SELECT * FROM saved_searches ORDER BY created_at").fetchall()
    ]

    labels = [
        dict(r) for r in conn.execute("SELECT * FROM device_labels ORDER BY receiver, ident")
    ]

    return {
        "format": FORMAT,
        "exported_at": now_iso(),
        "lists": lists,
        "list_items": list_items,
        "saved_searches": saved_searches,
        "profiles": [p.model_dump() for p in profiles],
        "tags": [t.model_dump() for t in tags],
        "signals": [s.model_dump() for s in signals],
        "receiver_states": [s.model_dump() for s in states],
        "journal_entries": [e.model_dump() for e in journal_entries],
        "device_labels": labels,
    }


def _journal_entry_from_row(row) -> JournalEntry:
    return JournalEntry(
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


def _journal_entry_exists(db: Database, id: str) -> bool:
    row = db.conn.execute("SELECT 1 FROM journal_entries WHERE id = ?", (id,)).fetchone()
    return row is not None


# The helpers below (_add_* / _replace_*) all write through `db.conn` without
# calling `.commit()` themselves -- unlike the repo methods they mirror, whose
# per-call commits would otherwise make a mid-import failure leave earlier
# writes in this same import durable. `import_all` owns the one transaction
# and commits (or rolls back) exactly once, at the end.


def _add_tag(db: Database, tag: Tag) -> None:
    db.conn.execute(
        "INSERT INTO tags (id, name, kind, color) VALUES (?, ?, ?, ?)",
        (tag.id, tag.name, tag.kind, tag.color),
    )


def _add_profile(db: Database, profile: ReceiverProfile) -> None:
    db.conn.execute(
        "INSERT INTO profiles (id, json, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            profile.id,
            profile.model_dump_json(),
            profile.name,
            profile.created_at,
            profile.updated_at,
        ),
    )


def _replace_profile(db: Database, profile: ReceiverProfile) -> None:
    # Not ProfileRepo.update(): import must keep the imported updated_at
    # exactly (it's already been compared and won), not stamp "now".
    db.conn.execute(
        "UPDATE profiles SET json=?, name=?, updated_at=? WHERE id=?",
        (profile.model_dump_json(), profile.name, profile.updated_at, profile.id),
    )


def _add_state(db: Database, state: ReceiverState) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO receiver_states (id, json, captured_at, source) "
        "VALUES (?, ?, ?, ?)",
        (state.id, state.model_dump_json(), state.captured_at, state.source),
    )


def _replace_signal_tags(db: Database, signal_id: str, tag_ids: list[str]) -> None:
    conn = db.conn
    conn.execute("DELETE FROM signal_tags WHERE signal_id = ?", (signal_id,))
    conn.executemany(
        "INSERT INTO signal_tags (signal_id, tag_id) VALUES (?, ?)",
        [(signal_id, tag_id) for tag_id in tag_ids],
    )


def _add_signal(db: Database, signal: Signal) -> None:
    db.conn.execute(
        "INSERT INTO signals ("
        "id, name, status, frequency_hz, demodulator, filter_bandwidth_hz, profile_id, "
        "station, location, latitude, longitude, urls_json, notes, first_heard_at, last_heard_at, "
        "heard_count, origin_state_id, color, satellite, created_at, updated_at, decoders_json, "
        "antenna"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
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
        ),
    )
    _replace_signal_tags(db, signal.id, signal.tag_ids)


def _replace_signal(db: Database, signal: Signal) -> None:
    # Not SignalRepo.update(): same reason as _replace_profile above.
    db.conn.execute(
        "UPDATE signals SET name=?, status=?, frequency_hz=?, demodulator=?, "
        "filter_bandwidth_hz=?, profile_id=?, station=?, location=?, latitude=?, "
        "longitude=?, urls_json=?, "
        "notes=?, first_heard_at=?, last_heard_at=?, heard_count=?, origin_state_id=?, "
        "color=?, satellite=?, updated_at=?, decoders_json=?, antenna=? WHERE id=?",
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
            signal.id,
        ),
    )
    _replace_signal_tags(db, signal.id, signal.tag_ids)


def _add_journal_entry(db: Database, entry: JournalEntry) -> None:
    db.conn.execute(
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


def _remap_tag_ids(
    tag_ids: list[str], id_remap: dict[str, str], known_ids: set[str]
) -> list[str]:
    # Rewrite each incoming signal's tag_ids through the id_remap built while
    # merging tags (source id -> local id for tags that matched by name), and
    # drop anything that still doesn't resolve to a local tag -- otherwise the
    # signal_tags FK insert below raises IntegrityError (F1).
    remapped = (id_remap.get(tag_id, tag_id) for tag_id in tag_ids)
    return [tag_id for tag_id in remapped if tag_id in known_ids]


class ImportReport(BaseModel):
    added: dict[str, int] = {}
    updated: dict[str, int] = {}
    skipped: dict[str, int] = {}


def _import_lists(conn, doc: dict, added: dict[str, int], skipped: dict[str, int]) -> None:
    """Lists merge by id, then by name (case-insensitive) -- the built-in
    "favorites" list shares its id across installs, a user list recreated
    on another machine only shares its name. Memberships are remapped through
    that match and inserted only if absent (appended after the local list's
    items); rows naming a signal that isn't in the library are dropped.
    Saved searches are inserted only if absent, their `list` param remapped."""
    by_id = {r["id"]: r for r in conn.execute("SELECT id, name FROM lists").fetchall()}
    by_name = {r["name"].lower(): r["id"] for r in by_id.values()}
    remap: dict[str, str] = {}
    for raw in doc.get("lists", []):
        if raw["id"] in by_id:
            remap[raw["id"]] = raw["id"]
            _bump(skipped, "lists")
            continue
        local = by_name.get(raw["name"].lower())
        if local is not None:
            remap[raw["id"]] = local
            _bump(skipped, "lists")
            continue
        position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM lists").fetchone()[0]
        conn.execute(
            "INSERT INTO lists (id, name, position, builtin, created_at) VALUES (?, ?, ?, 0, ?)",
            (raw["id"], raw["name"], position, raw.get("created_at") or now_iso()),
        )
        by_id[raw["id"]] = raw
        by_name[raw["name"].lower()] = raw["id"]
        remap[raw["id"]] = raw["id"]
        _bump(added, "lists")

    for raw in doc.get("list_items", []):
        list_id = remap.get(raw["list_id"])
        exists = conn.execute("SELECT 1 FROM signals WHERE id = ?", (raw["signal_id"],)).fetchone()
        if list_id is None or exists is None:
            _bump(skipped, "list_items")
            continue
        present = conn.execute(
            "SELECT 1 FROM list_items WHERE list_id = ? AND signal_id = ?",
            (list_id, raw["signal_id"]),
        ).fetchone()
        if present is not None:
            _bump(skipped, "list_items")
            continue
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM list_items WHERE list_id = ?", (list_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO list_items (list_id, signal_id, position, skip, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                list_id,
                raw["signal_id"],
                position,
                bool(raw.get("skip", False)),
                raw.get("added_at") or now_iso(),
            ),
        )
        _bump(added, "list_items")

    for raw in doc.get("saved_searches", []):
        if conn.execute("SELECT 1 FROM saved_searches WHERE id = ?", (raw["id"],)).fetchone():
            _bump(skipped, "saved_searches")
            continue
        params = dict(raw.get("params") or {})
        if "list" in params:
            params["list"] = remap.get(params["list"], params["list"])
        conn.execute(
            "INSERT INTO saved_searches (id, name, params, created_at) VALUES (?, ?, ?, ?)",
            (raw["id"], raw["name"], json.dumps(params), raw.get("created_at") or now_iso()),
        )
        _bump(added, "saved_searches")


def _import_labels(
    conn, doc: dict, added: dict[str, int], updated: dict[str, int], skipped: dict[str, int]
) -> None:
    """Labels are checked with the same field rules as LabelsRepo.put, but a
    bad row is skipped rather than failing the whole import. The rotating-
    address check is not repeated: it needs the observed address type, which
    an import doesn't have, and every exported label already passed it."""
    for raw in doc.get("device_labels", []):
        try:
            receiver = raw["receiver"]
            ident = normalize_ident(receiver, raw["ident"])
            name = str(raw["name"]).strip()
            note = str(raw.get("note") or "")
            cls = raw["class"]
            created_at = float(raw["created_at"])
            updated_at = float(raw["updated_at"])
            if not 1 <= len(name) <= 80 or len(note) > 500 or cls not in CLASSES:
                raise LabelError("invalid label fields")
        except (KeyError, TypeError, ValueError):
            _bump(skipped, "device_labels")
            continue
        existing = conn.execute(
            "SELECT updated_at FROM device_labels WHERE receiver = ? AND ident = ?",
            (receiver, ident),
        ).fetchone()
        if existing is not None and updated_at <= existing["updated_at"]:
            _bump(skipped, "device_labels")
            continue
        conn.execute(
            "INSERT INTO device_labels (receiver, ident, name, note, class, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (receiver, ident) DO UPDATE SET"
            " name = excluded.name, note = excluded.note, class = excluded.class,"
            " updated_at = excluded.updated_at",
            (receiver, ident, name, note, cls, created_at, updated_at),
        )
        _bump(added if existing is None else updated, "device_labels")


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def import_all(db: Database, doc: dict) -> "ImportReport":
    if doc.get("format") != FORMAT:
        raise ValueError(f"unsupported export format: {doc.get('format')!r}")

    added: dict[str, int] = {}
    updated: dict[str, int] = {}
    skipped: dict[str, int] = {}

    conn = db.conn
    tag_repo = TagRepo(db)
    profile_repo = ProfileRepo(db)
    state_repo = StateRepo(db)
    signal_repo = SignalRepo(db)

    try:
        # Tags: merge by id, then by name. Tags matched by name get a
        # different local id than the source used (the ten category tags get
        # fresh uuid7 ids on every install), so remember source id -> local
        # id for every such match; incoming signals below are rewritten
        # through this map.
        tags_by_id = {t.id: t for t in tag_repo.list()}
        tags_by_name = {t.name: t for t in tags_by_id.values()}
        id_remap: dict[str, str] = {}
        for raw in doc.get("tags", []):
            tag = Tag.model_validate(raw)
            if tag.id in tags_by_id:
                _bump(skipped, "tags")
                continue
            existing = tags_by_name.get(tag.name)
            if existing is not None:
                id_remap[tag.id] = existing.id
                _bump(skipped, "tags")
                continue
            _add_tag(db, tag)
            tags_by_id[tag.id] = tag
            tags_by_name[tag.name] = tag
            _bump(added, "tags")

        # Profiles: newer updated_at wins; equal -> skipped.
        for raw in doc.get("profiles", []):
            profile = ReceiverProfile.model_validate(raw)
            existing = profile_repo.get(profile.id)
            if existing is None:
                _add_profile(db, profile)
                _bump(added, "profiles")
            elif profile.updated_at > existing.updated_at:
                _replace_profile(db, profile)
                _bump(updated, "profiles")
            else:
                _bump(skipped, "profiles")

        # States: content-addressed by id, insert-if-absent.
        for raw in doc.get("receiver_states", []):
            state = ReceiverState.model_validate(raw)
            if state_repo.get(state.id) is not None:
                _bump(skipped, "receiver_states")
                continue
            _add_state(db, state)
            _bump(added, "receiver_states")

        # Signals: newer updated_at wins; equal -> skipped. tag_ids are
        # rewritten through id_remap (dropping anything that still doesn't
        # resolve) before either insert or update.
        for raw in doc.get("signals", []):
            signal = Signal.model_validate(raw)
            signal.tag_ids = _remap_tag_ids(signal.tag_ids, id_remap, set(tags_by_id))
            existing = signal_repo.get(signal.id)
            if existing is None:
                _add_signal(db, signal)
                _bump(added, "signals")
            elif signal.updated_at > existing.updated_at:
                _replace_signal(db, signal)
                _bump(updated, "signals")
            else:
                _bump(skipped, "signals")

        _import_lists(conn, doc, added, skipped)
        _import_labels(conn, doc, added, updated, skipped)

        # Journal entries: append-only, insert-if-absent.
        for raw in doc.get("journal_entries", []):
            entry = JournalEntry.model_validate(raw)
            if _journal_entry_exists(db, entry.id):
                _bump(skipped, "journal_entries")
                continue
            _add_journal_entry(db, entry)
            _bump(added, "journal_entries")
    except Exception:
        # Profiles/states/etc. above wrote through `conn` without committing
        # (see the _add_*/_replace_* helpers), so a failure anywhere in this
        # import -- e.g. a bad row -- rolls back everything from this call,
        # not just the item that failed.
        conn.rollback()
        raise
    else:
        conn.commit()

    return ImportReport(added=added, updated=updated, skipped=skipped)
