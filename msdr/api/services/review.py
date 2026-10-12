"""Review: source-independent playback, decode, audio, bookmarks, clips.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import asyncio
import json
import shutil
import time
from pathlib import Path

from fastapi import HTTPException

from msdr.api.services._common import (
    OutOfSpanError,
    ReceiverStopped,
    ReplayDecodeSession,
    RestoreRunning,
    SurveyError,
    VirtualReceiver,
    resolve_binaries,
)
from msdr.observations.models import Observation as BookmarkObservation
from msdr.observations.models import RecordingReference
from msdr.radio.registry import REGISTRY
from msdr.tasks import spawn

# Longest clip POST /api/review/clips accepts: bounds one
# request's disk write and thread time.
_CLIP_MAX_S = 600.0


class _ClipExistsError(Exception):
    """The clip directory already exists (same recording + range): never
    overwritten. Not an OSError, so it maps to 409, not 503."""


def _write_clip(
    iq_file, clip_dir: Path, stem: str, run, sample_start: int, count: int, info: dict
) -> int:
    """Copy `count` samples of one run into a new SigMF clip directory
    (runs in a worker thread). The data is streamed in chunks so a 10-minute
    clip at a high rate never sits in memory whole; the meta is written only
    after the data so the pair is consistent. Returns samples written."""
    clip_dir.parent.mkdir(parents=True, exist_ok=True)  # OSError -> 503
    try:
        clip_dir.mkdir()
    except FileExistsError:
        raise _ClipExistsError(str(clip_dir)) from None
    try:
        return _write_clip_files(iq_file, clip_dir, stem, run, sample_start, count, info)
    except BaseException:
        # No half-written clip left behind to be mistaken for a good one.
        shutil.rmtree(clip_dir, ignore_errors=True)
        raise


def _write_clip_files(
    iq_file, clip_dir: Path, stem: str, run, sample_start: int, count: int, info: dict
) -> int:
    from msdr.recorder.sigmf import build_meta

    written = 0
    chunk = max(1, run.sample_rate_hz)  # ~1 s per read
    with open(clip_dir / f"{stem}.sigmf-data", "wb") as out:
        while written < count:
            block = iq_file.read(run.index, sample_start + written, min(chunk, count - written))
            n = block.size // 2
            if n == 0:
                break
            out.write(block.astype("<i2", copy=False).tobytes())
            written += n
    if written != count:
        raise OSError(f"short read: {written} of {count} samples available")
    capture_start = info.get("capture_start")
    annotations = []
    if run.meta_path is not None:
        parent_meta = json.loads(run.meta_path.read_text())
        clip_end = sample_start + written
        for source in parent_meta.get("annotations") or []:
            position = source.get("core:sample_start")
            if position is None:
                continue
            position = int(position)
            extent = int(source.get("core:sample_count", 0))
            if extent > 0:
                lo, hi = max(position, sample_start), min(position + extent, clip_end)
                if lo >= hi:
                    continue
                clipped = {
                    **source,
                    "core:sample_start": lo - sample_start,
                    "core:sample_count": hi - lo,
                }
            else:
                if not sample_start <= position < clip_end:
                    continue
                # A gap at the clip's first sample happened before this clip.
                if source.get("core:label") == "gap" and position == sample_start:
                    continue
                clipped = {**source, "core:sample_start": position - sample_start}
            annotations.append(clipped)
    meta = build_meta(
        sample_rate=run.sample_rate_hz,
        center_hz=run.center_hz,
        start_wall=capture_start if capture_start is not None else 0.0,
        annotations=annotations,
        description=f"msdr clip of {info['parent_name']}",
    )
    if capture_start is None:
        # Capture time unknown (e.g. an SDRconnect WAV): omit it rather than
        # writing the epoch as if it were real.
        meta["captures"][0].pop("core:datetime", None)
    (clip_dir / f"{stem}.sigmf-meta").write_text(json.dumps(meta, indent=2))
    (clip_dir / "clip.json").write_text(json.dumps({**info, "samples": written}, indent=2))
    return written


class ReviewServiceMixin:
    # --- review: source-independent playback + audio --------------------
    # A VirtualReceiver only ever tunes/listens to the survey's replay
    # ReplaySource (msdr.survey.replay) -- it never touches self.adapter, so
    # replay tuning/listening/decoding makes zero live-radio writes. Survey
    # doesn't expose its ReplaySource through a public accessor, so this reaches into
    # Survey._replay directly.

    def _review_replay_source(self):
        return getattr(self.survey, "_replay", None) if self.survey is not None else None

    def _require_replay(self):
        self._require_survey()
        replay = self._review_replay_source()
        if replay is None:
            raise SurveyError("no replay running")
        return replay

    async def _attach_virtual_receiver(self) -> None:
        """(Re)binds self.virtual_rx to the survey's current replay source --
        called once a replay has (re)started. Any previous receiver (a prior
        replay) is torn down first."""
        await self._detach_virtual_receiver()
        if VirtualReceiver is None:  # msdr.source not importable -- see guard above
            return
        replay = self._review_replay_source()
        if replay is None:
            return
        rx = VirtualReceiver(replay)
        rx.configure_audio(**self.settings.native_audio.model_dump())
        await rx.start()
        self.virtual_rx = rx

    async def _detach_virtual_receiver(self) -> None:
        # A review decode session is bound to this receiver/replay pair
        # -- it never survives past its own receiver, so it's torn down first.
        try:
            await self._stop_replay_decode()
        finally:
            if self.virtual_rx is not None:
                rx, self.virtual_rx = self.virtual_rx, None
                await self._stop_review_resource(rx.stop)

    def review_status(self) -> dict:
        replay = self._review_replay_source()
        return {
            "replay": replay.status() if replay is not None else None,
            "receiver": self.virtual_rx.status() if self.virtual_rx is not None else None,
            "native_audio": self.settings.native_audio.model_dump(),
            "bookmarks": (
                self.review_bookmarks_list(str(replay.file.path)) if replay is not None else []
            ),
            "decode": self._replay_decode.status() if self._replay_decode is not None else None,
        }

    # --- review decode: ReplayDecodeSession bound to the running
    # replay's VirtualReceiver + ReplaySource --------------------------------

    def _on_replay_decode_event(self, event) -> None:
        """ReplayDecodeSession's `on_event`: store like a live decode event
        (DecodeEventRepo.add accepts anything with DecodeEvent's attributes)
        and publish through the exact same path live decoder_events use --
        the event itself already carries source_id="replay:<name>" and
        analysis_run_id (ReplayDecodeSession's own contract), which is what
        makes the recorder/beacons/survey fan-ins in
        _publish_decoder_message treat it as review analysis, not a live hit."""
        stored = self.decode_events.add(event)
        self._publish_decoder_message({"type": "decoder_event", "event": stored})

    def _publish_review_decode_status(self, status: dict) -> None:
        msg = {"type": "review_decode_status", "status": status}
        spawn(self.broadcaster.publish(msg))

    async def review_decode_start(
        self,
        decoders: list[str],
        mode: str = "single",
        *,
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        async with self._review_lifecycle_lock:
            replay = self._require_replay()
            if ReplayDecodeSession is None:  # msdr.decoder.replay_decode not importable yet
                raise SurveyError("review decode unavailable")
            if self.virtual_rx is None:
                raise SurveyError("no replay running")
            await self._stop_replay_decode()
            calls_dir = self._resolve_calls_dir(self.settings)
            calls_dir.mkdir(parents=True, exist_ok=True)
            fax_dir = self._resolve_fax_dir(self.settings)
            fax_dir.mkdir(parents=True, exist_ok=True)
            self._review_decode_generation += 1
            generation = self._review_decode_generation
            session = None

            def is_current() -> bool:
                return (
                    generation == self._review_decode_generation and self._replay_decode is session
                )

            def on_event(event) -> None:
                if is_current():
                    self._on_replay_decode_event(event)

            def on_status(status: dict) -> None:
                if is_current():
                    self._publish_review_decode_status(status)

            session = ReplayDecodeSession(
                self.virtual_rx,
                replay,
                binaries=resolve_binaries(self.settings.decoder.bin_dirs),
                settings=self.settings.decoder,
                on_event=on_event,
                on_status=on_status,
                calls_dir=calls_dir,
                fax_dir=fax_dir,
            )
            self._replay_decode = session
            try:
                if mode == "band":
                    await session.start_band(decoders, max_channels, threshold_db)
                else:
                    await session.start_single(decoders)
            except BaseException:
                if self._replay_decode is session:
                    self._replay_decode = None
                    self._review_decode_generation += 1
                await self._stop_review_resource(session.stop)
                raise
            return self.review_status()

    async def review_decode_stop(self) -> dict:
        async with self._review_lifecycle_lock:
            self._require_replay()
            await self._stop_replay_decode()
            return self.review_status()

    async def _stop_replay_decode(self) -> None:
        if self._replay_decode is not None:
            session, self._replay_decode = self._replay_decode, None
            self._review_decode_generation += 1
            await self._stop_review_resource(session.stop)

    @staticmethod
    async def _stop_review_resource(stop) -> None:
        """Finish replay cleanup even if the caller is cancelled."""
        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            finally:
                raise

    def review_tune(self, frequency_hz: int, demodulator: str, bandwidth_hz: int) -> dict:
        self._require_replay()
        if self.virtual_rx is None:
            raise SurveyError("no replay running")
        try:
            self.virtual_rx.tune(frequency_hz, demodulator, bandwidth_hz)
        except (OutOfSpanError, ValueError) as exc:
            raise SurveyError(str(exc)) from exc
        return self.virtual_rx.status()

    async def configure_native_audio(self, mode: str, changes: dict) -> dict:
        rx = await self.workbench.inspection_receiver(mode)
        merged = self.settings.native_audio.model_dump()
        merged.update(changes)
        from msdr.settings import NativeAudioSettings

        native = NativeAudioSettings.model_validate(merged)
        rx.configure_audio(**native.model_dump())
        self.settings = await self.apply_settings({"native_audio": native.model_dump()})
        return {"settings": native.model_dump(), "receiver": rx.status(), "scope": mode}

    async def receiver_audio_dsp(self, property: str, value: object) -> dict:
        from msdr.engine.restore import SetStep

        allowed = {
            "agc_enable",
            "agc_threshold",
            "noise_reduction_enable",
            "noise_reduction_strength",
            "squelch_enable",
            "squelch_threshold",
        }
        spec = REGISTRY.get(property)
        if property not in allowed or spec is None or spec.read_only:
            return {
                "status": "unsupported",
                "property": property,
                "value": value,
                "reason": "SDRconnect property is not supported",
            }
        if spec.kind is bool:
            if type(value) is not bool:
                raise ValueError(f"{property} must be a boolean")
        elif spec.kind is int:
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f"{property} must be an integer from 0 to 100")
        elif spec.kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{property} must be numeric")
            import math

            if not math.isfinite(float(value)):
                raise ValueError(f"{property} must be finite")
            if property == "noise_reduction_strength" and not 0 <= float(value) <= 100:
                raise ValueError("noise_reduction_strength must be between 0 and 100 percent")
            value = int(value) if property == "noise_reduction_strength" else float(value)
        try:
            report = await self._restore(
                [SetStep(property, value, False, spec.timeout_s)],
                desired={property: value},
                kind="tune",
                push_history=False,
                journal_action=False,
            )
        except (ConnectionError, ReceiverStopped, RestoreRunning) as exc:
            return {
                "status": "rejected",
                "property": property,
                "value": value,
                "confirmed_value": self.confirmed.props.get(property),
                "reason": str(exc),
            }
        outcome = report.outcomes[0] if report.outcomes else None
        status = "confirmed" if report.status == "confirmed" else "rejected"
        return {
            "status": status,
            "property": property,
            "value": value,
            "confirmed_value": self.confirmed.props.get(property),
            "reason": outcome.detail if outcome else report.detail,
        }

    def review_audio(
        self, *, squelch_db: float | None, volume: float | None, mute: bool | None
    ) -> dict:
        self._require_replay()
        if self.virtual_rx is None:
            raise SurveyError("no replay running")
        if squelch_db is not None:
            self.virtual_rx.squelch(squelch_db)
        if volume is not None:
            self.virtual_rx.volume(volume)
        if mute is not None:
            self.virtual_rx.mute(mute)
        return self.virtual_rx.status()

    def review_seek(self, position_s: float) -> dict:
        self._require_replay()
        self.survey.replay_control(position_s=position_s)
        return self.review_status()

    def review_loop(self, loop: bool) -> dict:
        self._require_replay()
        self.survey.replay_control(loop=loop)
        return self.review_status()

    # -- bookmarks (Observations with provenance="bookmark") -----------------

    def review_bookmark_create(self, note: str) -> dict:
        replay = self._require_replay()
        status = replay.status()
        run_index = getattr(replay, "run_index", 0) or 0
        sample_index = getattr(replay, "sample_index", None)
        if sample_index is None:  # earlier ReplaySource: fall back to the file clock
            sample_index = int(replay.position_s * replay.sample_rate_hz)
        capture_start = status.get("capture_start")
        sample_rate_hz = replay.sample_rate_hz or 1
        start_ts = (
            capture_start + sample_index / sample_rate_hz if capture_start is not None else None
        )
        session = self.obs_recorder.session
        obs = BookmarkObservation(
            session_id=session.id if session is not None else None,
            sample_start=sample_index,
            sample_end=sample_index,
            start_ts=start_ts,
            end_ts=start_ts,
            provenance="bookmark",
        )
        obs = self.observations.add(obs)
        ref = RecordingReference(
            observation_id=obs.id,
            path=str(replay.file.path),
            name=replay.file.path.name,
            run_index=run_index,
            sample_start=sample_index,
            # RecordingReference.note is the only free-text field on this pair
            # of rows -- the bookmark's note lives here.
            note=note or "",
        )
        self.observations.add_ref(ref)
        return self.observations.summary(obs)

    def review_bookmarks_list(self, path: str) -> list[dict]:
        """GET /api/review/bookmarks?path=: bookmark Observations whose
        RecordingReference names this recording path (indexed join)."""
        out = []
        for obs_id in self.observations.observation_ids_by_ref_path(path, provenance="bookmark"):
            obs = self.observations.get(obs_id)
            if obs is not None:
                out.append(self.observations.summary(obs))
        return out

    def review_bookmark_seek(self, bookmark_id: str) -> dict:
        """Seek to a bookmark -- or a clip (its start on the parent
        recording). A reference whose media is gone is a 404 with the reason,
        checked before "no replay running": the recording can't be replayed."""
        obs = self.observations.get(bookmark_id)
        if obs is None or obs.provenance not in ("bookmark", "clip"):
            raise HTTPException(404, "bookmark not found")
        refs = self.observations.refs(bookmark_id)
        replay = self._review_replay_source()
        replay_path = str(replay.file.path) if replay is not None else None
        ref = next((r for r in refs if r.path == replay_path), None)
        if ref is not None and not ref.available:
            raise HTTPException(404, "recording no longer exists")
        if ref is None and any(not r.available for r in refs):
            raise HTTPException(404, "recording no longer exists")
        replay = self._require_replay()
        if ref is None:
            raise SurveyError("bookmark is for a different recording")
        # The ref stores (run_index, run-local sample); the timeline maps
        # that to file-relative elapsed time across runs and known gaps.
        position_s = replay.timeline.elapsed_s(ref.run_index or 0, ref.sample_start or 0)
        self.survey.replay_control(position_s=position_s)
        return self.review_status()

    # -- clips: one run's sample range copied to its own SigMF dir --------

    def _clips_dir(self) -> Path:
        if self.settings.clips_dir:
            return Path(self.settings.clips_dir).expanduser()
        return Path(self.settings.recorder_root).expanduser() / "clips"

    async def review_clip_create(self, start_s: float, end_s: float, note: str = "") -> dict:
        replay = self._require_replay()
        length_s = end_s - start_s
        if not (start_s >= 0 and 0 < length_s <= _CLIP_MAX_S):
            raise HTTPException(
                409, f"clip range must satisfy 0 < end - start <= {_CLIP_MAX_S:.0f} s, start >= 0"
            )
        timeline = replay.timeline
        if end_s > timeline.total_elapsed_s + 1e-9:
            raise HTTPException(409, "clip range ends past the end of the recording")
        run_index, sample_start = timeline.locate(start_s)
        run = timeline.runs[run_index]
        run_end_s = timeline.elapsed_s(run_index, run.samples)
        if end_s > run_end_s + 1e-9:
            raise HTTPException(
                409,
                f"clip range crosses the end of run {run_index + 1} "
                f"(at {run_end_s:.3f} s): one run per clip",
            )
        sample_end = timeline.local_sample_at(run_index, end_s)
        if sample_end <= sample_start:
            raise HTTPException(409, "clip range contains no stored samples")
        parent = replay.file.path
        parent_name = parent.name if parent.is_dir() else parent.stem
        clip_name = f"{parent_name}_{round(start_s * 1000)}-{round(end_s * 1000)}"
        clip_dir = self._clips_dir() / clip_name
        stem = f"run-01_{run.sample_rate_hz}SPS_{run.center_hz}Hz"
        capture_start = None
        if run.start_wall is not None:
            capture_start = (
                run.start_wall
                + timeline.elapsed_s(run_index, sample_start)
                - timeline.run_start_s(run_index)
            )
        count = sample_end - sample_start
        clip_gap_samples = sum(
            missing
            for position, missing in run.interior_gaps
            if sample_start < position < sample_end
        )
        created_at = time.time()
        info = {
            "parent_path": str(parent),
            "parent_name": parent.name,
            "run_index": run_index,
            "sample_start": sample_start,
            "sample_end": sample_end,
            "samples": count,
            "sample_rate": run.sample_rate_hz,
            "center_hz": run.center_hz,
            "start_s": start_s,
            "end_s": end_s,
            "note": note,
            "created_at": created_at,
            "capture_start": capture_start,
        }
        try:
            await asyncio.to_thread(
                _write_clip, replay.file, clip_dir, stem, run, sample_start, count, info
            )
        except _ClipExistsError:
            raise HTTPException(409, f"clip already exists: {clip_dir}") from None
        except OSError as exc:
            raise HTTPException(503, f"clip write failed: {exc}") from exc

        session = self.obs_recorder.session
        receiver = self.virtual_rx.status() if self.virtual_rx is not None else None
        obs = self.observations.add(
            BookmarkObservation(
                session_id=session.id if session is not None else None,
                start_ts=capture_start,
                end_ts=(
                    capture_start + (count + clip_gap_samples) / run.sample_rate_hz
                    if capture_start is not None
                    else None
                ),
                sample_start=sample_start,
                sample_end=sample_end,
                receiver=receiver if isinstance(receiver, dict) else None,
                provenance="clip",
                notes=note,
            )
        )
        self.observations.add_ref(
            RecordingReference(
                observation_id=obs.id,
                path=str(clip_dir),
                name=clip_dir.name,
                run_index=0,
                sample_start=0,
                sample_end=count,
                note="clip",
            )
        )
        self.observations.add_ref(
            RecordingReference(
                observation_id=obs.id,
                path=str(parent),
                name=parent.name,
                run_index=run_index,
                sample_start=sample_start,
                sample_end=sample_end,
                note=note,
            )
        )
        return {**self.observations.summary(obs), "clip_path": str(clip_dir)}

    def review_clips_list(self, path: str) -> list[dict]:
        """GET /api/review/clips?path=: clip Observations referencing this
        recording (joined by the parent reference's path)."""
        out = []
        for obs_id in self.observations.observation_ids_by_ref_path(path, provenance="clip"):
            obs = self.observations.get(obs_id)
            if obs is None:
                continue
            clip_ref = next((r for r in self.observations.refs(obs_id) if r.path != path), None)
            out.append(
                {
                    **self.observations.summary(obs),
                    "clip_path": clip_ref.path if clip_ref is not None else None,
                }
            )
        return out
