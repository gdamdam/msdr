"""`RulesRepo`: watch rules and their fired-event log, in the main database
(migration 022, docs/rules.md). Parallel to `msdr.history.labels.LabelsRepo`
-- same database, same "backup carries it" reasoning, since these are small
user-authored config and a bounded log, not bulk observation data."""

from __future__ import annotations

import json
import time

from msdr.storage.db import Database

KINDS = ("beacon_presence", "sensor_threshold", "drone_geofence")
#: Hard cap on rule_events rows -- pruned opportunistically on every insert
#: past this count (docs/rules.md "Limits"), independent of age.
MAX_EVENTS = 5000
DEFAULT_COOLDOWN_S = 60.0


class RuleError(ValueError):
    pass


def _rule_row(r) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "config": json.loads(r["config"]),
        "cooldown_s": r["cooldown_s"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _event_row(r) -> dict:
    return {
        "id": r["id"],
        "rule_id": r["rule_id"],
        "kind": r["kind"],
        "target": r["target"],
        "message": r["message"],
        "detail": json.loads(r["detail"]),
        "fired_at": r["fired_at"],
    }


class RulesRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def conn(self):
        return self.db.conn

    # --- rules ---------------------------------------------------------

    def list(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM rules ORDER BY name").fetchall()
        return [_rule_row(r) for r in rows]

    def get(self, rule_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return _rule_row(r) if r else None

    def create(
        self, *, kind: str, name: str, config: dict, enabled: bool = True,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> dict:
        if kind not in KINDS:
            raise RuleError(f"kind must be one of {', '.join(KINDS)}")
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise RuleError("name must be 1-80 characters")
        if cooldown_s < 0:
            raise RuleError("cooldown_s must be non-negative")
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO rules (kind, name, enabled, config, cooldown_s, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, name, int(enabled), json.dumps(config), cooldown_s, now, now),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)  # type: ignore[return-value, arg-type]

    def update(
        self, rule_id: int, *, name: str | None = None, config: dict | None = None,
        enabled: bool | None = None, cooldown_s: float | None = None,
    ) -> dict | None:
        existing = self.get(rule_id)
        if existing is None:
            return None
        if name is not None:
            name = name.strip()
            if not 1 <= len(name) <= 80:
                raise RuleError("name must be 1-80 characters")
        if cooldown_s is not None and cooldown_s < 0:
            raise RuleError("cooldown_s must be non-negative")
        self.conn.execute(
            "UPDATE rules SET name = ?, config = ?, enabled = ?, cooldown_s = ?, updated_at = ?"
            " WHERE id = ?",
            (
                name if name is not None else existing["name"],
                json.dumps(config) if config is not None else json.dumps(existing["config"]),
                int(enabled) if enabled is not None else int(existing["enabled"]),
                cooldown_s if cooldown_s is not None else existing["cooldown_s"],
                time.time(),
                rule_id,
            ),
        )
        self.conn.commit()
        return self.get(rule_id)

    def delete(self, rule_id: int) -> bool:
        n = self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount
        self.conn.commit()
        return n > 0

    # --- events ----------------------------------------------------------

    def add_event(
        self, *, rule_id: int, kind: str, target: str, message: str, detail: dict, fired_at: float,
    ) -> dict:
        cur = self.conn.execute(
            "INSERT INTO rule_events (rule_id, kind, target, message, detail, fired_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (rule_id, kind, target, message, json.dumps(detail), fired_at),
        )
        self.conn.commit()
        self._prune_events()
        row = self.conn.execute(
            "SELECT * FROM rule_events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _event_row(row)

    def _prune_events(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) FROM rule_events").fetchone()[0]
        if total <= MAX_EVENTS:
            return
        self.conn.execute(
            "DELETE FROM rule_events WHERE id IN ("
            " SELECT id FROM rule_events ORDER BY fired_at ASC LIMIT ?)",
            (total - MAX_EVENTS,),
        )
        self.conn.commit()

    def list_events(self, *, rule_id: int | None = None, limit: int = 200) -> list[dict]:
        if rule_id is None:
            rows = self.conn.execute(
                "SELECT * FROM rule_events ORDER BY fired_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM rule_events WHERE rule_id = ? ORDER BY fired_at DESC LIMIT ?",
                (rule_id, limit),
            ).fetchall()
        return [_event_row(r) for r in rows]

    def last_fired_by_target(self) -> dict[tuple[int, str], float]:
        """`(rule_id, target) -> most recent fired_at`, used to seed a fresh
        `EngineState.last_fired` at startup so a cooldown already in progress
        before a restart is honoured (docs/rules.md "Limits")."""
        rows = self.conn.execute(
            "SELECT rule_id, target, MAX(fired_at) AS last_fired_at FROM rule_events"
            " GROUP BY rule_id, target"
        ).fetchall()
        return {(r["rule_id"], r["target"]): r["last_fired_at"] for r in rows}
