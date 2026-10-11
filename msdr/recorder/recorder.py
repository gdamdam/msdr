"""Pre-roll IQ recorder state machine: off -> armed -> recording -> finalizing -> (armed|off).

A writer failure or a radio stop can also drive the machine to "error", from
which a later arm()/stop() clears the error and proceeds normally.
"""

import asyncio
import contextlib
import inspect
import json
import logging
import shutil
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from msdr.recorder.iq_stream import IQFrame, IQStream
from msdr.recorder.ring import SegmentRing
from msdr.recorder.sigmf import annotation, build_meta
from msdr.tasks import spawn

logger = logging.getLogger(__name__)

_State = Literal["off", "armed", "recording", "finalizing", "error"]

# ~2s of frames at SDRconnect's observed ~50ms/frame cadence (spec measured
# facts), floor of 8 so a much slower device still gets a usable buffer.
_QUEUE_SIZE = max(8, round(2.0 / 0.05))
# SDRconnect frames carry no sequence number, so upstream drops only show up as
# fewer samples than wall-clock time implies. Frames arrive ~50 ms apart with
# jitter well under this, so a larger deficit is a real gap, not scheduling noise.
_PACE_TOLERANCE_S = 0.5

# Receiver properties whose changes are recorded as segment sidecar events
# rather than closing the segment (only sample_rate/center_hz do that).
# Maps SDRconnect property name -> sidecar settings key.
_TRACKED_SETTINGS = {
    "lna_state": "lna_state",
    "active_antenna": "active_antenna",
    "device_vfo_frequency": "vfo_hz",
    "demodulator": "demodulator",
    "filter_bandwidth": "filter_bandwidth",
}

_RECORD_PRESSED = "__record_pressed__"

# status()["data_ok"]: a frame must have arrived within this long ago while
# armed/recording, else the UI shows "no samples" (plan U1 item 4).
_DATA_OK_WINDOW_S = 2.0
_METRIC_WINDOW_S = 5.0


class RecorderError(Exception):
    """User-facing recorder error (e.g. low disk, radio not started)."""


@dataclass(frozen=True)
class RecorderConfig:
    root: Path  # uses root/"preroll" and root/"recordings"
    preroll_s: float = 300
    min_free_gb: float = 20
    max_recording_s: float = 3600
    segment_s: float = 10
    host: str = "127.0.0.1"
    port: int = 5454


class Recorder:
    def __init__(
        self,
        config: RecorderConfig,
        stream: "IQStream | None" = None,
        on_status: Callable[[dict], Awaitable[None] | None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._stream = stream if stream is not None else IQStream(config.host, config.port)
        self._on_status = on_status
        self._clock = clock
        # Wall clock stamps segments and metadata; durations (recording_s,
        # max_recording_s) use this one so an NTP step can't stretch or
        # shorten a recording.
        self._monotonic = monotonic

        self._state: _State = "off"
        self._armed = False
        self._connected = False
        self._started = False
        self._sample_rate: int | None = None
        self._center_hz: int | None = None
        self._settings: dict[str, object] = {}

        self._preroll_ring = SegmentRing(config.root / "preroll")
        self._recording_ring: SegmentRing | None = None
        self._active_ring = self._preroll_ring
        self._ring_lock = asyncio.Lock()
        self._arm_lock = asyncio.Lock()

        self._queue: asyncio.Queue[IQFrame] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._gap_pending_samples = 0
        self._pace_t0: float | None = None
        self._pace_rate: int | None = None
        self._pace_samples = 0
        self._reader_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None

        self._recording_dir: Path | None = None
        self._finalizing_dir: Path | None = None
        self._record_started_at: float | None = None
        self._record_started_mono: float | None = None
        self._last_recording: Path | None = None
        self._error: str | None = None

        # data_ok/frames/dropped (plan U1 item 4): reset on each arm() so a
        # session's counters don't carry over stale drops from a previous run.
        self._frame_count = 0
        self._dropped_count = 0
        self._dropped_samples = 0
        self._last_frame_at: float | None = None
        # finalize_progress: segments merged so far / total segments across
        # every run of the recording currently being finalized.
        self._finalize_total = 0
        self._finalize_done = 0
        self._capture_generation = 0
        self._expected_stream_generation: int | None = None
        self._metric_frames: deque[tuple[float, int]] = deque()
        self._metric_writes: deque[tuple[float, int]] = deque()
        self._bytes_written = 0
        self._finalize_reserve_bytes = 0
        self._recoverable_cache: list[dict] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # No disk I/O here: the root may be an unmounted external drive, and
        # that must not stop msdr from starting. Folders are prepared on arm.
        self._error = None
        # Best-effort background scan keeps an unavailable external volume
        # from delaying application startup.
        spawn(self._scan_recoverable_recordings())

    async def _scan_recoverable_recordings(self) -> None:
        try:
            previous = self._recoverable_cache
            self._recoverable_cache = await asyncio.to_thread(self.recoverable_recordings)
            if self._recoverable_cache != previous:
                self._notify()
        except Exception:
            logger.debug("could not scan interrupted recordings", exc_info=True)

    def recoverable_recordings(self) -> list[dict]:
        """List directories with closed IQ segments that still need repair."""
        root = self.config.root / "recordings"
        try:
            dirs = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        candidates = []
        for directory in dirs:
            if (
                not directory.is_dir()
                or directory == self._recording_dir
                or directory == self._finalizing_dir
            ):
                continue
            segments = SegmentRing(directory / "segments").closed_segments()
            finalized = False
            if (directory / "recording.json").exists():
                with contextlib.suppress(Exception):
                    self._verify_recording(directory)
                    finalized = True
            if segments and not finalized:
                candidates.append({
                    "name": directory.name,
                    "segments": len(segments),
                    "samples": sum(s.samples for s in segments),
                    "bytes": sum(s.path.stat().st_size for s in segments if s.path.exists()),
                })
        self._recoverable_cache = candidates
        return candidates

    async def repair_recording(self, name: str) -> dict:
        """Finish an interrupted recording and verify its SigMF outputs."""
        root = (self.config.root / "recordings").resolve()
        directory = (root / name).resolve()
        if directory.parent != root or not directory.is_dir():
            raise RecorderError("recording not found")
        if directory == self._recording_dir or self._state in ("recording", "finalizing"):
            raise RecorderError("stop recording before repairing interrupted recordings")
        if (directory / "recording.json").exists():
            try:
                self._verify_recording(directory)
            except Exception:
                pass
            else:
                raise RecorderError("recording is already finalized")
        segments = SegmentRing(directory / "segments").closed_segments()
        if not segments:
            raise RecorderError("no recoverable segments")
        self._error = None
        self._finalizing_dir = directory
        self._set_state("finalizing")
        await self._finalize(directory, None, self._clock(), recovered=True)
        if self._error:
            raise RecorderError(self._error)
        self._verify_recording(directory)
        return {"name": directory.name, "verified": True, "recording": str(directory)}

    async def stop(self) -> None:
        if self._state == "recording":
            await self.stop_recording()
        # Give a backgrounded finalize a chance to finish cleanly rather than
        # leaving segments mid-merge; bounded so shutdown can't hang forever.
        for _ in range(100):
            if self._state != "finalizing":
                break
            await asyncio.sleep(0.05)
        if self._state != "off":
            async with self._arm_lock:
                await self._disarm()

    # -- control -------------------------------------------------------------

    async def arm(self, on: bool) -> dict:
        # Everything up to the first await is check-then-act on self._state;
        # two concurrent arms would both pass it and the second would orphan
        # the first's reader/writer tasks.
        async with self._arm_lock:
            return await self._arm_locked(on)

    async def _arm_locked(self, on: bool) -> dict:
        if on:
            if self._state == "armed":
                return self.status()
            if self._state not in ("off", "error"):
                raise RecorderError(f"cannot arm while {self._state}")
            if not self._connected:
                raise RecorderError("radio not connected")
            if not self._started:
                raise RecorderError("radio not started")
            try:
                self.config.root.mkdir(parents=True, exist_ok=True)
                self._preroll_ring.clear()  # stale preroll is disposable
            except OSError as exc:
                raise RecorderError(
                    f"recording folder unavailable: {self.config.root} ({exc.strerror})"
                ) from exc
            free_gb = self._free_gb()
            if free_gb < self.config.min_free_gb:
                raise RecorderError(
                    f"low disk: {free_gb:.1f} GB free, need {self.config.min_free_gb:.0f} GB"
                )
            self._clear_frame_queue()
            self._capture_generation += 1
            await self._stream.start()
            self._expected_stream_generation = getattr(self._stream, "generation", None)
            await self._stream.enable(True)
            self._pace_t0 = None
            self._armed = True
            self._error = None
            self._frame_count = 0
            self._dropped_count = 0
            self._dropped_samples = 0
            self._last_frame_at = None
            self._metric_frames.clear()
            self._metric_writes.clear()
            self._bytes_written = 0
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._writer_task = asyncio.create_task(self._writer_loop())
            self._set_state("armed")
        else:
            if self._state == "off":
                return self.status()
            if self._state in ("recording", "finalizing"):
                raise RecorderError(f"cannot disarm while {self._state}")
            await self._disarm()
        return self.status()

    async def record(self) -> dict:
        if self._state != "armed":
            raise RecorderError("not armed")
        if self._sample_rate is None or self._center_hz is None:
            raise RecorderError("radio settings not yet known")

        ts = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d_%H-%M-%S_%f")
        unique_id = uuid.uuid4().hex[:8]
        recording_dir = (
            self.config.root / "recordings" / f"{ts}_{self._center_hz}Hz_{unique_id}"
        )
        segments_dir = recording_dir / "segments"

        async with self._ring_lock:
            self._finish_segment(self._preroll_ring)
            # Exclusive creation is the final guard against collisions across
            # clock rollback, parallel recorders, or repeated timestamps.
            self._recording_dir = recording_dir
            try:
                recording_dir.mkdir(parents=True, exist_ok=False)
                segments_dir.mkdir(exist_ok=False)
                for meta in self._preroll_ring.closed_segments():
                    meta.path.rename(segments_dir / meta.path.name)
                    meta.meta_path.rename(segments_dir / meta.meta_path.name)
                self._recording_ring = SegmentRing(segments_dir)
                self._active_ring = self._recording_ring
                self._recording_ring.open_segment(
                    self._sample_rate, self._center_hz, dict(self._settings), now=self._clock()
                )
                # Marks the live/pre-roll boundary for the REC-pressed annotation.
                self._recording_ring.record_event(_RECORD_PRESSED, True)
            except Exception:
                if self._recording_ring is not None:
                    self._finish_segment(self._recording_ring)
                self._recording_ring = None
                self._active_ring = self._preroll_ring
                self._recording_dir = None
                raise

        self._record_started_at = self._clock()
        self._record_started_mono = self._monotonic()
        self._set_state("recording")
        return self.status()

    async def stop_recording(self) -> dict:
        if self._state != "recording":
            raise RecorderError("not recording")
        async with self._ring_lock:
            self._finish_segment(self._recording_ring)
            self._active_ring = self._preroll_ring  # resume buffering pre-roll immediately

        recording_dir = self._recording_dir
        end_wall = self._clock()
        start_wall = self._record_started_at
        self._finalizing_dir = recording_dir
        self._recording_dir = None
        self._recording_ring = None
        self._record_started_at = None
        self._record_started_mono = None
        self._set_state("finalizing")
        spawn(self._finalize(recording_dir, start_wall, end_wall))
        return self.status()

    def on_property(self, prop: str, value: object) -> None:
        if prop == "connected":
            self._connected = bool(value)
            if not value:
                self._auto_disarm("radio disconnected")
            return
        if prop == "started":
            self._started = bool(value)
            if not value:
                self._auto_disarm("radio stopped")
            return
        if prop == "device_sample_rate":
            with contextlib.suppress(TypeError, ValueError):
                self._sample_rate = int(value)
            return
        if prop == "device_center_frequency":
            with contextlib.suppress(TypeError, ValueError):
                self._center_hz = int(value)
            return
        key = _TRACKED_SETTINGS.get(prop)
        if key is not None:
            self._settings[key] = value
            ring = self._active_ring
            if self._state in ("armed", "recording") and ring.current is not None:
                ring.record_event(key, value)

    def status(self) -> dict:
        recording_s = 0.0
        if self._state in ("recording", "finalizing") and self._record_started_mono is not None:
            recording_s = self._monotonic() - self._record_started_mono
        data_ok = (
            self._state in ("armed", "recording")
            and self._last_frame_at is not None
            and (self._clock() - self._last_frame_at) <= _DATA_OK_WINDOW_S
        )
        finalize_progress = None
        if self._state == "finalizing" and self._finalize_total:
            finalize_progress = self._finalize_done / self._finalize_total
        now_mono = time.monotonic()
        self._prune_metrics(now_mono)
        frame_bytes = sum(n for _, n in self._metric_frames)
        write_bytes = sum(n for _, n in self._metric_writes)
        frame_span = max(1.0, now_mono - self._metric_frames[0][0]) if self._metric_frames else 1.0
        write_span = max(1.0, now_mono - self._metric_writes[0][0]) if self._metric_writes else 1.0
        iq_bps = frame_bytes / frame_span if self._metric_frames else 0.0
        disk_bps = write_bytes / write_span if self._metric_writes else 0.0
        stream_backlog = getattr(self._stream, "backlog_frames", 0)
        stream_dropped_samples = getattr(self._stream, "dropped_samples", 0)
        frame_duration = (
            (sum(n for _, n in self._metric_frames) / len(self._metric_frames))
            / (4 * self._sample_rate)
            if self._metric_frames and self._sample_rate
            else 0.0
        )
        queued_s = self._queue.qsize() * frame_duration
        backlog_s = queued_s + stream_backlog * frame_duration
        recording_remaining = max(0.0, self.config.max_recording_s - recording_s)
        merge_headroom = self._pending_iq_bytes()
        writable_remaining = (
            max(
                0.0,
                self._free_gb() * 1e9
                - self.config.min_free_gb * 1e9
                - merge_headroom,
            ) / (2 * disk_bps)
            if disk_bps > 0 else recording_remaining
        )
        if self._state == "finalizing":
            writable_remaining = 0.0
        return {
            "state": self._state,
            "armed": self._armed,
            "buffered_s": self._preroll_ring.buffered_s(),
            "recording_s": recording_s,
            "preroll_s": self.config.preroll_s,
            "sample_rate": self._sample_rate,
            "center_hz": self._center_hz,
            "free_gb": self._free_gb(),
            "min_free_gb": self.config.min_free_gb,
            "root": str(self.config.root),
            "last_recording": str(self._last_recording) if self._last_recording else None,
            "error": self._error,
            "data_ok": data_ok,
            "frames": self._frame_count,
            "dropped": self._dropped_count,
            "finalize_progress": finalize_progress,
            "recoverable_count": len(self._recoverable_cache),
            "capture_health": {
                "iq_throughput_bps": iq_bps,
                "dropped_samples": self._dropped_samples + stream_dropped_samples,
                "backlog_s": backlog_s,
                "disk_write_bps": disk_bps,
                "remaining_recording_s": min(recording_remaining, writable_remaining),
            },
        }

    # -- internals: frame pipeline -------------------------------------------

    async def _reader_loop(self) -> None:
        capture_generation = self._capture_generation
        try:
            async for frame in self._stream.frames():
                if capture_generation != self._capture_generation:
                    break
                if (self._expected_stream_generation is not None
                        and frame.session_generation != self._expected_stream_generation):
                    self._dropped_count += 1
                    self._dropped_samples += len(frame.data) // 4
                    continue
                self._frame_count += 1
                self._last_frame_at = frame.received_at
                self._check_pace(frame)
                self._metric_frames.append((time.monotonic(), len(frame.data)))
                try:
                    self._queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Never block the reader -- drop the frame and note the
                    # gap; the writer folds it into the current segment.
                    self._gap_pending_samples += len(frame.data) // 4
                    self._dropped_count += 1
                    self._dropped_samples += len(frame.data) // 4
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("IQ reader loop failed")
            # Without this the recorder sits in armed/recording with no frames
            # and max_recording_s (checked only on rotation) never trips.
            # _enter_error cancels and awaits the reader task, so it must run
            # outside this task (same shape as _auto_disarm).
            spawn(self._enter_error("IQ stream failed"))

    def _check_pace(self, frame: IQFrame) -> None:
        rate = self._sample_rate
        if rate is None:
            self._pace_t0 = None
            return
        if self._pace_t0 is None or self._pace_rate != rate:
            # (Re)start the baseline: stream just enabled or the rate changed.
            self._pace_t0, self._pace_rate, self._pace_samples = frame.received_at, rate, 0
        else:
            deficit = (frame.received_at - self._pace_t0) * rate - self._pace_samples
            if deficit > rate * _PACE_TOLERANCE_S:
                self._gap_pending_samples += int(deficit)
                self._dropped_count += 1
                self._dropped_samples += int(deficit)
                self._pace_t0, self._pace_samples = frame.received_at, 0
        self._pace_samples += len(frame.data) // 4

    async def _writer_loop(self) -> None:
        try:
            while True:
                frame = await self._queue.get()
                if not self._disk_reserve_ok(
                    self._finalize_reserve_bytes + len(frame.data)
                ):
                    self._error = "low disk"
                    if self._state == "recording":
                        # stop_recording() alone leaves "armed" with the
                        # reader running and this writer dead, and a
                        # successful finalize clears the error. Disarm fully
                        # (stops the recording first, sets the error last).
                        self._auto_disarm("low disk")
                    else:
                        await self._enter_error("low disk")
                    return
                async with self._ring_lock:
                    rotated = await self._process_frame_locked(frame)
                if rotated and self._state == "recording":
                    await self._check_recording_limits()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("recorder writer loop failed")
            await self._enter_error(str(exc))

    async def _process_frame_locked(self, frame: IQFrame) -> bool:
        if self._sample_rate is None or self._center_hz is None:
            return False  # can't label a segment yet; drop until settings are known
        ring = self._active_ring
        cur = ring.current
        now = self._clock()
        rotated = False
        if (
            cur is None
            or cur.sample_rate != self._sample_rate
            or cur.center_hz != self._center_hz
            or (now - cur.start_wall) >= self.config.segment_s
        ):
            self._finish_segment(ring)
            ring.open_segment(self._sample_rate, self._center_hz, dict(self._settings), now=now)
            rotated = True
        if self._gap_pending_samples:
            ring.record_gap(self._gap_pending_samples)
            self._gap_pending_samples = 0
        await asyncio.to_thread(ring.write, frame.data)
        self._metric_writes.append((time.monotonic(), len(frame.data)))
        self._bytes_written += len(frame.data)
        return rotated

    def _finish_segment(self, ring: SegmentRing) -> None:
        if self._gap_pending_samples and ring.current is not None:
            ring.record_gap(self._gap_pending_samples)
            self._gap_pending_samples = 0
        closed = ring.close_segment()
        if closed is not None and ring is self._preroll_ring and self._state == "armed":
            ring.evict(self.config.preroll_s)

    async def _check_recording_limits(self) -> None:
        if self._state != "recording" or self._record_started_mono is None:
            return
        elapsed = self._monotonic() - self._record_started_mono
        if elapsed >= self.config.max_recording_s:
            await self.stop_recording()
        elif not self._disk_reserve_ok():
            self._error = "low disk"
            await self.stop_recording()

    # -- internals: arm/disarm -------------------------------------------

    def _auto_disarm(self, reason: str) -> None:
        # on_property is sync (called from the host app's event handling), so
        # the actual async teardown runs as a background task.
        if self._state == "off":
            return
        spawn(self._auto_disarm_async(reason))

    async def _auto_disarm_async(self, reason: str) -> None:
        if self._state == "recording":
            with contextlib.suppress(RecorderError):
                await self.stop_recording()
            # stop_recording() only starts finalize as a background task;
            # wait for it so the following check sees the post-finalize
            # state ("armed"/"off") rather than "finalizing" and skipping
            # teardown, which would leave the recorder armed with the
            # stream still enabled after the radio itself stopped.
            for _ in range(100):
                if self._state != "finalizing":
                    break
                await asyncio.sleep(0.05)
        # _disarm yields (task joins, stream stop) while _state is still
        # "armed"/"error"; an arm(True) interleaving there would create new
        # reader/writer tasks that the resuming _disarm then clobbers and
        # orphans. Take the same lock arm() uses so the two serialize.
        async with self._arm_lock:
            if self._state != "off":
                await self._disarm()
        self._error = reason
        self._notify()

    async def _enter_error(self, message: str) -> None:
        self._capture_generation += 1
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        with contextlib.suppress(Exception):
            await self._stream.enable(False)
            await self._stream.stop()
        async with self._ring_lock:
            # Close out whatever segment was open so its sidecar meta is
            # valid; already-closed segments are left untouched on disk.
            self._finish_segment(self._active_ring)
            self._active_ring = self._preroll_ring
            self._clear_frame_queue()
        self._error = message
        self._set_state("error")

    async def _disarm(self) -> None:
        self._armed = False
        self._capture_generation += 1
        self._error = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer_task is not None:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
            self._writer_task = None
        with contextlib.suppress(Exception):
            await self._stream.enable(False)
            await self._stream.stop()
        async with self._ring_lock:
            # Close out whatever segment was open so its sidecar meta is
            # valid and the file handle doesn't linger until GC; _finish_segment
            # is a no-op when nothing is open, so plain stop() after an
            # already-finished segment is safe to call again.
            self._finish_segment(self._active_ring)
            self._clear_frame_queue()
        self._set_state("off")

    # -- internals: finalize -------------------------------------------

    async def _finalize(
        self, recording_dir: Path, start_wall: float | None, end_wall: float,
        recovered: bool = False,
    ) -> None:
        try:
            segments_dir = recording_dir / "segments"
            segs = SegmentRing(segments_dir).recover_open_segments()
            if not segs:
                raise RecorderError("no segments captured")

            # A previous process may have stopped during a merge. The segment
            # files are the source of truth, so discard reproducible outputs.
            for pattern in ("run-*.sigmf-data", "run-*.sigmf-meta"):
                for path in recording_dir.glob(pattern):
                    path.unlink(missing_ok=True)

            runs: list[list] = [[segs[0]]]
            for m in segs[1:]:
                last = runs[-1][-1]
                if m.sample_rate == last.sample_rate and m.center_hz == last.center_hz:
                    runs[-1].append(m)
                else:
                    runs.append([m])

            self._finalize_total = len(segs)
            self._finalize_done = 0
            # Keep every source segment until the entire index is verified.
            # This makes retries safe even if a later run fails to merge.
            self._finalize_reserve_bytes = sum(m.path.stat().st_size for m in segs)
            run_infos = []
            for idx, run_segs in enumerate(runs, start=1):
                if not self._disk_reserve_ok(self._finalize_reserve_bytes):
                    raise RecorderError(
                        "low disk to merge segments: "
                        f"need {self._finalize_reserve_bytes / 1e9:.2f} GB plus "
                        f"{self.config.min_free_gb:.2f} GB reserve"
                    )
                run_infos.append(await self._finalize_run(recording_dir, idx, run_segs))
                self._finalize_done += len(run_segs)
                self._notify()  # progress tick -- state itself is unchanged

            index = {
                "start_wall": start_wall,
                "end_wall": end_wall,
                "preroll_s": self.config.preroll_s,
                "runs": run_infos,
                "recovered": recovered,
            }
            (recording_dir / "recording.json").write_text(json.dumps(index, indent=2))
            self._verify_recording(recording_dir)
            for segment in segs:
                with contextlib.suppress(OSError):
                    segment.path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    segment.meta_path.unlink(missing_ok=True)
            self._last_recording = recording_dir
            self._error = None
        except Exception as exc:
            # Never delete unmerged data on error -- segments stay on disk.
            with contextlib.suppress(OSError):
                (recording_dir / "recording.json").unlink(missing_ok=True)
            logger.exception("finalize failed for %s", recording_dir)
            self._error = f"finalize failed: {exc}"
        finally:
            self._finalize_total = 0
            self._finalize_done = 0
            self._finalize_reserve_bytes = 0
            self._finalizing_dir = None
            with contextlib.suppress(Exception):
                self._recoverable_cache = await asyncio.to_thread(self.recoverable_recordings)
            self._set_state("armed" if self._armed else "off")

    async def _finalize_run(self, recording_dir: Path, idx: int, run_segs: list) -> dict:
        sample_rate = run_segs[0].sample_rate
        center_hz = run_segs[0].center_hz
        stem = f"run-{idx:02d}_{sample_rate}SPS_{center_hz}Hz"
        data_path = recording_dir / f"{stem}.sigmf-data"
        meta_path = recording_dir / f"{stem}.sigmf-meta"

        annotations = []
        offset = 0
        for m in run_segs:
            for ev in m.events:
                if ev["property"] == _RECORD_PRESSED:
                    annotations.append(
                        annotation(offset + ev["sample"], "REC", "recording started")
                    )
                else:
                    annotations.append(
                        annotation(offset + ev["sample"], ev["property"], f"= {ev['value']}")
                    )
            for gap in m.gaps:
                annotations.append(
                    annotation(
                        offset + gap["sample"], "gap", f"missing ~{gap['missing_estimate']} samples"
                    )
                )
            offset += m.samples

        await asyncio.to_thread(
            _concat_segments, run_segs, data_path, self._consume_finalize_reserve
        )
        if data_path.stat().st_size != sum(m.path.stat().st_size for m in run_segs):
            raise RecorderError(f"merged IQ verification failed for {data_path.name}")

        meta = build_meta(
            sample_rate=sample_rate,
            center_hz=center_hz,
            start_wall=run_segs[0].start_wall,
            annotations=annotations,
        )
        meta_path.write_text(json.dumps(meta, indent=2))

        return {
            "data": data_path.name,
            "meta": meta_path.name,
            "sample_rate": sample_rate,
            "center_hz": center_hz,
            "samples": offset,
            "start_wall": run_segs[0].start_wall,
            "settings": run_segs[0].settings,
        }

    # -- misc -------------------------------------------

    def _free_gb(self) -> float:
        try:
            return shutil.disk_usage(self.config.root).free / 1e9
        except OSError:
            return 0.0

    def _disk_reserve_ok(self, required_extra_bytes: int = 0) -> bool:
        try:
            free = shutil.disk_usage(self.config.root).free
        except OSError:
            return False
        return free - required_extra_bytes >= self.config.min_free_gb * 1e9

    def _pending_iq_bytes(self) -> int:
        if self._recording_dir is not None:
            directory = self._recording_dir / "segments"
        elif self._finalizing_dir is not None:
            directory = self._finalizing_dir / "segments"
        else:
            directory = self._preroll_ring.directory
        try:
            return sum(path.stat().st_size for path in directory.glob("*.cs16"))
        except OSError:
            return 0

    def _consume_finalize_reserve(self, bytes_written: int) -> None:
        self._finalize_reserve_bytes = max(0, self._finalize_reserve_bytes - bytes_written)
        if not self._disk_reserve_ok(self._finalize_reserve_bytes):
            raise RecorderError("low disk while merging segments")

    def _clear_frame_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _prune_metrics(self, now: float) -> None:
        cutoff = now - _METRIC_WINDOW_S
        while self._metric_frames and self._metric_frames[0][0] < cutoff:
            self._metric_frames.popleft()
        while self._metric_writes and self._metric_writes[0][0] < cutoff:
            self._metric_writes.popleft()

    @staticmethod
    def _verify_recording(directory: Path) -> None:
        """Check the index, SigMF metadata, and IQ byte counts agree."""
        index = json.loads((directory / "recording.json").read_text())
        if not index.get("runs"):
            raise RecorderError("recording verification failed: no runs")
        for run in index["runs"]:
            data_path = directory / run["data"]
            meta_path = directory / run["meta"]
            meta = json.loads(meta_path.read_text())
            if not data_path.is_file() or not meta_path.is_file():
                raise RecorderError("recording verification failed: missing SigMF output")
            if data_path.stat().st_size != int(run["samples"]) * 4:
                raise RecorderError("recording verification failed: IQ sample count mismatch")
            if not meta.get("global", {}).get("core:datatype") == "ci16_le":
                raise RecorderError("recording verification failed: unexpected IQ datatype")

    def _set_state(self, state: str) -> None:
        self._state = state
        self._notify()

    def _notify(self) -> None:
        if self._on_status is None:
            return
        result = self._on_status(self.status())
        if inspect.isawaitable(result):
            spawn(result)


def _concat_segments(
    run_segs: list,
    data_path: Path,
    on_chunk_written: Callable[[int], None] | None = None,
) -> None:
    with open(data_path, "wb") as out:
        for m in run_segs:
            with open(m.path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    out.write(chunk)
                    out.flush()
                    if on_chunk_written is not None:
                        on_chunk_written(len(chunk))
