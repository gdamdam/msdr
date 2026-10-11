"""Activity-triggered control for the pre-roll IQ recorder.

The controller consumes SNR observations from a caller (normally the live
spectrum/signal tracker). It does not own the radio stream: Recorder remains
the sole writer, and this object stops only a recording it started itself.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ActivityTriggerConfig:
    frequency_hz: int
    start_snr_db: float = 10.0
    stop_snr_db: float = 6.0
    frequency_tolerance_hz: int = 0
    min_active_s: float = 1.0
    quiet_hold_s: float = 2.0
    max_recording_s: float | None = None
    event_cap: int | None = None

    def __post_init__(self) -> None:
        if self.frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        if not math.isfinite(self.start_snr_db) or not math.isfinite(self.stop_snr_db):
            raise ValueError("SNR thresholds must be finite")
        if self.start_snr_db <= self.stop_snr_db:
            raise ValueError("start_snr_db must exceed stop_snr_db")
        if self.frequency_tolerance_hz < 0:
            raise ValueError("frequency_tolerance_hz must be nonnegative")
        if self.min_active_s < 0 or self.quiet_hold_s < 0:
            raise ValueError("min_active_s and quiet_hold_s must be nonnegative")
        if self.max_recording_s is not None and self.max_recording_s <= 0:
            raise ValueError("max_recording_s must be positive")
        if self.event_cap is not None and self.event_cap <= 0:
            raise ValueError("event_cap must be positive")


class ActivityTrigger:
    """Apply SNR hysteresis and quiet timing to an armed ``Recorder``.

    ``observe`` handles a fresh measurement. ``tick`` handles timers during
    gaps in the measurement stream and should be called periodically by the
    owning service. ``rearm`` explicitly clears an event-cap lockout.
    """

    def __init__(
        self,
        recorder,
        config: ActivityTriggerConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        start_recording: Callable[[], Awaitable | object] | None = None,
        stop_recording: Callable[[], Awaitable | object] | None = None,
    ):
        self.recorder = recorder
        self.config = config
        self._clock = clock
        # Services can inject their public record/stop facade to preserve
        # observation and audit hooks while keeping Recorder as state owner.
        self._start_recording = start_recording or recorder.record
        self._stop_recording = stop_recording or recorder.stop_recording
        self._lock = asyncio.Lock()
        self.enabled = False
        self._owns_recording = False
        self._recording_started_at: float | None = None
        self._quiet_since: float | None = None
        self._last_snr_db: float | None = None
        self._last_observed_at: float | None = None
        self._event_count = 0
        self._capped = False
        self._error: str | None = None

    async def set_enabled(self, enabled: bool) -> dict:
        async with self._lock:
            if not enabled:
                self.enabled = False
                if self._owns_recording:
                    await self._stop_owned()
                self._quiet_since = None
            else:
                state = self.recorder.status().get("state")
                if state != "armed":
                    raise RuntimeError("activity trigger requires an armed recorder")
                if self._capped:
                    raise RuntimeError("activity trigger is capped; rearm it first")
                self.enabled = True
                self._error = None
            return self.status()

    async def observe(
        self, frequency_hz: int, snr_db: float | None, *, at: float | None = None
    ) -> bool:
        """Consume an SNR measurement; return whether it matched this watch."""
        if abs(int(frequency_hz) - self.config.frequency_hz) > self.config.frequency_tolerance_hz:
            return False
        now = self._clock() if at is None else float(at)
        if not math.isfinite(now):
            return False
        value = float(snr_db) if snr_db is not None else None
        if value is not None and not math.isfinite(value):
            value = None
        async with self._lock:
            self._last_snr_db = value
            self._last_observed_at = now
            await self._reconcile_owner()
            if self.enabled and not self._capped and not self._owns_recording:
                if value is not None and value >= self.config.start_snr_db:
                    await self._start_owned(now)
            elif self._owns_recording:
                if value is not None and value >= self.config.stop_snr_db:
                    self._quiet_since = None
                elif self._quiet_since is None:
                    self._quiet_since = now
                await self._maybe_finish(now)
            return True

    async def tick(self, *, at: float | None = None) -> dict:
        """Advance quiet/max-duration timers without requiring a new frame."""
        now = self._clock() if at is None else float(at)
        async with self._lock:
            await self._reconcile_owner()
            if self._owns_recording:
                await self._maybe_finish(now)
            return self.status()

    async def suspend_observations(self) -> dict:
        """Pause quiet-stop timing when the watched frequency is unobserved.

        Missing coverage, stale frames, or a coarse unresolved bin provide no
        evidence of silence. An active recording remains owned and its hard
        duration cap continues through ``tick``.
        """
        async with self._lock:
            await self._reconcile_owner()
            self._quiet_since = None
            return self.status()

    async def rearm(self) -> dict:
        """Clear the event cap after the owned recording has ended."""
        async with self._lock:
            await self._reconcile_owner()
            if self._owns_recording or self.recorder.status().get("state") == "recording":
                raise RuntimeError("cannot rearm while a recording is active")
            if self.recorder.status().get("state") != "armed":
                raise RuntimeError("recorder must be armed before rearming the trigger")
            self._event_count = 0
            self._capped = False
            self._quiet_since = None
            self._error = None
            return self.status()

    def status(self) -> dict:
        now = self._clock()
        elapsed = (
            max(0.0, now - self._recording_started_at)
            if self._owns_recording and self._recording_started_at is not None else 0.0
        )
        state = "recording_capped" if self._capped and self._owns_recording else (
            "capped" if self._capped else
            "recording" if self._owns_recording and self._quiet_since is None else
            "waiting_quiet" if self._owns_recording else
            "armed" if self.enabled else "disabled"
        )
        return {
            "state": state,
            "enabled": self.enabled,
            "frequency_hz": self.config.frequency_hz,
            "snr_db": self._last_snr_db,
            "last_observed_at": self._last_observed_at,
            "quiet_since": self._quiet_since,
            "recording_s": elapsed,
            "event_count": self._event_count,
            "event_cap": self.config.event_cap,
            "owns_recording": self._owns_recording,
            "error": self._error,
        }

    async def _start_owned(self, now: float) -> None:
        # Recheck under the controller lock: manual recording and finalization
        # are owner-controlled states and must never be adopted implicitly.
        if self.recorder.status().get("state") != "armed":
            self.enabled = False
            return
        try:
            result = self._start_recording()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self._error = str(exc)
            self.enabled = False
            return
        self._owns_recording = True
        self._recording_started_at = now
        self._quiet_since = None
        self._event_count += 1
        if self.config.event_cap is not None and self._event_count >= self.config.event_cap:
            # Let this event finish by silence or duration cap, then require an
            # explicit rearm before another recording can start.
            self._capped = True

    async def _maybe_finish(self, now: float) -> None:
        if self._recording_started_at is None:
            return
        elapsed = max(0.0, now - self._recording_started_at)
        duration_reached = (
            self.config.max_recording_s is not None
            and elapsed >= self.config.max_recording_s
        )
        quiet_reached = (
            self._quiet_since is not None
            and elapsed >= self.config.min_active_s
            and now - self._quiet_since >= self.config.quiet_hold_s
        )
        if duration_reached or quiet_reached:
            await self._stop_owned()

    async def _stop_owned(self) -> None:
        # Never stop a recording unless our earlier record() succeeded and it
        # is still recording. A manual stop/disconnect clears ownership below.
        if not self._owns_recording:
            return
        if self.recorder.status().get("state") == "recording":
            try:
                result = self._stop_recording()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._error = str(exc)
        self._owns_recording = False
        self._recording_started_at = None
        self._quiet_since = None

    async def _reconcile_owner(self) -> None:
        if self._owns_recording and self.recorder.status().get("state") != "recording":
            # Recorder can be stopped by a user, radio loss, or writer failure.
            # Do not try to take ownership back during that session.
            self._owns_recording = False
            self._recording_started_at = None
            self._quiet_since = None
            self.enabled = False
