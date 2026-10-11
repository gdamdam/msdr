"""Beacon monitor (S10): an engine, not a decoder -- cycles the receiver over
NCDXF (or NDB) beacon frequencies, logs SNR per scheduled slot, and uses the
existing `cw` decoder to confirm call signs / idents. Pattern: msdr/survey/
survey.py (runtime start/stop, publish callable, status dict, *Error style).

Spec: epic S10, decoder spec P6, docs/superpowers/plans/2026-09-25-survey-
s10-beacons.md (this file is Stream A: schedule math + BeaconMonitor).
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from msdr.decoder.backends import reap_cancelled

logger = logging.getLogger(__name__)

# NCDXF/IARU beacon network: 5 bands, 18 stations, each transmitting 10s on
# each band in turn -- a full lap (one station per 10s slot on 14.100, one
# band up every subsequent slot) takes 18*10 = 180s = 3 minutes.
NCDXF_BANDS_HZ = (14_100_000, 18_110_000, 21_150_000, 24_930_000, 28_200_000)
NCDXF_BEACONS = (
    "4U1UN",
    "VE8AT",
    "W6WX",
    "KH6RS",
    "ZL6B",
    "VK6RBP",
    "JA2IGY",
    "RR9O",
    "VR2B",
    "4S7B",
    "ZS6DN",
    "5Z4B",
    "4X6TU",
    "OH2B",
    "CS3B",
    "LU4AA",
    "OA4B",
    "YV5B",
)

_SLOT_DURATION_S = 10.0
_SLOTS_PER_CYCLE = len(NCDXF_BEACONS)  # 18
# Both NCDXF and NDB beacons are keyed CW at a slow, narrow-shift rate -- the
# same filter width suits either mode.
_CW_MODE = "cw"
_CW_BANDWIDTH_HZ = 500
_DEFAULT_SNR_THRESHOLD_DB = 6.0
_DEFAULT_NDB_DWELL_S = 30
# An ident is a >=2-letter group repeated at least twice inside one dwell --
# real NDBs repeat their call every few seconds, so seeing it twice in a
# 30s dwell is the signal, distinguishing it from a one-off decode error.
_IDENT_RE = re.compile(r"[A-Z]{2,}")


class BeaconError(Exception):
    """Feature disabled or a bad request -- AppService maps this to 409."""


def slot_at(t_utc: float) -> tuple[int, float, float]:
    """(slot index 0..17, slot_start, slot_end) for the 3-minute NCDXF cycle
    containing `t_utc`. Slots are wall-clock UTC, not relative to when any
    particular monitor session started."""
    slot_start = (t_utc // _SLOT_DURATION_S) * _SLOT_DURATION_S
    slot_idx = int(slot_start // _SLOT_DURATION_S) % _SLOTS_PER_CYCLE
    return slot_idx, slot_start, slot_start + _SLOT_DURATION_S


def beacon_on(frequency_hz: int, t_utc: float) -> str | None:
    """Call sign scheduled on `frequency_hz` at `t_utc`, or None when
    `frequency_hz` isn't one of the 5 NCDXF bands."""
    try:
        band_idx = NCDXF_BANDS_HZ.index(frequency_hz)
    except ValueError:
        return None
    slot_idx, _, _ = slot_at(t_utc)
    return NCDXF_BEACONS[(slot_idx - band_idx) % _SLOTS_PER_CYCLE]


def schedule(t_utc: float) -> list[tuple[int, str]]:
    """[(frequency_hz, beacon)] for the current slot on all 5 bands."""
    slot_idx, _, _ = slot_at(t_utc)
    return [
        (band_hz, NCDXF_BEACONS[(slot_idx - band_idx) % _SLOTS_PER_CYCLE])
        for band_idx, band_hz in enumerate(NCDXF_BANDS_HZ)
    ]


def _find_ndb_ident(events: list[dict]) -> str | None:
    counts: dict[str, int] = {}
    for event in events:
        text = str(event.get("text") or "").upper()
        for token in _IDENT_RE.findall(text):
            counts[token] = counts.get(token, 0) + 1
    for token, count in counts.items():
        if count >= 2:
            return token
    return None


class BeaconMonitor:
    def __init__(
        self,
        *,
        adapter,
        tune: Callable[[int, str, int], Awaitable[None]],
        decode_start: Callable[[int], Awaitable[None]],
        decode_stop: Callable[[], Awaitable[None]],
        repo,
        publish: Callable[[dict], None],
        settings_getter: Callable[[], object],
        feature_enabled: Callable[[], bool],
        clock: Callable[[], float] = time.time,
        # Injectable alongside clock (not just clock alone): the session
        # loop waits out real slot/dwell durations (10s/30s) with this, so
        # tests can pair a fake clock with a sleep that advances it
        # instantly instead of actually waiting minutes per test.
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._adapter = adapter
        self._tune = tune
        self._decode_start = decode_start
        self._decode_stop = decode_stop
        self._repo = repo
        self._publish = publish
        self._settings_getter = settings_getter
        self._feature_enabled = feature_enabled
        self._clock = clock
        self._sleep = sleep

        self._active = False
        self._mode: str | None = None
        self._band_hz: int | None = None
        self._slot: int | None = None
        self._current_beacon: str | None = None
        self._current_until: float | None = None
        self._since: float | None = None
        self._last_stop_reason: str | None = None
        self._cycle = 0
        self._misses: dict[str, int] = {}
        # Decode events collected for the dwell/slot currently in progress;
        # reset at the start of each one, checked against the scheduled call
        # sign / NDB ident at its end.
        self._pending_events: list[dict] = []

        self._threshold_db = _DEFAULT_SNR_THRESHOLD_DB
        self._band_order: list[int] = list(NCDXF_BANDS_HZ)
        self._ndb_dwell_s = _DEFAULT_NDB_DWELL_S
        self._task: asyncio.Task | None = None
        self._last_publish_at = float("-inf")

    # -- runtime (pattern: Survey.start/stop) -- nothing periodic while idle;
    # the session lifecycle lives entirely in start_session/stop_session. ---

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._active:
            await self.stop_session("shutdown")

    # -- control -------------------------------------------------------------

    async def start_session(
        self, mode: Literal["ncdxf", "ndb"], frequencies_hz: list[int] | None = None
    ) -> dict:
        if not self._feature_enabled():
            raise BeaconError("beacons feature disabled")
        if self._active:
            raise BeaconError("a beacon session is already active")

        settings = self._settings_getter()
        self._threshold_db = float(getattr(settings, "snr_threshold_db", _DEFAULT_SNR_THRESHOLD_DB))

        if mode == "ncdxf":
            self._band_order = list(getattr(settings, "band_order_hz", None) or NCDXF_BANDS_HZ)
            if not self._band_order:
                raise BeaconError("no NCDXF bands configured")
            coro = self._run_ncdxf()
        elif mode == "ndb":
            configured = getattr(settings, "ndb_frequencies_hz", None)
            frequencies = list(frequencies_hz or configured or [])
            if not frequencies:
                raise BeaconError("no NDB frequencies configured")
            self._ndb_dwell_s = int(getattr(settings, "ndb_dwell_s", _DEFAULT_NDB_DWELL_S))
            coro = self._run_ndb(frequencies)
        else:
            raise BeaconError(f"unknown beacon mode: {mode}")

        self._mode = mode
        self._active = True
        self._since = self._clock()
        self._last_stop_reason = None
        self._cycle = 0
        self._misses = {}
        self._task = asyncio.create_task(coro)
        self._publish_status(force=True)
        return self.status()

    async def stop_session(self, reason: str = "user") -> dict:
        if not self._active:
            return self.status()
        self._active = False
        self._last_stop_reason = reason
        if self._task is not None:
            self._task.cancel()
            await reap_cancelled(self._task)
            self._task = None
        try:
            await self._decode_stop()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, session is over either way
            logger.exception("beacon monitor: decode_stop failed on session stop")
        self._mode = None
        self._band_hz = None
        self._slot = None
        self._current_beacon = None
        self._current_until = None
        self._since = None
        self._publish_status(force=True)
        return self.status()

    def note_decode_event(self, event: dict) -> None:
        """Fed by the service for every `decoder_event` while a session is
        active (like Survey.note_decode_event) -- just collected for the
        dwell/slot in progress; matched at its end."""
        if self._active:
            self._pending_events.append(event)

    def status(self) -> dict:
        current = None
        if self._current_beacon is not None:
            current = {"beacon": self._current_beacon, "until": self._current_until}
        return {
            "active": self._active,
            "mode": self._mode,
            "band_hz": self._band_hz,
            "slot": self._slot,
            "current": current,
            "since": self._since,
            "last_stop_reason": self._last_stop_reason,
            "cycle": self._cycle,
            "summary": self._repo.summary(),
            "misses": dict(self._misses),
        }

    # -- internals: NCDXF session --------------------------------------------

    async def _run_ncdxf(self) -> None:
        try:
            idx = 0
            while True:
                band_hz = self._band_order[idx % len(self._band_order)]
                self._band_hz = band_hz
                self._slot = None
                await self._tune(band_hz, _CW_MODE, _CW_BANDWIDTH_HZ)
                await self._decode_start(band_hz)
                for _ in range(_SLOTS_PER_CYCLE):
                    await self._run_ncdxf_slot(band_hz)
                await self._decode_stop()
                self._cycle += 1
                idx += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("beacon monitor (ncdxf) crashed")
            self._active = False
            self._last_stop_reason = "error"
            self._publish_status(force=True)

    async def _run_ncdxf_slot(self, band_hz: int) -> None:
        slot_idx, slot_start, slot_end = slot_at(self._clock())
        beacon = beacon_on(band_hz, slot_start)
        self._slot = slot_idx
        self._current_beacon = beacon
        self._current_until = slot_end
        self._pending_events = []
        self._publish_status()

        max_snr = await self._sample_snr(slot_end)
        decoded = beacon is not None and any(
            beacon in str(event.get("text") or "") for event in self._pending_events
        )
        probable = max_snr is not None and max_snr >= self._threshold_db
        if beacon is not None and (probable or decoded):
            self._repo.add(
                {
                    "ts": slot_end,
                    "beacon": beacon,
                    "frequency_hz": band_hz,
                    "snr_db": max_snr,
                    "decoded": decoded,
                    "probable": probable,
                    "mode": "ncdxf",
                }
            )
        elif beacon is not None:
            key = f"{beacon}@{band_hz}"
            self._misses[key] = self._misses.get(key, 0) + 1
        self._publish_status()

    # -- internals: NDB session -----------------------------------------------

    async def _run_ndb(self, frequencies: list[int]) -> None:
        try:
            idx = 0
            while True:
                freq = frequencies[idx % len(frequencies)]
                self._band_hz = freq
                self._slot = None
                self._current_beacon = None
                await self._tune(freq, _CW_MODE, _CW_BANDWIDTH_HZ)
                await self._decode_start(freq)
                until = self._clock() + self._ndb_dwell_s
                self._current_until = until
                self._pending_events = []
                self._publish_status()

                max_snr = await self._sample_snr(until)
                ident = _find_ndb_ident(self._pending_events)
                if ident is not None:
                    self._repo.add(
                        {
                            "ts": until,
                            "beacon": ident,
                            "frequency_hz": freq,
                            "snr_db": max_snr,
                            "decoded": True,
                            "probable": False,
                            "mode": "ndb",
                        }
                    )
                else:
                    key = f"NDB@{freq}"
                    self._misses[key] = self._misses.get(key, 0) + 1
                await self._decode_stop()
                self._cycle += 1
                idx += 1
                self._publish_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("beacon monitor (ndb) crashed")
            self._active = False
            self._last_stop_reason = "error"
            self._publish_status(force=True)

    # -- internals: shared -----------------------------------------------------

    async def _sample_snr(self, until: float) -> float | None:
        """Max of `adapter.get("signal_snr")` sampled at ~1 Hz until `until`,
        ignoring failures (offline/flaky adapter shouldn't cost a slot its hit)."""
        max_snr: float | None = None
        while True:
            try:
                value = await self._adapter.get("signal_snr")
            except Exception:  # noqa: BLE001 -- ignored per spec
                value = None
            if value is not None:
                try:
                    value = float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    value = None
            if value is not None:
                max_snr = value if max_snr is None else max(max_snr, value)
            remaining = until - self._clock()
            if remaining <= 0:
                break
            await self._sleep(min(1.0, remaining))
        return max_snr

    def _publish_status(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_publish_at < 1.0:
            return
        self._last_publish_at = now
        self._publish({"type": "beacons", **self.status()})
