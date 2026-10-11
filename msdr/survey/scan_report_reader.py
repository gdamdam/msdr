"""Isolate large legacy JSON parsing from live DSP's Python interpreter.

A lazily started worker retains at most one legacy payload. Its flattened
observation index holds references, not duplicate observation dictionaries.
No conversion writes to the user's database.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

_cache_key = None
_cache_payload = None
_cache_observations = None
_cache_summary = None


def _connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _base(row):
    return {
        "id": row["id"],
        "config": json.loads(row["config_json"]),
        "state": row["state"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
    }


def _legacy(conn, row):
    global _cache_key, _cache_payload, _cache_observations, _cache_summary
    key = (conn.execute("PRAGMA database_list").fetchone()[2], row["id"], row["updated_at"])
    if key != _cache_key:
        # Release the previous payload before allocating the next large report.
        _cache_key = _cache_payload = _cache_observations = _cache_summary = None
        payload = conn.execute(
            "SELECT payload_json FROM survey_band_scans WHERE id=?", (row["id"],)
        ).fetchone()[0]
        _cache_payload = json.loads(payload or "{}")
        _cache_key = key
    return _cache_payload


def _legacy_summary(conn, row):
    # Deferred: scan_storage imports ScanReportReader at module load, so a
    # top-level import here would be circular. By call time both modules are
    # already fully initialized (same pattern as the deferred import below).
    from msdr.survey.scan_storage import _identity

    global _cache_summary
    payload = _legacy(conn, row)
    if _cache_summary is not None:
        return _cache_summary
    results = []
    total = 0
    for result in payload.get("results", []):
        observations = result.get("observations", [])
        total += len(observations)
        numeric = [o for o in observations if _number(o.get("snr_db"))]
        peak = max(numeric, key=lambda o: o["snr_db"]) if numeric else None
        representatives = []
        for sample in observations[:1] + ([peak] if peak else []) + observations[-1:]:
            if sample not in representatives:
                representatives.append(sample)
        # Shared with BandScanRepo._summary's legacy branch so both paths
        # agree on identity extraction (rds/recognized/family/confirmed).
        identities = sorted({_identity(o) or "" for o in observations})
        results.append(
            {
                **{k: v for k, v in result.items() if k != "observations"},
                "observations": representatives,
                "observation_count": len(observations),
                "peak_snr_db": peak["snr_db"] if peak else None,
                "snr_max_db": peak["snr_db"] if peak else None,
                "identifications": identities,
            }
        )
    _cache_summary = {
        **_base(row),
        "payload": {**payload, "results": results},
        "observation_count": total,
        "compact": True,
        "legacy": True,
    }
    return _cache_summary


def _legacy_page(conn, row, offset=0, limit=100, frequency_hz=None, min_peak_snr_db=None):
    global _cache_observations
    payload = _legacy(conn, row)
    if _cache_observations is None:
        _cache_observations = [
            (result["frequency_hz"], observation)
            for result in payload.get("results", [])
            for observation in result.get("observations", [])
        ]
        _cache_observations.sort(key=lambda item: item[1].get("observed_at") or 0, reverse=True)
    observations = _cache_observations
    if frequency_hz is not None:
        observations = [item for item in observations if item[0] == frequency_hz]
    if min_peak_snr_db is not None:
        eligible = {
            result["frequency_hz"]
            for result in _legacy_summary(conn, row)["payload"]["results"]
            if _number(result.get("peak_snr_db")) and result["peak_snr_db"] >= min_peak_snr_db
        }
        observations = [item for item in observations if item[0] in eligible]
    return {
        "id": row["id"],
        "total": len(observations),
        "offset": offset,
        "limit": limit,
        "observations": [
            {**sample, "frequency_hz": hz} for hz, sample in observations[offset : offset + limit]
        ],
        "legacy": True,
    }


def _read(path, operation, scan_id, kwargs):
    from msdr.storage.db import Database
    from msdr.survey.scan_storage import BandScanRepo

    conn = _connect(path)
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT id,config_json,state,started_at,updated_at FROM survey_band_scans WHERE id=?",
            (scan_id,),
        ).fetchone()
        if row is None:
            return None
        # Read-only construction creates neither a writer thread nor a schema migration.
        repo = BandScanRepo(Database(path), start_writer=False)
        normalized = repo._is_compact(conn, scan_id)
        if not normalized:
            if operation == "summary":
                return _legacy_summary(conn, row)
            if operation == "observations":
                return _legacy_page(conn, row, **kwargs)
            if operation == "get":
                return {**_base(row), "payload": _legacy(conn, row)}
            if operation == "export":
                raw = (
                    conn.execute(
                        "SELECT payload_json FROM survey_band_scans WHERE id=?", (scan_id,)
                    ).fetchone()[0]
                    or "{}"
                )
                # Preserve the stored payload exactly, including every legacy observation.
                prefix = json.dumps(_base(row), separators=(",", ":"))[:-1]
                return (prefix + ',"payload":' + raw + "}").encode("utf-8")
        if operation == "export":
            return json.dumps(
                repo._get(conn, scan_id), separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        method = {
            "summary": repo._summary,
            "observations": repo._observations,
            "spectrum": repo._spectrum,
            "get": repo._get,
        }[operation]
        if operation == "observations":
            return method(
                conn,
                scan_id,
                kwargs.get("offset", 0),
                kwargs.get("limit", 100),
                kwargs.get("frequency_hz"),
                kwargs.get("min_peak_snr_db"),
            )
        return method(conn, scan_id)
    finally:
        conn.close()


class ScanReportReader:
    """One read worker per repository, created only when a report is opened."""

    def __init__(self, path):
        self.path = str(path)
        self._pool = None

    def _new_pool(self):
        return ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )

    async def read(self, operation, scan_id, **kwargs):
        if self._pool is None:
            self._pool = self._new_pool()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                self._pool,
                _read,
                self.path,
                operation,
                scan_id,
                kwargs,
            )
        except BrokenProcessPool:
            # A dead worker poisons this executor permanently; replace it
            # and retry once rather than failing every later read.
            broken, self._pool = self._pool, None
            broken.shutdown(wait=False)
            self._pool = self._new_pool()
            return await asyncio.get_running_loop().run_in_executor(
                self._pool,
                _read,
                self.path,
                operation,
                scan_id,
                kwargs,
            )

    async def aclose(self):
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)

    def close(self):
        if self._pool is not None:
            pool, self._pool = self._pool, None
            pool.shutdown(wait=True, cancel_futures=True)
