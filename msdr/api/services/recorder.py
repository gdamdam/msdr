"""Pre-roll IQ recorder and finished SigMF recordings.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import HTTPException

from msdr.api.services._common import (
    _RECORDER_FORWARD_PROPS,
    Recorder,
    RecorderConfig,
    RecorderError,
)
from msdr.observations.models import Observation as BookmarkObservation
from msdr.observations.models import RecordingReference
from msdr.settings import Settings

logger = logging.getLogger(__name__)


_RECORDER_TICK_INTERVAL_S = 1.0


class RecorderServiceMixin:
    def _build_recorder(self, settings: Settings):
        if Recorder is None:  # msdr.recorder not importable -- see guard above
            return None
        return Recorder(
            RecorderConfig(
                root=Path(settings.recorder_root).expanduser(),
                preroll_s=settings.recorder_preroll_minutes * 60,
                min_free_gb=settings.recorder_min_free_gb,
                max_recording_s=settings.recorder_max_recording_minutes * 60,
                host=settings.host,
                port=settings.port,
            ),
            on_status=self._publish_recorder_status,
        )

    async def _start_recorder_runtime(self) -> None:
        # The running flag is set only after the await below; two concurrent
        # toggles (double-click, settings PATCH racing startup) would both
        # pass the check and double-start, so start/stop share a lock.
        async with self._recorder_runtime_lock:
            await self._start_recorder_runtime_locked()

    async def _start_recorder_runtime_locked(self) -> None:
        """Bring the recorder's own SDRconnect stream/ring up -- mirrors
        self._dwell_running for journal_passive: gated by the feature flag at
        startup and again live in apply_settings()."""
        if self.recorder is None or self._recorder_running:
            return
        # A replacement recorder missed the existing connection/property events.
        self.recorder.on_property("connected", self.adapter.connected)
        for name in _RECORDER_FORWARD_PROPS:
            if name in self.confirmed.props:
                self.recorder.on_property(name, self.confirmed.props[name])
        await self.recorder.start()
        self._recorder_running = True
        self._recorder_task = asyncio.create_task(self._recorder_tick_loop())

    async def _stop_recorder_runtime(self) -> None:
        async with self._recorder_runtime_lock:
            await self._stop_recorder_runtime_locked()

    async def _stop_recorder_runtime_locked(self) -> None:
        if not self._recorder_running:
            return
        try:
            # Best effort: leave no armed/recording state behind a feature
            # that just went away (its routes 404 from here on, same as the
            # scanner's own disable-while-running handling below).
            status = self.recorder.status()
            if self._rec_obs is not None:
                self._rec_obs["user_stop"] = True  # shutdown/disable: a deliberate stop
            if status.get("state") in ("recording", "finalizing"):
                await self.recorder.stop_recording()
            if status.get("state") != "off":
                await self.recorder.arm(False)
        except Exception:
            logger.exception("recorder: failed to disarm/stop cleanly on shutdown")
        if self._recorder_task is not None:
            self._recorder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recorder_task
            self._recorder_task = None
        try:
            await self.recorder.stop()
        except Exception:
            logger.exception("recorder: stop failed")
        # recorder.stop() waited for any finalize: close the observation now
        # rather than via a status push that may land after the db closes.
        self._sync_recording_observation(self.recorder.status())
        self._recorder_running = False

    async def _recorder_tick_loop(self) -> None:
        """~1 Hz status publish while armed/recording (buffered_s/recording_s
        tick up with no property change to hang a push off of) -- state
        transitions themselves are pushed immediately via on_status."""
        while True:
            await asyncio.sleep(_RECORDER_TICK_INTERVAL_S)
            if self.recorder is None:
                continue
            status = self.recorder.status()
            if status.get("state") in ("armed", "recording"):
                await self._publish_recorder_status(status)

    async def _maybe_rearm_recorder(self) -> None:
        """One-shot: re-apply a remembered `recorder_armed` once the receiver
        is actually connected+started (arming while stopped is refused, same
        restriction as a restore -- see msdr.recorder.Recorder.arm)."""
        if not self._recorder_rearm_pending or self.recorder is None:
            return
        if not (
            self.settings.features.recorder
            and self.adapter.connected
            and self.confirmed.props.get("started", False)
        ):
            return
        self._recorder_rearm_pending = False
        try:
            await self.recorder.arm(True)
        except RecorderError:
            logger.warning("recorder: startup re-arm refused (see recorder status)")
        except Exception:
            logger.exception("recorder: startup re-arm failed")

    async def _publish_recorder_status(self, status: dict) -> None:
        self._sync_recording_observation(status)
        await self.broadcaster.publish({"type": "recorder", **status})

    # -- recording -> observation -----------------------------------

    def _recorder_attr(self, name: str):
        # msdr.recorder.Recorder keeps the in-progress recording's directory
        # and REC wall time private and status() doesn't carry them; read
        # them directly (same call-out as _review_replay_source) rather than
        # guess them from the directory naming scheme.
        return getattr(self.recorder, name, None) if self.recorder is not None else None

    def _open_recording_observation(self, preroll_hint_s: float | None) -> None:
        """REC pressed: open the recording's Observation + its reference.

        `start_ts` must be the capture time of the recording's first sample
        -- REC time minus the pre-roll actually promoted. The recorder does
        not report that length at REC; `buffered_s` just before REC is an
        estimate (eviction may still trim it), so it's used and labelled as
        such here (ref note), then replaced at finalize by recording.json's
        run-1 start_wall (exact: the first promoted segment's own start).
        `provenance` stays exactly "recording" (the UI/list joins on it);
        only a length that is never known gets the contract's note."""
        if self._rec_obs is not None:
            return
        rec_dir = self._recorder_attr("_recording_dir")
        if rec_dir is None:
            return
        start_wall = self._recorder_attr("_record_started_at") or time.time()
        if preroll_hint_s is not None:
            start_ts = start_wall - preroll_hint_s
            ref_note = f"pre-roll ~{preroll_hint_s:.1f} s (estimate at REC)"
        else:
            start_ts = start_wall
            ref_note = "pre-roll length not recorded"
        props = self.confirmed.props
        vfo = props.get("device_vfo_frequency")
        bw = props.get("filter_bandwidth")
        session = self.obs_recorder.session
        obs = self.observations.add(
            BookmarkObservation(
                session_id=session.id if session is not None else None,
                start_ts=start_ts,
                sample_start=0,
                reported_hz=int(vfo) if vfo is not None else None,
                bandwidth_hz=int(bw) if bw is not None else None,
                receiver={
                    **self._session_receiver_snapshot(),
                    "center_hz": props.get("device_center_frequency"),
                    "sample_rate_hz": props.get("device_sample_rate"),
                },
                provenance="recording",
            )
        )
        rec_dir = Path(rec_dir)
        ref = self.observations.add_ref(
            RecordingReference(
                observation_id=obs.id,
                path=str(rec_dir),
                name=rec_dir.name,
                run_index=0,
                sample_start=0,
                note=ref_note,
            )
        )
        self._rec_obs = {
            "id": obs.id,
            "ref_id": ref.id,
            "dir": rec_dir,
            "errors": [],
            "user_stop": False,
        }

    def _sync_recording_observation(self, status: dict) -> None:
        """Every recorder status (on_status push, 1 Hz tick, REC/STOP
        routes): collect errors while recording/finalizing -- a successful
        finalize clears the recorder's own `error` (e.g. "low disk"), so it
        has to be caught on the way -- and close the observation once the
        recorder's *live* state has left recording/finalizing (a queued,
        older status snapshot must not close it early)."""
        try:
            if self.recorder is None:
                return
            if self._rec_obs is None:
                # The recorder only enters "recording" through record(), which
                # recorder_start already handles; this covers a record() made
                # some other way. The live directory check ignores stale snapshots.
                if self.recorder.status().get("state") == "recording":
                    self._open_recording_observation(None)
                return
            rec = self._rec_obs
            if status.get("state") in ("recording", "finalizing", "error"):
                err = status.get("error")
                if err and err not in rec["errors"]:
                    rec["errors"].append(err)
            live = self.recorder.status()
            if live.get("state") in ("recording", "finalizing"):
                return
            err = live.get("error")
            if err and err not in rec["errors"]:
                rec["errors"].append(err)
            self._close_recording_observation(live)
        except Exception:
            logger.exception("observations: recording observation sync failed")

    def _close_recording_observation(self, live: dict) -> None:
        rec, self._rec_obs = self._rec_obs, None
        obs = self.observations.get(rec["id"])
        if obs is None:
            return
        rec_dir: Path = rec["dir"]
        index = None
        with contextlib.suppress(OSError, ValueError):
            index = json.loads((rec_dir / "recording.json").read_text())
        # losses: the capture is incomplete (-> missing_frames); info: an
        # honest caveat that isn't a loss. Both land in provenance.
        losses = list(rec["errors"])
        info: list[str] = []
        quality = list(obs.capture_quality)
        update: dict = {"closed": True}
        ref_update: dict = {}
        if index is not None and index.get("runs"):
            run0 = index["runs"][0]
            update["end_ts"] = index.get("end_wall")
            update["sample_end"] = run0.get("samples")
            ref_update["sample_end"] = run0.get("samples")
            rec_wall = index.get("start_wall")
            if run0.get("start_wall") is not None and rec_wall is not None:
                update["start_ts"] = run0["start_wall"]
                ref_update["note"] = f"pre-roll {max(0.0, rec_wall - run0['start_wall']):.2f} s"
            else:
                info.append("pre-roll length not recorded")
            last = live.get("last_recording")
            if last:
                ref_update["path"] = str(last)
                ref_update["name"] = Path(last).name
        else:
            update["end_ts"] = time.time()
            losses.append("recording not finalized")
            info.append("pre-roll length not recorded")
        if not rec["user_stop"]:
            # Auto-stop. Hitting max length is a normal end; a radio that
            # stopped/disconnected under the recording is a loss.
            if not (self.adapter.connected and self.confirmed.props.get("started", False)):
                losses.append("radio stopped or disconnected during recording")
        if losses and "missing_frames" not in quality:
            quality.append("missing_frames")
            update["capture_quality"] = quality
        if losses or info:
            update["provenance"] = "; ".join([obs.provenance, *losses, *info])
        self.observations.update(obs.model_copy(update=update))
        if ref_update:
            ref = next((r for r in self.observations.refs(obs.id) if r.id == rec["ref_id"]), None)
            if ref is not None:
                self.observations.update_ref(ref.model_copy(update=ref_update))

    # --- pre-roll IQ recorder ------------------------------------------------

    def _require_recorder_feature(self) -> None:
        if not self.settings.features.recorder or self.recorder is None:
            raise HTTPException(404, "feature disabled: recorder")

    def recorder_status(self) -> dict:
        self._require_recorder_feature()
        return self.recorder.status()

    async def recorder_arm(self, on: bool) -> dict:
        self._require_band_scan_idle()
        self._require_recorder_feature()
        status = await self.recorder.arm(on)  # RecorderError -> 409 (app.py handler)
        self.settings.recorder_armed = on
        if str(self.settings.db_path) != ":memory:":
            self.settings.save(self.settings_path)
        return status

    async def recorder_start(self) -> dict:
        self._require_band_scan_idle()
        self._require_recorder_feature()
        before = self.recorder.status()
        status = await self.recorder.record()
        if status.get("state") == "recording":
            # buffered_s just before REC: what REC is about to promote (an
            # estimate -- see _open_recording_observation).
            self._open_recording_observation(before.get("buffered_s"))
        return status

    async def recorder_stop(self) -> dict:
        self._require_recorder_feature()
        status = await self.recorder.stop_recording()
        if self._rec_obs is not None:
            self._rec_obs["user_stop"] = True
        self._sync_recording_observation(status)
        return status

    # --- recordings (U1 item 5: browse/manage finished SigMF recordings) ----

    def _recordings_root(self) -> Path:
        return Path(self.settings.recorder_root).expanduser() / "recordings"

    def _recording_dir_for(self, name: str) -> Path:
        """Resolves and validates a recording directory name against the
        recordings root. The root itself, nested paths, and traversal attempts
        all resolve to "not found", same pattern as
        decoder_call_wav_path/decoder_image_path above."""
        root = self._recordings_root().resolve()
        candidate = (root / name).resolve()
        if candidate.parent != root:
            raise HTTPException(404, "recording not found") from None
        if not candidate.is_dir():
            raise HTTPException(404, "recording not found")
        return candidate

    def _recording_row(self, d: Path) -> dict:
        """One row of GET /api/recordings: reads every *.sigmf-meta in the
        recording's directory (a recording can hold more than one run if the
        center/rate changed mid-recording -- see Recorder._finalize) for
        sample_rate/center_hz (from the first run), gap annotations (summed
        across all runs), and pairs each with its *.sigmf-data for size/
        duration. `created` prefers recording.json's start_wall (the real
        wall-clock the REC button was pressed) when present, else the
        directory's own mtime."""
        sample_rate = None
        center_hz = None
        gaps = 0
        duration_s = 0.0
        size_bytes = 0
        runs = 0
        for meta_path in sorted(d.glob("*.sigmf-meta")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            runs += 1
            glb = meta.get("global") or {}
            if sample_rate is None:
                sample_rate = glb.get("core:sample_rate")
            captures = meta.get("captures") or [{}]
            if center_hz is None:
                center_hz = captures[0].get("core:frequency")
            gaps += sum(1 for a in meta.get("annotations") or [] if a.get("core:label") == "gap")
            data_path = meta_path.with_suffix(".sigmf-data")
            if data_path.is_file():
                size = data_path.stat().st_size
                size_bytes += size
                run_rate = glb.get("core:sample_rate")
                if isinstance(run_rate, (int, float)) and run_rate > 0:
                    duration_s += (size // 4) / run_rate
        created = d.stat().st_mtime
        # `capture_start` is the real reception time or None -- never the
        # directory mtime (Review shell: "capture time unknown"
        # must stay honest). `created` keeps its old fallback for
        # sorting/display of the list.
        capture_start = None
        with contextlib.suppress(OSError, ValueError):
            index = json.loads((d / "recording.json").read_text())
            if index.get("start_wall") is not None:
                created = capture_start = index["start_wall"]
        return {
            "path": str(d),
            "name": d.name,
            "center_hz": center_hz,
            "sample_rate": sample_rate,
            "duration_s": duration_s,
            "size_bytes": size_bytes,
            "gaps": gaps,
            "runs": runs,
            "created": created,
            "capture_start": capture_start,
            "error": None,
        }

    def _recording_error_row(self, d: Path, exc: Exception) -> dict:
        """A recording whose *.sigmf-meta is structurally invalid (e.g. valid
        JSON that isn't an object -- `meta.get(...)` then raises) must not
        take the rest of the list down with it. Minimal identity only: the
        caller has no trustworthy sample_rate/center_hz/etc for this row."""
        try:
            created = d.stat().st_mtime
        except OSError:
            created = None
        return {
            "path": str(d),
            "name": d.name,
            "center_hz": None,
            "sample_rate": None,
            "duration_s": None,
            "size_bytes": None,
            "gaps": None,
            "runs": None,
            "created": created,
            "capture_start": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    def recordings_list(self) -> list[dict]:
        self._require_recorder_feature()
        root = self._recordings_root()
        if not root.is_dir():
            return []
        try:
            # `wifi/` holds packet-capture sessions (msdr/wifi/sessions.py), listed in
            # Review's Wi-Fi sessions panel; it is not an IQ recording.
            dirs = sorted(d for d in root.iterdir() if d.is_dir() and d.name != "wifi")
        except OSError as exc:
            # An unreadable recordings folder (external volume denied to this
            # process) is an explicit unavailable outcome, not an empty list.
            raise HTTPException(503, f"recordings folder not readable: {exc}") from exc
        rows = []
        for d in dirs:
            try:
                rows.append(self._recording_row(d))
            except Exception as exc:  # one damaged recording must not 500 the whole list
                rows.append(self._recording_error_row(d, exc))
        return rows

    def recordings_delete(self, name: str) -> dict:
        self._require_recorder_feature()
        if self.recorder.status().get("state") in ("recording", "finalizing"):
            raise RecorderError("stop and finish the recording before deleting recordings")
        d = self._recording_dir_for(name)
        shutil.rmtree(d)
        # References to it stay, explicitly unavailable (never silently
        # dropped). Both spellings: refs carry the recorder's unresolved
        # root path, _recording_dir_for resolved it.
        self.observations.mark_unavailable(str(d))
        unresolved = str(self._recordings_root() / d.name)
        if unresolved != str(d):
            self.observations.mark_unavailable(unresolved)
        return {"deleted": True}

    def recordings_reveal(self, name: str) -> dict:
        """POST /api/recordings/{name}/reveal: `open <dir>` on macOS opens the
        recording's own folder in Finder (the button says "Open folder";
        `open -R` only selected it inside the parent, which read as "nothing
        happened"). Elsewhere there's no equivalent, so hand back the path."""
        self._require_recorder_feature()
        d = self._recording_dir_for(name)
        if sys.platform != "darwin":
            return {"ok": False, "error": None, "path": str(d)}
        try:
            subprocess.run(["open", str(d)], check=True)
            return {"ok": True, "error": None}
        except (OSError, subprocess.CalledProcessError) as exc:
            return {"ok": False, "error": str(exc)}

    def recordings_analyze(self, name: str) -> dict:
        """POST /api/recordings/{name}/analyze: hands the recording's IQ data
        to the same iqscan runner /api/scans/run uses (run_scan above), off
        its first run's *.sigmf-meta for sample_rate/center_hz."""
        self._require_recorder_feature()
        d = self._recording_dir_for(name)
        metas = sorted(d.glob("*.sigmf-meta"))
        if not metas:
            raise HTTPException(404, "no IQ data in recording")
        data_path = metas[0].with_suffix(".sigmf-data")
        if not data_path.is_file():
            raise HTTPException(404, "no IQ data in recording")
        meta = json.loads(metas[0].read_text())
        sample_rate = (meta.get("global") or {}).get("core:sample_rate")
        captures = meta.get("captures") or [{}]
        center_hz = captures[0].get("core:frequency")
        return self.run_scan(str(data_path), sample_rate=sample_rate, center_hz=center_hz)
