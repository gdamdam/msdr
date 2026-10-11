"""Dwell observer: notices when the operator has settled on a new signal via
SDRconnect's own GUI (spec §7) and reports it once things go quiet.

Only "key" properties (frequency, mode, bandwidth, antenna) count as changing
what the operator is listening to; gain/audio tweaks don't reset the timer.
`cause="own"` changes belong to a restore msdr itself is driving, so they
cancel any pending timer rather than starting one.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from msdr.engine.confirmed import ConfirmedState, StateChanged
from msdr.engine.state import Observation, ReceiverState

_KEY_PROPS = frozenset(
    {"device_vfo_frequency", "demodulator", "filter_bandwidth", "active_antenna"}
)


class DwellObserver:
    def __init__(
        self,
        confirmed: ConfirmedState,
        *,
        dwell_seconds: float,
        on_dwell: Callable[[ReceiverState, Observation], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._confirmed = confirmed
        self.dwell_seconds = dwell_seconds
        self._on_dwell = on_dwell
        self._clock = clock
        self._queue: asyncio.Queue[StateChanged] | None = None
        self._consumer_task: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None
        self._last_app_action: float = float("-inf")
        self._last_emitted_key: tuple | None = None
        # The scanner (v0.12.0) sets this while it's driving the VFO itself,
        # so its own sweeping never gets journalled as "operator dwelled
        # here" -- a flag rather than start()/stop() so the subscription (and
        # any timer already in flight when a scan starts) survives untouched.
        self.suspended = False

    async def start(self) -> None:
        self._queue = self._confirmed.subscribe()
        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        await self._cancel_timer()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        if self._queue is not None:
            self._confirmed.unsubscribe(self._queue)
            self._queue = None

    def note_app_action(self) -> None:
        self._last_app_action = self._clock()

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            sc = await self._queue.get()
            if sc.cause == "own":
                # Belongs to a restore msdr is driving -- not a dwell candidate.
                await self._cancel_timer()
            elif sc.property in _KEY_PROPS:
                await self._cancel_timer()
                self._timer_task = asyncio.create_task(self._fire_after_dwell())
            # Non-key external changes (gain, squelch, ...) never touch the timer.

    async def _cancel_timer(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer_task
            self._timer_task = None

    async def _fire_after_dwell(self) -> None:
        await asyncio.sleep(self.dwell_seconds)
        self._timer_task = None
        if self.suspended:
            return
        if not self._confirmed.is_complete():
            return
        if self._clock() - self._last_app_action < self.dwell_seconds:
            return
        snapshot = self._confirmed.snapshot("observed")
        key = snapshot.key()
        if key == self._last_emitted_key:
            return
        self._last_emitted_key = key
        await self._on_dwell(snapshot, self._confirmed.observation())
