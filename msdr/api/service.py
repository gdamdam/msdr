"""Application service: wires radio/engine/library/storage/journal together
behind an asyncio event loop, independent of the web framework in front of it.
"""

import asyncio
import contextlib
import logging
import tempfile
import time
import typing
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from msdr import __version__
from msdr.allocations.store import AllocationStore
from msdr.api.schemas import (
    outcome_to_dict,
    restore_report_to_dict,
)

# Re-exported so `from msdr.api.service import X` (tests, app.py) keeps working
# after these moved next to the domain mixins that use them.
from msdr.api.services._common import (
    _ALL_SIGNALS_LIMIT,  # noqa: F401
    _RECORDER_FORWARD_PROPS,  # noqa: F401
    AudioStream,  # noqa: F401
    DecoderError,  # noqa: F401
    DecoderManager,  # noqa: F401
    IQStream,  # noqa: F401
    OutOfSpanError,  # noqa: F401
    ReceiverStopped,  # noqa: F401
    Recorder,  # noqa: F401
    RecorderConfig,  # noqa: F401
    RecorderError,  # noqa: F401
    ReplayDecodeSession,  # noqa: F401
    RestoreRunning,  # noqa: F401
    SpectrumHub,  # noqa: F401
    Survey,  # noqa: F401
    SurveyError,  # noqa: F401
    VirtualReceiver,  # noqa: F401
    describe_decoders,  # noqa: F401
    resolve_binaries,  # noqa: F401
)
from msdr.api.services.allocations import (
    AllocationServiceMixin,
    allocation_summary,  # noqa: F401
    build_allocation_dataset,  # noqa: F401
    format_services,  # noqa: F401
)
from msdr.api.services.beacons import (
    BeaconError,  # noqa: F401
    BeaconMonitor,  # noqa: F401
    BeaconServiceMixin,
)
from msdr.api.services.ble import BleServiceMixin
from msdr.api.services.catalog import (
    _CATALOG_ENCODINGS,  # noqa: F401
    _CATALOG_FETCH_TIMEOUT_S,  # noqa: F401
    _CATALOG_IDENTIFY_MIN_TOLERANCE_HZ,  # noqa: F401
    _CATALOG_PARSERS,  # noqa: F401
    _CATALOG_ZIP_MEMBERS,  # noqa: F401
    _MAX_IMPORT_BYTES,  # noqa: F401
    CatalogServiceMixin,
    _catalog_note_line,  # noqa: F401
    _parse_eibi_fetched,  # noqa: F401
    catalog_payload_text,  # noqa: F401
    fetch_catalog_text,  # noqa: F401
    fetch_priyom_catalog,  # noqa: F401
)
from msdr.api.services.decoders import (
    _BAND_SAVE_TOLERANCE_HZ,  # noqa: F401
    _DECODER_PRESETS,  # noqa: F401
    DecoderServiceMixin,
)
from msdr.api.services.history import HistoryServiceMixin
from msdr.api.services.iqscan import IqScanServiceMixin
from msdr.api.services.library import (
    LibraryServiceMixin,
    _bulk_undo_label,  # noqa: F401
)
from msdr.api.services.listen import (
    _BEACON_CALLSIGNS,  # noqa: F401
    _BEACON_CYCLE_S,  # noqa: F401
    _BEACON_FILTER_BANDWIDTH_HZ,  # noqa: F401
    _BEACON_FREQUENCIES_HZ,  # noqa: F401
    _BEACON_SLOT_S,  # noqa: F401
    _LISTEN_CANDIDATE_LIMIT,  # noqa: F401
    _LISTEN_DECODABLE_TOKENS,  # noqa: F401
    _LISTEN_STARTING_SOON_WINDOW_MIN,  # noqa: F401
    ListenServiceMixin,
    _catalog_listen_item,  # noqa: F401
    _signal_listen_item,  # noqa: F401
    beacon_schedule,  # noqa: F401
)
from msdr.api.services.observations import ObservationServiceMixin
from msdr.api.services.recorder import (
    _RECORDER_TICK_INTERVAL_S,  # noqa: F401
    RecorderServiceMixin,
)
from msdr.api.services.remoteid import RemoteidServiceMixin
from msdr.api.services.review import (
    _CLIP_MAX_S,  # noqa: F401
    ReviewServiceMixin,
    _ClipExistsError,  # noqa: F401
    _write_clip,  # noqa: F401
    _write_clip_files,  # noqa: F401
)
from msdr.api.services.rules import RulesServiceMixin
from msdr.api.services.scanner import ScannerServiceMixin
from msdr.api.services.sky import (
    SkyServiceMixin,
    _ics_escape,  # noqa: F401
    _ics_utc,  # noqa: F401
    _passes_ics,  # noqa: F401
)
from msdr.api.services.survey import SurveyServiceMixin
from msdr.api.services.wifi import WifiServiceMixin
from msdr.catalog.store import CatalogRepo
from msdr.engine.confirmed import ConfirmedState, matches
from msdr.engine.dwell import DwellObserver
from msdr.engine.history import HistoryStack
from msdr.engine.lna_auto import auto_lna_state, backoff_state, band_index
from msdr.engine.restore import (
    RestoreExecutor,
    RestoreReport,
    SetStep,
    Step,
    StepOutcome,
    plan_with_desired,
)
from msdr.engine.scanner import (
    ScannerEngine,
)
from msdr.engine.state import Observation, ProfileSettings, ReceiverState, TuneRecipe
from msdr.integrations.manager import IntegrationManager
from msdr.journal.service import JournalService
from msdr.library.models import Signal, now_iso
from msdr.observations.legacy import backup_db, import_legacy, legacy_import_done
from msdr.observations.recorder import ObservationRecorder
from msdr.observations.repo import ObservationRepo
from msdr.providers.bandplan import BandPlanProvider
from msdr.providers.iqscan import IqScanProvider, IqScanRunner
from msdr.providers.nextpass import NextPassProvider
from msdr.providers.tracking import (
    DopplerLoop,
    SatTracker,
    resolve_location,
)
from msdr.radio.protocol import ConnectionChanged, PropertyChanged, RadioAdapter
from msdr.radio.registry import READ_ONLY_SEED, RESTORABLE
from msdr.settings import Settings, available_timezone_names, effective_timezone
from msdr.storage.db import Database
from msdr.storage.repos import (
    BeaconHitRepo,
    DecodeEventRepo,
    FindRepo,
    HistoryRepo,
    JournalRepo,
    LibraryUndoRepo,
    ListRepo,
    ProfileRepo,
    SavedSearchRepo,
    SettingsRepo,
    SignalRepo,
    StateRepo,
    TagRepo,
)
from msdr.survey.bandscan import (
    BandScan,
    BandScanError,  # noqa: F401 -- re-exported for app.py
    BandScanRepo,
)
from msdr.wifi.sessions import WifiRecordingSession, WifiSessionStore

logger = logging.getLogger(__name__)


_DISCONNECTED_HINT_S = 2.0


_DISCONNECTED_HINT = "Enable SDRconnect ▸ Preferences ▸ WebSocket Server (port 5454)"


# review_bookmarks_list's client-side page (S3): generous enough that a
# recent bookmark is never missed, small enough to stay cheap without a
# dedicated "list by recording path" repo query.

# Settings fields typed to accept None (e.g. `location: Location | None`) --
# apply_settings uses this so an explicit `null` in a PATCH (the route already
# drops unset keys via exclude_unset) clears the field, while a null for a
# non-nullable field is still ignored rather than failing Settings validation.
_NULLABLE_SETTINGS_FIELDS = {
    name for name, f in Settings.model_fields.items() if type(None) in typing.get_args(f.annotation)
}


# signal_power/signal_snr/overload are read-only, so ConfirmedState.apply()
# updates them silently and never emits a StateChanged -- nothing would ever
# reach the browser and its S-meter would freeze. They also change far faster
# than a UI needs, so publish at most this often and let bursts coalesce onto
# the next slot (the publish always carries the latest observation).
_TELEMETRY_PROPS = frozenset({"signal_power", "signal_snr", "overload"})


_TELEMETRY_MIN_INTERVAL_S = 0.5


# --- receiver owner (U1) ---------------------------------------------------
# Which controller is currently driving the VFO: scanner > tracking > beacons
# > manual (see AppService._owner_payload). "also" lists any other retuning
# controller that's active at the same time (rare -- each of these normally
# refuses to start while another one owns the receiver).
_OWNER_PRIORITY = ("scanner", "tracking", "beacons")


_OWNER_LABELS = {"scanner": "SCANNER", "beacons": "BEACONS"}


# GET /api/owner/conflicts?action=... -> which currently-active kinds would
# be interrupted (retuned out from under, or -- for "beacons" -- simply
# refused) by starting `action`. Mirrors the refusal rules the services
# themselves enforce (see AppService._check_beacons_refusal): scanner/
# tracking/beacons all retune the VFO, so starting any one of them conflicts
# with the other two; a survey span change retunes too, and also disturbs any
# band-decode pins; band decode/analyzer/record don't retune (Facts) so
# starting one of those has no interrupts today.
_OWNER_CONFLICT_RULES: dict[str, tuple[str, ...]] = {
    "scan": ("tracking", "beacons"),
    "track": ("scanner", "beacons"),
    "beacons": ("scanner", "tracking", "band_decode", "analyzer", "recording"),
    "survey_span": ("scanner", "tracking", "beacons", "band_decode"),
    "band_decode": (),
    "analyzer": (),
    "record": (),
}


def _iso_to_epoch(iso: str | None) -> float | None:
    """now_iso()'s "...Z" format -> Unix epoch seconds, for owner.since (the
    scanner only tracks started_at as an ISO string -- see ScannerEngine)."""
    if iso is None:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# Per-subscriber queue bound: unbounded queues let one slow browser (tab
# backgrounded, laptop asleep, flaky wifi) accumulate messages forever.
_MAX_QUEUE_SIZE = 512

# A full queue's publishes are counted; if it stays full this long (either
# measure trips first) the client is presumed gone and disconnected so it
# reconnects with a clean queue instead of trailing further behind.
_SLOW_CONSUMER_FULL_STREAK = 2000
_SLOW_CONSUMER_FULL_SECONDS = 30.0

# Message types that are a full snapshot of current status rather than a
# discrete one-off event -- a newer one fully supersedes an older one, so a
# full queue can drop the stale copy instead of a message a client would
# otherwise never see at all (a journal entry, a scan hit, ...).
_COALESCABLE_TYPES = frozenset(
    {
        "state",
        "telemetry",
        "tracking",
        "owner",
        "recorder",
        "scan",
        "history",
        "connection",
        "wifi",
        "ble",
        "remoteid",
    }
)


class _BoundedQueue(asyncio.Queue):
    """A subscriber's queue plus the bookkeeping Broadcaster needs to notice
    and disconnect a slow consumer (see Broadcaster.publish)."""

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self.dropped = 0
        self.full_streak = 0
        self.full_since: float | None = None
        self.slow = False
        # Set when a non-coalescable message (one the client would otherwise
        # never see again -- a decoder event, journal entry, ...) was dropped
        # because the queue was full and no coalescable entry could be
        # evicted to make room. next_outgoing() turns this into a one-shot
        # "resync" marker so the browser knows to reload instead of silently
        # missing events while looking connected.
        self.resync_required = False


class Broadcaster:
    """Fan-out of JSON-able dicts to browser sockets; each subscriber gets its own
    bounded queue. A client that falls behind has same-type status messages
    coalesced (newest kept, oldest dropped) and, if its queue stays
    persistently full, is marked slow so the /ws endpoint disconnects it.
    """

    def __init__(self) -> None:
        self._subscribers: list[_BoundedQueue] = []

    def subscribe(self) -> _BoundedQueue:
        q = _BoundedQueue(_MAX_QUEUE_SIZE)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: _BoundedQueue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, msg: dict) -> None:
        for q in self._subscribers:
            self._deliver(q, msg)

    def _deliver(self, q: _BoundedQueue, msg: dict) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            self._handle_full(q, msg)
            return
        q.full_streak = 0
        q.full_since = None

    def _coalesce(self, q: _BoundedQueue, msg: dict) -> bool:
        """Drop the oldest queued message of the same (supersedable) type to
        make room for `msg`, keeping only the newest. Returns whether a slot
        was freed this way."""
        msg_type = msg.get("type")
        if msg_type not in _COALESCABLE_TYPES:
            return False
        pending: list[dict] = []
        found = False
        while not q.empty():
            item = q.get_nowait()
            if not found and item.get("type") == msg_type:
                found = True
                continue
            pending.append(item)
        for item in pending:
            q.put_nowait(item)
        if found:
            q.put_nowait(msg)
        return found

    def _evict_coalescable(self, q: _BoundedQueue) -> bool:
        """Drop the oldest coalescable (snapshot-type) entry in the queue to
        free a slot for a non-coalescable message that would otherwise be
        dropped outright. Returns whether an entry was evicted."""
        pending: list[dict] = []
        evicted = False
        while not q.empty():
            item = q.get_nowait()
            if not evicted and item.get("type") in _COALESCABLE_TYPES:
                evicted = True
                continue
            pending.append(item)
        for item in pending:
            q.put_nowait(item)
        return evicted

    def _handle_full(self, q: _BoundedQueue, msg: dict) -> None:
        msg_type = msg.get("type")
        delivered = self._coalesce(q, msg)
        if not delivered and msg_type not in _COALESCABLE_TYPES and self._evict_coalescable(q):
            q.put_nowait(msg)
            delivered = True
        if not delivered:
            q.dropped += 1
            if msg_type not in _COALESCABLE_TYPES:
                # The client will never see this one again unless we tell it
                # to resync -- mark it so next_outgoing() sends the marker.
                q.resync_required = True
        q.full_streak += 1
        if q.full_since is None:
            q.full_since = time.monotonic()
        if q.slow:
            return
        stalled_long = time.monotonic() - q.full_since >= _SLOW_CONSUMER_FULL_SECONDS
        if q.full_streak >= _SLOW_CONSUMER_FULL_STREAK or stalled_long:
            q.slow = True
            logger.warning(
                "broadcaster: client queue stuck full (%d publishes, %.1fs) -- "
                "disconnecting so it reconnects fresh (%d messages dropped)",
                q.full_streak,
                time.monotonic() - q.full_since,
                q.dropped,
            )


async def next_outgoing(q: _BoundedQueue) -> dict:
    """What the /ws handler should send this subscriber next.

    If a non-coalescable message was dropped since the last delivery, a
    one-shot resync marker takes priority over whatever's queued -- the
    browser reloads the histories it fetches at startup rather than
    silently missing events it never sees queued. Otherwise, the next
    queued message (blocking until one arrives).
    """
    if q.resync_required:
        dropped = q.dropped
        q.resync_required = False
        q.dropped = 0
        return {"type": "resync", "reason": "events_dropped", "dropped": dropped}
    return await q.get()


class AppService(
    AllocationServiceMixin,
    SkyServiceMixin,
    RecorderServiceMixin,
    DecoderServiceMixin,
    SurveyServiceMixin,
    BeaconServiceMixin,
    ScannerServiceMixin,
    IqScanServiceMixin,
    CatalogServiceMixin,
    LibraryServiceMixin,
    ReviewServiceMixin,
    ListenServiceMixin,
    ObservationServiceMixin,
    WifiServiceMixin,
    BleServiceMixin,
    HistoryServiceMixin,
    RemoteidServiceMixin,
    RulesServiceMixin,
):
    def __init__(
        self,
        settings: Settings,
        adapter: RadioAdapter,
        db: Database,
        settings_path: Path | None = None,
        # Pre-roll recorder, injectable for tests -- same idea as `adapter`
        # above. None builds the real msdr.recorder.Recorder (or leaves the
        # feature unavailable if that package can't be imported).
        recorder=None,
        # Signal decoder manager, injectable for tests -- same idea as
        # `recorder` above. None builds the real msdr.decoder.DecoderManager
        # (or leaves the feature unavailable if that package can't be imported).
        decoder=None,
        # Wide-view survey, injectable for tests -- same idea as `decoder`
        # above. None builds the real msdr.survey.Survey (or leaves the
        # feature unavailable if that package can't be imported).
        survey=None,
        # Beacon monitor, injectable for tests -- same idea as `survey`
        # above. None builds the real msdr.survey.beacons.BeaconMonitor (or
        # leaves the feature unavailable if that package can't be imported).
        beacons=None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.db = db
        # Where PATCH /api/settings persists to; None means Settings' own
        # default path. Carried explicitly so a --settings override on the
        # CLI is honoured by live edits too, not just the initial load.
        self.settings_path = settings_path

        self.confirmed = ConfirmedState()
        self.history = HistoryStack()
        self.broadcaster = Broadcaster()
        # External digital-mode apps are always separate processes. The
        # integration manager only opens local receive/status APIs and sends
        # normalized events into the same WS broadcaster as other live data.
        self.integrations = IntegrationManager(
            settings.integrations,
            on_event=self.broadcaster.publish,
        )

        self.signals = SignalRepo(db)
        self.profiles = ProfileRepo(db)
        # The profile the operator last applied (directly or via a signal's
        # profile). Only it can be reported as active -- see state_payload.
        self._applied_profile_id: str | None = None
        self.tags = TagRepo(db)
        self.states = StateRepo(db)
        self.settings_repo = SettingsRepo(db)
        self.history_repo = HistoryRepo(db)
        self.journal_repo = JournalRepo(db)
        self.library_undo = LibraryUndoRepo(db)
        self.lists = ListRepo(db)
        self.saved_searches = SavedSearchRepo(db)
        self.decode_events = DecodeEventRepo(db)
        self.beacon_hits = BeaconHitRepo(db)
        self.finds = FindRepo(db)
        # Observations and honest evidence (S2): sessions/observations/claims
        # over decode events, scanner hits and survey labels.
        self.observations = ObservationRepo(db)
        self.obs_recorder = ObservationRecorder(self.observations)
        # Ignored-find frequencies, cached so the scanner's skip_ignored check
        # (called per channel, synchronously) never hits the db -- refreshed
        # whenever a find's status changes (see _refresh_ignored_finds).
        self._ignored_find_freqs: set[int] = set()
        self.journal = JournalService(
            self.journal_repo, self.signals, self.states, nearby_hz=settings.nearby_hz
        )
        # Built in start(), not here: CatalogRepo's FTS5 bootstrap needs
        # db.connect() to have already run.
        self.catalog: CatalogRepo
        self.identify_provider = BandPlanProvider(settings.region)
        # Frequency allocation reference (docs/allocations.md): bundled
        # dataset overlaid by the user data dir's import, all in memory.
        self.allocations = AllocationStore(settings.allocation_data_dir)
        self.nextpass_provider = NextPassProvider(settings.nextpass_bin)
        self.iqscan_provider = IqScanProvider(settings.iqscan_scan_root)
        self.iqscan_runner = IqScanRunner(
            settings.iqscan_bin, settings.iqscan_scan_root, on_status=self._publish_scan_job
        )
        self.tle_store = self._build_tle_store(settings)
        self.tracker = SatTracker(self.tle_store, resolve_location(settings), self._extra_catalog())
        self.doppler_loop = DopplerLoop(self)

        self.dwell = DwellObserver(
            self.confirmed, dwell_seconds=settings.dwell_seconds, on_dwell=self._on_dwell
        )
        # own_subscription=False: AppService's own event task is the single
        # adapter consumer (see _consume_events); the executor rides along
        # via feed() instead of subscribing itself.
        self._executor = RestoreExecutor(adapter, self.confirmed, own_subscription=False)
        # One SpectrumHub shared by the scanner's own spectrum source and
        # Survey (msdr.survey, S1) -- both enable/read the same SDRconnect
        # spectrum stream, refcounted so it's only live while one of them
        # needs it. None when msdr.survey isn't importable (see guard above).
        self.spectrum_hub = SpectrumHub(self.adapter) if SpectrumHub is not None else None
        # The scanner drives its own RestoreExecutor (own_subscription=True)
        # rather than sharing self._executor: a manual tune must be able to
        # go through and pause the scan (per the design spec) instead of
        # always losing a race for the one shared executor.
        scanner_kwargs = {}
        if Survey is not None:
            scanner_kwargs["spectrum_hub"] = self.spectrum_hub
        self.scanner = ScannerEngine(
            adapter,
            self.confirmed,
            self.lists,
            self.signals,
            self.dwell,
            get_nearby_hz=lambda: self.settings.nearby_hz,
            capture=self.capture,
            on_status=self._publish_scan_status,
            on_hit=self._publish_scan_hit,
            on_find=self._on_find,
            classify=self._classify_find,
            is_ignored=self._is_find_ignored,
            **scanner_kwargs,
        )
        self.recorder = recorder if recorder is not None else self._build_recorder(settings)
        # One-shot: re-arm on the first moment we're connected+started, if the
        # last session left it armed (see _maybe_rearm_recorder). Cleared
        # after the first attempt, success or failure -- this is a startup
        # convenience, not a standing "keep armed" policy.
        self._recorder_rearm_pending = settings.recorder_armed and self.recorder is not None
        self._recorder_running = False
        self._recorder_runtime_lock = asyncio.Lock()

        self.decoder = decoder if decoder is not None else self._build_decoder(settings)
        self._decoder_running = False
        self._decoder_runtime_lock = asyncio.Lock()
        self._signal_test_lock = asyncio.Lock()

        self.survey = survey if survey is not None else self._build_survey(settings)
        self._survey_running = False
        self._survey_runtime_lock = asyncio.Lock()
        self._band_scan_active = False
        self.band_scan = BandScan(
            BandScanRepo(db),
            self._band_scan_tune_span,
            self._band_scan_confirmed_span,
            self._band_scan_labels,
            publish=self._publish_survey_message,
            snapshot=self._band_scan_snapshot,
        )
        # Review (S3): a VirtualReceiver bound to the survey's replay
        # ReplaySource only -- never the live adapter (see
        # _attach_virtual_receiver/_detach_virtual_receiver). None while idle
        # or while surveying the live radio.
        self.virtual_rx: VirtualReceiver | None = None
        from msdr.engine.workbench import SignalWorkbench

        self.workbench = SignalWorkbench(self)
        # Review decode (S4): a ReplayDecodeSession bound to the same replay
        # as self.virtual_rx -- never a live decode, always tagged with a
        # source_id starting "replay:" and an analysis_run_id (see
        # review_decode_start/_publish_replay_decode_message). None while no
        # decode is running against the current replay.
        self._replay_decode: ReplayDecodeSession | None = None
        # ReplaySource, VirtualReceiver and ReplayDecodeSession form one
        # lifecycle. Serialize source changes and decode start/stop so an old
        # decoder can never outlive (or publish against) a replacement source.
        self._review_lifecycle_lock = asyncio.Lock()
        self._review_decode_generation = 0
        # S5: the Observation (provenance="recording") for the recording in
        # progress -- opened at REC, closed once the recorder has finalized
        # (or failed). See _open_recording_observation/_sync_recording_observation.
        self._rec_obs: dict | None = None
        # One /ws/audio consumer at a time (S3 contract: "one consumer per
        # socket") -- a second concurrent connection is told to go away
        # rather than racing the first for the receiver's single audio queue.
        self._audio_ws_active = False

        self.beacons = beacons if beacons is not None else self._build_beacons(settings)
        self._beacons_running = False
        self._beacons_runtime_lock = asyncio.Lock()

        # Passive Wi-Fi reception (docs/wifi.md) -- entirely independent of
        # the radio adapter/receiver ownership; see WifiServiceMixin.
        self.wifi = self._build_wifi(settings)
        self._wifi_settings_pending = False
        self._wifi_settings_lock = asyncio.Lock()
        self.wifi_sessions = WifiSessionStore(self._wifi_sessions_root())
        self._wifi_session: WifiRecordingSession | None = None
        self._wifi_last_push: float = 0.0
        self._wifi_last_device: dict | None = None  # from the last --list (wifi_devices())
        # Channel survey (docs/history.md "Wi-Fi channel survey"): the running
        # ChannelSurvey (if any), the task watching it for natural completion
        # so capture can be restored, and what capture was doing before the
        # survey started (restored once it ends or is stopped).
        self._wifi_survey = None
        self._wifi_survey_watch_task: asyncio.Task | None = None
        self._wifi_survey_stop_task: asyncio.Task | None = None
        self._wifi_survey_prior: dict | None = None

        # Passive BLE reception (docs/ble.md) -- same independence from the
        # radio adapter/receiver ownership as Wi-Fi above; see BleServiceMixin.
        self.ble = self._build_ble(settings)
        self._ble_settings_pending = False
        self._ble_settings_lock = asyncio.Lock()
        self._ble_last_push: float = 0.0
        self._ble_last_device: dict | None = None  # from the last --list (ble_devices())

        # Device history and labels (docs/history.md); see HistoryServiceMixin.
        self._build_history(settings)

        # Live Remote ID (docs/remote-id.md "Live"); see RemoteidServiceMixin.
        self._build_remoteid()

        # Watch rules (docs/rules.md); see RulesServiceMixin.
        self._build_rules()

        self._adapter_q: asyncio.Queue | None = None
        self._confirmed_q: asyncio.Queue | None = None
        self._event_task: asyncio.Task | None = None
        self._state_task: asyncio.Task | None = None
        self._hint_task: asyncio.Task | None = None
        # Tracks whether `self.dwell` is actually subscribed/running, since
        # DwellObserver.start() isn't itself idempotent -- apply_settings
        # needs this to start/stop it safely as journal_passive flips live.
        self._dwell_running = False
        self._telemetry_task: asyncio.Task | None = None
        # Frequency-adaptive LNA: the band's starting LNA state (overload
        # backoff counts steps from it) and the running overload watch.
        self._lna_baseline: int | None = None
        self._lna_overload_task: asyncio.Task | None = None
        self._telemetry_at: float = float("-inf")
        self._recorder_task: asyncio.Task | None = None
        # Owner chip (U1): DopplerLoop doesn't track its own start time (see
        # _owner_payload), so this is set/cleared there the first/last time
        # tracking is observed active -- scanner has its own started_at, and
        # BeaconMonitor.status()["since"] is already a float, so neither
        # needs an equivalent here.
        self._tracking_since: float | None = None

    async def start(self) -> None:
        self.db.connect()
        self.band_scan.recover()
        # Observations legacy import (S2): a one-time backfill from finds/
        # decode_events, idempotent via the `observations.legacy_import_done`
        # settings key -- backup only ahead of the run that will actually
        # import something, not on every later startup.
        if not legacy_import_done(self.db):
            backup_db(self.settings.db_path)
        import_counts = import_legacy(self.db, self.observations)
        logger.info("observations: legacy import counts=%s", import_counts)
        self.catalog = CatalogRepo(self.db)
        self.tags.ensure_categories()
        self._refresh_ignored_finds()
        await self._start_history_runtime()
        await self._start_remoteid_runtime()
        await self._start_rules_runtime()

        state_ids, cursor = self.history_repo.load()
        loaded = self.states.get_many(state_ids)
        by_id = {state.id: state for state in loaded}
        # get_many() silently drops ids it can't find; a missing id at or
        # before the cursor shifts every later index left by one, so the
        # cursor has to move with it or back()/forward() land on the wrong
        # entry after restart.
        dropped_before_cursor = sum(
            1 for i, sid in enumerate(state_ids) if sid not in by_id and i <= cursor
        )
        states = [by_id[sid] for sid in state_ids if sid in by_id]
        self.history = HistoryStack.load(states, cursor - dropped_before_cursor)

        start = getattr(self.adapter, "start", None)
        if start is not None:
            await start()

        if self.settings.features.journal_passive:
            await self.dwell.start()
            self._dwell_running = True

        self._adapter_q = self.adapter.subscribe()
        self._event_task = asyncio.create_task(self._consume_events())
        self._confirmed_q = self.confirmed.subscribe()
        self._state_task = asyncio.create_task(self._consume_state_changes())
        self._hint_task = asyncio.create_task(self._hint_if_disconnected())

        if self.settings.features.recorder:
            await self._start_recorder_runtime()

        if self.settings.features.decoder:
            await self._start_decoder_runtime()

        await self.integrations.start()

        # The hub fans adapter SpectrumFrames out to the scanner and the survey;
        # without its task nothing downstream ever sees a frame (live check
        # 2026-09-25). It runs regardless of the survey flag: the scanner's
        # spectrum source goes through it too.
        if self.spectrum_hub is not None:
            await self.spectrum_hub.start()

        if self.settings.features.survey:
            await self._start_survey_runtime()

        if self.settings.features.beacons:
            await self._start_beacons_runtime()

        # Retention prune of Wi-Fi packet sessions: only when Wi-Fi is enabled,
        # and never fatal -- an unreadable or unmounted recordings root must not
        # stop the app (the recordings API reports that state on its own).
        if self.settings.wifi.enabled:
            try:
                self.wifi_sessions.prune(self.settings.wifi.retention_days)
            except OSError as exc:
                logger.warning("wifi session retention prune skipped: %s", exc)

        if self.adapter.connected:
            # Real adapters emit ConnectionChanged(True) once connected after
            # start(); fakes/tests are already connected before we subscribed
            # above, so there's no event to react to -- seed directly.
            await self._seed()
            await self._maybe_rearm_recorder()

    async def stop(self) -> None:
        await self.workbench.stop()
        if self._band_scan_active:
            try:
                await self.band_scan.stop()
            finally:
                self._band_scan_active = False
        if self.scanner.state != "idle":
            await self.scanner.stop()
        await self.doppler_loop.stop()
        await self.iqscan_runner.cancel_all()
        await self._stop_recorder_runtime()
        await self._stop_decoder_runtime()
        await self.integrations.stop()
        async with self._review_lifecycle_lock:
            await self._detach_virtual_receiver()
            await self._stop_survey_runtime()
        await self._stop_beacons_runtime()
        await self._wifi_survey_shutdown()
        self._wifi_finalize_session("app shutdown")
        await self.wifi.stop()
        await self.ble.stop()
        # After both receivers stop, so their last observations are flushed.
        await self._stop_history_runtime()
        await self._stop_remoteid_runtime()
        await self._stop_rules_runtime()
        if self.spectrum_hub is not None:
            await self.spectrum_hub.stop()
        for task in (
            self._hint_task, self._state_task, self._event_task, self._telemetry_task,
            self._lna_overload_task,
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._hint_task = self._state_task = self._event_task = self._telemetry_task = None
        self._lna_overload_task = None

        if self._dwell_running:
            await self.dwell.stop()
            self._dwell_running = False

        if self._confirmed_q is not None:
            self.confirmed.unsubscribe(self._confirmed_q)
            self._confirmed_q = None
        if self._adapter_q is not None:
            self.adapter.unsubscribe(self._adapter_q)
            self._adapter_q = None

        stop = getattr(self.adapter, "stop", None)
        if stop is not None:
            await stop()

        try:
            await self.band_scan.repo.aclose()
        finally:
            self.db.close()

    async def _hint_if_disconnected(self) -> None:
        await asyncio.sleep(_DISCONNECTED_HINT_S)
        if not self.adapter.connected:
            logger.info(_DISCONNECTED_HINT)

    async def _seed(self) -> None:
        props: dict[str, object] = {}
        failed: list[str] = []
        for name in [spec.name for spec in RESTORABLE] + list(READ_ONLY_SEED):
            try:
                props[name] = await self.adapter.get(name)
            except TimeoutError:
                failed.append(name)
        if "api_version" in props:
            self.confirmed.api_version = str(props["api_version"])
        self.confirmed.seed(props)
        # The recorder only ever saw *changes* (see _consume_events), so a
        # radio that was already playing at connect time left it believing
        # started=False/rate unknown until something changed -- ARM stayed
        # 409 "radio not started" (S5 workflow test). Seed it like confirmed.
        if self.recorder is not None:
            for name in _RECORDER_FORWARD_PROPS:
                if name in props:
                    self.recorder.on_property(name, props[name])
        if failed:
            # Any restorable prop missing here leaves confirmed state stale,
            # so capture/restore stay 503 until it can be read -- say which.
            logger.warning("seed: no response for %s", ", ".join(failed))

    async def _consume_events(self) -> None:
        assert self._adapter_q is not None
        while True:
            ev = await self._adapter_q.get()
            try:
                if isinstance(ev, PropertyChanged):
                    # The app state and controls target SDRconnect's primary
                    # receiver. RSPduo can publish same-named properties for
                    # its secondary tuner on this socket; ignore those before
                    # they reach any service-side lifecycle handlers.
                    if ev.device != "primary":
                        continue
                    if self._executor.running:
                        self._executor.feed(ev)  # feed() applies to confirmed itself
                    else:
                        self.confirmed.apply(ev)
                    if ev.property == "overload":
                        self._on_overload(bool(ev.value))
                    if ev.property in _TELEMETRY_PROPS and self.settings.features.telemetry:
                        self._note_telemetry()
                    if (
                        self.recorder is not None
                        and ev.device == "primary"
                        and ev.property in _RECORDER_FORWARD_PROPS
                    ):
                        self.recorder.on_property(ev.property, ev.value)
                    if ev.device == "primary" and ev.property in {
                        "started", "device_center_frequency", "device_sample_rate"
                    }:
                        await self.workbench.receiver_changed()
                    if ev.property == "can_control":
                        # `can_control` is read-only and does not emit a
                        # StateChanged. `started` does emit one so lifecycle
                        # consumers (including the UI) get the same path as
                        # other primary receiver state changes.
                        await self.broadcaster.publish({"type": "state", **self.state_payload()})
                    if ev.property == "started":
                        await self._maybe_rearm_recorder()
                elif isinstance(ev, ConnectionChanged):
                    if self.recorder is not None:
                        self.recorder.on_property("connected", ev.connected)
                    if ev.connected:
                        await self._seed()
                        await self._maybe_rearm_recorder()
                    else:
                        # Whatever the old connection reported may already be
                        # wrong by the time we come back; don't let a reconnect
                        # read that times out keep it alive.
                        self.confirmed.mark_stale()
                        if self.scanner.state != "idle":
                            await self.scanner.pause("disconnected")
                    await self.workbench.receiver_changed()
                    await self.broadcaster.publish(
                        {"type": "connection", "connected": ev.connected}
                    )
                    # Always follow with "state": on connect, a UI that
                    # connected while SDRconnect was down never had a "state"
                    # message to sit on top of (F4). On disconnect,
                    # mark_stale() flips synced to False without going through
                    # ConfirmedState's own subscribers, so this is the only
                    # "state" push that would tell a connected UI about it.
                    await self.broadcaster.publish({"type": "state", **self.state_payload()})
            except Exception:
                # This is the sole adapter consumer -- letting an exception
                # escape (e.g. _seed() hitting ConnectionError on a flapping
                # reconnect) would kill the task and freeze confirmed state
                # forever. Log and keep consuming.
                logger.exception("error handling adapter event %r", ev)

    def _note_telemetry(self) -> None:
        if self._telemetry_task is not None and not self._telemetry_task.done():
            return  # a publish is already pending; it will carry the newest reading
        self._telemetry_task = asyncio.create_task(self._publish_telemetry())

    async def _publish_telemetry(self) -> None:
        delay = self._telemetry_at + _TELEMETRY_MIN_INTERVAL_S - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._telemetry_at = time.monotonic()
        await self.broadcaster.publish(
            {
                "type": "telemetry",
                "observation": self.confirmed.observation().model_dump(mode="json"),
            }
        )

    async def _publish_history(self) -> None:
        await self.broadcaster.publish({"type": "history", **self.state_payload()["history"]})

    async def _consume_state_changes(self) -> None:
        assert self._confirmed_q is not None
        while True:
            await self._confirmed_q.get()
            await self.broadcaster.publish({"type": "state", **self.state_payload()})

    async def _on_dwell(self, state: ReceiverState, observation: Observation) -> None:
        entry = self.journal.record_observed(state, observation)
        await self.broadcaster.publish({"type": "journal", "entry": entry.model_dump(mode="json")})

    # --- receiver owner (U1) ------------------------------------------------

    def _owner_active_kinds(self) -> dict[str, bool]:
        """Which controllers are currently active -- used both for the owner
        chip (scanner/tracking/beacons) and the conflicts matrix (which also
        needs band_decode/analyzer/recording, mirroring
        _check_beacons_refusal's own checks)."""
        beacons_status = self.beacons.status() if self.beacons is not None else None
        survey_status = self.survey.status() if self.survey is not None else None
        decoder_status = self.decoder.status() if self.decoder is not None else None
        recorder_status = self.recorder.status() if self.recorder is not None else None
        return {
            "scanner": self.scanner.state != "idle",
            "tracking": self.doppler_loop.status is not None,
            "beacons": bool(beacons_status and beacons_status.get("active")),
            "band_decode": bool(
                decoder_status and (decoder_status.get("band") or {}).get("active")
            ),
            "analyzer": bool(survey_status and (survey_status.get("analyzer") or {}).get("active")),
            "recording": bool(
                recorder_status and recorder_status.get("state") in ("recording", "finalizing")
            ),
        }

    def _owner_payload(self) -> dict:
        """state_payload()["owner"] (contract item 1): which controller is
        driving the VFO right now, priority scanner > tracking > beacons >
        manual; any other retuning controller active at the same time is
        listed in "also"."""
        if self.workbench.owns_receiver:
            return {"kind": "watch", "label": "WATCH", "since": None,
                    "stop": "/api/owner/stop"}
        active = self._owner_active_kinds()
        tracking_status = self.doppler_loop.status
        if active["tracking"]:
            if self._tracking_since is None:
                self._tracking_since = time.time()
        else:
            self._tracking_since = None

        present = [kind for kind in _OWNER_PRIORITY if active[kind]]
        if not present:
            return {"kind": "manual", "label": "MANUAL", "since": None, "stop": None}

        kind = present[0]
        also = present[1:]
        if kind == "scanner":
            label = _OWNER_LABELS["scanner"]
            since = _iso_to_epoch(self.scanner.status(all_hits=False).get("started_at"))
        elif kind == "tracking":
            satellite = (tracking_status or {}).get("satellite")
            label = f"TRACKING {satellite}" if satellite else "TRACKING"
            since = self._tracking_since
        else:
            label = _OWNER_LABELS["beacons"]
            beacons_status = self.beacons.status() if self.beacons is not None else {}
            since = beacons_status.get("since")

        payload = {"kind": kind, "label": label, "since": since, "stop": "/api/owner/stop"}
        if also:
            payload["also"] = also
        return payload

    async def _publish_owner(self) -> None:
        await self.broadcaster.publish({"type": "owner", **self._owner_payload()})

    async def owner_stop(self) -> dict:
        """POST /api/owner/stop (contract item 2): stops whichever controller
        is currently reported as owner.kind. A no-op while manual."""
        owner = self._owner_payload()
        kind = owner["kind"]
        if kind == "watch":
            await self.workbench.stop()
        elif kind == "scanner":
            await self.scanner.stop()
        elif kind == "tracking":
            await self.doppler_loop.stop()
        elif kind == "beacons" and self.beacons is not None:
            await self.beacons.stop_session("owner_stop")
        return self._owner_payload()

    def owner_conflicts(self, action: str) -> dict:
        """GET /api/owner/conflicts?action=... (contract item 3): which
        currently-active kinds would be interrupted (or, for beacons,
        refused) by starting `action` -- see _OWNER_CONFLICT_RULES."""
        if action not in _OWNER_CONFLICT_RULES:
            raise HTTPException(422, f"unknown action: {action!r}")
        active = self._owner_active_kinds()
        interrupts = [kind for kind in _OWNER_CONFLICT_RULES[action] if active.get(kind)]
        message = f"This will stop {', '.join(interrupts)}." if interrupts else ""
        return {"interrupts": interrupts, "message": message}

    def _next_retry_s(self) -> float | None:
        # retry_deadline is adapter-specific (fakes without it just have no
        # backoff to report), hence getattr rather than a RadioAdapter method.
        deadline = getattr(self.adapter, "retry_deadline", None)
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def retry_connection(self) -> dict:
        retry_now = getattr(self.adapter, "retry_now", None)
        if retry_now is not None:
            retry_now()
        return {"connected": self.adapter.connected, "next_retry_s": self._next_retry_s()}

    def state_payload(self) -> dict:
        props = self.confirmed.props
        # Profiles hold only antenna/LNA/rate/audio settings, so guessing from
        # a match labelled unrelated tunings with whatever profile shared two
        # of them. Report only the applied profile, and only while the
        # receiver still matches it; any change drops it back to "custom".
        active_profile_id = None
        if self._applied_profile_id is not None:
            applied = self.profiles.get(self._applied_profile_id)
            if applied is not None and applied.matches(props):
                active_profile_id = applied.id
            elif self.confirmed.is_complete():
                self._applied_profile_id = None

        vfo = props.get("device_vfo_frequency")
        nearby_signals: list[Signal] = []
        if vfo is not None and self.settings.features.nearby:
            # near() filters in SQL and returns nearest-first, so a match in a
            # large library can't be lost to signals.list()'s page limit.
            nearby_signals = self.signals.near(
                vfo,
                int(max((props.get("filter_bandwidth") or 0) / 2, self.settings.nearby_hz)),
            )

        # Tuned signal: the nearest signal within a tighter radius
        # (bandwidth-aware, but never less than 1000 Hz) whose demodulator
        # doesn't contradict what's currently confirmed. Queried separately
        # from nearby_signals (rather than reusing it) because settings.nearby_hz
        # can be configured below 1000 Hz, in which case nearby_signals' own
        # radius would be narrower than this one's and under-report candidates.
        tuned_signal_id = None
        if vfo is not None and self.settings.features.nearby:
            radius = int(max((props.get("filter_bandwidth") or 0) / 2, 1000))
            demodulator = props.get("demodulator")
            for signal in self.signals.near(vfo, radius):
                if signal.demodulator is None or signal.demodulator == demodulator:
                    tuned_signal_id = signal.id
                    break

        return {
            "connected": self.adapter.connected,
            "can_control": bool(props.get("can_control", False)),
            "started": bool(props.get("started", False)),
            "api_version": self.confirmed.api_version,
            "version": __version__,
            "complete": self.confirmed.is_complete(),
            "synced": self.confirmed.is_complete(),
            "next_retry_s": self._next_retry_s(),
            "props": props,
            "observation": self.confirmed.observation().model_dump(mode="json"),
            "active_profile_id": active_profile_id,
            "history": {
                "can_back": self.history.can_back(),
                "can_forward": self.history.can_forward(),
                "position": self.history.position,
                "length": len(self.history.entries),
            },
            "nearby": [s.model_dump(mode="json") for s in nearby_signals],
            "tuned_signal_id": tuned_signal_id,
            "location_ready": self.tracker.location is not None,
            "tracking": self.doppler_loop.status,
            "features": self.settings.features.model_dump(),
            "decoder": self.decoder.status() if self.decoder is not None else None,
            "survey": self.survey.status() if self.survey is not None else None,
            "beacons": self.beacons.status() if self.beacons is not None else None,
            "owner": self._owner_payload(),
            "timezone": self._timezone_payload(),
        }

    def _timezone_payload(self) -> dict:
        name, tz = effective_timezone(self.settings)
        offset = datetime.now(tz).utcoffset()
        return {
            "name": name,
            "auto": self.settings.timezone is None,
            "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        }

    def available_timezones(self) -> list[str]:
        return available_timezone_names()

    async def capture(self, recipe: TuneRecipe | None = None) -> Signal:
        if not self.confirmed.is_complete() or not self.adapter.connected:
            raise ConnectionError("not connected")
        state = self.confirmed.snapshot("capture")
        observation = self.confirmed.observation()
        # `recipe` is scanner-hits-only (ScannerEngine._maybe_capture_hit): a
        # manual override can retune the receiver mid-hold before this runs,
        # so the memory must use the channel that actually produced the hit,
        # not the receiver's current (possibly since-moved) state.
        frequency_hz = recipe.frequency_hz if recipe is not None else state.vfo_hz
        demodulator = recipe.demodulator if recipe is not None else state.demodulator
        filter_bandwidth_hz = (
            recipe.filter_bandwidth_hz if recipe is not None else state.filter_bandwidth_hz
        )
        signal = Signal(
            name=Signal.unknown_name(frequency_hz),
            status="unknown",
            frequency_hz=frequency_hz,
            demodulator=demodulator,
            filter_bandwidth_hz=filter_bandwidth_hz,
            origin_state_id=state.id,
            first_heard_at=now_iso(),
            last_heard_at=now_iso(),
            heard_count=1,
            antenna=state.antenna,
            lna_state=state.lna_state,
        )
        self.signals.add(signal)
        self.library_undo.record(f"capture {signal.name}", [(signal.id, None)])
        entry = self.journal.record_action(
            "capture", state=state, signal_id=signal.id, observation=observation
        )
        await self.broadcaster.publish({"type": "journal", "entry": entry.model_dump(mode="json")})
        await self.broadcaster.publish({"type": "inbox"})
        return signal

    async def tune_signal(self, signal_id: str) -> RestoreReport:
        signal = self.signals.get(signal_id)
        if signal is None:
            raise HTTPException(404, f"signal not found: {signal_id!r}")
        profile = None
        if signal.profile_id is not None:
            record = self.profiles.get(signal.profile_id)
            if record is not None:
                profile = record.settings()
        if signal.antenna is not None:
            # The signal's own antenna overrides its profile's (or, with no
            # profile, is the only thing this restore changes besides the
            # recipe) -- older signals without one leave the current antenna
            # unchanged, see Signal.antenna.
            profile = (profile or ProfileSettings()).model_copy(
                update={"antenna": signal.antenna}
            )
        if signal.lna_state is not None:
            # Same override for LNA; settings.lna_locked still wins (_restore).
            profile = (profile or ProfileSettings()).model_copy(
                update={"lna_state": signal.lna_state}
            )
        profile = self._with_auto_lna(profile, signal.frequency_hz)
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props, profile=profile, recipe=signal.recipe()
        )
        report = await self._restore(
            steps,
            desired=desired,
            kind="tune",
            signal_id=signal_id,
            profile_id=signal.profile_id,
        )
        if report.status in ("confirmed", "partial"):
            await self._sync_decoder_for_tune(signal, report)
        return report

    async def tune_frequency(
        self,
        hz: int,
        demodulator: str | None = None,
        filter_bandwidth_hz: int | None = None,
        manual_mode: bool = False,
    ) -> RestoreReport:
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props,
            profile=self._with_auto_lna(None, hz),
            recipe=TuneRecipe(
                frequency_hz=hz, demodulator=demodulator, filter_bandwidth_hz=filter_bandwidth_hz
            ),
        )
        return await self._restore(
            steps, desired=desired, kind="tune", manual_mode=manual_mode
        )

    async def set_antenna(self, antenna: str) -> RestoreReport:
        """POST /api/receiver/antenna. Goes through the same staged
        restore/confirm/rollback path as tune_frequency (so it gets its
        readiness checks, including the 409s a running restore/survey band
        scan/cycling signal watch already refuse tuning with -- see
        AppService._restore), but as its own `kind` and with only `antenna`
        in `desired`: unlike a retune, this never touches
        device_vfo_frequency/device_center_frequency/demodulator, so
        ScannerEngine._watch_overrides (keyed on exactly those three) and
        DopplerLoop._watch_external (device_vfo_frequency only) never see it
        and never pause/stop scanning or tracking over it, and a band decode/
        analyzer/recording in progress isn't touched either.
        """
        valid = [a.strip() for a in self.confirmed.props.get("valid_antennas", "").split(",")]
        valid = [a for a in valid if a]
        if valid and antenna not in valid:
            raise ValueError(f"invalid antenna: {antenna!r} (valid: {', '.join(valid)})")
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props, profile=ProfileSettings(antenna=antenna)
        )
        return await self._restore(steps, desired=desired, kind="antenna")

    async def set_lna(self, lna_state: int) -> RestoreReport:
        """POST /api/receiver/lna. Same staged restore path as set_antenna,
        as its own `kind` so the LNA lock lets it through."""
        low = self.confirmed.props.get("lna_state_min")
        high = self.confirmed.props.get("lna_state_max")
        if (low is not None and lna_state < low) or (high is not None and lna_state > high):
            raise ValueError(f"invalid LNA state: {lna_state} (valid: {low}-{high})")
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props, profile=ProfileSettings(lna_state=lna_state)
        )
        # A hand-set LNA is the new reference for overload backoff.
        self._lna_baseline = lna_state
        return await self._restore(steps, desired=desired, kind="lna")

    def _with_auto_lna(self, profile: ProfileSettings | None, target_hz: int):
        """Frequency-adaptive LNA (settings.lna_auto): a tune into another
        band gets that band's LNA state, unless the signal/profile already
        sets one or the LNA is locked. Also resets the backoff baseline."""
        config = self.settings.lna_auto
        current_hz = self.confirmed.props.get("device_vfo_frequency")
        if band_index(target_hz, config) == band_index(current_hz, config):
            return profile
        if profile is not None and profile.lna_state is not None:
            self._lna_baseline = profile.lna_state
            return profile
        self._lna_baseline = None
        if self.settings.lna_locked:
            return profile
        state = auto_lna_state(
            target_hz, current_hz, config, self.confirmed.props.get("lna_state_max")
        )
        if state is None:
            return profile
        self._lna_baseline = state
        return (profile or ProfileSettings()).model_copy(update={"lna_state": state})

    def _on_overload(self, overloaded: bool) -> None:
        task = self._lna_overload_task
        if not overloaded:
            if task is not None and not task.done():
                task.cancel()
            self._lna_overload_task = None
        elif task is None or task.done():
            self._lna_overload_task = asyncio.create_task(self._lna_overload_watch())

    async def _lna_overload_watch(self) -> None:
        """While the receiver stays overloaded, add one LNA attenuation step
        every overload_hold_s, up to max_backoff_steps over the band's
        baseline. Quiet (no history/journal): it's a safety net, and the
        panel's LNA readout shows the change."""
        while True:
            config = self.settings.lna_auto
            await asyncio.sleep(config.overload_hold_s)
            props = self.confirmed.props
            if not props.get("overload") or self.settings.lna_locked:
                return
            if self._executor.running or self.scanner.is_tuning:
                continue  # a tune is in flight; re-check after it settles
            current = props.get("lna_state")
            if self._lna_baseline is None:
                self._lna_baseline = current
            nxt = backoff_state(current, self._lna_baseline, config, props.get("lna_state_max"))
            if nxt is None:
                return
            steps, desired = plan_with_desired(
                confirmed=props, profile=ProfileSettings(lna_state=nxt)
            )
            try:
                await self._restore(
                    steps, desired=desired, kind="lna", push_history=False, journal_action=False
                )
            except Exception as exc:  # noqa: BLE001 -- best effort; stop on refusal
                logger.info("LNA overload backoff stopped: %s", exc)
                return
            logger.info("LNA overload backoff: %s -> %s", current, nxt)

    async def apply_profile(self, profile_id: str) -> RestoreReport:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise HTTPException(404, f"profile not found: {profile_id!r}")
        steps, desired = plan_with_desired(
            confirmed=self.confirmed.props, profile=profile.settings()
        )
        return await self._restore(steps, desired=desired, kind="profile", profile_id=profile_id)

    async def back(self) -> RestoreReport | None:
        state = self.history.back()
        if state is None:
            return None
        steps, desired = plan_with_desired(confirmed=self.confirmed.props, full=state)
        try:
            report = await self._restore(steps, desired=desired, kind="back", push_history=False)
        except Exception:
            # _restore can raise before attempting anything (busy/disconnected);
            # the cursor already moved above, so put it back before propagating.
            self.history.undo_move("back")
            self.history_repo.save(self.history)
            await self._publish_history()
            raise
        if report.status in ("failed", "interrupted"):
            self.history.undo_move("back")
            await self._publish_history()
        self.history_repo.save(self.history)
        return report

    async def forward(self) -> RestoreReport | None:
        state = self.history.forward()
        if state is None:
            return None
        steps, desired = plan_with_desired(confirmed=self.confirmed.props, full=state)
        try:
            report = await self._restore(steps, desired=desired, kind="forward", push_history=False)
        except Exception:
            self.history.undo_move("forward")
            self.history_repo.save(self.history)
            await self._publish_history()
            raise
        if report.status in ("failed", "interrupted"):
            self.history.undo_move("forward")
            await self._publish_history()
        self.history_repo.save(self.history)
        return report

    async def apply_settings(self, patch: dict) -> Settings:
        prior_recorder_root = self.settings.recorder_root
        prior_region = self.settings.region
        prior_allocation_data_dir = self.settings.allocation_data_dir
        prior_nextpass_bin = self.settings.nextpass_bin
        prior_iqscan_bin = self.settings.iqscan_bin
        prior_iqscan_scan_root = self.settings.iqscan_scan_root
        prior_passes = {
            k: getattr(self.settings, k)
            for k in (
                "passes_days",
                "passes_min_elevation",
                "passes_horizon",
                "passes_top",
                "passes_satellites",
                "location",
            )
        }
        prior_tracking = {
            k: getattr(self.settings, k)
            for k in (
                "location",
                "location_file",
                "tle_cache_dir",
                "nextpass_cache_dir",
                "passes_extra_satellites",
            )
        }
        prior_features = self.settings.features
        data = self.settings.model_dump()
        patch = dict(patch)
        features_patch = patch.get("features")
        if features_patch:
            # Partial merge, not a wholesale replace: SettingsPatch.features
            # only carries the keys the caller actually set (see schemas.py),
            # so missing keys must keep their current value, not Features'
            # own field defaults.
            patch["features"] = {**data["features"], **features_patch}
        native_audio_patch = patch.get("native_audio")
        if native_audio_patch:
            patch["native_audio"] = {**data["native_audio"], **native_audio_patch}
        wifi_patch = patch.get("wifi")
        if wifi_patch:
            patch["wifi"] = {**data["wifi"], **wifi_patch}
        ble_patch = patch.get("ble")
        if ble_patch:
            patch["ble"] = {**data["ble"], **ble_patch}
        history_patch = patch.get("history")
        if history_patch:
            patch["history"] = {**data["history"], **history_patch}
        integrations_patch = patch.get("integrations")
        if integrations_patch:
            # Preserve omitted application settings when only changing one
            # integration field (for example toggling JS8Call on).
            patch["integrations"] = {
                **data["integrations"],
                **{
                    key: {**data["integrations"].get(key, {}), **value}
                    if isinstance(value, dict)
                    else value
                    for key, value in integrations_patch.items()
                },
            }
        lna_auto_patch = patch.get("lna_auto")
        if lna_auto_patch:
            patch["lna_auto"] = {**data["lna_auto"], **lna_auto_patch}
        survey_patch = patch.get("survey")
        if survey_patch:
            # SettingsPatch preserves which nested survey fields were sent.
            # Merge those fields into the current settings so undo payloads
            # and other partial updates don't replace omitted preferences.
            def merge_nested(current: dict, changes: dict) -> dict:
                merged = dict(current)
                for key, value in changes.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key] = merge_nested(merged[key], value)
                    else:
                        merged[key] = value
                return merged

            patch["survey"] = merge_nested(data["survey"], survey_patch)
        # `layout` (back-compat: a pre-workspaces client's PATCH) always
        # targets the active workspace; `workspaces` (U2) merges per id. Both
        # go through the same partial-merge reasoning as `features` above --
        # `Settings._finalize_workspaces` re-derives the mirrored top-level
        # `layout` from `workspaces[workspace]` once this is validated, so
        # `patch["layout"]` itself is dropped rather than applied directly.
        layout_patch = patch.pop("layout", None)
        workspaces_patch = patch.get("workspaces")
        if layout_patch or workspaces_patch:
            active_workspace = patch.get("workspace", data["workspace"])
            merged_workspaces = {k: dict(v) for k, v in data["workspaces"].items()}
            if layout_patch:
                merged_workspaces[active_workspace] = {
                    **merged_workspaces.get(active_workspace, {}),
                    **layout_patch,
                }
            if workspaces_patch:
                for workspace_id, workspace_patch in workspaces_patch.items():
                    merged_workspaces[workspace_id] = {
                        **merged_workspaces.get(workspace_id, {}),
                        **workspace_patch,
                    }
            patch["workspaces"] = merged_workspaces
        data.update(
            {k: v for k, v in patch.items() if v is not None or k in _NULLABLE_SETTINGS_FIELDS}
        )
        updated = Settings.model_validate(data)
        recorder_root_changed = updated.recorder_root != prior_recorder_root
        if recorder_root_changed:
            if self._wifi_session is not None:
                raise RecorderError("stop the Wi-Fi recording before changing its folder")
            status = self.recorder.status() if self.recorder is not None else {}
            if (
                status.get("state") not in (None, "off")
                or status.get("armed")
                or self.settings.recorder_armed
                or self._recorder_rearm_pending
            ):
                raise RecorderError("disarm and stop the recorder before changing its folder")
            if self._review_replay_source() is not None:
                raise RecorderError(
                    "stop survey/review playback before changing the recording folder"
                )
            if getattr(self, "_validation_busy", False):
                raise RecorderError(
                    "wait for the validation capture before changing the recording folder"
                )
            try:
                root = Path(updated.recorder_root).expanduser()
            except (RuntimeError, ValueError) as exc:
                raise RecorderError(f"invalid recording folder: {exc}") from exc
            if not root.is_absolute():
                raise RecorderError("recording folder must be an absolute path")
            try:
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=root):
                    pass
            except OSError as exc:
                raise RecorderError(f"recording folder is not writable: {exc}") from exc
            updated.recorder_root = str(root)
            replacement = self._build_recorder(updated)
            if str(updated.db_path) != ":memory:":
                updated.save(self.settings_path)
            if self._recorder_running:
                await self._stop_recorder_runtime()
            self.recorder = replacement
            self._recorder_rearm_pending = False
        wifi_helper_changed = (
            updated.wifi.helper_path,
            tuple(updated.wifi.helper_command or []),
        ) != (
            self.settings.wifi.helper_path,
            tuple(self.settings.wifi.helper_command or []),
        )
        ble_helper_changed = (
            updated.ble.usb_id,
            updated.ble.backend,
            updated.ble.serial_port,
            updated.ble.baud,
            updated.ble.phy,
            updated.ble.channel,
            tuple(updated.ble.helper_command or []),
        ) != (
            self.settings.ble.usb_id,
            self.settings.ble.backend,
            self.settings.ble.serial_port,
            self.settings.ble.baud,
            self.settings.ble.phy,
            self.settings.ble.channel,
            tuple(self.settings.ble.helper_command or []),
        )
        self.settings = updated
        if recorder_root_changed:
            self.wifi_sessions = WifiSessionStore(self._wifi_sessions_root())
        if wifi_helper_changed:
            self._wifi_settings_pending = True
            if not self.wifi.running and not (
                self._wifi_survey is not None and self._wifi_survey.state == "running"
            ):
                await self._apply_pending_wifi_settings()
        if ble_helper_changed:
            self._ble_settings_pending = True
            if not self.ble.running:
                await self._apply_pending_ble_settings()
        await self.integrations.configure(self.settings.integrations)
        if not recorder_root_changed and str(self.settings.db_path) != ":memory:":
            self.settings.save(self.settings_path)
        self.dwell.dwell_seconds = self.settings.dwell_seconds
        self.journal.nearby_hz = self.settings.nearby_hz
        if self.settings.features.journal_passive != prior_features.journal_passive:
            if self.settings.features.journal_passive:
                if not self._dwell_running:
                    await self.dwell.start()
                    self._dwell_running = True
            elif self._dwell_running:
                await self.dwell.stop()
                self._dwell_running = False
        if not self.settings.features.tracking and prior_features.tracking:
            # Live-off: stop a Doppler run already in flight rather than
            # waiting for it to notice on its own next tick.
            await self.doppler_loop.stop()
        if not self.settings.features.scanner and prior_features.scanner:
            # Same as tracking above: a disabled feature 404s every
            # /api/scan/* route (see _require_scanner_feature), so a scan
            # already running would otherwise keep sweeping with no way left
            # to stop it.
            if self.scanner.state != "idle":
                await self.scanner.stop()
        if recorder_root_changed and self.settings.features.recorder:
            await self._start_recorder_runtime()
        elif self.settings.features.recorder != prior_features.recorder:
            if self.settings.features.recorder:
                await self._start_recorder_runtime()
                await self._maybe_rearm_recorder()
            else:
                await self._stop_recorder_runtime()
        if self.settings.features.decoder != prior_features.decoder:
            if self.settings.features.decoder:
                await self._start_decoder_runtime()
            else:
                await self._stop_decoder_runtime()
        if self.settings.features.survey != prior_features.survey:
            if self.settings.features.survey:
                await self._start_survey_runtime()
            else:
                async with self._review_lifecycle_lock:
                    await self._detach_virtual_receiver()
                    await self._stop_survey_runtime()
        if self.settings.features.beacons != prior_features.beacons:
            if self.settings.features.beacons:
                await self._start_beacons_runtime()
            else:
                await self._stop_beacons_runtime()
        if self.settings.region != prior_region:
            self.identify_provider = BandPlanProvider(self.settings.region)
        if self.settings.allocation_data_dir != prior_allocation_data_dir:
            # Jurisdiction is read per lookup; only the data dir needs a rebuild.
            self.allocations = AllocationStore(self.settings.allocation_data_dir)
        if any(getattr(self.settings, k) != v for k, v in prior_tracking.items()):
            # No dedicated tracker cache like nextpass_provider's -- rebuild
            # so a changed location/cache dir takes effect immediately.
            self.tle_store = self._build_tle_store(self.settings)
            self.tracker = SatTracker(
                self.tle_store, resolve_location(self.settings), self._extra_catalog()
            )
        if self.settings.nextpass_bin != prior_nextpass_bin:
            self.nextpass_provider = NextPassProvider(self.settings.nextpass_bin)
        elif any(getattr(self.settings, k) != v for k, v in prior_passes.items()):
            # No bin change, but a passes_*/location field did -- rebuild so
            # the provider's cache (keyed on the query, not on settings)
            # never serves a stale prediction under the new parameters.
            self.nextpass_provider = NextPassProvider(self.settings.nextpass_bin)
        if self.settings.iqscan_bin != prior_iqscan_bin:
            self.iqscan_runner.bin = self.settings.iqscan_bin
        if self.settings.iqscan_scan_root != prior_iqscan_scan_root:
            self.iqscan_provider = IqScanProvider(self.settings.iqscan_scan_root)
            self.iqscan_runner.scan_root = self.settings.iqscan_scan_root
        return self.settings

    async def _restore(
        self,
        steps: list[Step],
        *,
        desired: dict[str, object],
        kind: str,
        signal_id: str | None = None,
        profile_id: str | None = None,
        push_history: bool = True,
        journal_action: bool = True,
        note: str | None = None,
        manual_mode: bool = False,
    ) -> RestoreReport:
        if self.workbench.owns_receiver and kind != "watch_span":
            raise RestoreRunning("stop the cycling signal watch before tuning")
        if self._band_scan_active and kind != "survey_band_scan":
            raise RestoreRunning("survey band scan owns the receiver")
        if self._executor.running or self.scanner.is_tuning:
            # The scanner's own executor is a separate instance (see
            # ScannerEngine's own docstring) so a manual tune between two of
            # its channel visits can go through and pause it -- but while its
            # tune step is actually in flight, both must not drive the same
            # ConfirmedState at once.
            raise RestoreRunning("restore running")
        if not self.confirmed.is_complete() or not self.adapter.connected:
            raise ConnectionError("not connected")
        if not self.confirmed.props.get("started", False):
            # Before pushing history or touching the executor (F7): SDRconnect
            # accepts the set_property call but never applies or echoes it
            # while stopped, so letting this through would just burn the full
            # per-step timeout and come back "failed".
            raise ReceiverStopped("receiver stopped")

        # Mutually exclusive with the Doppler loop: a restore and a tracking
        # tick both command the VFO, so the explicit user action wins and the
        # loop is stopped before any history is pushed.
        await self.doppler_loop.stop()
        # Re-check: a concurrent restore may have passed the guard above while
        # we awaited, and would otherwise push a duplicate pre-restore history
        # entry before executor.run() raises on the second caller.
        if self._executor.running or self.scanner.is_tuning:
            raise RestoreRunning("restore running")

        # ANT / MODE / LNA locks: memories, profiles and Back/Forward keep
        # the current antenna/mode/LNA; only the ANT select (kind "antenna"),
        # the MODE select (manual_mode) and the LNA field (kind "lna") change
        # them. Pinning `desired` to the
        # current value (not just dropping the step) stops a native profile
        # switch from moving it either.
        locked = []
        if self.settings.antenna_locked and kind != "antenna":
            locked.append("active_antenna")
        if self.settings.demodulator_locked and not manual_mode:
            locked.append("demodulator")
        if self.settings.lna_locked and kind != "lna":
            locked.append("lna_state")
        for prop in locked:
            steps = [s for s in steps if not (isinstance(s, SetStep) and s.property == prop)]
            current = self.confirmed.props.get(prop)
            desired = {k: v for k, v in desired.items() if k != prop}
            if current is not None:
                desired[prop] = current

        pre_props = dict(self.confirmed.props)
        if push_history:
            source = (
                kind
                if kind in {"tune", "back", "forward", "profile", "capture", "observed"}
                else "tune"
            )
            pre_snapshot = self.confirmed.snapshot(source)
            self.history.push(pre_snapshot)
            # Without this, HistoryRepo only persists the state *id*; after a
            # restart HistoryRepo.load() + StateRepo.get_many() can't find the
            # row and silently drop this entry (F2).
            self.states.add(pre_snapshot)
        self.dwell.note_app_action()

        async def on_progress(outcome: StepOutcome) -> None:
            await self.broadcaster.publish(
                {"type": "restore", "phase": "step", "outcome": outcome_to_dict(outcome)}
            )

        report = await self._executor.run(steps, on_progress, desired=desired)

        # An "interrupted"/"failed" restore can still have moved the receiver
        # -- SDRconnect couples the VFO to the centre frequency, so a tune can
        # abort with the radio somewhere new. Record where it actually ended
        # up, or history has no entry for it and Back leaves the operator
        # stranded with no way home.
        moved = self.confirmed.is_complete() and any(
            not matches(spec.name, pre_props.get(spec.name), self.confirmed.props.get(spec.name))
            for spec in RESTORABLE
        )

        if profile_id is not None and report.status in ("confirmed", "partial"):
            self._applied_profile_id = profile_id

        entry = None
        if report.status in ("confirmed", "partial") or moved:
            source = (
                kind
                if kind in {"tune", "back", "forward", "profile", "capture", "observed"}
                else "tune"
            )
            snap = self.confirmed.snapshot(source)
            if push_history:
                self.history.push(snap)
                self.states.add(snap)  # see the pre-restore push above (F2)
            if journal_action:
                observation = self.confirmed.observation()
                entry = self.journal.record_action(
                    kind,
                    state=snap,
                    signal_id=signal_id,
                    profile_id=profile_id,
                    observation=observation,
                    note=note,
                )
                if signal_id is not None:
                    signal = self.signals.get(signal_id)
                    if signal is not None:
                        self.journal.touch_heard(signal)
                self.history_repo.save(self.history)

        await self.broadcaster.publish(
            {"type": "restore", "phase": "done", "report": restore_report_to_dict(report)}
        )
        if entry is not None:
            await self.broadcaster.publish(
                {"type": "journal", "entry": entry.model_dump(mode="json")}
            )
        await self._publish_history()
        await self.broadcaster.publish({"type": "state", **self.state_payload()})
        return report
