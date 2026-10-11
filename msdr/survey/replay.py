"""Survey replay: an IQ recording played back as if it were the live radio.

The survey has two live inputs from SDRconnect -- the spectrum stream (512
uint8 bins, drives occupancy/peak tracking/waterfall) and the IQ stream
(drives the analyzer and RDS). ReplaySource stands in for both from a file:
it reads the recording in real time (loop optional), hands the raw IQ to
ReplayIQStream consumers (the same start/stop/enable/frames() interface as
msdr.recorder.iq_stream.IQStream) and computes the spectrum frames itself.

Files: SDRconnect IQ WAVs (RIFF or RF64, 16-bit stereo I/Q; the centre
frequency is only in the name, "..._13000000HZ.wav") and msdr's own SigMF
recordings (ci16_le). A recording directory is a RecordingTimeline of runs
(msdr.source.timeline); runs at another centre/rate are played, not skipped,
and every run change is a SourceEvent + generation bump.

Clock (msdr.contracts.source): the run-local sample index is the replay
clock; wall-clock only paces. Nothing is skipped to catch up -- when the
host is slow the pump keeps emitting back-to-back and status()["behind_s"]
reports the lag.
"""

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from msdr.contracts.source import Discontinuity, DiscontinuityKind, SourceDescriptor
from msdr.radio.protocol import SpectrumFrame
from msdr.recorder.iq_stream import IQFrame
from msdr.source.base import ListenerSet, SourceEvent, SourceListener
from msdr.source.timeline import RecordingError, RecordingTimeline, TimelineRun

logger = logging.getLogger(__name__)

_BINS = 512
# Spectrum frames per second -- SDRconnect's stream measured ~23 fps.
_SPECTRUM_FPS = 20
# FFT segments averaged per spectrum frame (512-point each).
_SPECTRUM_AVERAGES = 32
_QUEUE_MAX = 50
# An enabled IQ consumer that hasn't taken a frame for this long is treated
# as stalled: its oldest frame is dropped (and counted) so one dead consumer
# can't freeze the replay for everyone.
_STALL_S = 2.0
# Discontinuities kept on the descriptor (the log would otherwise grow
# without bound on a looping replay).
_DISCONTINUITY_LOG = 32

# Kept for callers that catch it (survey.start_replay).
ReplayError = RecordingError


class IQFile:
    """A recording's timeline + memory-mapped int16 I/Q per run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.timeline = RecordingTimeline.from_path(self.path)
        self._maps: list[np.ndarray | None] = []
        for run in self.timeline.runs:
            if run.samples:
                shape = (run.samples * 2,)
                self._maps.append(
                    np.memmap(run.path, dtype="<i2", mode="r", offset=run.data_offset, shape=shape)
                )
            else:
                self._maps.append(None)
        if not self.total_samples:
            raise RecordingError(f"recording is empty: {self.path.name}")
        first = self.first_run
        self.center_hz = first.center_hz
        self.sample_rate_hz = first.sample_rate_hz

    @property
    def first_run(self) -> TimelineRun:
        return next(r for r in self.timeline.runs if r.samples)

    @property
    def total_samples(self) -> int:
        return self.timeline.total_samples

    @property
    def duration_s(self) -> float:
        return self.timeline.total_elapsed_s

    def read(self, *args: int) -> np.ndarray:
        """``read(run_index, start, count)``: up to `count` complex samples
        (interleaved int16) of one run, shorter at its end.

        Legacy ``read(start, count)``: global sample index across all runs
        in order (single-run files: exactly the old behaviour)."""
        if len(args) == 3:
            return self._read_run(*args)
        if len(args) == 2:
            return self._read_global(*args)
        raise TypeError("read(run_index, start, count) or read(start, count)")

    def _read_run(self, run_index: int, start: int, count: int) -> np.ndarray:
        data = self._maps[run_index]
        n = self.timeline.runs[run_index].samples
        if data is None or start >= n or count <= 0:
            return np.zeros(0, dtype="<i2")
        take = min(count, n - start)
        return np.asarray(data[start * 2 : (start + take) * 2])

    def _read_global(self, start: int, count: int) -> np.ndarray:
        out = []
        for i, run in enumerate(self.timeline.runs):
            if start >= run.samples:
                start -= run.samples
                continue
            chunk = self._read_run(i, start, count)
            out.append(chunk)
            count -= chunk.size // 2
            start = 0
            if count <= 0:
                break
        return np.concatenate(out) if out else np.zeros(0, dtype="<i2")


def spectrum_db(iq16: np.ndarray, averages: int = _SPECTRUM_AVERAGES) -> np.ndarray:
    """512-bin averaged power spectrum (dBFS, DC centred) of interleaved int16
    I/Q -- bin i <-> centre - rate/2 + (i + 0.5) * rate/512, like SDRconnect."""
    n = min(averages, iq16.size // (2 * _BINS))
    if n == 0:
        return np.full(_BINS, -150.0)
    x = iq16[: n * _BINS * 2].astype(np.float32) / 32768.0
    z = (x[0::2] + 1j * x[1::2]).reshape(n, _BINS)
    window = np.hanning(_BINS).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(z * window, axis=1), axes=1)
    power = np.mean(np.abs(spec) ** 2, axis=0) / (_BINS * float(np.sum(window**2)))
    return 10.0 * np.log10(power + 1e-20)


class ReplayIQStream:
    """IQStream stand-in fed by a ReplaySource (see module doc).

    Unlike the live stream it applies backpressure: the replay waits for a
    full queue to drain instead of dropping, so a slow consumer slows the
    replay (visible as behind_s) rather than losing samples. Only a consumer
    stalled for _STALL_S loses frames, counted in `dropped`.
    """

    def __init__(self, source: "ReplaySource") -> None:
        self._source = source
        self._queue: asyncio.Queue[IQFrame] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._enabled = False
        self.dropped = 0

    async def start(self) -> None:
        self._source._iq_streams.add(self)

    async def stop(self) -> None:
        self._source._iq_streams.discard(self)

    async def enable(self, on: bool) -> None:
        self._enabled = on

    async def _put(self, frame: IQFrame) -> None:
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self._queue.put(frame), _STALL_S)
        except TimeoutError:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(frame)
            if not self.dropped:
                logger.warning("replay IQ consumer stalled for %.0f s; dropping", _STALL_S)
            self.dropped += 1

    async def frames(self):
        while True:
            yield await self._queue.get()


class ReplaySource:
    """Plays an IQFile in real time to spectrum queues and ReplayIQStreams.

    One asyncio task (`_run`) owns the clock. seek() from another coroutine
    only records a pending target; the pump applies it between blocks, so a
    generation bump is never followed by a frame of the old position.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        loop: bool = True,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.file = IQFile(path)
        self.timeline = self.file.timeline
        self.source_id = f"replay:{self.file.path.name}"
        self.loop = loop
        self._playing = True
        self._run_index = self.file.first_run.index
        self._sample_index = 0  # run-local clock
        self._gap_cursor = 0  # next interior gap in this run
        self.generation = 0
        self._discontinuities: list[Discontinuity] = []
        self._pending_seek: float | None = None
        self._behind_s = 0.0
        self._listeners = ListenerSet()
        self._wake = asyncio.Event()
        self._clock = clock
        self._sleep = sleep
        self._spectrum_queues: set[asyncio.Queue] = set()
        self._iq_streams: set[ReplayIQStream] = set()
        self._task: asyncio.Task | None = None
        # Fixed display levels for the whole replay, from the file's opening
        # second: SDRconnect's ref/base play the same role (the survey maps
        # uint8 bins back to dB with them).
        first_run = self.file.first_run
        first = spectrum_db(
            self.file.read(first_run.index, 0, first_run.sample_rate_hz), averages=256
        )
        floor_db = float(np.median(first))
        self.base_db = round(floor_db - 15.0)
        self.ref_level_db = round(max(float(first.max()) + 5.0, self.base_db + 60.0))

    # -- clock / descriptor ------------------------------------------------

    @property
    def run(self) -> TimelineRun:
        return self.timeline.runs[self._run_index]

    @property
    def run_index(self) -> int:
        return self._run_index

    @property
    def sample_index(self) -> int:
        return self._sample_index

    @property
    def center_hz(self) -> int:
        return self.run.center_hz

    @property
    def sample_rate_hz(self) -> int:
        return self.run.sample_rate_hz

    @property
    def _block(self) -> int:
        return max(_BINS, int(self.run.sample_rate_hz / _SPECTRUM_FPS))

    @property
    def position_s(self) -> float:
        if self._pending_seek is not None:
            return self.timeline.elapsed_s(*self.timeline.locate(self._pending_seek))
        return self.timeline.elapsed_s(self._run_index, self._sample_index)

    @property
    def playing(self) -> bool:
        return self._playing

    @playing.setter
    def playing(self, on: bool) -> None:
        on = bool(on)
        if on == self._playing:
            return
        self._playing = on
        self._wake.set()
        self._notify("position")

    def descriptor(self) -> SourceDescriptor:
        run = self.run
        return SourceDescriptor(
            source_id=self.source_id,
            mode="replay",
            sample_rate_hz=run.sample_rate_hz,
            center_hz=run.center_hz,
            capture_time=(
                datetime.fromtimestamp(run.start_wall, tz=UTC)
                if run.start_wall is not None
                else None
            ),
            origin=str(self.file.path),
            sample_index=self._sample_index,
            generation=self.generation,
            # The run's length: sample_index is run-local.
            total_samples=run.samples,
            discontinuities=list(self._discontinuities),
        )

    def add_listener(self, cb: SourceListener) -> None:
        self._listeners.add(cb)

    def remove_listener(self, cb: SourceListener) -> None:
        self._listeners.remove(cb)

    def _notify(self, kind: str, disc: Discontinuity | None = None) -> None:
        self._listeners.notify(
            SourceEvent(
                kind=kind,  # type: ignore[arg-type]
                generation=self.generation,
                sample_index=self._sample_index,
                run_index=self._run_index,
                descriptor=self.descriptor(),
                discontinuity=disc,
            )
        )

    def _bump(
        self, kind: DiscontinuityKind, *, run_changed: bool, gap_samples=None, note: str = ""
    ) -> None:
        """New generation at the *current* (already moved) position, then
        tell listeners: "run" first when the run changed, then "generation"."""
        self.generation += 1
        disc = Discontinuity(
            kind=kind,
            at_sample=self._sample_index,
            generation=self.generation,
            gap_samples=gap_samples,
            note=note,
        )
        self._discontinuities = [*self._discontinuities, disc][-_DISCONTINUITY_LOG:]
        if run_changed:
            self._notify("run", disc)
        self._notify("generation", disc)

    # -- controls --------------------------------------------------------------

    def seek(self, position_s: float) -> None:
        """Move to timeline elapsed `position_s` (clamped to the recording)."""
        if self._task is None or self._task.done():
            self._apply_seek(position_s)
        else:
            self._pending_seek = float(position_s)
            self._wake.set()

    def _apply_seek(self, position_s: float) -> None:
        run_index, local = self.timeline.locate(position_s)
        old = self.run
        new = self.timeline.runs[run_index]
        run_changed = run_index != old.index
        self._run_index, self._sample_index = run_index, local
        # A seek to a gap's post-gap sample already starts a new generation.
        self._gap_cursor = sum(s <= local for s, _ in new.interior_gaps)
        span_changed = (new.center_hz, new.sample_rate_hz) != (old.center_hz, old.sample_rate_hz)
        kind: DiscontinuityKind = "retune" if run_changed and span_changed else "seek"
        self._bump(kind, run_changed=run_changed, note=f"seek to {position_s:.3f} s")

    def _advance(self) -> None:
        """The current run is exhausted: next run, or the end (loop/stop)."""
        runs = self.timeline.runs
        nxt = self._run_index + 1
        while nxt < len(runs) and not runs[nxt].samples:
            nxt += 1
        if nxt < len(runs):
            gap = runs[nxt].gap_before_samples
            self._run_index, self._sample_index = nxt, 0
            self._gap_cursor = 0
            if gap is None:
                self._bump("gap", run_changed=True, note=f"run {nxt}: gap of unknown length")
            elif gap > 0:
                self._bump("gap", run_changed=True, gap_samples=gap, note=f"run {nxt}")
            else:
                self._bump("retune", run_changed=True, note=f"run {nxt}")
            return
        self._notify("end")
        first = self.file.first_run.index
        run_changed = first != self._run_index
        self._run_index, self._sample_index = first, 0
        self._gap_cursor = 0
        if self.loop:
            self._bump("loop", run_changed=run_changed)
        else:
            # Old behaviour kept: rewind and pause, PLAY starts over.
            self._playing = False
            self._bump("seek", run_changed=run_changed, note="rewound at end")

    def status(self) -> dict:
        run = self.run
        return {
            "path": str(self.file.path),
            "name": self.file.path.name,
            "center_hz": self.center_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "duration_s": self.file.duration_s,
            "position_s": self.position_s,
            "playing": self.playing,
            "loop": self.loop,
            "source_id": self.source_id,
            "generation": self.generation,
            "run_index": self._run_index,
            "sample_index": self._sample_index,
            "runs": self.timeline.as_dicts(),
            "capture_start": run.start_wall,
            "behind_s": round(self._behind_s, 3),
            "dropped_frames": sum(s.dropped for s in self._iq_streams),
        }

    # -- streams -------------------------------------------------------------

    def iq_stream(self) -> ReplayIQStream:
        return ReplayIQStream(self)

    def acquire_spectrum(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._spectrum_queues.add(q)
        return q

    def release_spectrum(self, q: asyncio.Queue) -> None:
        self._spectrum_queues.discard(q)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _to_bins(self, iq16: np.ndarray) -> bytes:
        db = spectrum_db(iq16)
        scale = 255.0 / (self.ref_level_db - self.base_db)
        return np.clip((db - self.base_db) * scale, 0, 255).astype(np.uint8).tobytes()

    async def _emit(self, iq16: np.ndarray, generation: int, sample_index: int) -> None:
        # Spectrum is display-only: drop-oldest, never blocks the pump.
        frame = SpectrumFrame(self._to_bins(iq16), time.monotonic())
        for q in self._spectrum_queues:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(frame)
        if self._iq_streams:
            iq_frame = IQFrame(
                iq16.tobytes(), time.time(), generation, sample_index, self._run_index
            )
            for stream in list(self._iq_streams):
                await stream._put(iq_frame)

    async def _wait_gap(self, seconds: float) -> None:
        """Pace a known gap while letting a seek or pause interrupt the wait."""
        if self._pending_seek is not None or not self._playing:
            return
        self._wake.clear()
        sleeper = asyncio.create_task(self._sleep(seconds))
        wake = asyncio.create_task(self._wake.wait())
        try:
            done, _ = await asyncio.wait((sleeper, wake), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            sleeper.cancel()
            wake.cancel()
            await asyncio.gather(sleeper, wake, return_exceptions=True)

    async def _run(self) -> None:
        next_at = self._clock()
        while True:
            if self._pending_seek is not None:
                target, self._pending_seek = self._pending_seek, None
                self._apply_seek(target)
                next_at = self._clock()
            if not self._playing:
                self._behind_s = 0.0
                self._wake.clear()
                if self._pending_seek is None and not self._playing:
                    await self._wake.wait()
                next_at = self._clock()
                continue
            run_index, start, gen = self._run_index, self._sample_index, self.generation
            run = self.timeline.runs[run_index]
            if start >= run.samples:
                self._advance()
                continue
            gaps = run.interior_gaps
            if self._gap_cursor < len(gaps) and start == gaps[self._gap_cursor][0]:
                missing = gaps[self._gap_cursor][1]
                self._gap_cursor += 1
                self._bump(
                    "gap",
                    run_changed=False,
                    gap_samples=missing,
                    note=f"run {run_index}: interior gap",
                )
                next_at += missing / run.sample_rate_hz
                lag = self._clock() - next_at
                self._behind_s = max(0.0, lag)
                if lag < 0:
                    await self._wait_gap(-lag)
                continue
            count = self._block
            if self._gap_cursor < len(gaps):
                count = min(count, gaps[self._gap_cursor][0] - start)
            iq16 = await asyncio.to_thread(self.file.read, run_index, start, count)
            if self._pending_seek is not None or not self._playing:
                continue  # stale read: the seek/pause wins, the clock stays put
            n = iq16.size // 2
            await self._emit(iq16, gen, start)
            self._sample_index = start + n
            if self._sample_index >= run.samples:
                self._advance()
            next_at += n / run.sample_rate_hz
            lag = self._clock() - next_at
            self._behind_s = max(0.0, lag)
            if lag < 0:
                await self._sleep(-lag)
