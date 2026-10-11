"""Survey: span picker + spectrum/waterfall/occupancy engine (S1). Owns a
SpectrumHub subscription and an OccupancyMap; publishes decimated `spectrum`
frames and throttled `survey` status pushes, the way DecoderManager owns an
audio stream and publishes `decoder` status -- runtime subscribe/unsubscribe
on start()/stop(), a background pump while `start_survey()` is active.
"""

import asyncio
import base64
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from msdr import rfml
from msdr.decoder.backends import reap_cancelled
from msdr.decoder.registry import DECODERS
from msdr.radio.protocol import SpectrumFrame
from msdr.survey.analyzer import Analyzer
from msdr.survey.classify import (
    activity_class,
    bandwidth_class,
    bandwidth_class_from_occupied,
)
from msdr.survey.decoder_candidates import decoder_candidates
from msdr.survey.occupancy import OccupancyMap
from msdr.survey.peaks_spectrum import SpectrumPeakTracker, TrackedPeak
from msdr.survey.rds import fm_channel_hz, in_fm_band
from msdr.survey.replay import ReplayError, ReplaySource
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.tasks import spawn

if TYPE_CHECKING:
    from msdr.engine.confirmed import ConfirmedState, StateChanged

logger = logging.getLogger(__name__)

# Measured (docs/research/2026-09-22-sdrconnect-websocket-api.md Sec7): the
# spectrum stream runs ~23 frames/s regardless of settings -- this is only
# used to turn occupancy_windows_s into ring sizes, not a knob.
_MEASURED_FPS = 23
_DEFAULT_REF_LEVEL_DB = -20.0
_DEFAULT_BASE_DB = -145.0
_BIN_LEVELS = 255.0
_BINS = 512
_DEFAULT_SAMPLE_RATES_HZ = (2_000_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000)
# The VFO must stay inside the new span with the same margin the restore
# planner/decoder manager use for "is this frequency in view".
_VFO_MARGIN_HZ = 25_000
_DEFAULT_DIMENSIONS = ["activity", "bandwidth", "bandplan", "catalog"]
_IDENTIFY_CACHE_TTL_S = 60.0
_MAX_LABELS = 64
_RETUNE_SETTLE_S = 0.25  # control/IQ streams have no hardware tune generation
# needs_iq dimensions have no IQ pipeline yet (S3+) -- set_dimensions rejects them.
AVAILABLE_DIMENSIONS = [
    {"id": "activity", "needs_iq": False},
    {"id": "bandwidth", "needs_iq": False},
    {"id": "bandplan", "needs_iq": False},
    {"id": "catalog", "needs_iq": False},
    {"id": "modulation", "needs_iq": True},
    {"id": "symbol_rate", "needs_iq": True},
    {"id": "voice", "needs_iq": True},
    {"id": "rds", "needs_iq": True, "label": "RDS (FM names)"},
]
_DIMENSION_NEEDS_IQ = {d["id"]: d["needs_iq"] for d in AVAILABLE_DIMENSIONS}
# bw_class values wide enough that IQ feature extraction is skipped entirely
# (S2 label only, modulation reported as the {"kind": "wideband"} bypass) --
# same table classify.bandwidth_class can return, mirrored here (and in
# families.py) since Survey needs it for target selection, not just display.
_WIDE_BW_CLASSES = ("wfm", "wide_digital")
# Mirrors analyzer._TARGET_MATCH_HZ: a label this close to an analyzer target
# is that target (see Analyzer.set_targets).
_ANALYZER_TARGET_MATCH_HZ = 6_000.0


def _analyzer_frequency_matches(
    value: float | int | None, frequency_hz: float, tolerance_hz: float
) -> bool:
    return value is not None and abs(float(value) - frequency_hz) <= tolerance_hz


def _analysis_state_for_label(
    frequency_hz: float,
    resolution_hz: float,
    analyzer_status: dict,
    analyzed_at: float | None,
    last_classification: dict | None,
    family: dict | None,
    *,
    now_epoch: float,
) -> dict:
    """Describe analysis freshness without equating scheduler eligibility to work."""
    tolerance_hz = max(resolution_hz, _ANALYZER_TARGET_MATCH_HZ)
    target = next(
        (target for target in analyzer_status.get("targets", [])
         if _analyzer_frequency_matches(target.get("frequency_hz"), frequency_hz, tolerance_hz)),
        None,
    )
    in_progress = _analyzer_frequency_matches(
        analyzer_status.get("analyzing"), frequency_hz, tolerance_hz
    )
    queued = any(
        _analyzer_frequency_matches(value, frequency_hz, tolerance_hz)
        for value in analyzer_status.get("queue", [])
    )
    eligible = any(
        _analyzer_frequency_matches(value, frequency_hz, tolerance_hz)
        for value in analyzer_status.get("candidate_frequencies_hz", [])
    )
    previous_at = analyzed_at or (last_classification or {}).get("at")
    previous_name = (last_classification or {}).get("name") or (family or {}).get("name")
    candidate_count = max(1, int(analyzer_status.get("candidate_count") or 1))
    freshness_s = max(
        120.0, float(analyzer_status.get("dwell_s") or 2.0) * candidate_count * 2
    )
    result_is_recent = analyzed_at is not None and now_epoch - analyzed_at <= freshness_s

    # A completed result remains current while the signal stays eligible,
    # even though a revisit is queued. The scheduler's "analyzed" bit alone
    # is insufficient: only analyzed_at proves an output was actually made.
    if (
        analyzer_status.get("active")
        and (target is not None or eligible)
        and result_is_recent
    ):
        return {
            "state": "current_result",
            "at": analyzed_at,
            "refresh_pending": in_progress or queued,
        }
    if analyzer_status.get("active") and (in_progress or queued or eligible):
        return {
            "state": "waiting",
            "previous_at": previous_at,
            "previous_name": previous_name,
        }
    if previous_at is not None:
        return {"state": "historical_result", "at": previous_at, "name": previous_name}
    return {"state": "not_analyzed"}
# Keep a completed dwell across a brief S2 track dropout. The analyzer itself
# retains the target for 30 seconds; use the same upper bound here.
_ANALYZER_RESULT_GRACE_S = 30.0


# Used to keep proposal ids stable across detector-box jitter of more than one
# bin, and to group repeated fragments of one frequency region before the
# survey's proposal cap is applied.
_PROPOSAL_OVERLAP_MIN = 0.5


def _box_overlap_ratio(low_a: float, high_a: float, low_b: float, high_b: float) -> float:
    overlap = max(0.0, min(high_a, high_b) - max(low_a, low_b))
    if overlap <= 0:
        return 0.0
    union = (high_a - low_a) + (high_b - low_b) - overlap
    return overlap / union if union > 0 else 0.0


def _aggregate_ml_frequency_regions(candidates: list[dict], *, limit: int = 16) -> list[dict]:
    """Group repeated detector fragments while retaining their time evidence.

    A group uses the strongest box as its frequency representative. Frequency
    overlap IoU and a minimum width ratio prevent a broad box from swallowing
    a distinct narrow channel. Member intervals remain separate; the group
    never claims continuous activity between fragments.
    """
    groups: list[dict] = []
    ordered = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)
    for candidate in ordered:
        low, high = candidate.get("low_hz"), candidate.get("high_hz")
        if (not isinstance(low, (int, float)) or not isinstance(high, (int, float))
                or not math.isfinite(low) or not math.isfinite(high) or not 0 < low < high):
            continue
        evidence = candidate.get("evidence") or {}
        class_id = evidence.get("class_id") if isinstance(evidence, dict) else None
        match = None
        for group in groups:
            representative = group["candidate"]
            rep_low, rep_high = representative["low_hz"], representative["high_hz"]
            rep_evidence = representative.get("evidence") or {}
            rep_class = rep_evidence.get("class_id") if isinstance(rep_evidence, dict) else None
            width_ratio = min(high - low, rep_high - rep_low) / max(high - low, rep_high - rep_low)
            if class_id == rep_class and width_ratio >= 0.5 and _box_overlap_ratio(
                low, high, rep_low, rep_high
            ) >= _PROPOSAL_OVERLAP_MIN:
                match = group
                break
        if match is None:
            intervals = []
            interval_count = 0
            if isinstance(candidate.get("start_s"), (int, float)) and isinstance(
                candidate.get("end_s"), (int, float)
            ):
                intervals = [{"start_s": candidate["start_s"], "end_s": candidate["end_s"]}]
                interval_count = 1
            groups.append({"candidate": candidate, "intervals": intervals,
                           "interval_count": interval_count, "member_count": 1})
            continue
        match["member_count"] += 1
        start, end = candidate.get("start_s"), candidate.get("end_s")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            match["interval_count"] += 1
            # Bound live status evidence for pathological detector output.
            if len(match["intervals"]) < 32:
                match["intervals"].append({"start_s": start, "end_s": end})
    selected = []
    for group in groups[:limit]:
        representative = dict(group["candidate"])
        representative["member_count"] = group["member_count"]
        representative["time_interval_count"] = group["interval_count"]
        if group["intervals"]:
            representative["time_intervals"] = group["intervals"]
        selected.append(representative)
    return selected


def _label_id(track: TrackedPeak) -> str:
    """A label's id: "p" + the tracker's track number -- opaque and stable
    for the life of the signal (the "p" prefix kept from the old f"p{kHz}"
    ids, which followed a jittering centroid; see the label loop)."""
    return "p" + track.id.removeprefix("t")


# "voice" is S4, NFM-class only (spec: digital voice/data on other classes
# has no discriminator structure resembling speech either way).
# Voice results are shown for narrowband labels: "nfm", and "unresolved" --
# at wide spans (19.5 kHz bins at 10 MS/s) no narrow signal ever resolves to
# "nfm", so voice was never shown there (live 2026-09-26, 2 m repeaters).
_VOICE_BW_CLASSES = ("nfm", "unresolved")
# Dimensions served by the narrowband round-robin (modulation/symbol-rate/
# voice analysis); "rds" alone runs only the RDS dwells.
_NARROWBAND_DIMENSIONS = ("modulation", "symbol_rate", "voice")
# Upper bound on RDS dwell targets (an 8-10 MHz span holds ~20-40 stations).
_MAX_RDS_TARGETS = 40
# A weaker WFM label within this distance of a stronger label's FM channel
# centre is a side peak of that station (e.g. 100.64 and 100.77 MHz around
# 100.7), not a station of its own -- less than the 100 kHz grid step, with
# room for the ~15 kHz label-centroid error of a real neighbour.
_RDS_SIDE_PEAK_HZ = 80_000
# Profile bins this close to the strongest one locate the FM carrier (see
# Survey._station_hz); HD Radio sidebands sit ~17 dB lower.
_RDS_TOP_DB = 3.0
# S5: how long a confirmed identity stays attached to its label_id (keyed on
# frequency, not track id -- see note_decode_event) after the peak was last
# actually visible in a labels pass; a track TTLs out of the tracker itself
# well before this (label_ttl_s, default 120s), so this is what lets
# "confirmed" survive a peak going briefly quiet and coming back.
_CONFIRMED_TTL_S = 600.0


class SurveyError(RuntimeError):
    """Feature disabled or a bad request -- AppService maps this to 409."""


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _dump(item: object) -> dict:
    if isinstance(item, dict):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return dict(item)  # type: ignore[arg-type]


class Survey:
    def __init__(
        self,
        *,
        adapter,
        hub: SpectrumHub,
        settings_getter: Callable[[], object],
        publish: Callable[[dict], None],
        confirmed_state: "ConfirmedState",
        feature_enabled: Callable[[], bool],
        identify: Callable[[int, int], list[dict]] | None = None,
        # docs/allocations.md: frequency -> compact allocation summary
        # ({lo_hz, hi_hz, jurisdiction, status, services}) or None. The
        # service passes AppService.allocation_summary_at; cached per band
        # here so labels sharing a band cost one lookup.
        allocation: Callable[[int], dict | None] | None = None,
        iq_stream_factory: Callable[[], object] | None = None,
        decode_start: Callable[[int, list[str]], Awaitable[dict]] | None = None,
        decode_stop: Callable[[int], Awaitable[dict]] | None = None,
        # S2 observations hook: called with the current labels list at
        # discrete boundaries only (a track appears/retires, a decode
        # confirm, an analyzer/RDS result landing, catalog/bandplan
        # resolving) -- never from the per-frame pump loop itself. The
        # service passes msdr.observations.recorder.ObservationRecorder.
        # sync_labels, which accepts exactly this shape.
        on_labels_changed: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._hub = hub
        self._settings_getter = settings_getter
        self._publish = publish
        self._confirmed = confirmed_state
        self._feature_enabled = feature_enabled
        self._identify = identify
        self._allocation = allocation
        # None (Stream B not wired up, or a caller that never passes it --
        # every existing test) keeps needs_iq dimensions rejected exactly
        # like before; set_dimensions is the only thing that checks this.
        self._iq_stream_factory = iq_stream_factory
        # IQ-recording replay (msdr.survey.replay): while set, the survey's
        # span, spectrum frames and analyzer IQ all come from the file.
        self._replay: ReplaySource | None = None
        # S5 decode handoff (-> DecoderManager.band_pin/band_unpin); None
        # keeps decode()/undecode() raising SurveyError, same "not wired up
        # yet" default as iq_stream_factory above.
        self._decode_start = decode_start
        self._decode_stop = decode_stop
        # S2 observations hook (constructor kwarg or set_on_labels_changed).
        self._on_labels_changed = on_labels_changed
        # label_id (= tracker track id) -> {"confirmed": {...},
        # "last_seen_mono": float}. A track outlives quiet gaps by label_ttl_s
        # (120 s), so a bursty signal keeps its confirm between bursts;
        # entries expire after _CONFIRMED_TTL_S unseen.
        # (named _confirmed_labels, not _confirmed -- that name is already
        # ConfirmedState's own subscription handle above.)
        self._confirmed_labels: dict[str, dict] = {}
        # Frequencies (Hz) the decoder currently has a pinned channel on,
        # refreshed only from what decode()/undecode() hand back (the
        # authoritative decoder-side state) -- see _update_decoding.
        self._decoding: set[int] = set()
        self._analyzer: Analyzer | None = None
        self._analyzer_start_task: asyncio.Task | None = None
        self._analyzer_stop_tasks: set[asyncio.Task] = set()
        self._ml_update_lock = asyncio.Lock()
        # [{"frequency_hz": int, "fields": {...}}, ...] -- pruned back to
        # only entries that matched a label on the last _build_labels() pass
        # (see _find_analyzer_result), which is what gives "cleared when a
        # label leaves" for free, no separate TTL bookkeeping needed.
        self._analyzer_results: list[dict] = []
        # RDS results ({"frequency_hz", "fields": {"rds": ..., "analyzed_at"}}),
        # kept apart from the narrowband ones and pruned the same way.
        self._rds_results: list[dict] = []

        self._queue: asyncio.Queue[StateChanged] | None = None
        self._consumer_task: asyncio.Task | None = None

        self._center_hz: int | None = None
        self._sample_rate_hz: int | None = None
        self._vfo_hz: int | None = None

        self._active = False
        self._since: float | None = None
        self._hub_queue: asyncio.Queue | None = None
        self._pump_task: asyncio.Task | None = None

        self._occupancy = OccupancyMap()
        self._tracker = SpectrumPeakTracker(dc_exclude_bins=-1)
        self._latest_peaks: list[TrackedPeak] = []
        # Track ids seen on the last pump pass, so _pump can tell "a track
        # appeared/retired" (an id set change) apart from "same tracks, new
        # frame" -- the only two things the tracker's own output tells us.
        self._known_track_ids: set[str] = set()
        # Set by _identify_for_label on a cache miss, consumed by
        # _build_labels right after -- lets "catalog/bandplan applied" fire
        # without _build_labels calling itself (see _notify_labels_changed).
        self._identify_refreshed = False
        self._identify_cache: dict[str, tuple[float, list[dict]]] = {}
        # (lo_hz, hi_hz) -> allocation summary, see _allocation_for_label.
        self._allocation_cache: dict[tuple[int, int], dict | None] = {}
        # Tracker ids of labels recognised on the last status pass (family,
        # RDS name or confirm) -- exempt from label_ttl_s, see status().
        self._recognized_ids: set[str] = set()
        # Bumped whenever everything learned is thrown away (span change,
        # clear(), a new start): status' "generation", so the UI drops its own
        # per-label memory (sticky table rows) instead of showing all of it as
        # just-gone -- track ids restart at t0 after a reset, too.
        self._generation = 0
        self._rfml_results: dict[str, dict] = {}
        self._rfml_received: dict[str, float] = {}
        self._last_detector_result = None
        # ml_proposals(): id -> (low_hz, high_hz) of the last round's boxes,
        # so a new detector box that overlaps one keeps its id instead of
        # minting a fresh one on every bit of detector jitter (see
        # _box_overlap_ratio). Plus a cache of the computed proposal list,
        # keyed on the detector result object's identity -- ml_proposals()
        # is called several times per spectrum frame for the same result.
        self._ml_proposal_boxes: dict[str, tuple[float, float]] = {}
        self._ml_proposal_seq = 0
        self._ml_proposal_cache_key: int | None = None
        self._ml_proposal_cache: list[dict] = []
        self._units_per_db = _BIN_LEVELS / 125.0
        settings = settings_getter()
        self._dimensions = list(getattr(settings, "dimensions", _DEFAULT_DIMENSIONS))
        self._ref_level_db = _DEFAULT_REF_LEVEL_DB
        self._base_db = _DEFAULT_BASE_DB

        self._frames_total = 0
        self._frame_seq = 0
        self._usable_spectrum = 0
        self._dsp_completed = 0
        self._latest_spectrum: SpectrumFrame | None = None
        self._retune_blocked = False
        self._accept_after_mono = 0.0
        self._ignore_managed_echo_until = 0.0
        self._last_spectrum_publish_at = 0.0
        self._last_survey_publish_at = float("-inf")

    # -- runtime (subscribe/unsubscribe ConfirmedState, pattern: decoder
    # manager) -- tracks the device span continuously, independent of
    # whether a survey session is active, so status()/set_span() always have
    # a current center/rate/vfo to work from. ---------------------------

    async def start(self) -> None:
        self._queue = self._confirmed.subscribe()
        props = getattr(self._confirmed, "props", {})
        self._center_hz = _as_int(props.get("device_center_frequency"))
        self._sample_rate_hz = _as_int(props.get("device_sample_rate"))
        self._vfo_hz = _as_int(props.get("device_vfo_frequency"))
        self._consumer_task = asyncio.create_task(self._consume_state())

    async def stop(self) -> None:
        if self._active:
            await self.stop_survey()
        if self._replay is not None:
            replay, self._replay = self._replay, None
            await replay.stop()
        self._stop_analyzer()
        if self._analyzer_stop_tasks:
            await asyncio.gather(*self._analyzer_stop_tasks)
        if self._consumer_task is not None and self._consumer_task is not asyncio.current_task():
            self._consumer_task.cancel()
            await reap_cancelled(self._consumer_task)
            self._consumer_task = None
        if self._queue is not None:
            self._confirmed.unsubscribe(self._queue)
            self._queue = None

    async def _consume_state(self) -> None:
        assert self._queue is not None
        while True:
            sc = await self._queue.get()
            if self._replay is not None:
                continue  # the span is the file's; the radio's moves don't apply
            if sc.property in ("device_center_frequency", "device_sample_rate"):
                if self._retune_blocked:
                    continue
                if time.monotonic() < self._ignore_managed_echo_until:
                    # A queued event can outlive a multi-property restore.
                    # ConfirmedState.props is its latest known state, so a
                    # historical echo is stale only if a newer confirmation
                    # has superseded it. A real manual tune back to the old
                    # value remains valid and must still reset the survey.
                    latest = getattr(self._confirmed, "props", {}).get(sc.property)
                    if latest is not None and _as_int(sc.new) != _as_int(latest):
                        continue
            if sc.property == "device_center_frequency":
                center_hz = _as_int(sc.new)
                if center_hz == self._center_hz:
                    continue  # ConfirmedState emits own no-op echoes too.
                self._center_hz = center_hz
                self._reset_span_state()
            elif sc.property == "device_sample_rate":
                sample_rate_hz = _as_int(sc.new)
                if sample_rate_hz == self._sample_rate_hz:
                    continue
                self._sample_rate_hz = sample_rate_hz
                self._reset_span_state()
            elif sc.property == "device_vfo_frequency":
                self._vfo_hz = _as_int(sc.new)
                # A receiver tune must take effect before the next spectrum
                # frame; sparse/paused spectrum delivery must not leave the
                # old channel ahead of the one the listener selected.
                if self._analyzer is not None:
                    self._analyzer.set_targets(
                        self._analyzer_targets(), priority_frequency_hz=self._tuned_analyzer_hz()
                    )
            else:
                continue
            if self._active:
                self._publish_survey(force=True)

    # -- replay (msdr.survey.replay) -------------------------------------------

    def _iq_factory(self):
        """The analyzer's IQ source: the replay while one runs, else live."""
        if self._replay is not None:
            return self._replay.iq_stream
        return self._iq_stream_factory

    async def start_replay(self, path: str, loop: bool = True) -> dict:
        """Survey an IQ recording instead of the radio: the live survey (and
        its analyzer) stop, everything learned is reset, and the survey runs
        on the file's span, paced in real time."""
        if not self._feature_enabled():
            raise SurveyError("survey feature disabled")
        try:
            source = ReplaySource(path, loop=loop)
        except (ReplayError, OSError, ValueError, KeyError) as exc:
            raise SurveyError(f"can't replay {path}: {exc}") from exc
        await self._leave_current_source()
        self._replay = source
        self._center_hz, self._sample_rate_hz = source.center_hz, source.sample_rate_hz
        self._vfo_hz = None
        # S3: a multi-run recording changes centre/rate mid-replay; the span
        # (occupancy, tracker, identify cache, analyzer binding) must follow
        # the run, otherwise labels from the old span would be read against
        # the new frequencies.
        source.add_listener(self._on_replay_event)
        await source.start()
        self._reset_span_state()
        return await self.start_survey()

    def _on_replay_event(self, event) -> None:
        # "generation" fires for every discontinuity (seek, loop, gap,
        # retune) -- always last when a "run" precedes it (ReplaySource
        # emits "run" then "generation" from the same bump), so acting only
        # here resets state exactly once per discontinuity, whether or not
        # the span (and so the rate-dependent DSP) actually moved.
        if event.kind != "generation" or self._replay is None or event.descriptor is None:
            return
        desc = event.descriptor
        span_changed = (desc.center_hz, desc.sample_rate_hz) != (
            self._center_hz,
            self._sample_rate_hz,
        )
        if span_changed:
            self._center_hz, self._sample_rate_hz = desc.center_hz, desc.sample_rate_hz
        self._reset_span_state()
        if span_changed:
            # The analyzer is bound to the source rate: rebuild it for the new run.
            self._stop_analyzer()
            self._sync_analyzer()
        self._publish_survey(force=True)

    async def stop_replay(self) -> dict:
        """Back to the live radio (survey left stopped -- START resumes it)."""
        if self._replay is None:
            return self.status()
        await self._leave_current_source()
        await self._refresh_span_from_adapter()
        self._reset_span_state()
        self._publish_survey(force=True)
        return self.status()

    async def _leave_current_source(self) -> None:
        if self._active:
            await self.stop_survey()
        # The analyzer is bound to its IQ source; the next start builds a new
        # one from _iq_factory().
        self._stop_analyzer()
        if self._replay is not None:
            replay, self._replay = self._replay, None
            await replay.stop()

    def replay_control(
        self, playing: bool | None = None, position_s: float | None = None, loop: bool | None = None
    ) -> dict:
        if self._replay is None:
            raise SurveyError("no replay running")
        if playing is not None:
            self._replay.playing = playing
        if loop is not None:
            self._replay.loop = loop
        if position_s is not None:
            self._replay.seek(position_s)
        self._publish_survey(force=True)
        return self.status()

    def clear(self) -> dict:
        """Forget everything learned on this span -- occupancy, tracked
        peaks/labels, confirms, analyzer and RDS results -- and start fresh
        without moving (same reset as a span change)."""
        self._reset_span_state()
        self._publish_survey(force=True)
        return self.status()

    def begin_retune(self) -> None:
        """Close the survey boundary before the first hardware tune command.

        SpectrumFrame carries an arrival time but no device tune generation,
        so old queued frames are drained and post-tune frames must settle.
        """
        if self._replay is not None:
            return
        if self._retune_blocked:
            return
        self._retune_blocked = True
        self._reset_span_state()

    def end_retune(
        self, center_hz: int | None = None, sample_rate_hz: int | None = None
    ) -> None:
        """Release a hardware tune boundary after all writes complete.

        Passing the final confirmed span avoids reading an intermediate
        ConfirmedState echo as the completed target. This also releases the
        gate after a failed restore, while still requiring fresh frames.
        """
        if self._replay is not None:
            return
        if center_hz is not None:
            self._center_hz = int(center_hz)
        if sample_rate_hz is not None:
            self._sample_rate_hz = int(sample_rate_hz)
        self._retune_blocked = False
        self._ignore_managed_echo_until = time.monotonic() + 1.0
        self._accept_after_mono = time.monotonic() + _RETUNE_SETTLE_S
        self._flush_spectrum_queue()
        if (self._analyzer is not None and self._center_hz is not None
                and self._sample_rate_hz is not None):
            analyzer = self._analyzer
            spawn(analyzer.retune(
                self._center_hz, self._sample_rate_hz,
                expected_generation=analyzer._retune_gen,
            ))
        elif (self._analyzer is None and self._center_hz is not None
                and self._sample_rate_hz is not None):
            self._sync_analyzer()

    def _flush_spectrum_queue(self) -> None:
        if self._hub_queue is None:
            return
        while True:
            try:
                self._hub_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _reset_span_state(self) -> None:
        """A moved span makes every tracked frequency, and every bandplan/
        catalog lookup keyed on the old frequencies, meaningless -- occupancy,
        the peak tracker and the identify cache all restart clean."""
        self._occupancy.reset()
        self._tracker.reset()
        self._identify_cache = {}
        self._allocation_cache = {}
        self._recognized_ids = set()
        self._generation += 1
        self._usable_spectrum = 0
        self._dsp_completed = 0
        self._latest_spectrum = None
        self._flush_spectrum_queue()
        if not self._retune_blocked and self._replay is None:
            self._accept_after_mono = time.monotonic() + _RETUNE_SETTLE_S
        self._rfml_results.clear()
        self._rfml_received.clear()
        self._last_detector_result = None
        self._ml_proposal_boxes = {}
        self._ml_proposal_seq = 0
        self._ml_proposal_cache_key = None
        self._ml_proposal_cache = []
        if self._analyzer is not None:
            self._analyzer.begin_retune()
        # Track ids restart at t0 with the tracker: an old confirm must not
        # attach to whatever new signal gets its id.
        self._confirmed_labels = {}
        self._latest_peaks = []
        self._known_track_ids = set()
        self._analyzer_results = []
        self._rds_results = []
        span_known = self._center_hz is not None and self._sample_rate_hz is not None
        if self._analyzer is not None and span_known and not self._retune_blocked:
            # Fire-and-forget like start/stop below: retune is async (rebuilds
            # the channelizer), _reset_span_state is called from sync
            # contexts (set_span, the ConfirmedState consumer), and nothing
            # here needs to block on it -- set_targets() on the next pump
            # tick re-derives targets for the new span anyway.
            analyzer = self._analyzer
            spawn(analyzer.retune(
                self._center_hz, self._sample_rate_hz,
                expected_generation=analyzer._retune_gen,
            ))
        elif self._analyzer is None and span_known and not self._retune_blocked:
            # IQ dimensions selected while the span was unknown (see
            # start_survey): start the analyzer now that there is one.
            self._sync_analyzer()

    # -- control ----------------------------------------------------------

    async def start_survey(self) -> dict:
        if not self._feature_enabled():
            raise SurveyError("survey feature disabled")
        if self._active:
            return self.status()

        settings = self._settings_getter()
        threshold_db = getattr(settings, "threshold_db", 8.0)
        peak_threshold_db = getattr(settings, "peak_threshold_db", 10.0)
        label_ttl_s = getattr(settings, "label_ttl_s", 120)
        windows_s = tuple(getattr(settings, "occupancy_windows_s", (60, 600)))
        self._recognized_ids = set()

        if self._replay is not None:
            self._center_hz = self._replay.center_hz
            self._sample_rate_hz = self._replay.sample_rate_hz
            self._vfo_hz = None
            self._ref_level_db = self._replay.ref_level_db
            self._base_db = self._replay.base_db
        else:
            await self._refresh_span_from_adapter()
            self._ref_level_db = await self._read_level("spectrum_ref_level", _DEFAULT_REF_LEVEL_DB)
            self._base_db = await self._read_level("spectrum_base", _DEFAULT_BASE_DB)
        db_range = self._ref_level_db - self._base_db
        units_per_db = _BIN_LEVELS / db_range if db_range > 0 else _BIN_LEVELS / 125.0
        self._units_per_db = units_per_db

        self._occupancy = OccupancyMap(
            windows_frames=(
                round(windows_s[0] * _MEASURED_FPS),
                round(windows_s[1] * _MEASURED_FPS),
            ),
            threshold_units=round(threshold_db * units_per_db),
        )
        self._tracker = SpectrumPeakTracker(
            threshold_units=round(peak_threshold_db * units_per_db),
            dc_exclude_bins=(2 if getattr(settings, "center_exclusion_enabled", False) else -1),
            ttl_s=float(label_ttl_s),
            history_s=float(windows_s[1]) if len(windows_s) > 1 else 600.0,
        )
        self._latest_peaks = []
        self._known_track_ids = set()
        self._identify_cache = {}
        self._allocation_cache = {}
        # The new tracker restarts ids at t0: a confirm left over from the
        # previous run would attach to whatever new signal gets its id (review
        # 2026-09-26: an AIS confirm reappeared on an unrelated 154 MHz label
        # after STOP -> START). Same reset as a span change.
        self._confirmed_labels = {}
        self._generation += 1

        self._hub_queue = (
            self._replay.acquire_spectrum() if self._replay is not None else self._hub.acquire()
        )
        self._active = True
        self._since = time.time()
        self._frames_total = 0
        self._usable_spectrum = 0
        self._dsp_completed = 0
        self._latest_spectrum = None
        self._last_spectrum_publish_at = 0.0
        self._pump_task = asyncio.create_task(self._pump())
        # Dimensions persisted in settings are restored at construction, before
        # the span is known -- _start_analyzer bailed out then and nothing
        # retried it, so after a restart the analyzer stayed off (no IQ cells,
        # no RDS, and no START ANALYZER button since IQ dims were selected).
        self._sync_analyzer()
        self._publish_survey(force=True)
        return self.status()

    async def stop_survey(self) -> dict:
        if not self._active:
            return self.status()
        self._active = False
        self._stop_analyzer()
        if self._analyzer_stop_tasks:
            await asyncio.gather(*self._analyzer_stop_tasks)
        if self._pump_task is not None:
            self._pump_task.cancel()
            await reap_cancelled(self._pump_task)
            self._pump_task = None
        if self._hub_queue is not None:
            if self._replay is not None:
                self._replay.release_spectrum(self._hub_queue)
            else:
                self._hub.release(self._hub_queue)
            self._hub_queue = None
        self._since = None
        self._publish_survey(force=True)
        return self.status()

    async def _refresh_span_from_adapter(self) -> None:
        """Fill in center/rate/vfo straight from the receiver.

        ConfirmedState only tells us about *changes*, so right after start-up
        (or when the receiver was stopped) the span is unknown until something
        moves -- the live check showed status()["span"] all None. Reading the
        properties directly costs three round-trips and makes status() and
        set_span()'s keep-VFO logic correct from the first call.
        """
        for prop, attr in (
            ("device_center_frequency", "_center_hz"),
            ("device_sample_rate", "_sample_rate_hz"),
            ("device_vfo_frequency", "_vfo_hz"),
        ):
            try:
                value = _as_int(await self._adapter.get(prop))
            except Exception:  # noqa: BLE001 -- offline adapter: keep what we have
                value = None
            if value is not None:
                setattr(self, attr, value)

    async def set_span(self, sample_rate_hz: int, center_hz: int, keep_vfo: bool = True) -> dict:
        if self._replay is not None:
            raise SurveyError("replaying a recording -- stop the replay to change the span")
        if self._vfo_hz is None or self._center_hz is None:
            await self._refresh_span_from_adapter()
        old_vfo = self._vfo_hz
        old_span = (self._center_hz, self._sample_rate_hz)
        if old_span != (center_hz, sample_rate_hz):
            self.begin_retune()
        completed = False
        try:
            await self._adapter.set("device_sample_rate", sample_rate_hz)
            await self._adapter.set("device_center_frequency", center_hz)
            if keep_vfo and old_vfo is not None:
                half_visible = sample_rate_hz / 2 - _VFO_MARGIN_HZ
                if abs(old_vfo - center_hz) <= half_visible:
                    await self._adapter.set("device_vfo_frequency", old_vfo)
                    self._vfo_hz = old_vfo
                # else: leave SDRconnect's own choice, whatever that turns out to be.
            completed = True
        finally:
            # ConfirmedState may lag the writes. On failure, release the gate
            # against the best known span and require fresh frames again.
            if old_span != (center_hz, sample_rate_hz):
                if not completed:
                    await self._refresh_span_from_adapter()
                self.end_retune(
                    center_hz if completed else self._center_hz,
                    sample_rate_hz if completed else self._sample_rate_hz,
                )

        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._publish_survey(force=True)
        return self.status()

    def set_dimensions(self, dimensions: list[str]) -> dict:
        for dim_id in dimensions:
            if dim_id not in _DIMENSION_NEEDS_IQ:
                raise SurveyError(f"unknown dimension: {dim_id}")
            # needs_iq dims are selectable once an iq_stream_factory exists
            # (Stream B wires the real one in) -- without one, still 409 like
            # before, since there's nothing that could ever fill them in.
            if _DIMENSION_NEEDS_IQ[dim_id] and self._iq_factory() is None:
                raise SurveyError(f"dimension needs the IQ analyzer (not available yet): {dim_id}")
        self._dimensions = list(dimensions)
        self._sync_analyzer()
        self._publish_survey(force=True)
        return self.status()

    # -- decode handoff (S5) -------------------------------------------------

    async def decode(self, frequency_hz: int, decoders: list[str] | None) -> dict:
        """Start decoding a label's peak (-> DecoderManager.band_pin via the
        `decode_start` hook). `decoders` defaults to the label's
        `family.suggested_decoders`; raises SurveyError (-> 409) when neither
        is available."""
        if self._decode_start is None:
            raise SurveyError("decode not available")
        if self._replay is not None:
            raise SurveyError("decoding needs the live radio -- stop the replay first")
        chosen = decoders
        if chosen is None:
            label = self._label_by_frequency(int(frequency_hz))
            family = label.get("family") if label is not None else None
            chosen = list(family["suggested_decoders"]) if family else []
            if not chosen:
                raise SurveyError(f"no suggested decoders for {frequency_hz}")
        result = await self._decode_start(int(frequency_hz), chosen)
        self._update_decoding(result)
        return result

    async def undecode(self, frequency_hz: int) -> dict:
        if self._decode_stop is None:
            raise SurveyError("decode not available")
        result = await self._decode_stop(int(frequency_hz))
        self._update_decoding(result)
        return result

    def _update_decoding(self, status: dict) -> None:
        band = status.get("band") if isinstance(status, dict) else None
        channels = (band or {}).get("channels") or []
        self._decoding = {int(c["frequency_hz"]) for c in channels if c.get("source") == "pinned"}
        if (
            isinstance(status, dict)
            and status.get("active")
            and status.get("frequency_hz") is not None
        ):
            self._decoding.add(int(status["frequency_hz"]))

    def set_on_labels_changed(self, callback: Callable[[list[dict]], None] | None) -> None:
        """Setter alternative to the constructor kwarg (S2 contract)."""
        self._on_labels_changed = callback

    def _notify_labels_changed(self, labels: list[dict] | None = None) -> None:
        """Fire the observations hook, guarded (never raises, mirrors
        note_decode_event's own contract). Callers that already have a
        freshly built labels list (see _build_labels) pass it directly, so
        this never calls _build_labels() from inside _build_labels itself."""
        # getattr: a couple of existing tests build a bare Survey via
        # object.__new__() to unit-test one internal method in isolation,
        # bypassing __init__ entirely.
        callback = getattr(self, "_on_labels_changed", None)
        if callback is None:
            return
        try:
            current = labels if labels is not None else self._build_labels()
            combined = [dict(label) for label in current]
            for proposal in self.ml_proposals():
                matches = [label for label in combined if not label.get("ml_only") and
                           proposal["low_hz"] <= label["frequency_hz"] <= proposal["high_hz"]]
                if matches:
                    nearest = min(matches, key=lambda label: abs(
                        label["frequency_hz"] - proposal["frequency_hz"]))
                    # Proposals arrive strongest first. A weaker box covering
                    # the same DSP label must not replace its best evidence.
                    nearest.setdefault("rfml_detection", proposal["rfml_detection"])
                    if proposal.get("rfml") is not None and nearest.get("rfml") is None:
                        nearest["rfml"] = proposal["rfml"]
                else:
                    combined.append(proposal)
            callback(combined)
        except Exception:
            logger.exception("survey: on_labels_changed failed")

    def note_decode_event(self, event: dict) -> None:
        """Called by the service for every `decoder_event` (guarded there,
        never raises): matches `event["frequency_hz"]` to a currently
        tracked peak within +-1 spectrum bin and confirms its label -- first
        sample text is kept, later events on the same label just bump the
        count. Only events from the survey's current source/generation may
        confirm a label; review decode must not leak into live labels, or
        stale replay generations into the current replay."""
        replay = self._replay
        source_id = event.get("source_id")
        generation = event.get("generation")
        if replay is None:
            if event.get("analysis_run_id") or str(source_id or "").startswith("replay:"):
                return
        elif source_id != replay.source_id or generation != replay.generation:
            return

        frequency_hz = event.get("frequency_hz")
        if frequency_hz is None or self._center_hz is None or self._sample_rate_hz is None:
            return
        resolution_hz = self._sample_rate_hz / _BINS
        best_label_id = None
        best_dist = None
        for track in self._latest_peaks:
            freq = self._center_hz - self._sample_rate_hz / 2 + track.centroid_bin * resolution_hz
            dist = abs(freq - frequency_hz)
            if dist <= resolution_hz and (best_dist is None or dist < best_dist):
                best_label_id = _label_id(track)
                best_dist = dist
        if best_label_id is None:
            return

        now = time.monotonic()
        entry = self._confirmed_labels.get(best_label_id)
        if entry is None:
            self._confirmed_labels[best_label_id] = {
                "confirmed": {
                    "decoder": event.get("decoder"),
                    "sample": event.get("text"),
                    "at": event.get("ts") or time.time(),
                    "events": 1,
                    **({"integrity": event["integrity"]} if event.get("integrity") else {}),
                },
                "last_seen_mono": now,
            }
        else:
            confirmation = entry["confirmed"]
            confirmation["events"] += 1
            # A claim represents the strongest evidence observed for this
            # decoder on the signal. A later valid frame upgrades an earlier
            # failed/unchecked event; weaker later output never erases a pass.
            if event.get("decoder") == confirmation.get("decoder") and event.get("integrity"):
                rank = {"unknown": 0, "unavailable": 1, "failed": 2, "passed": 3}
                old = confirmation.get("integrity")
                if old is None or rank.get(event["integrity"], 0) > rank.get(old, 0):
                    confirmation["integrity"] = event["integrity"]
                    # Keep the output that actually supports the strongest
                    # integrity result. The display sample remains the first
                    # event for continuity, while observation claims cite this.
                    if event.get("text"):
                        confirmation["integrity_sample"] = event["text"]
            entry["last_seen_mono"] = now
        self._notify_labels_changed()

    def identity_summary(self, frequency_hz: int) -> dict | None:
        """For SAVE (S5): name/decoders/notes for the label nearest
        `frequency_hz`, or None when nothing is currently tracked there."""
        label = self._label_by_frequency(int(frequency_hz))
        if label is None:
            return None
        confirmed = label.get("confirmed")
        family = label.get("family")
        if confirmed is not None and confirmed["decoder"] == "rds" and family is not None:
            # RDS isn't a DecoderManager decoder -- the station name it
            # decoded (in family) is the identity, with nothing to pin.
            name = family["name"]
            decoders = []
        elif confirmed is not None:
            spec = DECODERS.get(confirmed["decoder"])
            name = spec.label if spec is not None else confirmed["decoder"]
            decoders = [confirmed["decoder"]]
        elif family is not None:
            name = family["name"]
            decoders = list(family.get("suggested_decoders") or [])
        else:
            resolution_hz = (self._sample_rate_hz / _BINS) if self._sample_rate_hz else 0.0
            width_bins = max(1, round(label["width_hz"] / resolution_hz)) if resolution_hz else 1
            bw_class = label.get("bw_class") or bandwidth_class(
                label["width_hz"], resolution_hz, width_bins, center_hz=label["frequency_hz"]
            )
            name = f"{bw_class} {label['activity']['kind']}"
            decoders = []
        return {"name": name, "decoders": decoders, "notes": self._identity_notes(label)}

    def _identity_notes(self, label: dict) -> str:
        """One line per known (non-None/non-empty) dimension on the label."""
        lines = []
        activity = label.get("activity")
        if activity:
            lines.append(f"activity: {activity['kind']} (duty {activity['duty']:.2f})")
        if label.get("bw_class"):
            lines.append(f"bandwidth: {label['bw_class']}")
        bandplan = label.get("bandplan")
        if bandplan:
            lines.append(f"bandplan: {bandplan.get('name')}")
        catalog = label.get("catalog") or []
        if catalog:
            lines.append(f"catalog: {catalog[0].get('name')}")
        modulation = label.get("modulation")
        if modulation:
            kind = modulation.get("kind")
            # S4 wording: a keyed carrier is a presence fact, not a code.
            lines.append(f"modulation: {'keyed carrier' if kind == 'ook' else kind}")
        raw = label.get("symbol_rate_raw_hz")
        snapped = label.get("symbol_rate_snapped_hz")
        if snapped is not None and raw is not None:
            tol = label.get("symbol_rate_tolerance") or 0.03
            lines.append(f"symbol rate: {snapped:.0f} Bd candidate (raw {raw:.0f}, ±{tol:.0%})")
        elif label.get("symbol_rate_hz"):
            lines.append(f"symbol rate: {label['symbol_rate_hz']:.0f} Hz (raw estimate)")
        voice = label.get("voice")
        if voice:
            # "speech-like audio" is the heuristic's claim; "analog NFM voice"
            # needs modulation evidence too (plan §3) -- the family carries
            # that stronger wording when both agree.
            kind = voice.get("kind")
            lines.append(f"voice: {'speech-like audio' if kind == 'voice' else kind}")
        family = label.get("family")
        if family and family.get("next_action"):
            lines.append(f"next: {family['next_action']}")
        return "\n".join(lines)

    def _label_by_frequency(self, frequency_hz: int) -> dict | None:
        """Nearest current label within +-1 spectrum bin of `frequency_hz`."""
        if self._sample_rate_hz is None:
            return None
        resolution_hz = self._sample_rate_hz / _BINS
        best = None
        best_dist = None
        for label in self._build_labels():
            dist = abs(label["frequency_hz"] - frequency_hz)
            if dist <= resolution_hz and (best is None or dist < best_dist):
                best, best_dist = label, dist
        return best

    def _is_decoding(self, frequency_hz: float, resolution_hz: float) -> bool:
        return any(abs(freq - frequency_hz) <= resolution_hz for freq in self._decoding)

    def _prune_confirmed(self, now: float) -> None:
        expired = [
            label_id
            for label_id, entry in self._confirmed_labels.items()
            if now - entry["last_seen_mono"] > _CONFIRMED_TTL_S
        ]
        for label_id in expired:
            del self._confirmed_labels[label_id]

    # -- internals: IQ analyzer lifecycle -----------------------------------

    def _sync_analyzer(self) -> None:
        """Selecting any IQ dimension starts the analyzer; de-selecting all
        of them stops it (spec S3/S4 contract) -- set_dimensions is the only
        caller, so this just reconciles "should it be running" against "is
        it"."""
        wants_iq = self._active and any(_DIMENSION_NEEDS_IQ.get(d, False) for d in self._dimensions)
        wants_iq = wants_iq or (self._active and self._ml_enabled())
        if wants_iq and self._analyzer is None and self._iq_factory() is not None:
            self._start_analyzer()
        elif not wants_iq and self._analyzer is not None:
            self._stop_analyzer()
        elif self._analyzer is not None:
            self._analyzer.set_targets(
                self._analyzer_targets(), priority_frequency_hz=self._tuned_analyzer_hz()
            )
            self._analyzer.set_rds_targets(self._rds_targets())

    async def update_ml(self) -> dict:
        async with self._ml_update_lock:
            prior = self._analyzer
            ml_settings = self._ml_settings()
            if prior is not None:
                if bool(prior._ml is not None) != bool(getattr(ml_settings, "enabled", False)):
                    self._rfml_results.clear()
                    self._rfml_received.clear()
                if bool(prior._ml_detector is not None) != bool(
                    getattr(getattr(ml_settings, "detector", None), "enabled", False)
                ):
                    self._last_detector_result = None
            start_task = self._analyzer_start_task
            if prior is not None and start_task is not None and start_task.done():
                dead = start_task.cancelled() or start_task.exception() is not None
                if dead:
                    # The analyzer's own startup already failed before this
                    # toggle -- awaiting it below would re-raise that stale
                    # error into this unrelated ML request (settings already
                    # persisted by the caller) and leave the dead analyzer
                    # registered instead of applying the new settings (review
                    # 2026-09-28). Discard it and let _sync_analyzer below
                    # start a fresh one with the current settings.
                    self._analyzer = None
                    self._analyzer_start_task = None
                    try:
                        await prior.stop()
                    except Exception:  # best-effort: it never finished starting
                        pass
            self._sync_analyzer()
            analyzer = self._analyzer
            if analyzer is not None:
                start_task = self._analyzer_start_task
                if start_task is not None:
                    await asyncio.shield(start_task)
                if analyzer is self._analyzer:
                    await analyzer.configure_ml(ml_settings)
            self._publish_survey(force=True)
            return self.status()

    def _start_analyzer(self) -> None:
        if self._center_hz is None or self._sample_rate_hz is None:
            return  # span not known yet; retried on the next set_dimensions/span change
        settings = self._settings_getter()
        analyzer = Analyzer(
            iq_stream_factory=self._iq_factory(),
            center_hz=self._center_hz,
            sample_rate_hz=self._sample_rate_hz,
            max_analyzed=getattr(settings, "max_analyzed", 6),
            dwell_s=getattr(settings, "analyze_dwell_s", 2.0),
            min_snr_db=getattr(settings, "analyze_min_snr_db", 8.0),
            on_result=lambda frequency_hz, fields: (
                self._on_analyzer_result(frequency_hz, fields)
                if self._analyzer is analyzer else None
            ),
            on_status=lambda: self._on_analyzer_status() if self._analyzer is analyzer else None,
            want_voice=lambda: "voice" in self._dimensions,
            min_active_s=getattr(settings, "analyze_min_active_s", 0.6),
            want_rds=lambda: "rds" in self._dimensions,
            rds_dwell_s=getattr(settings, "rds_dwell_s", 6.0),
            ml_settings=self._ml_settings(settings),
            ml_context=self._ml_context,
            on_ml_result=lambda metadata, result: (
                self._on_ml_result(metadata, result) if self._analyzer is analyzer else None
            ),
        )
        analyzer.set_targets(
            self._analyzer_targets(), priority_frequency_hz=self._tuned_analyzer_hz()
        )
        analyzer.set_rds_targets(self._rds_targets())
        self._analyzer = analyzer
        # set_dimensions is sync (matches its pre-existing contract/tests),
        # so start -- which awaits the IQ stream's own connect -- runs in
        # the background, the same way BandDecoder's manager kicks off a
        # session from a sync API call.
        self._analyzer_start_task = asyncio.create_task(analyzer.start())

    def _stop_analyzer(self) -> None:
        analyzer, self._analyzer = self._analyzer, None
        self._rfml_results.clear()
        self._rfml_received.clear()
        self._last_detector_result = None
        if analyzer is not None:
            analyzer.invalidate_ml()
        self._analyzer_results = []
        self._rds_results = []
        if analyzer is not None:
            start_task = self._analyzer_start_task
            self._analyzer_start_task = None

            async def finish_stop():
                if start_task is not None:
                    try:
                        await start_task
                    except Exception:  # startup failed; still release any partial stream
                        pass
                await analyzer.stop()

            task = asyncio.create_task(finish_stop())
            self._analyzer_stop_tasks.add(task)
            task.add_done_callback(self._analyzer_stop_tasks.discard)

    def _on_analyzer_result(self, frequency_hz: int, fields: dict) -> None:
        if (getattr(self, "_retune_blocked", False)
                or time.monotonic() < getattr(self, "_accept_after_mono", 0.0)):
            return
        fields = dict(fields)
        result_gen = fields.pop("_analyzer_epoch", None)
        analyzer = getattr(self, "_analyzer", None)
        if result_gen is not None and (analyzer is None or result_gen != analyzer._retune_gen):
            return
        if fields.get("analyzed_at") is not None:
            self._dsp_completed = getattr(self, "_dsp_completed", 0) + 1
        if "rds" in fields:
            # Each RDS result is cumulative (per-station accumulator), so it
            # replaces whatever this station had rather than piling up.
            channel_hz = self._fm_channel(frequency_hz)
            self._rds_results = [
                r for r in self._rds_results if self._fm_channel(r["frequency_hz"]) != channel_hz
            ]
            self._rds_results.append({"frequency_hz": frequency_hz, "fields": fields})
            self._notify_labels_changed()
            return
        # One result per target: labels match the first result within +-1 bin,
        # so appending left every label on its *oldest* result -- usually the
        # initial "waiting for signal" (live 2026-09-26: APRS analyzed, row
        # still "analyzing…"). Replace instead, but never let a later
        # "waiting" dwell (accumulator aged out) hide a real analysis.
        # These are keyed by the analyzer's stable target identity. A whole
        # spectrum bin at a wide span can exceed a 12.5 kHz channel spacing;
        # merging by one-bin distance overwrote a neighbouring channel.
        tolerance_hz = _ANALYZER_TARGET_MATCH_HZ
        same = [
            r
            for r in self._analyzer_results
            if abs(r["frequency_hz"] - frequency_hz) <= tolerance_hz
        ]
        if fields.get("analyzed_at") is None and any(
            r["fields"].get("analyzed_at") is not None for r in same
        ):
            return
        # Keep a useful interpretation separate from the latest measurement.
        # Silence/unknown must not erase it or make it look like a live result.
        previous = next((r["fields"].get("last_classification") for r in same
                         if r["fields"].get("last_classification")), None)
        family_name = (fields.get("family") or {}).get("name")
        voice_kind = (fields.get("voice") or {}).get("kind")
        name = family_name or ({"voice": "Speech-like audio", "data": "Data-like signal"}
                               .get(voice_kind))
        if name and fields.get("analyzed_at"):
            previous = {"name": name, "at": fields["analyzed_at"], "source": "analyzer"}
        if previous:
            fields = {**fields, "last_classification": previous}
        self._analyzer_results = [r for r in self._analyzer_results if r not in same]
        self._analyzer_results.append({
            "frequency_hz": frequency_hz,
            "fields": fields,
            "received_mono": time.monotonic(),
        })
        self._notify_labels_changed()

    def _ml_settings(self, settings=None):
        """Survey's ML settings, or None while ML is dormant (rfml.AVAILABLE):
        with None the analyzer builds no workers, whatever the saved config says."""
        if not rfml.AVAILABLE:
            return None
        settings = settings if settings is not None else self._settings_getter()
        return getattr(settings, "ml", None)

    def _ml_enabled(self) -> bool:
        ml = self._ml_settings()
        return bool(getattr(ml, "enabled", False) or
                    getattr(getattr(ml, "detector", None), "enabled", False))

    def _ml_context(self, frequency_hz: int) -> dict | None:
        """Bind a delayed prediction to a track or fresh proposal in this generation."""
        if self._sample_rate_hz is None or self._center_hz is None:
            return None
        resolution = self._sample_rate_hz / _BINS

        def frequency(track):
            return self._center_hz - self._sample_rate_hz / 2 + track.centroid_bin * resolution

        track = min(self._latest_peaks, key=lambda t: abs(frequency(t) - frequency_hz),
                    default=None)
        tolerance = max(resolution, _ANALYZER_TARGET_MATCH_HZ)
        proposals = [p for p in self.ml_proposals()
                     if p["width_hz"] <= 25_000 and
                     abs(p["frequency_hz"] - frequency_hz) <= tolerance]
        proposal = min(proposals, key=lambda p: abs(p["frequency_hz"] - frequency_hz),
                       default=None)
        track_distance = abs(frequency(track) - frequency_hz) if track is not None else math.inf
        proposal_distance = (abs(proposal["frequency_hz"] - frequency_hz)
                             if proposal is not None else math.inf)
        if (
            track is not None
            and track_distance <= tolerance
            and track_distance <= proposal_distance
        ):
            candidate_id = _label_id(track)
            display_snr_db = track.snr_units / self._units_per_db if self._units_per_db else None
            bandwidth_hz = (track.bin_hi - track.bin_lo + 1) * resolution
        else:
            if proposal is None:
                return None
            candidate_id = proposal["id"]
            display_snr_db = None
            bandwidth_hz = proposal["width_hz"]
        return {
            "candidate_id": candidate_id,
            "survey_generation": self._generation,
            "display_snr_db": display_snr_db,
            "spectrum_bandwidth_hz": bandwidth_hz,
            "receiver_center_hz": self._center_hz,
            "receiver_sample_rate_hz": self._sample_rate_hz,
            "source_id": self._replay.source_id if self._replay else "live:sdrconnect",
        }

    def _on_ml_result(self, metadata: dict, result: dict) -> None:
        if metadata.get("survey_generation") != self._generation:
            return
        valid = {_label_id(t) for t in self._latest_peaks}
        valid.update(p["id"] for p in self.ml_proposals() if p["width_hz"] <= 25_000)
        candidate = metadata.get("candidate_id")
        if candidate not in valid:
            return
        self._rfml_results = {k: v for k, v in self._rfml_results.items() if k in valid}
        self._rfml_results[candidate] = result
        self._rfml_received = {k: v for k, v in self._rfml_received.items() if k in valid}
        self._rfml_received[candidate] = time.monotonic()
        self._notify_labels_changed()
        self._publish_survey(force=True)

    def _fresh_ml_result(self, label_id: str) -> dict | None:
        result = getattr(self, "_rfml_results", {}).get(label_id)
        if result is None:
            return None
        received = getattr(self, "_rfml_received", {}).get(label_id)
        age = max(0.0, time.monotonic() - received) if received is not None else None
        ttl = getattr(getattr(self._settings_getter(), "ml", None), "result_ttl_s", 60.0)
        return {**result, "age_s": age, "stale": age is None or age > ttl}

    def _on_analyzer_status(self) -> None:
        self._sync_detector_result()
        self._publish_survey(force=True)

    def _sync_detector_result(self) -> None:
        result = (self._analyzer.detector_result().get("result")
                  if self._analyzer is not None else None)
        if result is not getattr(self, "_last_detector_result", None):
            self._last_detector_result = result
            self._notify_labels_changed()

    def ml_proposals(self) -> list[dict]:
        """Bounded, fresh shadow proposals for inspector and observation history.

        They never enter the DSP tracker or become a validated identity.
        """
        analyzer = getattr(self, "_analyzer", None)
        status = analyzer.detector_result() if analyzer else {}
        result = status.get("result") or {}
        # Called several times per spectrum frame (_build_labels,
        # _notify_labels_changed, _analyzer_targets, _sync_detector_result,
        # _ml_context) for the same detector result -- free on a cache hit.
        # Only geometry/ids are cached: a classifier result arriving between
        # detector runs and the capture age must not be frozen with them
        # (review 2026-09-28).
        cache_key = id(result) if result else None
        if cache_key is not None and cache_key == self._ml_proposal_cache_key:
            return self._decorate_ml_proposals(self._ml_proposal_cache, status)
        proposals = []
        matched_ids: set[str] = set()
        boxes: dict[str, tuple[float, float]] = {}
        for candidate in _aggregate_ml_frequency_regions(result.get("candidates", [])):
            low, high = candidate.get("low_hz"), candidate.get("high_hz")
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                continue
            if not math.isfinite(low) or not math.isfinite(high) or not 0 < low < high:
                continue
            center = int(round((low + high) / 2))
            # Match this box to a still-live proposal from the last round by
            # frequency overlap and keep its id -- grid-rounded ids churned
            # on detector box jitter of more than one bin, opening/retiring
            # an Observation every detector interval and orphaning
            # classifier results stored under the old id (review 2026-09-28).
            candidate_id = next(
                (existing_id for existing_id, (elow, ehigh) in self._ml_proposal_boxes.items()
                 if existing_id not in matched_ids
                 and _box_overlap_ratio(low, high, elow, ehigh) >= _PROPOSAL_OVERLAP_MIN),
                None,
            )
            if candidate_id is None:
                self._ml_proposal_seq += 1
                candidate_id = f"ml-{self._generation}-{self._ml_proposal_seq}"
            matched_ids.add(candidate_id)
            boxes[candidate_id] = (low, high)
            details = {**candidate, "model_id": result.get("model_id"),
                       "model_version": result.get("model_version"),
                       "preprocessing_version": result.get("preprocessing_version"),
                       "input": result.get("input"),
                       "bin_width_hz": result.get("bin_width_hz"), "shadow": True}
            proposals.append({"id": candidate_id,
                              "ml_only": True, "frequency_hz": center,
                              "width_hz": high - low, "low_hz": low, "high_hz": high,
                              "rfml_detection": details})
        self._ml_proposal_boxes = boxes
        if cache_key is not None:
            self._ml_proposal_cache_key = cache_key
            self._ml_proposal_cache = proposals
        return self._decorate_ml_proposals(proposals, status)

    def _decorate_ml_proposals(self, cached: list[dict], status: dict) -> list[dict]:
        """Attach the live parts to cached proposal geometry: the detector
        capture age/staleness and the current classifier result for the id."""
        age = status.get("result_age_s")
        stale = status.get("result_stale")
        return [
            {**proposal,
             "rfml_detection": {**proposal["rfml_detection"], "age_s": age, "stale": stale},
             "rfml": self._fresh_ml_result(proposal["id"])}
            for proposal in cached
        ]

    def _tuned_analyzer_hz(self) -> int | None:
        """The live receiver's narrowband VFO, if its channel fits this span."""
        vfo = getattr(self, "_vfo_hz", None)
        center = self._center_hz
        rate = self._sample_rate_hz
        if (
            getattr(self, "_replay", None) is not None
            or vfo is None or center is None or rate is None
            or abs(vfo - center) > rate / 2 - _VFO_MARGIN_HZ
        ):
            return None
        demodulator = (getattr(self._confirmed, "props", None) or {}).get("demodulator")
        if str(demodulator).upper() in ("WFM", "WBFM"):
            return None  # WFM/RDS uses a separate wideband analyzer.
        return vfo

    def _analyzer_targets(self) -> list[int]:
        """Prioritize the tuned VFO, then all narrow eligible candidates.

        The Analyzer keeps at most max_analyzed active slots and pages through
        the rest after completed dwells. Do not truncate here: selecting the
        same top six on every spectrum frame permanently starved weaker
        signals. Duty/SNR still order the first page, so steady signals get
        an early result. Peak frequencies come from bins inside the span.
        """
        if self._sample_rate_hz is None or self._center_hz is None:
            return []
        if (
            not any(d in self._dimensions for d in _NARROWBAND_DIMENSIONS)
            and not self._ml_enabled()
        ):
            return []
        settings = self._settings_getter()
        min_snr_db = getattr(settings, "analyze_min_snr_db", 8.0)
        resolution_hz = self._sample_rate_hz / _BINS
        now = time.monotonic()
        history_s = self._tracker.history_s
        candidates: list[tuple[float, float, int]] = []
        for track in self._latest_peaks:
            frequency_hz = (
                self._center_hz - self._sample_rate_hz / 2 + track.centroid_bin * resolution_hz
            )
            # SDRconnect's persistent center spike is a local DC artifact.
            if abs(track.centroid_bin - _BINS // 2) <= self._tracker.dc_exclude_bins:
                continue
            width_bins = track.bin_hi - track.bin_lo + 1
            width_hz = width_bins * resolution_hz
            bw_class = bandwidth_class(
                width_hz,
                resolution_hz,
                width_bins,
                center_hz=frequency_hz,
                profile=track.profile,
            )
            if bw_class in _WIDE_BW_CLASSES:
                continue
            snr_db = track.snr_units / self._units_per_db if self._units_per_db else 0.0
            if snr_db < min_snr_db:
                continue
            duty = activity_class(track, now, history_s)["duty"]
            candidates.append((duty, snr_db, int(round(frequency_hz))))
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        tuned = self._tuned_analyzer_hz()
        targets = [tuned] if tuned is not None else []
        for _, _, freq in candidates:
            if tuned is None or abs(freq - tuned) > _ANALYZER_TARGET_MATCH_HZ:
                targets.append(freq)
        if not getattr(getattr(settings, "ml", None), "enabled", False):
            return targets
        lower = self._center_hz - self._sample_rate_hz / 2
        upper = lower + self._sample_rate_hz
        for proposal in self.ml_proposals():
            freq = proposal["frequency_hz"]
            if (proposal["width_hz"] > 25_000 or not lower <= freq < upper or
                any(abs(freq - existing) <= _ANALYZER_TARGET_MATCH_HZ
                    for existing in targets)):
                continue
            targets.append(freq)
        return targets

    def _rds_targets(self) -> list[int]:
        """Wide labels (wfm, plus wide_digital as a fallback for a
        misclassified broadcast) centred in the FM broadcast band, strongest
        first -- only while the "rds" dimension is selected. One target per
        FM channel (the strongest label's), snapped to the channel centre:
        side peaks of one station (HD sidebands, a split label) would each
        cost a dwell and each get their own station accumulator."""
        if self._sample_rate_hz is None or self._center_hz is None:
            return []
        if "rds" not in self._dimensions:
            return []
        resolution_hz = self._sample_rate_hz / _BINS
        candidates: list[tuple[float, int]] = []
        for track in self._latest_peaks:
            frequency_hz = (
                self._center_hz - self._sample_rate_hz / 2 + track.centroid_bin * resolution_hz
            )
            if not in_fm_band(frequency_hz):
                continue
            width_bins = track.bin_hi - track.bin_lo + 1
            bw_class = bandwidth_class(
                width_bins * resolution_hz,
                resolution_hz,
                width_bins,
                center_hz=frequency_hz,
                profile=track.profile,
            )
            if bw_class not in _WIDE_BW_CLASSES:
                continue
            snr_db = track.snr_units / self._units_per_db if self._units_per_db else 0.0
            candidates.append((snr_db, self._station_hz(track, frequency_hz, resolution_hz)))
        if self._vfo_hz is not None and in_fm_band(self._vfo_hz):
            # The station being listened to sits on the span centre when
            # SDRconnect is tuned to it, where the DC-excluded bins split it
            # into narrow fragments that never classify as wfm.
            candidates.append((float("inf"), self._vfo_hz))
        candidates.sort(reverse=True)
        channels: list[int] = []
        for _, frequency_hz in candidates:
            # Strongest first: a weaker label this close to an already
            # chosen channel is that station's side peak, not a neighbour.
            if any(abs(frequency_hz - ch) < _RDS_SIDE_PEAK_HZ for ch in channels):
                continue
            channel_hz = self._fm_channel(frequency_hz)
            if channel_hz not in channels:
                channels.append(channel_hz)
        return channels[:_MAX_RDS_TARGETS]

    def _fm_channel(self, frequency_hz: float) -> int:
        """The FM channel key for RDS targets/results/labels -- one grid for
        all three (SurveySettings.rds_odd_channels)."""
        odd = bool(getattr(self._settings_getter(), "rds_odd_channels", False))
        return fm_channel_hz(frequency_hz, odd_tenths=odd)

    def _station_hz(self, track: TrackedPeak, frequency_hz: float, resolution_hz: float) -> int:
        """Where the FM carrier is: the centroid of the profile bins within
        _RDS_TOP_DB of its strongest one. The whole-label centroid is pulled
        off by HD Radio sidebands (~17 dB down, +-100..200 kHz), which merge
        into the label -- live: KLLC 97.31 labelled 97.39, snapped to the
        empty 97.4. Falls back to `frequency_hz` without a profile."""
        profile = track.profile
        if not profile or self._center_hz is None or self._sample_rate_hz is None:
            return int(round(frequency_hz))
        top = max(profile) - _RDS_TOP_DB * (self._units_per_db or 1.0)
        bins = [i for i, level in enumerate(profile) if level >= top]
        centre_bin = track.profile_lo + sum(bins) / len(bins)
        base_hz = self._center_hz - self._sample_rate_hz / 2
        return int(round(base_hz + centre_bin * resolution_hz))

    def _find_analyzer_result(
        self, frequency_hz: float, resolution_hz: float, results: list[dict] | None = None
    ) -> dict | None:
        """Nearest stored result within +-1 spectrum bin of `frequency_hz` --
        or within the analyzer's target-matching tolerance when bins are
        finer than that (results are keyed by the target the analyzer kept,
        which the label's centroid may have drifted from by that much)."""
        best = None
        best_dist = None
        tolerance_hz = max(resolution_hz, _ANALYZER_TARGET_MATCH_HZ)
        for result in self._analyzer_results if results is None else results:
            dist = abs(result["frequency_hz"] - frequency_hz)
            if dist <= tolerance_hz and (best is None or dist < best_dist):
                best, best_dist = result, dist
        return best

    def _sync_peak_detection_settings(self) -> None:
        enabled = bool(getattr(self._settings_getter(), "center_exclusion_enabled", False))
        self._tracker.dc_exclude_bins = 2 if enabled else -1

    def _spectrum_markers(self) -> list[dict]:
        """Compact, uncapped anchors for every tracked spectrum peak.

        Rich labels are display-capped and may move to a nearby centroid;
        these anchors retain the strongest actual FFT bin for drawing on the
        trace. Retired tracks remain marked inactive until their TTL expires.
        """
        self._sync_peak_detection_settings()
        if self._center_hz is None or not self._sample_rate_hz:
            return []
        resolution_hz = self._sample_rate_hz / _BINS
        low_hz = self._center_hz - self._sample_rate_hz / 2
        now = time.monotonic()
        epoch_offset = time.time() - now
        markers = []
        for track in self._latest_peaks:
            peak_bin = track.peak_bin
            if peak_bin is None:
                peak_bin = (
                    track.profile_lo + max(range(len(track.profile)), key=track.profile.__getitem__)
                    if track.profile else int(round(track.centroid_bin))
                )
            peak_bin = max(0, min(_BINS - 1, peak_bin))
            if (self._tracker.dc_exclude_bins >= 0
                    and abs(peak_bin - _BINS // 2) <= self._tracker.dc_exclude_bins):
                continue
            markers.append({
                "id": _label_id(track),
                "frequency_hz": int(round(low_hz + track.centroid_bin * resolution_hz)),
                "peak_frequency_hz": int(round(low_hz + peak_bin * resolution_hz)),
                "peak_bin": peak_bin,
                "bin_lo": track.bin_lo,
                "bin_hi": track.bin_hi,
                "width_hz": (track.bin_hi - track.bin_lo + 1) * resolution_hz,
                "level": track.level,
                "snr_db": track.snr_units / self._units_per_db if self._units_per_db else 0.0,
                "active_now": track.active_now,
                "last_seen": track.last_seen + epoch_offset,
                "age_s": max(0.0, now - track.last_seen),
            })
        markers.sort(key=lambda marker: (marker["active_now"], marker["snr_db"]), reverse=True)
        return markers

    def _peak_detection_status(self) -> dict:
        self._sync_peak_detection_settings()
        if (self._tracker.dc_exclude_bins < 0 or self._center_hz is None
                or not self._sample_rate_hz):
            center_exclusion = None
        else:
            resolution_hz = self._sample_rate_hz / _BINS
            excluded_bins = self._tracker.dc_exclude_bins
            center_exclusion = {
                "start_hz": self._center_hz - (excluded_bins + 0.5) * resolution_hz,
                "end_hz": self._center_hz + (excluded_bins + 0.5) * resolution_hz,
                "first_bin": _BINS // 2 - excluded_bins,
                "last_bin": _BINS // 2 + excluded_bins,
                "reason": "receiver_dc",
            }
        return {
            "threshold_db": getattr(self._settings_getter(), "peak_threshold_db", 10.0),
            "center_exclusion_enabled": self._tracker.dc_exclude_bins >= 0,
            "marker_hold_s": getattr(self._settings_getter(), "marker_hold_s", 15.0),
            "center_exclusion": center_exclusion,
        }

    def scan_labels(self) -> list[dict]:
        """All measured labels for scanning, including lower-ranked peaks."""
        return self._build_labels(limit=None)

    def scan_snapshot(self) -> dict:
        """Cheap per-generation progress and latest settled spectrum frame.

        Spectrum bins are dB, converted from the radio's 8-bit display scale.
        Center bins are retained unless optional DC exclusion is enabled.
        SpectrumFrame has no remote tune-generation metadata;
        the gate and settle interval are the best available causal boundary.
        """
        self._sync_peak_detection_settings()
        frame = self._latest_spectrum
        analyzer = self._analyzer
        dsp_targets = len(analyzer._candidate_targets) if analyzer is not None else 0
        dsp_analyzed = (
            sum(bool(analyzer._target_state.get(f) and analyzer._target_state[f].analyzed)
                for f in analyzer._candidate_targets)
            if analyzer is not None else 0
        )
        spectrum = None
        if frame is not None and self._center_hz is not None and self._sample_rate_hz is not None:
            spectrum = {
                "bins": [self._base_db + value / self._units_per_db for value in frame.bins],
                "center_hz": self._center_hz,
                "span_hz": self._sample_rate_hz,
                "at": time.time() - (time.monotonic() - frame.at),
                "center_exclusion_bins": self._tracker.dc_exclude_bins,
            }
        return {
            "frame_seq": self._frame_seq,
            "connected": self._active and (self._replay is not None or bool(self._hub.connected)),
            "survey_center_hz": self._center_hz,
            "survey_sample_rate_hz": self._sample_rate_hz,
            "survey_span_hz": self._sample_rate_hz,
            "generation": self._generation,
            "usable_spectrum": self._usable_spectrum,
            "dsp_completed": self._dsp_completed,
            "dsp_targets": dsp_targets,
            "dsp_analyzed": dsp_analyzed,
            "dsp_pending": dsp_targets - dsp_analyzed,
            "spectrum": spectrum,
        }

    def status(self) -> dict:
        settings = self._settings_getter()
        occ = self._occupancy.snapshot()
        windows_s = list(getattr(settings, "occupancy_windows_s", (60, 600)))
        elapsed = time.time() - self._since if self._active and self._since else 0.0
        fps = self._frames_total / elapsed if elapsed > 0 else 0.0
        presets = [_dump(p) for p in getattr(settings, "presets", [])]
        labels = self._build_labels()
        # Preserve the detector's overlap-stable proposal IDs for the UI's
        # retained table rows. These remain separate from measured labels.
        ml_proposals = self.ml_proposals()
        return {
            "ml_config": {
                # False while ML is dormant; the UI hides every ML control.
                "available": rfml.AVAILABLE,
                "classifier_enabled": rfml.AVAILABLE and bool(
                    getattr(getattr(settings, "ml", None), "enabled", False)),
                "detector_enabled": rfml.AVAILABLE and bool(
                    getattr(getattr(getattr(settings, "ml", None), "detector", None),
                            "enabled", False)),
                "detector_interval_s": getattr(
                    getattr(getattr(settings, "ml", None), "detector", None),
                    "interval_s", None),
            },
            "active": self._active,
            "since": self._since,
            "generation": self._generation,
            "spectrum_markers": self._spectrum_markers(),
            "peak_detection": self._peak_detection_status(),
            "replay": self._replay.status() if self._replay is not None else None,
            "span": {
                "center_hz": self._center_hz,
                "sample_rate_hz": self._sample_rate_hz,
                "vfo_hz": self._vfo_hz,
            },
            "stream": {
                "connected": True if self._replay is not None else bool(self._hub.connected),
                "frames": self._frames_total,
                "fps": fps,
            },
            "occupancy": {
                "short": occ["short"],
                "long": occ["long"],
                "floor": occ["floor"],
                "threshold_db": getattr(settings, "threshold_db", 8.0),
                "windows_s": windows_s,
            },
            "presets": presets,
            "sample_rates_hz": list(_DEFAULT_SAMPLE_RATES_HZ),
            "labels": labels,
            "ml_proposals": ml_proposals,
            "dimensions": list(self._dimensions),
            "available_dimensions": AVAILABLE_DIMENSIONS,
            "resolution_hz": (self._sample_rate_hz / _BINS) if self._sample_rate_hz else 0.0,
            "analyzer": (
                self._analyzer.status()
                if self._analyzer is not None
                else {
                    "active": False,
                    "queue": [],
                    "analyzing": None,
                    "targets": [],
                    "iq": {"connected": False, "frames": 0, "dropped": 0, "cpu_load": 0.0},
                    "max_analyzed": getattr(settings, "max_analyzed", 6),
                    "dwell_s": getattr(settings, "analyze_dwell_s", 2.0),
                    "last_error": None,
                }
            ),
        }

    # -- internals: labels --------------------------------------------------

    def _build_labels(self, *, limit: int | None = _MAX_LABELS) -> list[dict]:
        self._sync_peak_detection_settings()
        if self._sample_rate_hz is None or self._center_hz is None:
            return []
        settings = self._settings_getter()
        label_ttl_s = getattr(settings, "label_ttl_s", 120)
        # SpectrumFrame.at is time.monotonic() (adapter), so every track time is
        # monotonic too: TTL, activity and the identify cache must use that clock
        # (mixing in time.time() made every track look ~1.8e9 s stale -- the live
        # check showed zero labels). Only the exported timestamps become epoch.
        now = time.monotonic()
        epoch_offset = time.time() - now
        resolution_hz = self._sample_rate_hz / _BINS
        history_s = self._tracker.history_s
        dims = self._dimensions
        want_bandwidth = "bandwidth" in dims
        want_bandplan = "bandplan" in dims
        want_catalog = "catalog" in dims
        want_modulation = "modulation" in dims
        want_symbol_rate = "symbol_rate" in dims
        want_voice = "voice" in dims
        want_analyzer = want_modulation or want_symbol_rate or want_voice
        want_rds = "rds" in dims
        analyzer_status = self._analyzer.status() if self._analyzer is not None else {}
        used_results: list[dict] = []
        self._prune_confirmed(now)
        self._identify_refreshed = False

        labels = []
        recognized_ttl_s = getattr(settings, "recognized_ttl_s", None)
        recognized: set[str] = set()
        for track in self._latest_peaks:
            if (self._tracker.dc_exclude_bins >= 0 and
                    abs((track.peak_bin if track.peak_bin is not None else track.centroid_bin)
                        - _BINS // 2) <= self._tracker.dc_exclude_bins):
                continue
            # A recognised signal (see `recognized` below) keeps its row past
            # label_ttl_s: it's the finding the survey is for, and the tracker
            # (pinned) keeps its history too. recognized_ttl_s None = for good.
            ttl_s = recognized_ttl_s if track.id in self._recognized_ids else label_ttl_s
            if ttl_s is not None and now - track.last_seen > ttl_s:
                continue
            frequency_hz = (
                self._center_hz - self._sample_rate_hz / 2 + track.centroid_bin * resolution_hz
            )
            width_bins = track.bin_hi - track.bin_lo + 1
            width_hz = width_bins * resolution_hz
            # The tracker's own id: stable for the life of the signal. The old
            # f"p{kHz}" id followed the centroid, which jitters by bins on
            # wideband signals -- half the table's rows got new ids every few
            # seconds (live 2026-09-26: p102118 -> p102105 for one station),
            # so rows vanished/reappeared and S5 confirms fell off.
            label_id = _label_id(track)
            snr_db = track.snr_units / self._units_per_db if self._units_per_db else 0.0

            # Computed unconditionally regardless of the "bandwidth" display
            # dimension -- the analyzer merge below needs the real class
            # (wideband bypass, "voice" NFM-only gate) even when bw_class
            # itself isn't shown.
            internal_bw_class = bandwidth_class(
                width_hz,
                resolution_hz,
                width_bins,
                center_hz=frequency_hz,
                profile=track.profile,
            )
            bw_class = internal_bw_class if want_bandwidth else None
            bw_source = "spectrum" if bw_class is not None else None
            bandplan = None
            allocation = None
            catalog: list[dict] = []
            if want_bandplan or want_catalog:
                candidates = self._identify_for_label(label_id, frequency_hz, width_hz, now)
                if want_bandplan:
                    allocation = self._allocation_for_label(frequency_hz)
                    bandplan = next(
                        (
                            {k: c.get(k) for k in ("name", "category", "confidence", "reason")}
                            for c in candidates
                            if c.get("provider") == "bandplan"
                        ),
                        None,
                    )
                if want_catalog:
                    catalog = [
                        {k: c.get(k) for k in ("name", "confidence", "reason")}
                        for c in candidates
                        if c.get("provider") == "catalog"
                    ][:3]

            modulation = symbol_rate_field = family = voice = None
            analyzed_at = None
            last_classification = None
            if want_analyzer:
                if internal_bw_class in _WIDE_BW_CLASSES:
                    # Wide signals never reach the analyzer at all (S2 label
                    # only) -- this is the label's entire "modulation" story.
                    if want_modulation:
                        modulation = {"kind": "wideband", "confidence": 0.5}
                else:
                    match = self._find_analyzer_result(frequency_hz, resolution_hz)
                    if match is not None:
                        used_results.append(match)
                        fields = match["fields"]
                        last_classification = fields.get("last_classification")
                        if want_modulation:
                            modulation = fields["modulation"]
                            family = fields["family"]
                            if family is not None and label_id in self._confirmed_labels:
                                # S5: a confirmed decode is ground truth --
                                # show it as certain regardless of what the
                                # analyzer's own line-strength x modulation
                                # confidence product came out to.
                                family = {**family, "confidence": 1.0}
                        if want_symbol_rate:
                            symbol_rate_field = fields["symbol_rate_hz"]
                        if want_voice and internal_bw_class in _VOICE_BW_CLASSES:
                            voice = fields["voice"]
                        analyzed_at = fields["analyzed_at"]
                        if want_bandwidth and bw_class == "unresolved" and analyzed_at:
                            # The spectrum couldn't tell; the analyzer measured it.
                            iq_class = bandwidth_class_from_occupied(
                                (fields["modulation"].get("features") or {}).get("occ_bw_hz"),
                                fields["modulation"].get("kind"),
                            )
                            if iq_class is not None:
                                bw_class = iq_class
                                bw_source = "iq"

            confirmed_entry = self._confirmed_labels.get(label_id)
            confirmed = None
            if confirmed_entry is not None:
                confirmed_entry["last_seen_mono"] = now
                confirmed = confirmed_entry["confirmed"]

            rds = None
            if want_rds and in_fm_band(frequency_hz):
                # RDS results are keyed by FM channel centre (see _rds_targets).
                # A wide label is keyed by where its carrier is, not by its
                # centroid (see _station_hz). A narrow fragment (the tuned
                # station split by the DC-excluded bins) only gets its own
                # channel's result, and only once a PI proves a station is there.
                if internal_bw_class in _WIDE_BW_CLASSES:
                    channel_hz = self._fm_channel(
                        self._station_hz(track, frequency_hz, resolution_hz)
                    )
                else:
                    channel_hz = self._fm_channel(frequency_hz)
                by_channel = {self._fm_channel(r["frequency_hz"]): r for r in self._rds_results}
                match = by_channel.get(channel_hz)
                if (
                    match is not None
                    and internal_bw_class not in _WIDE_BW_CLASSES
                    and not match["fields"]["rds"].get("pi")
                ):
                    match = None
                if match is not None:
                    rds = match["fields"]["rds"]
                    analyzed_at = match["fields"].get("analyzed_at")
                    ps = rds.get("ps")
                    # Call letters (from the PI, US) are known within a few
                    # groups and stay stable while PS scrolls song titles.
                    callsign = rds.get("callsign")
                    # Once when the PS is the call sign itself (KOIT sends "KOIT").
                    name = " ".join(dict.fromkeys(x.strip() for x in (callsign, ps) if x))
                    if name:
                        # The station's own name: as good as a decoder confirm.
                        family = {
                            "name": f"FM broadcast · {name}",
                            "confidence": 0.9,
                            "suggested_decoders": [],
                        }
                    if ps and confirmed is None:
                        rt = rds.get("rt")
                        confirmed = {
                            "decoder": "rds",
                            "sample": ps + (" · " + rt if rt else ""),
                            "at": analyzed_at,
                            "events": rds.get("groups", 0),
                        }

            analysis_state = _analysis_state_for_label(
                frequency_hz,
                resolution_hz,
                analyzer_status,
                analyzed_at,
                last_classification,
                family,
                now_epoch=time.time(),
            )

            if (
                family is not None
                or confirmed is not None
                or (rds is not None and (rds.get("ps") or rds.get("callsign")))
            ):
                recognized.add(track.id)
            labels.append(
                {
                    "id": label_id,
                    "recognized": track.id in recognized,
                    "frequency_hz": int(round(frequency_hz)),
                    "width_hz": width_hz,
                    "snr_db": snr_db,
                    "level": track.level,
                    "active_now": track.active_now,
                    # Always computed, per contract, regardless of dimension selection.
                    "activity": activity_class(track, now, history_s),
                    "bw_class": bw_class,
                    # "spectrum" (label width) or "iq" (analyzer's measurement)
                    "bw_source": bw_source,
                    "bandplan": bandplan,
                    # Full allocation of the band (every service); "bandplan"
                    # above stays the first channel/application hint for
                    # saved-report compatibility.
                    "allocation": allocation,
                    "catalog": catalog,
                    "modulation": modulation,
                    "rfml": self._fresh_ml_result(label_id),
                    "symbol_rate_hz": symbol_rate_field,
                    # Separate, explicitly qualified protocol suggestions;
                    # they do not alter family or modulation measurements.
                    "decoder_candidates": decoder_candidates(
                        frequency_hz,
                        modulation,
                        symbol_rate_field,
                        demodulator=(getattr(self._confirmed, "props", None) or {}).get(
                            "demodulator"
                        ),
                    ),
                    "family": family,
                    "voice": voice,
                    "confirmed": confirmed,
                    "rds": rds,
                    "decoding": self._is_decoding(frequency_hz, resolution_hz),
                    "first_seen": track.first_seen + epoch_offset,
                    "last_seen": track.last_seen + epoch_offset,
                    "analyzed_at": analyzed_at,
                    "last_classification": last_classification,
                    "analysis_state": analysis_state,
                }
            )

        # A tracked peak may disappear for one spectrum frame while the IQ
        # analyzer has already completed its dwell. Keep that result briefly
        # so the returning label can use it; respect a shorter configured
        # label TTL and never carry it indefinitely into another session.
        result_grace_s = min(
            _ANALYZER_RESULT_GRACE_S,
            max(0.0, float(label_ttl_s)) if label_ttl_s is not None
            else _ANALYZER_RESULT_GRACE_S,
        )
        self._analyzer_results = [
            r for r in self._analyzer_results
            if r in used_results or now - r.get("received_mono", float("-inf")) <= result_grace_s
        ]
        self._recognized_ids = recognized
        self._tracker.pinned = recognized
        self._tracker.pinned_ttl_s = None if recognized_ttl_s is None else float(recognized_ttl_s)
        # RDS results are NOT pruned per pass or by age: a station's label can
        # drop out or flip bw_class for a second, and the next dwell on that
        # channel can be many minutes away. They live exactly as long as the
        # analyzer's per-station accumulators they mirror (one per FM channel,
        # both reset on span change / analyzer stop) -- a TTL here showed PI
        # codes in the analyzer status but none on the labels (live 2026-09-26).
        labels.sort(key=lambda item: (bool(item["active_now"]), item["snr_db"]), reverse=True)
        if limit is not None:
            labels = labels[:limit]
        # Carry detector support into existing DSP rows without changing their
        # measured boundaries, ranking, or identity. Proposals are score ordered.
        for proposal in self.ml_proposals():
            matches = [label for label in labels
                       if proposal["low_hz"] <= label["frequency_hz"] <= proposal["high_hz"]]
            if matches:
                nearest = min(matches, key=lambda label: abs(
                    label["frequency_hz"] - proposal["frequency_hz"]))
                nearest.setdefault("rfml_detection", proposal["rfml_detection"])
        if self._identify_refreshed:
            # catalog/bandplan boundary: a label's identify cache was just
            # refreshed (not merely reused), which happens once per label per
            # _IDENTIFY_CACHE_TTL_S regardless of how often build_labels()
            # itself is called -- never every spectrum frame. Pass the
            # labels already built here so this doesn't recurse.
            self._notify_labels_changed(labels)
        return labels

    def _identify_for_label(
        self, label_id: str, frequency_hz: float, width_hz: float, now: float
    ) -> list[dict]:
        if self._identify is None:
            return []
        cached = self._identify_cache.get(label_id)
        if cached is not None and now - cached[0] <= _IDENTIFY_CACHE_TTL_S:
            return cached[1]
        candidates = self._identify(int(round(frequency_hz)), int(round(width_hz)))
        self._identify_cache[label_id] = (now, candidates)
        self._identify_refreshed = True
        return candidates

    def _allocation_for_label(self, frequency_hz: float) -> dict | None:
        if self._allocation is None:
            return None
        hz = int(round(frequency_hz))
        for (lo, hi), cached in self._allocation_cache.items():
            if lo <= hz < hi:
                return cached
        summary = self._allocation(hz)
        lo, hi = (summary or {}).get("lo_hz"), (summary or {}).get("hi_hz")
        if isinstance(lo, int) and isinstance(hi, int) and lo < hi:
            self._allocation_cache[(lo, hi)] = summary
        return summary

    # -- internals: spectrum pump -----------------------------------------

    async def _pump(self) -> None:
        assert self._hub_queue is not None
        try:
            while True:
                frame: SpectrumFrame = await self._hub_queue.get()
                if self._retune_blocked or frame.at < self._accept_after_mono:
                    continue
                if len(frame.bins) != _BINS:
                    continue
                self._frames_total += 1
                self._frame_seq += 1
                self._usable_spectrum += 1
                self._latest_spectrum = frame
                # Settings may change while Survey is running (including the
                # header's DSP peak control); apply the new threshold to the
                # next frame without discarding accumulated survey history.
                peak_db = getattr(self._settings_getter(), "peak_threshold_db", 10.0)
                self._tracker.threshold_units = round(peak_db * self._units_per_db)
                self._sync_peak_detection_settings()
                self._occupancy.update(frame.bins)
                self._latest_peaks = self._tracker.update(
                    frame.bins, frame.at, self._occupancy.floor
                )
                # track appears/retires boundary (S2): an id set change is
                # the tracker actually creating or dropping a track, not just
                # this frame's re-measurement of ones already there -- most
                # frames leave the set unchanged and notify nothing.
                current_track_ids = {t.id for t in self._latest_peaks}
                if current_track_ids != self._known_track_ids:
                    self._known_track_ids = current_track_ids
                    self._notify_labels_changed()
                if self._analyzer is not None:
                    self._analyzer.set_targets(
                        self._analyzer_targets(),
                        priority_frequency_hz=self._tuned_analyzer_hz(),
                    )
                    self._analyzer.set_rds_targets(self._rds_targets())
                self._maybe_publish_spectrum(frame)
                self._maybe_publish_survey()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("survey pump crashed")

    def _maybe_publish_spectrum(self, frame: SpectrumFrame) -> None:
        settings = self._settings_getter()
        fps = max(1, int(getattr(settings, "spectrum_fps", 10)))
        min_interval = 1.0 / fps
        now = time.monotonic()
        if now - self._last_spectrum_publish_at < min_interval:
            return
        self._last_spectrum_publish_at = now
        self._publish(
            {
                "type": "spectrum",
                "bins": base64.b64encode(frame.bins).decode("ascii"),
                "center_hz": self._center_hz,
                "sample_rate_hz": self._sample_rate_hz,
                "at": frame.at,
                "ref_level_db": self._ref_level_db,
                "base_db": self._base_db,
            }
        )

    def _maybe_publish_survey(self) -> None:
        now = time.monotonic()
        if now - self._last_survey_publish_at < 1.0:
            return
        # Expiry changes a detector result to None without a worker callback.
        # Retire the shadow observation on the next ordinary status publish.
        self._sync_detector_result()
        self._publish_survey()

    def _publish_survey(self, *, force: bool = False) -> None:
        self._last_survey_publish_at = time.monotonic()
        self._publish({"type": "survey", **self.status()})

    async def _read_level(self, prop: str, default: float) -> float:
        try:
            value = await self._adapter.get(prop)
        except (TimeoutError, ConnectionError):
            return default
        if value is None:
            return default
        return _as_float(value, default)
