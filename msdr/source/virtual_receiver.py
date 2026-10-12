"""Virtual receiver: demodulates a channel out of any ``IQSource``.

It never talks to the radio: tuning is bounded by the source's recorded
span, and everything it produces is tagged with the source generation so a
seek, loop or new run can never leak audio from before the break.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import numpy as np

from msdr.contracts.source import ChannelDescriptor, SourceDescriptor
from msdr.source.audio_dsp import NativeAudioEffects
from msdr.source.dsp import (
    DEMODULATORS,
    VOICE_RATE_HZ,
    AmDemod,
    AudioBlock,
    ChannelExtractor,
    CwDemod,
    DemodChain,
    NfmDemod,
    SsbDemod,
    WfmDemod,
    channel_filter,
    filter_margin_hz,
    iq16_to_complex,
    to_s16,
)
from msdr.source.inspection import CarrierDriftTracker, ReceiverInspection

if TYPE_CHECKING:
    from msdr.source.base import IQSource, SourceEvent

logger = logging.getLogger(__name__)

_CPU_EMA_ALPHA = 0.1


class OutOfSpanError(ValueError):
    """The requested channel (plus filter margin) is outside the source span.
    ``reason`` is user-facing text (the API returns it with a 409)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _span_reason(ch: ChannelDescriptor, desc: SourceDescriptor, margin: int) -> str:
    lo, hi = desc.span_hz
    half = ch.bandwidth_hz // 2 + margin
    return (
        f"{ch.demodulator} {ch.frequency_hz / 1e6:.6f} MHz (±{half / 1e3:g} kHz incl. filter "
        f"margin) is outside the recorded span {lo / 1e6:.6f}–{hi / 1e6:.6f} MHz"
    )


def _put(q: asyncio.Queue, item) -> bool:
    """Bounded: a slow listener loses the oldest audio, never the newest."""
    dropped = q.full()
    if dropped:
        q.get_nowait()
    q.put_nowait(item)
    return dropped


class VirtualReceiver:
    def __init__(
        self, source: "IQSource", *, audio_rate: int = VOICE_RATE_HZ, queue_max: int = 32
    ) -> None:
        if audio_rate != VOICE_RATE_HZ:
            raise ValueError(f"demodulators run at {VOICE_RATE_HZ} Hz only")
        self._source = source
        self.audio_rate = audio_rate
        self._queue: asyncio.Queue[AudioBlock] = asyncio.Queue(maxsize=queue_max)
        self._flat_queue: asyncio.Queue[tuple[bytes, int, int]] = asyncio.Queue(maxsize=queue_max)
        self._flat_consumers = 0
        self._clean_queue: asyncio.Queue[tuple[bytes, int, int]] = asyncio.Queue(maxsize=queue_max)
        self._clean_consumers = 0
        self._channel: ChannelDescriptor | None = None
        self._chain: DemodChain | None = None
        self._decoder_demod = None
        self._effects = NativeAudioEffects(audio_rate)
        self._native_audio = {
            "agc_mode": "default",
            "manual_gain": 1.0,
            "ssb_low_hz": 0,
            "ssb_high_hz": None,
            "cw_pitch_hz": 700,
            "notch_enabled": False,
            "notch_frequency_hz": 1000,
            "notch_width_hz": 100,
            "wfm_deemphasis_us": 75,
            "nfm_audio_mode": "voice",
            "nr_enabled": False,
            "nr_strength": 0.5,
            "blanker_enabled": False,
            "blanker_threshold": 8.0,
        }
        self.inspection = ReceiverInspection()
        self._inspection_extractor: ChannelExtractor | None = None
        self._inspection_rate = 0
        self._inspection_index: int | None = None
        self._audio_inspection_index: int | None = None
        self._drift = CarrierDriftTracker()
        self._drift_track = False
        self._drift_correct = False
        self._drift_signal_kind = "unconfigured"
        self._fs_in = 0.0
        desc = source.descriptor()
        self._generation = desc.generation
        self._next_index = desc.sample_index
        self._pending_index: int | None = None
        self.channel_generation = 0
        self._reason: str | None = None
        self._unavailable = False
        self._squelch_db: float | None = None
        self._volume = 1.0
        self._muted = False
        self.dropped_blocks = 0
        # Decoder-tap blocks lost to a slow decode consumer (flat/clean taps).
        self.dropped_tap_blocks = 0
        self.stale_frames = 0
        # A frame the upstream IQ stream dropped for stalling (same
        # generation, sample_index jumped ahead of what was expected): the
        # DSP chain's filter/decimator state no longer applies, so it's
        # rebuilt same as a generation break -- visible here as the reason.
        self.dropped_frame_gaps = 0
        self._cpu_load = 0.0
        self._stream = None
        self._task: asyncio.Task | None = None
        self._error_logged = False

    # -- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._source.add_listener(self._on_event)
        # Adopt the source's current clock: frames already tagged with it are ours.
        desc = self._source.descriptor()
        self._generation = desc.generation
        self._next_index = desc.sample_index
        self._stream = self._source.iq_stream()
        await self._stream.start()
        await self._stream.enable(True)
        self._task = asyncio.create_task(self._run(self._stream))

    async def stop(self) -> None:
        self._source.remove_listener(self._on_event)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await self._stream.enable(False)
            await self._stream.stop()
            self._stream = None

    async def _run(self, stream) -> None:
        async for frame in stream.frames():
            try:
                self.process_frame(frame)
            except Exception:  # noqa: BLE001 -- one bad frame mustn't kill listening
                if not self._error_logged:
                    logger.exception("virtual receiver DSP failed")
                    self._error_logged = True
            # DSP runs inline on the loop; yield so a burst of frames can't
            # starve the websocket writers.
            await asyncio.sleep(0)

    # -- control ---------------------------------------------------------------

    def tune(self, frequency_hz: int, demodulator: str, bandwidth_hz: int) -> dict:
        mode = demodulator.upper()
        if mode not in DEMODULATORS:
            raise ValueError(f"unknown demodulator {demodulator!r}; use one of {DEMODULATORS}")
        desc = self._source.descriptor()
        ch = ChannelDescriptor(
            channel_id="virtual-rx",
            source_id=getattr(self._source, "source_id", desc.source_id),
            frequency_hz=int(frequency_hz),
            bandwidth_hz=int(bandwidth_hz),
            demodulator=mode,
            consumers=["listen", "decode"] if mode == "NFM" else ["listen"],
            generation=desc.generation,
        )
        margin = filter_margin_hz(mode, ch.bandwidth_hz)
        if mode in ("USB", "LSB"):
            high = self._native_audio["ssb_high_hz"] or ch.bandwidth_hz
            if high > ch.bandwidth_hz or self._native_audio["ssb_low_hz"] + 200 > high:
                raise ValueError("SSB passband does not fit the tuned bandwidth")
        if not ch.fits(desc, margin_hz=margin):
            # Replay tuning is bounded by what was recorded; the
            # current channel keeps playing.
            raise OutOfSpanError(_span_reason(ch, desc, margin))
        self._channel = ch
        self.channel_generation += 1
        self._flush()
        self._rebuild(desc)
        return self.status()

    def squelch(self, threshold_db: float | None) -> None:
        self._squelch_db = threshold_db
        self._apply_listening()

    def volume(self, v: float) -> None:
        self._volume = max(0.0, float(v))
        self._apply_listening()

    def mute(self, on: bool) -> None:
        self._muted = bool(on)
        self._apply_listening()

    def configure_audio(self, **kwargs) -> dict:
        """Configure the native listening path; rejects a whole invalid patch."""
        limits = {
            "manual_gain": (0.0, 20.0),
            "ssb_low_hz": (0, 3000),
            "ssb_high_hz": (300, 10_000),
            "cw_pitch_hz": (300, 1500),
            "notch_frequency_hz": (100, 5000),
            "notch_width_hz": (10, 500),
            "nr_strength": (0.0, 1.0),
            "blanker_threshold": (4.0, 20.0),
        }
        candidate = dict(self._native_audio)
        for key, value in kwargs.items():
            if key not in candidate:
                raise ValueError(f"unknown native audio setting: {key}")
            if key in limits:
                if value is None and key == "ssb_high_hz":
                    pass
                elif isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} must be numeric")
                elif not np.isfinite(value) or not limits[key][0] <= value <= limits[key][1]:
                    raise ValueError(f"{key} outside {limits[key]}")
            elif key in ("notch_enabled", "nr_enabled", "blanker_enabled"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
            elif key == "agc_mode" and value not in ("default", "off", "fast", "slow"):
                raise ValueError("agc_mode must be default, off, fast or slow")
            elif key == "wfm_deemphasis_us" and value not in (0, 50, 75):
                raise ValueError("wfm_deemphasis_us must be 0, 50 or 75")
            elif key == "nfm_audio_mode" and value not in ("voice", "flat"):
                raise ValueError("nfm_audio_mode must be voice or flat")
            candidate[key] = value
        high = candidate["ssb_high_hz"]
        if high is not None and high - candidate["ssb_low_hz"] < 200:
            raise ValueError("SSB passband must be at least 200 Hz wide")
        ch = self._channel
        if ch is not None and ch.demodulator in ("USB", "LSB"):
            effective_high = high or ch.bandwidth_hz
            if effective_high > ch.bandwidth_hz or candidate["ssb_low_hz"] + 200 > effective_high:
                raise ValueError("SSB passband exceeds tuned bandwidth; retune wider first")
        previous = self._native_audio
        self._native_audio = candidate
        if candidate != previous:
            self._apply_native_audio(previous)
        return dict(candidate)

    def _apply_native_audio(self, previous: dict | None = None) -> None:
        self._effects.configure(self._native_audio)
        if self._chain is None:
            return
        settings = self._native_audio
        demod = self._chain.demod
        changed = set(settings) if previous is None else {
            key for key in settings if settings[key] != previous[key]
        }
        if isinstance(demod, (AmDemod, SsbDemod)) and changed & {"agc_mode", "manual_gain"}:
            demod.agc_mode = settings["agc_mode"]
            demod.manual_gain = float(settings["manual_gain"])
            demod.reset()
        if isinstance(demod, SsbDemod) and not isinstance(demod, CwDemod):
            if changed & {"ssb_low_hz", "ssb_high_hz"}:
                demod.set_passband(settings["ssb_low_hz"], settings["ssb_high_hz"] or demod.bw_hz)
        if isinstance(demod, CwDemod) and "cw_pitch_hz" in changed:
            demod.set_pitch(settings["cw_pitch_hz"])
        if isinstance(demod, WfmDemod) and "wfm_deemphasis_us" in changed:
            demod.deemphasis_us = settings["wfm_deemphasis_us"]
            demod.reset()
        if isinstance(demod, NfmDemod) and "nfm_audio_mode" in changed:
            demod.audio_mode = settings["nfm_audio_mode"]

    def _apply_listening(self) -> None:
        # Listening path only (§2): the NFM flat tap is never scaled/gated.
        if self._chain is None:
            return
        demod = self._chain.demod
        demod.volume, demod.muted = self._volume, self._muted
        if isinstance(demod, NfmDemod):
            was = demod.squelch_db
            demod.squelch_db = self._squelch_db
            if self._squelch_db is None:
                demod.squelch_open = True
            elif was is None:
                demod.squelch_open = False  # re-arm: must prove quieting to open

    # -- source events ---------------------------------------------------------

    def _on_event(self, event: "SourceEvent") -> None:
        if event.kind not in ("generation", "run"):
            return
        self._generation = event.generation
        self._next_index = event.sample_index
        self._flush()
        self._rebuild(event.descriptor)

    def _flush(self) -> None:
        self._pending_index = None
        for q in (self._queue, self._flat_queue, self._clean_queue):
            while not q.empty():
                q.get_nowait()

    def _rebuild(self, desc: SourceDescriptor) -> None:
        """Fresh DSP state for the (possibly new) run; a channel outside the
        new span goes silent as ``unavailable`` rather than erroring (§2)."""
        self._chain = None
        self._decoder_demod = None
        self._effects.reset()
        self._inspection_extractor = None
        self._inspection_index = None
        self._audio_inspection_index = None
        self._drift.reset()
        self._unavailable = False
        self._reason = None
        ch = self._channel
        if ch is None:
            return
        margin = filter_margin_hz(ch.demodulator, ch.bandwidth_hz)
        if not ch.fits(desc, margin_hz=margin):
            self._unavailable = True
            self._reason = _span_reason(ch, desc, margin)
            return
        self._fs_in = float(desc.sample_rate_hz)
        self._chain = DemodChain(
            desc.sample_rate_hz, ch.offset_hz(desc), ch.demodulator, ch.bandwidth_hz
        )
        # A separate default demodulator keeps decoder taps unchanged when
        # listening AGC/passband/de-emphasis is adjusted.
        if ch.demodulator != "NFM":
            self._decoder_demod = DemodChain(
                desc.sample_rate_hz, ch.offset_hz(desc), ch.demodulator, ch.bandwidth_hz
            ).demod
        self._apply_native_audio()
        # The same selected-channel tap supplies envelope, I/Q scope and an
        # opt-in carrier estimate. It is decimated to a bounded inspection rate.
        cutoff, protect, _ = channel_filter(ch.demodulator, ch.bandwidth_hz)
        inspection_rate = 250_000 if ch.demodulator == "WFM" else 50_000
        self._inspection_rate = min(inspection_rate, int(desc.sample_rate_hz))
        self._inspection_extractor = ChannelExtractor(
            desc.sample_rate_hz,
            ch.offset_hz(desc),
            self._inspection_rate,
            cutoff,
            protect,
        )
        self._apply_listening()

    def configure_drift(
        self,
        enabled: bool = False,
        *,
        max_offset_hz: float = 5_000.0,
        alpha: float = 0.2,
        min_level_db: float = -60.0,
        kind: str = "carrier",
    ) -> dict:
        """Track an explicitly selected continuous carrier; optionally correct it.

        ``enabled=False`` leaves correction off while the estimate remains
        available for inspection. Supported kinds are unmodulated carriers
        only; modulation-bearing protocols are not inferred from this tap.
        """
        if kind not in ("carrier", "unmodulated_carrier"):
            raise ValueError(
                "drift tracking supports only explicitly selected unmodulated carriers"
            )
        self._drift = CarrierDriftTracker(
            max_offset_hz=max_offset_hz,
            min_rms=10 ** (float(min_level_db) / 20.0),
            smoothing=alpha,
        )
        self._drift_track = True
        self._drift_correct = bool(enabled)
        self._drift_signal_kind = "unmodulated_carrier"
        return self.drift_status()

    def drift_status(self) -> dict:
        return {
            **self._drift.reading.as_dict(),
            "tracking": self._drift_track,
            "correction_enabled": self._drift_correct,
            "signal_kind": self._drift_signal_kind,
            "supported_signal_kinds": list(CarrierDriftTracker.SUPPORTED_SIGNALS),
            "max_offset_hz": self._drift.max_offset_hz,
        }

    # -- data path ---------------------------------------------------------------

    def process_frame(self, frame) -> AudioBlock | None:
        """Demodulate one IQFrame; returns the AudioBlock queued (if any)."""
        gen = getattr(frame, "generation", 0)
        if gen != self._generation:
            desc = self._source.descriptor()
            if gen > self._generation and desc.generation == gen:
                # Event missed (listener not attached yet): resync from the
                # source rather than play a generation we have no state for.
                self._generation = gen
                self._flush()
                self._rebuild(desc)
                frame_idx = getattr(frame, "sample_index", None)
                # Already resynced above; this frame's index is the new
                # baseline, not a gap to flag again below.
                self._next_index = frame_idx if frame_idx is not None else desc.sample_index
            else:
                self.stale_frames += 1
                return None
        idx = getattr(frame, "sample_index", None)
        if idx is None:
            idx = self._next_index
        gap = idx != self._next_index
        if gap:
            # Same generation, but not contiguous with what we last saw: a
            # frame was dropped in transport (stalled consumer,
            # "dropped"). The chain's filter/decimator state assumes
            # unbroken samples, so it's no longer trustworthy -- flush and
            # rebuild exactly as a generation break would.
            self.dropped_frame_gaps += 1
            gap = True
            # Restart replay protocol decoders on their next tap block too.
            self.channel_generation += 1
            logger.warning(
                "virtual receiver: sample_index gap (expected %d, got %d); "
                "resetting DSP state",
                self._next_index,
                idx,
            )
            desc = self._source.descriptor()
            self._flush()
            self._rebuild(desc)
        n = len(frame.data) // 4
        self._next_index = idx + n
        chain = self._chain
        if chain is None or n == 0:
            return None
        if self._pending_index is None:
            self._pending_index = idx
        t0 = time.perf_counter()
        demod = chain.demod
        clean = None
        raw_iq = iq16_to_complex(frame.data)
        session_id = (
            f"{getattr(self._source, 'source_id', 'source')}:"
            f"{getattr(frame, 'run_index', 0)}:{getattr(frame, 'session_generation', None)}:"
            f"{self.channel_generation}"
        )
        if self._inspection_extractor is not None:
            selected_iq = self._inspection_extractor.process(raw_iq)
            if selected_iq.size:
                if self._inspection_index is None or gap:
                    self._inspection_index = int(round(idx * self._inspection_rate / self._fs_in))
                self.inspection.append_channel_iq(
                    selected_iq,
                    sample_rate_hz=self._inspection_rate,
                    sample_index=self._inspection_index,
                    generation=gen,
                    session_id=session_id,
                )
                if self._drift_track:
                    self._drift.update(
                        selected_iq,
                        sample_rate_hz=self._inspection_rate,
                        sample_index=self._inspection_index,
                        generation=gen,
                        session_id=session_id,
                        signal_kind=self._drift_signal_kind,
                    )
                self._inspection_index += selected_iq.size
        if self._drift_correct:
            raw_iq = self._drift.correct(raw_iq, sample_rate_hz=self._fs_in)
        custom_demod = any(
            self._native_audio[key] != default
            for key, default in (
                ("agc_mode", "default"),
                ("ssb_low_hz", 0),
                ("ssb_high_hz", None),
                ("cw_pitch_hz", 700),
                ("wfm_deemphasis_us", 75),
            )
        )
        if self._clean_consumers and custom_demod and not isinstance(demod, NfmDemod):
            audio = chain.process(raw_iq)
            clean = self._decoder_demod.process(chain.selected)
        elif self._clean_consumers and not isinstance(demod, NfmDemod):
            # Decoder tap: run the demod at unity gain, unmuted, and
            # apply the listener's volume/mute here instead -- identical
            # listening output (_Listening._out is exactly x*volume, or
            # zeros when muted), plus the pre-gain signal for decoders.
            volume, muted = demod.volume, demod.muted
            demod.volume, demod.muted = 1.0, False
            try:
                clean = chain.process(raw_iq)
            finally:
                demod.volume, demod.muted = volume, muted
            audio = (
                np.zeros(clean.size, dtype=np.float32)
                if muted
                else (clean * volume).astype(np.float32)
            )
        else:
            audio = chain.process(raw_iq)
        audio = self._effects.process(audio)
        if audio.size:
            if self._audio_inspection_index is None or gap:
                self._audio_inspection_index = int(round(idx * self.audio_rate / self._fs_in))
            self.inspection.append_audio(
                audio,
                sample_rate_hz=self.audio_rate,
                sample_index=self._audio_inspection_index,
                generation=gen,
                session_id=session_id,
            )
            self._audio_inspection_index += audio.size
        flat = chain.flat
        load = (time.perf_counter() - t0) / (n / self._fs_in)
        self._cpu_load += _CPU_EMA_ALPHA * (load - self._cpu_load)
        if audio.size == 0:
            return None
        block = AudioBlock(to_s16(audio), self.audio_rate, gen, self._pending_index)
        if flat is not None and self._flat_consumers:
            if _put(self._flat_queue, (to_s16(flat), gen, self._pending_index)):
                self.dropped_tap_blocks += 1
        if self._clean_consumers:
            # NFM: the discriminator output is already pre-squelch/pre-gain.
            tap = flat if flat is not None else clean
            if tap is not None:
                if _put(self._clean_queue, (to_s16(tap), gen, self._pending_index)):
                    self.dropped_tap_blocks += 1
        self._pending_index = None
        if _put(self._queue, block):
            self.dropped_blocks += 1
        return block

    async def audio(self) -> AsyncIterator[AudioBlock]:
        while True:
            block = await self._queue.get()
            if block.generation == self._generation:
                yield block

    async def flat_tap(self) -> AsyncIterator[tuple[bytes, int, int]]:
        """NFM decoder tap: (s16 mono at 48 kHz, generation, sample_index)."""
        self._flat_consumers += 1
        try:
            while True:
                pcm, gen, idx = await self._flat_queue.get()
                if gen == self._generation:
                    yield pcm, gen, idx
        finally:
            self._flat_consumers -= 1

    async def clean_tap(self) -> AsyncIterator[tuple[bytes, int, int]]:
        """Decoder tap for every demodulator: (s16 mono at 48 kHz,
        generation, sample_index) of the demod output *before* the
        listening volume/mute and squelch -- AM/SSB/CW keep their AGC (part
        of the demodulator), NFM yields the flat discriminator."""
        self._clean_consumers += 1
        try:
            while True:
                pcm, gen, idx = await self._clean_queue.get()
                if gen == self._generation:
                    yield pcm, gen, idx
        finally:
            self._clean_consumers -= 1

    def rds(self) -> dict | None:
        if self._chain is None or self._chain.rds_parser is None:
            return None
        return self._chain.rds_parser.result()

    # -- status ------------------------------------------------------------------

    def status(self) -> dict:
        ch = self._channel
        if self._unavailable:
            state = "unavailable"
        elif ch is not None and self.running:
            state = "running"
        else:
            state = "idle"
        demod = self._chain.demod if self._chain is not None else None
        rds = self.rds()
        return {
            "source_id": getattr(self._source, "source_id", None),
            "channel": (
                {
                    "frequency_hz": ch.frequency_hz,
                    "bandwidth_hz": ch.bandwidth_hz,
                    "demodulator": ch.demodulator,
                }
                if ch is not None
                else None
            ),
            "frequency_hz": ch.frequency_hz if ch else None,
            "demodulator": ch.demodulator if ch else None,
            "bandwidth_hz": ch.bandwidth_hz if ch else None,
            "state": state,
            "reason": self._reason,
            "squelch_open": demod.squelch_open if isinstance(demod, NfmDemod) else True,
            "squelch_db": self._squelch_db,
            "quieting_db": (
                round(demod.quieting_db, 1)
                if isinstance(demod, NfmDemod) and demod.quieting_db is not None
                else None
            ),
            "volume": self._volume,
            "muted": self._muted,
            "native_audio": dict(self._native_audio),
            "rds": (
                {k: rds[k] for k in ("pi", "ps", "rt", "block_error_rate", "groups")}
                if rds
                else None
            ),
            "dropped_blocks": self.dropped_blocks,
            "stale_frames": self.stale_frames,
            "dropped_frame_gaps": self.dropped_frame_gaps,
            "generation": self._generation,
            "channel_generation": self.channel_generation,
            "audio_rate": self.audio_rate,
            "audio_latency_ms": round(1000 * 512 / self.audio_rate, 2)
            if self._native_audio["nr_enabled"]
            else 0.0,
            "cpu_load": float(np.round(self._cpu_load, 4)),
            "drift": self.drift_status(),
        }
