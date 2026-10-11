"""`ChannelSurvey`: hop across Wi-Fi channels, dwelling on each long enough to
sample traffic, driven by an asyncio task that the API service owns (see
docs/history.md "Wi-Fi channel survey"). The ACH helper has no channel
hopping of its own, so a survey restarts `WifiSupervisor` per channel
(`start(ch, configured_channel=ch, keep_table=True)`) and watches its
`health_state()` until frames start arriving before counting dwell time.

This module only depends on a WifiSupervisor-shaped object (the real one, or
a fake with the same surface in tests) -- it never imports anything from
`msdr.api` and never touches the radio adapter, recorder or scanner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Literal

from msdr import oui
from msdr.wifi.observations import DEFAULT_MAX_BSSIDS

SurveyState = Literal["idle", "running", "stopping", "done", "stopped", "failed"]
# What a running survey is doing right now: restarting the helper on the next
# channel, or counting dwell time on it.
SurveyPhase = Literal["switching", "listening"]
ChannelStatus = Literal["pending", "switching", "listening", "done", "failed"]

# Guess for one helper restart before any hop has been measured; replaced by
# the measured average once the first hop is ready.
SWITCH_ESTIMATE_S = 5.0

# health_state() values that mean the helper will never start receiving on
# its own -- waiting any longer is pointless, so a hop in this state fails
# the whole survey immediately (docs/history.md: "survey failed with the
# error").
_TERMINAL_HEALTH_STATES = frozenset({"unavailable", "dependency_missing", "no_device", "failed"})

# How long a hop may spend waiting for the supervisor to leave "starting"
# before the survey gives up on it (a hung helper must not wedge the survey
# forever). Must outlast WifiSupervisor's crash-retry schedule (backoffs
# 1+2+5+10 s) plus one helper start (USB reset and firmware load take a few
# seconds each): the ACH's firmware load fails now and then, and a hop that
# needs a retry or two must still be able to come up.
HOP_READY_TIMEOUT_S = 45.0

# A channel that can't be brought up is recorded as failed and skipped, so a
# single bad hop doesn't throw away a 38-channel survey. This many failures
# in a row means the adapter itself is gone, and the survey stops instead
# of spending HOP_READY_TIMEOUT_S on every remaining channel.
MAX_CONSECUTIVE_FAILURES = 3

# Poll granularity while waiting for "receiving"/"stale" and while dwelling.
_POLL_S = 0.01

# Per-channel "top" networks list is capped (docs/history.md: "up to 50").
_TOP_LIMIT = 50


class ChannelSurveyError(Exception):
    """A hop could not complete (helper reached a terminal health state, or
    never became ready in time) -- caught by `_run` and turned into the
    survey's own "failed" state with `.error` set."""


@dataclass
class ChannelResult:
    """Per-channel results, accumulated across every visit (docs/history.md:
    "The network table keeps accumulating across hops during a survey")."""

    channel: int
    visits: int = 0
    listened_s: float = 0.0
    frames: int = 0
    networks: int = 0
    top: list[dict] = field(default_factory=list)
    status: ChannelStatus = "pending"
    error: str | None = None  # why the last visit failed, for status "failed"

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "status": self.status,
            "error": self.error,
            "visits": self.visits,
            "listened_s": round(self.listened_s, 2),
            "frames": self.frames,
            "networks": self.networks,
            "top": self.top,
        }


class ChannelSurvey:
    def __init__(
        self,
        supervisor,
        *,
        channels: list[int],
        dwell_s: float,
        rounds: int,
    ) -> None:
        self.supervisor = supervisor
        self.channels = list(channels)
        self.dwell_s = dwell_s
        self.rounds = rounds

        self.state: SurveyState = "idle"
        self.phase: SurveyPhase | None = None
        self.round = 0
        self.current_channel: int | None = None
        self.channel_ends_at: float | None = None
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.error: str | None = None

        self._results: dict[int, ChannelResult] = {
            ch: ChannelResult(channel=ch) for ch in self.channels
        }
        self._stop_requested = False
        self.task: asyncio.Task | None = None
        # The dwell in progress: (channel, listen_start, frames_start), so
        # status() can report it live and a stop can keep what it heard.
        self._listening: tuple[int, float, int] | None = None
        self._switch_times: list[float] = []
        self._consecutive_failures = 0

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.state = "running"
        self.started_at = time.time()
        self.task = asyncio.create_task(self._run())

    def request_stop(self) -> None:
        """Mark the survey as stopping right away (no awaiting): keep the
        partial dwell of the channel being listened to, so a stop never
        throws away what was heard. `stop()` then cancels the hop task."""
        if self.state != "running":
            return
        self._stop_requested = True
        self._finish_listening(time.time())
        self._settle_switching()
        self.phase = None
        self.channel_ends_at = None
        self.state = "stopping"

    async def stop(self, *, finalize: bool = True) -> None:
        """Stop between/mid hop. Idempotent -- a survey that already finished
        on its own (done/failed) is left alone. `finalize=False` leaves it in
        "stopping" for a caller that still has work to do (restoring the
        prior capture) before it may call itself stopped."""
        self.request_stop()
        self._stop_requested = True
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if finalize and self.state in ("running", "stopping"):
            self.state = "stopped"
            self.ended_at = time.time()

    # --- the hop loop --------------------------------------------------------

    async def _run(self) -> None:
        try:
            visited_round = 0
            while self.rounds == 0 or visited_round < self.rounds:
                visited_round += 1
                self.round = visited_round
                for channel in self.channels:
                    if self._stop_requested:
                        return
                    try:
                        await self._visit(channel)
                    except ChannelSurveyError as exc:
                        result = self._results[channel]
                        result.status, result.error = "failed", str(exc)
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            raise ChannelSurveyError(
                                f"{self._consecutive_failures} channels in a row could not be "
                                f"started; last: {exc}"
                            ) from exc
                        continue
                    self._consecutive_failures = 0
                    if self._stop_requested:
                        return
            failed = [r for r in self._results.values() if r.status == "failed"]
            if failed and not any(r.visits for r in self._results.values()):
                raise ChannelSurveyError(failed[-1].error or "no channel could be started")
            self.state = "done"
            self.phase = None
            self.ended_at = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # ChannelSurveyError, or any bug: never hang in "running"
            self._listening = None
            self.state = "failed"
            self._settle_switching()
            for result in self._results.values():
                if result.status == "listening":
                    result.status = "done" if result.visits else "pending"
            self.phase = None
            self.error = str(exc) if isinstance(exc, ChannelSurveyError) else (
                f"internal error: {type(exc).__name__}: {exc}"
            )
            self.ended_at = time.time()

    async def _visit(self, channel: int) -> None:
        self.current_channel = channel
        self.phase = "switching"
        # The previous channel's end time must not linger as "0 s left"
        # while the helper restarts.
        self.channel_ends_at = None
        result = self._results[channel]
        result.status = "switching"
        switch_started = time.monotonic()
        await self.supervisor.start(channel, configured_channel=channel, keep_table=True)

        ready_deadline = time.monotonic() + HOP_READY_TIMEOUT_S
        while True:
            state = self.supervisor.health_state()
            if state in ("receiving", "stale"):
                break
            if state in _TERMINAL_HEALTH_STATES:
                raise ChannelSurveyError(
                    f"channel {channel}: helper is {state}{self._helper_detail()}"
                )
            if time.monotonic() >= ready_deadline:
                raise ChannelSurveyError(
                    f"channel {channel}: helper did not start within "
                    f"{HOP_READY_TIMEOUT_S:.0f} s{self._helper_detail()}"
                )
            if self._stop_requested:
                return
            await asyncio.sleep(_POLL_S)

        self._switch_times.append(time.monotonic() - switch_started)
        listen_start = time.time()
        self._listening = (channel, listen_start, self.supervisor.frames)
        self.phase = "listening"
        result.status = "listening"
        self.channel_ends_at = listen_start + self.dwell_s

        remaining = self.dwell_s
        while remaining > 0 and not self._stop_requested:
            step = min(_POLL_S, remaining)
            await asyncio.sleep(step)
            remaining -= step
        self._finish_listening(time.time())

    def _helper_detail(self) -> str:
        """The supervisor's own last error (e.g. the helper's firmware
        failure), so a failed hop says why rather than just that it timed out."""
        detail = getattr(self.supervisor, "last_error", None)
        return f" ({detail})" if detail else ""

    def _settle_switching(self) -> None:
        """A hop cut short while the helper restarted heard nothing there:
        back to "done" if an earlier round visited it, else "pending"."""
        for result in self._results.values():
            if result.status == "switching":
                result.status = "done" if result.visits else "pending"

    def _finish_listening(self, listen_end: float) -> None:
        """Fold the dwell in progress (if any) into its channel's results."""
        if self._listening is None:
            return
        channel, listen_start, frames_start = self._listening
        self._listening = None
        networks, top = self._collect(listen_start, listen_end)  # may raise: update nothing first
        result = self._results[channel]
        result.visits += 1
        result.listened_s += listen_end - listen_start
        result.frames += self.supervisor.frames - frames_start
        result.networks, result.top = networks, top
        result.status = "done"

    def _collect(self, listen_start: float, listen_end: float) -> tuple[int, list[dict]]:
        """Distinct BSSIDs whose good frames arrived while this channel was
        tuned (`last_seen` inside the dwell window), strongest first,
        capped at `_TOP_LIMIT` (docs/history.md). `ds_channel` is the row's
        own `channel` -- it differs from the configured channel exactly when
        a beacon heard here announced another one (2.4 GHz overlap)."""
        table = self.supervisor.table
        if table is None:
            return 0, []
        rows, _total = table.rows(sort="signal", order="desc", limit=DEFAULT_MAX_BSSIDS)
        in_window = [r for r in rows if listen_start <= r["last_seen"] <= listen_end]
        top = [
            {
                "bssid": r["bssid"],
                "vendor": oui.vendor(r["bssid"]),
                "local_admin": oui.is_locally_administered(r["bssid"]),
                "ssid": r["ssid"],
                "ds_channel": r["channel"],
                "signal": r["signal_raw_code"],
            }
            for r in in_window[:_TOP_LIMIT]
        ]
        return len(in_window), top

    # --- status --------------------------------------------------------------

    def _estimated_total_s(self) -> float | None:
        """Whole-run estimate: (dwell + one helper restart) per channel per
        round, using the measured restart time once a hop has been timed.
        None for an open-ended survey (rounds 0)."""
        if not self.rounds:
            return None
        switch = (
            sum(self._switch_times) / len(self._switch_times)
            if self._switch_times else SWITCH_ESTIMATE_S
        )
        return round(len(self.channels) * self.rounds * (self.dwell_s + switch), 1)

    def _results_snapshot(self) -> list[dict]:
        """Per-channel results, with the dwell in progress counted live so
        the table and network list fill while a channel is being heard."""
        rows = {ch: self._results[ch].as_dict() for ch in self.channels}
        if self._listening is not None:
            channel, listen_start, frames_start = self._listening
            now = time.time()
            row = rows[channel]
            row["listened_s"] = round(row["listened_s"] + now - listen_start, 2)
            row["frames"] += self.supervisor.frames - frames_start
            row["networks"], row["top"] = self._collect(listen_start, now)
        return [rows[ch] for ch in self.channels]

    def status(self) -> dict:
        return {
            "state": self.state,
            "phase": self.phase,
            "estimated_total_s": self._estimated_total_s(),
            "failed_channels": sum(1 for r in self._results.values() if r.status == "failed"),
            "switch_s": (
                round(sum(self._switch_times) / len(self._switch_times), 2)
                if self._switch_times else None
            ),
            "channels": self.channels,
            "dwell_s": self.dwell_s,
            "rounds": self.rounds,
            "round": self.round,
            "current_channel": self.current_channel,
            "channel_ends_at": self.channel_ends_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "results": self._results_snapshot(),
        }
