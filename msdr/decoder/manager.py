"""DecoderManager: runtime that turns a memory's `decoders` list into running
subprocess decoders fed from SDRconnect's audio stream, and back again.

Subscription pattern mirrors engine/dwell.py's DwellObserver: subscribe to
ConfirmedState on start(), one background task consumes StateChanged events
for as long as the manager is alive, unsubscribe on stop().
"""

import asyncio
import contextlib
import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np

from msdr.decoder.audio_stream import AudioFrame
from msdr.decoder.backends import DecodeEvent, DecoderError, build_backend, reap_cancelled
from msdr.decoder.band import BandDecoder
from msdr.decoder.channel import DecodeChannel
from msdr.decoder.pcm import RateEstimator, to_mono_f32
from msdr.decoder.readiness import decoder_facts
from msdr.decoder.registry import (
    DECODERS,
    available,
    compatibility_issue,
    effective_binaries,
    resolve_binaries,
)

if TYPE_CHECKING:
    from msdr.engine.confirmed import ConfirmedState, StateChanged
    from msdr.settings import DecoderSettings

logger = logging.getLogger(__name__)


def _make_protocol_iq_backend(
    spec, *, sample_rate_hz, center_hz, frequency_hz, decoder_settings=None
):
    module_name, class_name = spec.args[0].split(":", 1)
    decoder_class = getattr(importlib.import_module(module_name), class_name)
    kwargs = {}
    if spec.id == "lora" and decoder_settings is not None:
        kwargs = {
            "spreading_factor": decoder_settings.lora_spreading_factor,
            "bandwidth_hz": decoder_settings.lora_bandwidth_hz,
            "coding_rate": decoder_settings.lora_coding_rate,
            "protocol": decoder_settings.lora_protocol,
        }
    return decoder_class(
        spec,
        sample_rate_hz=sample_rate_hz,
        center_hz=center_hz,
        frequency_hz=frequency_hz,
        **kwargs,
    )


# RMS below this for _SILENCE_S is "no audio", not just a quiet signal --
# SDRconnect's audio stream is int16 full-scale so real noise floor is well above this.
_SILENCE_RMS = 1e-4
_SILENCE_S = 3.0


def _empty_counts() -> dict:
    return {"events": 0, "valid_frames": 0, "last_event_ts": None}


class AudioStreamLike(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def enable(self, on: bool) -> None: ...
    def frames(self): ...

    connected: bool


class DecoderManager:
    def __init__(
        self,
        *,
        adapter,
        stream_factory: Callable[[], AudioStreamLike],
        repo,
        publish: Callable[[dict], None],
        settings_getter: Callable[[], "DecoderSettings"],
        confirmed_state: "ConfirmedState",
        feature_enabled: Callable[[], bool],
        iq_stream_factory: Callable[[], object] = lambda: None,
        signals_in_span: Callable[
            [float, int], list[tuple[float, str, list[str]]]
        ] = lambda c, s: [],
        calls_dir: Path | str | None = None,
        fax_dir: Path | str | None = None,
    ):
        self._adapter = adapter
        self._stream_factory = stream_factory
        self._repo = repo
        self._publish = publish
        self._settings_getter = settings_getter
        self._confirmed = confirmed_state
        self._feature_enabled = feature_enabled
        self._iq_stream_factory = iq_stream_factory
        self._signals_in_span = signals_in_span
        # Created lazily by the backend that actually needs it (dsd-fme's
        # per-call WAVs / the upcoming radiofax PNGs), not here.
        self._calls_dir = Path(calls_dir) if calls_dir is not None else Path("decoder/calls")
        self._fax_dir = Path(fax_dir) if fax_dir is not None else Path("decoder/fax")

        self._queue: asyncio.Queue[StateChanged] | None = None
        self._consumer_task: asyncio.Task | None = None

        self._stream: AudioStreamLike | None = None
        self._pump_task: asyncio.Task | None = None
        self._event_tasks: list[asyncio.Task] = []

        self._active = False
        # start_decoding awaits backend spawns and the stream before setting
        # _active; without a lock two concurrent starts both pass the check
        # and the loser's backends/stream are never stopped.
        self._lock = asyncio.Lock()
        self._source: Literal["memory", "manual"] | None = None
        self._source_kind: Literal["iq", "audio"] | None = None
        self._signal_id: str | None = None
        self._frequency_hz: int | None = None
        self._since: float | None = None
        self._channels: dict[str, DecodeChannel] = {}
        # image events (wefax): stored event id keyed by the image_path they
        # were persisted under, so a later event carrying update_of_path can
        # be turned into repo.update_event(id, ...) instead of a new row --
        # the PyDecoder itself never sees the repo/event id.
        self._image_event_ids: dict[str, str] = {}
        self._warnings: list[str] = []
        self._last_stop_reason: str | None = None
        self._squelch_was_enabled: bool | None = None

        self._rate_estimator: RateEstimator | None = None
        self._rate_configured = 48000
        self._rate_observed: int | None = None
        self._silent = False
        self._silence_since: float | None = None
        self._events_total = 0
        # S4 §1 per-decoder facts for the audio path (the IQ path's live in
        # its BandChannel): {"events", "valid_frames", "last_event_ts"}.
        self._decoder_counts: dict[str, dict] = {}
        # decoder id -> reason, for decoders started on a demodulator not in
        # their spec (they start anyway; the session says unsupported_input).
        self._unsupported: dict[str, str] = {}
        self._agc_was_enabled: bool | None = None

        self._band: BandDecoder | None = None
        # start_band/band_pin test `self._band is None`, await band.start()
        # (subprocess spawn), then assign: two concurrent HTTP calls would
        # both pass the check and the loser's BandDecoder leaks its processes.
        self._band_lock = asyncio.Lock()
        self._band_last_stop_reason: str | None = None
        # True only when band_pin() itself started self._band (no session was
        # active) -- the last band_unpin() then stops it too; a session the
        # user started with start_band() is never stopped by unpinning.
        self._band_started_by_pin = False

        # Fixed single-channel IQ session (this task): a BandDecoder used as
        # a one-channel decode instead of the audio path, independent of the
        # user-started `self._band` multi-channel session above.
        self._single_band: BandDecoder | None = None

    # -- runtime (subscribe/unsubscribe ConfirmedState) ----------------------

    async def start(self) -> None:
        self._queue = self._confirmed.subscribe()
        self._consumer_task = asyncio.create_task(self._consume_state())

    async def stop(self) -> None:
        if self._active:
            await self.stop_decoding("shutdown")
        if self._band is not None:
            await self.stop_band("shutdown")
        if self._consumer_task is not None and self._consumer_task is not asyncio.current_task():
            self._consumer_task.cancel()
            await reap_cancelled(self._consumer_task)
            self._consumer_task = None
        if self._queue is not None:
            self._confirmed.unsubscribe(self._queue)
            self._queue = None

    # -- control ---------------------------------------------------------

    async def start_decoding(
        self,
        decoders: list[str],
        frequency_hz: int,
        source: Literal["memory", "manual"],
        signal_id: str | None = None,
    ) -> dict:
        async with self._lock:
            return await self._start_decoding_locked(decoders, frequency_hz, source, signal_id)

    async def _start_decoding_locked(
        self,
        decoders: list[str],
        frequency_hz: int,
        source: Literal["memory", "manual"],
        signal_id: str | None,
    ) -> dict:
        if not self._feature_enabled():
            raise DecoderError("decoder feature disabled")
        if self._active:
            await self._stop_decoding_locked("restarted")

        settings = self._settings_getter()
        binaries = resolve_binaries(settings.bin_dirs)

        specs = []
        warnings: list[str] = []
        for decoder_id in decoders:
            spec = DECODERS.get(decoder_id)
            if spec is None:
                warnings.append(f"unknown decoder: {decoder_id}")
                continue
            if issue := compatibility_issue(spec):
                warnings.append(f"{decoder_id}: {issue}")
                continue
            names = effective_binaries(spec, binaries)
            missing = [name for name in names if not binaries.get(name)]
            if missing:
                warnings.append(f"{decoder_id}: missing {', '.join(missing)}")
                continue
            specs.append(spec)
        if decoders and not specs:
            raise DecoderError("no requested decoders are available")

        raw_iq = [spec for spec in specs if spec.iq_only and spec.demodulators == ("IQ",)]
        if raw_iq:
            if await self._adapter.get("started") in (False, "false"):
                warnings.append("receiver is stopped: press Play in SDRconnect to receive IQ")
            if len(raw_iq) != len(specs):
                raise DecoderError("Run raw-IQ decoders separately from audio decoders")
            from msdr.decoder.iq_session import IQDecoderSession
            from msdr.decoder.pydecoders.dsc import DSCBackend
            from msdr.decoder.pydecoders.navigation import NavigationIQDecoder
            from msdr.decoder.standalone_adsb import StandaloneADSBBackend
            from msdr.decoder.standalone_ft8 import StandaloneFT8Backend

            stream = self._iq_stream_factory()
            if stream is None:
                raise DecoderError("raw-IQ decoding requires a live IQ stream")
            center = float(await self._adapter.get("device_center_frequency"))
            rate = int(await self._adapter.get("device_sample_rate"))
            for spec in raw_iq:
                if rate < spec.input_rate:
                    raise DecoderError(f"{spec.label}: needs IQ rate ≥ {spec.input_rate} samples/s")
                if abs(frequency_hz - center) + spec.default_bandwidth_hz / 2 > rate / 2:
                    raise DecoderError(f"{spec.label}: full signal bandwidth must fit in IQ span")
            session = IQDecoderSession(
                stream=stream,
                backends={
                    spec.id: (
                        StandaloneADSBBackend(
                            binaries.get("dump1090"),
                            sample_rate_hz=rate,
                            center_hz=center,
                            frequency_hz=frequency_hz,
                        )
                        if spec.backend == "standalone_adsb"
                        else StandaloneFT8Backend(
                            binaries.get("jt9"),
                            sample_rate_hz=rate,
                            center_hz=center,
                            frequency_hz=frequency_hz,
                        )
                        if spec.backend == "standalone_ft8"
                        else DSCBackend(
                            spec,
                            sample_rate_hz=rate,
                            center_hz=center,
                            frequency_hz=frequency_hz,
                        )
                        if spec.backend == "standalone_dsc"
                        else NavigationIQDecoder(
                            spec,
                            sample_rate_hz=rate,
                            center_hz=center,
                            frequency_hz=frequency_hz,
                        )
                        if spec.backend == "standalone_navigation"
                        else _make_protocol_iq_backend(
                            spec,
                            sample_rate_hz=rate,
                            center_hz=center,
                            frequency_hz=frequency_hz,
                            decoder_settings=settings,
                        )
                    )
                    for spec in raw_iq
                },
                frequency_hz=frequency_hz,
                signal_id=signal_id,
                on_event=self._on_band_event,
                on_status=self._publish_status,
            )
            await session.start()
            self._single_band = session
            self._warnings = warnings
            self._source = source
            self._source_kind = "iq"
            self._signal_id = signal_id
            self._frequency_hz = frequency_hz
            self._since = time.time()
            self._active = True
            self._publish_status()
            return self.status()

        # SDRconnect tolerated enabling the audio stream while stopped in a live
        # check, but nothing arrives until Play -- say so instead of refusing.
        started = await self._adapter.get("started")
        receiver_stopped = started in (False, "false")
        if receiver_stopped:
            warnings.append("receiver is stopped: press Play in SDRconnect to receive audio")

        # NFM-family decoders (POCSAG/FLEX/EAS/APRS/DTMF) decode cleanly from
        # this manager's own IQ path -- SDRconnect's audio chain corrupts the
        # first codeword (DC blocking/de-emphasis, live test). cw/rtty and any
        # mixed set keep using the audio path unchanged.
        all_nfm = bool(specs) and all("NFM" in spec.demodulators for spec in specs)
        override = getattr(settings, "single_channel_source", "iq")
        iq_stream = None
        center_hz: float | None = None
        sample_rate_hz: int | None = None
        if override != "audio" and all_nfm and not receiver_stopped:
            candidate_stream = self._iq_stream_factory()
            if candidate_stream is not None:
                center_hz = float(await self._adapter.get("device_center_frequency"))
                sample_rate_hz = int(await self._adapter.get("device_sample_rate"))
                if abs(frequency_hz - center_hz) <= sample_rate_hz / 2 - 25_000:
                    iq_stream = candidate_stream
                else:
                    warnings.append("outside IQ span, using audio")

        if iq_stream is not None:
            assert center_hz is not None and sample_rate_hz is not None
            return await self._start_decoding_iq(
                decoders=decoders,
                frequency_hz=frequency_hz,
                source=source,
                signal_id=signal_id,
                warnings=warnings,
                iq_stream=iq_stream,
                binaries=binaries,
                center_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                settings=settings,
            )

        # iq_only decoders (e.g. 9600-baud FSK) need the raw discriminator
        # output the IQ path provides; the audio path always garbles them, so
        # skip rather than start them somewhere they can't decode.
        iq_only_specs = [spec for spec in specs if spec.iq_only]
        if iq_only_specs:
            specs = [spec for spec in specs if not spec.iq_only]
            for spec in iq_only_specs:
                warnings.append(f"{spec.id}: needs the IQ path (raw discriminator)")

        return await self._start_decoding_audio(
            specs=specs,
            frequency_hz=frequency_hz,
            source=source,
            signal_id=signal_id,
            warnings=warnings,
            binaries=binaries,
            settings=settings,
        )

    async def _start_decoding_audio(
        self,
        *,
        specs: list,
        frequency_hz: int,
        source: Literal["memory", "manual"],
        signal_id: str | None,
        warnings: list[str],
        binaries: dict[str, str | None],
        settings: "DecoderSettings",
    ) -> dict:
        demod = await self._adapter.get("demodulator")
        unsupported: dict[str, str] = {}
        for spec in specs:
            if demod not in spec.demodulators:
                # Incompatible combos start anyway (spec "Starting and
                # stopping"); msdr never changes the memory's demodulator.
                reason = f"{spec.id}: demodulator {demod} not in {spec.demodulators}"
                warnings.append(reason)
                unsupported[spec.id] = reason

        squelch = bool(await self._adapter.get("squelch_enable"))
        self._squelch_was_enabled = squelch
        if squelch:
            await self._adapter.set("squelch_enable", False)

        # SDRconnect's AGC re-gains at burst onset, garbling the first
        # codewords of a POCSAG/FLEX transmission (live test: the first page text
        # came out garbled, 2 of 6 pages decoded) -- same
        # disable-while-decoding, restore-on-stop treatment as squelch.
        agc = await self._adapter.get("agc_enable")
        agc_enabled = agc if isinstance(agc, bool) else str(agc).lower() == "true"
        self._agc_was_enabled = agc_enabled
        if agc_enabled:
            await self._adapter.set("agc_enable", False)

        channels: dict[str, DecodeChannel] = {}
        for spec in specs:
            try:
                backend = build_backend(spec, binaries, fax_dir=self._fax_dir)
                await backend.start()
            except DecoderError as exc:
                warnings.append(str(exc))
                continue
            channel = DecodeChannel(
                spec, backend, settings.audio_sample_rate, settings.audio_channels
            )
            channel.state = "running"
            channels[spec.id] = channel

        if not channels:
            if self._squelch_was_enabled:
                await self._adapter.set("squelch_enable", True)
            self._squelch_was_enabled = None
            if self._agc_was_enabled:
                await self._adapter.set("agc_enable", True)
            self._agc_was_enabled = None
            raise DecoderError("no requested decoders could be started")

        self._channels = channels
        self._decoder_counts = {decoder_id: _empty_counts() for decoder_id in channels}
        self._unsupported = unsupported
        self._warnings = warnings
        self._source = source
        self._source_kind = "audio"
        self._signal_id = signal_id
        self._frequency_hz = frequency_hz
        self._since = time.time()
        self._rate_estimator = RateEstimator()
        self._rate_configured = settings.audio_sample_rate
        self._rate_observed = None
        self._silent = False
        self._silence_since = None
        self._image_event_ids = {}

        self._stream = self._stream_factory()
        await self._stream.start()
        await self._stream.enable(True)
        self._active = True

        self._pump_task = asyncio.create_task(self._pump())
        self._event_tasks = [
            asyncio.create_task(self._drain_events(channel)) for channel in channels.values()
        ]

        self._publish_status()
        return self.status()

    async def _start_decoding_iq(
        self,
        *,
        decoders: list[str],
        frequency_hz: int,
        source: Literal["memory", "manual"],
        signal_id: str | None,
        warnings: list[str],
        iq_stream: object,
        binaries: dict[str, str | None],
        center_hz: float,
        sample_rate_hz: int,
        settings: "DecoderSettings",
    ) -> dict:
        band = BandDecoder(
            iq_stream_factory=lambda: iq_stream,
            decoders=decoders,
            binaries=binaries,
            center_hz=center_hz,
            sample_rate_hz=sample_rate_hz,
            memories=[(frequency_hz, signal_id, decoders)],
            max_channels=0,
            threshold_db=getattr(settings, "band_threshold_db", 10.0),
            idle_s=getattr(settings, "band_idle_s", 60),
            spacing_hz=getattr(settings, "band_spacing_hz", 12500),
            on_event=self._on_band_event,
            on_status=self._publish_status,
            detect=False,
            source_tag="iq",
            calls_dir=self._calls_dir,
            fax_dir=self._fax_dir,
        )
        await band.start()

        self._single_band = band
        self._warnings = warnings
        self._source = source
        self._source_kind = "iq"
        self._signal_id = signal_id
        self._frequency_hz = frequency_hz
        self._since = time.time()
        self._active = True

        self._publish_status()
        return self.status()

    async def stop_decoding(self, reason: str = "user") -> dict:
        async with self._lock:
            return await self._stop_decoding_locked(reason)

    async def _stop_decoding_locked(self, reason: str) -> dict:
        if not self._active:
            return self.status()
        self._active = False
        self._last_stop_reason = reason

        if self._source_kind == "iq":
            await self._stop_single_iq()
        else:
            if self._pump_task is not None:
                self._pump_task.cancel()
                await reap_cancelled(self._pump_task)
                self._pump_task = None
            for task in self._event_tasks:
                task.cancel()
            for task in self._event_tasks:
                await reap_cancelled(task)
            self._event_tasks = []

            for channel in self._channels.values():
                await channel.stop()
            self._channels = {}

            if self._stream is not None:
                await self._stream.stop()
                self._stream = None

            if self._squelch_was_enabled:
                await self._adapter.set("squelch_enable", True)
            self._squelch_was_enabled = None

            if self._agc_was_enabled:
                await self._adapter.set("agc_enable", True)
            self._agc_was_enabled = None

        self._source_kind = None
        self._source = None
        self._signal_id = None
        self._frequency_hz = None
        self._since = None
        self._rate_estimator = None

        self._publish_status()
        return self.status()

    async def _stop_single_iq(self) -> None:
        if self._single_band is not None:
            await self._single_band.stop()
            self._single_band = None

    def status(self) -> dict:
        settings = self._settings_getter()

        if self._source_kind == "iq" and self._single_band is not None:
            single_status = self._single_band.status()
            channels = single_status["channels"]
            if channels:
                fixed = channels[0]
                per_decoder = {d["id"]: d for d in fixed.get("decoder_status") or []}
                decoders_status = [
                    per_decoder.get(decoder_id)
                    or decoder_facts(decoder_id, state=fixed["state"], error=fixed["error"])
                    for decoder_id in fixed["decoders"]
                ]
            else:
                decoders_status = []
            audio_connected = single_status["iq"]["connected"]
        else:
            decoders_status = [
                self._audio_decoder_facts(channel) for channel in self._channels.values()
            ]
            audio_connected = bool(self._stream.connected) if self._stream is not None else False

        return {
            "active": self._active,
            "source": self._source,
            "signal_id": self._signal_id,
            "frequency_hz": self._frequency_hz,
            "since": self._since,
            "decoders": decoders_status,
            "warnings": list(self._warnings),
            "last_stop_reason": self._last_stop_reason,
            "audio": {
                "connected": audio_connected,
                "rate_configured": self._rate_configured,
                "rate_observed": self._rate_observed,
                "silent": self._silent,
            },
            "available": available(settings.bin_dirs),
            "binaries": resolve_binaries(settings.bin_dirs),
            "events_total": self._events_total,
            "band": self._band_status(),
            "source_kind": self._source_kind,
        }

    def _audio_decoder_facts(self, channel: DecodeChannel) -> dict:
        counts = self._decoder_counts.get(channel.spec.id) or _empty_counts()
        dsd_state = getattr(getattr(channel, "_backend", None), "dsd_state", None)
        return decoder_facts(
            channel.spec.id,
            state=channel.state,
            error=channel.error,
            events=counts["events"],
            valid_frames=counts["valid_frames"],
            last_event_ts=counts["last_event_ts"],
            synchronized=getattr(dsd_state, "synchronized", None),
            quiet=self._silent,
            unsupported=self._unsupported.get(channel.spec.id),
        )

    # -- band decode (multi-channel from IQ, P2) -----------------------------

    async def start_band(
        self,
        decoders: list[str],
        *,
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        async with self._band_lock:
            return await self._start_band_locked(
                decoders, max_channels=max_channels, threshold_db=threshold_db
            )

    async def _start_band_locked(
        self,
        decoders: list[str],
        *,
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        if not self._feature_enabled():
            raise DecoderError("decoder feature disabled")
        invalid = [
            d for d in decoders if d not in DECODERS or "NFM" not in DECODERS[d].demodulators
        ]
        if invalid:
            raise DecoderError(f"not NFM-family decoders: {', '.join(invalid)}")

        # Same "enabling while stopped" precedent as the IQ stream / single
        # decode: nothing arrives until Play, so refuse rather than spin up
        # a channelizer with no data.
        started = await self._adapter.get("started")
        if started in (False, "false"):
            raise DecoderError("receiver is stopped")

        if self._band is not None:
            await self._stop_band_locked("restarted")

        settings = self._settings_getter()
        binaries = resolve_binaries(settings.bin_dirs)
        center_hz = int(await self._adapter.get("device_center_frequency"))
        sample_rate_hz = int(await self._adapter.get("device_sample_rate"))
        memories = self._signals_in_span(center_hz, sample_rate_hz)

        band = BandDecoder(
            iq_stream_factory=self._iq_stream_factory,
            decoders=decoders,
            binaries=binaries,
            center_hz=center_hz,
            sample_rate_hz=sample_rate_hz,
            memories=memories,
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
            on_status=self._publish_status,
            calls_dir=self._calls_dir,
            fax_dir=self._fax_dir,
        )
        await band.start()
        self._band = band
        self._publish_status()
        return self.status()

    async def stop_band(self, reason: str = "user") -> dict:
        async with self._band_lock:
            return await self._stop_band_locked(reason)

    async def _stop_band_locked(self, reason: str) -> dict:
        if self._band is None:
            return self.status()
        self._band_last_stop_reason = reason
        await self._band.stop()
        self._band = None
        self._band_started_by_pin = False
        self._publish_status()
        return self.status()

    # -- pinned channel (S5 decode handoff) ----------------------------------

    async def band_pin(self, frequency_hz: int, decoders: list[str]) -> dict:
        """Start decoding one peak from the survey view: adds a fixed
        (never-idle-retired) channel to the running band session, starting a
        `detect=False, max_channels=0` session first if none is active."""
        async with self._band_lock:
            return await self._band_pin_locked(frequency_hz, decoders)

    async def _band_pin_locked(self, frequency_hz: int, decoders: list[str]) -> dict:
        if not self._feature_enabled():
            raise DecoderError("decoder feature disabled")

        if self._band is not None:
            band_status = self._band.status()
            center_hz = band_status["center_hz"]
            sample_rate_hz = band_status["sample_rate_hz"]
        else:
            started = await self._adapter.get("started")
            if started in (False, "false"):
                raise DecoderError("receiver is stopped")
            center_hz = int(await self._adapter.get("device_center_frequency"))
            sample_rate_hz = int(await self._adapter.get("device_sample_rate"))

        if abs(frequency_hz - center_hz) > sample_rate_hz / 2 - 25_000:
            raise DecoderError(f"{frequency_hz} Hz is outside the current span")

        if self._band is None:
            settings = self._settings_getter()
            binaries = resolve_binaries(settings.bin_dirs)

            band = BandDecoder(
                iq_stream_factory=self._iq_stream_factory,
                decoders=decoders,
                binaries=binaries,
                center_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                memories=[],
                max_channels=0,
                threshold_db=getattr(settings, "band_threshold_db", 10.0),
                idle_s=getattr(settings, "band_idle_s", 60),
                spacing_hz=getattr(settings, "band_spacing_hz", 12500),
                on_event=self._on_band_event,
                on_status=self._publish_status,
                detect=False,
                calls_dir=self._calls_dir,
                fax_dir=self._fax_dir,
            )
            await band.start()
            self._band = band
            self._band_started_by_pin = True

        await self._band.pin(frequency_hz, None, decoders)
        self._publish_status()
        return self.status()

    async def band_unpin(self, frequency_hz: int) -> dict:
        async with self._band_lock:
            return await self._band_unpin_locked(frequency_hz)

    async def _band_unpin_locked(self, frequency_hz: int) -> dict:
        if self._band is None:
            return self.status()

        await self._band.unpin(frequency_hz)
        still_pinned = any(c["source"] == "pinned" for c in self._band.status()["channels"])
        if not still_pinned and self._band_started_by_pin:
            return await self._stop_band_locked("pin session ended")

        self._publish_status()
        return self.status()

    def _on_band_event(self, event: DecodeEvent) -> None:
        stored = self._repo.add(event)
        self._events_total = self._repo.count()
        self._publish({"type": "decoder_event", "event": stored})

    def _band_status(self) -> dict:
        if self._band is not None:
            status = self._band.status()
        else:
            settings = self._settings_getter()
            status = {
                "active": False,
                "decoders": [],
                "center_hz": None,
                "sample_rate_hz": None,
                "spacing_hz": None,
                "max_channels": getattr(settings, "band_max_channels", 8),
                "threshold_db": getattr(settings, "band_threshold_db", 10.0),
                "since": None,
                "channels": [],
                "candidates": [],
                "iq": {"connected": False, "frames": 0, "dropped": 0, "cpu_load": 0.0},
            }
        status["last_stop_reason"] = self._band_last_stop_reason
        return status

    # -- internals: audio pump -----------------------------------------------

    async def _pump(self) -> None:
        assert self._stream is not None
        try:
            async for frame in self._stream.frames():
                self._check_rate(frame)
                self._check_silence(frame)
                changed = False
                for channel in self._channels.values():
                    channel.feed(frame.data)
                    err = channel.backend_error
                    if err and channel.state != "error":
                        channel.state = "error"
                        channel.error = err
                        changed = True
                if changed:
                    self._publish_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("decoder pump crashed")

    def _check_rate(self, frame: AudioFrame) -> None:
        if self._rate_estimator is None:
            return
        settings = self._settings_getter()
        n_samples = len(frame.data) // (2 * max(settings.audio_channels, 1))
        observed_raw = self._rate_estimator.add(n_samples, frame.received_at)
        if observed_raw is None:
            return
        observed = RateEstimator.snap(observed_raw)
        configured = self._rate_configured
        if configured and abs(observed - configured) / configured > 0.05:
            logger.warning(
                "decoder: observed audio rate %d deviates from configured %d", observed, configured
            )
            for channel in self._channels.values():
                channel.reconfigure(observed)
            self._rate_configured = observed
        self._rate_observed = observed
        self._rate_estimator = None  # estimation window done
        self._publish_status()

    def _check_silence(self, frame: AudioFrame) -> None:
        settings = self._settings_getter()
        mono = to_mono_f32(frame.data, max(settings.audio_channels, 1))
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        now = frame.received_at
        if rms < _SILENCE_RMS:
            if self._silence_since is None:
                self._silence_since = now
            elif not self._silent and now - self._silence_since >= _SILENCE_S:
                self._silent = True
                self._publish_status()
        else:
            was_silent = self._silent
            self._silent = False
            self._silence_since = None
            if was_silent:
                self._publish_status()

    async def _drain_events(self, channel: DecodeChannel) -> None:
        try:
            async for event in channel.events():
                event = replace(event, frequency_hz=self._frequency_hz, signal_id=self._signal_id)

                # wefax (S7-S10): the finishing event carries update_of_path
                # instead of a fresh image_path -- merge it into the event
                # stored at start rather than adding a new row.
                update_of_path = event.update_of_path
                if update_of_path is not None:
                    stored_id = self._image_event_ids.get(update_of_path)
                    if stored_id is not None:
                        updated = self._repo.update_event(
                            stored_id, text=event.text, fields=event.fields
                        )
                        if updated is not None:
                            self._publish({"type": "decoder_event_update", "event": updated})
                        continue
                    # No matching start event (e.g. it was trimmed already) --
                    # fall through and store it as an ordinary new row.

                stored = self._repo.add(event)
                # Any image decoder's start event (wefax "fax started", sstv
                # "SSTV <mode> started") carries image_path; later ones only
                # update_of_path.
                if event.kind == "image" and event.image_path and update_of_path is None:
                    self._image_event_ids[event.image_path] = stored["id"]
                counts = self._decoder_counts.setdefault(channel.spec.id, _empty_counts())
                counts["events"] += 1
                if event.integrity == "passed":
                    counts["valid_frames"] += 1
                counts["last_event_ts"] = event.ts
                self._events_total = self._repo.count()
                self._publish({"type": "decoder_event", "event": stored})
        except asyncio.CancelledError:
            raise

    # -- internals: follow the operator's own tuning -------------------------

    async def _consume_state(self) -> None:
        # Unlike P1, band decoding tracks the device span independently of
        # whether a single-channel decode is active (both may run at once),
        # so this can no longer bail out early on `not self._active`.
        assert self._queue is not None
        while True:
            sc = await self._queue.get()
            try:
                await self._handle_state_change(sc)
            except (ConnectionError, TimeoutError, TypeError, ValueError) as exc:
                # A span event followed by a failed read leaves us without a
                # trustworthy centre/rate. Stop every IQ consumer instead of
                # running it against a possibly stale span, then keep this
                # long-lived subscriber alive for the next confirmed change.
                logger.warning(
                    "decoder state refresh failed (%s); stopping active sessions",
                    type(exc).__name__,
                )
                if self._active:
                    with contextlib.suppress(Exception):
                        await self.stop_decoding("receiver state unavailable")
                if self._band is not None and sc.property in (
                    "device_center_frequency",
                    "device_sample_rate",
                ):
                    with contextlib.suppress(Exception):
                        await self.stop_band("receiver state unavailable")

    async def _handle_state_change(self, sc: "StateChanged") -> None:
        if sc.property == "started" and sc.new is False:
            if self._active:
                await self.stop_decoding("receiver stopped")
            if self._band is not None:
                await self.stop_band("receiver stopped")
            return
        if (
            self._active
            and sc.property == "device_vfo_frequency"
            and self._frequency_hz is not None
        ):
            with contextlib.suppress(TypeError, ValueError):
                new_vfo = float(sc.new)
                bw = await self._adapter.get("filter_bandwidth")
                half_bw = (bw or 0) / 2
                if abs(new_vfo - self._frequency_hz) > half_bw:
                    await self.stop_decoding("retuned")
        if (
            self._active
            and self._source_kind == "iq"
            and sc.property in ("device_center_frequency", "device_sample_rate")
        ):
            # The fixed channel's index (k) was computed against the old
            # centre/sample rate; any change makes it meaningless, unlike
            # the multi-channel `self._band` session below which rebuilds
            # its channelizer instead of stopping.
            await self.stop_decoding("retuned")
        if self._band is not None and sc.property in (
            "device_center_frequency",
            "device_sample_rate",
        ):
            center_hz = int(await self._adapter.get("device_center_frequency"))
            sample_rate_hz = int(await self._adapter.get("device_sample_rate"))
            memories = self._signals_in_span(center_hz, sample_rate_hz)
            await self._band.retune(center_hz, sample_rate_hz, memories)

    def _publish_status(self) -> None:
        self._publish({"type": "decoder", **self.status()})
