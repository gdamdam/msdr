"""Live inspection and spectrum watch lifecycle; all control stays on the service loop."""

import asyncio
import contextlib
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from msdr.recorder.iq_stream import IQStream
from msdr.recorder.trigger import ActivityTrigger, ActivityTriggerConfig
from msdr.source.live import LiveSource
from msdr.source.virtual_receiver import VirtualReceiver
from msdr.survey.watch import WithinBandWatcher, plan_span_groups


class WatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    frequencies_hz: list[int] = Field(min_length=1, max_length=64)
    priority_hz: int | None = None
    threshold_db: float = Field(default=10, ge=0, le=80)
    stop_threshold_db: float = Field(default=6, ge=-10, le=80)
    min_active_s: float = Field(default=0.3, ge=0, le=60)
    quiet_hold_s: float = Field(default=5, ge=0.1, le=120)
    max_recording_s: float = Field(default=120, ge=1, le=3600)
    event_cap: int = Field(default=20, ge=1, le=1000)
    channel_width_hz: int = Field(default=12500, ge=100, le=250000)
    demodulator: str = "NFM"
    auto_record: bool = False
    follow: bool = False
    cycle_groups: bool = False
    dwell_s: float = Field(default=10, ge=1, le=600)

    @model_validator(mode="after")
    def validate_watch(self):
        if any(f <= 0 or f > 10**11 for f in self.frequencies_hz):
            raise ValueError("watch frequencies must be positive Hz")
        self.frequencies_hz = list(dict.fromkeys(self.frequencies_hz))
        if self.priority_hz is not None and self.priority_hz not in self.frequencies_hz:
            raise ValueError("priority must be a watched frequency")
        if self.stop_threshold_db >= self.threshold_db:
            raise ValueError("stop threshold must be lower than start threshold")
        if self.demodulator not in {"AM", "NFM", "WFM", "USB", "LSB", "CW"}:
            raise ValueError("unsupported demodulator")
        return self


class LiveInspectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency_hz: int | None = Field(default=None, gt=0)
    demodulator: Literal["AM", "NFM", "WFM", "USB", "LSB", "CW"] = "NFM"
    bandwidth_hz: int = Field(default=12500, ge=100, le=250000)


class DriftConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    max_offset_hz: float = Field(default=500, gt=0, le=5000)
    alpha: float = Field(default=0.2, gt=0, le=1)
    min_level_db: float = Field(default=-60, ge=-140, le=0)
    kind: Literal["carrier"] = "carrier"


class ScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    stream: Literal["audio", "iq_envelope", "iq_i", "iq_q"] = "audio"
    trigger_mode: Literal["none", "rising", "falling"] = "none"
    trigger_level: float | None = None


class _CaptureFacade:
    def __init__(self, owner):
        self.owner = owner

    def status(self):
        return self.owner.service.recorder.status()

    async def record(self):
        service = self.owner.service
        result = await service.recorder_start()
        if service._rec_obs:
            obs = service.observations.get(service._rec_obs["id"])
            if obs:
                obs.reported_hz = self.owner.trigger.config.frequency_hz
                obs.notes = (f"Activity trigger at {obs.reported_hz} Hz; "
                             f"start {self.owner.config.threshold_db:g} dB above estimated floor; "
                             f"quiet hold {self.owner.config.quiet_hold_s:g} s. Full-span IQ.")
                service.observations.update(obs)
        return result

    async def stop_recording(self):
        return await self.owner.service.recorder_stop()


class SignalWorkbench:
    def __init__(self, service):
        self.service = service
        self.live_rx = None
        self._live_span = None
        self._live_lock = asyncio.Lock()
        self._watch_lock = asyncio.Lock()
        self._task = None
        self._queue = None
        self.config = None
        self.watcher = None
        self.trigger = None
        self.groups = []
        self.group_index = 0
        self.error = None
        self._valid_after = 0.0
        self._group_since = 0.0
        self._last_frame_at = None
        self._confirmed_span = None
        self._group_tuning = False
        self.audio_active = False

    @property
    def active(self):
        return self._task is not None and not self._task.done()

    @property
    def owns_receiver(self):
        return self.active and self.config.cycle_groups

    def span(self):
        props = self.service.confirmed.props
        if (not self.service.adapter.connected or not props.get("started")
                or not self.service.confirmed.is_complete()):
            raise ValueError("start the receiver before live inspection or watching")
        center, rate = props.get("device_center_frequency"), props.get("device_sample_rate")
        if not center or not rate:
            raise ValueError("receiver span is not confirmed")
        return int(center), int(rate)

    async def start_live(self, frequency_hz=None, demodulator="NFM", bandwidth_hz=12500):
        async with self._live_lock:
            center, rate = self.span()
            await self._stop_live()
            source = LiveSource(adapter=self.service.adapter, hub=self.service.spectrum_hub,
                                iq_stream_factory=lambda: IQStream(
                                    self.service.settings.host, self.service.settings.port),
                                center_hz=center, sample_rate_hz=rate)
            # Fixed confirmed span. The watcher/inspection API tears it down on
            # any hardware span change; software channel tuning stays inside it.
            rx = VirtualReceiver(source)
            rx.configure_audio(**self.service.settings.native_audio.model_dump())
            rx.tune(
                frequency_hz or self.service.confirmed.props.get("device_vfo_frequency", center),
                demodulator,
                bandwidth_hz,
            )
            await rx.start()
            self.live_rx, self._live_span = rx, (center, rate)
            return rx.status()

    async def receiver_changed(self):
        """Invalidate spectrum work and stop a fixed-span live receiver.

        AppService calls this after confirmed span/lifecycle updates. A watch
        survives its own grouped tune; queued frames and old measurements are
        discarded in every case so no frame is attributed to the wrong span.
        """
        try:
            current_span = self.span()
        except ValueError:
            current_span = None
        if self.live_rx is not None and current_span != self._live_span:
            await self.stop_live()
        if self.active and current_span != self._confirmed_span:
            self._confirmed_span = current_span
            self._invalidate_watch_frames(
                "receiver_unavailable" if current_span is None else "span_changed"
            )
            if self.trigger:
                await self.trigger.suspend_observations()
                await self.trigger.tick()
        return self.status()

    def _invalidate_watch_frames(self, reason="span_changed"):
        self._valid_after = time.monotonic() + 0.25
        self._group_since = self._valid_after
        self._last_frame_at = None
        if self.watcher is not None:
            self.watcher.invalidate(reason)
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()

    async def _stop_live(self):
        rx, self.live_rx = self.live_rx, None
        self._live_span = None
        if rx:
            await rx.stop()

    async def stop_live(self):
        async with self._live_lock:
            await self._stop_live()

    async def inspection_receiver(self, mode):
        if mode == "review":
            rx = self.service.virtual_rx
        elif mode == "live":
            try:
                valid = self.span() == self._live_span
            except ValueError:
                valid = False
            if self.live_rx is not None and not valid:
                await self.stop_live()
            rx = self.live_rx
        else:
            raise ValueError("mode must be review or live")
        if rx is None:
            raise ValueError(f"start {mode} inspection first")
        return rx

    def status(self):
        props = self.service.confirmed.props
        return {"active": self.active, "error": self.error,
                "config": self.config.model_dump() if self.config else None,
                "watch": self.watcher.status() if self.watcher else None,
                "trigger": self.trigger.status() if self.trigger else None,
                "groups": [g.as_dict() for g in self.groups], "group_index": self.group_index,
                "live_receiver": self.live_rx.status() if self.live_rx else None,
                "tuned_hz": props.get("device_vfo_frequency"),
                # Confirmed receiver span and readiness, so the UI can preview
                # which watch frequencies are covered before a watch starts.
                "center_hz": props.get("device_center_frequency"),
                "sample_rate_hz": props.get("device_sample_rate"),
                "receiver": {"connected": bool(self.service.adapter.connected),
                             "started": bool(props.get("started")),
                             "complete": bool(self.service.confirmed.is_complete())},
                "fresh": self._last_frame_at is not None and
                         time.monotonic() - self._last_frame_at < 2}

    async def start_watch(self, config):
        async with self._watch_lock:
            await self.stop_watch()
            center, rate = self.span()
            service = self.service
            if service.spectrum_hub is None:
                raise ValueError("spectrum stream is unavailable")
            if (service.scanner.state != "idle" or service._band_scan_active
                    or service.doppler_loop.status is not None
                    or (service.beacons and service.beacons.status().get("active"))):
                raise ValueError("stop scanning, tracking and beacons before starting watch")
            if config.cycle_groups and service.decoder and service.decoder.status().get("active"):
                raise ValueError("stop decoding before cycling receiver spans")
            if (service.recorder and service.recorder.status().get("state")
                    in {"recording", "finalizing"}):
                raise ValueError("finish the current recording before starting watch")
            self.config, self.error = config, None
            self.watcher = WithinBandWatcher(config.frequencies_hz, hold_s=config.quiet_hold_s,
                                             channel_width_hz=config.channel_width_hz,
                                             activity_threshold_db=config.threshold_db,
                                             stop_threshold_db=config.stop_threshold_db)
            self.watcher.set_priority(config.priority_hz)
            self.groups = plan_span_groups(config.frequencies_hz, rate)
            self.group_index = 0
            self.trigger = None
            if config.auto_record:
                service._require_recorder_feature()
                await service.recorder_arm(True)
                self.trigger = ActivityTrigger(_CaptureFacade(self), ActivityTriggerConfig(
                    frequency_hz=config.priority_hz or config.frequencies_hz[0],
                    start_snr_db=config.threshold_db, stop_snr_db=config.stop_threshold_db,
                    min_active_s=config.min_active_s, quiet_hold_s=config.quiet_hold_s,
                    max_recording_s=config.max_recording_s, event_cap=config.event_cap))
                await self.trigger.set_enabled(True)
            self._queue = service.spectrum_hub.acquire()
            self._valid_after = time.monotonic() + 0.25
            self._group_since = time.monotonic()
            self._last_frame_at = None
            self._confirmed_span = (center, rate)
            self._task = asyncio.create_task(self._run_watch(), name="signal-watch")
            return self.status()

    async def stop_watch(self):
        task, self._task = self._task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.trigger:
            await self.trigger.set_enabled(False)
        if self._queue is not None:
            self.service.spectrum_hub.release(self._queue)
            self._queue = None
        self._last_frame_at = None
        return self.status()

    async def stop(self):
        await self.stop_watch()
        await self.stop_live()

    async def _tune_group(self):
        group = self.groups[self.group_index]
        _, rate = self.span()
        await self.stop_live()
        self._group_tuning = True
        try:
            confirmed = await self.service._band_scan_tune_span(
                rate, group.center_hz, kind="watch_span"
            )
            if not confirmed or self.span() != (group.center_hz, rate):
                raise ValueError("receiver did not confirm the watch span")
            self._confirmed_span = (group.center_hz, rate)
            self._invalidate_watch_frames("span_changed")
            if self.trigger:
                await self.trigger.suspend_observations()
                await self.trigger.tick()
        finally:
            self._group_tuning = False

    async def _run_watch(self):
        try:
            if self.config.cycle_groups:
                await self._tune_group()
            while True:
                now = time.monotonic()
                center, rate = self.span()
                if (center, rate) != self._confirmed_span:
                    self._confirmed_span = (center, rate)
                    self._invalidate_watch_frames("span_changed")
                    if self.trigger:
                        await self.trigger.suspend_observations()
                        await self.trigger.tick()
                    continue
                frame_epoch_span = (center, rate)
                try:
                    frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except TimeoutError:
                    if self.trigger:
                        await self.trigger.suspend_observations()
                        await self.trigger.tick()
                    continue
                # The center/rate can change while get() is suspended. Such a
                # frame has no embedded span metadata, so discard it and start
                # a fresh settle window before interpreting later frames.
                after_span = self.span()
                if after_span != frame_epoch_span:
                    self._confirmed_span = after_span
                    self._invalidate_watch_frames("span_changed")
                    if self.trigger:
                        await self.trigger.suspend_observations()
                        await self.trigger.tick()
                    continue
                if frame.at < self._valid_after or time.monotonic() - frame.at > 1:
                    if self.trigger:
                        await self.trigger.suspend_observations()
                        await self.trigger.tick()
                    continue
                status = self.watcher.consume(frame, center_hz=center, sample_rate_hz=rate)
                self._last_frame_at = time.monotonic()
                if self.trigger:
                    row = next(r for r in status["measurements"]
                               if r["frequency_hz"] == self.trigger.config.frequency_hz)
                    if (row["covered"] and row["snr_db"] is not None
                            and row["activity_state"] != "unresolved"):
                        await self.trigger.observe(
                            row["frequency_hz"], row["snr_db"], at=frame.at
                        )
                    else:
                        await self.trigger.suspend_observations()
                    await self.trigger.tick()
                target = status["decode_target_hz"]
                if self.config.follow and target:
                    if self.live_rx is None or self._live_span != (center, rate):
                        await self.start_live(target, self.config.demodulator,
                                              self.config.channel_width_hz)
                    elif self.live_rx.status().get("frequency_hz") != target:
                        self.live_rx.tune(target, self.config.demodulator,
                                          self.config.channel_width_hz)
                recording = self.service.recorder and self.service.recorder.status().get("state")
                if (self.config.cycle_groups and len(self.groups) > 1 and not target
                        and now - self._group_since >= self.config.dwell_s
                        and recording not in {"recording", "finalizing"}):
                    self.group_index = (self.group_index + 1) % len(self.groups)
                    await self._tune_group()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self.trigger:
                await self.trigger.set_enabled(False)
            if self._queue is not None:
                self.service.spectrum_hub.release(self._queue)
                self._queue = None
            if self._task is asyncio.current_task():
                self._task = None
            publish_owner = getattr(self.service, "_publish_owner", None)
            if publish_owner is not None:
                with contextlib.suppress(Exception):
                    await publish_owner()
