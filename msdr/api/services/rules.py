"""Watch rules (docs/rules.md): the API-facing mixin over
`msdr.rules.engine`/`msdr.rules.store`.

AppService mixin, same shape as `RemoteidServiceMixin`/`HistoryServiceMixin`:
owns a background tick (``RULES_TICK_S``) that re-evaluates every enabled
rule against the other mixins' already-live state -- ``self.wifi.table``/
``self.ble.table`` (WifiServiceMixin/BleServiceMixin), ``self.labels``/
``self._observed_kind`` and ``self.history_store`` (HistoryServiceMixin), and
``self.remoteid_tracker`` (RemoteidServiceMixin) -- instead of inventing a
second notification channel or a second copy of device state. Fired events
are persisted to ``rule_events`` and read back by the UI the same way
``band-scan-inbox.js`` polls a REST endpoint rather than depending on the
websocket.

Rule config validation lives here (needs live context: is this BLE address
rotating right now?) rather than in ``msdr.rules.engine``, which only ever
sees already-validated config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import HTTPException

from msdr.api.services.remoteid import STALE_AFTER_S
from msdr.history.labels import LabelError, check_labelable, normalize_ident
from msdr.rules.engine import (
    DEFAULT_MAX_AGE_S,
    BleAdvertiser,
    Context,
    DronePosition,
    EngineState,
    Rule,
    WifiNetwork,
    evaluate,
)
from msdr.rules.store import KINDS, RuleError, RulesRepo

logger = logging.getLogger(__name__)

#: Within the Signal workbench watch's own 1-2 s range (docs/rules.md).
RULES_TICK_S = 1.5


def _resolve_ble_sensor_readings(table, addr: str) -> dict[str, float]:
    """Defensive lookup of `sensor.readings[name].value` on a live BLE
    advertiser (msdr/ble/sensors.py, added alongside this feature): neither
    `AdvertiserTable.details()` carrying a `"sensor"` key nor the raw
    `AdvertiserEntry.sensor` attribute is assumed to exist yet. Either
    missing shape, or a reading with no numeric `.value`, is simply skipped
    -- a sensor_threshold rule for a name that never appears just never
    fires, it never raises."""
    readings: dict[str, float] = {}
    raw = None
    details = table.details(addr) if hasattr(table, "details") else None
    if isinstance(details, dict):
        sensor = details.get("sensor")
        if isinstance(sensor, dict):
            raw = sensor.get("readings")
    if raw is None:
        entries = getattr(table, "_entries", None)
        entry = entries.get(addr) if isinstance(entries, dict) else None
        sensor_obj = getattr(entry, "sensor", None)
        raw = getattr(sensor_obj, "readings", None)
    if not raw or not hasattr(raw, "items"):
        return readings
    for name, reading in raw.items():
        value = (
            reading.get("value") if isinstance(reading, dict) else getattr(reading, "value", None)
        )
        if isinstance(value, int | float):
            readings[name] = float(value)
    return readings


class RulesServiceMixin:
    def _build_rules(self) -> None:
        self.rules: RulesRepo | None = None
        self._rules_state = EngineState()
        self._rules_task: asyncio.Task | None = None

    async def _start_rules_runtime(self) -> None:
        self.rules = RulesRepo(self.db)
        # Seed cooldowns already in progress so a restart right after a fire
        # does not immediately refire the same condition (docs/rules.md
        # "Limits": the presence/armed/inside edges themselves are not
        # seeded, only the cooldown clock).
        self._rules_state = EngineState(last_fired=self.rules.last_fired_by_target())
        self._rules_task = asyncio.create_task(self._rules_loop())

    async def _stop_rules_runtime(self) -> None:
        task, self._rules_task = self._rules_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _rules_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(RULES_TICK_S)
                try:
                    self._rules_tick()
                except Exception:
                    logger.exception("rules: tick failed")
        except asyncio.CancelledError:
            raise

    def _rules_tick(self) -> None:
        assert self.rules is not None
        rows = self.rules.list()
        if not any(r["enabled"] for r in rows):
            return
        rules = [self._as_engine_rule(r) for r in rows]
        ctx = self._rules_context()
        for event in evaluate(rules, ctx, self._rules_state):
            self.rules.add_event(
                rule_id=event.rule_id, kind=event.kind, target=event.target,
                message=event.message, detail=event.detail, fired_at=event.at,
            )

    @staticmethod
    def _as_engine_rule(row: dict) -> Rule:
        return Rule(
            id=row["id"], kind=row["kind"], name=row["name"], enabled=row["enabled"],
            config=row["config"], cooldown_s=row["cooldown_s"],
        )

    # --- context gathering (read-only over the other mixins' live state) --

    def _rules_context(self) -> Context:
        return Context(
            now=time.time(),
            wifi=self._rules_wifi_entries(),
            ble=self._rules_ble_entries(),
            drones=self._rules_drones(),
            coverage_fraction=self._rules_coverage_fraction,
        )

    def _rules_wifi_entries(self) -> dict[str, WifiNetwork]:
        table = self.wifi.table
        if table is None:
            return {}
        rows, _ = table.rows(limit=1_000_000)
        return {r["bssid"]: WifiNetwork(bssid=r["bssid"], last_seen=r["last_seen"]) for r in rows}

    def _rules_ble_entries(self) -> dict[str, BleAdvertiser]:
        table = self.ble.table
        if table is None:
            return {}
        rows, _ = table.rows(limit=1_000_000)
        out = {}
        for r in rows:
            addr = r["addr"]
            out[addr] = BleAdvertiser(
                addr=addr, identity=r["identity"], last_seen=r["last_seen"],
                readings=_resolve_ble_sensor_readings(table, addr),
                readings_at=(r.get("sensor") or {}).get("ts", 0.0),
            )
        return out

    def _rules_drones(self) -> list[DronePosition]:
        now = time.time()
        groups = self.remoteid_tracker.groups(now=now, stale_after_s=STALE_AFTER_S)
        out = []
        for group in groups:
            positions = [
                t.drone_position for t in group.tracks
                if t.drone_position is not None and t.drone_position.has_position
            ]
            if not positions:
                continue
            fix = max(positions, key=lambda p: p.received_at)
            age_s = now - fix.received_at
            out.append(
                DronePosition(
                    key=group.key, latitude_deg=fix.latitude_deg, longitude_deg=fix.longitude_deg,
                    age_s=age_s, stale=age_s > STALE_AFTER_S,
                )
            )
        return out

    def _rules_coverage_fraction(self, receiver: str, start: float, end: float) -> float:
        """How much of `[start, end)` the receiver was listening, reusing
        HistoryServiceMixin's own coverage table (docs/history.md
        "coverage") instead of a second tracker -- 0.0 (never "missing")
        when history is disabled/unavailable or the window is empty."""
        if (
            end <= start or not self.settings.history.enabled
            or self.history_store.db._conn is None
        ):
            return 0.0
        try:
            self._history_flush()
            covered = self.history_store.covered_seconds(receiver, start, end)
        except Exception:
            logger.exception("rules: coverage query failed")
            return 0.0
        return min(1.0, covered / max(1.0, end - start))

    # --- config validation (needs live context; msdr.rules.engine does not) -

    def _rules_validate_config(self, kind: str, config: dict) -> dict:
        if kind == "beacon_presence":
            return self._rules_validate_beacon(config)
        if kind == "sensor_threshold":
            return self._rules_validate_sensor(config)
        if kind == "drone_geofence":
            return self._rules_validate_geofence(config)
        raise HTTPException(422, f"unknown kind: {kind!r}")

    def _rules_validate_beacon(self, config: dict) -> dict:
        receiver = config.get("receiver")
        if receiver not in ("wifi", "ble"):
            raise HTTPException(422, "receiver must be 'wifi' or 'ble'")
        ident = config.get("ident")
        if not isinstance(ident, str) or not ident:
            raise HTTPException(422, "ident is required")
        try:
            ident = normalize_ident(receiver, ident)
            check_labelable(receiver, ident, self._observed_kind(receiver, ident))
        except LabelError as exc:
            raise HTTPException(422, str(exc)) from None
        mode = config.get("mode", "seen")
        if mode not in ("seen", "missing"):
            raise HTTPException(422, "mode must be 'seen' or 'missing'")
        out = {"receiver": receiver, "ident": ident, "mode": mode}
        if mode == "missing":
            minutes = config.get("missing_after_minutes")
            if not isinstance(minutes, int | float) or not 1 <= minutes <= 1440:
                raise HTTPException(422, "missing_after_minutes must be 1-1440")
            out["missing_after_minutes"] = float(minutes)
        return out

    def _rules_validate_sensor(self, config: dict) -> dict:
        addr = config.get("addr")
        if not isinstance(addr, str) or not addr:
            raise HTTPException(422, "addr is required")
        try:
            addr = normalize_ident("ble", addr)
            check_labelable("ble", addr, self._observed_kind("ble", addr))
        except LabelError as exc:
            raise HTTPException(422, str(exc)) from None
        name = config.get("sensor_name")
        if not isinstance(name, str) or not name:
            raise HTTPException(422, "sensor_name is required")
        comparison = config.get("comparison")
        if comparison not in ("above", "below"):
            raise HTTPException(422, "comparison must be 'above' or 'below'")
        threshold = config.get("threshold")
        if not isinstance(threshold, int | float):
            raise HTTPException(422, "threshold must be a number")
        hysteresis = config.get("hysteresis", 0.0)
        if not isinstance(hysteresis, int | float) or hysteresis < 0:
            raise HTTPException(422, "hysteresis must be a non-negative number")
        return {
            "addr": addr, "sensor_name": name, "comparison": comparison,
            "threshold": float(threshold), "hysteresis": float(hysteresis),
        }

    def _rules_validate_geofence(self, config: dict) -> dict:
        geometry = config.get("geometry")
        if not isinstance(geometry, dict):
            raise HTTPException(422, "geometry is required")
        gtype = geometry.get("type")
        if gtype == "circle":
            center = geometry.get("center")
            radius = geometry.get("radius_m")
            if not isinstance(center, dict) or "lat" not in center or "lon" not in center:
                raise HTTPException(422, "circle geometry needs center {lat, lon}")
            if not isinstance(radius, int | float) or radius <= 0:
                raise HTTPException(422, "radius_m must be positive")
            out_geometry = {
                "type": "circle",
                "center": {"lat": float(center["lat"]), "lon": float(center["lon"])},
                "radius_m": float(radius),
            }
        elif gtype == "polygon":
            points = geometry.get("points")
            if not isinstance(points, list) or len(points) < 3:
                raise HTTPException(422, "polygon geometry needs at least 3 points")
            out_points = []
            for p in points:
                if not isinstance(p, dict) or "lat" not in p or "lon" not in p:
                    raise HTTPException(422, "each polygon point needs {lat, lon}")
                out_points.append({"lat": float(p["lat"]), "lon": float(p["lon"])})
            out_geometry = {"type": "polygon", "points": out_points}
        else:
            raise HTTPException(422, "geometry.type must be 'circle' or 'polygon'")
        max_age_s = config.get("max_age_s", DEFAULT_MAX_AGE_S)
        if not isinstance(max_age_s, int | float) or max_age_s <= 0:
            raise HTTPException(422, "max_age_s must be positive")
        return {"geometry": out_geometry, "max_age_s": float(max_age_s)}

    # --- read/write API ----------------------------------------------------

    def rules_list(self) -> dict:
        assert self.rules is not None
        return {"items": self.rules.list()}

    def rules_create(
        self, *, kind: str, name: str, config: dict, enabled: bool, cooldown_s: float
    ) -> dict:
        assert self.rules is not None
        if kind not in KINDS:
            raise HTTPException(422, f"kind must be one of {', '.join(KINDS)}")
        normalized = self._rules_validate_config(kind, config)
        try:
            return self.rules.create(
                kind=kind, name=name, config=normalized, enabled=enabled, cooldown_s=cooldown_s
            )
        except RuleError as exc:
            raise HTTPException(422, str(exc)) from None

    def rules_update(
        self, rule_id: int, *, name: str | None, config: dict | None, enabled: bool | None,
        cooldown_s: float | None,
    ) -> dict:
        assert self.rules is not None
        existing = self.rules.get(rule_id)
        if existing is None:
            raise HTTPException(404, "rule not found")
        normalized = (
            self._rules_validate_config(existing["kind"], config) if config is not None else None
        )
        try:
            updated = self.rules.update(
                rule_id, name=name, config=normalized, enabled=enabled, cooldown_s=cooldown_s
            )
        except RuleError as exc:
            raise HTTPException(422, str(exc)) from None
        assert updated is not None
        return updated

    def rules_delete(self, rule_id: int) -> dict:
        assert self.rules is not None
        if not self.rules.delete(rule_id):
            raise HTTPException(404, "rule not found")
        return {"ok": True}

    def rules_events(self, *, rule_id: int | None, limit: int) -> dict:
        assert self.rules is not None
        return {"items": self.rules.list_events(rule_id=rule_id, limit=limit)}

    def rules_test(self, rule_id: int) -> dict:
        """Evaluate one rule against the current live context right now,
        against a *fresh* `EngineState` (docs/rules.md "API"): ignores
        cooldown and any in-progress presence/armed/inside edge so the
        result always reflects the rule's condition as configured, not
        whether it happens to be in a cooldown window. Never persists an
        event or touches the live engine state."""
        assert self.rules is not None
        row = self.rules.get(rule_id)
        if row is None:
            raise HTTPException(404, "rule not found")
        rule = self._as_engine_rule({**row, "enabled": True})
        ctx = self._rules_context()
        events = evaluate([rule], ctx, EngineState())
        return {
            "rule_id": rule_id,
            "would_fire": [
                {"target": e.target, "message": e.message, "detail": e.detail} for e in events
            ],
        }
