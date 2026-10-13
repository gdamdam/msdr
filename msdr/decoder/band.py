"""BandDecoder: multi-channel background decoding from the recorder's IQ stream (P2).

Owns its own IQStream connection (never the single-channel AudioStream one --
spec P2 "Two IQ connections work simultaneously"), runs the channelizer and
peak detector once per IQ frame, and keeps one ProcessBackend per (channel,
decoder) pair, mirroring channel.py/manager.py's P1 pattern but keyed by the
channelizer's bin index `k` instead of "the one active frequency".
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import numpy as np

from msdr.decoder.backends import (
    DecodeEvent,
    DecoderError,
    ProcessBackend,
    build_backend,
    reap_cancelled,
)
from msdr.decoder.channelizer import AMChannel, NFMChannel, PolyphaseChannelizer
from msdr.decoder.peaks import Peak, PeakDetector
from msdr.decoder.readiness import decoder_facts
from msdr.decoder.registry import DECODERS

# A detected channel needs to be this much stronger than the weakest one
# already running before it's allowed to bump it (spec P2 "Rules").
_REPLACE_MARGIN_DB = 6.0
# Backpressure: more IQ frames than this queued for processing and the
# oldest is dropped (spec P2 "Rules").
_MAX_QUEUED_FRAMES = 10
# IQStream has no `connected` attribute; "connected" is instead "a frame
# arrived within this many seconds" (same idea as manager.py's _SILENCE_S).
_IQ_CONNECTED_TIMEOUT_S = 2.0


@dataclass
class BandChannel:
    frequency_hz: float
    source: Literal["memory", "detected", "pinned"]
    signal_id: str | None
    k: int
    residual_hz: float
    snr_db: float
    since: float
    last_event_at: float | None = None
    events: int = 0
    state: Literal["running", "error"] = "running"
    error: str | None = None
    # runtime only -- not part of the status payload
    decoders: list[str] = field(default_factory=list, repr=False, compare=False)
    # One NFMChannel per distinct decoder input_rate needed on this channel
    # (e.g. 22,050 Hz for POCSAG alongside 48,000 Hz for DMR), keyed by rate;
    # rate_by_decoder says which one feeds each backend.
    nfm_by_rate: dict[int, NFMChannel | AMChannel] = field(
        default_factory=dict, repr=False, compare=False
    )
    rate_by_decoder: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    backends: dict[str, ProcessBackend] = field(default_factory=dict, repr=False, compare=False)
    event_tasks: list[asyncio.Task] = field(default_factory=list, repr=False, compare=False)
    last_seen_at: float = field(default=0.0, repr=False, compare=False)
    # per-decoder session facts, kept across backend restarts (a seek
    # restarts the process, it doesn't start a new session).
    events_by_decoder: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    valid_by_decoder: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    last_event_ts_by_decoder: dict[str, float] = field(
        default_factory=dict, repr=False, compare=False
    )
    # (generation, sample_index) of the latest IQ frame fed to this
    # channel's backends -- the provenance stamped on its next event.
    last_input: tuple[int | None, int | None, int | None] = field(
        default=(None, None, None), repr=False, compare=False
    )


@dataclass(frozen=True)
class _QueuedIQFrame:
    frame: object
    span_generation: int


def _frame_to_iq(data: bytes) -> np.ndarray:
    """int16 interleaved I/Q (IQStream's format) -> complex64 in [-1, 1]."""
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    usable = (samples.size // 2) * 2
    samples = samples[:usable]
    return (samples[0::2] + 1j * samples[1::2]).astype(np.complex64)


class BandDecoder:
    def __init__(
        self,
        *,
        iq_stream_factory: Callable[[], object],
        decoders: list[str],
        binaries: dict[str, str | None],
        center_hz: float,
        sample_rate_hz: int,
        memories: list[tuple[float, str, list[str]]],
        max_channels: int,
        threshold_db: float,
        idle_s: float,
        spacing_hz: int,
        on_event: Callable[[DecodeEvent], None],
        on_status: Callable[[], None],
        detect: bool = True,
        source_tag: str = "band",
        calls_dir: Path | None = None,
        fax_dir: Path | None = None,
        track_generation: bool = False,
        mode: Literal["NFM", "AM"] = "NFM",
        am_bandwidth_hz: int = 8_000,
        am_squelch_db: float = 10.0,
    ):
        # One demodulation mode per band session: the decoders in a session
        # share it, and it keeps the channel list and API unchanged for NFM.
        self._mode = mode
        self._am_bandwidth_hz = am_bandwidth_hz
        self._am_squelch_db = am_squelch_db
        self._iq_stream_factory = iq_stream_factory
        self._decoders = list(decoders)
        self._binaries = binaries
        self._calls_dir = calls_dir
        self._fax_dir = fax_dir
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._memories = list(memories)
        self._max_channels = max_channels
        self._threshold_db = threshold_db
        self._idle_s = idle_s
        self._spacing_hz = spacing_hz
        self._on_event = on_event
        self._on_status = on_status
        self._source_tag = source_tag
        # Replay: a frame from a newer source generation restarts
        # every channel's backends before it is processed, and older frames
        # are dropped, so no stale symbols cross a seek. Live sources never
        # bump the generation, so this stays off for them.
        self._track_generation = track_generation
        self._generation: int | None = None
        self._next_sample_index: int | None = None
        self._pending_span: tuple[float, int] | None = None
        self.restarts = 0
        self.stale_frames = 0
        self.dropped_frame_gaps = 0
        # Serialises the pump's per-frame work with restart/retune so a
        # restart never races a block already being channelized.
        self._lock = asyncio.Lock()

        self._channelizer = PolyphaseChannelizer(int(sample_rate_hz), spacing_hz)
        # A fixed channel must be centered on its exact frequency. The PFB's
        # nearest bin can leave one FSK sideband in its transition band (a
        # live 152.840 MHz page at -10 kHz offset failed at 6 MS/s).
        self._fixed_frequency_hz = (
            memories[0][0] if not detect and max_channels == 0 and len(memories) == 1 else None
        )
        self._fixed_extractor = self._make_fixed_extractor()
        # detect=False (fixed single-channel decode, manager.py) skips the
        # peak detector entirely -- no detected channels are possible with
        # max_channels=0 anyway, and PeakDetector's FFT isn't free.
        self._peaks: PeakDetector | None = (
            PeakDetector(int(sample_rate_hz), center_hz, threshold_db=threshold_db)
            if detect
            else None
        )
        self._channels: dict[int, BandChannel] = {}
        self._candidates: list[Peak] = []

        self._stream = None
        self._reader_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)
        # A live IQFrame has no tune generation. Stamp frames as the reader
        # receives them so a frame already dequeued by the pump cannot cross a
        # retune while waiting for _lock. Replay has its own source generation.
        self._span_generation = 0
        self._accept_after_wall = 0.0

        self._since: float | None = None
        self.last_stop_reason: str | None = None
        self._frames = 0
        self._dropped = 0
        # Live IQFrame objects have no sample index, so a bounded-queue drop
        # needs an explicit discontinuity marker for the decoder pump.
        self._pending_frame_gap = False
        self._cpu_load = 0.0
        self._last_frame_at: float | None = None
        self._clock: Callable[[], float] = time.time
        self._perf: Callable[[], float] = time.perf_counter

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._stream = self._iq_stream_factory()
        await self._stream.start()
        await self._stream.enable(True)
        self._since = self._clock()

        for frequency_hz, signal_id, mem_decoders in self._memories:
            await self._add_memory_channel(frequency_hz, signal_id, mem_decoders)

        self._reader_task = asyncio.create_task(self._read_frames())
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        for task in (self._reader_task, self._pump_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._pump_task):
            if task is not None:
                await reap_cancelled(task)
        self._reader_task = None
        self._pump_task = None

        for channel in list(self._channels.values()):
            await self._stop_channel(channel)
        self._channels = {}
        self._candidates = []

        if self._stream is not None:
            await self._stream.stop()
            self._stream = None
        self._since = None
        self._last_frame_at = None

    async def retune(
        self, center_hz: float, sample_rate_hz: int, memories: list[tuple[float, str, list[str]]]
    ) -> None:
        """Rebuild the channelizer/detector for the new span; detected channels
        don't survive a retune (their k/residual are meaningless for a
        different centre or sample rate) but memory channels are recreated.
        Pinned channels (decode handoff) are re-added too, as long as
        their frequency still falls inside the new span -- otherwise they're
        dropped like a detected channel would be (reported in the next
        status(), no error raised)."""
        async with self._lock:
            if not self._track_generation:
                self._span_generation += 1
                # IQStream's own bounded queue can still contain frames read
                # before SDRconnect's tune confirmation. Drop those until a
                # fresh frame has had time to arrive on the new span.
                self._accept_after_wall = time.time() + 0.25
                while True:
                    try:
                        self._queue.get_nowait()
                        self._dropped += 1
                    except asyncio.QueueEmpty:
                        break
                self._pending_frame_gap = False
            await self._retune_locked(center_hz, sample_rate_hz, memories)
        self._on_status()

    async def _retune_locked(
        self, center_hz: float, sample_rate_hz: int, memories: list[tuple[float, str, list[str]]]
    ) -> None:
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._memories = list(memories)

        pinned = [
            (c.frequency_hz, c.signal_id, list(c.decoders))
            for c in self._channels.values()
            if c.source == "pinned"
        ]

        for channel in list(self._channels.values()):
            await self._stop_channel(channel)
        self._channels = {}
        self._candidates = []

        self._channelizer = PolyphaseChannelizer(int(sample_rate_hz), self._spacing_hz)
        self._fixed_frequency_hz = (
            memories[0][0]
            if self._peaks is None and self._max_channels == 0 and len(memories) == 1
            else None
        )
        self._fixed_extractor = self._make_fixed_extractor()
        if self._peaks is not None:
            self._peaks.retune(int(sample_rate_hz), center_hz)

        for frequency_hz, signal_id, mem_decoders in self._memories:
            await self._add_memory_channel(frequency_hz, signal_id, mem_decoders)

        half_span_hz = sample_rate_hz / 2
        for frequency_hz, signal_id, decoders in pinned:
            if abs(frequency_hz - center_hz) <= half_span_hz:
                await self._pin_channel(frequency_hz, signal_id, decoders)

    # -- generation handling (replay) ------------------------------------

    def expect_span(self, center_hz: float, sample_rate_hz: int) -> None:
        """Called synchronously from a source "run" listener: the next
        newer-generation frame retunes to this span instead of just
        restarting the backends (the pump applies it, in frame order)."""
        self._pending_span = (center_hz, int(sample_rate_hz))

    async def _on_new_generation(self) -> None:
        """Runs inside the pump (lock held), before the first frame of a new
        generation is processed."""
        # Anything still queued is at least this generation (FIFO), but an
        # older one could only be stale -- drop those explicitly.
        kept = []
        while not self._queue.empty():
            frame = self._queue.get_nowait()
            if getattr(frame, "generation", None) == self._generation:
                kept.append(frame)
            else:
                self.stale_frames += 1
        for frame in kept:
            self._queue.put_nowait(frame)
        span, self._pending_span = self._pending_span, None
        if span is not None and span != (self._center_hz, self._sample_rate_hz):
            await self._retune_locked(span[0], span[1], self._memories)
        else:
            self._reset_filter_state()
            for channel in self._channels.values():
                await self._restart_channel(channel)
        self.restarts += 1

    def _reset_filter_state(self) -> None:
        """Clear IQ history after a replay discontinuity without changing span."""
        self._channelizer = PolyphaseChannelizer(int(self._sample_rate_hz), self._spacing_hz)
        self._fixed_extractor = self._make_fixed_extractor()
        if self._peaks is not None:
            self._peaks.retune(int(self._sample_rate_hz), self._center_hz)
        self._candidates = []

    def _make_fixed_extractor(self):
        if self._fixed_frequency_hz is None:
            return None
        # Import here: source.dsp also reaches decoder modules through the
        # survey package during startup, so a module-level import cycles.
        from msdr.source.dsp import ChannelExtractor

        bandwidth = max(
            (DECODERS[d].default_bandwidth_hz for d in self._decoders if d in DECODERS),
            default=12_500,
        )
        cutoff = min(10_000.0, max(9_000.0, bandwidth / 2))
        return ChannelExtractor(
            self._sample_rate_hz,
            self._fixed_frequency_hz - self._center_hz,
            48_000,
            cutoff_hz=cutoff,
            protect_hz=cutoff,
        )

    async def _on_dropped_frame_gap(self) -> None:
        """Restart channelizer and protocol framing after lost IQ samples."""
        self.dropped_frame_gaps += 1
        self._next_sample_index = None
        self._reset_filter_state()
        for channel in self._channels.values():
            await self._restart_channel(channel)
        self.restarts += 1

    async def _restart_channel(self, channel: BandChannel) -> None:
        """Fresh backends and fresh demod state for the same decoders; the
        per-decoder counters survive (same session)."""
        decoder_ids = list(channel.decoders)
        await self._stop_channel(channel)
        channel.decoders = []
        channel.rate_by_decoder = {}
        channel.error = None
        channel.last_input = (None, None, None)
        await self._start_channel(channel, decoder_ids)

    # -- pinned channels (decode handoff) ---------------------------------

    async def pin(self, frequency_hz: float, signal_id: str | None, decoders: list[str]) -> None:
        """Runtime add of a memory-style channel that's never idle-retired
        (`_retire_idle` only ever touches `source == "detected"`) -- used to
        start decoding a peak the operator picked from the survey view."""
        await self._pin_channel(frequency_hz, signal_id, decoders)
        self._on_status()

    async def unpin(self, frequency_hz: float) -> None:
        k, _residual = self._channelizer.channel_for(frequency_hz, self._center_hz)
        channel = self._channels.get(k)
        if channel is not None and channel.source == "pinned":
            await self._stop_channel(channel)
            del self._channels[k]
        self._on_status()

    async def _pin_channel(
        self, frequency_hz: float, signal_id: str | None, decoders: list[str]
    ) -> None:
        decoder_ids = (
            [d for d in decoders if d in self._decoders] if decoders else list(self._decoders)
        )
        if not decoder_ids:
            return
        k, residual = self._channelizer.channel_for(frequency_hz, self._center_hz)
        existing = self._channels.get(k)
        if existing is not None:
            if existing.source == "pinned":
                return  # already pinned at this channel index
            await self._stop_channel(existing)
        now = self._clock()
        channel = BandChannel(
            frequency_hz=frequency_hz,
            source="pinned",
            signal_id=signal_id,
            k=k,
            residual_hz=residual,
            snr_db=0.0,
            since=now,
            last_seen_at=now,
        )
        await self._start_channel(channel, decoder_ids)
        self._channels[k] = channel

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        decoded_ks = set(self._channels)
        candidates = [
            p
            for p in self._candidates
            if self._channelizer.channel_for(p.frequency_hz, self._center_hz)[0] not in decoded_ks
        ]
        candidates.sort(key=lambda p: p.snr_db, reverse=True)
        return {
            "active": self._stream is not None,
            "decoders": list(self._decoders),
            "mode": self._mode,
            "am_bandwidth_hz": self._am_bandwidth_hz,
            "center_hz": self._center_hz,
            "sample_rate_hz": self._sample_rate_hz,
            "spacing_hz": self._channelizer.spacing_hz,
            "max_channels": self._max_channels,
            "threshold_db": self._threshold_db,
            "since": self._since,
            "channels": [
                {
                    "frequency_hz": c.frequency_hz,
                    "source": c.source,
                    "signal_id": c.signal_id,
                    "snr_db": c.snr_db,
                    "events": c.events,
                    "since": c.since,
                    "last_event_at": c.last_event_at,
                    "state": c.state,
                    "error": c.error,
                    "decoders": list(c.decoders),
                    # per-decoder session facts ("decoders" stays the
                    # plain id list existing callers match against).
                    "decoder_status": [self._decoder_status(c, d) for d in c.decoders],
                }
                for c in self._channels.values()
            ],
            "candidates": [
                {"frequency_hz": p.frequency_hz, "snr_db": p.snr_db} for p in candidates[:20]
            ],
            "iq": {
                "connected": (
                    self._last_frame_at is not None
                    and (self._clock() - self._last_frame_at) < _IQ_CONNECTED_TIMEOUT_S
                ),
                "frames": self._frames,
                "dropped": self._dropped,
                "dropped_frame_gaps": self.dropped_frame_gaps,
                "cpu_load": self._cpu_load,
            },
            "restarts": self.restarts,
            "backend_dropped_frames": sum(
                getattr(b, "dropped_frames", 0)
                for c in self._channels.values()
                for b in c.backends.values()
            ),
        }

    @staticmethod
    def _decoder_status(channel: BandChannel, decoder_id: str) -> dict:
        backend = channel.backends.get(decoder_id)
        error = getattr(backend, "error", None) if backend is not None else None
        dsd_state = getattr(backend, "dsd_state", None)
        return decoder_facts(
            decoder_id,
            state="error" if error else channel.state,
            error=error or channel.error,
            events=channel.events_by_decoder.get(decoder_id, 0),
            valid_frames=channel.valid_by_decoder.get(decoder_id, 0),
            last_event_ts=channel.last_event_ts_by_decoder.get(decoder_id),
            synchronized=getattr(dsd_state, "synchronized", None),
        )

    # -- channel management -------------------------------------------------

    async def _add_memory_channel(
        self, frequency_hz: float, signal_id: str | None, mem_decoders: list[str]
    ) -> None:
        k, residual = self._channelizer.channel_for(frequency_hz, self._center_hz)
        decoder_ids = (
            [d for d in mem_decoders if d in self._decoders]
            if mem_decoders
            else list(self._decoders)
        )
        if not decoder_ids:
            return  # a saved channel for another protocol must not use a band slot
        now = self._clock()
        channel = BandChannel(
            frequency_hz=frequency_hz,
            source="memory",
            signal_id=signal_id,
            k=k,
            residual_hz=residual,
            snr_db=0.0,
            since=now,
            last_seen_at=now,
        )
        await self._start_channel(channel, decoder_ids)
        self._channels[k] = channel

    async def _start_channel(self, channel: BandChannel, decoder_ids: list[str]) -> None:
        rates_needed = {
            DECODERS[decoder_id].input_rate for decoder_id in decoder_ids if decoder_id in DECODERS
        }
        in_rate = 48_000 if self._fixed_extractor is not None else self._channelizer.out_rate
        residual_hz = 0.0 if self._fixed_extractor is not None else channel.residual_hz
        if self._mode == "AM":
            channel.nfm_by_rate = {
                rate: AMChannel(
                    in_rate,
                    residual_hz,
                    target_rate=rate,
                    bandwidth_hz=self._am_bandwidth_hz,
                    squelch_db=self._am_squelch_db,
                )
                for rate in rates_needed
            }
        else:
            channel.nfm_by_rate = {
                rate: NFMChannel(in_rate, residual_hz, target_rate=rate) for rate in rates_needed
            }
        for decoder_id in decoder_ids:
            spec = DECODERS.get(decoder_id)
            if spec is None:
                continue
            try:
                backend = build_backend(
                    spec, self._binaries, calls_dir=self._calls_dir, fax_dir=self._fax_dir
                )
                await backend.start()
            except DecoderError as exc:
                if channel.error is None:
                    channel.error = str(exc)
                continue
            channel.backends[decoder_id] = backend
            channel.rate_by_decoder[decoder_id] = spec.input_rate
            channel.decoders.append(decoder_id)
            channel.event_tasks.append(asyncio.create_task(self._drain_events(channel, backend)))
        channel.state = "running"

    async def _stop_channel(self, channel: BandChannel) -> None:
        for task in channel.event_tasks:
            task.cancel()
        for task in channel.event_tasks:
            await reap_cancelled(task)
        channel.event_tasks = []
        for backend in channel.backends.values():
            await backend.stop()
        channel.backends = {}

    async def _drain_events(self, channel: BandChannel, backend: ProcessBackend) -> None:
        try:
            async for event in backend.events():
                extra = {}
                if self._track_generation:
                    # Upper bound: the latest block fed before the backend
                    # produced this event (external decoders add latency).
                    gen, idx, run_index = channel.last_input
                    extra = {
                        "generation": gen,
                        "sample_index": idx,
                        "run_index": run_index,
                    }
                event = replace(
                    event,
                    frequency_hz=int(channel.frequency_hz),
                    signal_id=channel.signal_id,
                    fields={**event.fields, "source": self._source_tag},
                    **extra,
                )
                now = self._clock()
                channel.events += 1
                channel.last_event_at = now
                decoder_id = getattr(backend, "decoder_id", "") or event.decoder
                channel.events_by_decoder[decoder_id] = (
                    channel.events_by_decoder.get(decoder_id, 0) + 1
                )
                if event.integrity == "passed":
                    channel.valid_by_decoder[decoder_id] = (
                        channel.valid_by_decoder.get(decoder_id, 0) + 1
                    )
                channel.last_event_ts_by_decoder[decoder_id] = now
                self._on_event(event)
        except asyncio.CancelledError:
            raise

    # -- IQ pump --------------------------------------------------------

    async def _read_frames(self) -> None:
        assert self._stream is not None
        try:
            async for frame in self._stream.frames():
                self._last_frame_at = self._clock()
                if (
                    not self._track_generation
                    and getattr(frame, "received_at", time.time()) < self._accept_after_wall
                ):
                    self._dropped += 1
                    continue
                if self._queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._queue.get_nowait()
                    self._dropped += 1
                    self._pending_frame_gap = True
                queued = (
                    frame
                    if self._track_generation
                    else _QueuedIQFrame(frame, self._span_generation)
                )
                self._queue.put_nowait(queued)
        except asyncio.CancelledError:
            raise

    async def _pump(self) -> None:
        try:
            while True:
                await self._pump_queued_frame(await self._queue.get())
        except asyncio.CancelledError:
            raise

    async def _pump_queued_frame(self, queued) -> None:
        if isinstance(queued, _QueuedIQFrame):
            frame, span_generation = queued.frame, queued.span_generation
        else:
            # Replay keeps its source generation; direct pump callers can
            # also supply an unwrapped frame.
            frame, span_generation = queued, self._span_generation
        async with self._lock:
            if not self._track_generation and span_generation != self._span_generation:
                self.stale_frames += 1
                self._dropped += 1
                return
            changed = await self._pump_frame(frame)
        if changed:
            self._on_status()

    async def _pump_frame(self, frame) -> bool:
        gen = getattr(frame, "generation", None)
        changed = False
        if self._pending_frame_gap:
            self._pending_frame_gap = False
            await self._on_dropped_frame_gap()
            changed = True
        if self._track_generation and gen is not None:
            if self._generation is None:
                self._generation = gen
            elif gen < self._generation:
                self.stale_frames += 1
                return False
            elif gen > self._generation:
                self._generation = gen
                self._next_sample_index = None
                await self._on_new_generation()
                changed = True
            idx = getattr(frame, "sample_index", None)
            if idx is not None:
                if self._next_sample_index is not None and idx != self._next_sample_index:
                    await self._on_dropped_frame_gap()
                    changed = True
                self._next_sample_index = idx
        self._frames += 1
        iq = _frame_to_iq(frame.data)

        t0 = self._perf()
        peaks, channel_pcm = await asyncio.to_thread(self._process_block, iq)
        dt = self._perf() - t0
        nominal = iq.size / self._sample_rate_hz if self._sample_rate_hz else dt
        load = dt / nominal if nominal > 0 else 0.0
        self._cpu_load = 0.3 * load + 0.7 * self._cpu_load

        self._candidates = peaks
        changed = await self._reconcile_channels(peaks) or changed
        sample_index = getattr(frame, "sample_index", None)
        run_index = getattr(frame, "run_index", 0)
        provenance = (gen, sample_index, run_index)
        for k, pcm_by_rate in channel_pcm.items():
            channel = self._channels.get(k)
            if channel is None:
                continue
            channel.last_input = provenance
            for decoder_id, backend in channel.backends.items():
                rate = channel.rate_by_decoder.get(decoder_id)
                pcm = pcm_by_rate.get(rate)
                if pcm:
                    backend.feed(pcm)
        if self._track_generation and sample_index is not None:
            self._next_sample_index = sample_index + iq.size
        return await self._retire_idle() or changed

    def _process_block(self, iq: np.ndarray) -> tuple[list[Peak], dict[int, dict[int, bytes]]]:
        """Runs off the event loop (asyncio.to_thread): pure numpy, no asyncio calls."""
        if self._fixed_extractor is not None:
            extracted = self._fixed_extractor.process(iq)
            return [], {
                k: {rate: nfm.process(extracted) for rate, nfm in channel.nfm_by_rate.items()}
                for k, channel in self._channels.items()
            }
        peaks = self._peaks.update(iq) if self._peaks is not None else []
        channelized = self._channelizer.process(iq)
        channel_pcm: dict[int, dict[int, bytes]] = {}
        for k, channel in self._channels.items():
            if not channel.nfm_by_rate:
                continue
            channel_pcm[k] = {
                rate: nfm.process(channelized[:, k]) for rate, nfm in channel.nfm_by_rate.items()
            }
        return peaks, channel_pcm

    async def _reconcile_channels(self, peaks: list[Peak]) -> bool:
        changed = False
        now = self._clock()
        for peak in peaks:
            k, residual = self._channelizer.channel_for(peak.frequency_hz, self._center_hz)
            existing = self._channels.get(k)
            if existing is not None:
                existing.snr_db = peak.snr_db
                existing.frequency_hz = peak.frequency_hz
                existing.last_seen_at = now
                continue

            memory_count = sum(1 for c in self._channels.values() if c.source == "memory")
            detected = [c for c in self._channels.values() if c.source == "detected"]
            if memory_count + len(detected) < self._max_channels:
                await self._add_detected_channel(k, residual, peak, now)
                changed = True
            elif detected:
                weakest = min(detected, key=lambda c: c.snr_db)
                if peak.snr_db >= weakest.snr_db + _REPLACE_MARGIN_DB:
                    await self._stop_channel(weakest)
                    del self._channels[weakest.k]
                    await self._add_detected_channel(k, residual, peak, now)
                    changed = True
        return changed

    async def _add_detected_channel(self, k: int, residual: float, peak: Peak, now: float) -> None:
        channel = BandChannel(
            frequency_hz=peak.frequency_hz,
            source="detected",
            signal_id=None,
            k=k,
            residual_hz=residual,
            snr_db=peak.snr_db,
            since=now,
            last_seen_at=now,
        )
        await self._start_channel(channel, list(self._decoders))
        self._channels[k] = channel

    async def _retire_idle(self) -> bool:
        now = self._clock()
        changed = False
        for k, channel in list(self._channels.items()):
            if channel.source != "detected":
                continue
            last_event = (
                channel.last_event_at if channel.last_event_at is not None else channel.since
            )
            if now - channel.last_seen_at >= self._idle_s and now - last_event >= self._idle_s:
                await self._stop_channel(channel)
                del self._channels[k]
                changed = True
        return changed
