"""One-time import of pre-S2 history (finds, decode_events) as legacy
Observations.

The old tables are aggregates: a find is one row per frequency merged over
many scan runs, and decode events carry no reception boundaries. So each
imported Observation is flagged `legacy=1`, says in its provenance what it
summarises, and takes its start/end only from the rows' own timestamps --
nothing is invented per reception beyond the events themselves.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from msdr.contracts.evidence import Claim, Measurement
from msdr.observations.models import Observation
from msdr.observations.recorder import DECODE_TOLERANCE_HZ, decoder_spec
from msdr.observations.repo import ObservationRepo
from msdr.storage.db import Database
from msdr.storage.repos import SettingsRepo

DONE_KEY = "observations.legacy_import_done"
# A decoder's events on one frequency with gaps up to this long are taken
# as one listening episode.
GROUP_GAP_S = 300.0


def legacy_import_done(db: Database) -> bool:
    return bool(SettingsRepo(db).get(DONE_KEY, False))


def _mark_done(db: Database) -> None:
    SettingsRepo(db).set(DONE_KEY, True)


def backup_db(path: Path | str, *, now: datetime | None = None) -> Path | None:
    """Copy the SQLite file to `<path>.bak-<YYYYmmdd-HHMMSS>` and return it.
    None for ":memory:" or a file that doesn't exist yet (nothing to lose).

    Uses SQLite's online backup API rather than a byte copy: the app runs in
    WAL mode, so recent commits may still live in the -wal file and a plain
    copy of the main file would silently miss them."""
    if str(path) == ":memory:":
        return None
    src_path = Path(path)
    if not src_path.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = src_path.with_name(f"{src_path.name}.bak-{stamp}")
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error:
        # Fall back to a byte copy (e.g. a locked file) rather than no backup.
        shutil.copy2(src_path, dest)
    finally:
        src.close()
    return dest


def _dt(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC)


def import_legacy(db: Database, repo: ObservationRepo) -> dict:
    """Import once (settings key `observations.legacy_import_done`). Returns
    counts; a repeat call imports nothing."""
    counts = {"finds": 0, "decode_events": 0, "decode_groups": 0, "claims": 0, "skipped": False}
    if legacy_import_done(db):
        counts["skipped"] = True
        return counts
    conn = db.conn

    # fetchall(): the loop updates `finds`, which must not happen under a
    # live SELECT cursor on the same table.
    finds = conn.execute(
        "SELECT * FROM finds WHERE observation_id IS NULL ORDER BY first_ts"
    ).fetchall()
    for row in finds:
        measurements = []
        if row["peak_snr_db"] is not None:
            measurements.append(
                Measurement(
                    name="peak_snr",
                    value=float(row["peak_snr_db"]),
                    unit="dB",
                    method="scanner_peak_snr",
                    reference="max over merged scanner hits",
                )
            )
        obs = repo.add(
            Observation(
                signal_id=row["signal_id"],
                find_id=row["id"],
                start_ts=row["first_ts"],
                end_ts=row["last_ts"],
                reported_hz=row["frequency_hz"],
                bandwidth_hz=row["bandwidth_hz"],
                receiver={"demodulator": row["demodulator"]} if row["demodulator"] else None,
                measurements=measurements,
                legacy=True,
                provenance=(
                    f"migrated: finds (frequency aggregate, seen_count={row['seen_count']})"
                ),
                closed=True,
            )
        )
        conn.execute("UPDATE finds SET observation_id = ? WHERE id = ?", (obs.id, row["id"]))
        conn.commit()
        counts["finds"] += 1

    for group in _group_events(
        conn.execute(
            "SELECT id, ts, frequency_hz, signal_id, decoder FROM decode_events "
            "WHERE observation_id IS NULL ORDER BY decoder, ts"
        ).fetchall()
    ):
        first, last = group[0], group[-1]
        obs = repo.add(
            Observation(
                signal_id=next((e["signal_id"] for e in group if e["signal_id"]), None),
                start_ts=first["ts"],
                end_ts=last["ts"],
                reported_hz=first["frequency_hz"],
                legacy=True,
                provenance=f"migrated: decode_events ({len(group)} events)",
                closed=True,
            )
        )
        repo.add_claim(
            Claim(
                **decoder_spec(first["decoder"]),
                observation_id=obs.id,
                observed_start=_dt(first["ts"]),
                observed_end=_dt(last["ts"]),
                support=[f"{len(group)} decode events (migrated)"],
            )
        )
        conn.executemany(
            "UPDATE decode_events SET observation_id = ? WHERE id = ?",
            [(obs.id, e["id"]) for e in group],
        )
        conn.commit()
        counts["decode_groups"] += 1
        counts["decode_events"] += len(group)
        counts["claims"] += 1

    _mark_done(db)
    return counts


def _group_events(rows: list) -> list[list]:
    """Group rows (sorted by decoder, ts) by (decoder, frequency within
    DECODE_TOLERANCE_HZ of the group's first event, gap <= GROUP_GAP_S).
    Several groups per decoder can be open at once (a decoder alternating
    between two channels keeps two episodes)."""
    groups: list[list] = []
    open_groups: list[list] = []
    decoder = object()
    for row in rows:
        if row["decoder"] != decoder:
            decoder = row["decoder"]
            open_groups = []
        target = None
        for g in open_groups:
            if row["ts"] - g[-1]["ts"] > GROUP_GAP_S:
                continue
            f0, f = g[0]["frequency_hz"], row["frequency_hz"]
            same = (f0 is None and f is None) or (
                f0 is not None and f is not None and abs(f - f0) <= DECODE_TOLERANCE_HZ
            )
            if same:
                target = g
                break
        if target is None:
            target = [row]
            groups.append(target)
            open_groups.append(target)
        else:
            target.append(row)
        # Drop groups that can no longer accept anything (rows are ts-ordered).
        open_groups = [g for g in open_groups if row["ts"] - g[-1]["ts"] <= GROUP_GAP_S]
    return groups
