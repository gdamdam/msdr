"""Incremental, bounded-memory persistence for wide-band scans.

Legacy JSON rows are never rewritten. New sessions use normalized observations;
a single worker owns its SQLite connection and receives bounded events.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

from msdr.storage.db import Database
from msdr.survey.scan_report_reader import ScanReportReader


def _json(value):
    return json.dumps(value, separators=(",", ":"), default=str)


def _identity(sample):
    for key in ("confirmed", "rds", "recognized", "family"):
        value = sample.get(key)
        if isinstance(value, dict):
            name = value.get("name") or value.get("ps") or value.get("label")
        else:
            name = value if isinstance(value, str) else None
        if name:
            return str(name)
    return None


def _signature(sample):
    # Collapse updates to clocks, strength and momentary duty measurements.
    # Keep semantic DSP/decoder changes, including evidence disappearing.
    stable = {
        k: v
        for k, v in sample.items()
        if k
        not in {
            "snr_db",
            "last_seen",
            "observed_at",
            "span_start_hz",
            "span_end_hz",
        }
    }
    activity = stable.get("activity")
    if isinstance(activity, dict):
        stable["activity"] = activity.get("kind") or activity.get("state")
    for key in ("analysis_state", "last_classification", "family", "confirmed", "rds", "rfml"):
        value = stable.get(key)
        if isinstance(value, dict):
            stable[key] = {
                k: v
                for k, v in value.items()
                if k
                not in {
                    "at",
                    "previous_at",
                    "analyzed_at",
                    "last_seen",
                    "age_s",
                    "duration_s",
                }
            }
    for key in ("bw_hz", "width_hz", "spectrum_bandwidth_hz"):
        if isinstance(stable.get(key), (int, float)):
            stable[key] = round(stable[key] / 100) * 100
    return hashlib.sha256(_json(stable).encode()).hexdigest()


def _snr(sample):
    value = sample.get("snr_db")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class ScanWriterError(RuntimeError):
    pass


class BandScanRepo:
    def __init__(self, db: Database, *, queue_size: int = 256, start_writer: bool = True):
        self.db = db
        self._memory = str(db.path) == ":memory:"
        self._report_reader = ScanReportReader(db.path) if not self._memory else None
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self.degraded_reason: str | None = None
        # Writes that were already accepted into the queue when the writer
        # failed, then drained without being applied. Surfaced so a degraded
        # run never looks complete.
        self.dropped_writes = 0
        self._legacy_cache: dict[str, tuple[float, dict]] = {}
        # Last captured_frames value actually committed per scan_id, so a
        # checkpoint can skip re-sending an unchanged spectrum export.
        self._spectrum_committed: dict[str, int] = {}
        self._pending_checkpoint = None
        self._pending_lock = threading.Lock()
        if not self._memory and start_writer:
            self._queue = queue.Queue(maxsize=max(1, queue_size))
            self._thread = threading.Thread(target=self._work, name="msdr-scan-writer", daemon=True)
            self._thread.start()

    def _work(self):
        conn = None
        try:
            try:
                conn = sqlite3.connect(str(self.db.path), timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA busy_timeout = 30000")
            except Exception as exc:
                self._error = exc
                self.degraded_reason = f"scan storage failed: {exc}"
            while True:
                kind, args = self._queue.get()
                try:
                    if kind == "close":
                        return
                    if self._error is not None:
                        if kind != "barrier":
                            self._note_dropped(1)
                    else:
                        self._apply(conn, kind, args)
                        # A coalesced checkpoint is newer than the queued ones.
                        # Apply it only after older work, before completing the
                        # barrier that promises all pending writes are durable.
                        if self._queue.empty() or kind == "barrier":
                            with self._pending_lock:
                                pending, self._pending_checkpoint = self._pending_checkpoint, None
                            if pending:
                                self._apply(conn, *pending)
                        conn.commit()
                except Exception as exc:
                    conn.rollback()
                    self._error = exc
                    self.degraded_reason = f"scan storage failed: {exc}"
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._error = exc
            self.degraded_reason = f"scan storage failed: {exc}"
        finally:
            if conn is not None:
                conn.close()

    def _note_dropped(self, n: int) -> None:
        with self._pending_lock:
            if self._pending_checkpoint is not None:
                self._pending_checkpoint = None
                n += 1
        self.dropped_writes += n
        self.degraded_reason = (
            f"scan storage failed: {self._error}; "
            f"{self.dropped_writes} queued write(s) dropped"
        )

    def _submit(self, kind, *args):
        if self._error is not None:
            raise ScanWriterError(str(self._error))
        if self._memory:
            self._apply(self.db.conn, kind, args)
            self.db.conn.commit()
            return
        if kind == "checkpoint":
            with self._pending_lock:
                if self._pending_checkpoint is not None:
                    self._pending_checkpoint = (kind, args)
                    return
        try:
            self._queue.put_nowait((kind, args))
        except queue.Full as exc:
            self.degraded_reason = (
                "scan storage is behind capture; checkpoint coalesced until writer catches up"
            )
            if kind == "checkpoint":
                with self._pending_lock:
                    self._pending_checkpoint = (kind, args)
                return
            raise ScanWriterError(
                "scan storage queue full; observation batch not accepted"
            ) from exc
        if self._queue.qsize() >= self._queue.maxsize * 0.8:
            self.degraded_reason = (
                "scan storage is behind capture; queued findings retained, "
                "capture may be incomplete"
            )

    async def aappend_observations(self, scan_id: str, observations: list[dict]) -> None:
        if not observations:
            return
        if self._memory:
            self.append_observations(scan_id, observations)
            return
        event = ("observations", (scan_id, copy.deepcopy(observations)))
        while True:
            if self._error:
                raise ScanWriterError(str(self._error))
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                self.degraded_reason = (
                    "scan storage is behind capture; capture paused until findings are queued"
                )
                await asyncio.sleep(0.05)

    async def aflush(self):
        if self._error:
            raise ScanWriterError(str(self._error))
        if self._queue is not None:
            # A barrier is processed after the last event and any coalesced
            # checkpoint; join alone could return while that checkpoint commits.
            await asyncio.to_thread(self._queue.put, ("barrier", ()))
            await asyncio.to_thread(self._queue.join)
        if self._error:
            raise ScanWriterError(str(self._error))

    async def aclose(self):
        try:
            if self._queue is not None and self._thread is not None:
                try:
                    await self.aflush()
                finally:
                    if self._thread.is_alive():
                        await asyncio.to_thread(self._queue.put, ("close", ()))
                        await asyncio.to_thread(self._thread.join)
                    self._thread = None
        finally:
            if self._report_reader is not None:
                await self._report_reader.aclose()

    def close(self):
        try:
            if self._queue is not None and self._thread is not None:
                if self._thread.is_alive():
                    self._queue.put(("barrier", ()))
                    self._queue.join()
                    self._queue.put(("close", ()))
                    self._thread.join()
                self._thread = None
            if self._error:
                raise ScanWriterError(str(self._error))
        finally:
            if self._report_reader is not None:
                self._report_reader.close()

    def create(self, scan_id: str, config: dict) -> None:
        now = time.time()
        # A new scan starts clean; degraded_reason is sticky only within the
        # scan that caused it, not across every later scan in this process.
        self.degraded_reason = None
        self.db.conn.execute(
            "INSERT INTO survey_band_scans (id,config_json,state,started_at,updated_at) "
            "VALUES (?,?,'running',?,?)",
            (scan_id, _json(config), now, now),
        )
        self.db.conn.commit()

    def checkpoint(
        self,
        scan_id: str,
        state: str,
        metadata: dict,
        span: dict | None = None,
        span_index: int | None = None,
        spectrum: dict | None = None,
    ) -> None:
        # export() rebuilds up to 8192 bins plus 256 time slices; only carry
        # it to the writer when frames were added since the last committed
        # write, and never deep-copy it (export() already returns fresh
        # containers each call, so a copy here only pays the cost twice).
        if spectrum is not None and spectrum.get("captured_frames") == self._spectrum_committed.get(
            scan_id
        ):
            spectrum = None
        self._submit(
            "checkpoint",
            scan_id,
            state,
            copy.deepcopy(metadata),
            copy.deepcopy(span),
            span_index,
            spectrum,
        )

    def append_observations(self, scan_id: str, observations: list[dict]) -> None:
        if observations:
            self._submit("observations", scan_id, copy.deepcopy(observations))

    def save(self, scan_id: str, state: str, payload: dict) -> None:
        """Legacy-compatible save for callers importing the old repository API."""
        spans = payload.get("spans", [])
        self.db.conn.execute(
            "UPDATE survey_band_scans SET state=?,payload_json=?,updated_at=?,"
            "result_count=?,span_count=?,covered_count=?,uncovered_count=? WHERE id=?",
            (
                state,
                _json(payload),
                time.time(),
                len(payload.get("results", [])),
                len(spans),
                sum(s.get("state") == "covered" for s in spans),
                sum(s.get("state") == "uncovered" for s in spans),
                scan_id,
            ),
        )
        self.db.conn.commit()

    def _apply(self, conn, kind, args):
        if kind == "checkpoint":
            scan_id, state, meta, span, span_index, spectrum = args
            if span is not None and span_index is not None:
                conn.execute(
                    "INSERT INTO survey_scan_spans(scan_id,span_index,data_json) VALUES(?,?,?) "
                    "ON CONFLICT(scan_id,span_index) DO UPDATE SET data_json=excluded.data_json",
                    (scan_id, span_index, _json(span)),
                )
            if spectrum is not None:
                conn.execute(
                    "INSERT INTO survey_scan_spectrum(scan_id,data_json) VALUES(?,?) "
                    "ON CONFLICT(scan_id) DO UPDATE SET data_json=excluded.data_json",
                    (scan_id, _json(spectrum)),
                )
                # Only durably-applied writes advance the tracker; a spectrum
                # that never got this far must still be sent on a later call.
                self._spectrum_committed[scan_id] = spectrum.get("captured_frames")
            conn.execute(
                "UPDATE survey_band_scans SET state=?,payload_json=?,updated_at=?,"
                "result_count=?,span_count=?,covered_count=?,uncovered_count=? WHERE id=?",
                (
                    state,
                    _json(meta),
                    time.time(),
                    meta.get("result_count", 0),
                    meta.get("span_count", 0),
                    meta.get("covered_count", 0),
                    meta.get("uncovered_count", 0),
                    scan_id,
                ),
            )
        elif kind == "barrier":
            pass
        elif kind == "observations":
            scan_id, observations = args
            for sample in observations:
                self._append_one(conn, scan_id, sample)

    def _append_one(self, conn, scan_id, sample):
        hz = int(round(sample["frequency_hz"]))
        at = sample["observed_at"]
        snr = _snr(sample)
        signature = _signature(sample)
        encoded = _json(sample)
        previous = conn.execute(
            "SELECT * FROM survey_scan_observations WHERE scan_id=? AND frequency_hz=? "
            "ORDER BY id DESC LIMIT 1",
            (scan_id, hz),
        ).fetchone()
        if previous and previous["signature"] == signature:
            latest = json.loads(previous["latest_json"])
            if (
                sample.get("last_seen") is not None
                and sample["last_seen"] == latest.get("last_seen")
                and snr == _snr(latest)
            ):
                # Polling a quiet retained label is not a new measurement.
                # Evidence changes still create a new run even at the same time.
                return
            peak = json.loads(previous["strongest_json"])
            if snr is not None and (
                previous["snr_max_db"] is None or snr >= previous["snr_max_db"]
            ):
                peak = sample
            conn.execute(
                "UPDATE survey_scan_observations SET last_seen=?,sample_count=sample_count+1,"
                "snr_min_db=?,snr_max_db=?,snr_sum_db=snr_sum_db+?,snr_count=snr_count+?,"
                "latest_json=?,strongest_json=? WHERE id=?",
                (
                    at,
                    min(snr, previous["snr_min_db"])
                    if snr is not None and previous["snr_min_db"] is not None
                    else snr
                    if snr is not None
                    else previous["snr_min_db"],
                    max(snr, previous["snr_max_db"])
                    if snr is not None and previous["snr_max_db"] is not None
                    else snr
                    if snr is not None
                    else previous["snr_max_db"],
                    snr or 0,
                    int(snr is not None),
                    encoded,
                    _json(peak),
                    previous["id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO survey_scan_observations(scan_id,frequency_hz,first_seen,last_seen,"
                "sample_count,snr_min_db,snr_max_db,snr_sum_db,snr_count,signature,first_json,"
                "latest_json,strongest_json) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    hz,
                    at,
                    at,
                    snr,
                    snr,
                    snr or 0,
                    int(snr is not None),
                    signature,
                    encoded,
                    encoded,
                    encoded,
                ),
            )
        result = conn.execute(
            "SELECT * FROM survey_scan_results WHERE scan_id=? AND frequency_hz=?", (scan_id, hz)
        ).fetchone()
        center = sample.get("span_center_hz")
        identity = _identity(sample)
        if result is None:
            conn.execute(
                "INSERT INTO survey_scan_results(scan_id,frequency_hz,first_seen,last_seen,"
                "observation_count,snr_min_db,snr_max_db,snr_sum_db,snr_count,span_centers_json,"
                "identities_json,latest_json,strongest_json) VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    hz,
                    at,
                    at,
                    snr,
                    snr,
                    snr or 0,
                    int(snr is not None),
                    _json([center] if center is not None else []),
                    _json([identity] if identity else []),
                    encoded,
                    encoded,
                ),
            )
        else:
            centers = json.loads(result["span_centers_json"])
            if center is not None and center not in centers:
                centers.append(center)
            identities = json.loads(result["identities_json"])
            if identity and identity not in identities:
                identities.append(identity)
            strongest = json.loads(result["strongest_json"])
            if snr is not None and (result["snr_max_db"] is None or snr >= result["snr_max_db"]):
                strongest = sample
            conn.execute(
                "UPDATE survey_scan_results SET last_seen=?,observation_count=observation_count+1,"
                "snr_min_db=?,snr_max_db=?,snr_sum_db=snr_sum_db+?,snr_count=snr_count+?,"
                "span_centers_json=?,identities_json=?,latest_json=?,strongest_json=? "
                "WHERE scan_id=? AND frequency_hz=?",
                (
                    at,
                    min(snr, result["snr_min_db"])
                    if snr is not None and result["snr_min_db"] is not None
                    else snr
                    if snr is not None
                    else result["snr_min_db"],
                    max(snr, result["snr_max_db"])
                    if snr is not None and result["snr_max_db"] is not None
                    else snr
                    if snr is not None
                    else result["snr_max_db"],
                    snr or 0,
                    int(snr is not None),
                    _json(centers),
                    _json(identities),
                    encoded,
                    _json(strongest),
                    scan_id,
                    hz,
                ),
            )

    def _read_conn(self):
        if self._memory:
            return self.db.conn, False
        uri = f"file:{Path(self.db.path).resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn, True

    def _with_read(self, method, *args):
        conn, owned = self._read_conn()
        try:
            return method(conn, *args)
        finally:
            if owned:
                conn.close()

    def _row(self, conn, scan_id):
        return conn.execute("SELECT * FROM survey_band_scans WHERE id=?", (scan_id,)).fetchone()

    def _is_compact(self, conn, scan_id):
        return (
            conn.execute(
                "SELECT 1 FROM survey_scan_spans WHERE scan_id=? LIMIT 1", (scan_id,)
            ).fetchone()
            is not None
            or conn.execute(
                "SELECT 1 FROM survey_scan_results WHERE scan_id=? LIMIT 1", (scan_id,)
            ).fetchone()
            is not None
            or conn.execute(
                "SELECT 1 FROM survey_scan_spectrum WHERE scan_id=? LIMIT 1", (scan_id,)
            ).fetchone()
            is not None
        )

    def _summary(self, conn, scan_id):
        row = self._row(conn, scan_id)
        if row is None:
            return None
        base = {
            "id": row["id"],
            "config": json.loads(row["config_json"]),
            "state": row["state"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
        }
        if not self._is_compact(conn, scan_id):
            cached = self._legacy_cache.get(scan_id)
            if cached and cached[0] == row["updated_at"]:
                return cached[1]
            payload = json.loads(row["payload_json"] or "{}")
            results = []
            count = 0
            for result in payload.get("results", []):
                obs = result.get("observations", [])
                count += len(obs)
                snrs = [o["snr_db"] for o in obs if isinstance(o.get("snr_db"), (int, float))]
                reps = [obs[0], obs[-1]] if obs else []
                if snrs:
                    strongest = max(
                        (o for o in obs if _snr(o) is not None), key=lambda o: o["snr_db"]
                    )
                    reps.append(strongest)
                results.append(
                    {
                        **{k: v for k, v in result.items() if k != "observations"},
                        "observations": list(
                            {o.get("observed_at", i): o for i, o in enumerate(reps)}.values()
                        ),
                        "observation_count": len(obs),
                        "snr_max_db": max(snrs) if snrs else None,
                        "peak_snr_db": max(snrs) if snrs else None,
                        "identifications": sorted({_identity(o) or "" for o in obs}),
                    }
                )
            summary = {
                **base,
                "payload": {**payload, "results": results},
                "observation_count": count,
                "compact": True,
                "legacy": True,
            }
            self._legacy_cache.clear()
            self._legacy_cache[scan_id] = (row["updated_at"], summary)
            return summary
        meta = json.loads(row["payload_json"] or "{}")
        spans = [
            json.loads(s[0])
            for s in conn.execute(
                "SELECT data_json FROM survey_scan_spans WHERE scan_id=? ORDER BY span_index",
                (scan_id,),
            )
        ]
        results = []
        count = 0
        for r in conn.execute(
            "SELECT * FROM survey_scan_results WHERE scan_id=? ORDER BY frequency_hz", (scan_id,)
        ):
            latest = json.loads(r["latest_json"])
            strongest = json.loads(r["strongest_json"])
            recent = conn.execute(
                "SELECT first_json FROM survey_scan_observations "
                "WHERE scan_id=? AND frequency_hz=? "
                "ORDER BY id DESC LIMIT 4",
                (scan_id, r["frequency_hz"]),
            ).fetchall()
            reps = [json.loads(item[0]) for item in reversed(recent)]
            if strongest not in reps:
                reps.append(strongest)
            if latest not in reps:
                reps.append(latest)
            results.append(
                {
                    "frequency_hz": r["frequency_hz"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "observation_count": r["observation_count"],
                    "snr_min_db": r["snr_min_db"],
                    "snr_max_db": r["snr_max_db"],
                    "snr_avg_db": r["snr_sum_db"] / r["snr_count"] if r["snr_count"] else None,
                    "peak_snr_db": r["snr_max_db"],
                    "identifications": json.loads(r["identities_json"]),
                    "span_centers_hz": json.loads(r["span_centers_json"]),
                    "observations": reps,
                }
            )
            count += r["observation_count"]
        return {
            **base,
            "payload": {**meta, "results": results, "spans": spans},
            "observation_count": count,
            "compact": True,
        }

    def summary(self, scan_id):
        return self._with_read(self._summary, scan_id)

    async def asummary(self, scan_id):
        return (
            await self._report_reader.read("summary", scan_id)
            if not self._memory
            else self.summary(scan_id)
        )

    def _observations(self, conn, scan_id, offset, limit, frequency_hz, min_peak_snr_db=None):
        row = self._row(conn, scan_id)
        if row is None:
            return None
        offset = max(0, int(offset))
        limit = max(1, min(500, int(limit)))
        if not self._is_compact(conn, scan_id):
            payload = json.loads(row["payload_json"] or "{}")
            all_obs = [
                {"frequency_hz": r["frequency_hz"], **o}
                for r in payload.get("results", [])
                for o in r.get("observations", [])
                if (frequency_hz is None or int(r["frequency_hz"]) == int(frequency_hz))
                and (
                    min_peak_snr_db is None
                    or max(
                        (
                            _snr(sample)
                            for sample in r.get("observations", [])
                            if _snr(sample) is not None
                        ),
                        default=float("-inf"),
                    )
                    >= min_peak_snr_db
                )
            ]
            all_obs.sort(key=lambda o: o.get("observed_at") or 0, reverse=True)
            return {
                "id": scan_id,
                "total": len(all_obs),
                "offset": offset,
                "limit": limit,
                "observations": all_obs[offset : offset + limit],
                "legacy": True,
            }
        clause = "scan_id=?" + (" AND frequency_hz=?" if frequency_hz is not None else "")
        args = (scan_id,) + ((int(frequency_hz),) if frequency_hz is not None else ())
        if min_peak_snr_db is not None:
            clause += (
                " AND frequency_hz IN (SELECT frequency_hz FROM survey_scan_results "
                "WHERE scan_id=? AND snr_max_db>=?)"
            )
            args += (scan_id, min_peak_snr_db)
        total = conn.execute(
            f"SELECT count(*) FROM survey_scan_observations WHERE {clause}", args
        ).fetchone()[0]
        records = []
        for r in conn.execute(
            f"SELECT * FROM survey_scan_observations WHERE {clause} "
            "ORDER BY last_seen DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ):
            latest = json.loads(r["latest_json"])
            records.append(
                {
                    **latest,
                    "id": r["id"],
                    "frequency_hz": r["frequency_hz"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "sample_count": r["sample_count"],
                    "snr_min_db": r["snr_min_db"],
                    "snr_max_db": r["snr_max_db"],
                    "snr_avg_db": r["snr_sum_db"] / r["snr_count"] if r["snr_count"] else None,
                    "first": json.loads(r["first_json"]),
                    "strongest": json.loads(r["strongest_json"]),
                }
            )
        return {
            "id": scan_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "observations": records,
            "sample_count": conn.execute(
                f"SELECT coalesce(sum(sample_count),0) FROM survey_scan_observations "
                f"WHERE {clause}",
                args,
            ).fetchone()[0],
        }

    def observations(self, scan_id, offset=0, limit=100, frequency_hz=None, min_peak_snr_db=None):
        return self._with_read(
            self._observations, scan_id, offset, limit, frequency_hz, min_peak_snr_db
        )

    async def aobservations(
        self, scan_id, offset=0, limit=100, frequency_hz=None, min_peak_snr_db=None
    ):
        if self._memory:
            return self.observations(scan_id, offset, limit, frequency_hz, min_peak_snr_db)
        return await self._report_reader.read(
            "observations",
            scan_id,
            offset=offset,
            limit=limit,
            frequency_hz=frequency_hz,
            min_peak_snr_db=min_peak_snr_db,
        )

    def _spectrum(self, conn, scan_id):
        row = self._row(conn, scan_id)
        if row is None:
            return None
        saved = conn.execute(
            "SELECT data_json FROM survey_scan_spectrum WHERE scan_id=?", (scan_id,)
        ).fetchone()
        if saved:
            return {"id": scan_id, **json.loads(saved[0])}
        return {
            "id": scan_id,
            "available": False,
            "reason": "Spectrum was not retained for this scan",
        }

    def spectrum(self, scan_id):
        return self._with_read(self._spectrum, scan_id)

    async def aspectrum(self, scan_id):
        return (
            await self._report_reader.read("spectrum", scan_id)
            if not self._memory
            else self.spectrum(scan_id)
        )

    def _get(self, conn, scan_id):
        row = self._row(conn, scan_id)
        if row is None:
            return None
        if not self._is_compact(conn, scan_id):
            return {
                "id": row["id"],
                "config": json.loads(row["config_json"]),
                "state": row["state"],
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
        summary = self._summary(conn, scan_id)
        by_hz = {r["frequency_hz"]: r for r in summary["payload"]["results"]}
        for r in by_hz.values():
            r["observations"] = []
        for obs in conn.execute(
            "SELECT * FROM survey_scan_observations WHERE scan_id=? ORDER BY id", (scan_id,)
        ):
            # Export retains every compact run and its statistics. It cannot
            # reconstruct identical raw frames; sample_count exposes that loss.
            item = json.loads(obs["latest_json"])
            item.update(
                sample_count=obs["sample_count"],
                first_seen=obs["first_seen"],
                last_seen=obs["last_seen"],
                snr_min_db=obs["snr_min_db"],
                snr_max_db=obs["snr_max_db"],
                snr_avg_db=obs["snr_sum_db"] / obs["snr_count"] if obs["snr_count"] else None,
            )
            item["first"] = json.loads(obs["first_json"])
            item["strongest"] = json.loads(obs["strongest_json"])
            by_hz[obs["frequency_hz"]]["observations"].append(item)
        summary.pop("compact", None)
        summary["spectrum"] = self._spectrum(conn, scan_id)
        return summary

    def get(self, scan_id):
        return self._with_read(self._get, scan_id)

    async def aget(self, scan_id):
        return (
            await self._report_reader.read("get", scan_id)
            if not self._memory
            else self.get(scan_id)
        )

    async def aexport_bytes(self, scan_id):
        if not self._memory:
            return await self._report_reader.read("export", scan_id)
        detail = self.get(scan_id)
        return _json(detail).encode("utf-8") if detail is not None else None

    def _delete_reports(self, scan_id=None):
        conn = self.db.conn if self._memory else sqlite3.connect(str(self.db.path), timeout=30)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            clause, args = ("id=?", (scan_id,)) if scan_id is not None else ("1", ())
            states = conn.execute(
                f"SELECT state FROM survey_band_scans WHERE {clause}",
                args,
            ).fetchall()
            if any(row[0] in {"running", "paused"} for row in states):
                raise ScanWriterError("stop the active band scan before deleting reports")
            deleted = conn.execute(f"DELETE FROM survey_band_scans WHERE {clause}", args).rowcount
            conn.commit()
            self._legacy_cache.clear()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            if not self._memory:
                conn.close()

    async def adelete(self, scan_id=None):
        if self._memory:
            return self._delete_reports(scan_id)
        return await asyncio.to_thread(self._delete_reports, scan_id)

    def latest(self, limit=20):
        def read(conn):
            rows = conn.execute(
                "SELECT id,config_json,state,result_count,span_count,covered_count,"
                "uncovered_count,started_at,updated_at FROM survey_band_scans "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out = []
            for row in rows:
                config = json.loads(row["config_json"])
                total = len(config.pop("centers_hz", []))
                out.append(
                    {
                        "id": row["id"],
                        "config": config,
                        "total_spans": total,
                        "state": row["state"],
                        "result_count": row["result_count"],
                        "span_count": row["span_count"],
                        "covered_count": row["covered_count"],
                        "uncovered_count": row["uncovered_count"],
                        "started_at": row["started_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            return out

        return self._with_read(read)

    def interrupt_unfinished(self):
        rows = self.db.conn.execute(
            "SELECT id,payload_json FROM survey_band_scans WHERE state IN ('running','paused')"
        ).fetchall()
        for row in rows:
            meta = json.loads(row["payload_json"] or "{}")
            normalized = self.db.conn.execute(
                "SELECT span_index,data_json FROM survey_scan_spans WHERE scan_id=? "
                "ORDER BY span_index",
                (row["id"],),
            ).fetchall()
            spans = (
                [json.loads(item["data_json"]) for item in normalized]
                if normalized
                else meta.get("spans", [])
            )
            for index, span in enumerate(spans):
                if span.get("state") not in {"covered", "uncovered"}:
                    span.update(state="uncovered", reason="application restarted during capture")
                    if normalized:
                        self.db.conn.execute(
                            "UPDATE survey_scan_spans SET data_json=? "
                            "WHERE scan_id=? AND span_index=?",
                            (_json(span), row["id"], normalized[index]["span_index"]),
                        )
            covered = sum(span.get("state") == "covered" for span in spans)
            uncovered = sum(span.get("state") == "uncovered" for span in spans)
            meta.update(
                state="interrupted",
                ended_at=time.time(),
                interruption_reason="application restarted during scan",
                coverage_complete=False,
                span_count=len(spans),
                covered_count=covered,
                uncovered_count=uncovered,
            )
            self.db.conn.execute(
                "UPDATE survey_band_scans SET state='interrupted',payload_json=?,updated_at=?, "
                "span_count=?,covered_count=?,uncovered_count=? WHERE id=?",
                (_json(meta), time.time(), len(spans), covered, uncovered, row["id"]),
            )
        self.db.conn.commit()
