"""Replay decode paths: the live decoders run on a replayed recording.

Two modes, bound to one ``IQSource`` (a ReplaySource) and the
``VirtualReceiver`` listening to it:

- **single**: one backend per decoder, fed from the receiver -- the flat NFM
  discriminator (``flat_tap``) for NFM-family decoders, the pre-volume,
  pre-squelch demod output (``clean_tap``) for AM/SSB/CW ones -- resampled
  48 kHz -> ``spec.input_rate``. A decoder whose demodulator doesn't match
  the receiver's is reported ``unsupported_input`` and never started. Raw-IQ
  decoders instead subscribe to the recording IQ stream, independently of
  audio demodulation, with rate/span checks and discontinuity resets.
- **band**: a ``BandDecoder`` on the source's own IQ stream (peak detection
  across the recorded span), ``track_generation=True``.

Seeks/loops/run changes bump the source generation. Both modes restart
their backends before the first block of a new generation is fed (single:
in the tap pump; band: in BandDecoder's pump) and drop older blocks, so no
decoder state or buffered symbols cross a seek. Every event is stamped with
``source_id``, ``generation``, ``sample_index`` (the start of the latest input
block fed before emission, not an exact message onset; external decoders add latency)
and this run's ``analysis_run_id`` so the service can file it as review
analysis rather than live reception.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, build_backend, reap_cancelled
from msdr.decoder.band import BandDecoder
from msdr.decoder.iq_backend import build_iq_backend
from msdr.decoder.pcm import Normalizer, Resampler, to_s16
from msdr.decoder.readiness import decoder_facts, merge_band_facts
from msdr.decoder.registry import DECODERS, DecoderSpec, effective_binaries
from msdr.ids import uuid7

if TYPE_CHECKING:
    from msdr.source.base import IQSource, SourceEvent
    from msdr.source.virtual_receiver import VirtualReceiver

logger = logging.getLogger(__name__)

_TAP_RATE = 48_000
# Same "no audio" rule as manager.py's audio path, measured on the tap.
_SILENCE_RMS = 1e-4
_SILENCE_S = 3.0

Tap = Literal["flat", "clean", "iq"]


@dataclass
class _Single:
    spec: DecoderSpec
    tap: Tap | None
    state: str = "running"
    error: str | None = None
    unsupported: str | None = None
    backend: object | None = None
    resampler: Resampler | None = None
    normalizer: Normalizer | None = None
    drain_task: asyncio.Task | None = None
    events: int = 0
    valid_frames: int = 0
    last_event_ts: float | None = None
    last_input: tuple[int | None, int | None] = (None, None)
    # (source generation, channel generation) this backend instance was
    # started for -- fixed for its whole life, unlike last_input which is
    # overwritten by every fed block. _drain compares it to the *current*
    # generation at emission time so a result queued from a backend that's
    # since been superseded (seek/retune pending a restart) is dropped
    # instead of escaping stamped with stale provenance.
    start_generation: tuple[int | None, int | None] = (None, None)
    start_run_index: int | None = None
    start_source_id: str | None = None


class ReplayDecodeSession:
    def __init__(
        self,
        receiver: "VirtualReceiver",
        source: "IQSource",
        *,
        binaries: dict[str, str | None],
        settings,
        on_event: Callable[[DecodeEvent], None],
        on_status: Callable[[dict], None],
        calls_dir: Path | str | None = None,
        fax_dir: Path | str | None = None,
    ) -> None:
        self._receiver = receiver
        self._source = source
        self._binaries = binaries
        self._settings = settings
        self._on_event = on_event
        self._on_status = on_status
        self._calls_dir = Path(calls_dir) if calls_dir is not None else Path("decoder/calls")
        self._fax_dir = Path(fax_dir) if fax_dir is not None else Path("decoder/fax")

        self.mode: Literal["single", "band"] | None = None
        self.analysis_run_id: str | None = None
        self._since: float | None = None
        self._warnings: list[str] = []
        self._last_stop_reason: str | None = None
        self._events_total = 0
        self.restarts = 0

        self._singles: dict[str, _Single] = {}
        self._pump_tasks: list[asyncio.Task] = []
        # Per tap: generation / receiver channel_generation last fed, and
        # the silence tracker.
        self._tap_generation: dict[str, int] = {}
        self._tap_channel_gen: dict[str, int] = {}
        self._quiet_samples: dict[str, int] = {}
        self._tap_dropped_count: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._iq_next_sample = None
        self._iq_span = None
        self._iq_dropped = 0

        self._band: BandDecoder | None = None
        self._band_decoders: list[str] = []

    @property
    def source_id(self) -> str:
        return getattr(self._source, "source_id", None) or self._source.descriptor().source_id

    @property
    def active(self) -> bool:
        return self.mode is not None

    # -- control -----------------------------------------------------------------

    async def start_single(self, decoder_ids: list[str]) -> dict:
        async with self._control_lock:
            try:
                return await self._start_single(decoder_ids)
            except BaseException:
                self.mode = "single"
                await self._stop("startup failed")
                raise

    async def _start_single(self, decoder_ids: list[str]) -> dict:
        if self.active:
            await self._stop("restarted")
        self._begin()
        rx = self._receiver.status()
        demod = rx.get("demodulator")
        for decoder_id in decoder_ids:
            spec = self._spec_or_warn(decoder_id)
            if spec is None:
                continue
            tap, reason = _tap_for(spec, demod)
            single = _Single(spec=spec, tap=tap)
            if tap is None:
                single.state = "unsupported_input"
                single.unsupported = reason
                self._warnings.append(reason)
            else:
                await self._start_backend(
                    single,
                    rx.get("generation"),
                    rx.get("channel_generation"),
                    getattr(self._source, "run_index", 0),
                )
            self._singles[decoder_id] = single
        if not self._singles:
            self._end()
            raise DecoderError("no requested decoders are available")
        self.mode = "single"
        for tap in sorted(
            {s.tap for s in self._singles.values() if s.backend is not None or s.tap == "iq"}
        ):
            self._pump_tasks.append(asyncio.create_task(self._pump_tap(tap)))
        self._publish()
        return self.status()

    async def start_band(self, decoder_ids, max_channels=None, threshold_db=None) -> dict:
        async with self._control_lock:
            return await self._start_band(decoder_ids, max_channels, threshold_db)

    async def _start_band(
        self,
        decoder_ids: list[str],
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        invalid = [
            d for d in decoder_ids if d not in DECODERS or "NFM" not in DECODERS[d].demodulators
        ]
        if invalid:
            raise DecoderError(f"not NFM-family decoders: {', '.join(invalid)}")
        if self.active:
            await self._stop("restarted")
        self._begin()
        decoders = [d for d in decoder_ids if self._spec_or_warn(d) is not None]
        if not decoders:
            self._end()
            raise DecoderError("no requested decoders are available")
        desc = self._source.descriptor()
        settings = self._settings
        band = BandDecoder(
            iq_stream_factory=self._source.iq_stream,
            decoders=decoders,
            binaries=self._binaries,
            center_hz=desc.center_hz,
            sample_rate_hz=desc.sample_rate_hz,
            memories=[],
            max_channels=(
                max_channels
                if max_channels is not None
                else getattr(settings, "band_max_channels", 8)
            ),
            threshold_db=(
                threshold_db
                if threshold_db is not None
                else getattr(settings, "band_threshold_db", 10.0)
            ),
            idle_s=getattr(settings, "band_idle_s", 60),
            spacing_hz=getattr(settings, "band_spacing_hz", 12500),
            on_event=self._on_band_event,
            on_status=self._publish,
            source_tag="replay-band",
            calls_dir=self._calls_dir,
            fax_dir=self._fax_dir,
            track_generation=True,
        )
        # Listener before start: a "run" arriving in between must not be missed.
        self._source.add_listener(self._on_source_event)
        try:
            await band.start()
        except BaseException:
            self._source.remove_listener(self._on_source_event)
            self._end()
            raise
        self._band = band
        self._band_decoders = decoders
        self.mode = "band"
        self._publish()
        return self.status()

    async def stop(self, reason: str = "user") -> dict:
        async with self._control_lock:
            return await self._stop(reason)

    async def _stop(self, reason: str = "user") -> dict:
        if not self.active:
            return self.status()
        self._last_stop_reason = reason
        self.mode = None  # invalidate output before the first cleanup await
        for task in self._pump_tasks:
            task.cancel()
        for task in self._pump_tasks:
            await reap_cancelled(task)
        self._pump_tasks = []
        failure = None
        for single in self._singles.values():
            try:
                await self._stop_backend(single)
            except BaseException as exc:
                failure = failure or exc
        if self._band is not None:
            self._source.remove_listener(self._on_source_event)
            band, self._band = self._band, None
            try:
                await band.stop()
            except BaseException as exc:
                failure = failure or exc
        self._end()
        self._publish()
        if failure is not None:
            raise failure
        return self.status()

    # -- status --------------------------------------------------------------------

    def status(self) -> dict:
        band_status = self._band.status() if self._band is not None else None
        if self.mode == "band" and band_status is not None:
            channels = band_status["channels"]
            decoders = []
            for decoder_id in self._band_decoders:
                running = [c for c in channels if decoder_id in c["decoders"]]
                merged = merge_band_facts(decoder_id, running) if running else {}
                decoders.append(
                    decoder_facts(
                        decoder_id,
                        state=merged.get("state", "running"),
                        error=merged.get("error"),
                        events=merged.get("events", 0),
                        valid_frames=merged.get("valid_frames", 0),
                        last_event_ts=max(
                            (
                                d["last_event_ts"]
                                for c in running
                                for d in c.get("decoder_status", [])
                                if d["id"] == decoder_id and d["last_event_ts"] is not None
                            ),
                            default=None,
                        ),
                        synchronized=merged.get("synchronized"),
                    )
                )
            dropped = band_status["iq"]["dropped"] + band_status.get("backend_dropped_frames", 0)
            restarts = band_status.get("restarts", 0)
        else:
            channels = []
            decoders = [self._single_facts(s) for s in self._singles.values()]
            dropped = sum(getattr(s.backend, "dropped_frames", 0) for s in self._singles.values())
            dropped += self._iq_dropped
            dropped += getattr(self._receiver, "dropped_tap_blocks", 0) if self.active else 0
            restarts = self.restarts
        return {
            "active": self.active,
            "mode": self.mode,
            "source_id": self.source_id,
            "analysis_run_id": self.analysis_run_id,
            "since": self._since,
            "decoders": decoders,
            "channels": channels,
            "warnings": list(self._warnings),
            "last_stop_reason": self._last_stop_reason,
            "events_total": self._events_total,
            "dropped_frames": dropped,
            "restarts": restarts,
            "band": band_status,
        }

    def _single_facts(self, single: _Single) -> dict:
        error = single.error or getattr(single.backend, "error", None)
        state = "error" if error else single.state
        dsd_state = getattr(single.backend, "dsd_state", None)
        quiet = (
            single.tap is not None
            and self._quiet_samples.get(single.tap, 0) >= _SILENCE_S * _TAP_RATE
        )
        return decoder_facts(
            single.spec.id,
            state=state,
            error=error,
            events=single.events,
            valid_frames=single.valid_frames,
            last_event_ts=single.last_event_ts,
            synchronized=getattr(dsd_state, "synchronized", None),
            quiet=quiet,
            unsupported=single.unsupported,
        )

    # -- internals: lifecycle ----------------------------------------------------------

    def _begin(self) -> None:
        self.analysis_run_id = uuid7()
        self._since = time.time()
        self._warnings = []
        self._events_total = 0
        self.restarts = 0
        self._singles = {}
        self._tap_generation = {}
        self._tap_channel_gen = {}
        self._quiet_samples = {}
        self._tap_dropped_count = {}
        self._iq_next_sample = None
        self._iq_span = None
        self._iq_dropped = 0

    def _end(self) -> None:
        self.mode = None
        self._since = None
        self._band_decoders = []

    def _spec_or_warn(self, decoder_id: str) -> DecoderSpec | None:
        spec = DECODERS.get(decoder_id)
        if spec is None:
            self._warnings.append(f"unknown decoder: {decoder_id}")
            return None
        missing = [n for n in effective_binaries(spec, self._binaries) if not self._binaries.get(n)]
        if missing:
            self._warnings.append(f"{decoder_id}: missing {', '.join(missing)}")
            return None
        return spec

    def _publish(self) -> None:
        try:
            self._on_status(self.status())
        except Exception:  # noqa: BLE001 -- a broken publisher mustn't stop decoding
            logger.exception("replay decode status publish failed")

    # -- internals: single-mode backends -------------------------------------------------

    async def _start_backend(
        self,
        single: _Single,
        generation: int | None = None,
        channel_generation: int | None = None,
        run_index: int | None = None,
    ) -> None:
        spec = single.spec
        backend = None
        try:
            if single.tap == "iq":
                desc = self._source.descriptor()
                generation = desc.generation
                if spec.id == "ft8" and desc.capture_time is None:
                    raise DecoderError("FT8 replay requires the recording's capture UTC timestamp")
                backend = build_iq_backend(
                    spec,
                    self._binaries,
                    sample_rate_hz=desc.sample_rate_hz,
                    center_hz=desc.center_hz,
                    frequency_hz=self._receiver.status().get("frequency_hz"),
                    decoder_settings=self._settings,
                )
            else:
                backend = build_backend(
                    spec, self._binaries, calls_dir=self._calls_dir, fax_dir=self._fax_dir
                )
            await backend.start()
        except BaseException as exc:
            if backend is not None:
                await backend.stop()
            if not isinstance(exc, DecoderError):
                raise
            single.state, single.error, single.backend = "error", str(exc), None
            self._warnings.append(str(exc))
            return
        single.backend = backend
        single.state, single.error = "running", None
        single.resampler = (
            Resampler(_TAP_RATE, spec.input_rate)
            if single.tap != "iq" and spec.input_rate != _TAP_RATE
            else None
        )
        # Flat discriminator: always level-normalised, as band.py's
        # NFMChannel does; demod audio: per spec, as channel.py does.
        normalize = single.tap != "iq" and (
            single.tap == "flat" or getattr(spec, "normalize", True)
        )
        single.normalizer = Normalizer(rate=spec.input_rate) if normalize else None
        single.last_input = (None, None)
        single.start_generation = (generation, channel_generation)
        single.start_run_index = run_index
        single.start_source_id = self.source_id
        single.drain_task = asyncio.create_task(self._drain(single, backend))

    async def _stop_backend(self, single: _Single) -> None:
        if single.drain_task is not None:
            single.drain_task.cancel()
            await reap_cancelled(single.drain_task)
            single.drain_task = None
        if single.backend is not None:
            backend, single.backend = single.backend, None
            await backend.stop()

    async def _restart_tap(
        self, tap: Tap, generation: int, channel_generation: int, run_index: int
    ) -> None:
        """Lock held: fresh backend + resampler state for every decoder on
        this tap; re-checks demodulator compatibility (a retuned receiver
        may no longer suit a decoder)."""
        demod = self._receiver.status().get("demodulator")
        for single in self._singles.values():
            if single.tap != tap:
                continue
            await self._stop_backend(single)
            new_tap, reason = _tap_for(single.spec, demod)
            if new_tap != tap:
                single.state, single.unsupported = "unsupported_input", reason
                continue
            await self._start_backend(single, generation, channel_generation, run_index)
        self.restarts += 1

    async def _drain(self, single: _Single, backend) -> None:
        try:
            async for event in backend.events():
                rx = self._receiver.status()
                if not self.active or single.backend is not backend:
                    continue
                if single.start_source_id != self.source_id or single.start_run_index != getattr(
                    self._source, "run_index", 0
                ):
                    continue
                if single.tap == "iq":
                    rx = {**rx, "generation": self._source.descriptor().generation}
                    if single.last_input[0] is None:
                        continue
                if single.start_generation != (rx.get("generation"), rx.get("channel_generation")):
                    # Stale: the source/channel moved on since this backend
                    # was started (seek/retune pending its restart) -- drop
                    # rather than emit with provenance that no longer holds.
                    continue
                gen, idx = single.last_input
                event = replace(
                    event,
                    frequency_hz=rx.get("frequency_hz"),
                    fields={
                        **event.fields,
                        "source": "replay-iq" if single.tap == "iq" else "replay-single",
                    },
                    source_id=self.source_id,
                    generation=gen,
                    sample_index=idx,
                    analysis_run_id=self.analysis_run_id,
                    run_index=single.start_run_index,
                )
                single.events += 1
                if event.integrity == "passed":
                    single.valid_frames += 1
                single.last_event_ts = time.time()
                self._emit(event)
        except asyncio.CancelledError:
            raise

    # -- internals: single-mode pump -----------------------------------------------------

    async def _pump_iq(self) -> None:
        stream = self._source.iq_stream()
        try:
            await stream.start()
            await stream.enable(True)
            async for frame in stream.frames():
                async with self._lock:
                    changed = await self._feed_iq(frame, getattr(stream, "dropped", 0))
                if changed:
                    self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("replay raw-IQ pump failed")
            for single in self._singles.values():
                if single.tap == "iq":
                    await self._stop_backend(single)
                    single.state, single.error = "error", str(exc)
            self._publish()
        finally:
            await stream.stop()

    def _iq_context(self):
        desc = self._source.descriptor()
        return (
            self.source_id,
            desc.generation,
            getattr(self._source, "run_index", 0),
            self._receiver.status().get("channel_generation"),
            desc.center_hz,
            desc.sample_rate_hz,
            self._receiver.status().get("frequency_hz"),
        )

    async def _feed_iq(self, frame, dropped: int = 0) -> bool:
        context = self._iq_context()
        _, generation, run_index, channel_gen, *_ = context
        if frame.generation != generation or frame.run_index != run_index:
            return False
        if frame.sample_index is None or frame.sample_index < 0 or len(frame.data) % 4:
            raise DecoderError("Replay IQ block lacks a valid sample clock or complete IQ pairs")
        if not frame.data:
            return False
        same_span = context == self._iq_span
        if (
            same_span
            and self._iq_next_sample is not None
            and frame.sample_index < self._iq_next_sample
        ):
            return False  # duplicate/out-of-order blocks must never be concatenated
        gap = (
            same_span
            and self._iq_next_sample is not None
            and frame.sample_index != self._iq_next_sample
        )
        first_superseded = self._iq_span is None and any(
            s.tap == "iq"
            and (
                s.start_generation != (generation, channel_gen)
                or s.start_run_index != run_index
                or s.start_source_id != self.source_id
            )
            for s in self._singles.values()
        )
        restart = (
            first_superseded
            or gap
            or dropped != self._iq_dropped
            or (self._iq_span is not None and not same_span)
        )
        if restart:
            await self._restart_tap("iq", generation, channel_gen, run_index)
            if self._iq_context() != context:
                return True  # a seek/retune raced the asynchronous backend startup
        self._iq_span = context
        self._iq_dropped = dropped
        self._iq_next_sample = frame.sample_index + len(frame.data) // 4
        changed = restart
        for single in self._singles.values():
            if single.tap != "iq" or single.backend is None:
                continue
            single.last_input = (generation, frame.sample_index)
            desc = self._source.descriptor()
            if hasattr(single.backend, "feed_at") and desc.capture_time is not None:
                epoch = desc.capture_time.timestamp() + frame.sample_index / desc.sample_rate_hz
                single.backend.feed_at(frame.data, epoch)
            else:
                single.backend.feed(frame.data)
            if getattr(single.backend, "error", None):
                single.error = single.backend.error
                single.state = "error"
                changed = True
        return changed

    async def _pump_tap(self, tap: Tap) -> None:
        if tap == "iq":
            await self._pump_iq()
            return
        stream = self._receiver.flat_tap() if tap == "flat" else self._receiver.clean_tap()
        try:
            async for pcm, gen, idx in stream:
                async with self._lock:
                    changed = await self._feed_block(tap, pcm, gen, idx)
                if changed:
                    self._publish()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("replay decode %s pump crashed", tap)
        finally:
            await stream.aclose()

    async def _feed_block(self, tap: Tap, pcm: bytes, gen: int, idx: int) -> bool:
        changed = False
        channel_gen = getattr(self._receiver, "channel_generation", 0)
        dropped_now = getattr(self._receiver, "dropped_tap_blocks", 0)
        dropped_before = self._tap_dropped_count.get(tap, dropped_now)
        tap_gap = dropped_now != dropped_before
        last_gen = self._tap_generation.get(tap)
        if last_gen is not None and gen < last_gen:
            return False  # stale: the receiver already moved on
        run_index = getattr(self._source, "run_index", 0)
        first_block_superseded = last_gen is None and any(
            single.tap == tap
            and single.backend is not None
            and (
                single.start_generation != (gen, channel_gen) or single.start_run_index != run_index
            )
            for single in self._singles.values()
        )
        if first_block_superseded or (
            last_gen is not None
            and (gen != last_gen or channel_gen != self._tap_channel_gen.get(tap) or tap_gap)
        ):
            await self._restart_tap(tap, gen, channel_gen, run_index)
            self._quiet_samples[tap] = 0
            changed = True
        self._tap_generation[tap] = gen
        self._tap_channel_gen[tap] = channel_gen
        self._tap_dropped_count[tap] = dropped_now

        singles = [s for s in self._singles.values() if s.tap == tap and s.backend is not None]
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        was_quiet = self._quiet_samples.get(tap, 0) >= _SILENCE_S * _TAP_RATE
        if samples.size and float(np.sqrt(np.mean(np.square(samples)))) < _SILENCE_RMS:
            self._quiet_samples[tap] = self._quiet_samples.get(tap, 0) + samples.size
        else:
            self._quiet_samples[tap] = 0
        now_quiet = self._quiet_samples[tap] >= _SILENCE_S * _TAP_RATE
        changed = changed or was_quiet != now_quiet
        if not singles:
            return changed
        # Resample + normalise off the loop (Normalizer is a per-sample loop).
        outputs = await asyncio.to_thread(_condition_all, singles, samples)
        for single, out in zip(singles, outputs, strict=True):
            single.last_input = (gen, idx)
            if out and single.backend is not None:
                single.backend.feed(out)
            if getattr(single.backend, "error", None) and single.state != "error":
                single.state = "error"
                changed = True
        return changed

    # -- internals: band mode ----------------------------------------------------------

    def _on_source_event(self, event: "SourceEvent") -> None:
        # Sync, on the loop thread: only record the new span; the band pump
        # applies it before the first frame of the new generation.
        if event.kind == "run" and self._band is not None:
            desc = event.descriptor
            self._band.expect_span(desc.center_hz, desc.sample_rate_hz)

    def _on_band_event(self, event: DecodeEvent) -> None:
        if (
            event.generation is not None
            and event.generation != self._source.descriptor().generation
        ):
            return
        if event.run_index is None:
            event = replace(event, run_index=getattr(self._source, "run_index", 0))
        self._emit(replace(event, source_id=self.source_id, analysis_run_id=self.analysis_run_id))

    def _emit(self, event: DecodeEvent) -> None:
        self._events_total += 1
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 -- a failing store/publish mustn't stop decoding
            logger.exception("replay decode event handler failed")


def _tap_for(spec: DecoderSpec, demodulator: str | None) -> tuple[Tap | None, str]:
    """Which receiver tap feeds `spec`, or (None, reason) when the virtual
    receiver's demodulator can't serve it."""
    if spec.iq_only and spec.demodulators == ("IQ",):
        return "iq", ""
    if demodulator is None:
        return None, f"{spec.id}: virtual receiver is not tuned"
    if "NFM" in spec.demodulators:
        if demodulator == "NFM":
            return "flat", ""
        return None, f"{spec.id}: needs the NFM discriminator; virtual receiver is {demodulator}"
    if demodulator in spec.demodulators:
        return "clean", ""
    return None, (
        f"{spec.id}: needs {'/'.join(spec.demodulators)}; virtual receiver is {demodulator}"
    )


def _condition_all(singles: list[_Single], samples: np.ndarray) -> list[bytes]:
    """Runs in a worker thread: pure numpy on per-decoder state that only
    this tap's pump (holding the session lock) touches."""
    out = []
    for single in singles:
        x = single.resampler.process(samples) if single.resampler is not None else samples
        if single.normalizer is not None:
            x = single.normalizer.process(x)
        out.append(to_s16(x) if x.size else b"")
    return out
