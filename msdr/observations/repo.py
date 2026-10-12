"""ObservationRepo: sessions, observations, append-only claims, recording refs.

Style follows msdr.storage.repos: synchronous, parameterised SQL over the one
`Database` connection, JSON columns for nested values, commit per write.
The public interface is fixed (the service/API codes
against it).
"""

# `list()` is a method name here (contract), so defer annotation evaluation
# to keep `list[...]` meaning the builtin inside the class body.
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from msdr.contracts.evidence import Claim, evidence_state, identity_established
from msdr.observations.models import Observation, RecordingReference, Session
from msdr.storage.db import Database

# Newest first by the best time we have: an observation's end, else its
# start, else when the row was last touched (legacy rows without times).
_ORDER = "ORDER BY COALESCE(end_ts, start_ts, updated_at) DESC, id DESC"


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value)


def _loads(text, default=None):
    return json.loads(text) if text else default


def _epoch(dt: datetime | None) -> float | None:
    return None if dt is None else dt.timestamp()


def _dt(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC)


def _session_from_row(row) -> Session:
    return Session(
        id=row["id"],
        source_id=row["source_id"],
        mode=row["mode"],
        origin=row["origin"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        center_hz=row["center_hz"],
        sample_rate_hz=row["sample_rate_hz"],
        receiver=_loads(row["receiver_json"]),
        notes=row["notes"],
    )


def _obs_from_row(row) -> Observation:
    return Observation(
        id=row["id"],
        session_id=row["session_id"],
        signal_id=row["signal_id"],
        find_id=row["find_id"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        sample_start=row["sample_start"],
        sample_end=row["sample_end"],
        reported_hz=row["reported_hz"],
        measured_hz=row["measured_hz"],
        bandwidth_hz=row["bandwidth_hz"],
        receiver=_loads(row["receiver_json"]),
        measurements=_loads(row["measurements_json"], []),
        capture_quality=_loads(row["capture_quality_json"], []),
        legacy=bool(row["legacy"]),
        provenance=row["provenance"],
        closed=bool(row["closed"]),
        notes=row["notes"] if "notes" in row.keys() else "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _claim_from_row(row) -> Claim:
    return Claim(
        claim_id=row["id"],
        observation_id=row["observation_id"],
        subject=row["subject"],
        value=row["value"],
        evidence_type=row["evidence_type"],
        integrity=row["integrity"],
        producer=row["producer"],
        producer_version=row["producer_version"],
        observed_start=_dt(row["observed_start"]),
        observed_end=_dt(row["observed_end"]),
        sample_start=row["sample_start"],
        sample_end=row["sample_end"],
        support=_loads(row["support_json"], []),
        measurements=_loads(row["measurements_json"], []),
        alternatives=_loads(row["alternatives_json"], []),
        limitations=_loads(row["limitations_json"], []),
        score=row["score"],
        next_action=row["next_action"],
        # review/replay decode claims carry analysis_run_id (column added
        # ahead of time by migration 012, wired up here with review decode) -- absent on
        # every live claim, so a missing/older column reads as None either way.
        analysis_run_id=row["analysis_run_id"] if "analysis_run_id" in row.keys() else None,
        superseded_by=row["superseded_by"],
        created_at=_dt(row["created_at"]),
    )


def _ref_from_row(row) -> RecordingReference:
    return RecordingReference(
        id=row["id"],
        observation_id=row["observation_id"],
        path=row["path"],
        name=row["name"],
        run_index=row["run_index"],
        sample_start=row["sample_start"],
        sample_end=row["sample_end"],
        available=bool(row["available"]),
        note=row["note"],
    )


def _json_list(models) -> str:
    return json.dumps([m.model_dump(mode="json") for m in models])


class ObservationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- sessions ---------------------------------------------------------

    def open_session(self, session: Session) -> Session:
        self.db.conn.execute(
            "INSERT INTO sessions (id, source_id, mode, origin, started_at, ended_at, "
            "center_hz, sample_rate_hz, receiver_json, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.source_id,
                session.mode,
                session.origin,
                session.started_at,
                session.ended_at,
                session.center_hz,
                session.sample_rate_hz,
                _dumps(session.receiver),
                session.notes,
            ),
        )
        self.db.conn.commit()
        return session

    def close_session(self, session_id: str, ended_at: float) -> None:
        self.db.conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (ended_at, session_id)
        )
        self.db.conn.commit()

    def get_session(self, session_id: str) -> Session | None:
        row = self.db.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _session_from_row(row) if row else None

    # -- observations -----------------------------------------------------

    def add(self, obs: Observation) -> Observation:
        now = time.time()
        obs = obs.model_copy(
            update={
                "created_at": obs.created_at if obs.created_at is not None else now,
                "updated_at": obs.updated_at if obs.updated_at is not None else now,
            }
        )
        self.db.conn.execute(
            "INSERT INTO observations (id, session_id, signal_id, find_id, start_ts, end_ts, "
            "sample_start, sample_end, reported_hz, measured_hz, bandwidth_hz, receiver_json, "
            "measurements_json, capture_quality_json, legacy, provenance, closed, notes, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.id,
                obs.session_id,
                obs.signal_id,
                obs.find_id,
                obs.start_ts,
                obs.end_ts,
                obs.sample_start,
                obs.sample_end,
                obs.reported_hz,
                obs.measured_hz,
                obs.bandwidth_hz,
                _dumps(obs.receiver),
                _json_list(obs.measurements),
                json.dumps(list(obs.capture_quality)),
                int(obs.legacy),
                obs.provenance,
                int(obs.closed),
                obs.notes,
                obs.created_at,
                obs.updated_at,
            ),
        )
        self.db.conn.commit()
        return obs

    def update(self, obs: Observation) -> None:
        """Persist the mutable reception fields (measurements, quality,
        times/samples, bandwidth, receiver, closed). Identity-ish links --
        session, signal (see link_signal), find, legacy -- are deliberately
        not rewritten here, so a stale in-memory copy can't undo a link."""
        self.db.conn.execute(
            "UPDATE observations SET start_ts = ?, end_ts = ?, sample_start = ?, sample_end = ?, "
            "measured_hz = ?, bandwidth_hz = ?, receiver_json = ?, measurements_json = ?, "
            "capture_quality_json = ?, provenance = ?, closed = ?, updated_at = ? WHERE id = ?",
            (
                obs.start_ts,
                obs.end_ts,
                obs.sample_start,
                obs.sample_end,
                obs.measured_hz,
                obs.bandwidth_hz,
                _dumps(obs.receiver),
                _json_list(obs.measurements),
                json.dumps(list(obs.capture_quality)),
                obs.provenance,
                int(obs.closed),
                time.time(),
                obs.id,
            ),
        )
        self.db.conn.commit()

    def close(self, observation_id: str, end_ts: float | None, sample_end: int | None) -> None:
        # COALESCE: closing without a time (replay) must not erase one that
        # was already recorded.
        self.db.conn.execute(
            "UPDATE observations SET closed = 1, end_ts = COALESCE(?, end_ts), "
            "sample_end = COALESCE(?, sample_end), updated_at = ? WHERE id = ?",
            (end_ts, sample_end, time.time(), observation_id),
        )
        self.db.conn.commit()

    def get(self, observation_id: str) -> Observation | None:
        row = self.db.conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        return _obs_from_row(row) if row else None

    def set_notes(self, observation_id: str, text: str) -> None:
        self.db.conn.execute(
            "UPDATE observations SET notes = ?, updated_at = ? WHERE id = ?",
            (text, time.time(), observation_id),
        )
        self.db.conn.commit()

    def link_signal(self, observation_id: str, signal_id: str | None) -> None:
        self.db.conn.execute(
            "UPDATE observations SET signal_id = ?, updated_at = ? WHERE id = ?",
            (signal_id, time.time(), observation_id),
        )
        self.db.conn.commit()

    def list(
        self,
        *,
        frequency_hz: int | None = None,
        tolerance_hz: int = 6000,
        signal_id: str | None = None,
        session_id: str | None = None,
        include_legacy: bool = True,
        limit: int = 50,
    ) -> list[Observation]:
        clauses: list[str] = []
        params: list = []
        if frequency_hz is not None:
            clauses.append("reported_hz BETWEEN ? AND ?")
            params += [frequency_hz - tolerance_hz, frequency_hz + tolerance_hz]
        if signal_id is not None:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if not include_legacy:
            clauses.append("legacy = 0")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self.db.conn.execute(
            f"SELECT * FROM observations {where}{_ORDER} LIMIT ?", (*params, limit)
        ).fetchall()
        return [_obs_from_row(r) for r in rows]

    def open_near(
        self, frequency_hz: int, tolerance_hz: int, session_id: str
    ) -> Observation | None:
        # `IS ?` so a session-less recorder (session_id None) still finds its
        # own open observations instead of matching nothing.
        row = self.db.conn.execute(
            "SELECT * FROM observations WHERE closed = 0 AND session_id IS ? "
            f"AND reported_hz BETWEEN ? AND ? {_ORDER} LIMIT 1",
            (session_id, frequency_hz - tolerance_hz, frequency_hz + tolerance_hz),
        ).fetchone()
        return _obs_from_row(row) if row else None

    # -- claims (append-only) --------------------------------------------

    def _insert_claim(self, claim: Claim) -> Claim:
        if claim.observation_id is None:
            raise ValueError("a stored claim needs an observation_id")
        if claim.created_at is None:
            claim = claim.model_copy(update={"created_at": datetime.now(UTC)})
        self.db.conn.execute(
            "INSERT INTO claims (id, observation_id, subject, value, evidence_type, integrity, "
            "producer, producer_version, observed_start, observed_end, sample_start, sample_end, "
            "support_json, measurements_json, alternatives_json, limitations_json, score, "
            "next_action, analysis_run_id, superseded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.claim_id,
                claim.observation_id,
                claim.subject,
                claim.value,
                claim.evidence_type,
                claim.integrity,
                claim.producer,
                claim.producer_version,
                _epoch(claim.observed_start),
                _epoch(claim.observed_end),
                claim.sample_start,
                claim.sample_end,
                json.dumps(claim.support),
                _json_list(claim.measurements),
                json.dumps(claim.alternatives),
                json.dumps(claim.limitations),
                claim.score,
                claim.next_action,
                claim.analysis_run_id,
                claim.superseded_by,
                _epoch(claim.created_at),
            ),
        )
        return claim

    def add_claim(self, claim: Claim) -> Claim:
        claim = self._insert_claim(claim)
        self.db.conn.commit()
        return claim

    def supersede(self, old_claim_id: str, new: Claim) -> Claim:
        """Append `new` and point the old claim at it. The old row is kept
        (historical claims stay inspectable)."""
        conn = self.db.conn
        old = conn.execute(
            "SELECT observation_id FROM claims WHERE id = ?", (old_claim_id,)
        ).fetchone()
        if old is None:
            raise KeyError(old_claim_id)
        if new.observation_id is None:
            new = new.model_copy(update={"observation_id": old["observation_id"]})
        try:
            new = self._insert_claim(new)
            conn.execute(
                "UPDATE claims SET superseded_by = ? WHERE id = ?", (new.claim_id, old_claim_id)
            )
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return new

    def claims(self, observation_id: str, *, include_historical: bool = True) -> list[Claim]:
        sql = "SELECT * FROM claims WHERE observation_id = ?"
        if not include_historical:
            sql += " AND superseded_by IS NULL"
        rows = self.db.conn.execute(sql + " ORDER BY created_at, id", (observation_id,))
        return [_claim_from_row(r) for r in rows.fetchall()]

    # -- recording refs ---------------------------------------------------

    def add_ref(self, ref: RecordingReference) -> RecordingReference:
        self.db.conn.execute(
            "INSERT INTO recording_refs (id, observation_id, path, name, run_index, "
            "sample_start, sample_end, available, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref.id,
                ref.observation_id,
                ref.path,
                ref.name,
                ref.run_index,
                ref.sample_start,
                ref.sample_end,
                int(ref.available),
                ref.note,
            ),
        )
        self.db.conn.commit()
        return ref

    def update_ref(self, ref: RecordingReference) -> None:
        """Rewrite a reference's location/range (a recording's ref is opened
        at REC and refreshed once the directory is finalized)."""
        self.db.conn.execute(
            "UPDATE recording_refs SET path = ?, name = ?, run_index = ?, sample_start = ?, "
            "sample_end = ?, available = ?, note = ? WHERE id = ?",
            (
                ref.path,
                ref.name,
                ref.run_index,
                ref.sample_start,
                ref.sample_end,
                int(ref.available),
                ref.note,
                ref.id,
            ),
        )
        self.db.conn.commit()

    def refs(self, observation_id: str) -> list[RecordingReference]:
        rows = self.db.conn.execute(
            "SELECT * FROM recording_refs WHERE observation_id = ? ORDER BY run_index, id",
            (observation_id,),
        ).fetchall()
        return [_ref_from_row(r) for r in rows]

    def observation_ids_by_ref_path(self, path: str, *, provenance: str | None = None) -> list[str]:
        """Observations that reference a recording path (bookmarks on one
        file), newest first -- an indexed lookup instead of scanning the
        whole observation list."""
        sql = (
            "SELECT DISTINCT o.id FROM observations o "
            "JOIN recording_refs r ON r.observation_id = o.id WHERE r.path = ?"
        )
        params: list = [path]
        if provenance is not None:
            sql += " AND o.provenance = ?"
            params.append(provenance)
        sql += " ORDER BY COALESCE(o.start_ts, o.created_at) DESC, o.id DESC"
        return [row[0] for row in self.db.conn.execute(sql, params).fetchall()]

    def mark_unavailable(self, path: str) -> int:
        cur = self.db.conn.execute(
            "UPDATE recording_refs SET available = 0 WHERE path = ? AND available = 1", (path,)
        )
        self.db.conn.commit()
        return cur.rowcount

    # -- links ------------------------------------------------------------

    def link_decode_event(self, event_id: str, observation_id: str) -> None:
        self.db.conn.execute(
            "UPDATE decode_events SET observation_id = ? WHERE id = ?", (observation_id, event_id)
        )
        self.db.conn.commit()

    def decode_event_ids(self, observation_id: str, limit: int = 50) -> list[str]:
        rows = self.db.conn.execute(
            "SELECT id FROM decode_events WHERE observation_id = ? ORDER BY ts DESC LIMIT ?",
            (observation_id, limit),
        ).fetchall()
        return [r["id"] for r in rows]

    def decode_events(self, observation_id: str) -> list[dict]:
        """Full decode_events rows linked to an observation (export), with
        `*_json` columns decoded under their bare name."""
        rows = self.db.conn.execute(
            "SELECT * FROM decode_events WHERE observation_id = ? ORDER BY ts, id",
            (observation_id,),
        ).fetchall()
        out = []
        for row in rows:
            d = {}
            for key in row.keys():
                if key.endswith("_json"):
                    d[key[: -len("_json")]] = _loads(row[key])
                else:
                    d[key] = row[key]
            out.append(d)
        return out

    def list_updated_since(self, since_ts: float | None, limit: int) -> list[Observation]:
        """Export listing: observations touched at/after `since_ts` (updated_at,
        so an edited note re-exports), oldest first so a caller can page."""
        rows = self.db.conn.execute(
            "SELECT * FROM observations WHERE updated_at >= ? ORDER BY updated_at, id LIMIT ?",
            (since_ts if since_ts is not None else float("-inf"), limit),
        ).fetchall()
        return [_obs_from_row(r) for r in rows]

    # -- summary ----------------------------------------------------------

    def summary(self, obs: Observation) -> dict:
        claims = self.claims(obs.id)
        current = [c for c in claims if not c.historical]
        return {
            **obs.model_dump(mode="json"),
            "evidence_state": evidence_state(current),
            "identity_established": identity_established(current),
            "claim_counts": {"current": len(current), "historical": len(claims) - len(current)},
            "claims": [{**c.model_dump(mode="json"), "historical": c.historical} for c in claims],
            "refs": [r.model_dump(mode="json") for r in self.refs(obs.id)],
            "decode_event_ids": self.decode_event_ids(obs.id),
        }
