"""`HistoryStore`: the SQLite side of device history (docs/history.md).

A separate database file from msdr.db (same `Database` class, its own
migrations directory), written with additive upserts so repeated flushes of
the same minute sum instead of overwrite.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from msdr.history.buckets import Drained, minute_of
from msdr.storage.db import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

RECEIVERS = ("wifi", "ble")
PRIVATE_KINDS = ("rpa", "nrpa", "random")
BUCKET_CHOICES = (60, 300, 900, 3600)
#: Auto bucket: the smallest choice that keeps the range under this many columns.
MAX_COLUMNS = 360

#: Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): a finer bucket set
#: than BUCKET_CHOICES since a per-second row is useful at short ranges.
ACTIVITY_BUCKET_CHOICES = (1, 5, 15, 60, 300)
# A single wifi_activity row's listened_s is credited by one supervisor tick
# at a time (msdr.wifi.activity.TICK_CREDIT_CAP_S caps that at 2 s); capped
# again here so a flush replay or clock jitter can never inflate a single
# second's listened time past what a tick could plausibly report.
_ACTIVITY_SECOND_CAP_S = 2.0


def choose_bucket(span_s: float, requested: int | None = None) -> int:
    if requested in BUCKET_CHOICES:
        return requested
    for b in BUCKET_CHOICES:
        if span_s / b <= MAX_COLUMNS:
            return b
    return BUCKET_CHOICES[-1]


def choose_activity_bucket(span_s: float, requested: int | None = None) -> int:
    if requested in ACTIVITY_BUCKET_CHOICES:
        return requested
    for b in ACTIVITY_BUCKET_CHOICES:
        if span_s / b <= MAX_COLUMNS:
            return b
    return ACTIVITY_BUCKET_CHOICES[-1]


def _avg(total: float, n: int) -> float | None:
    return round(total / n, 1) if n else None


class HistoryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self.db = Database(path, migrations_dir=MIGRATIONS_DIR)

    def connect(self) -> None:
        self.db.connect()

    def close(self) -> None:
        self.db.close()

    @property
    def conn(self):
        return self.db.conn

    # --- writes ------------------------------------------------------------

    def write(self, drained: Drained) -> None:
        if not drained:
            return
        conn = self.conn
        conn.executemany(
            "INSERT INTO buckets"
            " (receiver, ident, minute, reports, sig_min, sig_max, sig_sum, sig_n)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (receiver, ident, minute) DO UPDATE SET"
            " reports = reports + excluded.reports,"
            " sig_min = CASE WHEN sig_min IS NULL THEN excluded.sig_min"
            "   WHEN excluded.sig_min IS NULL THEN sig_min ELSE MIN(sig_min, excluded.sig_min) END,"
            " sig_max = CASE WHEN sig_max IS NULL THEN excluded.sig_max"
            "   WHEN excluded.sig_max IS NULL THEN sig_max ELSE MAX(sig_max, excluded.sig_max) END,"
            " sig_sum = sig_sum + excluded.sig_sum,"
            " sig_n = sig_n + excluded.sig_n",
            [
                (r, i, m, b.reports, b.sig_min, b.sig_max, b.sig_sum, b.sig_n)
                for (r, i, m), b in drained.buckets.items()
            ],
        )
        conn.executemany(
            "INSERT INTO devices (receiver, ident, name, kind, signal_kind, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (receiver, ident) DO UPDATE SET"
            " name = COALESCE(excluded.name, name),"
            " kind = COALESCE(excluded.kind, kind),"
            " signal_kind = COALESCE(excluded.signal_kind, signal_kind),"
            " first_seen = MIN(first_seen, excluded.first_seen),"
            " last_seen = MAX(last_seen, excluded.last_seen)",
            [
                (r, i, m.name, m.kind, m.signal_kind, m.first_seen, m.last_seen)
                for (r, i), m in drained.devices.items()
            ],
        )
        conn.commit()

    def add_coverage(self, receiver: str, ts: float, channel: int, seconds: float) -> None:
        """Seconds of listening ending at `ts`, split across minute
        boundaries so each minute holds only its own share."""
        rows: list[tuple[str, int, int, float]] = []
        end = ts
        start = ts - seconds
        while end > start:
            # The minute that contains the instant just before `end`; an exact
            # boundary belongs to the previous minute (1e-9 is lost at 1.79e9).
            minute = minute_of(end)
            if minute >= end:
                minute -= 60
            piece = end - max(start, minute)
            rows.append((receiver, minute, channel, piece))
            end = minute
        self.conn.executemany(
            "INSERT INTO coverage (receiver, minute, channel, seconds) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (receiver, minute, channel) DO UPDATE SET"
            " seconds = MIN(60.0, seconds + excluded.seconds)",
            rows,
        )
        self.conn.commit()

    def write_activity(self, cells) -> None:
        """`cells`: a `{(second, channel): cell}` mapping (an
        `ActivityAccumulator.drain().cells`, msdr/wifi/activity.py) -- taken
        structurally rather than importing that type here, so this generic
        history store stays receiver-agnostic (see `write` above)."""
        if not cells:
            return
        conn = self.conn
        conn.executemany(
            "INSERT INTO wifi_activity (second, channel, packets, listened_s, sig_sum, sig_n)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (second, channel) DO UPDATE SET"
            " packets = packets + excluded.packets,"
            f" listened_s = MIN({_ACTIVITY_SECOND_CAP_S}, listened_s + excluded.listened_s),"
            " sig_sum = sig_sum + excluded.sig_sum,"
            " sig_n = sig_n + excluded.sig_n",
            [
                (second, channel, c.packets, c.listened_s, c.sig_sum, c.sig_n)
                for (second, channel), c in cells.items()
            ],
        )
        conn.commit()

    def prune_activity(self, retention_hours: int, *, now: float | None = None) -> int:
        cutoff = int((now or time.time()) - retention_hours * 3600)
        n = self.conn.execute(
            "DELETE FROM wifi_activity WHERE second < ?", (cutoff,)
        ).rowcount
        self.conn.commit()
        return n

    def prune(self, retention_days: int, *, now: float | None = None) -> int:
        cutoff = minute_of((now or time.time()) - retention_days * 86400)
        conn = self.conn
        n = conn.execute("DELETE FROM buckets WHERE minute < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM coverage WHERE minute < ?", (cutoff,))
        conn.execute("DELETE FROM devices WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return n

    def clear(self) -> None:
        for table in ("buckets", "devices", "coverage", "wifi_activity"):
            self.conn.execute(f"DELETE FROM {table}")  # noqa: S608 -- fixed table names
        self.conn.commit()

    # --- reads -------------------------------------------------------------

    def status(self) -> dict:
        conn = self.conn
        span = conn.execute("SELECT MIN(minute) AS lo, MAX(minute) AS hi FROM buckets").fetchone()
        size = None
        if str(self.path) != ":memory:":
            p = Path(self.path)
            size = sum(f.stat().st_size for f in (p, Path(f"{p}-wal")) if f.exists())
        return {
            "db_path": str(self.path),
            "size_bytes": size,
            "oldest": span["lo"],
            "newest": span["hi"] + 60 if span["hi"] is not None else None,
            "devices": conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "buckets": conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0],
        }

    def _coverage(self, receivers, start: int, end: int, bucket: int) -> dict:
        out: dict[str, list] = {r: [] for r in receivers}
        rows = self.conn.execute(
            "SELECT receiver, (minute / ?) * ? AS t, channel, SUM(seconds) AS s FROM coverage"
            f" WHERE minute >= ? AND minute < ? AND receiver IN ({','.join('?' * len(receivers))})"
            " GROUP BY receiver, t, channel ORDER BY t",
            (bucket, bucket, start, end, *receivers),
        ).fetchall()
        merged: dict[tuple[str, int], list] = {}
        for row in rows:
            slot = merged.setdefault((row["receiver"], row["t"]), [0.0, []])
            slot[0] += row["s"]
            if row["channel"]:
                slot[1].append(row["channel"])
        for (receiver, t), (seconds, channels) in sorted(merged.items(), key=lambda kv: kv[0][1]):
            seconds = round(min(seconds, float(bucket)), 1)
            out[receiver].append(
                [t, seconds, sorted(channels)] if receiver == "wifi" else [t, seconds]
            )
        return out

    def covered_seconds(self, receiver: str, start: float, end: float) -> float:
        """Conservative coverage inside the exact window for missing rules.

        Minute totals do not record where within a minute listening occurred.
        At each edge subtract all time outside the window, so an alert never
        borrows listening from before/after its absence interval.
        """
        if end <= start:
            return 0.0
        rows = self.conn.execute(
            "SELECT minute, SUM(seconds) AS seconds FROM coverage"
            " WHERE receiver = ? AND minute >= ? AND minute < ? GROUP BY minute",
            (receiver, minute_of(start), end),
        )
        total = 0.0
        for row in rows:
            overlap = max(0.0, min(end, row["minute"] + 60) - max(start, row["minute"]))
            total += max(0.0, min(60.0, row["seconds"]) - (60.0 - overlap))
        return total

    def timeline(
        self,
        *,
        start: float,
        end: float,
        receivers: tuple[str, ...] = RECEIVERS,
        include_private: bool = False,
        limit: int = 200,
        bucket: int | None = None,
        exclude: set[tuple[str, str]] | None = None,
        match: set[tuple[str, str]] | None = None,
    ) -> dict:
        """`exclude`: devices to drop (hidden label classes); `match`: when
        not None, only these devices (a search resolved by the caller, since
        labels live in the other database)."""
        bucket = choose_bucket(end - start, bucket)
        lo = int(start // bucket) * bucket
        hi = int(math.ceil(end / bucket)) * bucket
        placeholders = ",".join("?" * len(receivers))
        totals = self.conn.execute(
            "SELECT b.receiver, b.ident, SUM(b.reports) AS reports,"
            " MIN(b.minute) AS first_in_range,"
            " d.name, d.kind, d.signal_kind, d.first_seen, d.last_seen"
            " FROM buckets b JOIN devices d ON d.receiver = b.receiver AND d.ident = b.ident"
            f" WHERE b.minute >= ? AND b.minute < ? AND b.receiver IN ({placeholders})"
            " GROUP BY b.receiver, b.ident",
            (lo, hi, *receivers),
        ).fetchall()
        candidates = []
        for row in totals:
            key = (row["receiver"], row["ident"])
            if not include_private and row["kind"] in PRIVATE_KINDS:
                continue
            if exclude and key in exclude:
                continue
            if match is not None and key not in match:
                continue
            candidates.append(row)
        device_total = len(candidates)
        candidates.sort(key=lambda r: (-r["reports"], r["receiver"], r["ident"]))
        chosen = candidates[:limit]
        chosen.sort(key=lambda r: (r["first_in_range"], r["receiver"], r["ident"]))
        cells: dict[tuple[str, str], list] = {(r["receiver"], r["ident"]): [] for r in chosen}
        if chosen:
            # One grouped scan for the whole range; rows for devices that did
            # not make the cut are skipped in Python (cheaper than an IN list
            # of up to 500 pairs).
            for row in self.conn.execute(
                "SELECT receiver, ident, (minute / ?) * ? AS t, SUM(reports) AS n,"
                " MIN(sig_min) AS lo, MAX(sig_max) AS hi, SUM(sig_sum) AS s, SUM(sig_n) AS k"
                f" FROM buckets WHERE minute >= ? AND minute < ? AND receiver IN ({placeholders})"
                " GROUP BY receiver, ident, t ORDER BY t",
                (bucket, bucket, lo, hi, *receivers),
            ):
                target = cells.get((row["receiver"], row["ident"]))
                if target is not None:
                    target.append(
                        [row["t"], row["n"], _avg(row["s"], row["k"]), row["lo"], row["hi"]]
                    )
        return {
            "start": lo,
            "end": hi,
            "bucket_s": bucket,
            "coverage": self._coverage(receivers, lo, hi, bucket),
            "devices": [
                {
                    "receiver": r["receiver"],
                    "ident": r["ident"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "signal_kind": r["signal_kind"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "reports": r["reports"],
                    "cells": cells[(r["receiver"], r["ident"])],
                }
                for r in chosen
            ],
            "device_total": device_total,
            "truncated": device_total > len(chosen),
        }

    def device(
        self, receiver: str, ident: str, *, start: float, end: float, bucket: int | None = None
    ) -> dict | None:
        meta = self.conn.execute(
            "SELECT * FROM devices WHERE receiver = ? AND ident = ?", (receiver, ident)
        ).fetchone()
        if meta is None:
            return None
        bucket = choose_bucket(end - start, bucket)
        lo = int(start // bucket) * bucket
        hi = int(math.ceil(end / bucket)) * bucket
        cells = [
            [row["t"], row["n"], _avg(row["s"], row["k"]), row["lo"], row["hi"]]
            for row in self.conn.execute(
                "SELECT (minute / ?) * ? AS t, SUM(reports) AS n, MIN(sig_min) AS lo,"
                " MAX(sig_max) AS hi, SUM(sig_sum) AS s, SUM(sig_n) AS k FROM buckets"
                " WHERE receiver = ? AND ident = ? AND minute >= ? AND minute < ?"
                " GROUP BY t ORDER BY t",
                (bucket, bucket, receiver, ident, lo, hi),
            )
        ]
        return {
            "device": dict(meta),
            "cells": cells,
            "coverage": self._coverage((receiver,), lo, hi, bucket)[receiver],
            "bucket_s": bucket,
            "start": lo,
            "end": hi,
        }

    def activity(self, *, start: float, end: float, bucket: int | None = None) -> dict:
        """Wi-Fi activity heatmap rows (docs/wifi.md "Wi-Fi activity"):
        packets and listened seconds per (time bucket, channel), summed from
        the per-second `wifi_activity` rows. Only cells with listened_s > 0
        are returned -- a (bucket, channel) this receiver was never tuned to
        during the range has no row at all, so the UI can draw it hatched
        ("not listened") instead of as a zero-rate cell."""
        bucket = choose_activity_bucket(end - start, bucket)
        lo = int(start // bucket) * bucket
        hi = int(math.ceil(end / bucket)) * bucket
        raw = self.conn.execute(
            "SELECT (second / ?) * ? AS t, channel, SUM(packets) AS packets,"
            " SUM(listened_s) AS listened_s, SUM(sig_sum) AS sig_sum, SUM(sig_n) AS sig_n"
            " FROM wifi_activity WHERE second >= ? AND second < ?"
            " GROUP BY t, channel HAVING SUM(listened_s) > 0 ORDER BY t, channel",
            (bucket, bucket, lo, hi),
        ).fetchall()
        channels = sorted({row["channel"] for row in raw})
        rows = []
        max_rate = 0.0
        for row in raw:
            # No display-time cap here: write_activity's upsert already caps
            # each underlying per-second row at _ACTIVITY_SECOND_CAP_S, which
            # bounds the sum regardless of how many flushes touch it.
            listened = round(row["listened_s"], 2)
            sig_mean = _avg(row["sig_sum"], row["sig_n"])
            rate = row["packets"] / listened if listened > 0 else 0.0
            max_rate = max(max_rate, rate)
            rows.append([row["t"], row["channel"], row["packets"], listened, sig_mean])
        return {
            "start": lo,
            "end": hi,
            "bucket_s": bucket,
            "channels": channels,
            "rows": rows,
            "max_rate": round(max_rate, 3),
        }

    def _window_coverage(self, receivers: tuple[str, ...], start: int, end: int) -> dict:
        """Total listened seconds and (Wi-Fi) distinct channels heard across
        the *whole* window -- a coarser cousin of `_coverage` (which buckets
        by time), used by `compare` to decide comparability and to normalise
        per-device rates by actual listening time rather than wall time."""
        out = {r: {"listened_s": 0.0, "channels": []} for r in receivers}
        rows = self.conn.execute(
            "SELECT receiver, channel, SUM(seconds) AS s FROM coverage"
            f" WHERE minute >= ? AND minute < ? AND receiver IN ({','.join('?' * len(receivers))})"
            " GROUP BY receiver, channel",
            (start, end, *receivers),
        ).fetchall()
        for row in rows:
            slot = out[row["receiver"]]
            slot["listened_s"] += row["s"]
            if row["channel"]:
                slot["channels"].append(row["channel"])
        for slot in out.values():
            slot["listened_s"] = round(slot["listened_s"], 1)
            slot["channels"] = sorted(set(slot["channels"]))
        return out

    def _window_devices(self, receivers: tuple[str, ...], start: int, end: int) -> dict:
        """`(receiver, ident) -> row` for every device with activity in the
        window, unfiltered (private BLE addresses included) -- `compare`
        decides what to do with them."""
        rows = self.conn.execute(
            "SELECT b.receiver, b.ident, SUM(b.reports) AS reports,"
            " SUM(b.sig_sum) AS sig_sum, SUM(b.sig_n) AS sig_n,"
            " d.name, d.kind, d.signal_kind"
            " FROM buckets b JOIN devices d ON d.receiver = b.receiver AND d.ident = b.ident"
            " WHERE b.minute >= ? AND b.minute < ?"
            f" AND b.receiver IN ({','.join('?' * len(receivers))})"
            " GROUP BY b.receiver, b.ident",
            (start, end, *receivers),
        ).fetchall()
        return {(r["receiver"], r["ident"]): r for r in rows}

    def compare(
        self,
        *,
        a_start: float,
        a_end: float,
        b_start: float,
        b_end: float,
        receivers: tuple[str, ...] = RECEIVERS,
        include_private: bool = False,
    ) -> dict:
        """Two listening windows, e.g. "device off" vs "device on", or an
        antenna/placement A/B test (docs/history.md "Capture comparison",
        development notes Phase 5). Per device: appeared (window B only), disappeared
        (window A only), or changed (both, with reports/mean-signal per
        window normalised by that window's actual listened time -- never by
        wall-clock duration). BLE is matched by stable address only: a
        resolvable/non-resolvable private address is never a device across
        two separate windows, so it is excluded from every list and counted
        instead. Reception percentages are refused outright: nothing here
        knows how many packets/advertisements a device actually sent, so no
        count of what was heard can be turned into a percentage of it."""
        a_lo, a_hi = minute_of(a_start), int(math.ceil(a_end / 60)) * 60
        b_lo, b_hi = minute_of(b_start), int(math.ceil(b_end / 60)) * 60
        cov_a = self._window_coverage(receivers, a_lo, a_hi)
        cov_b = self._window_coverage(receivers, b_lo, b_hi)
        comparable = {}
        for r in receivers:
            la, lb = cov_a[r]["listened_s"], cov_b[r]["listened_s"]
            if la <= 0 or lb <= 0:
                comparable[r] = False  # one window never listened on this receiver at all
            elif r == "wifi" and cov_a[r]["channels"] != cov_b[r]["channels"]:
                comparable[r] = False  # different Wi-Fi channels listened -- not comparable
            else:
                comparable[r] = True

        raw_a = self._window_devices(receivers, a_lo, a_hi)
        raw_b = self._window_devices(receivers, b_lo, b_hi)

        def is_private(row) -> bool:
            return row["receiver"] == "ble" and row["kind"] in PRIVATE_KINDS

        excluded_keys = {k for k, v in raw_a.items() if is_private(v)} | {
            k for k, v in raw_b.items() if is_private(v)
        }
        keys_a = {k for k in raw_a if k not in excluded_keys}
        keys_b = {k for k in raw_b if k not in excluded_keys}
        if not include_private:
            excluded_private = None
        else:
            excluded_private = []
            for k in sorted(excluded_keys):
                row = raw_a.get(k) or raw_b.get(k)
                window = "both" if k in raw_a and k in raw_b else ("a" if k in raw_a else "b")
                excluded_private.append(
                    {"receiver": k[0], "ident": k[1], "kind": row["kind"], "window": window}
                )

        def summarize(row, listened_s: float) -> dict:
            minutes = listened_s / 60.0
            mean_signal = round(row["sig_sum"] / row["sig_n"], 1) if row["sig_n"] else None
            rate = round(row["reports"] / minutes, 2) if minutes > 0 else None
            return {
                "receiver": row["receiver"],
                "ident": row["ident"],
                "name": row["name"],
                "kind": row["kind"],
                "reports": row["reports"],
                "mean_signal": mean_signal,
                "rate_per_min": rate,
                "comparable": comparable[row["receiver"]],
            }

        disappeared = [
            summarize(raw_a[k], cov_a[raw_a[k]["receiver"]]["listened_s"])
            for k in sorted(keys_a - keys_b)
        ]
        appeared = [
            summarize(raw_b[k], cov_b[raw_b[k]["receiver"]]["listened_s"])
            for k in sorted(keys_b - keys_a)
        ]
        changed = []
        for k in sorted(keys_a & keys_b):
            ra, rb = raw_a[k], raw_b[k]
            sa = summarize(ra, cov_a[ra["receiver"]]["listened_s"])
            sb = summarize(rb, cov_b[rb["receiver"]]["listened_s"])
            signal_change = (
                round(sb["mean_signal"] - sa["mean_signal"], 1)
                if sa["mean_signal"] is not None and sb["mean_signal"] is not None
                else None
            )
            changed.append(
                {
                    "receiver": k[0],
                    "ident": k[1],
                    "name": rb["name"] or ra["name"],
                    "kind": rb["kind"] or ra["kind"],
                    "reports_a": sa["reports"],
                    "reports_b": sb["reports"],
                    "rate_a": sa["rate_per_min"],
                    "rate_b": sb["rate_per_min"],
                    "mean_signal_a": sa["mean_signal"],
                    "mean_signal_b": sb["mean_signal"],
                    "signal_change": signal_change,
                    "comparable": comparable[k[0]],
                }
            )

        notes = [
            "Reception percentages are not reported: no transmitted packet or "
            "advertisement count is known for these devices, only what this "
            "receiver heard.",
        ]
        if any(not v for v in comparable.values()):
            notes.append(
                "Coverage differs between the two windows for at least one receiver "
                "(no listening time, or different Wi-Fi channels) -- rows from that "
                "receiver are flagged comparable: false rather than compared as if "
                "coverage had matched."
            )

        return {
            "a": {"start": a_lo, "end": a_hi, "coverage": cov_a},
            "b": {"start": b_lo, "end": b_hi, "coverage": cov_b},
            "receivers": list(receivers),
            "comparable": comparable,
            "excluded_private_rows": len(excluded_keys),
            **({"excluded_private": excluded_private} if include_private else {}),
            "appeared": appeared,
            "disappeared": disappeared,
            "changed": changed,
            "notes": notes,
        }

    def search(self, q: str, receivers: tuple[str, ...] = RECEIVERS) -> set[tuple[str, str]]:
        needle = f"%{q.lower()}%"
        rows = self.conn.execute(
            "SELECT receiver, ident FROM devices"
            f" WHERE receiver IN ({','.join('?' * len(receivers))})"
            " AND (ident LIKE ? OR LOWER(COALESCE(name, '')) LIKE ?)",
            (*receivers, needle, needle),
        ).fetchall()
        return {(r["receiver"], r["ident"]) for r in rows}
