"""Signal decoders (P1): runtime, prepare/restore, band decode.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import logging
from pathlib import Path

from fastapi import HTTPException

from msdr.api.schemas import restore_report_to_dict
from msdr.api.services._common import (
    _ALL_SIGNALS_LIMIT,
    AudioStream,
    DecoderError,
    DecoderManager,
    IQStream,
    describe_decoders,
)
from msdr.engine.confirmed import matches
from msdr.engine.restore import RestoreReport
from msdr.library.models import Signal
from msdr.radio.registry import RESTORABLE
from msdr.settings import Settings
from msdr.tasks import spawn

logger = logging.getLogger(__name__)


# Decoder presets (P1 spec "Presets"): (name, frequency_hz, demodulator,
# filter_bandwidth_hz, decoder ids). decoder_presets_install() creates these
# as ordinary memories via the existing signal-creation path.
_DECODER_PRESETS: list[tuple[str, int, str, int, list[str]]] = [
    ("Pager 152.840", 152_840_000, "NFM", 12_500, ["pocsag", "flex"]),
    ("APRS 144.390", 144_390_000, "NFM", 12_500, ["aprs"]),
    ("NOAA WX 162.400", 162_400_000, "NFM", 12_500, ["eas"]),
    ("NOAA WX 162.475", 162_475_000, "NFM", 12_500, ["eas"]),
    ("NOAA WX 162.550", 162_550_000, "NFM", 12_500, ["eas"]),
    ("RTTY 14.085", 14_085_000, "USB", 2_800, ["rtty"]),
    ("CW 14.050", 14_050_000, "CW", 500, ["cw"]),
    # HF radiofax/NAVTEX + time signals. Frequencies are
    # examples (the user edits them); WEFAX tunes 1.9 kHz below the listed
    # fax carrier, DCF77/MSF need HDR mode below 2 MHz.
    ("WEFAX Boston 12750", 12_748_100, "USB", 3_000, ["wefax"]),
    ("NAVTEX 518 kHz", 516_300, "USB", 1_000, ["navtex"]),
    ("WWV 10 MHz", 10_000_000, "AM", 6_000, ["wwv"]),
    ("DCF77 77.5 kHz", 77_500, "AM", 1_000, ["dcf77"]),
    ("MSF 60 kHz", 60_000, "AM", 1_000, ["msf"]),
]


# BAND DECODE "save as memory" (P2): a save within this tolerance of an
# existing memory is treated as the same channel (returns it unchanged)
# rather than creating a near-duplicate.
_BAND_SAVE_TOLERANCE_HZ = 6_000


class DecoderServiceMixin:
    def _build_decoder(self, settings: Settings):
        if DecoderManager is None:  # msdr.decoder not importable -- see guard above
            return None
        calls_dir = self._resolve_calls_dir(settings)
        calls_dir.mkdir(parents=True, exist_ok=True)
        fax_dir = self._resolve_fax_dir(settings)
        fax_dir.mkdir(parents=True, exist_ok=True)
        common_kwargs = dict(
            adapter=self.adapter,
            stream_factory=lambda: AudioStream(settings.host, settings.port),
            repo=self.decode_events,
            # DecoderManager calls `publish` synchronously (Callable[[dict],
            # None], per its own contract) -- Broadcaster.publish is a
            # coroutine, so it's scheduled as a task rather than passed
            # directly (which would just build an un-awaited coroutine and
            # silently drop every decoder/decoder_event push).
            publish=self._publish_decoder_message,
            settings_getter=lambda: self.settings.decoder,
            confirmed_state=self.confirmed,
            feature_enabled=lambda: self.settings.features.decoder,
        )
        # BAND DECODE (P2) -- its own IQStream connection (separate from
        # AudioStream above; two IQ sockets run fine simultaneously, see the
        # plan's "Measured facts") and the memories to seed as band channels,
        # filtered to whatever span the manager is tuned to.
        return DecoderManager(
            **common_kwargs,
            iq_stream_factory=lambda: IQStream(settings.host, settings.port),
            signals_in_span=self._decoder_signals_in_span,
            calls_dir=calls_dir,
            fax_dir=fax_dir,
        )

    def _resolve_calls_dir(self, settings: Settings) -> Path:
        """Where dsd-fme's per-call WAVs live: an explicit
        `decoder.calls_dir` override, or `<db_path's dir>/decoder/calls` by
        default -- mirrors _build_recorder's use of settings.recorder_root."""
        if settings.decoder.calls_dir:
            return Path(settings.decoder.calls_dir).expanduser()
        return Path(settings.db_path).parent / "decoder" / "calls"

    def _resolve_fax_dir(self, settings: Settings) -> Path:
        """Where wefax's progressive PNGs live: an explicit
        `decoder.fax_dir` override, or `<db_path's dir>/decoder/fax` by
        default -- mirrors _resolve_calls_dir above."""
        if settings.decoder.fax_dir:
            return Path(settings.decoder.fax_dir).expanduser()
        return Path(settings.db_path).parent / "decoder" / "fax"

    def _decoder_signals_in_span(
        self, center_hz: float, sample_rate_hz: float
    ) -> list[tuple[float, str, list[str]]]:
        """Memories with decoders that fall inside a BAND DECODE span, for
        DecoderManager's `signals_in_span` kwarg -- half the sample rate,
        pulled in by 25 kHz so a channel at the very edge isn't cut off."""
        half_span = sample_rate_hz / 2 - 25_000
        return [
            (s.frequency_hz, s.id, list(s.decoders))
            for s in self.signals.list(limit=_ALL_SIGNALS_LIMIT)
            if s.decoders and abs(s.frequency_hz - center_hz) <= half_span
        ]

    def _navaid_ident_check(self, msg: dict) -> None:
        """Cross-check a confirmed VOR/ILS Morse ident against FAA NASR navaids
        near the event frequency (reference catalog, docs/reference-catalogs.md).
        Adds `ident_catalog_match` (True/False, or None when no navaid is
        listed there) and the catalog idents. Never raises: a missing or
        unreadable catalog leaves the event as decoded."""
        event = msg.get("event") or {}
        fields = event.get("fields")
        if event.get("decoder") not in {"vor", "ils"} or not isinstance(fields, dict):
            return
        ident = fields.get("ident")
        freq = event.get("frequency_hz")
        catalog = getattr(self, "catalog", None)
        if not ident or freq is None or catalog is None:
            return
        try:
            # VOR/ILS channels are 50 kHz apart; 10 kHz stays inside one.
            near = [
                e for e in catalog.near(int(freq), tolerance_hz=10_000)
                if e.source == "faa_nasr" and e.station_id
            ]
        except Exception:
            logger.exception("navaid ident cross-check failed")
            return
        listed = sorted({e.station_id.replace("-", "").upper() for e in near})
        fields["ident_catalog_ids"] = listed
        fields["ident_catalog_match"] = (ident.upper() in listed) if listed else None

    def _publish_decoder_message(self, msg: dict) -> None:
        if msg.get("type") == "decoder_event":
            self._navaid_ident_check(msg)
        spawn(self.broadcaster.publish(msg))
        # Bearing/DDM and other instrument reports are useful output, not
        # decoded identity evidence. Keep them visible without confirming a
        # station, beacon or observation claim from receiver measurements.
        if msg.get("type") == "decoder_event" and (msg.get("event") or {}).get("kind") in {
            "measurement",
            "sync",
        }:
            return
        # fan-in: every decoder_event also reaches Survey so it can
        # confirm a matching label (note_decode_event's own contract says it
        # never raises, but this runs from DecoderManager's synchronous
        # publish callback -- guard anyway so a bug here can never take the
        # publish down with it).
        if msg.get("type") == "decoder_event" and self.survey is not None:
            try:
                self.survey.note_decode_event(msg["event"])
            except Exception:
                logger.exception("survey: note_decode_event failed")
        # fan-in: every decoder_event also reaches the beacon monitor, same
        # shape/guard as the Survey fan-in above.
        event = msg.get("event") or {}
        is_replay = bool(event.get("analysis_run_id")) or str(
            event.get("source_id") or ""
        ).startswith("replay:")
        if msg.get("type") == "decoder_event" and self.beacons is not None and not is_replay:
            try:
                self.beacons.note_decode_event(msg["event"])
            except Exception:
                logger.exception("beacons: note_decode_event failed")
        # fan-in: every stored decode event also reaches the observations
        # recorder, same shape/guard as the Survey/beacons fan-ins above.
        if msg.get("type") == "decoder_event":
            try:
                self.obs_recorder.on_decode_event(msg.get("event") or {})
            except Exception:
                logger.exception("observations: on_decode_event failed")
        if msg.get("type") == "decoder_event":
            event = msg.get("event") or {}
            if event.get("kind") == "call" and (event.get("fields") or {}).get("wav"):
                self._trim_calls_retention()
            if event.get("kind") == "image" and event.get("image_path"):
                self._trim_fax_retention()

    def _trim_calls_retention(self) -> None:
        """Delete the oldest per-call WAVs beyond decoder.calls_keep --
        mirrors DecodeEventRepo.trim's "oldest beyond limit" semantics, but
        for files on disk: a call event's DB row is kept regardless, only the
        WAV bytes are reclaimed once there are more than calls_keep of them."""
        calls_dir = self._resolve_calls_dir(self.settings)
        keep = self.settings.decoder.calls_keep
        try:
            wavs = sorted(calls_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in wavs[: max(0, len(wavs) - keep)]:
            try:
                path.unlink()
            except OSError:
                logger.warning("decoder: failed to trim call wav %s", path)

    def _trim_fax_retention(self) -> None:
        """Delete the oldest fax PNGs beyond decoder.fax_keep -- mirrors
        _trim_calls_retention above, for wefax's progressive images."""
        fax_dir = self._resolve_fax_dir(self.settings)
        keep = self.settings.decoder.fax_keep
        try:
            pngs = sorted(fax_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in pngs[: max(0, len(pngs) - keep)]:
            try:
                path.unlink()
            except OSError:
                logger.warning("decoder: failed to trim fax png %s", path)

    def decoder_image_path(self, event_id: str) -> Path:
        """GET /api/decoder/images/{event_id}.png: resolves and validates the
        stored image-kind event's PNG path -- mirrors decoder_call_wav_path
        above. A missing event, an event with no image_path, a resolved path
        outside the fax dir, or a file that isn't actually there all raise
        the same 404."""
        event = self.decode_events.get(event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        image_path = event.get("image_path")
        if not image_path:
            raise HTTPException(404, "event not found")
        fax_dir = self._resolve_fax_dir(self.settings).resolve()
        candidate = Path(image_path)
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (fax_dir / candidate).resolve()
        )
        try:
            resolved.relative_to(fax_dir)
        except ValueError:
            raise HTTPException(404, "event not found") from None
        if not resolved.is_file():
            raise HTTPException(404, "event not found")
        return resolved

    def decoder_call_wav_path(self, event_id: str) -> Path:
        """GET /api/decoder/calls/{event_id}.wav: resolves and validates
        the stored call event's WAV path. A missing event, an event with no
        fields.wav, a resolved path outside the calls dir (a traversal
        attempt learns nothing more than "not found"), or a file that isn't
        actually there all raise the same 404."""
        event = self.decode_events.get(event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        wav = (event.get("fields") or {}).get("wav")
        if not wav:
            raise HTTPException(404, "event not found")
        calls_dir = self._resolve_calls_dir(self.settings).resolve()
        candidate = Path(wav)
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (calls_dir / candidate).resolve()
        )
        try:
            resolved.relative_to(calls_dir)
        except ValueError:
            raise HTTPException(404, "event not found") from None
        if not resolved.is_file():
            raise HTTPException(404, "event not found")
        return resolved

    async def _start_decoder_runtime(self) -> None:
        # The running flag is set only after the await below; two concurrent
        # toggles (double-click, settings PATCH racing startup) would both
        # pass the check and double-start, so start/stop share a lock.
        async with self._decoder_runtime_lock:
            await self._start_decoder_runtime_locked()

    async def _start_decoder_runtime_locked(self) -> None:
        """Bring the decoder manager's own ConfirmedState subscription up --
        mirrors _start_recorder_runtime, gated by the feature flag at startup
        and again live in apply_settings()."""
        if self.decoder is None or self._decoder_running:
            return
        await self.decoder.start()
        self._decoder_running = True

    async def _stop_decoder_runtime(self) -> None:
        async with self._decoder_runtime_lock:
            await self._stop_decoder_runtime_locked()

    async def _stop_decoder_runtime_locked(self) -> None:
        if not self._decoder_running:
            return
        try:
            await self.decoder.stop()
        except Exception:
            logger.exception("decoder: stop failed")
        self._decoder_running = False

    async def _sync_decoder_for_tune(self, signal: Signal, report: RestoreReport) -> None:
        """Start/stop memory-driven decoding to match `signal.decoders` after a
        successful tune. Never raises: a decode failure is logged and folded
        into the tune's own report.detail instead of failing the tune (see the
        design spec's "Starting and stopping")."""
        if self.decoder is None:
            return
        try:
            if signal.decoders:
                if signal.decoder_preset:
                    from msdr.decoder.receiver_preset import PROFILES

                    decoder_settings = self.settings.decoder.model_dump()
                    decoder_settings.update(
                        PROFILES[signal.decoder_preset]["decoder_settings"]
                    )
                    await self.apply_settings({"decoder": decoder_settings})
                await self.decoder.start_decoding(
                    signal.decoders, signal.frequency_hz, "memory", signal.id
                )
            else:
                status = self.decoder.status()
                if status.get("active") and status.get("source") == "memory":
                    await self.decoder.stop_decoding("tuned elsewhere")
        except Exception as exc:
            logger.exception("decoder: start/stop failed during tune")
            note = f"decoder: {exc}"
            report.detail = f"{report.detail}; {note}" if report.detail else note

    # --- signal decoders (P1) ------------------------------------------------

    def _require_decoder(self) -> None:
        # Unlike the recorder's feature gate (404), an unavailable decoder
        # package is a DecoderError -> 409 (app.py handler), same status the
        # manager itself raises when the feature is off (see the API table
        # in the design spec) -- both are "start refused", not "not found".
        if self.decoder is None:
            raise DecoderError("decoder unavailable")

    def decoder_status(self) -> dict | None:
        return self.decoder.status() if self.decoder is not None else None

    def list_decoders(self) -> list[dict]:
        # Session facts come from the live manager status so "installed",
        # "validated" and "what it's doing now" stay three separate answers.
        # describe() may probe binary versions (bounded, cached
        # subprocess calls), so app.py runs this off the event loop.
        return describe_decoders(self.settings.decoder.bin_dirs, session=self.decoder_status())

    def _decoder_prepare_conflicts(self):
        conflicts = [k for k, active in self._owner_active_kinds().items() if active]
        status = self.decoder_status() or {}
        if status.get("active"):
            conflicts.append("single-channel decoding")
        survey = self.survey_status() or {}
        if survey.get("active") or survey.get("replay"):
            conflicts.append("Survey / replay")
        if self.recorder and self.recorder.status().get("state") == "armed":
            conflicts.append("armed recorder")
        return conflicts

    async def _apply_decoder_receiver_props(self, desired):
        from msdr.engine.restore import SetStep

        steps = [
            SetStep(s.name, desired[s.name], s.critical, s.timeout_s)
            for s in RESTORABLE
            if s.name in desired
            and not matches(s.name, self.confirmed.props.get(s.name), desired[s.name])
        ]
        return await self._restore(steps, desired=desired, kind="tune")

    async def decoder_prepare(self, request):
        from msdr.decoder.receiver_preset import receiver_preset
        from msdr.ids import uuid7

        self._require_decoder()
        if not self.adapter.connected or not self.confirmed.props.get("started"):
            raise ValueError("Connect and start the receiver first")
        conflicts = self._decoder_prepare_conflicts()
        if request.token:
            entry = getattr(self, "_decoder_preset", None)
            if not entry or entry["token"] != request.token or entry.get("applied"):
                raise ValueError("Preview expired; prepare the receiver again")
            if conflicts:
                raise ValueError("Stop first: " + ", ".join(conflicts))
            if any(self.confirmed.props.get(k) != v for k, v in entry["before"].items()):
                raise ValueError("Receiver changed; refresh the preview")
            if entry.get("decoder_settings_before") and any(
                getattr(self.settings.decoder, key) != value
                for key, value in entry["decoder_settings_before"].items()
            ):
                raise ValueError("LoRa decoder settings changed; refresh the preview")
            # Keep the undo snapshot even if only part of a hardware change succeeds.
            entry["applied"] = True
            report = await self._apply_decoder_receiver_props(entry["desired"])
            if report.status == "confirmed" and entry.get("decoder_settings_desired"):
                decoder_settings = self.settings.decoder.model_dump()
                decoder_settings.update(entry["decoder_settings_desired"])
                await self.apply_settings({"decoder": decoder_settings})
            return {**entry, "report": restore_report_to_dict(report), "conflicts": []}
        entry = receiver_preset(
            request.decoder, request.frequency_hz, self.confirmed.props, request.lna_state,
            profile=getattr(request, "profile", None),
            decoder_settings=self.settings.decoder.model_dump(),
        )
        entry.update(token=uuid7(), conflicts=conflicts)
        # A preview must not replace the previous undo record.
        previous = getattr(self, "_decoder_preset", None)
        if previous and previous.get("applied"):
            self._decoder_preset_undo = previous
        self._decoder_preset = entry
        return entry

    async def decoder_restore(self, token):
        entries = [
            getattr(self, name, None) for name in ("_decoder_preset", "_decoder_preset_undo")
        ]
        entry = next((e for e in entries if e and e["token"] == token and e.get("applied")), None)
        if entry is None:
            raise ValueError("Receiver restore expired")
        conflicts = self._decoder_prepare_conflicts()
        if conflicts:
            raise ValueError("Stop first: " + ", ".join(conflicts))
        report = await self._apply_decoder_receiver_props(entry["before"])
        if report.status == "confirmed":
            if entry.get("decoder_settings_before"):
                decoder_settings = self.settings.decoder.model_dump()
                decoder_settings.update(entry["decoder_settings_before"])
                await self.apply_settings({"decoder": decoder_settings})
            entry["applied"] = False
        return restore_report_to_dict(report)

    async def decoder_reapply(self, token):
        entries = [
            getattr(self, name, None) for name in ("_decoder_preset", "_decoder_preset_undo")
        ]
        entry = next(
            (e for e in entries if e and e["token"] == token and not e.get("applied")),
            None,
        )
        if entry is None:
            raise ValueError("Receiver redo expired")
        conflicts = self._decoder_prepare_conflicts()
        if conflicts:
            raise ValueError("Stop first: " + ", ".join(conflicts))
        if any(self.confirmed.props.get(k) != v for k, v in entry["before"].items()):
            raise ValueError("Receiver changed since undo; redo is no longer safe")
        if entry.get("decoder_settings_before") and any(
            getattr(self.settings.decoder, key) != value
            for key, value in entry["decoder_settings_before"].items()
        ):
            raise ValueError("LoRa decoder settings changed since undo; redo is no longer safe")
        entry["applied"] = True  # keep restore available after a partial apply
        report = await self._apply_decoder_receiver_props(entry["desired"])
        if report.status == "confirmed" and entry.get("decoder_settings_desired"):
            decoder_settings = self.settings.decoder.model_dump()
            decoder_settings.update(entry["decoder_settings_desired"])
            await self.apply_settings({"decoder": decoder_settings})
        return restore_report_to_dict(report)

    async def decoder_start(self, decoders: list[str], frequency_hz: int | None = None) -> dict:
        self._require_band_scan_idle()
        self._require_decoder()
        if frequency_hz is not None:
            # The signal panel's DECODE names the signal it shows; with the
            # survey off that used to decode whatever was tuned (review
            # 2026-09-26: a find selected while listening elsewhere).
            target = int(frequency_hz)
            report = await self.tune_frequency(target)
            vfo = self.confirmed.props.get("device_vfo_frequency")
            if report.status != "confirmed" or not matches("device_vfo_frequency", vfo, target):
                detail = f": {report.detail}" if report.detail else ""
                raise DecoderError(
                    f"tune to {target} Hz was not confirmed (status: {report.status}){detail}"
                )
        else:
            vfo = self.confirmed.props.get("device_vfo_frequency")
        if vfo is None:
            raise DecoderError("no VFO frequency: not connected")
        return await self.decoder.start_decoding(decoders, int(vfo), "manual")

    async def decoder_stop(self) -> dict:
        self._require_decoder()
        return await self.decoder.stop_decoding("user")

    def decoder_events(
        self,
        *,
        limit: int = 200,
        decoder: str | None = None,
        q: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        return self.decode_events.list(limit=limit, decoder=decoder, q=q, since=since)

    def decoder_clear(self) -> int:
        return self.decode_events.clear()

    def decoder_presets_install(self) -> dict:
        """Create the spec's preset memories (idempotent on frequency +
        decoder set) via the existing signal-creation path."""
        existing = self.signals.list(limit=_ALL_SIGNALS_LIMIT)
        created = 0
        skipped = 0
        for name, freq, demod, bw, decoders in _DECODER_PRESETS:
            wanted = set(decoders)
            if any(s.frequency_hz == freq and set(s.decoders) == wanted for s in existing):
                skipped += 1
                continue
            signal = Signal(
                name=name,
                frequency_hz=freq,
                demodulator=demod,
                filter_bandwidth_hz=bw,
                decoders=list(decoders),
            )
            self.create_signal(signal)
            existing.append(signal)
            created += 1
        return {"created": created, "skipped": skipped}

    async def decoder_band_start(
        self,
        decoders: list[str],
        max_channels: int | None = None,
        threshold_db: float | None = None,
    ) -> dict:
        self._require_band_scan_idle()
        self._require_decoder()
        return await self.decoder.start_band(
            decoders, max_channels=max_channels, threshold_db=threshold_db
        )

    async def decoder_band_stop(self) -> dict:
        self._require_decoder()
        return await self.decoder.stop_band("user")

    def decoder_band_save(self, frequency_hz: int, name: str | None = None) -> dict:
        """Save a BAND DECODE channel as an NFM/12.5 kHz memory, carrying the
        band session's current decoder set -- through the same
        signal-creation path decoder_presets_install() uses. Idempotent
        within +-6 kHz (a re-save of the same channel returns the existing
        memory rather than creating a duplicate)."""
        self._require_decoder()
        band = (self.decoder.status() or {}).get("band") or {}
        return self._save_identified_channel(
            frequency_hz, name=name, decoders=list(band.get("decoders", []))
        )

    def _save_identified_channel(
        self, frequency_hz: int, *, name: str | None, decoders: list[str], notes: str = ""
    ) -> dict:
        """Shared by decoder_band_save (P2) and survey_save: an
        NFM/12.5 kHz memory, idempotent within +-6 kHz of an existing memory
        (a re-save of the same channel returns the existing one unchanged
        rather than creating a near-duplicate)."""
        for signal in self.signals.list(limit=_ALL_SIGNALS_LIMIT):
            if abs(signal.frequency_hz - frequency_hz) <= _BAND_SAVE_TOLERANCE_HZ:
                return signal.model_dump(mode="json")
        signal = Signal(
            name=name or f"{frequency_hz / 1e6:.4f} band decode",
            frequency_hz=frequency_hz,
            demodulator="NFM",
            filter_bandwidth_hz=12_500,
            decoders=list(decoders),
            notes=notes,
        )
        self.create_signal(signal)
        return signal.model_dump(mode="json")
