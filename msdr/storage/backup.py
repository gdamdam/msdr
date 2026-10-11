"""Versioned full-database backups and portable signal evidence bundles."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sqlite3
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database
from msdr.storage.repos import SignalRepo

BACKUP_FORMAT = "msdr/backup/1"
EVIDENCE_FORMAT = "msdr/evidence/1"
_MANIFEST = "manifest.json"


class RestoreReport(BaseModel):
    verified: bool
    table_counts: dict[str, int]
    media_count: int = 0


def _tables(conn: sqlite3.Connection) -> list[str]:
    # Virtual FTS tables and their shadow tables are derived indexes. SQLite
    # rebuilds these from their source tables and they may contain opaque blobs.
    return [
        r[1]
        for r in conn.execute("PRAGMA table_list")
        if r[2] == "table" and r[1] != "schema_version" and not r[1].startswith("sqlite_")
    ]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _json_safe(value):
    if isinstance(value, bytes):
        return {"$msdr_blob": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _from_json_safe(value):
    if isinstance(value, dict) and set(value) == {"$msdr_blob"}:
        return base64.b64decode(value["$msdr_blob"], validate=True)
    if isinstance(value, dict):
        return {key: _from_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_json_safe(item) for item in value]
    return value


def _rows(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    result = {}
    for table in _tables(conn):
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        primary_key = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        order = (
            " ORDER BY " + ",".join('"' + c.replace('"', '""') + '"' for c in primary_key)
            if primary_key
            else ""
        )
        values = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"{order}')]
        if not primary_key:
            values.sort(key=_canonical)
        result[table] = values
    return result


def _snapshot_rows(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Read all tables from one SQLite snapshot without committing caller work."""
    conn.execute("SAVEPOINT msdr_backup_snapshot")
    try:
        snapshot = _rows(conn)
    except Exception:
        conn.execute("ROLLBACK TO msdr_backup_snapshot")
        conn.execute("RELEASE msdr_backup_snapshot")
        raise
    conn.execute("RELEASE msdr_backup_snapshot")
    return snapshot


def _media_candidates(rows: dict[str, list[dict]], roots: list[Path]) -> dict[str, Path]:
    """Return referenced regular files contained beneath one of the supplied roots."""
    allowed = [p.resolve() for p in roots]
    found: dict[str, Path] = {}
    for table_rows in rows.values():
        for row in table_rows:
            for key, raw in row.items():
                if key not in {"path", "image_path"} or not raw:
                    continue
                candidate = Path(str(raw)).expanduser()
                candidates = [candidate]
                if candidate.is_dir():
                    candidates = sorted(p for p in candidate.rglob("*") if p.is_file())
                for path in candidates:
                    try:
                        resolved = path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if not resolved.is_file() or not any(
                        resolved == root or root in resolved.parents for root in allowed
                    ):
                        continue
                    found[str(resolved)] = resolved
    return found


def create_backup(
    db: Database,
    destination: Path | str,
    *,
    include_media: bool = False,
    media_roots: list[Path | str] | None = None,
) -> dict:
    """Write a complete, versioned SQLite data backup as a ZIP archive.

    Media is opt-in and limited to referenced files beneath `media_roots`.
    The returned manifest contains table row counts and content digests for
    later restore verification.
    """
    rows = _snapshot_rows(db.conn)
    media = _media_candidates(rows, [Path(p) for p in (media_roots or [])]) if include_media else {}
    encoded_rows = _canonical(rows)
    manifest = {
        "format": BACKUP_FORMAT,
        "tables": {
            name: {"rows": len(items), "sha256": _digest(_canonical(items))}
            for name, items in rows.items()
        },
        "data_sha256": _digest(encoded_rows),
        "media": {},
    }
    media_files = {}
    resolved_roots = [Path(p).resolve() for p in (media_roots or [])]
    path_remaps = {}
    for source, path in sorted(media.items()):
        root_index = next(
            (i for i, root in enumerate(resolved_roots) if path == root or root in path.parents),
            None,
        )
        if root_index is None:
            continue
        name = f"media/root{root_index}/{path.relative_to(resolved_roots[root_index]).as_posix()}"
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
                size += len(chunk)
        media_files[name] = path
        manifest["media"][source] = {"archive": name, "sha256": hasher.hexdigest(), "bytes": size}
    packed_paths = set(manifest["media"])
    if include_media:
        for table_rows in rows.values():
            for row in table_rows:
                for column in ("path", "image_path"):
                    raw = row.get(column)
                    if not raw:
                        continue
                    try:
                        resolved = Path(str(raw)).expanduser().resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    packed = str(resolved) in packed_paths or any(
                        candidate.startswith(str(resolved).rstrip("/") + "/")
                        for candidate in packed_paths
                    )
                    if packed:
                        root_index = next(
                            (
                                i
                                for i, root in enumerate(resolved_roots)
                                if resolved == root or root in resolved.parents
                            ),
                            None,
                        )
                        if root_index is not None:
                            relative = resolved.relative_to(resolved_roots[root_index])
                            suffix = "" if relative.as_posix() == "." else f"/{relative.as_posix()}"
                            path_remaps[str(resolved)] = f"media/root{root_index}{suffix}"
    manifest["path_remaps"] = path_remaps
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_MANIFEST, _canonical(manifest))
        archive.writestr("database.json", encoded_rows)
        for name, path in media_files.items():
            archive.write(path, name)
    # Media can change between the checksum pass and zip streaming (for
    # example, while a recording is being finalized). Never return a backup
    # whose declared digests do not match its archived bytes.
    try:
        with zipfile.ZipFile(dest, "r") as archive:
            for source, item in manifest["media"].items():
                hasher = hashlib.sha256()
                size = 0
                with archive.open(item["archive"]) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
                if size != item["bytes"] or hasher.hexdigest() != item["sha256"]:
                    raise ValueError(f"media changed while creating backup: {source}")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return manifest


def _validate_archive(archive: zipfile.ZipFile) -> tuple[dict, dict[str, list[dict]]]:
    manifest = json.loads(archive.read(_MANIFEST))
    if manifest.get("format") != BACKUP_FORMAT:
        raise ValueError(f"unsupported backup format: {manifest.get('format')!r}")
    rows = json.loads(archive.read("database.json"))
    if _digest(_canonical(rows)) != manifest.get("data_sha256"):
        raise ValueError("backup database checksum mismatch")
    for table, expected in manifest.get("tables", {}).items():
        values = rows.get(table)
        if not isinstance(values, list) or len(values) != expected.get("rows"):
            raise ValueError(f"backup row count mismatch for {table}")
        if _digest(_canonical(values)) != expected.get("sha256"):
            raise ValueError(f"backup checksum mismatch for {table}")
    return manifest, _from_json_safe(rows)


def restore_backup(
    db: Database, source: Path | str, *, media_root: Path | str | None = None
) -> RestoreReport:
    """Replace application data atomically and verify rows before commit.

    Optional media is validated and staged in a unique directory first. Paths
    in the restored database are remapped to that directory within the same
    SQLite transaction, so a failed restore leaves the previous DB intact.
    """
    with zipfile.ZipFile(source, "r") as archive:
        manifest, rows = _validate_archive(archive)
        media_items = manifest.get("media", {})
        archive_paths = set()
        for item in media_items.values():
            name = item.get("archive", "")
            parts = Path(name).parts
            if not name.startswith("media/") or ".." in parts or name in archive_paths:
                raise ValueError("invalid media archive path")
            archive_paths.add(name)
            if name not in archive.namelist():
                raise ValueError(f"missing media entry: {name}")
        valid_remaps = set(archive_paths)
        for name in archive_paths:
            parent = Path(name).parent
            while str(parent) != ".":
                valid_remaps.add(parent.as_posix())
                parent = parent.parent
        for original, remap in manifest.get("path_remaps", {}).items():
            if ".." in Path(remap).parts or remap not in valid_remaps:
                raise ValueError(f"invalid media path remap: {original}")
        expected_tables = set(_tables(db.conn))
        if set(rows) != expected_tables:
            raise ValueError("backup schema does not match the initialized database")
        conn = db.conn
        staging = None
        if media_root is not None and media_items:
            staging = Path(media_root) / "media" / f"restore-{uuid.uuid4().hex}"
            try:
                for original, item in media_items.items():
                    archive_name = item["archive"]
                    relative = Path(archive_name).relative_to("media")
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    hasher = hashlib.sha256()
                    size = 0
                    with archive.open(archive_name) as source_stream, destination.open("wb") as out:
                        for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                            hasher.update(chunk)
                            size += len(chunk)
                            out.write(chunk)
                    if size != item.get("bytes") or hasher.hexdigest() != item.get("sha256"):
                        raise ValueError(f"media checksum mismatch: {original}")
            except Exception:
                if staging:
                    shutil.rmtree(staging, ignore_errors=True)
                raise

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            for table in _tables(conn):
                conn.execute(f'DELETE FROM "{table}"')
            for table in _tables(conn):
                columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                if not rows[table]:
                    continue
                quoted = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
                placeholders = ",".join("?" for _ in columns)
                conn.executemany(
                    f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                    [tuple(row.get(col) for col in columns) for row in rows[table]],
                )
            # Validate restored bytes and relational integrity while rollback
            # is still possible. Remap eligible media refs before final check.
            restored = _rows(conn)
            if _digest(_canonical(restored)) != manifest["data_sha256"]:
                raise ValueError("restored database verification failed")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError("restored database contains foreign-key violations")

            expected_rows = copy.deepcopy(rows)
            if staging is not None:
                for table, table_rows in rows.items():
                    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                    columns = {r[1] for r in info}
                    path_columns = [c for c in ("path", "image_path") if c in columns]
                    primary_key = [
                        r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])
                    ]
                    for index, row in enumerate(table_rows):
                        for column in path_columns:
                            original = row.get(column)
                            source_path = (
                                str(Path(original).expanduser().resolve()) if original else ""
                            )
                            archive_path = manifest.get("path_remaps", {}).get(source_path)
                            if not archive_path:
                                continue
                            local_relative = Path(archive_path).relative_to("media")
                            local_path = str(staging / local_relative)
                            expected_rows[table][index][column] = local_path
                            if primary_key:
                                where = " AND ".join(f'"{key}" IS ?' for key in primary_key)
                                values = [row[key] for key in primary_key]
                            else:
                                # No path-bearing table currently lacks a key;
                                # guard instead of updating an arbitrary row.
                                raise ValueError(f"cannot safely remap media path in {table}")
                            conn.execute(
                                f'UPDATE "{table}" SET "{column}"=? WHERE {where}',
                                (local_path, *values),
                            )
                restored = _rows(conn)
                if _canonical(restored) != _canonical(expected_rows):
                    raise ValueError("restored media path verification failed")
            conn.commit()
        except Exception:
            conn.rollback()
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return RestoreReport(
        verified=True,
        table_counts={k: len(v) for k, v in rows.items()},
        media_count=len(media_items) if staging else 0,
    )


_SIGMF_SAMPLE_BYTES = {"ci8": 2, "ci16_le": 4, "ci32_le": 8, "cf32_le": 8}


def _iq_clip(repo: ObservationRepo, observation_ids: list[str], max_samples: int):
    """Return (bytes, SigMF metadata) only when source format is known."""
    for observation_id in observation_ids:
        for ref in repo.refs(observation_id):
            if not ref.available:
                continue
            path = Path(ref.path).expanduser()
            if path.is_dir():
                if ref.run_index is not None:
                    candidates = sorted(path.glob(f"run-{ref.run_index:02d}_*.sigmf-data"))
                else:
                    candidates = sorted(path.glob("*.sigmf-data"))
            elif path.name.endswith(".sigmf-data"):
                candidates = [path]
            else:
                candidates = []
            for candidate in candidates:
                try:
                    source_meta = json.loads(candidate.with_suffix(".sigmf-meta").read_text())
                    global_meta = source_meta.get("global", {})
                    datatype = global_meta.get("core:datatype")
                    sample_bytes = _SIGMF_SAMPLE_BYTES.get(datatype)
                    sample_rate = global_meta.get("core:sample_rate")
                    if (
                        not sample_bytes
                        or not isinstance(sample_rate, (int, float))
                        or sample_rate <= 0
                    ):
                        continue
                    start = max(0, ref.sample_start or 0)
                    end = ref.sample_end if ref.sample_end is not None else start + max_samples
                    end = min(end, start + max_samples)
                    with candidate.open("rb") as stream:
                        stream.seek(start * sample_bytes)
                        clip = stream.read(max(0, end - start) * sample_bytes)
                    if clip:
                        captures = sorted(
                            source_meta.get("captures", []),
                            key=lambda item: int(item.get("core:sample_start", 0)),
                        )
                        active = [
                            item
                            for item in captures
                            if int(item.get("core:sample_start", 0)) <= start
                        ]
                        selected = []
                        if active:
                            selected.append((active[-1], start))
                        selected.extend(
                            (item, int(item.get("core:sample_start", 0)))
                            for item in captures
                            if start < int(item.get("core:sample_start", 0)) < end
                        )
                        clip_captures = []
                        for item, original_start in selected:
                            capture = {
                                "core:sample_start": original_start - start,
                                **{
                                    key: value
                                    for key, value in item.items()
                                    if key != "core:sample_start"
                                },
                            }
                            date_text = capture.get("core:datetime")
                            if date_text and original_start == start:
                                try:
                                    date = datetime.fromisoformat(
                                        str(date_text).replace("Z", "+00:00")
                                    )
                                    if date.tzinfo is None:
                                        date = date.replace(tzinfo=UTC)
                                    offset = start - int(item.get("core:sample_start", 0))
                                    date += timedelta(seconds=offset / sample_rate)
                                    capture["core:datetime"] = date.isoformat(
                                        timespec="microseconds"
                                    ).replace("+00:00", "Z")
                                except ValueError:
                                    capture.pop("core:datetime", None)
                            clip_captures.append(capture)
                        clip_meta = {
                            "global": global_meta,
                            "captures": clip_captures,
                            "annotations": [
                                {**item, "core:sample_start": item["core:sample_start"] - start}
                                for item in source_meta.get("annotations", [])
                                if "core:sample_start" in item
                                and start <= item["core:sample_start"] < end
                            ],
                        }
                        actual_end = start + len(clip) // sample_bytes
                        return (
                            clip,
                            clip_meta,
                            {
                                "source_ref_id": ref.id,
                                "source_name": ref.name,
                                "run_index": ref.run_index,
                                "sample_start": start,
                                "sample_end": actual_end,
                            },
                        )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
    return None


def create_evidence_bundle(
    db: Database,
    signal_id: str,
    destination: Path | str,
    *,
    include_iq_clip: bool = True,
    max_iq_samples: int = 250_000,
) -> dict:
    """Export a signal's observations, claims, decoder versions, notes and short IQ clip."""
    signal = SignalRepo(db).get(signal_id)
    if signal is None:
        raise KeyError(signal_id)
    conn = db.conn
    observations = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM observations WHERE signal_id=? ORDER BY start_ts, id", (signal_id,)
        )
    ]
    ids = [r["id"] for r in observations]
    claims, events, refs = [], [], []
    if ids:
        marks = ",".join("?" for _ in ids)
        claims = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM claims WHERE observation_id IN ({marks}) ORDER BY created_at, id",
                ids,
            )
        ]
        events = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM decode_events WHERE observation_id IN ({marks}) ORDER BY ts, id",
                ids,
            )
        ]
        refs = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM recording_refs WHERE observation_id IN ({marks}) ORDER BY id", ids
            )
        ]
    versions = sorted(
        {(r["producer"], r.get("producer_version") or "") for r in claims if r.get("producer")},
        key=lambda item: (str(item[0]), str(item[1])),
    )
    payload = {
        "format": EVIDENCE_FORMAT,
        "signal": signal.model_dump(mode="json"),
        "observations": observations,
        "claims": claims,
        "decode_events": events,
        "recording_refs": refs,
        "decoder_versions": [dict(producer=p, version=v) for p, v in versions],
    }
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    clip_result = (
        _iq_clip(ObservationRepo(db), ids, max_iq_samples)
        if include_iq_clip and max_iq_samples > 0
        else None
    )
    if clip_result:
        clip, clip_meta, clip_source = clip_result
        payload["iq_clip"] = {
            "path": "iq/clip.sigmf-data",
            "metadata_path": "iq/clip.sigmf-meta",
            "bytes": len(clip),
            "sha256": _digest(clip),
            "sample_rate_hz": clip_meta["global"]["core:sample_rate"],
            "datatype": clip_meta["global"]["core:datatype"],
            **clip_source,
        }
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence.json", _canonical(payload))
        if clip_result:
            archive.writestr("iq/clip.sigmf-data", clip)
            archive.writestr("iq/clip.sigmf-meta", _canonical(clip_meta))
    return payload
