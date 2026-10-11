"""`LabelsRepo`: user labels for Wi-Fi and BLE devices, in the main database
(migration 021, docs/history.md "Labels")."""

from __future__ import annotations

import re
import time

from msdr.ble.adv import classify_address
from msdr.storage.db import Database

CLASSES = ("mine", "known", "ignore")
RECEIVERS = ("wifi", "ble")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class LabelError(ValueError):
    pass


def normalize_ident(receiver: str, ident: str) -> str:
    if receiver not in RECEIVERS:
        raise LabelError(f"unknown receiver: {receiver!r}")
    ident = ident.strip().lower().replace("-", ":")
    if not _MAC.match(ident):
        raise LabelError(f"not a MAC address: {ident!r}")
    return ident


def check_labelable(receiver: str, ident: str, kind: str | None) -> None:
    """A rotating BLE private address cannot carry a lasting label. The kind
    comes from what was observed; without one, the address bits decide
    (the caller cannot know whether a BLE address is public or random, so an
    unobserved address is classified as random)."""
    if receiver != "ble":
        return
    kind = kind or classify_address(ident, "random")
    if kind in ("rpa", "nrpa"):
        raise LabelError(
            "this address rotates, so a label would stop matching; "
            "rotation is a privacy protection, so msdr does not link rotating addresses"
        )


def _row(r) -> dict:
    return {
        "receiver": r["receiver"],
        "ident": r["ident"],
        "name": r["name"],
        "note": r["note"],
        "class": r["class"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


class LabelsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self, receiver: str | None = None) -> list[dict]:
        if receiver is None:
            rows = self.db.conn.execute("SELECT * FROM device_labels ORDER BY name").fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM device_labels WHERE receiver = ? ORDER BY name", (receiver,)
            ).fetchall()
        return [_row(r) for r in rows]

    def get(self, receiver: str, ident: str) -> dict | None:
        r = self.db.conn.execute(
            "SELECT * FROM device_labels WHERE receiver = ? AND ident = ?", (receiver, ident)
        ).fetchone()
        return _row(r) if r else None

    def put(self, receiver: str, ident: str, *, name: str, note: str, cls: str) -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 80:
            raise LabelError("name must be 1-80 characters")
        if len(note) > 500:
            raise LabelError("note must be at most 500 characters")
        if cls not in CLASSES:
            raise LabelError(f"class must be one of {', '.join(CLASSES)}")
        now = time.time()
        self.db.conn.execute(
            "INSERT INTO device_labels (receiver, ident, name, note, class, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (receiver, ident) DO UPDATE SET"
            " name = excluded.name, note = excluded.note, class = excluded.class,"
            " updated_at = excluded.updated_at",
            (receiver, ident, name, note, cls, now, now),
        )
        self.db.conn.commit()
        return self.get(receiver, ident)  # type: ignore[return-value]

    def delete(self, receiver: str, ident: str) -> bool:
        n = self.db.conn.execute(
            "DELETE FROM device_labels WHERE receiver = ? AND ident = ?", (receiver, ident)
        ).rowcount
        self.db.conn.commit()
        return n > 0

    def keys(self, classes: set[str]) -> set[tuple[str, str]]:
        if not classes:
            return set()
        rows = self.db.conn.execute(
            "SELECT receiver, ident FROM device_labels"
            f" WHERE class IN ({','.join('?' * len(classes))})",
            tuple(classes),
        ).fetchall()
        return {(r["receiver"], r["ident"]) for r in rows}

    def by_key(self) -> dict[tuple[str, str], dict]:
        return {(lab["receiver"], lab["ident"]): lab for lab in self.list()}

    def search(self, q: str) -> set[tuple[str, str]]:
        needle = f"%{q.lower()}%"
        rows = self.db.conn.execute(
            "SELECT receiver, ident FROM device_labels"
            " WHERE LOWER(name) LIKE ? OR LOWER(note) LIKE ?",
            (needle, needle),
        ).fetchall()
        return {(r["receiver"], r["ident"]) for r in rows}
