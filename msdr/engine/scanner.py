"""Server-side scanner engine: sweeps a list of signals, a frequency range, or
(v0.13.0) live spectrum, tuning the same way AppService's manual tune does
(via plan_with_desired + RestoreExecutor) but without history/journal side
effects, and holds on any channel whose SNR crosses a threshold.

List/range channels come from a `Source` (ListSource | RangeSource) through
`build_channels()` -- the scan loop (_run) only ever sees the resulting
`list[Channel]` and never looks at where they came from. SpectrumSource is
different: there's no fixed channel list to precompute, so ScannerEngine runs
a separate loop (_run_spectrum) that detects candidates live from the
adapter's spectrum stream each cycle, then reuses `_visit_channel` (tune/
settle/dwell/hold) exactly like A/B.

This module never imports the web framework (only msdr/api/* may -- see api/app.py's
own docstring): source/state errors are plain ValueError subclasses that
AppService maps to HTTP status codes.
"""

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.engine.restore import RestoreExecutor, plan_with_desired
from msdr.engine.state import TuneRecipe
from msdr.library.frequency import format_mhz
from msdr.library.models import Signal, now_iso
from msdr.radio.protocol import RadioAdapter, SpectrumFrame
from msdr.storage.repos import ListRepo, SignalRepo

if TYPE_CHECKING:
    from msdr.survey.spectrum_hub import SpectrumHub

logger = logging.getLogger(__name__)

# A channel visit this short (settle) mustn't stall the loop, and a poll this
# fine keeps hang/dwell timing accurate down to the "tens of ms" tests use.
_TICK_S = 0.01
# S-meter-style updates during dwell/hold are throttled like AppService's own
# telemetry publish, just a little faster per the design spec (~4 Hz).
_SNR_PUBLISH_MIN_INTERVAL_S = 0.25
_HITS_KEEP = 200
_MAX_CHANNELS = 10_000
_DEFAULT_SETTLE_MS = 150
_DEFAULT_THRESHOLD_SNR_DB = 10.0
_DEFAULT_HANG_S = 2.0
# Scanner finds log (2026-09-25 plan): classify() is service-provided and may
# hit a DB/decoder -- bounded so a slow/hanging classifier can't stall the scan.
_CLASSIFY_TIMEOUT_S = 2.0
# --- spectrum source (v0.13.0) -- see docs/research/2026-09-22-sdrconnect-
# websocket-api.md Sec7 for the measured facts these constants encode. ------
_SPECTRUM_BINS = 512
_SPECTRUM_FRAME_HISTORY = 5
_SPECTRUM_CENTER_BIN = 256  # DC/LO spike
_SPECTRUM_CENTER_EXCLUDE_BINS = 2  # +/- around the centre bin
_SPECTRUM_EDGE_FRACTION = 0.03  # outer 3% of bins excluded each side
_SPECTRUM_IDLE_SLEEP_S = 0.3
_SPECTRUM_SEGMENT_STEP_FRACTION = 0.8
_SPECTRUM_STATUS_BINS = 128  # 512 -> 128 by max, for the status broadcast
# A scan pass in which every channel was an ignored find rests this long.
_ALL_IGNORED_REST_S = 1.0
_SPECTRUM_PUBLISH_MIN_INTERVAL_S = 0.5  # ~2 Hz cap on the extra status pushes
_DEFAULT_SPECTRUM_STEP_HZ = 12_500
_DEFAULT_SPECTRUM_THRESHOLD = 20
_DEFAULT_SPECTRUM_DEMODULATOR = "NFM"
_DEFAULT_SPECTRUM_FILTER_BW_HZ = 12_500
# A restore/tune touching any of these away from what the scanner itself just
# commanded is the operator (or another AppService action) taking the radio
# back -- mirrors how the restore engine tells "own" from "external" changes,
# but compares against what the scanner is doing rather than an expectation
# dict, since a manual *tune* is itself a legitimate "own" echo to something
# else's expectations.
_OVERRIDE_PROPS = frozenset({"device_vfo_frequency", "device_center_frequency", "demodulator"})


class ScanSourceError(ValueError):
    """Base for a bad scan source or config. Plain ValueError (not
    HTTPException, see module docstring) -- AppService maps subclasses to the
    right status code."""


class ListSourceNotFound(ScanSourceError):
    pass


class TooManyChannels(ScanSourceError):
    pass


class EmptyChannelSet(ScanSourceError):
    pass


@dataclass(frozen=True)
class ListSource:
    list_id: str
    type: Literal["list"] = "list"


@dataclass(frozen=True)
class RangeSource:
    start_hz: int
    stop_hz: int
    step_hz: int
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = None
    type: Literal["range"] = "range"


@dataclass(frozen=True)
class SpectrumSource:
    """Channels aren't precomputed for this source -- ScannerEngine drives a
    separate loop (_run_spectrum) that detects candidates live from the
    spectrum stream each cycle. build_channels() is never called for it."""

    start_hz: int | None = None
    stop_hz: int | None = None
    step_hz: int = _DEFAULT_SPECTRUM_STEP_HZ
    level_threshold: int = _DEFAULT_SPECTRUM_THRESHOLD
    demodulator: str = _DEFAULT_SPECTRUM_DEMODULATOR
    filter_bandwidth_hz: int = _DEFAULT_SPECTRUM_FILTER_BW_HZ
    type: Literal["spectrum"] = "spectrum"


Source = ListSource | RangeSource | SpectrumSource


def validate_spectrum_source(source: SpectrumSource) -> None:
    """Raised the same way build_channels() raises for list/range -- AppService
    maps ScanSourceError subclasses to HTTP status codes."""
    if source.step_hz <= 0:
        raise ScanSourceError("step_hz must be positive")
    if (
        source.start_hz is not None
        and source.stop_hz is not None
        and source.start_hz >= source.stop_hz
    ):
        raise ScanSourceError("stop_hz must be > start_hz")


@dataclass(frozen=True)
class Channel:
    index: int
    frequency_hz: int
    demodulator: str | None
    filter_bandwidth_hz: int | None
    signal_id: str | None
    label: str


@dataclass
class Hit:
    at: str
    frequency_hz: int
    label: str
    signal_id: str | None
    peak_snr_db: float
    duration_s: float = 0.0


_DEFAULT_FINDS_DWELL_S = 3.0


@dataclass(frozen=True)
class ScanConfig:
    source: Source
    dwell_ms: int
    settle_ms: int = _DEFAULT_SETTLE_MS
    threshold_snr_db: float = _DEFAULT_THRESHOLD_SNR_DB
    hang_s: float = _DEFAULT_HANG_S
    stop_on_hit: bool = False
    capture_hits: bool = False
    # Scanner finds log (2026-09-25 plan): all default to today's behaviour
    # (no logging, hold until hang_s like before, no ignore skipping).
    log_finds: bool = False
    keep_scanning: bool = False
    finds_dwell_s: float = _DEFAULT_FINDS_DWELL_S
    skip_ignored: bool = True


def default_dwell_ms(source: Source) -> int:
    """400 list / 250 range, per the design spec -- only used when the
    caller doesn't say dwell_ms explicitly."""
    return 400 if isinstance(source, ListSource) else 250


def build_channels(source: Source, *, lists: ListRepo, signals: SignalRepo) -> list[Channel]:
    if isinstance(source, ListSource):
        channels = _list_channels(source, lists=lists, signals=signals)
    elif isinstance(source, RangeSource):
        channels = _range_channels(source)
    else:  # pragma: no cover -- exhaustive over the Source union that exists today
        raise TypeError(f"unknown scan source: {source!r}")
    if not channels:
        raise EmptyChannelSet("scan source has no channels")
    if len(channels) > _MAX_CHANNELS:
        raise TooManyChannels(f"too many channels: {len(channels)} (max {_MAX_CHANNELS})")
    return channels


def _list_channels(source: ListSource, *, lists: ListRepo, signals: SignalRepo) -> list[Channel]:
    if lists.get(source.list_id) is None:
        raise ListSourceNotFound(f"list not found: {source.list_id!r}")
    rows = lists.item_rows(source.list_id)
    ids = [row["signal_id"] for row in rows if not row["skip"]]
    sigs = signals.get_many(ids)  # preserves list order, drops any dangling id
    return [
        Channel(
            index=i,
            frequency_hz=s.frequency_hz,
            demodulator=s.demodulator,
            filter_bandwidth_hz=s.filter_bandwidth_hz,
            signal_id=s.id,
            label=s.name,
        )
        for i, s in enumerate(sigs)
    ]


def _range_channels(source: RangeSource) -> list[Channel]:
    if source.step_hz <= 0:
        raise ScanSourceError("step_hz must be positive")
    if source.stop_hz < source.start_hz:
        raise ScanSourceError("stop_hz must be >= start_hz")
    count = (source.stop_hz - source.start_hz) // source.step_hz + 1  # inclusive of stop_hz
    if count > _MAX_CHANNELS:
        raise TooManyChannels(f"too many channels: {count} (max {_MAX_CHANNELS})")
    freq = source.start_hz
    channels = []
    for i in range(count):
        channels.append(
            Channel(
                index=i,
                frequency_hz=freq,
                demodulator=source.demodulator,
                filter_bandwidth_hz=source.filter_bandwidth_hz,
                signal_id=None,
                label=format_mhz(freq),
            )
        )
        freq += source.step_hz
    return channels


def _average_bins(frames: "deque[list[int]]") -> list[float]:
    n = len(frames)
    return [sum(f[i] for f in frames) / n for i in range(_SPECTRUM_BINS)]


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _downsample_max(values: list[float], out_len: int) -> list[int]:
    size = len(values)
    if out_len <= 0 or size == 0:
        return []
    group = max(1, size // out_len)
    return [int(max(values[i : i + group])) for i in range(0, size, group)][:out_len]


def _bin_to_hz(idx: float, *, center: int, sample_rate: int) -> float:
    """bin i -> center - sample_rate/2 + i * sample_rate/512: FFT order, bin 256
    is the centre (measured 2026-09-26 against an FFT of the IQ stream -- the
    old (i + 0.5) put every signal half a bin high)."""
    return center - sample_rate / 2 + idx * sample_rate / _SPECTRUM_BINS


def _spectrum_candidates(
    avg: list[float],
    *,
    floor: float,
    threshold: float,
    center: int,
    sample_rate: int,
    step_hz: int,
) -> list[tuple[int, float]]:
    """Level-weighted-centroid candidates, snapped to `step_hz` (grid anchored
    at 0 Hz), as (frequency_hz, level) ordered however `avg` iterates (caller
    sorts by level)."""
    n = len(avg)
    edge = int(n * _SPECTRUM_EDGE_FRACTION)
    lo_excl = _SPECTRUM_CENTER_BIN - _SPECTRUM_CENTER_EXCLUDE_BINS
    hi_excl = _SPECTRUM_CENTER_BIN + _SPECTRUM_CENTER_EXCLUDE_BINS
    eligible = [
        i
        for i in range(edge, n - edge)
        if avg[i] >= floor + threshold and not (lo_excl <= i <= hi_excl)
    ]
    if not eligible:
        return []
    clusters: list[list[int]] = [[eligible[0]]]
    for i in eligible[1:]:
        if i == clusters[-1][-1] + 1:
            clusters[-1].append(i)
        else:
            clusters.append([i])
    results: list[tuple[int, float]] = []
    for cluster in clusters:
        weight = sum(avg[i] for i in cluster)
        centroid = sum(i * avg[i] for i in cluster) / weight
        freq = _bin_to_hz(centroid, center=center, sample_rate=sample_rate)
        snapped = round(freq / step_hz) * step_hz
        results.append((snapped, max(avg[i] for i in cluster)))
    return results


class ScannerEngine:
    """One scan at a time; idle | running | holding | paused. Drives `adapter`
    itself via its own RestoreExecutor (own_subscription=True, like a plain
    engine-layer test would use RestoreExecutor directly) so it needs no
    plumbing from AppService's own event consumer -- only the confirmed state
    is shared."""

    def __init__(
        self,
        adapter: RadioAdapter,
        confirmed: ConfirmedState,
        lists: ListRepo,
        signals: SignalRepo,
        dwell: DwellObserver,
        *,
        get_nearby_hz: Callable[[], int],
        capture: Callable[[TuneRecipe | None], Awaitable[Signal]],
        on_status: Callable[[dict], Awaitable[None]] | None = None,
        on_hit: Callable[[dict], Awaitable[None]] | None = None,
        spectrum_hub: "SpectrumHub | None" = None,
        on_find: Callable[[dict], Awaitable[None]] | None = None,
        classify: Callable[[int], Awaitable[dict | None]] | None = None,
        is_ignored: Callable[[int], bool] | None = None,
    ) -> None:
        self._adapter = adapter
        self._confirmed = confirmed
        self._lists = lists
        self._signals = signals
        self._dwell = dwell
        self._get_nearby_hz = get_nearby_hz
        self._capture = capture
        self._on_status = on_status
        self._on_hit = on_hit
        # Scanner finds log (2026-09-25 plan) -- all optional/None so a caller
        # that doesn't wire them up gets exactly today's behaviour.
        self._on_find = on_find
        self._classify = classify
        self._is_ignored = is_ignored
        self._executor = RestoreExecutor(adapter, confirmed)
        # Migration to msdr.survey.SpectrumHub (targeted, see module docstring
        # update below): when set, this engine acquires/releases a consumer
        # queue from the hub instead of calling set_spectrum_stream itself, so
        # scanning and Survey can share the one stream. None keeps today's
        # behaviour exactly (direct adapter calls).
        self._spectrum_hub = spectrum_hub
        self._hub_queue: asyncio.Queue | None = None

        self.state: Literal["idle", "running", "holding", "paused"] = "idle"
        self.reason: str | None = None
        self.config: ScanConfig | None = None
        self.channel: Channel | None = None
        self.position: int = 0
        self.total: int = 0
        self.snr_db: float | None = None
        self.hits: list[Hit] = []
        self.started_at: str | None = None
        self.skipped_ignored: int = 0
        # True after a whole pass tuned nothing because every channel is
        # ignored; cleared as soon as a pass tunes at least one channel.
        self.all_ignored: bool = False
        self._skipped_this_pass = 0

        self._channels: list[Channel] = []
        self._idx = 0  # _run()'s cursor into _channels; skip() also rewrites it (see skip())
        self._stop = False
        self._skip = False
        self._manual_hold = False
        # True only while this engine's own tune step (plus its settle_ms
        # absorption window) is in flight -- the override watcher ignores
        # confirmed-state changes while this is set, same as restore.py's own
        # _settle() absorbs a device's coupled echoes after a step confirms.
        self._tuning = False
        self._wake = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._last_snr_publish_at = float("-inf")

        # --- spectrum source (v0.13.0) --------------------------------------
        self._spectrum_watch_task: asyncio.Task | None = None
        self._spectrum_frames: deque[list[int]] = deque(maxlen=_SPECTRUM_FRAME_HISTORY)
        self._spectrum_floor: float | None = None
        self._spectrum_candidate_hz: list[int] = []
        # Cleared per sweep/segment (a frequency just tried); session lockout
        # (explicit skip()) persists for the whole scan -- see skip() below.
        self._spectrum_visited: set[int] = set()
        self._spectrum_lockout: set[int] = set()
        self._last_spectrum_publish_at = float("-inf")

    @property
    def is_tuning(self) -> bool:
        """True while this engine's own RestoreExecutor is mid-step -- lets
        AppService._restore() treat a concurrent manual tune request the same
        way it treats its own executor already running (409), rather than
        risk two RestoreExecutors driving the same ConfirmedState at once."""
        return self._tuning

    # --- lifecycle -----------------------------------------------------

    async def start(self, config: ScanConfig) -> dict:
        is_spectrum = isinstance(config.source, SpectrumSource)
        if is_spectrum:
            validate_spectrum_source(config.source)
            channels: list[Channel] = []
        else:
            channels = build_channels(config.source, lists=self._lists, signals=self._signals)
        self.config = config
        self._channels = channels
        self.total = len(channels)
        self.position = 0
        self.channel = None
        self.hits = []
        self.snr_db = None
        self.reason = None
        self.started_at = now_iso()
        self.skipped_ignored = 0
        self.all_ignored = False
        self._skipped_this_pass = 0
        self._stop = False
        self._skip = False
        self._manual_hold = False
        self._tuning = False
        self._last_snr_publish_at = float("-inf")
        self._spectrum_frames.clear()
        self._spectrum_floor = None
        self._spectrum_candidate_hz = []
        self._spectrum_visited = set()
        self._spectrum_lockout = set()
        self._last_spectrum_publish_at = float("-inf")
        self._wake = asyncio.Event()
        self._dwell.suspended = True
        self.state = "running"
        self._run_task = asyncio.create_task(self._run_spectrum() if is_spectrum else self._run())
        await self._publish_status()
        return self.status()

    async def _run(self) -> None:
        self._watch_task = asyncio.create_task(self._watch_overrides())
        try:
            self._idx = 0
            while True:
                if self.state == "paused":
                    await self._wake.wait()
                    self._wake.clear()
                    if self._stop:
                        break
                    continue
                if self._stop:
                    break
                # Recomputed each pass (not cached): skip(persist=True) can
                # shrink _channels mid-scan (see skip()), which also rewrites
                # _idx to land on the right survivor.
                n = len(self._channels)
                if n == 0:
                    break  # every channel got skipped away
                channel = self._channels[self._idx % n]
                self.position = self._idx % n
                result = await self._visit_channel(channel)
                if result == "stop":
                    break
                if result == "paused":
                    continue
                if not self._manual_hold:
                    self._idx += 1
                    if self._idx % n == 0:
                        # End of a pass. Every channel ignored means nothing was
                        # tuned or dwelt on all pass: rest before the next one
                        # instead of spinning (interruptible like any dwell).
                        # Visible outcome (plan S5 lifecycle case): the UI
                        # must be able to say "every channel is ignored"
                        # rather than look like a scan that finds nothing.
                        self.all_ignored = self._skipped_this_pass >= n
                        if self.all_ignored:
                            await self._sleep(_ALL_IGNORED_REST_S)
                        self._skipped_this_pass = 0
                # else: manual hold keeps revisiting the same channel
        finally:
            if self._watch_task is not None:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
                self._watch_task = None
            self._dwell.suspended = False
            self.state = "idle"
            self.reason = None
            self.channel = None
            self._run_task = None
            await self._publish_status()

    async def _watch_overrides(self) -> None:
        q = self._confirmed.subscribe()
        try:
            while True:
                sc = await q.get()
                if self._tuning or sc.property not in _OVERRIDE_PROPS:
                    continue
                if self.state not in ("running", "holding"):
                    continue
                await self.pause("manual tune")
        finally:
            self._confirmed.unsubscribe(q)

    # --- spectrum source: live candidate detection (v0.13.0) ---------------

    async def _run_spectrum(self) -> None:
        self._watch_task = asyncio.create_task(self._watch_overrides())
        if self._spectrum_hub is not None:
            self._hub_queue = self._spectrum_hub.acquire()
        else:
            with contextlib.suppress(ConnectionError):
                await self._adapter.set_spectrum_stream(True)
        self._spectrum_watch_task = asyncio.create_task(self._watch_spectrum_frames())
        try:
            while True:
                if self.state == "paused":
                    await self._wake.wait()
                    self._wake.clear()
                    if self._stop:
                        break
                    continue
                if self._stop:
                    break
                if self._manual_hold and self.channel is not None:
                    channel = self.channel  # manual hold keeps revisiting it
                else:
                    channel = await self._next_spectrum_candidate()
                    if channel is None:
                        continue  # idle sleep / segment step already happened
                result = await self._visit_channel(channel)
                if result == "stop":
                    break
                if result == "paused":
                    continue
                self._spectrum_visited.add(channel.frequency_hz)
        finally:
            for task_attr in ("_spectrum_watch_task", "_watch_task"):
                task = getattr(self, task_attr)
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    setattr(self, task_attr, None)
            if self._spectrum_hub is not None:
                if self._hub_queue is not None:
                    self._spectrum_hub.release(self._hub_queue)
                    self._hub_queue = None
            else:
                with contextlib.suppress(ConnectionError):
                    await self._adapter.set_spectrum_stream(False)
            self._dwell.suspended = False
            self.state = "idle"
            self.reason = None
            self.channel = None
            self._run_task = None
            await self._publish_status()

    async def _watch_spectrum_frames(self) -> None:
        # Hub-fed queue only ever carries SpectrumFrame; the direct adapter
        # subscription also sees PropertyChanged/ConnectionChanged, hence the
        # isinstance filter kept for both paths.
        q = self._hub_queue if self._spectrum_hub is not None else self._adapter.subscribe()
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, SpectrumFrame):
                    self._spectrum_frames.append(list(ev.bins))
        finally:
            if self._spectrum_hub is None:
                self._adapter.unsubscribe(q)

    async def _next_spectrum_candidate(self) -> Channel | None:
        assert self.config is not None and isinstance(self.config.source, SpectrumSource)
        source = self.config.source
        center = self._confirmed.props.get("device_center_frequency")
        sample_rate = self._confirmed.props.get("device_sample_rate")
        if not self._spectrum_frames or center is None or sample_rate is None:
            await self._sleep(0.05)
            return None
        center = int(center)
        sample_rate = int(sample_rate)
        avg = _average_bins(self._spectrum_frames)
        floor = _median(avg)
        self._spectrum_floor = floor
        raw = _spectrum_candidates(
            avg,
            floor=floor,
            threshold=source.level_threshold,
            center=center,
            sample_rate=sample_rate,
            step_hz=source.step_hz,
        )
        bin_width = sample_rate / _SPECTRUM_BINS
        lockout = self._spectrum_visited | self._spectrum_lockout
        candidates = [
            (freq, level)
            for freq, level in raw
            if (source.start_hz is None or freq >= source.start_hz)
            and (source.stop_hz is None or freq <= source.stop_hz)
            and not any(abs(freq - locked) <= bin_width for locked in lockout)
        ]
        candidates.sort(key=lambda t: t[1], reverse=True)
        self._spectrum_candidate_hz = [f for f, _ in candidates]
        await self._maybe_publish_spectrum()

        if not candidates:
            moved = await self._maybe_step_spectrum_segment(source, center, sample_rate)
            self._spectrum_visited.clear()
            if not moved:
                await self._sleep(_SPECTRUM_IDLE_SLEEP_S)
            return None

        freq, _level = candidates[0]
        return Channel(
            index=0,
            frequency_hz=freq,
            demodulator=source.demodulator,
            filter_bandwidth_hz=source.filter_bandwidth_hz,
            signal_id=None,
            label=f"spectrum {format_mhz(freq)}",
        )

    async def _maybe_step_spectrum_segment(
        self, source: SpectrumSource, center: int, sample_rate: int
    ) -> bool:
        """Bring the requested range into view. A range that fits in one
        span is recentred on (once) when the current span doesn't already
        cover it; a wider range is walked in ~0.8*sample_rate segments,
        jumping to its first segment when the centre is outside the range
        and wrapping back to the start past stop_hz. Returns False when
        nothing needed to move (no range, or it's already fully visible).

        Found on the real radio: without the "move into the range" cases a
        range outside the current span (e.g. FM broadcast while parked on
        162 MHz) never produced a candidate, so the scan sat idle forever."""
        if source.start_hz is None or source.stop_hz is None:
            return False
        start, stop = source.start_hz, source.stop_hz
        # Same ±0.45*sample_rate "visible" window the restore planner uses
        # to decide when a VFO move needs a recentre (engine/restore.py).
        half_visible = int(0.45 * sample_rate)
        step = max(1, int(_SPECTRUM_SEGMENT_STEP_FRACTION * sample_rate))
        if stop - start <= step:
            if center - half_visible <= start and stop <= center + half_visible:
                return False
            new_center = (start + stop) // 2
        elif not start <= center <= stop:
            new_center = start + step // 2
        else:
            # The last segment is pinned so stop_hz is visible; only once
            # there does the walk wrap back to the first segment.
            last_center = stop - step // 2
            if center >= last_center:
                new_center = start + step // 2
            else:
                new_center = min(center + step, last_center)
        if new_center == center:
            return False
        channel = Channel(
            index=0,
            frequency_hz=new_center,
            demodulator=None,
            filter_bandwidth_hz=None,
            signal_id=None,
            label="",
        )
        ok = await self._tune(channel)
        self._tuning = False
        self._spectrum_frames.clear()
        if ok and not self._stop and self.state != "paused":
            await self._sleep(self.config.settle_ms / 1000)
        return True

    async def _maybe_publish_spectrum(self) -> None:
        now = time.monotonic()
        if now - self._last_spectrum_publish_at < _SPECTRUM_PUBLISH_MIN_INTERVAL_S:
            return
        self._last_spectrum_publish_at = now
        await self._publish_status()

    # --- per-channel visit ------------------------------------------------

    async def _visit_channel(self, channel: Channel) -> Literal["advance", "paused", "stop"]:
        assert self.config is not None
        if (
            self.config.skip_ignored
            and self._is_ignored is not None
            and self._is_ignored(channel.frequency_hz)
        ):
            # An ignored find -- move past it without ever tuning there. This
            # branch has no await of its own: with every channel ignored the
            # scan loop never yielded, starving the API (STOP included) --
            # review 2026-09-26. Yield here, and let the pass loop below rest
            # when a whole pass was skipped.
            self.skipped_ignored += 1
            self._skipped_this_pass += 1
            await asyncio.sleep(0)
            return "advance"
        ok = await self._tune(channel)
        if self._stop:
            self._tuning = False
            return "stop"
        if not ok:
            self._tuning = False
            return "paused"
        self.channel = channel
        await self._publish_status()
        await self._sleep(self.config.settle_ms / 1000)  # still _tuning: absorbs coupled echoes
        self._tuning = False
        if self._stop:
            return "stop"
        if self.state == "paused":
            return "paused"
        if self._skip:
            self._skip = False
            return "advance"

        peak = await self._sample_dwell()
        if self._stop:
            return "stop"
        if self.state == "paused":
            return "paused"
        if self._skip:
            self._skip = False
            return "advance"

        if peak is not None and peak >= self.config.threshold_snr_db:
            await self._enter_hold(channel, peak)
            if self._stop:
                return "stop"
            if self.state == "paused":
                return "paused"
        return "advance"

    async def _tune(self, channel: Channel) -> bool:
        """Tune to `channel` the same way AppService.tune_frequency does
        (plan_with_desired + RestoreExecutor), but with no Back/Forward
        history push and no journal entry -- this is the scanner sweeping,
        not an operator action."""
        self._tuning = True
        recipe = TuneRecipe(
            frequency_hz=channel.frequency_hz,
            demodulator=channel.demodulator,
            filter_bandwidth_hz=channel.filter_bandwidth_hz,
        )
        steps, desired = plan_with_desired(confirmed=self._confirmed.props, recipe=recipe)
        if not steps:
            return True  # already there
        report = await self._executor.run(steps, desired=desired)
        if report.status in ("failed", "interrupted"):
            detail = f": {report.detail}" if report.detail else ""
            await self.pause(f"tune {report.status}{detail}")
            return False
        return True

    async def _sample_dwell(self) -> float | None:
        assert self.config is not None
        deadline = time.monotonic() + self.config.dwell_ms / 1000
        peak: float | None = None
        while True:
            now = time.monotonic()
            if now >= deadline or self._stop or self.state == "paused" or self._skip:
                return peak
            snr = self._confirmed.props.get("signal_snr")
            if snr is not None:
                peak = snr if peak is None else max(peak, snr)
                self.snr_db = snr
                await self._maybe_publish_snr()
            await asyncio.sleep(min(_TICK_S, max(0.0, deadline - now)))

    async def _enter_hold(self, channel: Channel, initial_snr: float) -> None:
        assert self.config is not None
        self.state = "holding"
        hit = Hit(
            at=now_iso(),
            frequency_hz=channel.frequency_hz,
            label=channel.label,
            signal_id=channel.signal_id,
            peak_snr_db=initial_snr,
        )
        self._append_hit(hit)
        await self._publish_hit(hit)
        await self._publish_status()

        start = time.monotonic()
        last_active = start
        while not self._stop and self.state == "holding" and not self._skip:
            snr = self._confirmed.props.get("signal_snr")
            now = time.monotonic()
            if snr is not None and snr >= self.config.threshold_snr_db:
                last_active = now
                hit.peak_snr_db = max(hit.peak_snr_db, snr)
                self.snr_db = snr
                await self._maybe_publish_snr()
            if now - last_active >= self.config.hang_s:
                break
            # keep_scanning: log-and-move-on mode -- cut the hold at
            # finds_dwell_s even while the channel is still active, instead of
            # waiting for hang_s (which could be a long time on a busy freq).
            if self.config.keep_scanning and now - start >= self.config.finds_dwell_s:
                break
            await asyncio.sleep(_TICK_S)
        hit.duration_s = time.monotonic() - start

        await self._maybe_log_find(hit)

        if self.config.capture_hits and isinstance(self.config.source, RangeSource):
            await self._maybe_capture_hit(hit, channel)

        if self._stop:
            return
        if self._skip:
            self._skip = False
            return
        if self.state != "holding":
            return  # paused mid-hold (manual override / disconnect / manual pause)
        if self.config.stop_on_hit:
            self._stop = True
            return
        self.state = "running"
        await self._publish_status()

    async def _maybe_log_find(self, hit: Hit) -> None:
        assert self.config is not None
        if not (self.config.log_finds and self._on_find is not None):
            return
        # demodulator/bandwidth come from confirmed state at hold end (what
        # the receiver actually settled on), not the channel's own recipe.
        demodulator = self._confirmed.props.get("demodulator")
        bandwidth_hz = self._confirmed.props.get("filter_bandwidth")
        try:
            bandwidth_hz = int(bandwidth_hz) if bandwidth_hz is not None else None
        except (TypeError, ValueError):
            bandwidth_hz = None
        classification: dict | None = None
        if self._classify is not None:
            try:
                classification = await asyncio.wait_for(
                    self._classify(hit.frequency_hz), _CLASSIFY_TIMEOUT_S
                )
            except Exception:
                # Timeout or classifier error -- the find is still logged,
                # just without a classification.
                logger.debug("classify(%s) failed/timed out", hit.frequency_hz, exc_info=True)
                classification = None
        payload = {
            "ts": time.time(),
            "frequency_hz": hit.frequency_hz,
            "label": hit.label,
            "signal_id": hit.signal_id,
            "peak_snr_db": hit.peak_snr_db,
            "duration_s": hit.duration_s,
            "demodulator": demodulator,
            "bandwidth_hz": bandwidth_hz,
            "classification": classification,
        }
        try:
            await self._on_find(payload)
        except Exception:
            # A find-log failure must never break the scan itself.
            logger.exception("on_find callback failed for %s", hit.frequency_hz)

    async def _maybe_capture_hit(self, hit: Hit, channel: Channel) -> None:
        nearby = self._signals.near(hit.frequency_hz, self._get_nearby_hz())
        if nearby:
            return  # a memory already covers this frequency -- don't duplicate the inbox
        # The channel's own recipe, not whatever the receiver is tuned to right
        # now -- a manual override can retune it mid-hold (pausing the scan,
        # see _watch_overrides) before this runs, and the memory must record
        # the hit's actual frequency/mode/bandwidth, not the operator's.
        recipe = TuneRecipe(
            frequency_hz=channel.frequency_hz,
            demodulator=channel.demodulator,
            filter_bandwidth_hz=channel.filter_bandwidth_hz,
        )
        with contextlib.suppress(ConnectionError):
            await self._capture(recipe)

    # --- sleeping / publishing ------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    def _append_hit(self, hit: Hit) -> None:
        self.hits.append(hit)
        if len(self.hits) > _HITS_KEEP:
            del self.hits[: len(self.hits) - _HITS_KEEP]

    async def _publish_status(self) -> None:
        if self._on_status is not None:
            await self._on_status(self.status())

    async def _publish_hit(self, hit: Hit) -> None:
        if self._on_hit is not None:
            await self._on_hit(_hit_dict(hit))

    async def _maybe_publish_snr(self) -> None:
        now = time.monotonic()
        if now - self._last_snr_publish_at < _SNR_PUBLISH_MIN_INTERVAL_S:
            return
        self._last_snr_publish_at = now
        await self._publish_status()

    # --- manual controls (AppService checks state/feature-flag first) ----

    async def pause(self, reason: str) -> dict:
        if self.state in ("running", "holding"):
            self.state = "paused"
            self.reason = reason
            # The stream is per-connection (Sec7): a disconnect has already
            # ended it, so there's nothing to send, but this keeps the
            # adapter's own idempotency flag in step for the resume below.
            # With a hub, it owns this reconnect handling instead (see
            # SpectrumHub._run's ConnectionChanged branch).
            if reason == "disconnected" and self._is_spectrum_scan() and self._spectrum_hub is None:
                with contextlib.suppress(ConnectionError):
                    await self._adapter.set_spectrum_stream(False)
            self._wake.set()
            await self._publish_status()
        return self.status()

    async def resume(self) -> dict:
        if self.state == "paused":
            if self._is_spectrum_scan() and self._spectrum_hub is None:
                # Reconnecting drops SDRconnect's spectrum stream even though
                # the scan itself just resumes -- re-enable it (idempotent).
                # A hub consumer doesn't need this: it never released its
                # queue across the pause, and the hub itself re-enables on
                # reconnect.
                with contextlib.suppress(ConnectionError):
                    await self._adapter.set_spectrum_stream(True)
            self.state = "running"
            self.reason = None
            self._wake.set()
            await self._publish_status()
        return self.status()

    def _is_spectrum_scan(self) -> bool:
        return self.config is not None and isinstance(self.config.source, SpectrumSource)

    async def stop(self) -> dict:
        self._stop = True
        self._wake.set()
        if self._run_task is not None:
            await self._run_task
        return self.status()

    def skip(self, *, persist: bool = False) -> dict:
        if (
            persist
            and self.channel is not None
            and self.channel.signal_id is not None
            and self.config is not None
            and isinstance(self.config.source, ListSource)
        ):
            self._lists.set_item_skip(self.config.source.list_id, self.channel.signal_id, True)
            # Keep the running scan's own channel list in sync with the DB --
            # otherwise "skip always" only takes effect on the *next* scan and
            # this one wraps the channel right back around (idx % n).
            try:
                pos = self._channels.index(self.channel)
            except ValueError:
                pos = None
            if pos is not None:
                del self._channels[pos]
                self.total = len(self._channels)
                # _run()'s _idx currently resolves to `pos`; the list just
                # shifted left under it, so back it up by one before _run()'s
                # normal advance (+1) lands on what's now at `pos` (or wraps
                # to 0 if `pos` was the last slot).
                self._idx = pos - 1
        if self._is_spectrum_scan() and self.channel is not None:
            self._spectrum_lockout.add(self.channel.frequency_hz)
        self._skip = True
        self._wake.set()
        return self.status()

    def toggle_hold(self) -> dict:
        self._manual_hold = not self._manual_hold
        return self.status()

    # --- status -----------------------------------------------------------

    def status(self, *, all_hits: bool = False) -> dict:
        hits = self.hits if all_hits else self.hits[-20:]
        return {
            "state": self.state,
            "reason": self.reason,
            "config": _config_dict(self.config),
            "channel": _channel_dict(self.channel),
            "position": self.position,
            "total": self.total,
            "snr_db": self.snr_db,
            "manual_hold": self._manual_hold,
            "hits": [_hit_dict(h) for h in hits],
            "started_at": self.started_at,
            "skipped_ignored": self.skipped_ignored,
            "all_ignored": self.all_ignored,
            "spectrum": self._spectrum_status(),
        }

    def _spectrum_status(self) -> dict | None:
        if not self._is_spectrum_scan():
            return None
        bins: list[int] = []
        if self._spectrum_frames:
            bins = _downsample_max(_average_bins(self._spectrum_frames), _SPECTRUM_STATUS_BINS)
        return {
            "center_hz": self._confirmed.props.get("device_center_frequency"),
            "sample_rate_hz": self._confirmed.props.get("device_sample_rate"),
            "floor": self._spectrum_floor,
            "bins": bins,
            "candidates": list(self._spectrum_candidate_hz),
            "locked_out": sorted(self._spectrum_visited | self._spectrum_lockout),
        }


def _config_dict(config: ScanConfig | None) -> dict | None:
    if config is None:
        return None
    return {
        "source": dataclasses.asdict(config.source),
        "dwell_ms": config.dwell_ms,
        "settle_ms": config.settle_ms,
        "threshold_snr_db": config.threshold_snr_db,
        "hang_s": config.hang_s,
        "stop_on_hit": config.stop_on_hit,
        "capture_hits": config.capture_hits,
        "log_finds": config.log_finds,
        "keep_scanning": config.keep_scanning,
        "finds_dwell_s": config.finds_dwell_s,
        "skip_ignored": config.skip_ignored,
    }


def _channel_dict(channel: Channel | None) -> dict | None:
    return dataclasses.asdict(channel) if channel is not None else None


def _hit_dict(hit: Hit) -> dict:
    return dataclasses.asdict(hit)
