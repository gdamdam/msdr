"""Device history, shared timeline and labels (docs/history.md): the
API-facing mixin over `msdr.history`.

AppService mixin. The Wi-Fi and BLE supervisors only know a
`MinuteAccumulator`; this mixin owns the history database, the 2 s coverage
sampler and the 30 s flush, and joins labels (main database) onto timeline
rows (history database) in Python, since they live in different files.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import HTTPException

from msdr.history.buckets import MinuteAccumulator
from msdr.history.labels import LabelError, LabelsRepo, check_labelable, normalize_ident
from msdr.history.store import RECEIVERS, HistoryStore
from msdr.settings import Settings
from msdr.wifi.activity import ActivityAccumulator

logger = logging.getLogger(__name__)

SAMPLE_EVERY_S = 2.0
FLUSH_EVERY_S = 30.0
PRUNE_EVERY_S = 3600.0
#: States in which a receiver is actually listening (docs/history.md coverage).
_LISTENING = frozenset({"receiving", "stale"})


def history_db_path(settings: Settings) -> Path | str:
    if settings.history.db_path is not None:
        return settings.history.db_path
    if str(settings.db_path) == ":memory:":
        return ":memory:"
    return Path(settings.db_path).parent / "history.db"


class HistoryServiceMixin:
    def _build_history(self, settings: Settings) -> None:
        self.history_acc = MinuteAccumulator()
        # Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): a second
        # sink alongside history_acc, Wi-Fi-only (BLE has no per-channel
        # concept this heatmap applies to).
        self.activity_acc = ActivityAccumulator()
        self.history_store = HistoryStore(history_db_path(settings))
        self.labels: LabelsRepo | None = None
        self._history_task: asyncio.Task | None = None
        self._history_last_sample: dict[str, float] = {}

    def _sync_history_sinks(self) -> None:
        """Point both supervisors (and their current tables) at the
        accumulator, or detach them when history is off. Re-run every tick
        because a settings change can replace a supervisor object."""
        acc = self.history_acc if self.settings.history.enabled else None
        for sup in (self.wifi, self.ble):
            sup.history = acc
            if sup.table is not None:
                sup.table.history = acc
        self.wifi.activity = self.activity_acc if self.settings.history.enabled else None

    async def _start_history_runtime(self) -> None:
        self.labels = LabelsRepo(self.db)
        try:
            self.history_store.connect()
            self.history_store.prune(self.settings.history.retention_days)
            self.history_store.prune_activity(self.settings.history.activity_retention_hours)
        except Exception:
            # A broken history file must not stop msdr; reads then 503.
            logger.exception("history: database unavailable at %s", self.history_store.path)
            return
        self._sync_history_sinks()
        self._history_task = asyncio.create_task(self._history_loop())

    async def _stop_history_runtime(self) -> None:
        task, self._history_task = self._history_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            self._history_flush()
        self.history_store.close()

    async def _history_loop(self) -> None:
        last_flush = last_prune = time.monotonic()
        while True:
            await asyncio.sleep(SAMPLE_EVERY_S)
            try:
                self._sync_history_sinks()
                self._history_sample_coverage(time.time())
                now = time.monotonic()
                if now - last_flush >= FLUSH_EVERY_S:
                    last_flush = now
                    self._history_flush()
                if now - last_prune >= PRUNE_EVERY_S:
                    last_prune = now
                    self.history_store.prune(self.settings.history.retention_days)
                    self.history_store.prune_activity(
                        self.settings.history.activity_retention_hours
                    )
            except Exception:
                logger.exception("history: tick failed")

    def _history_sample_coverage(self, now: float) -> None:
        """Credit the time since the previous sample to each receiver that
        is listening now (on its current channel). Gaps longer than two
        sample periods are not credited: a stalled loop must not invent
        listening time."""
        if not self.settings.history.enabled:
            self._history_last_sample.clear()
            return
        for receiver, sup in (("wifi", self.wifi), ("ble", self.ble)):
            listening = sup.health_state() in _LISTENING
            prev = self._history_last_sample.get(receiver)
            if listening:
                if prev is not None:
                    elapsed = min(now - prev, SAMPLE_EVERY_S * 2)
                    if elapsed > 0:
                        channel = (sup.channel or 0) if receiver == "wifi" else 0
                        self.history_store.add_coverage(receiver, now, channel, elapsed)
                self._history_last_sample[receiver] = now
            else:
                self._history_last_sample.pop(receiver, None)

    def _history_flush(self) -> None:
        self.history_store.write(self.history_acc.drain())
        self.history_store.write_activity(self.activity_acc.drain().cells)

    def _history_ready(self) -> HistoryStore:
        if self.history_store.db._conn is None:
            raise HTTPException(503, "history database unavailable")
        self._history_flush()
        return self.history_store

    # --- history API -------------------------------------------------------

    def history_status(self) -> dict:
        out = {
            "enabled": self.settings.history.enabled,
            "retention_days": self.settings.history.retention_days,
        }
        if self.history_store.db._conn is None:
            return {**out, "db_path": str(self.history_store.path), "available": False}
        self._history_flush()
        return {
            **out,
            **self.history_store.status(),
            "available": True,
            "pending_dropped": self.history_acc.dropped,
        }

    def history_clear(self) -> dict:
        store = self._history_ready()
        store.clear()
        return {"ok": True}

    def history_timeline(
        self,
        *,
        hours: float | None,
        start: float | None,
        end: float | None,
        receivers: tuple[str, ...],
        include_private: bool,
        hide: set[str],
        q: str | None,
        limit: int,
        bucket: int | None,
    ) -> dict:
        store = self._history_ready()
        now = time.time()
        if start is None or end is None:
            end = now
            start = now - (hours or 6.0) * 3600
        if end <= start:
            raise HTTPException(422, "end must be after start")
        assert self.labels is not None
        match = None
        if q:
            match = store.search(q, receivers) | self.labels.search(q)
        result = store.timeline(
            start=start,
            end=end,
            receivers=receivers,
            include_private=include_private,
            limit=limit,
            bucket=bucket,
            exclude=self.labels.keys(hide),
            match=match,
        )
        labels = self.labels.by_key()
        for d in result["devices"]:
            lab = labels.get((d["receiver"], d["ident"]))
            d["label"] = {"name": lab["name"], "class": lab["class"]} if lab else None
        return result

    def history_compare(
        self,
        *,
        a_start: float,
        a_end: float,
        b_start: float,
        b_end: float,
        receivers: tuple[str, ...],
        include_private: bool,
    ) -> dict:
        """Two listening windows, e.g. "device off" vs "device on", or an
        antenna/placement A/B test (docs/history.md "Capture comparison")."""
        store = self._history_ready()
        if a_end <= a_start or b_end <= b_start:
            raise HTTPException(422, "each window's end must be after its start")
        return store.compare(
            a_start=a_start,
            a_end=a_end,
            b_start=b_start,
            b_end=b_end,
            receivers=receivers,
            include_private=include_private,
        )

    def wifi_activity(
        self, *, minutes: float, bucket: int | None
    ) -> dict:
        """Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): packets
        received per listened second, by channel and time bucket. Flushes
        first like history_timeline/history_device so a just-received packet
        shows up without waiting for the next 30 s flush."""
        store = self._history_ready()
        now = time.time()
        end = now
        start = now - minutes * 60
        return store.activity(start=start, end=end, bucket=bucket)

    def history_device(
        self, receiver: str, ident: str, *, hours: float, bucket: int | None
    ) -> dict:
        store = self._history_ready()
        try:
            ident = normalize_ident(receiver, ident)
        except LabelError as exc:
            raise HTTPException(422, str(exc)) from None
        now = time.time()
        result = store.device(receiver, ident, start=now - hours * 3600, end=now, bucket=bucket)
        if result is None:
            raise HTTPException(404, "device never seen")
        assert self.labels is not None
        result["label"] = self.labels.get(receiver, ident)
        return result

    # --- labels API --------------------------------------------------------

    def _observed_kind(self, receiver: str, ident: str) -> str | None:
        if receiver == "ble":
            table = self.ble.table
            entry = table.details(ident) if table is not None else None
            if entry is not None:
                return entry["identity"]
        if self.history_store.db._conn is not None:
            row = self.history_store.conn.execute(
                "SELECT kind FROM devices WHERE receiver = ? AND ident = ?", (receiver, ident)
            ).fetchone()
            if row is not None:
                return row["kind"]
        return None

    def labels_list(self, receiver: str | None) -> dict:
        assert self.labels is not None
        if receiver is not None and receiver not in RECEIVERS:
            raise HTTPException(422, f"unknown receiver: {receiver!r}")
        return {"items": self.labels.list(receiver)}

    def label_put(self, receiver: str, ident: str, *, name: str, note: str, cls: str) -> dict:
        assert self.labels is not None
        try:
            ident = normalize_ident(receiver, ident)
            check_labelable(receiver, ident, self._observed_kind(receiver, ident))
            return self.labels.put(receiver, ident, name=name, note=note, cls=cls)
        except LabelError as exc:
            raise HTTPException(422, str(exc)) from None

    def label_delete(self, receiver: str, ident: str) -> dict:
        assert self.labels is not None
        try:
            ident = normalize_ident(receiver, ident)
        except LabelError as exc:
            raise HTTPException(422, str(exc)) from None
        if not self.labels.delete(receiver, ident):
            raise HTTPException(404, "label not found")
        return {"ok": True}
