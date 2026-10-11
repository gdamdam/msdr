"""Wide-view survey (S1) and band scans.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import HTTPException

from msdr.api.services._common import IQStream, Survey, SurveyError
from msdr.engine.restore import plan_with_desired
from msdr.engine.state import ReceiverState
from msdr.providers.base import Candidate, IdentifyQuery
from msdr.radio.registry import RESTORABLE
from msdr.settings import Settings
from msdr.survey.bandscan import BandScanError
from msdr.tasks import spawn

logger = logging.getLogger(__name__)


class SurveyServiceMixin:
    def _build_survey(self, settings: Settings):
        if Survey is None:  # msdr.survey not importable -- see guard above
            return None
        return Survey(
            adapter=self.adapter,
            hub=self.spectrum_hub,
            settings_getter=lambda: self.settings.survey,
            publish=self._publish_survey_message,
            confirmed_state=self.confirmed,
            feature_enabled=lambda: self.settings.features.survey,
            identify=self._identify_for_survey,
            # docs/allocations.md: label["allocation"] (every service of the
            # band, frequency-only) beside the first-candidate bandplan hint.
            allocation=self.allocation_summary_at,
            # S3/S4 IQ analyzer's own SDRconnect stream (separate socket, same
            # pattern as _build_decoder's candidate_stream above).
            iq_stream_factory=(
                (lambda: IQStream(settings.host, settings.port)) if IQStream is not None else None
            ),
            # S5 decode handoff: Survey.decode/undecode drive BAND DECODE
            # pin/unpin through these -- see _survey_decode_start/_stop.
            decode_start=self._survey_decode_start,
            decode_stop=self._survey_decode_stop,
            # S2 observations: fired at label boundaries only (never per
            # spectrum frame -- see Survey's own contract).
            on_labels_changed=self.obs_recorder.sync_labels,
        )

    async def _survey_decode_start(self, frequency_hz: int, decoders: list[str]) -> dict:
        """Survey's `decode_start` hook (S5 contract): starts/adds a pinned
        BAND DECODE channel. A SurveyError (not DecoderError) so it maps the
        same way whether msdr.decoder isn't importable at all or the feature
        is off -- both are "decode refused" from Survey's point of view."""
        if self.decoder is None:
            raise SurveyError("decoder unavailable")
        from msdr.decoder.registry import DECODERS

        if any(
            DECODERS.get(d) and DECODERS[d].iq_only and DECODERS[d].demodulators == ("IQ",)
            for d in decoders
        ):
            # Wideband protocols need original IQ, never an NFM channelizer
            # slot. The manager checks rate/span and isolates this session.
            return await self.decoder.start_decoding(decoders, frequency_hz, "manual")
        return await self.decoder.band_pin(frequency_hz, decoders)

    async def _survey_decode_stop(self, frequency_hz: int) -> dict:
        if self.decoder is None:
            raise SurveyError("decoder unavailable")
        from msdr.decoder.registry import DECODERS

        status = self.decoder.status()
        if (
            status.get("active")
            and status.get("frequency_hz") == frequency_hz
            and any(
                DECODERS.get(d.get("id"))
                and DECODERS[d["id"]].iq_only
                and DECODERS[d["id"]].demodulators == ("IQ",)
                for d in status.get("decoders", [])
            )
        ):
            return await self.decoder.stop_decoding("survey")
        return await self.decoder.band_unpin(frequency_hz)

    def _identify_for_survey(self, frequency_hz: int, width_hz: float) -> list[dict]:
        """Enrichment callback for msdr.survey labels (S2 contract) -- reuses
        identify()/_catalog_candidates but never raises (this runs from the
        survey's own status-build loop, not a request) and returns [] when
        the relevant feature is off."""
        try:
            q = IdentifyQuery(frequency_hz=frequency_hz, filter_bandwidth_hz=int(width_hz) or 12500)
            candidates: list[Candidate] = []
            if self.settings.features.ident:
                candidates.extend(self.identify_provider.identify(q))
            if self.settings.features.catalog:
                candidates.extend(self._catalog_candidates(q))
            return [
                {
                    "name": c.name,
                    "category": c.category,
                    "confidence": c.confidence,
                    "reason": c.reason,
                    "provider": c.provider,
                }
                for c in candidates
            ]
        except Exception:
            logger.debug("survey identify failed for %s Hz", frequency_hz, exc_info=True)
            return []

    def _publish_survey_message(self, msg: dict) -> None:
        if msg.get("type") == "survey_band_scan":
            state = (msg.get("status") or {}).get("state")
            if state in {"complete", "stopped", "failed"}:
                self._band_scan_active = False
        spawn(self.broadcaster.publish(msg))

    async def _start_survey_runtime(self) -> None:
        # The running flag is set only after the await below; two concurrent
        # toggles (double-click, settings PATCH racing startup) would both
        # pass the check and double-start, so start/stop share a lock.
        async with self._survey_runtime_lock:
            await self._start_survey_runtime_locked()

    async def _start_survey_runtime_locked(self) -> None:
        """Bring Survey's own ConfirmedState subscription up -- mirrors
        _start_decoder_runtime, gated by the feature flag at startup and
        again live in apply_settings()."""
        if self.survey is None or self._survey_running:
            return
        await self.survey.start()
        self._survey_running = True

    async def _stop_survey_runtime(self) -> None:
        async with self._survey_runtime_lock:
            await self._stop_survey_runtime_locked()

    async def _stop_survey_runtime_locked(self) -> None:
        if not self._survey_running:
            return
        try:
            await self.survey.stop()
        except Exception:
            logger.exception("survey: stop failed")
        self._survey_running = False

    # --- wide-view survey (S1) ------------------------------------------------

    def _require_survey(self) -> None:
        # Same shape as _require_decoder: an unavailable survey package is a
        # SurveyError -> 409 (app.py handler), not a 404 -- "start refused",
        # not "not found". A disabled survey feature is refused the same way,
        # inside Survey itself (feature_enabled, per its own contract).
        if self.survey is None:
            raise SurveyError("survey unavailable")

    def _session_receiver_snapshot(self) -> dict:
        """Antenna/gain/profile snapshot for a new observations Session's
        `receiver` field (S2) -- every RESTORABLE property except the
        span/VFO ones, which the session carries separately as
        center_hz/sample_rate_hz."""
        skip = {"device_sample_rate", "device_center_frequency", "device_vfo_frequency"}
        return {
            spec.name: self.confirmed.props.get(spec.name)
            for spec in RESTORABLE
            if spec.name not in skip
        }

    def survey_status(self) -> dict | None:
        status = self.survey.status() if self.survey is not None else None
        if status is not None:
            session = self.obs_recorder.session
            status = {**status, "session_id": session.id if session is not None else None}
        return status

    async def survey_start(self) -> dict:
        self._require_survey()
        was_active = self.survey.status()["active"]
        status = await self.survey.start_survey()
        if not was_active and status.get("active"):
            span = status.get("span") or {}
            self.obs_recorder.start_session(
                "live:sdrconnect",
                "live",
                self.settings.host,
                center_hz=span.get("center_hz"),
                sample_rate_hz=span.get("sample_rate_hz"),
                receiver=self._session_receiver_snapshot(),
            )
        return status

    def survey_band_scan_status(self) -> dict:
        return self.band_scan.status()

    async def survey_band_scan_read(self, scan_id: str, view: str, **kwargs) -> dict | bytes:
        """Read reports off the live receiver loop, including legacy payloads."""
        methods = {
            "summary": self.band_scan.repo.asummary,
            "observations": self.band_scan.repo.aobservations,
            "spectrum": self.band_scan.repo.aspectrum,
            "export": self.band_scan.repo.aget,
            "export_bytes": self.band_scan.repo.aexport_bytes,
        }
        result = await methods[view](scan_id, **kwargs)
        if result is None:
            raise HTTPException(404, "survey band scan not found")
        return result

    async def survey_band_scan_delete(self, scan_id: str | None = None) -> dict:
        from msdr.survey.scan_storage import ScanWriterError

        current = self.band_scan.current
        if current and current["state"] in {"running", "paused"} and (
            scan_id is None or scan_id == current["id"]
        ):
            raise HTTPException(409, "stop the active band scan before deleting reports")
        try:
            deleted = await self.band_scan.repo.adelete(scan_id)
        except ScanWriterError as exc:
            raise HTTPException(409, str(exc)) from exc
        if scan_id is not None and not deleted:
            raise HTTPException(404, "survey band scan not found")
        if self.band_scan.current is current and current and (
            scan_id is None or scan_id == current["id"]
        ):
            self.band_scan.current = None
        return {"deleted": deleted}

    def _band_scan_labels(self) -> list[dict]:
        if self.survey is None:
            return []
        scan_labels = getattr(self.survey, "scan_labels", None)
        return scan_labels() if scan_labels else self.survey.status().get("labels", [])

    def _require_band_scan_idle(self) -> None:
        if self.workbench.owns_receiver:
            raise HTTPException(409, "stop the cycling signal watch before tuning")
        if self._band_scan_active:
            raise BandScanError("survey band scan owns the receiver")

    async def survey_band_scan_start(self, request: dict) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        if self.survey.status().get("replay"):
            raise SurveyError("stop replay before starting a live band scan")
        if self.scanner.state != "idle" or self.scanner.is_tuning:
            raise BandScanError("stop the scanner before starting a survey band scan")
        if self.decoder is not None and self.decoder.status().get("active"):
            raise BandScanError("stop live decoding before starting a survey band scan")
        if self.recorder is not None and self.recorder.status().get("state") == "recording":
            raise BandScanError("stop recording before starting a survey band scan")
        if not self.confirmed.is_complete() or not self.adapter.connected:
            raise ConnectionError("not connected")
        supported = self.survey.status().get("sample_rates_hz", [])
        if request["span_hz"] not in supported:
            raise BandScanError(f"span must be one of the supported sample rates: {supported}")
        if self.beacons and self.beacons.status().get("active"):
            raise BandScanError("stop beacon monitoring before starting a survey band scan")
        if self.doppler_loop.status is not None:
            raise BandScanError("stop satellite tracking before starting a survey band scan")
        self._band_scan_active = True
        try:
            if not self.survey.status().get("active"):
                await self.survey_start()
            return self.band_scan.start(**request)
        except Exception:
            self._band_scan_active = False
            raise

    def survey_band_scan_pause(self) -> dict:
        return self.band_scan.pause()

    def survey_band_scan_resume(self) -> dict:
        return self.band_scan.resume()

    async def survey_band_scan_stop(self) -> dict:
        try:
            return await self.band_scan.stop()
        finally:
            self._band_scan_active = False

    def _band_scan_confirmed_span(self) -> tuple[int | None, int | None, int | None]:
        return (
            self.confirmed.props.get("device_center_frequency"),
            self.confirmed.props.get("device_sample_rate"),
            self.confirmed.props.get("device_vfo_frequency"),
        )

    def _band_scan_snapshot(self) -> dict:
        scan_snapshot = getattr(self.survey, "scan_snapshot", None)
        if scan_snapshot is not None:
            snapshot = scan_snapshot()
            snapshot["connected"] = bool(self.adapter.connected and snapshot.get("connected"))
            return snapshot
        status = self.survey.status() if self.survey is not None else {}
        span = status.get("span") or {}
        stream = status.get("stream") or {}
        return {
            "frame_seq": stream.get("frames", 0),
            "connected": bool(self.adapter.connected and stream.get("connected")),
            "survey_center_hz": span.get("center_hz"),
            "survey_sample_rate_hz": span.get("sample_rate_hz"),
        }

    async def _band_scan_tune_span(
        self, sample_rate_hz: int, center_hz: int, *, kind: str = "survey_band_scan"
    ) -> bool:
        target = dict(self.confirmed.props)
        target.update(
            {
                "device_sample_rate": sample_rate_hz,
                "device_center_frequency": center_hz,
                "device_vfo_frequency": center_hz,
            }
        )
        full = ReceiverState.from_props(
            target, source="tune", api_version=self.confirmed.api_version
        )
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props,
            full=full,
        )
        begin_retune = getattr(self.survey, "begin_retune", None)
        end_retune = getattr(self.survey, "end_retune", None)
        if begin_retune is not None:
            begin_retune()
        try:
            report = await self._restore(
                steps,
                desired=desired,
                kind=kind,
                push_history=False,
                journal_action=False,
            )
            return report.status == "confirmed"
        finally:
            # A failed or cancelled restore can still move the receiver. End
            # the boundary at the actual confirmed span, never the requested one.
            if end_retune is not None:
                end_retune(
                    self.confirmed.props.get("device_center_frequency"),
                    self.confirmed.props.get("device_sample_rate"),
                )

    async def survey_stop(self) -> dict:
        self._require_survey()
        if self._band_scan_active:
            await self.band_scan.stop()
            self._band_scan_active = False
        status = await self.survey.stop_survey()
        if self.obs_recorder.session is not None:
            self.obs_recorder.end_session()
        return status

    async def survey_replay(self, path: str, loop: bool = True) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        async with self._review_lifecycle_lock:
            previous_replay = self._review_replay_source()
            # Stop decode before Survey can replace the ReplaySource it reads.
            await self._detach_virtual_receiver()
            try:
                status = await self.survey.start_replay(path, loop)
                span = status.get("span") or {}
                # start_session() ends whatever session was already open.
                self.obs_recorder.start_session(
                    f"replay:{Path(path).name}",
                    "replay",
                    path,
                    center_hz=span.get("center_hz"),
                    sample_rate_hz=span.get("sample_rate_hz"),
                    receiver=self._session_receiver_snapshot(),
                )
                await self._attach_virtual_receiver()
                return status
            except BaseException:
                # Invalid paths are rejected before Survey changes sources;
                # restore its receiver if that is still the active source.
                if previous_replay is not None and self._review_replay_source() is previous_replay:
                    await self._attach_virtual_receiver()
                raise

    async def survey_replay_stop(self) -> dict:
        self._require_survey()
        async with self._review_lifecycle_lock:
            await self._detach_virtual_receiver()
            status = await self.survey.stop_replay()
            if self.obs_recorder.session is not None:
                self.obs_recorder.end_session()
            return status

    async def survey_replay_control(
        self, playing: bool | None, position_s: float | None, loop: bool | None
    ) -> dict:
        self._require_survey()
        return self.survey.replay_control(playing, position_s, loop)

    async def survey_clear(self) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        return self.survey.clear()

    async def survey_span(self, sample_rate_hz: int, center_hz: int, keep_vfo: bool = True) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        return await self.survey.set_span(sample_rate_hz, center_hz, keep_vfo)

    def survey_dimensions(self, dimensions: list[str]) -> dict:
        self._require_survey()
        status = self.survey.set_dimensions(dimensions)  # SurveyError -> 409 first
        # Remembered across restarts, like recorder_arm: every msdr restart used
        # to drop the IQ dimensions back to the defaults (analyzer off).
        self.settings.survey.dimensions = list(dimensions)
        if str(self.settings.db_path) != ":memory:":
            self.settings.save(self.settings_path)
        return status

    async def survey_validation(self, request: dict) -> dict:
        self._require_survey()
        analyzer = self.survey._analyzer
        if analyzer is None or not self.survey.status().get("active"):
            raise ValueError("Start Survey and enable an IQ analysis dimension before capture")
        from msdr.rfml.validation_capture import save_capture

        if getattr(self, "_validation_busy", False):
            raise ValueError("A validation capture is already being saved")
        self._validation_busy = True
        try:
            duration = request.get("duration_s", 0)
            iq, rate, metadata = await analyzer.capture_validation(
                request["kind"], **({"duration_s": duration} if duration else {})
            )
            metadata["survey"] = self.survey.status()
            # One identifier per application session, never derived from predictions.
            if not hasattr(self, "_validation_session"):
                import uuid

                self._validation_session = uuid.uuid4().hex
            metadata["session_id"] = self._validation_session
            directory = (
                Path(self.settings.validation_samples_dir).expanduser()
                if self.settings.validation_samples_dir
                else Path(self.settings.recorder_root).expanduser() / "rfml-validation"
            )
            return await asyncio.to_thread(
                save_capture,
                directory,
                iq,
                rate,
                metadata,
                request["label"],
                request["notes"],
            )
        finally:
            self._validation_busy = False

    async def survey_ml(self, patch: dict[str, bool | float]) -> dict:
        self._require_survey()
        from msdr import rfml

        if not rfml.AVAILABLE:
            raise HTTPException(404, "RF ML is not available in this build")
        ml = self.settings.survey.ml
        updated = ml.model_copy(deep=True)
        if "classifier_enabled" in patch:
            updated.enabled = patch["classifier_enabled"]
        if "detector_enabled" in patch:
            updated.detector.enabled = patch["detector_enabled"]
        if "detector_interval_s" in patch:
            updated.detector.interval_s = patch["detector_interval_s"]
        if updated == ml:
            return self.survey_status()
        # Commit the durable choice before changing workers. A failed write
        # must remain visible to the caller and leave runtime state untouched.
        new_settings = self.settings.model_copy(deep=True)
        new_settings.survey.ml = updated
        if str(new_settings.db_path) != ":memory:":
            new_settings.save(self.settings_path)
        self.settings = new_settings
        await self.survey.update_ml()
        return self.survey_status()

    async def survey_decode(self, frequency_hz: int, decoders: list[str] | None = None) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        return await self.survey.decode(frequency_hz, decoders)

    async def survey_test_signal(
        self, frequency_hz: int, decoders: list[str], *, optimize: bool = False
    ) -> dict:
        """Probe current IQ passively; a test never tunes SDRconnect or saves messages."""
        self._require_survey()
        self._require_band_scan_idle()
        status = self.survey.status()
        if not status.get("active") or status.get("replay"):
            raise SurveyError("start a live survey before testing a signal")
        if IQStream is None:
            raise SurveyError("IQ stream unavailable")
        if self._signal_test_lock.locked():
            raise SurveyError("a signal test is already running")
        async with self._signal_test_lock:
            from msdr.decoder.signal_test import test_signal

            center = int(await self.adapter.get("device_center_frequency"))
            rate = int(await self.adapter.get("device_sample_rate"))
            if await self.adapter.get("started") in (False, "false"):
                raise SurveyError("receiver is stopped")
            try:
                result = await test_signal(
                    frequency_hz=frequency_hz,
                    center_hz=center,
                    sample_rate_hz=rate,
                    requested=decoders,
                    bin_dirs=self.settings.decoder.bin_dirs,
                    iq_stream_factory=lambda: IQStream(self.settings.host, self.settings.port),
                    optimize=optimize,
                )
            except ValueError as exc:
                raise SurveyError(str(exc)) from exc
            if (
                int(await self.adapter.get("device_center_frequency")) != center
                or int(await self.adapter.get("device_sample_rate")) != rate
            ):
                result["outcome"] = "span_changed"
                result["recommended_hz"] = None
                result["recommended_decoder"] = None
            return result

    async def survey_undecode(self, frequency_hz: int) -> dict:
        self._require_survey()
        self._require_band_scan_idle()
        return await self.survey.undecode(frequency_hz)

    def survey_save(self, frequency_hz: int, name: str | None = None) -> dict:
        """POST /api/survey/save (S5): reuses decoder_band_save's
        creation/idempotency path, but sources decoders/name/notes from the
        survey's own identity_summary instead of the running band session."""
        self._require_survey()
        summary = self.survey.identity_summary(frequency_hz) or {}
        return self._save_identified_channel(
            frequency_hz,
            name=name or summary.get("name"),
            decoders=list(summary.get("decoders") or []),
            notes=summary.get("notes") or "",
        )
