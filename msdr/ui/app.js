import { LitElement, html } from "./vendor/lit.js";
import * as api from "./api.js";
import { profileFromProps } from "./profile-summary.js";
import { savedFrequencyMatches } from "./saved-frequency.js";
import { StepTuner } from "./step-tuner.js";
import { installUiHelp } from "./ui-help.js";
import {
  todayUtc,
  resolveFeatures,
  resolveWorkspaceLayout,
  sectionVisible,
  SECTION_LABELS,
  LAYOUT_PRESETS,
  WORKSPACE_IDS,
  WORKSPACE_LABELS,
  WORKSPACE_HELP,
  DISCOVER_VIEW_HELP,
  WORKSPACE_SECTIONS,
  DEFAULT_WORKSPACE_COLUMNS,
  DEFAULT_WORKSPACE_WIDE,
  mhz,
  defaultStepHz,
  STEP_CYCLE_HZ,
} from "./format.js";
import "./components/instrument-panel.js";
import "./components/diag-banner.js";
import "./components/receiver-lost-bar.js";
import { detectLoss, isRecovered, surveyNote, shouldNotify } from "./receiver-loss.js";
import "./components/getting-started.js";
import "./components/restore-trail.js";
import "./components/nearby-list.js";
import "./components/inbox-list.js";
import "./components/recent-list.js";
import "./components/journal-view.js";
import "./components/listen-view.js";
import "./components/listen-workbench.js";
import "./components/passes-view.js";
import "./components/sky-view.js";
import "./components/discover-map.js";
import "./components/wifi-view.js";
import "./components/ble-view.js";
import "./components/remoteid-view.js";
import "./components/timeline-view.js";
import "./components/rules-view.js";
import "./components/scans-view.js";
import "./components/catalog-view.js";
import "./components/library-view.js";
import "./components/palette.js";
import "./components/drawer.js";
import "./components/setup-drawer.js";
import "./components/decode-view.js";
import "./components/status-line.js";
import "./components/survey-view.js";
import "./components/band-scan-report.js";
import "./components/beacons-view.js";
import "./components/finds-view.js";
import "./components/recordings-view.js";
import "./components/signal-panel.js";
import "./components/allocation-chart.js";
// S1: the scanner/decode decks are grid sections now ("scan"/"decode"), no
// longer children of instrument-panel, so app.js has to register them itself.
import "./components/scanner-deck.js";
import "./components/decode-deck.js";
import "./components/review-view.js";
import "./components/signal-workbench.js";
import "./components/signal-instruments.js";

/** Pause before the current-frequency details section follows a retune. */
const FREQ_DETAILS_SETTLE_MS = 400;

const PASSES_REFRESH_MS = 10 * 60 * 1000;

const RESTORE_CLEAR_MS = 4000;
const LAYOUT_SAVE_MS = 300;
const STEP_TUNE_INTERVAL_MS = 150;
const RESYNC_DEBOUNCE_MS = 300;

function isTyping() {
  const tag = document.activeElement?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/** localStorage "1"/"0" flag; storage can throw (private mode, blocked
 * site data), which must read as "off" rather than break the app. */
function readLocalFlag(key) {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function readLocalValue(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocal(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Not persisted -- the in-memory state still applies for this page load.
  }
}

// Workspaces that show <survey-view> in the fixed primary slot under the tab
// strip instead of in their grid (S1 contract, "Primary spectrum slot").
const PRIMARY_SPECTRUM_WORKSPACES = new Set(["listen", "discover"]);
const LISTEN_WORKBENCH_SECTIONS = new Set(["nearby", "recent", "listen", "log", "decode"]);

export class MsdrApp extends LitElement {
  static properties = {
    state: { state: true },
    settings: { state: true },
    _surveyPeakBusy: { state: true },
    workspace: { state: true },
    discoverView: { state: true },
    _wifiInspectSource: { state: true },
    layout: { state: true },
    _dragId: { state: true },
    _dropTarget: { state: true },
    inbox: { state: true },
    recent: { state: true },
    favorites: { state: true },
    profiles: { state: true },
    tags: { state: true },
    journalDay: { state: true },
    journal: { state: true },
    journalDays: { state: true },
    passes: { state: true },
    selectedPass: { state: true },
    _satelliteSettingsBusy: { state: true },
    scans: { state: true },
    scan: { state: true },
    recorder: { state: true },
    decoders: { state: true },
    decodeEvents: { state: true },
    decodeViewOpen: { state: true },
    decodeViewPresetFrequencyHz: { state: true },
    spectrumFrame: { state: true },
    spectrumRing: { state: true },
    _lastFind: { state: true },
    signals: { state: true },
    restore: { state: true },
    selected: { state: true },
    paletteOpen: { state: true },
    setupOpen: { state: true },
    validation: { state: true }, // GET /api/validation, fetched when SETUP opens -- see updated()
    _layoutEditMode: { state: true },
    _reviewRefreshAt: { state: true },
    _libraryRefreshAt: { state: true },
    diagOpen: { state: true },
    error: { state: true },
    failure: { state: true },
    lastAction: { state: true },
    settingsSavedAt: { state: true },
    wsDown: { state: true },
    _lostAlerts: { state: true },
    _notifyLost: { state: true },
    _layoutAnnouncement: { state: true },
    _stepOverrideHz: { state: true },
    recordingsOpen: { state: true },
    _interruptConfirm: { state: true },
    _signalTarget: { state: true },
    _freqDetailsOpen: { state: true },
    _freqDetailsTarget: { state: true },
    _freqDetailsTuning: { state: true },
    _allocationInspectHz: { state: true },
    _reportScanId: { state: true },
    _bandScanDraft: { state: true },
    _bandScanStatus: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.state = null;
    this.discoverView = "spectrum";
    this._wifiInspectSource = null;
    this._reportScanId = null;
    this._bandScanDraft = { start: "136.000", end: "174.000", span: null, dwell: "10", preset: "10", overlap: "25" };
    this._bandScanStatus = { state: "idle" };
    this.settings = null;
    this.workspace = "listen";
    this.layout = resolveWorkspaceLayout(null, "listen");
    this._dragId = null;
    this._dropTarget = null;
    this._layoutSaveTimer = null;
    this.inbox = [];
    this.recent = [];
    this.favorites = [];
    this.profiles = [];
    this.tags = [];
    this.journalDay = todayUtc();
    this.journal = [];
    this.journalDays = [];
    this.passes = { passes: [], generated_at: null, source: null, timezone: null, error: null };
    this.selectedPass = null;
    this.scans = { scans: [], root: null };
    // Scanner status (v0.12.0): kept live via the "scan"/"scan_hit" websocket
    // messages (see _onWsMessage) after the initial GET /api/scan below --
    // this default mirrors an idle scan with no config/hits yet.
    this.scan = { state: "idle", reason: null, config: null, channel: null, position: 0, total: 0, snr_db: null, hits: [], started_at: null };
    // Pre-roll recorder status (kept live via the "recorder" websocket
    // message after the initial GET /api/recorder below) -- this default
    // mirrors an unarmed recorder with no status pushed yet.
    this.recorder = {
      state: "off",
      armed: false,
      buffered_s: 0,
      recording_s: 0,
      preroll_s: 300,
      sample_rate: null,
      center_hz: null,
      free_gb: null,
      root: null,
      last_recording: null,
      error: null,
    };
    // Decoders P1: registry (GET /api/decoders, static per session) and the
    // live-capped (500) message history (kept fresh via the "decoder_event"
    // websocket message -- see _onWsMessage). state.decoder itself (status)
    // rides along in `state`/the "decoder" ws message, same as recorder/scan.
    this.decoders = [];
    this.decodeEvents = [];
    this.decodeViewOpen = false;
    this.decodeViewPresetFrequencyHz = null;
    // Survey (S1): state.survey (status) rides along in `state`/the "survey"
    // ws message, same as decoder above. The spectrum frames themselves
    // don't -- spectrumFrame is the latest decoded {bins: Uint8Array, ...}
    // and spectrumRing a newest-first array capped at 2000 for survey-view's
    // waterfall history (see _onWsMessage's "spectrum" case).
    this.spectrumFrame = null;
    this.spectrumRing = [];
    // Scanner finds log (2026-09-25 plan): finds-view fetches its own list
    // (GET /api/finds, same self-fetch pattern as beacons-view's hits) --
    // this is just the forwarded "find" ws push for its live merge.
    this._lastFind = null;
    this.signals = [];
    this.restore = { steps: [], report: null };
    this.selected = null;
    this.paletteOpen = false;
    this.setupOpen = false;
    this.validation = null;
    // S1: LIBRARY is a workspace now (was a full-view swap, `libraryOpen`).
    this._libraryRefreshAt = null;
    this._reviewRefreshAt = null;
    this._workspaceChosen = false;
    // Drag grips / ⇔ WIDE toggles are layout-editing chrome, hidden unless
    // the operator turns on SETUP → LAYOUT → EDIT LAYOUT (S1); per-browser,
    // like the other msdr.* UI preferences.
    this._layoutEditMode = readLocalFlag("msdr.layoutEdit");
    this.diagOpen = false;
    this.error = null;
    this.failure = null;
    this.lastAction = null;
    this.settingsSavedAt = null;
    // The socket to msdr itself is gone: readings on screen are frozen, so
    // the panel must say so rather than keep showing a green LINK lamp.
    this.wsDown = false;
    // "Receiver lost" bar entries (receiver-loss.js), one per lost receiver;
    // _rxPrev keeps the last wifi/ble status so only a transition alerts.
    this._lostAlerts = [];
    this._rxPrev = { wifi: null, ble: null };
    this._notifyLost = readLocalValue("msdr.notifyLost") === "1";
    this.lastTuned = null;
    // Cmd/Ctrl-Z interleaves confirmed receiver tunes and saved UI-setting
    // changes with the database's timestamped library-undo log.
    this.addEventListener("decoder-preset-applied", e => this._rememberUndo({kind: "decoder-preset", token: e.detail.token}));
    this.addEventListener("decoder-preset-restored", e => {
      this._undoActions = this._undoActions.filter(a => a.token !== e.detail.token);
      this._redoActions.push({kind: "decoder-preset", token: e.detail.token, at: Date.now()});
    });
    this._undoActions = [];
    this._redoActions = [];
    this._undoOrder = 0;
    this._undoing = false;
    this._undoPending = false;
    this.addEventListener("library-undoable", (ev) => {
      if (ev.detail?.undo) {
        this._invalidateRedoForNewLibraryEdit(ev.detail.undo);
        this._rememberLibraryUndo(ev.detail.undo, ev.detail.at);
      }
      else this._syncLibraryUndo(ev.detail?.at);
    });
    this.addEventListener("library-undo-consumed", (ev) => {
      this._forgetLibraryUndo(ev.detail?.id);
      if (ev.detail?.id) this._redoActions.push({kind: "library", undoId: ev.detail.id, at: Date.now()});
    });
    this._closeWs = null;
    this._restoreTimer = null;
    this._resyncTimer = null;
    this._passesTimer = null;
    this._layoutAnnouncement = "";
    this._focusedGripId = null;
    this._layoutUnsaved = false;
    this._layoutEdit = 0;
    // Step-tuning (ArrowLeft/Right, instrument-panel's ◂ ▸ and step-size
    // button): _stepOverrideHz is an explicit step size that overrides the
    // mode default until the mode changes (see updated()); StepTuner keeps
    // held-key targets accumulated while serializing bounded tune requests.
    this._stepOverrideHz = null;
    this._stepTuner = new StepTuner(STEP_TUNE_INTERVAL_MS);
    this._stepTunePauseCount = 0;
    this._stepConfirmedTargetSeen = false;
    this._lastStepMode = undefined;
    // U1: RECORDINGS modal (recordings-view.js) and confirmInterrupt's
    // pending inline confirm strip ({message, resolve} or null while nothing
    // is pending -- see confirmInterrupt/instrument-panel's interruptMessage).
    this.recordingsOpen = false;
    this._interruptConfirm = null;
    this._wsStateSeq = 0; // bumped per ws "state" push; see _fetchState
    this._journalSeq = 0; // latest _loadJournalDay request; older replies are dropped
    // U3: signal-detail panel (signal-panel.js) -- {frequency_hz, signal_id?,
    // find_id?} while open, null while closed. Opened from any "select a
    // signal" surface via openSignal()/the bubbling "signal-open" event (see
    // _onSignalOpen below), rather than threading a listener through every
    // nested component's template.
    this._signalTarget = null;
    // Current-frequency details: a full-width section under the receiver
    // panel (instrument-panel's ⓘ) that follows every retune, debounced so
    // held-key/wheel tuning doesn't fetch /api/signal-detail per step.
    this._freqDetailsOpen = false;
    this._freqDetailsTarget = null;
    this._freqDetailsTimer = null;
    this._freqDetailsPendingHz = null;
    // Allocations (v0.53.0): the frequency the <allocation-chart> workspace
    // jumps to and inspects when opened from <allocation-summary>'s "Open
    // chart" action (the bubbling "open-allocation-chart" event below) --
    // null otherwise, so the chart just opens at its last position/zoom.
    this._allocationInspectHz = null;
    this._onKeydown = this._onKeydown.bind(this);
    this._onFocus = this._onFocus.bind(this);
    this._onPageHide = this._onPageHide.bind(this);
    this._onSignalOpen = this._onSignalOpen.bind(this);
    this._onOpenAllocationChart = this._onOpenAllocationChart.bind(this);
  }

  /** Keeps keyboard focus on the grip of the section the operator is moving
   * (see _onGripKeydown), across *every* re-render rather than just the one
   * right after a move: columns are diffed by position (no keyed repeat in
   * the vendored lit), so any later render that shifts sections -- the move
   * itself, a settings/state reload -- can hand the focused node to another
   * section or drop it. Only steps in while focus is still on a grip (or
   * lost to <body>), so it never steals focus the operator moved elsewhere. */
  updated(changedProps) {
    if (changedProps.has("state") && this._freqDetailsOpen) this._followFrequencyDetails();
    if (changedProps.has("state")) {
      const mode = this.state?.props?.demodulator ?? null;
      // Clear an explicit step-size override once the mode actually changes
      // (not on the first state load, or unrelated state pushes that leave
      // the mode alone).
      if (this._lastStepMode !== undefined && mode !== this._lastStepMode) {
        this._stepOverrideHz = null;
      }
      if (mode !== this._lastStepMode) this._cancelStepTuning();
      this._lastStepMode = mode;
      if (!this._canStepTune()) this._cancelStepTuning();
      if (this._stepTuner?.isSettled && this._stepTuner.confirmedAt != null) {
        const vfoHz = this.state?.props?.device_vfo_frequency;
        if (vfoHz === this._stepTuner.targetHz) this._stepConfirmedTargetSeen = true;
        else if (this._stepConfirmedTargetSeen) this._cancelStepTuning();
      }
    }
    // S5: SETUP's VALIDATION group -- fetched fresh each time the drawer
    // opens (cheap, reads a markdown summary rather than running decoders)
    // so a scorecard/gate change elsewhere in the same session shows up.
    if (changedProps.has("setupOpen") && this.setupOpen) this._loadValidation();
    const id = this._focusedGripId;
    if (!id) return;
    const want = this.querySelector(`[data-section-id="${id}"] .grip`);
    const active = document.activeElement;
    if (!want || active === want) return;
    if (active === document.body || active?.classList?.contains("grip")) want.focus();
  }

  /** A grip losing focus to something that isn't a grip (Tab, a click
   * elsewhere) ends the keyboard move session. Checked after the event
   * settles: a re-render that removes the focused node blurs it with no
   * relatedTarget, and that must not end the session -- updated() re-focuses. */
  _onGripBlur() {
    setTimeout(() => {
      const active = document.activeElement;
      if (active && active !== document.body && !active.classList?.contains("grip")) {
        this._focusedGripId = null;
      }
    });
  }

  connectedCallback() {
    super.connectedCallback();
    this._removeUiHelp = installUiHelp(this);
    window.addEventListener("keydown", this._onKeydown);
    window.addEventListener("focus", this._onFocus);
    window.addEventListener("pagehide", this._onPageHide);
    window.addEventListener("signal-open", this._onSignalOpen);
    window.addEventListener("open-allocation-chart", this._onOpenAllocationChart);
    this._closeWs = api.connectWs(
      (msg) => this._onWsMessage(msg),
      () => {
        this.wsDown = true;
        this._noteReceiver("sdrconnect", this.state, { connected: false, wsDown: true });
        this._cancelStepTuning();
      },
      (isReconnect) => {
        // A reconnect can cover a gap no "resync" marker ever announced --
        // the socket was simply down while the server published events, so
        // there was no queue to overflow and flag in the first place.
        if (isReconnect) this._resyncAfterEventLoss();
      }
    );
    this._loadAll();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._removeUiHelp?.();
    this._removeUiHelp = null;
    this._cancelStepTuning();
    window.removeEventListener("keydown", this._onKeydown);
    window.removeEventListener("focus", this._onFocus);
    window.removeEventListener("pagehide", this._onPageHide);
    window.removeEventListener("signal-open", this._onSignalOpen);
    window.removeEventListener("open-allocation-chart", this._onOpenAllocationChart);
    clearInterval(this._passesTimer);
    clearTimeout(this._resyncTimer);
    if (this._closeWs) this._closeWs();
  }

  /** A pending debounced layout PATCH (see _setLayout) would otherwise be
   * lost if the tab closes/navigates within the 300ms debounce window --
   * keepalive lets the browser finish a fetch started right as it unloads. */
  _onPageHide() {
    this._cancelStepTuning();
    if (this._layoutSaveTimer == null) return;
    clearTimeout(this._layoutSaveTimer);
    this._layoutSaveTimer = null;
    fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspaces: { [this.workspace]: { columns: this.layout.columns } } }),
      keepalive: true,
    });
  }

  _onFocus() {
    this._loadPasses();
  }

  // --- data loading -------------------------------------------------------

  async _loadAll() {
    await Promise.all([
      this._run(() => this._fetchState()),
      this._loadInbox(),
      this._loadRecent(),
      this._loadFavorites(),
      this._loadProfiles(),
      this._loadTags(),
      this._loadJournalDays(),
      this._loadJournalDay(this.journalDay),
      this._loadSignals(),
      this._loadPasses(),
      this._loadScans(),
      this._loadScanStatus(),
      this._loadBandScanStatus(),
      this._loadRecorderStatus(),
      this._loadDecoders(),
      this._loadDecodeEvents(),
      this._loadSettings(),
    ]);
    this._syncFeatureTimers();
  }

  /** GET /api/settings, just for catalog_languages (CATALOG's "MY LANGUAGES"
   * filter) -- degrades to no languages rather than breaking. */
  async _loadSettings() {
    try {
      this.settings = await api.getSettings();
    } catch {
      this.settings = null;
    }
    document.documentElement.dataset.evidencePanelStyle =
      this.settings?.evidence_panel_style === "outline" ? "outline" : "tinted";
    // The server is the source of truth for layout -- nothing persisted in
    // localStorage, re-read fresh here on load and after settings-saved --
    // except while a local edit hasn't been saved yet: a GET that lands
    // after a quick move (e.g. right after page load) would otherwise revert
    // it, and the pending save would then PATCH the reverted layout. The
    // active workspace (U2) is re-read the same way -- see _switchWorkspace,
    // which PATCHes it immediately (not debounced) so there's no analogous
    // in-flight window to guard here.
    // S1: a workspace the operator already picked in this session (key,
    // tab, LIBRARY button) wins over what the server remembers -- the
    // initial GET can land after an immediate switch and would otherwise
    // flip the view back (seen in tests/e2e: press "l" right after load).
    if (!this._layoutUnsaved && !this._workspaceChosen) {
      this.workspace = this.settings?.workspace ?? "listen";
      this.layout = resolveWorkspaceLayout(this.settings?.workspaces, this.workspace);
    }
  }

  /** Feature defaults are resolved in format.js, including scanner off. */
  get features() {
    return resolveFeatures(this.state?.features);
  }

  /** The PASSES poll timer lives here (its data feeds passes-view, sky-view's
   * default-satellite pick, and drawer.js's next-pass line), so hiding
   * passes-view alone wouldn't stop it -- gate it explicitly whenever the
   * flag might have changed. SKY's own poll timer lives inside sky-view and
   * stops itself via disconnectedCallback when that section is hidden. */
  _syncFeatureTimers() {
    const enabled = this.features.passes;
    if (enabled && !this._passesTimer) {
      this._passesTimer = setInterval(() => this._loadPasses(), PASSES_REFRESH_MS);
      this._loadPasses();
    } else if (!enabled && this._passesTimer) {
      clearInterval(this._passesTimer);
      this._passesTimer = null;
    }
  }

  /** `source`/`subject`, when given, also route a thrown error into
   * this.failure (see _reportFailure) for the matching panel's inline note.
   * `fn` itself is responsible for clearing a stale failure on its own
   * success path (via _clearFailure or _reportStatus) -- this catch is only
   * for the network/4xx case, not a 2xx response that didn't fully take. */
  async _run(fn, source, subject) {
    try {
      await fn();
    } catch (err) {
      this._status(err.message);
      if (source) this._reportFailure(source, subject ?? source, "error", err.message);
    }
  }

  _loadInbox() {
    return this._run(async () => {
      this.inbox = await api.inbox();
      this._clearFailure("inbox");
    }, "inbox", "inbox");
  }

  _loadRecent() {
    return this._run(async () => {
      this.recent = await api.recent();
      this._clearFailure("recent");
    }, "recent", "recent");
  }

  _loadFavorites() {
    return this._run(async () => (this.favorites = await api.listListItems("favorites")));
  }

  _newProfile(fromCurrent) {
    // fromCurrent: prefill from the live receiver's confirmed props.
    const prefill = fromCurrent ? profileFromProps(this.state?.props) : null;
    this.selected = { kind: "profile", item: null, prefill };
  }

  _openProfile(id) {
    const item = this.profiles.find((p) => p.id === id);
    if (item) this.selected = { kind: "profile", item };
  }

  _loadProfiles() {
    return this._run(async () => (this.profiles = await api.listProfiles()));
  }

  _loadTags() {
    return this._run(async () => (this.tags = await api.listTags()));
  }

  /** GET /api/state, unless a websocket "state" push landed while it was in
   * flight: the push is newer than this HTTP snapshot, so don't overwrite it. */
  async _fetchState() {
    const wsSeq = this._wsStateSeq;
    const state = await api.getState();
    if (wsSeq === this._wsStateSeq) this.state = state;
  }

  _loadJournalDays() {
    return this._run(async () => (this.journalDays = (await api.journalDays()).days));
  }

  _loadJournalDay(day) {
    // A slow reply for a previously selected day must not replace the newer one.
    const seq = ++this._journalSeq;
    return this._run(async () => {
      const journal = await api.journalDay(day);
      if (seq === this._journalSeq) this.journal = journal;
    });
  }

  _loadSignals() {
    return this._run(async () => (this.signals = await api.listSignals()));
  }

  /** PassesResult carries its own `error` field for an expected condition
   * (nextpass not installed, etc.); a thrown fetch error means the route
   * itself isn't there yet -- both degrade to the same dim section line,
   * so this never routes through the shared _run/_status error path. */
  async _loadPasses() {
    try {
      this.passes = await api.getPasses();
    } catch {
      this.passes = { passes: [], generated_at: null, source: null, timezone: null, error: "nextpass not available" };
    }
  }

  async _loadScans() {
    try {
      this.scans = await api.listScans();
    } catch {
      this.scans = { scans: [], root: null, error: "no scans" };
    }
  }

  /** Route not landed yet, or the scanner feature is off -- keep the idle
   * default from the constructor rather than breaking the initial load. */
  async _loadScanStatus() {
    try {
      this.scan = await api.scannerStatus();
    } catch {
      // keep this.scan as-is
    }
  }

  /** A saved scan may still be active after a page reload. */
  async _loadBandScanStatus() {
    try { this._bandScanStatus = await api.getSurveyBandScan(); }
    catch { /* Preserve the last known scan state during transient failures. */ }
  }

  /** A disabled recorder keeps the constructor's off default. */
  async _loadRecorderStatus() {
    try {
      this.recorder = await api.getRecorder();
    } catch {
      // keep this.recorder as-is
    }
  }

  /** GET /api/decoders: the static registry (id/label/kind/demodulators/
   * availability) -- degrades to [] when the route or the decoder package
   * isn't there yet, same as every other optional-feature load here. */
  async _loadDecoders() {
    try {
      this.decoders = await api.getDecoders();
    } catch {
      this.decoders = [];
    }
  }

  /** Initial decode message history (server-capped at decoder.history_limit,
   * newest first); kept live afterwards by the "decoder_event" websocket
   * message (see _onWsMessage), capped at 500 in memory same as scan hits. */
  async _loadDecodeEvents() {
    try {
      this.decodeEvents = await api.listDecodeEvents({ limit: 500 });
    } catch {
      this.decodeEvents = [];
    }
  }

  // --- websocket -----------------------------------------------------------

  /** Raise or clear the "receiver lost" bar entry for `kind` (see
   * receiver-loss.js for the rules). */
  _noteReceiver(kind, prev, next) {
    const have = this._lostAlerts.some((a) => a.kind === kind);
    if (have && isRecovered(kind, next)) {
      this._lostAlerts = this._lostAlerts.filter((a) => a.kind !== kind);
      return;
    }
    const alert = have ? null : detectLoss(kind, prev, next);
    if (!alert) return;
    this._lostAlerts = [...this._lostAlerts, alert];
    if (kind === "wifi") this._addSurveyNote(alert);
    // The bar covers a visible tab; the notification is for when msdr is behind other windows.
    if (typeof Notification !== "undefined"
      && shouldNotify({ pref: this._notifyLost, permission: Notification.permission, hidden: document.hidden })) {
      try {
        new Notification("msdr: " + alert.title, { body: alert.detail });
      } catch {
        // Some browsers restrict the constructor; the bar still shows.
      }
    }
  }

  /** The Wi-Fi status push doesn't say whether a channel survey was running,
   * so ask once at the moment of loss. */
  async _addSurveyNote(alert) {
    try {
      const note = surveyNote(await api.getWifiSurvey());
      if (note) this._updateLost(alert.kind, { detail: `${alert.detail} ${note}` });
    } catch {
      // No survey info: the bar is still accurate without it.
    }
  }

  _updateLost(kind, patch) {
    this._lostAlerts = this._lostAlerts.map((a) => (a.kind === kind ? { ...a, ...patch } : a));
  }

  async _retryLost(kind) {
    if (kind === "sdrconnect") {
      this.diagOpen = true;
      return;
    }
    const alert = this._lostAlerts.find((a) => a.kind === kind);
    this._updateLost(kind, { busy: true, error: null });
    try {
      if (kind === "wifi") await api.startWifi(alert?.channel ?? undefined);
      else await api.startBle();
    } catch (err) {
      this._updateLost(kind, { error: err.message });
    } finally {
      this._updateLost(kind, { busy: false });
    }
  }

  async _setNotifyLost(on) {
    if (on && typeof Notification !== "undefined" && Notification.permission !== "granted") {
      try {
        on = (await Notification.requestPermission()) === "granted";
      } catch {
        on = false;
      }
      if (!on) this._status("Notifications are blocked for this site in the browser settings.");
    }
    this._notifyLost = on;
    writeLocal("msdr.notifyLost", on ? "1" : "0");
  }

  _onWsMessage(msg) {
    switch (msg.type) {
      case "state": {
        const { type: _type, ...rest } = msg;
        this._wsStateSeq += 1;
        this._noteReceiver("sdrconnect", this.state, { connected: !!rest.connected, wsDown: false });
        this.state = rest;
        this._syncFeatureTimers();
        this.wsDown = false; // first state after a reconnect: readings are live again
        break;
      }
      case "wifi":
      case "ble":
        this._noteReceiver(msg.type, this._rxPrev[msg.type], msg.status);
        this._rxPrev[msg.type] = msg.status;
        break;
      case "connection":
        this._noteReceiver("sdrconnect", this.state, { connected: !!msg.connected, wsDown: false });
        if (this.state) this.state = { ...this.state, connected: msg.connected };
        break;
      case "resync":
        // Broadcaster.next_outgoing: a non-coalescable event (decoder_event,
        // journal, scan_hit, ...) was dropped from a full queue -- reload
        // the histories it would otherwise have patched in incrementally.
        this._resyncAfterEventLoss();
        break;
      case "telemetry":
        // S-meter readings only; merged in rather than replacing `state` so
        // the rest of the payload isn't churned twice a second.
        if (this.state)
          this.state = { ...this.state, observation: { ...this.state.observation, ...msg.observation } };
        break;
      case "restore":
        if (msg.phase === "step") this._onRestoreStep(msg.outcome);
        else if (msg.phase === "done") this._onRestoreDone(msg.report);
        break;
      case "journal":
        if (this.journalDay === todayUtc()) this._loadJournalDay(this.journalDay);
        this._loadInbox();
        this._loadRecent();
        this._loadJournalDays();
        break;
      case "inbox":
        this._loadInbox();
        this._loadFavorites();
        // Also published after a library bulk op/undo lands elsewhere (e.g.
        // another tab) -- refresh the open Library view and its undo label.
        this._libraryRefreshAt = Date.now();
        break;
      case "tracking": {
        const { type: _type, ...tracking } = msg;
        this.state = { ...(this.state ?? {}), tracking };
        break;
      }
      case "history": {
        // Published on its own when back()/forward() undo their cursor move
        // (a failed/interrupted restore) without a full "state" broadcast --
        // merge it in directly so Back/Forward affordances don't go stale.
        const { type: _type, ...history } = msg;
        this.state = { ...(this.state ?? {}), history };
        break;
      }
      case "owner": {
        // U1: receiver owner (manual/scanner/tracking/beacons), pushed on
        // every change of the sources it's computed from -- merged into
        // state.owner, mirroring "history" above (also rides along in GET
        // /api/state's payload, see instrument-panel.js's owner chip).
        const { type: _type, ...owner } = msg;
        this.state = { ...(this.state ?? {}), owner };
        break;
      }
      case "scan": {
        // Full scanner status (see scannerStatus/scan/start/pause/...) --
        // replaces this.scan wholesale, same as "state" does for the receiver.
        const { type: _type, ...status } = msg;
        this.scan = status;
        break;
      }
      case "survey_band_scan":
        this._bandScanStatus = msg.status ?? { state: "idle" };
        break;
      case "scan_hit":
        // Lightweight companion to "scan" (a full status broadcast may or may
        // not follow right away) -- prepend the new hit, capped so the array
        // can't grow unbounded over a long-running sweep (the hit log itself
        // only ever shows the newest 50, see scanner-view.js's HITS_SHOWN).
        this.scan = { ...this.scan, hits: [msg.hit, ...(this.scan?.hits ?? [])].slice(0, 200) };
        break;
      case "recorder": {
        // Full recorder status, pushed on every state change and ~1 Hz while
        // armed/recording (see AppService._publish_recorder_status) --
        // replaces this.recorder wholesale, same as "scan" does for the scanner.
        const { type: _type, ...status } = msg;
        this.recorder = status;
        break;
      }
      case "decoder": {
        // Full DecoderManager.status() (msdr/decoder/manager.py), pushed on
        // every change -- merged into state.decoder, mirroring how it also
        // rides along in GET /api/state's payload (see AppService.state_payload).
        const { type: _type, ...status } = msg;
        this.state = { ...(this.state ?? {}), decoder: status };
        break;
      }
      case "decoder_event":
        // One decoded message -- prepend, capped at 500 in memory (server
        // history is separately capped at decoder.history_limit).
        this.decodeEvents = [msg.event, ...this.decodeEvents].slice(0, 500);
        break;
      case "survey": {
        // Full Survey.status() (msdr/survey/survey.py), pushed on change
        // (at most 1/s) -- merged into state.survey, mirroring "decoder".
        const { type: _type, ...status } = msg;
        this.state = { ...(this.state ?? {}), survey: status };
        break;
      }
      case "beacons": {
        // Full BeaconMonitor.status() (msdr/survey/beacons.py), pushed on
        // change (at most 1/s) -- merged into state.beacons, mirroring "survey".
        const { type: _type, ...status } = msg;
        this.state = { ...(this.state ?? {}), beacons: status };
        break;
      }
      case "find":
        // Scanner finds log (2026-09-25 plan): pushed on every logged find --
        // forwarded to finds-view as a fresh object each time (so a repeat
        // find, same id, still re-triggers its updated()) for a live
        // insert/merge instead of waiting on its own poll.
        this._lastFind = { find: msg.find, created: !!msg.created, at: Date.now() };
        break;
      case "spectrum": {
        // One 512-bin spectrum frame (up to settings.survey.spectrum_fps/s):
        // base64 -> Uint8Array, kept as the latest frame plus a newest-first
        // ring (survey-view's waterfall history) capped at 2000 rows.
        const bins = Uint8Array.from(atob(msg.bins), (c) => c.charCodeAt(0));
        const frame = {
          bins,
          center_hz: msg.center_hz,
          sample_rate_hz: msg.sample_rate_hz,
          at: msg.at,
          ref_level_db: msg.ref_level_db,
          base_db: msg.base_db,
        };
        this.spectrumFrame = frame;
        const previous = this.spectrumRing[0];
        const sameSpan = previous?.center_hz === frame.center_hz &&
          previous?.sample_rate_hz === frame.sample_rate_hz && frame.at > previous.at;
        this.spectrumRing = [frame, ...(sameSpan ? this.spectrumRing : [])].slice(0, 2000);
        break;
      }
      default:
        break;
    }
  }

  /** Reloads the histories the ws's incremental event types (decoder_event,
   * journal, scan_hit) normally patch in locally, rather than leaving the
   * UI quietly behind one of those events. Triggered by a "resync" marker
   * (above) or by any websocket reconnect after the first -- a plain
   * reconnect can cover the same kind of gap without any marker at all,
   * since there's no queue to overflow while the socket is simply down.
   * Debounced so a burst of resync markers (the same overflow condition can
   * repeat for several publishes in a row) triggers one reload, not several
   * concurrent ones. */
  _resyncAfterEventLoss() {
    clearTimeout(this._resyncTimer);
    this._resyncTimer = setTimeout(() => {
      this._loadDecodeEvents();
      this._loadJournalDays();
      this._loadJournalDay(this.journalDay);
      this._loadInbox();
      this._loadRecent();
      this._loadScanStatus();
      this._action("some live updates were missed; reloaded");
    }, RESYNC_DEBOUNCE_MS);
  }

  _onRestoreStep(outcome) {
    // A pinned (non-confirmed) report from a previous restore stays on
    // screen until dismissed or the next restore starts -- this first step
    // of a new one is exactly that "starts" signal, so drop the stale trail.
    if (this.restore.report && this.restore.report.status !== "confirmed") {
      this.restore = { steps: [outcome], report: null };
      return;
    }
    const key = (o) => `${o.kind}:${o.property ?? o.name}`;
    const steps = [...this.restore.steps];
    const idx = steps.findIndex((o) => key(o) === key(outcome));
    if (idx >= 0) steps[idx] = outcome;
    else steps.push(outcome);
    this.restore = { steps, report: null };
  }

  /** Confirmed still auto-clears after a few seconds like before; anything
   * else (partial/failed/interrupted) stays pinned -- and highlighted, see
   * restore-trail.js -- until _dismissRestore (×) or the next restore's
   * first step (_onRestoreStep above). */
  _onRestoreDone(report) {
    this.restore = { ...this.restore, report };
    clearTimeout(this._restoreTimer);
    if (report?.status === "confirmed") {
      this._restoreTimer = setTimeout(() => {
        this.restore = { steps: [], report: null };
      }, RESTORE_CLEAR_MS);
    }
  }

  _dismissRestore() {
    clearTimeout(this._restoreTimer);
    this.restore = { steps: [], report: null };
  }

  // --- footer status line ------------------------------------------------
  // Replaces the old floating toast: errors show in the footer until
  // dismissed (×, status-line.js) or the next action, instead of flashing
  // for a few seconds -- easy to miss is exactly the failure mode this
  // sub-project is closing.

  _status(message) {
    this.error = message;
  }

  _dismissError() {
    this.error = null;
  }

  _action(text) {
    this.error = null;
    this.lastAction = text;
  }

  /** Sets/clears the section-local failure note (see failure-note.js):
   * `source` is one of the section ids app.js hands out ("listen", "catalog",
   * "inbox", "recent", "nearby", "passes", "drawer", "setup", "instrument",
   * "library"; palette failures deliberately don't set one -- restore-trail's own
   * pinning already covers them, see _onRestoreDone). */
  _reportFailure(source, subject, status, detail) {
    this.failure = { source, subject, status, detail, at: Date.now() };
  }

  _clearFailure(source) {
    if (this.failure?.source === source) this.failure = null;
  }

  _failureFor(source) {
    return this.failure?.source === source ? this.failure : null;
  }

  /** Shared handler for a component's plain `api-error` event: footer text
   * plus (when `source` is one of the panels that renders .failure) the
   * pinned inline note. */
  _apiError(source, message, subject) {
    this._status(message);
    this._reportFailure(source, subject ?? source, "error", message);
  }

  /** Maps a RestoreReport (`{status, detail, ...}`, or null when there was
   * nothing to restore) to the footer: only "confirmed" is a real success --
   * a 2xx response with status partial/interrupted/failed must not read as
   * "tuned" in the UI. Returns true on confirmed so callers can gate
   * lastTuned/selection updates on it (partial is treated as not-confirmed:
   * some of the recipe may not have taken, so IDENT/undo shouldn't assume it did).
   * `source`, when given, also routes a non-confirmed result into the
   * matching panel's inline failure note (cleared again on the next confirmed
   * report for that source). */
  _reportStatus(report, successMessage, subject, source) {
    if (report && report.status === "confirmed") {
      this._action(successMessage);
      if (source) this._clearFailure(source);
      return true;
    }
    const detail = report?.detail ? `: ${report.detail}` : "";
    this._status(`${subject} ${report?.status ?? "failed"}${detail}`);
    if (source) this._reportFailure(source, subject, report?.status ?? "failed", report?.detail);
    return false;
  }

  _receiverUndoTarget() {
    const props = this.state?.props ?? {};
    if (props.device_vfo_frequency == null) return null;
    return {
      hz: props.device_vfo_frequency,
      demodulator: props.demodulator ?? "NFM",
      bandwidthHz: props.filter_bandwidth ?? null,
    };
  }

  _rememberUndo(action, at = Date.now()) {
    if (this._undoing) return;
    if (action.kind !== "library") this._redoActions = [];
    this._undoActions.push({ ...action, at, order: ++this._undoOrder });
    if (this._undoActions.length > 50) this._undoActions.shift();
  }

  _rememberTune(before, after, at) {
    if (!before || !after) return;
    this._rememberUndo({ kind: "tune", before }, at);
  }

  _rememberLibraryUndo(undo, at = Date.now()) {
    if (!undo?.id || this._undoActions.some((item) => item.kind === "library" && item.undoId === undo.id)) return;
    const eventAt = this._libraryUndoTime(undo, at);
    this._rememberUndo({ kind: "library", undoId: undo.id, label: undo.label }, eventAt);
  }

  _libraryUndoTime(undo, fallback = Date.now()) {
    const uuid = undo.id.replaceAll("-", "");
    const uuidTime = /^[0-9a-f]{12}/i.test(uuid) ? Number.parseInt(uuid.slice(0, 12), 16) : 0;
    return uuidTime || Date.parse(undo.created_at ?? "") || fallback;
  }

  _invalidateRedoForNewLibraryEdit(undo) {
    const lastRedoAt = this._redoActions.at(-1)?.at;
    if (lastRedoAt && this._libraryUndoTime(undo, 0) > lastRedoAt) this._redoActions = [];
  }

  _forgetLibraryUndo(id) {
    if (!id) return;
    this._undoActions = this._undoActions.filter((item) => !(item.kind === "library" && item.undoId === id));
  }

  async _syncLibraryUndo(at = Date.now()) {
    try {
      const { undo } = await api.libraryUndoPeek();
      if (undo) this._invalidateRedoForNewLibraryEdit(undo);
      this._rememberLibraryUndo(undo, at);
    } catch {
      // A failed peek must not turn an otherwise successful library action
      // into a failed action; the library's own inline undo remains available.
    }
  }

  _settingsBeforePatch(patch, original) {
    const before = {};
    for (const [key, value] of Object.entries(patch ?? {})) {
      if (!Object.hasOwn(original ?? {}, key)) continue;
      const previous = original[key];
      if (value && typeof value === "object" && !Array.isArray(value) && previous && typeof previous === "object") {
        const nested = this._settingsBeforePatch(value, previous);
        if (Object.keys(nested).length) before[key] = nested;
      } else {
        before[key] = previous;
      }
    }
    return before;
  }

  _rememberSettingsUndo(patch, original, label = "settings", at = Date.now()) {
    const inverse = this._settingsBeforePatch(patch, original);
    if (Object.keys(inverse).length) this._rememberUndo({ kind: "settings", patch: inverse, label }, at);
  }

  get tagsById() {
    return Object.fromEntries(this.tags.map((t) => [t.id, t]));
  }

  /** Color of the signal currently under the VFO (state.tuned_signal_id),
   * searched across the lists the UI already holds; null lights nothing. */
  get tunedColor() {
    const id = this.state?.tuned_signal_id;
    if (!id) return null;
    const found =
      (this.state?.nearby ?? []).find((s) => s.id === id) ||
      this.inbox.find((s) => s.id === id) ||
      this.recent.find((s) => s.id === id);
    return found?.color ?? null;
  }

  // --- signal detail panel (U3) --------------------------------------------
  // One aggregate panel (signal-panel.js) any "select a signal" surface can
  // open -- survey label chip/row, find row, memory rows (nearby/inbox/
  // recent/library), decode-view rows, scanner hits, catalog rows. Each of
  // those dispatches a bubbling "signal-open" CustomEvent rather than
  // threading a listener through every nested component's template; this is
  // the one place that catches it (window, since every component here is
  // light-DOM and the event bubbles straight up through it).

  /** `detail`: {frequency_hz, signal_id?, find_id?} -- at least one of
   * frequency_hz/signal_id/find_id should be set (the backend resolves the
   * others from whichever it gets). */
  openSignal(detail) {
    this._signalTarget = detail;
    if (detail) this._closeFrequencyDetails();
  }

  _onSignalOpen(ev) {
    this.openSignal(ev.detail);
  }

  // --- current-frequency details (ⓘ in the receiver strip) -----------------
  toggleFrequencyDetails() {
    if (this._freqDetailsOpen) this._closeFrequencyDetails();
    else {
      this._signalTarget = null;
      this._freqDetailsOpen = true;
      this._followFrequencyDetails(true);
    }
  }

  /** The ⓘ detail's suggested tuning, only while it is for the receiver's
   * current VFO (both known); the Scope row seeds its channel from it. */
  _scopeSuggestion() {
    const hz = this.state?.props?.device_vfo_frequency;
    const tuning = this._freqDetailsTuning;
    return hz != null && tuning?.frequency_hz === hz ? tuning : null;
  }

  /** Scope row under the waterfall: time domain beneath frequency domain. */
  _renderScope() {
    return html`<signal-instruments
      .frequencyHz=${this.state?.props?.device_vfo_frequency ?? 0}
      .receiverDemodulator=${this.state?.props?.demodulator ?? ""}
      .receiverBandwidth=${this.state?.props?.filter_bandwidth ?? 0}
      .suggestedDemodulator=${this._scopeSuggestion()?.demodulator ?? ""}
      .suggestedBandwidth=${this._scopeSuggestion()?.bandwidth_hz ?? 0}
      .replayActive=${!!this.state?.survey?.replay}></signal-instruments>`;
  }

  _closeFrequencyDetails() {
    clearTimeout(this._freqDetailsTimer);
    this._freqDetailsTimer = null;
    this._freqDetailsOpen = false;
    this._freqDetailsTarget = null;
  }

  /** Retarget the section to the receiver's VFO. Immediate on open; after a
   * state push it waits for tuning to pause (FREQ_DETAILS_SETTLE_MS) so a
   * held step key or a wheel burst issues one fetch, not one per step. A
   * disconnected receiver (no VFO) clears the target; the section says so. */
  _followFrequencyDetails(immediate = false) {
    const hz = this.state?.props?.device_vfo_frequency ?? null;
    const apply = () => {
      this._freqDetailsTimer = null;
      const now = this.state?.props?.device_vfo_frequency ?? null;
      if (now == null) this._freqDetailsTarget = null;
      else if (this._freqDetailsTarget?.frequency_hz !== now) this._freqDetailsTarget = { frequency_hz: now };
    };
    if (immediate || hz == null) {
      clearTimeout(this._freqDetailsTimer);
      return apply();
    }
    if (this._freqDetailsTarget?.frequency_hz === hz) {
      clearTimeout(this._freqDetailsTimer);
      this._freqDetailsTimer = null;
      return;
    }
    // Only a *different* pending frequency restarts the settle timer; repeated
    // state pushes carrying the same new frequency must not starve it.
    if (this._freqDetailsTimer && this._freqDetailsPendingHz === hz) return;
    clearTimeout(this._freqDetailsTimer);
    this._freqDetailsPendingHz = hz;
    this._freqDetailsTimer = setTimeout(apply, FREQ_DETAILS_SETTLE_MS);
  }

  // --- allocations (v0.53.0) -----------------------------------------------
  // <allocation-summary>'s "Open chart" action dispatches a bubbling
  // "open-allocation-chart" CustomEvent ({detail: {hz}}); this switches to
  // the ALLOCATIONS workspace and hands the frequency to <allocation-chart>
  // as `inspectHz`, which jumps to it and runs the same lookup a band click
  // would (see components/allocation-chart.js's `updated()`).

  async _onOpenAllocationChart(ev) {
    this._allocationInspectHz = ev.detail?.hz ?? null;
    await this._switchWorkspace("allocations");
  }

  // --- receiver owner (U1) -------------------------------------------------
  // "Who controls the receiver" (state.owner, see the "owner" ws case above)
  // plus the one shared gate every mode-taking action goes through before it
  // actually starts.

  /** GET /api/owner/conflicts?action=... ; when it would interrupt the
   * current owner, blocks on instrument-panel's inline CONTINUE/CANCEL strip
   * (not window.confirm) instead of firing the action outright. Resolves
   * true to proceed, false on CANCEL. Fails open (proceeds) if the conflicts
   * check itself errors -- a routing hiccup here shouldn't block the action;
   * the action's own request can still fail on its own terms. */
  async confirmInterrupt(action) {
    let conflicts;
    try {
      conflicts = await api.ownerConflicts(action);
    } catch {
      return true;
    }
    if (!conflicts?.interrupts?.length) return true;
    const message =
      conflicts.message || `This will stop ${conflicts.interrupts.join(", ").toUpperCase()}. Continue?`;
    // A second prompt replaces the first: settle the earlier one as CANCEL so
    // its caller (e.g. _doSurveyStart) isn't orphaned awaiting it forever.
    this._interruptConfirm?.resolve(false);
    return new Promise((resolve) => {
      this._interruptConfirm = {
        message,
        resolve: (ok) => {
          this._interruptConfirm = null;
          resolve(ok);
        },
      };
    });
  }

  _resolveInterrupt(ok) {
    this._interruptConfirm?.resolve(ok);
  }

  async _doStatusStop(kind) {
    if (kind === "survey") return this._doSurveyStop();
    if (kind === "band_scan") {
      return this._run(async () => {
        this._bandScanStatus = await api.stopSurveyBandScan();
        this._action("band scan stopped");
        this._clearFailure("survey");
      }, "survey", "band scan");
    }
    return this._doOwnerStop();
  }

  async _doOwnerStop() {
    await this._run(async () => {
      await api.ownerStop();
      this._action("stopped");
      this._clearFailure("instrument");
    }, "instrument", "owner stop");
  }

  /** Tracking start/stop (drawer.js's [ ⦿ TRACK ] button and sky-view.js's
   * own copy): both dispatch a plain "track-toggle" event (`active` = is
   * tracking currently on; `id` = the signal to track, only meaningful when
   * starting) rather than calling api.startTrack/stopTrack directly, so the
   * "track" conflicts check lives in one place. `source`, when given ("drawer"),
   * routes a failure into that panel's inline note, same as _reportFailure. */
  async _doTrackToggle(active, id, source) {
    if (active) {
      try {
        await api.stopTrack();
        this._action("tracking stopped");
        if (source) this._clearFailure(source);
      } catch (err) {
        if (source) this._apiError(source, err.message, "tracking");
        else this._status(err.message);
      }
      return;
    }
    if (!id) return;
    if (!(await this.confirmInterrupt("track"))) return;
    try {
      await api.startTrack(id);
      this._action("tracking started");
      if (source) this._clearFailure(source);
    } catch (err) {
      if (source) this._apiError(source, err.message, "tracking");
      else this._status(err.message);
    }
  }

  // --- actions -----------------------------------------------------------
  // Success paths intentionally don't refetch `state`: the server pushes a
  // fresh "state" (and "restore"/"journal") over the websocket for all of these.

  // Capture always lands in the inbox, and Back/Forward live on the
  // instrument panel, regardless of which UI (keyboard, palette, ...)
  // actually triggered them -- so their failures are tagged with that fixed
  // source rather than threading one through from every caller.

  async _doCapture() {
    if (this.state?.survey?.replay) {
      this._status("Replay is active; capture from the live receiver after stopping replay in Review.");
      return;
    }
    const undoAt = Date.now();
    await this._run(async () => {
      await api.capture();
      await this._syncLibraryUndo(undoAt);
      this._action("stored capture");
      this._clearFailure("inbox");
    }, "inbox", "capture");
  }

  async _doFavoriteCurrent() {
    if (this.state?.survey?.replay) {
      this._status("Stop replay before favoriting the live receiver frequency.");
      return;
    }
    const hz = this.state?.props?.device_vfo_frequency;
    if (!this.state?.connected || !Number.isFinite(hz) || hz <= 0) return;
    await this._run(async () => {
      const signals = await api.listSignals();
      let signal = signals.find((s) => Math.abs(s.frequency_hz - hz) <= 1 &&
        s.demodulator === this.state?.props?.demodulator);
      if (!signal) {
        const undoAt = Date.now();
        signal = await api.capture();
        await this._syncLibraryUndo(undoAt);
        this._loadInbox();
        this._loadRecent();
        this._loadSignals();
      }
      if (!this.favorites.some((s) => s.id === signal.id)) {
        await api.addListItems("favorites", [signal.id]);
        this._action(`favorited ${(hz / 1e6).toFixed(6)} MHz`);
      } else {
        this._action("frequency already in Favorites");
      }
      await this._loadFavorites();
      await this._loadRecent();
    }, "listen", "favorite current frequency");
  }

  async _doBack(stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doBack(true));
    if (this.state?.survey?.replay) return this._status("Back is unavailable while replay is active.");
    await this._run(async () => {
      const { report } = await api.back();
      if (report === null) {
        this._action("nothing to go back to");
        return;
      }
      this._reportStatus(report, "moved back", "back", "instrument");
    }, "instrument", "back");
  }

  async _doForward(stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doForward(true));
    if (this.state?.survey?.replay) return this._status("Forward is unavailable while replay is active.");
    await this._run(async () => {
      const { report } = await api.forward();
      if (report === null) {
        this._action("nothing to go forward to");
        return;
      }
      this._reportStatus(report, "moved forward", "forward", "instrument");
    }, "instrument", "forward");
  }

  /** `source`, when given, tags a failure to the panel the tune was
   * triggered from (nearby/recent/listen/passes/inbox); omitted for palette
   * and drawer.js tunes, which rely on the footer + restore-trail instead. */
  async _doTuneSignal(id, source, stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doTuneSignal(id, source, true));
    const found =
      this.signals.find((s) => s.id === id) ||
      this.inbox.find((s) => s.id === id) ||
      this.recent.find((s) => s.id === id) ||
      (this.state?.nearby ?? []).find((s) => s.id === id);
    const label = found ? found.name : "signal";
    if (this.state?.survey?.replay) {
      if (found?.frequency_hz != null) {
        return this._doReviewTune(found.frequency_hz, found.demodulator, found.filter_bandwidth_hz);
      }
      return this._status(`Cannot tune ${label} during replay; stop replay in Review first.`);
    }
    const before = this._receiverUndoTarget();
    const undoAt = Date.now();
    let confirmed = false;
    await this._run(async () => {
      const report = await api.tuneSignal(id);
      confirmed = this._reportStatus(report, `tuned ${label}`, label, source);
    }, source, label);
    // Only trust a confirmed restore for IDENT/undo's "what am I tuned to" --
    // a partial/failed one may not have actually landed on `found`.
    if (confirmed) {
      if (found) this.lastTuned = { kind: "signal", item: found };
      this._rememberTune(before, found
        ? { hz: found.frequency_hz, demodulator: found.demodulator ?? before?.demodulator ?? "NFM", bandwidthHz: found.filter_bandwidth_hz ?? before?.bandwidthHz ?? null }
        : this._receiverUndoTarget(), undoAt);
    }
  }

  async _doTuneFrequency(hz, demodulator, filterBandwidthHz, source, options = {}) {
    if (!options.step && !options.stepPauseHeld) {
      return this._withStepTunePaused(() => this._doTuneFrequency(
        hz, demodulator, filterBandwidthHz, source, { ...options, stepPauseHeld: true }
      ));
    }
    if (this.state?.survey?.replay) {
      return this._doReviewTune(hz, demodulator, filterBandwidthHz);
    }
    const before = this._receiverUndoTarget();
    const undoAt = Date.now();
    const after = {
      hz,
      demodulator: demodulator ?? before?.demodulator ?? "NFM",
      bandwidthHz: filterBandwidthHz ?? before?.bandwidthHz ?? null,
    };
    let confirmed = false;
    await this._run(async () => {
      const report = await api.tuneFrequency(hz, demodulator, filterBandwidthHz, options.manualMode);
      confirmed = this._reportStatus(report, `tuned ${mhz(hz)}`, mhz(hz), source);
      if (confirmed) {
        this._rememberTune(before, after, undoAt);
      }
    }, source, mhz(hz));
    return confirmed;
  }

  /** ANT select (instrument-panel.js's _onAntennaChange): a RestoreReport
   * like the tune routes, routed through the same "instrument" failure note
   * -- the panel clears its own pending marker once this.state (confirmed)
   * or this.failure updates, see its updated(). */
  async _doSetAntenna(antenna) {
    await this._run(async () => {
      const report = await api.setAntenna(antenna);
      this._reportStatus(report, `antenna ${antenna}`, antenna, "instrument");
    }, "instrument", antenna);
  }

  /** Primary spectrum's tune-frequency while workspace is REVIEW and a
   * replay is running: tunes the *virtual* receiver (POST /api/review/tune)
   * instead of _doTuneFrequency's POST /api/tune/frequency -- the S3
   * zero-live-writes exit gate means a click on the replay's spectrum must
   * never reach the live SDRconnect adapter. Keeps the currently-tuned
   * demodulator/bandwidth (read fresh from GET /api/review -- app.js doesn't
   * otherwise track the virtual receiver's state; review-view.js owns that
   * polling loop). */
  async _doReviewTune(hz, demodulator, bandwidthHz) {
    await this._run(async () => {
      const current = await api.getReview();
      const receiver = current?.receiver;
      await api.reviewTune(
        hz,
        demodulator ?? receiver?.demodulator ?? "NFM",
        bandwidthHz ?? receiver?.bandwidth_hz ?? null
      );
    }, "survey", mhz(hz));
  }

  _canStepTune() {
    return !!this.state?.connected && !!this.state?.can_control && !this.wsDown &&
      !this.state?.survey?.replay && this._stepTunePauseCount === 0;
  }

  _cancelStepTuning() {
    this._stepConfirmedTargetSeen = false;
    return this._stepTuner?.cancel() ?? Promise.resolve();
  }

  async _withStepTunePaused(operation) {
    this._stepTunePauseCount += 1;
    try {
      await this._cancelStepTuning();
      return await operation();
    } finally {
      this._stepTunePauseCount -= 1;
    }
  }

  /** ArrowLeft/Right or instrument-panel's ◂ ▸ buttons: steps by the mode's
   * default size (defaultStepHz) unless _stepOverrideHz is set, scaled by
   * `multiplier` (Shift x10, Alt x0.1, floored at 10 Hz). The adjacent fine
   * controls supply an exact fixedStepHz independent of that step setting.
   * The first tune is
   * immediate; held repeats accumulate from its target and drain serially at
   * a bounded rate, so the final requested frequency is retained. */
  _doStepFrequency(direction, multiplier = 1, fixedStepHz = null) {
    if (!this._canStepTune()) {
      this._cancelStepTuning();
      return;
    }
    const base = this.state?.props?.device_vfo_frequency;
    if (base === null || base === undefined) return;
    const stepHz = this._stepOverrideHz ?? defaultStepHz(this.state?.props?.demodulator);
    const amount = Number.isFinite(fixedStepHz)
      ? Math.max(1, Math.round(fixedStepHz))
      : Math.max(10, Math.round(stepHz * multiplier));
    this._stepTuner.step(base, direction * amount, (hz) => {
      if (!this._canStepTune()) return Promise.resolve(false);
      this._stepConfirmedTargetSeen = false;
      return this._doTuneFrequency(hz, undefined, undefined, "instrument", { step: true })
        .then((confirmed) => {
          this._stepConfirmedTargetSeen = !!confirmed && this.state?.props?.device_vfo_frequency === hz;
          this.requestUpdate();
          return confirmed;
        });
    });
  }

  /** instrument-panel's step-size button: walks STEP_CYCLE_HZ, wrapping --
   * an explicit choice that overrides the mode default until the mode
   * changes (see updated()). */
  _cycleStep() {
    this._cancelStepTuning();
    const current = this._stepOverrideHz ?? defaultStepHz(this.state?.props?.demodulator);
    const idx = STEP_CYCLE_HZ.indexOf(current);
    this._stepOverrideHz = STEP_CYCLE_HZ[(idx + 1) % STEP_CYCLE_HZ.length];
  }

  /** Global ArrowLeft/Right step shortcut is skipped when a grip or another
   * focused button/input would otherwise own arrow keys (layout grips call
   * preventDefault in their own keydown handler, see _onGripKeydown), or
   * while the palette/setup drawer/an editor panel is open. */
  _blocksGlobalStep(ev) {
    if (this.paletteOpen || this.setupOpen || this.selected || this._signalTarget) return true;
    const t = ev.target;
    if (t?.classList?.contains("grip")) return true;
    const tag = t?.tagName;
    return tag === "BUTTON" || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  }

  /** PASSES row click: tunes the memory bound to that satellite, if any --
   * searched across the full signal list (inbox/recent don't cover it). */
  async _doTuneSatellite(satellite, downlink) {
    const found = this.signals.find((s) => s.satellite === satellite);
    if (found) {
      await this._doTuneSignal(found.id, "passes");
      return;
    }
    // No bound memory -- fall back to the pass row's own primary downlink
    // (msdr's SatNOGS catalog, or nextpass --radio), when there is one.
    if (downlink) {
      await this._doTuneFrequency(downlink.frequency_hz, downlink.mode, undefined, "passes");
      return;
    }
    this._action(`no memory bound to ${satellite}`);
  }

  async _onSelectPass(detail) {
    this.selectedPass = detail;
    if (detail.open) {
      await this.updateComplete;
      const chart = this.querySelector("sky-view");
      await chart?.updateComplete;
      chart?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      chart?.querySelector(".satellite-charts")?.focus({ preventScroll: true });
      if (!chart) this._status("Enable SKY in Setup → Features to show pass charts.");
    }
  }

  /** Settings PATCH may have changed state.features (or catalog_languages),
   * so re-fetch both and re-sync the passes timer/gated sections rather than
   * just the passes refresh the old handler did. */
  async _onSettingsSaved() {
    await Promise.all([
      this._run(() => this._fetchState()),
      this._loadSettings(),
    ]);
    this._syncFeatureTimers();
    if (this.features.passes) await this._loadPasses();
    this.settingsSavedAt = Date.now();
  }

  async _doTuneCatalog(id, label, source, stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doTuneCatalog(id, label, source, true));
    if (this.state?.survey?.replay) {
      return this._status("Catalog tuning is live-radio only; stop replay in Review first.");
    }
    const before = this._receiverUndoTarget();
    const undoAt = Date.now();
    await this._run(async () => {
      const report = await api.tuneCatalog(id);
      if (this._reportStatus(
        report,
        label ? `tuned ${label}` : "tuned catalog entry",
        label || "catalog entry",
        source
      ) && before) this._rememberUndo({ kind: "tune", before }, undoAt);
    }, source, label || "catalog entry");
  }

  /** CATALOG/LISTEN row "★": saves a catalog entry as an inbox signal.
   * Recipe (mode/bandwidth/notes) is computed server-side from the entry
   * (see AppService.save_catalog_entry) so it matches what tuning the entry
   * would actually set, instead of a hardcoded AM/9kHz. */
  async _doStarCatalog(id, label, source) {
    const undoAt = Date.now();
    await this._run(async () => {
      const signal = await api.saveCatalogEntry(id);
      await this._syncLibraryUndo(undoAt);
      this._loadInbox();
      this._loadRecent();
      this._action(`saved ${label || signal.station}`);
      if (source) this._clearFailure(source);
    }, source, label || "catalog entry");
  }

  async _doRefreshPasses() {
    this._action("refreshing TLEs…");
    try {
      this.passes = await api.refreshPasses();
      this._action("TLEs refreshed");
      this._clearFailure("passes");
    } catch (err) {
      this._apiError("passes", err.message, "TLE refresh");
    }
  }

  /** SCANS event row "★": saves an iqscan detection as an inbox signal. */
  async _doStarEvent(scan, event) {
    const mhzVal = (event.frequency_hz / 1_000_000).toFixed(6);
    const undoAt = Date.now();
    await this._run(async () => {
      await api.createSignal({
        name: `UNKNOWN ${mhzVal} MHz`,
        frequency_hz: event.frequency_hz,
        status: "unknown",
        notes: `iqscan ${scan.id} event ${event.id}`,
      });
      await this._syncLibraryUndo(undoAt);
      this._loadInbox();
      this._action(`saved UNKNOWN ${mhzVal} MHz`);
    });
  }

  // --- scanner -------------------------------------------------------------
  // Every scan/* endpoint returns the fresh status, so the success path here
  // just adopts it directly rather than waiting on the next "scan" websocket
  // push -- the push still arrives and keeps things live between actions.

  async _doScanStart(config) {
    if (!(await this.confirmInterrupt("scan"))) return;
    await this._run(async () => {
      this.scan = await api.scannerStart(config);
      this._action("scan started");
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  async _doScanPause() {
    await this._run(async () => {
      this.scan = await api.scannerPause();
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  async _doScanResume() {
    await this._run(async () => {
      this.scan = await api.scannerResume();
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  async _doScanStop() {
    await this._run(async () => {
      this.scan = await api.scannerStop();
      this._action("scan stopped");
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  async _doScanHold() {
    await this._run(async () => {
      this.scan = await api.scannerHold();
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  async _doScanSkip(persist) {
    await this._run(async () => {
      this.scan = await api.scannerSkip(persist);
      this._clearFailure("scanner");
    }, "scanner", "scan");
  }

  // --- pre-roll recorder ---------------------------------------------------

  async _doRecorderArm(on) {
    await this._run(async () => {
      this.recorder = await api.recorderArm(on);
      this._action(on ? "recorder armed" : "recorder disarmed");
      this._clearFailure("instrument");
    }, "instrument", "recorder");
  }

  async _doRecorderRec() {
    if (!(await this.confirmInterrupt("record"))) return;
    await this._run(async () => {
      this.recorder = await api.recorderStart();
      this._action("recording");
      this._clearFailure("instrument");
    }, "instrument", "recorder");
  }

  async _doRecorderStop() {
    await this._run(async () => {
      this.recorder = await api.recorderStop();
      this._action("recording stopped");
      this._clearFailure("instrument");
    }, "instrument", "recorder");
  }

  /** RECORDINGS modal row actions (recordings-view.js handles its own list
   * refresh/errors for reveal/analyze/delete there) -- these two are just the
   * recorder-control.js post-stop recap's OPEN FOLDER/ANALYZE shortcuts for
   * the just-finished recording (status.last_recording), forwarded through
   * instrument-panel. */
  async _doRecorderReveal(name) {
    await this._run(async () => {
      const res = await api.revealRecording(name);
      if (res && res.ok === false) this._status(res.error || res.path || "could not open folder");
      else this._action(`opened ${name}`);
    }, "instrument", "recordings");
  }

  async _doRecorderAnalyze(name) {
    await this._run(async () => {
      await api.analyzeRecording(name);
      this._action(`analyzing ${name}`);
    }, "instrument", "recordings");
  }

  // --- decoders (P1) -------------------------------------------------------
  // Manual start/stop on the current VFO (decode-deck's gear modal); memory-
  // driven start/stop happens server-side in AppService.tune_signal and just
  // shows up here via the "decoder"/"decoder_event" websocket messages above.

  async _doDecoderStart(decoders, frequencyHz = null) {
    if (this.state?.survey?.replay) return this._status("Live decoding is unavailable during replay; stop replay in Review first.");
    await this._run(async () => {
      const status = await api.startDecoder(decoders, frequencyHz);
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("decoding started");
      this._clearFailure("decoder");
    }, "decoder", "decode");
  }

  async _doDecoderStop() {
    if (this.state?.survey?.replay) return this._status("Live decoder controls are unavailable during replay; stop replay in Review first.");
    await this._run(async () => {
      const status = await api.stopDecoder();
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("decoding stopped");
      this._clearFailure("decoder");
    }, "decoder", "decode");
  }

  // --- band decode (P2) -----------------------------------------------------
  // Multi-channel NFM-family decode across a span (decode-deck's "Band
  // decode" section); status rides along in the same status.decoder payload,
  // under status.band.

  async _doBandStart(detail) {
    if (this.state?.survey?.replay) return this._status("Live band decoding is unavailable during replay; stop replay in Review first.");
    if (!(await this.confirmInterrupt("band_decode"))) return;
    await this._run(async () => {
      const status = await api.startBand({
        decoders: detail.decoders,
        max_channels: detail.max_channels,
        threshold_db: detail.threshold_db,
      });
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("band decode started");
      this._clearFailure("decoder");
    }, "decoder", "band decode");
  }

  async _doBandStop() {
    if (this.state?.survey?.replay) return this._status("Live band decoder controls are unavailable during replay; stop replay in Review first.");
    await this._run(async () => {
      const status = await api.stopBand();
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("band decode stopped");
      this._clearFailure("decoder");
    }, "decoder", "band decode");
  }

  async _doBandSave(frequencyHz) {
    if (this.state?.survey?.replay) return this._status("Live band decoder controls are unavailable during replay; stop replay in Review first.");
    await this._run(async () => {
      const signal = await api.saveBandChannel(frequencyHz);
      this._loadSignals();
      this._loadInbox();
      this._action(`saved ${signal.name ?? "channel"} as memory`);
      this._clearFailure("decoder");
    }, "decoder", "band decode");
  }

  async _doInstallPresets() {
    await this._run(async () => {
      const { created, skipped } = await api.installDecoderPresets();
      this._loadSignals();
      this._loadInbox();
      this._action(`presets: ${created} created, ${skipped} skipped`);
      this._clearFailure("decoder");
    }, "decoder", "install presets");
  }

  // --- survey (S1) ---------------------------------------------------------
  // Span picker + spectrum/waterfall/occupancy (survey-view.js). Status
  // (Survey.status()) rides along in state.survey (see GET /api/state and the
  // "survey" ws case above); each of these REST calls also returns the fresh
  // status directly, so it's applied here without waiting for the next push.

  async _doSurveyStart() {
    await this._run(async () => {
      const status = await api.startSurvey();
      this.state = { ...(this.state ?? {}), survey: status };
      this._action("survey started");
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  async _doSurveyStop() {
    await this._run(async () => {
      const status = await api.stopSurvey();
      this.state = { ...(this.state ?? {}), survey: status };
      this._action("survey stopped");
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  async _doSurveyClear() {
    await this._run(async () => {
      const status = await api.clearSurvey();
      this.state = { ...(this.state ?? {}), survey: status };
      this._action("survey cleared");
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  async _doSurveySpan(body) {
    if (!(await this.confirmInterrupt("survey_span"))) return;
    await this._run(async () => {
      const status = await api.setSurveySpan(body);
      this.state = { ...(this.state ?? {}), survey: status };
      this._action(`survey span ${mhz(body.center_hz)}`);
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  async _doSurveyMarkerSettings(detail) {
    if (this._surveyPeakBusy || !this.settings?.survey) return;
    const patch = {};
    if (typeof detail.center_exclusion_enabled === "boolean") patch.center_exclusion_enabled = detail.center_exclusion_enabled;
    if (Number.isFinite(detail.marker_hold_s) && detail.marker_hold_s >= 0 && detail.marker_hold_s <= 60) patch.marker_hold_s = detail.marker_hold_s;
    if (!Object.keys(patch).length) return;
    this._surveyPeakBusy = true;
    try {
      await this._run(async () => {
        this.settings = await api.patchSettings({survey: {...this.settings.survey, ...patch}});
        this._clearFailure("survey");
      }, "survey", "DSP markers");
    } finally { this._surveyPeakBusy = false; }
  }

  async _setReceiverLock(setting, what, locked) {
    await this._run(async () => {
      this.settings = await api.patchSettings({ [setting]: locked });
      this._action(locked ? `${what} locked` : `${what} unlocked`);
    }, "instrument", `${what} lock`);
  }

  async _doSurveyPeakThreshold(db) {
    if (this._surveyPeakBusy || !Number.isFinite(db) || db < 0 || db > 60 || !this.settings?.survey) return;
    const previous = this.settings;
    if (db === previous.survey.peak_threshold_db) return;
    const patch = { survey: { ...previous.survey, peak_threshold_db: db } };
    this._surveyPeakBusy = true;
    try {
      await this._run(async () => {
        this.settings = await api.patchSettings(patch);
        this._rememberSettingsUndo({ survey: { peak_threshold_db: db } }, previous, "DSP peak threshold");
        this._action(`DSP peak threshold ${db} dB`);
        this._clearFailure("survey");
      }, "survey", "peak threshold");
    } finally {
      this._surveyPeakBusy = false;
    }
  }

  /** REPLAY picker's ▶ PLAY button (survey-view.js): surveys a recorded
   * SigMF file instead of the live stream -- POST /api/survey/replay. A bad
   * path/folder comes back as a 409, which _run/_reportFailure surfaces the
   * same way every other survey REST call's errors do. */
  async _doSurveyReplay(detail) {
    await this._run(async () => {
      const status = await api.startSurveyReplay(detail.path, detail.loop);
      this.state = { ...(this.state ?? {}), survey: status };
      this._action(`survey replay: ${detail.path}`);
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  /** Replay transport controls (survey-view.js's ⏸/▶, seek slider and loop
   * checkbox) -- POST /api/survey/replay/control. */
  async _doSurveyReplayControl(detail) {
    await this._run(async () => {
      const status = await api.controlSurveyReplay(detail);
      this.state = { ...(this.state ?? {}), survey: status };
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  /** ⏹ STOP REPLAY: back to the live SDRconnect stream (survey itself stops,
   * same as ■ STOP -- the operator restarts it live from there). */
  async _doSurveyReplayStop() {
    await this._run(async () => {
      const status = await api.stopSurveyReplay();
      this.state = { ...(this.state ?? {}), survey: status };
      this._action("survey replay stopped");
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  /** S2 dimension toggles (survey-view.js's checkboxes) -- POST /api/survey/dimensions.
   * Turning on an IQ-needing dimension while the analyzer isn't already
   * running starts it, so that case goes through the "analyzer" conflicts
   * check first (see confirmInterrupt). */
  async _doSurveyDimensions(dimensions) {
    const availableDims = this.state?.survey?.available_dimensions ?? [];
    const needsIq = new Set(availableDims.filter((d) => d.needs_iq).map((d) => d.id));
    const analyzerActive = !!this.state?.survey?.analyzer?.active;
    const startsAnalyzer = !analyzerActive && dimensions.some((d) => needsIq.has(d));
    if (startsAnalyzer && !(await this.confirmInterrupt("analyzer"))) return;
    await this._run(async () => {
      const status = await api.setSurveyDimensions(dimensions);
      this.state = { ...(this.state ?? {}), survey: status };
      this._action(`survey dimensions: ${dimensions.length ? dimensions.join(", ") : "none"}`);
      this._clearFailure("survey");
    }, "survey", "survey");
  }

  /** S5 row action (survey-view.js's "→ DECODE" button): pins a band-decoder
   * channel on this label's frequency (POST /api/survey/decode), starting a
   * band session if none is active yet. `detail.decoders` is null to take the
   * server's default (the label's family.suggested_decoders) or an explicit
   * list from survey-view's manual-pick popover. Result is a decoder status
   * (same shape as startBand/stopBand), merged into state.decoder like the P2
   * band-decode handlers. */
  async _doSurveyDecode(detail) {
    await this._run(async () => {
      const status = await api.surveyDecode(detail.frequency_hz, detail.decoders ?? null);
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("decode started");
      this._clearFailure("decoder");
    }, "decoder", "survey decode");
  }

  /** S5 row action ("■" button, shown when label.decoding): unpins the
   * band-decoder channel on this frequency. */
  async _doSurveyUndecode(frequencyHz) {
    await this._run(async () => {
      const status = await api.surveyUndecode(frequencyHz);
      this.state = { ...(this.state ?? {}), decoder: status };
      this._action("decode stopped");
      this._clearFailure("decoder");
    }, "decoder", "survey decode");
  }

  /** S5 row action ("SAVE" button, always enabled): saves the label as a
   * memory, server side deriving name/decoders/notes from
   * Survey.identity_summary. */
  async _doSurveySave(frequencyHz) {
    const undoAt = Date.now();
    await this._run(async () => {
      const signal = await api.surveySave(frequencyHz, null);
      await this._syncLibraryUndo(undoAt);
      this._loadSignals();
      this._loadInbox();
      this._action(`saved ${signal.name ?? "channel"} as memory`);
      this._clearFailure("decoder");
    }, "decoder", "survey save");
  }

  /** S5: clicking a confirmed label's ✓ chip (survey-view.js) opens the
   * decoded-messages modal preset to that label's frequency. */
  _doOpenSurveyMessages(frequencyHz) {
    this.decodeViewPresetFrequencyHz = frequencyHz;
    this.decodeViewOpen = true;
  }

  // --- beacons (S10) --------------------------------------------------------
  // NCDXF/NDB beacon monitor (beacons-view.js): status (BeaconMonitor.status())
  // rides along in state.beacons (see GET /api/state and the "beacons" ws case
  // above); start/stop also return the fresh status directly, applied here
  // without waiting for the next push, same as survey's start/stop.

  async _doBeaconsStart(mode, frequenciesHz) {
    if (!(await this.confirmInterrupt("beacons"))) return;
    await this._run(async () => {
      const status = await api.startBeacons({ mode, frequencies_hz: frequenciesHz ?? null });
      this.state = { ...(this.state ?? {}), beacons: status };
      this._action(`beacons started (${mode})`);
      this._clearFailure("beacons");
    }, "beacons", "beacons");
  }

  async _doBeaconsStop() {
    await this._run(async () => {
      const status = await api.stopBeacons();
      this.state = { ...(this.state ?? {}), beacons: status };
      this._action("beacons stopped");
      this._clearFailure("beacons");
    }, "beacons", "beacons");
  }

  /** Scanner hit log "★": saves a hit as an inbox signal, using the scan's
   * current recipe (demodulator/bandwidth) for a range hit -- a list hit
   * already names an existing signal_id, so there's no recipe to attach. */
  async _doStarHit(hit) {
    const label = hit.label || mhz(hit.frequency_hz);
    const undoAt = Date.now();
    await this._run(async () => {
      const cfg = this.scan?.config ?? {};
      const body = {
        name: label,
        frequency_hz: hit.frequency_hz,
        status: "unknown",
        notes: `scanner hit ${label}`,
      };
      if (cfg.demodulator) body.demodulator = cfg.demodulator;
      if (cfg.filter_bandwidth_hz) body.filter_bandwidth_hz = cfg.filter_bandwidth_hz;
      await api.createSignal(body);
      await this._syncLibraryUndo(undoAt);
      this._loadInbox();
      this._loadRecent();
      this._action(`saved ${label}`);
      this._clearFailure("scanner");
    }, "scanner", label);
  }

  async _doApplyProfile(id, source, stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doApplyProfile(id, source, true));
    if (this.state?.survey?.replay) {
      return this._status("Profiles change the live SDRconnect receiver; stop replay in Review first.");
    }
    const found = this.profiles.find((p) => p.id === id);
    let confirmed = false;
    await this._run(async () => {
      const report = await api.applyProfile(id);
      confirmed = this._reportStatus(
        report,
        found ? `applied ${found.name}` : "applied profile",
        found?.name ?? "profile",
        source
      );
    }, source, found?.name ?? "profile");
    if (confirmed && found) this.lastTuned = { kind: "profile", item: found };
  }

  async _doExport() {
    await this._run(async () => {
      const doc = await api.exportLibrary();
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `msdr-export-${todayUtc()}.json`;
      a.click();
      URL.revokeObjectURL(url);
      this._action("exported library");
    });
  }

  /** Global Cmd/Ctrl-Z: compare the newest server-side library edit with this
   * tab's reversible receiver/settings actions, then undo whichever is newer.
   * Text fields keep browser-native undo because the key handler exits while
   * typing. Received decoder output and recordings never enter this stack. */
  async _doUndo(stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doUndo(true));
    if (this._undoPending) return;
    this._undoPending = true;
    this._undoing = true;
    try {
      const latestLibrary = (await api.libraryUndoPeek())?.undo ?? null;
      const libraryAction = latestLibrary
        ? this._undoActions.findLast((item) => item.kind === "library" && item.undoId === latestLibrary.id)
        : null;
      const local = this._undoActions.filter((item) => item.kind !== "library").at(-1) ?? null;
      const latestId = latestLibrary?.id?.replaceAll("-", "") ?? "";
      const latestUuidTime = /^[0-9a-f]{12}/i.test(latestId) ? Number.parseInt(latestId.slice(0, 12), 16) : 0;
      const libraryAt = libraryAction?.at ?? (latestUuidTime || Date.parse(latestLibrary?.created_at ?? "") || 0);
      if (latestLibrary && (!local || libraryAt >= local.at)) {
        const result = await api.libraryUndo();
        this._forgetLibraryUndo(latestLibrary.id);
        this._redoActions.push({ kind: "library", undoId: latestLibrary.id, at: Date.now() });
        this._action(`undone: ${result.undone}`);
        this._loadInbox();
        this._loadRecent();
        this._loadSignals();
        this._libraryRefreshAt = Date.now();
        return;
      }
      if (!local) {
        this._status("Nothing to undo");
        return;
      }
      if (local.kind === "tune") {
        if (this.state?.survey?.replay) {
          this._status("Stop replay in Review before undoing a live receiver tune.");
          return;
        }
        const redoTarget = this._receiverUndoTarget();
        const { hz, demodulator, bandwidthHz } = local.before;
        const report = await api.tuneFrequency(hz, demodulator, bandwidthHz);
        if (this._reportStatus(report, `undid tune to ${mhz(hz)}`, mhz(hz), "instrument")) {
          this._undoActions = this._undoActions.filter((item) => item !== local);
          if (redoTarget) this._redoActions.push({ kind: "tune", target: redoTarget, at: Date.now() });
        }
      } else if (local.kind === "decoder-preset") {
        const report = await api.restoreDecoderReceiver(local.token);
        if (this._reportStatus(report, "Receiver settings restored", "decoder preset", "decoder")) {
          this._undoActions = this._undoActions.filter(item => item !== local);
          this._redoActions.push({ kind: "decoder-preset", token: local.token, at: Date.now() });
          const deck = this.querySelector('decode-deck');
          if (deck) deck._restoreToken = null;
        }
      } else if (local.kind === "settings") {
        const redoPatch = this._settingsBeforePatch(local.patch, this.settings);
        await api.patchSettings(local.patch);
        this._undoActions = this._undoActions.filter((item) => item !== local);
        this._redoActions.push({ kind: "settings", patch: redoPatch, label: local.label, at: Date.now() });
        this._action(`undid ${local.label}`);
        await this._onSettingsSaved();
      }
    } catch (err) {
      this._status(err.message);
    } finally {
      this._undoing = false;
      this._undoPending = false;
    }
  }

  /** Redo only a confirmed Undo. New edits clear this stack; the Library
   * endpoint also checks the exact undo id so another tab cannot redirect it. */
  async _doRedo(stepPauseHeld = false) {
    if (!stepPauseHeld) return this._withStepTunePaused(() => this._doRedo(true));
    if (this._undoPending) return;
    const action = this._redoActions.at(-1);
    if (!action) return this._status("Nothing to redo");
    this._undoPending = true;
    this._undoing = true;
    try {
      if (action.kind === "library") {
        const result = await api.libraryRedo(action.undoId);
        this._redoActions.pop();
        this._undoActions.push({ kind: "library", undoId: action.undoId,
          label: result.redone, at: Date.now(), order: ++this._undoOrder });
        this._action(`redone: ${result.redone}`);
        this._loadInbox(); this._loadRecent(); this._loadSignals();
        this._libraryRefreshAt = Date.now();
      } else if (action.kind === "tune") {
        if (this.state?.survey?.replay) {
          this._status("Stop replay in Review before redoing a live receiver tune.");
          return;
        }
        const before = this._receiverUndoTarget();
        const { hz, demodulator, bandwidthHz } = action.target;
        const report = await api.tuneFrequency(hz, demodulator, bandwidthHz);
        if (this._reportStatus(report, `redid tune to ${mhz(hz)}`, mhz(hz), "instrument")) {
          this._redoActions.pop();
          if (before) this._undoActions.push({ kind: "tune", before,
            at: Date.now(), order: ++this._undoOrder });
        }
      } else if (action.kind === "decoder-preset") {
        const report = await api.reapplyDecoderReceiver(action.token);
        if (this._reportStatus(report, "Receiver settings reapplied", "decoder preset", "decoder")) {
          this._redoActions.pop();
          this._undoActions.push({ kind: "decoder-preset", token: action.token,
            at: Date.now(), order: ++this._undoOrder });
          const deck = this.querySelector("decode-deck");
          if (deck) deck._restoreToken = action.token;
        }
      } else if (action.kind === "settings") {
        const undoPatch = this._settingsBeforePatch(action.patch, this.settings);
        await api.patchSettings(action.patch);
        this._redoActions.pop();
        this._undoActions.push({ kind: "settings", patch: undoPatch, label: action.label,
          at: Date.now(), order: ++this._undoOrder });
        this._action(`redid ${action.label}`);
        await this._onSettingsSaved();
      }
    } catch (err) {
      if (action.kind === "library" && /nothing to redo|changed since undo/.test(err.message)) {
        this._redoActions = [];
      }
      this._status(err.message);
    } finally {
      this._undoing = false;
      this._undoPending = false;
    }
  }

  _shiftDay(offset) {
    const sorted = [...this.journalDays].sort();
    const idx = sorted.indexOf(this.journalDay);
    const next = sorted[idx + offset];
    if (!next) return;
    this.journalDay = next;
    this._loadJournalDay(next);
  }

  // --- layout (drag-to-rearrange sections) --------------------------------
  // Server-side source of truth (Settings.workspaces[workspace], U2's own
  // Layout per workspace, msdr/settings.py): loaded in _loadSettings(),
  // PATCHed back debounced so a drag doesn't spam the API. Hidden
  // (feature-off) sections keep their slot in layout.columns but are skipped
  // by _visibleIdsForColumn/_sectionNode. layout.wide ids also keep their
  // columns slot (so toggling back to CARD restores their old position) but
  // are skipped by _visibleIdsForColumn too, since they render in the wide
  // band instead -- see _visibleWideIds/_renderWideBand. On top of that, a
  // workspace's Layout also carries every *other* workspace's section ids,
  // backfilled into its last column by the backend's Layout validator so it
  // stays a full, valid Layout (see format.js's WORKSPACE_SECTIONS) -- both
  // methods below additionally drop anything not in the active workspace's
  // own WORKSPACE_SECTIONS, so that backfilled tail never renders.

  _workspaceMembers() {
    const members = WORKSPACE_SECTIONS[this.workspace] ?? [];
    // The scanner lives with the receiver. Keep saved "scan" layout entries
    // valid for older settings, but do not render a second deck in Discover.
    if (this.workspace === "discover") {
      return this.discoverView === "spectrum"
        ? members.filter(id => !["scan", "finds", "inbox"].includes(id)) : [];
    }
    return members;
  }

  _visibleIdsForColumn(colIndex) {
    const wideIds = this.layout?.wide ?? DEFAULT_WORKSPACE_WIDE[this.workspace] ?? [];
    const ids = this.layout?.columns?.[colIndex]
      ?? DEFAULT_WORKSPACE_COLUMNS[this.workspace]?.[colIndex]
      ?? [];
    const members = this._workspaceMembers();
    return ids.filter(
      (id) =>
        members.includes(id) &&
        !(this._usesListenWorkbench() && LISTEN_WORKBENCH_SECTIONS.has(id)) &&
        sectionVisible(id, this.features) &&
        !wideIds.includes(id) &&
        !this._inPrimarySlot(id) &&
        !this._renderedElsewhere(id)
    );
  }

  /** Sections shown somewhere other than the grid/wide band: Discover's
   * Beacons tab (0.75.15) and the Scans list, which lives in the BAND SCANS
   * workspace above the report. They stay in the section tables for
   * layout/settings parity (format.js mirrors msdr/settings.py) but never
   * render as a grid card. */
  _renderedElsewhere(id) {
    return this.workspace === "discover" && (id === "beacons" || id === "scans");
  }

  /** True when `id` is rendered by the primary spectrum slot (see render())
   * for the active workspace, so the band/grid must skip it -- rendering it
   * twice would create a second <survey-view> instance. S3: REVIEW also gets
   * the primary slot, but only while a replay is actually running --
   * "survey" isn't a WORKSPACE_SECTIONS member of "review" at all, so this
   * never double-renders it elsewhere once a replay stops. */
  _inPrimarySlot(id) {
    if (id !== "survey") return false;
    if (this.workspace === "review") return !!this.state?.survey?.replay;
    return PRIMARY_SPECTRUM_WORKSPACES.has(this.workspace);
  }

  _showPrimarySpectrum() {
    if (!sectionVisible("survey", this.features)) return false;
    if (this.workspace === "review") return !!this.state?.survey?.replay;
    return PRIMARY_SPECTRUM_WORKSPACES.has(this.workspace);
  }

  /** layout.wide ids, in order, filtered to visible (feature-on, active
   * workspace) sections -- the wide band's contents (see _renderWideBand). */
  _visibleWideIds() {
    const wideIds = this.layout?.wide ?? DEFAULT_WORKSPACE_WIDE[this.workspace] ?? [];
    const members = this._workspaceMembers();
    return wideIds.filter(
      (id) =>
        members.includes(id) &&
        !(this._usesListenWorkbench() && LISTEN_WORKBENCH_SECTIONS.has(id)) &&
        sectionVisible(id, this.features) &&
        !this._inPrimarySlot(id) &&
        !this._renderedElsewhere(id)
    );
  }

  async _updatePassSettings(patch) {
    if (this._satelliteSettingsBusy) return;
    this._satelliteSettingsBusy = true;
    try {
      this.settings = await api.patchSettings(patch);
      this.selectedPass = null;
      await this._loadPasses();
      this._clearFailure("passes");
    } catch (err) {
      this._apiError("passes", err.message, "Save pass filters");
    } finally {
      this._satelliteSettingsBusy = false;
    }
  }

  _defaultPassSelection() {
    return [...(this.passes?.passes ?? [])]
      .filter(p => Date.parse(p.rise) > Date.now())
      .sort((a, b) => Date.parse(a.rise) - Date.parse(b.rise))[0] ?? null;
  }

  /** Discover tabs that use the SDR receiver. Wi-Fi, BLE, Remote ID, Timeline
   * and Watch rules don't, so the SDR-only panels (latest decoded output,
   * Channel watch) stay off them. */
  _discoverSdrView() {
    return ["spectrum", "inbox", "table"].includes(this.discoverView);
  }

  _renderSatellites() {
    return html`<div class="satellite-workspace">
      <div>${this.features.passes ? this._sectionNode("passes") : html`<div class="panel">Enable PASSES in Setup → Features.</div>`}</div>
      <div>${this.features.sky ? this._sectionNode("sky") : html`<div class="panel"><h2>Pass charts</h2><p>Enable SKY to inspect the selected pass.</p><button @click=${() => (this.setupOpen = true)}>Setup</button></div>`}</div>
    </div>`;
  }

  _sectionNode(id) {
    switch (id) {
      case "survey":
        return html`<survey-view
          .mode=${this.workspace}
          .receiverHz=${this.state?.props?.device_vfo_frequency ?? null}
          .receiverSampleRate=${this.state?.props?.device_sample_rate ?? null}
          .status=${this.state?.survey ?? null}
          .frame=${this.spectrumFrame}
          .ring=${this.spectrumRing}
          .connected=${!!this.state?.connected}
          .canControl=${!!this.state?.can_control && !this.wsDown}
          .decoders=${this.decoders}
          .savedSignals=${this.signals}
          .favorites=${this.favorites}
          .failure=${this._failureFor("survey")}
          .wfSettings=${this.settings?.survey ?? null}
          .bandScanDraft=${this._bandScanDraft}
          @band-scan-draft-change=${(ev) => this._updateBandScanDraft(ev.detail)}
          .peakBusy=${!!this._surveyPeakBusy}
          @survey-start=${() => this._doSurveyStart()}
          @survey-stop=${() => this._doSurveyStop()}
          @survey-clear=${() => this._doSurveyClear()}
          @survey-span=${(ev) => this._doSurveySpan(ev.detail)}
          @survey-peak-threshold=${(ev) => this._doSurveyPeakThreshold(ev.detail.db)}
          @survey-marker-settings=${(ev) => this._doSurveyMarkerSettings(ev.detail)}
          @survey-replay=${(ev) => this._doSurveyReplay(ev.detail)}
          @survey-replay-control=${(ev) => this._doSurveyReplayControl(ev.detail)}
          @survey-replay-stop=${() => this._doSurveyReplayStop()}
          @survey-dimensions=${(ev) => this._doSurveyDimensions(ev.detail.dimensions)}
          @survey-decode=${(ev) => this._doSurveyDecode(ev.detail)}
          @survey-undecode=${(ev) => this._doSurveyUndecode(ev.detail.frequency_hz)}
          @survey-save=${(ev) => this._doSurveySave(ev.detail.frequency_hz)}
          @open-band-scan-report=${(ev) => this._openBandScanReport(ev.detail.id)}
          @survey-open-messages=${(ev) => this._doOpenSurveyMessages(ev.detail.frequency_hz)}
          @tune-frequency=${(ev) =>
            this.state?.survey?.replay
              ? this._doReviewTune(ev.detail.hz)
              : this._doTuneFrequency(ev.detail.hz, undefined, undefined, "survey")}
          @dismiss-failure=${() => this._clearFailure("survey")}
        ></survey-view>`;
      // Build scanner controls once for the receiver's operations zone.
      case "scan": {
        const s = this.state;
        const props = s?.props ?? {};
        const connected = !!s?.connected;
        return html`<scanner-deck
          .scan=${this.scan}
          .connected=${connected}
          .started=${!(connected && !(s?.started ?? true))}
          .canControl=${!!s?.can_control}
          .centerHz=${props.device_center_frequency ?? null}
          .sampleRateHz=${props.device_sample_rate ?? null}
          .vfoHz=${props.device_vfo_frequency ?? null}
          .failure=${this._failureFor("scanner")}
          @scan-start=${(ev) => this._doScanStart(ev.detail)}
          @scan-pause=${() => this._doScanPause()}
          @scan-resume=${() => this._doScanResume()}
          @scan-stop=${() => this._doScanStop()}
          @scan-hold=${() => this._doScanHold()}
          @scan-skip=${(ev) => this._doScanSkip(ev.detail.persist)}
          @tune-frequency=${(ev) =>
            this._doTuneFrequency(ev.detail.hz, ev.detail.demodulator, ev.detail.filter_bandwidth_hz, "scanner")}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "scanner")}
          @star-hit=${(ev) => this._doStarHit(ev.detail.hit)}
          @dismiss-failure=${() => this._clearFailure("scanner")}
        ></scanner-deck>`;
      }
      case "decode": {
        const s = this.state;
        const connected = !!s?.connected;
        return html`<decode-deck
          .frequencyHz=${s?.props?.device_vfo_frequency}
          .status=${s?.decoder ?? null}
          .decoders=${this.decoders}
          .receiverProps=${s?.props ?? null}
          .lastEvent=${this.decodeEvents[0] ?? null}
          .decodeEvents=${this.decodeEvents}
          .connected=${connected}
          .started=${!(connected && !(s?.started ?? true))}
          .canControl=${!!s?.can_control}
          .failure=${this._failureFor("decoder")}
          @decode-start=${(ev) => this._doDecoderStart(ev.detail.decoders, ev.detail.frequency_hz)}
          @decode-stop=${() => this._doDecoderStop()}
          @band-start=${(ev) => this._doBandStart(ev.detail)}
          @band-stop=${() => this._doBandStop()}
          @band-save=${(ev) => this._doBandSave(ev.detail.frequency_hz)}
          @install-presets=${() => this._doInstallPresets()}
          @open-decode-view=${() => (this.decodeViewOpen = true)}
          @dismiss-failure=${() => this._clearFailure("decoder")}
        ></decode-deck>`;
      }
      // Replay goes through the same REST calls (and "survey" failure scope)
      // as survey-view's own REPLAY picker, so both surfaces stay in sync.
      case "review":
        return html`<review-view
          .replay=${this.state?.survey?.replay ?? null}
          .recorderStatus=${this.recorder}
          .tz=${this.state?.timezone ?? null}
          .failure=${this._failureFor("survey")}
          .refreshAt=${this._reviewRefreshAt}
          @survey-replay=${(ev) => this._doSurveyReplay(ev.detail)}
          @survey-replay-control=${(ev) => this._doSurveyReplayControl(ev.detail)}
          @survey-replay-stop=${() => this._doSurveyReplayStop()}
          @dismiss-failure=${() => this._clearFailure("survey")}
          @wifi-inspect=${(ev) => this._openWifiInspect(ev.detail.id)}
        ></review-view>`;
      case "library":
        return html`<library-view
          .tags=${this.tags}
          .tagsById=${this.tagsById}
          .failure=${this._failureFor("library")}
          .refreshAt=${this._libraryRefreshAt}
          .profiles=${this.profiles}
          @open-profile=${(ev) => this._openProfile(ev.detail.id)}
          @apply-profile=${(ev) => this._doApplyProfile(ev.detail.id)}
          @refresh=${() => this._loadProfiles()}
          @select-signal=${(ev) => (this.selected = ev.detail)}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "library")}
          @capture=${() => this._doCapture()}
          @action=${(ev) => this._action(ev.detail.message)}
          @api-error=${(ev) => this._apiError("library", ev.detail.message)}
          @dismiss-failure=${() => this._clearFailure("library")}
        ></library-view>`;
      case "nearby":
        return html`<nearby-list
          .signals=${this.state?.nearby ?? []}
          .vfoHz=${this.state?.props?.device_vfo_frequency ?? null}
          .tagsById=${this.tagsById}
          .failure=${this._failureFor("nearby")}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "nearby")}
          @dismiss-failure=${() => this._clearFailure("nearby")}
        ></nearby-list>`;
      case "inbox":
        return html`<inbox-list
          .signals=${this.inbox}
          .tagsById=${this.tagsById}
          .identFeature=${this.features.ident}
          .failure=${this._failureFor("inbox")}
          @select=${(ev) => (this.selected = ev.detail)}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "inbox")}
          @dismiss-failure=${() => this._clearFailure("inbox")}
        ></inbox-list>`;
      case "recent":
        return html`<recent-list
          .signals=${this.recent}
          .tagsById=${this.tagsById}
          .identFeature=${this.features.ident}
          .failure=${this._failureFor("recent")}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "recent")}
          @select=${(ev) => (this.selected = ev.detail)}
          @dismiss-failure=${() => this._clearFailure("recent")}
        ></recent-list>`;
      case "listen":
        return html`<listen-view
          .vfoHz=${this.state?.props?.device_vfo_frequency ?? null}
          .catalogFeature=${this.features.catalog}
          .settingsSavedAt=${this.settingsSavedAt}
          .tz=${this.state?.timezone ?? null}
          .failure=${this._failureFor("listen")}
          @tune-catalog=${(ev) => this._doTuneCatalog(ev.detail.id, ev.detail.label, "listen")}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "listen")}
          @tune-frequency=${(ev) =>
            this._doTuneFrequency(
              ev.detail.hz,
              ev.detail.demodulator,
              ev.detail.filter_bandwidth_hz,
              "listen"
            )}
          @star-catalog=${(ev) => this._doStarCatalog(ev.detail.id, ev.detail.label, "listen")}
          @dismiss-failure=${() => this._clearFailure("listen")}
        ></listen-view>`;
      case "log":
        return html`<journal-view
          .entries=${this.journal}
          .day=${this.journalDay}
          .days=${this.journalDays}
          .tagsById=${this.tagsById}
          @prev-day=${() => this._shiftDay(-1)}
          @next-day=${() => this._shiftDay(1)}
          @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id)}
          @tune-frequency=${(ev) => this._doTuneFrequency(ev.detail.hz)}
        ></journal-view>`;
      case "passes":
        return html`<passes-view
          .settings=${this.settings}
          .busy=${!!this._satelliteSettingsBusy}
          .satelliteDefaults=${[...new Set((this.signals ?? []).filter(s => s.satellite).map(s => s.satellite))]}
          @passes-settings=${ev => this._updatePassSettings(ev.detail)}
          .selectedPass=${this.selectedPass ?? this._defaultPassSelection()}
          .visibleOnly=${!!this.settings?.passes_visible_only}
          .passes=${this.passes}
          .hasLocation=${this.state?.location_ready ?? !!this.settings?.location}
          .failure=${this._failureFor("passes")}
          @tune-satellite=${(ev) => this._doTuneSatellite(ev.detail.satellite, ev.detail.downlink)}
          @refresh-passes=${() => this._doRefreshPasses()}
          @select-pass=${(ev) => this._onSelectPass(ev.detail)}
          @dismiss-failure=${() => this._clearFailure("passes")}
          @setup=${() => (this.setupOpen = true)}
        ></passes-view>`;
      case "sky":
        return html`<sky-view
          .passes=${this.passes}
          .signals=${this.signals}
          .selectedPass=${this.selectedPass}
          .tracking=${this.state?.tracking ?? null}
          .connected=${!!this.state?.connected}
          .started=${this.state?.started ?? true}
          .hasLocation=${this.state?.location_ready ?? !!this.settings?.location}
          .trackingFeature=${this.features.tracking}
          @select-pass=${(ev) => this._onSelectPass(ev.detail)}
          @refresh=${() => this._loadSignals()}
          @track-toggle=${(ev) => this._doTrackToggle(ev.detail.active, ev.detail.id, "sky")}
          @api-error=${(ev) => this._status(ev.detail.message)}
          @setup=${() => (this.setupOpen = true)}
        ></sky-view>`;
      case "scans":
        return html`<scans-view
          .scans=${this.scans}
          @tune-frequency=${(ev) => this._doTuneFrequency(ev.detail.hz)}
          @star-event=${(ev) => this._doStarEvent(ev.detail.scan, ev.detail.event)}
          @scan-completed=${() => this._loadScans()}
          @api-error=${(ev) => this._status(ev.detail.message)}
          @setup=${() => (this.setupOpen = true)}
        ></scans-view>`;
      case "catalog":
        return html`<catalog-view
          .languages=${this.settings?.catalog_languages ?? []}
          .tz=${this.state?.timezone ?? null}
          .failure=${this._failureFor("catalog")}
          @tune-catalog=${(ev) => this._doTuneCatalog(ev.detail.id, ev.detail.label, "catalog")}
          @star-catalog=${(ev) => this._doStarCatalog(ev.detail.id, ev.detail.label, "catalog")}
          @dismiss-failure=${() => this._clearFailure("catalog")}
          @catalog-setup=${() => {
            this.setupOpen = true;
            this.updateComplete.then(() => this.querySelector("setup-drawer")?.focusCatalog());
          }}
        ></catalog-view>`;
      case "allocations":
        return html`<allocation-chart
          .tunedHz=${this.state?.props?.device_vfo_frequency ?? null}
          .inspectHz=${this._allocationInspectHz}
          jurisdiction=${this.settings?.allocation_jurisdiction ?? "US"}
        ></allocation-chart>`;
      case "beacons":
        return html`<beacons-view
          .status=${this.state?.beacons ?? null}
          .ndbConfigured=${(this.settings?.beacons?.ndb_frequencies_hz ?? []).length > 0}
          .connected=${!!this.state?.connected}
          .analyzerActive=${!!this.state?.survey?.analyzer?.active}
          .bandDecodeActive=${!!this.state?.decoder?.band?.active}
          .recording=${["recording", "finalizing"].includes(this.recorder?.state)}
          .failure=${this._failureFor("beacons")}
          @beacons-start=${(ev) => this._doBeaconsStart(ev.detail.mode)}
          @beacons-stop=${() => this._doBeaconsStop()}
          @action=${(ev) => this._action(ev.detail.message)}
          @api-error=${(ev) => this._apiError("beacons", ev.detail.message)}
          @dismiss-failure=${() => this._clearFailure("beacons")}
        ></beacons-view>`;
      case "finds":
        return html`<finds-view
          .lastFind=${this._lastFind}
          .failure=${this._failureFor("finds")}
          @action=${(ev) => this._action(ev.detail.message)}
          @api-error=${(ev) => this._apiError("finds", ev.detail.message)}
          @dismiss-failure=${() => this._clearFailure("finds")}
        ></finds-view>`;
      default:
        return "";
    }
  }

  _usesListenWorkbench() {
    return this.workspace === "listen" ||
      (this.workspace === "discover" && this.discoverView === "spectrum");
  }

  _surveyFirst() {
    // Editing auxiliary cards must not replace the approved primary workbench.
    return this._usesListenWorkbench() && this._showPrimarySpectrum();
  }

  _listenWorkbench() {
    const state = this.state ?? {};
    return html`<listen-workbench
      .nearby=${state.nearby ?? []}
      .recent=${this.recent}
      .favorites=${this.favorites}
      .vfoHz=${state.props?.device_vfo_frequency ?? null}
      .tagsById=${this.tagsById}
      .catalogFeature=${this.features.catalog}
      .identFeature=${this.features.ident}
      .settingsSavedAt=${this.settingsSavedAt}
      .tz=${state.timezone ?? null}
      .nearbyFailure=${this._failureFor("nearby")}
      .recentFailure=${this._failureFor("recent")}
      .suggestionsFailure=${this._failureFor("listen")}
      .journal=${this.journal}
      .journalDay=${this.journalDay}
      .journalDays=${this.journalDays}
      .decoderFeature=${this.features.decoder}
      .decodeStatus=${state.decoder ?? null}
      .decoders=${this.decoders}
      .receiverProps=${state.props ?? null}
      .lastDecodeEvent=${this.decodeEvents[0] ?? null}
      .decodeEvents=${this.decodeEvents}
      .connected=${!!state.connected}
      .started=${!(state.connected && !(state.started ?? true))}
      .canControl=${!!state.can_control && !state.survey?.replay}
      .decodeFailure=${this._failureFor("decoder")}
      .receiver=${{ connected: !!state.connected, started: !!state.started, complete: !!state.complete }}
      .recorderArmed=${!!this.recorder?.armed}
      @open-instruments=${() => {
        const scope = this.querySelector("signal-instruments");
        scope?.expand();
        scope?.scrollIntoView({ behavior: "smooth", block: "start" });
      }}
      @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "listen")}
      @tune-frequency=${(ev) =>
        this._doTuneFrequency(ev.detail.hz, ev.detail.demodulator, ev.detail.filter_bandwidth_hz, "listen")}
      @tune-catalog=${(ev) => this._doTuneCatalog(ev.detail.id, ev.detail.label, "listen")}
      @star-catalog=${(ev) => this._doStarCatalog(ev.detail.id, ev.detail.label, "listen")}
      @select-signal=${(ev) => (this.selected = ev.detail)}
      @api-error=${(ev) => this._apiError("listen", ev.detail.message)}
      @prev-day=${() => this._shiftDay(-1)}
      @next-day=${() => this._shiftDay(1)}
      @dismiss-nearby-failure=${() => this._clearFailure("nearby")}
      @dismiss-recent-failure=${() => this._clearFailure("recent")}
      @favorites-changed=${() => this._loadFavorites()}
      @dismiss-suggestions-failure=${() => this._clearFailure("listen")}
      @decode-start=${(ev) => this._doDecoderStart(ev.detail.decoders, ev.detail.frequency_hz)}
      @decode-stop=${() => this._doDecoderStop()}
      @band-start=${(ev) => this._doBandStart(ev.detail)}
      @band-stop=${() => this._doBandStop()}
      @band-save=${(ev) => this._doBandSave(ev.detail.frequency_hz)}
      @install-presets=${() => this._doInstallPresets()}
      @open-decode-view=${() => (this.decodeViewOpen = true)}
      @dismiss-decode-failure=${() => this._clearFailure("decoder")}
    ></listen-workbench>`;
  }

  /** The one place a section's element is wrapped for reordering: an
   * absolutely-positioned grip laid over the section's own existing markup's
   * title row (unchanged, including whatever collapse toggle it already
   * has) -- see .section-shell/.section-shell .grip in styles.css. The grip
   * is a real button (draggable="true" for mouse/touch drag, ArrowUp/Down/
   * Left/Right for keyboard -- see _onGripKeydown) rather than a plain span,
   * so it's reachable and operable without a pointer.
   *
   * `colIndex` is null for a wide-band section (see _renderWideBand): it has
   * no column position to drag within, so it gets the ⇥ CARD toggle only (no
   * grip). A grid section gets the grip plus a ⇔ WIDE toggle next to it. */
  _sectionShell(id, colIndex) {
    const isWide = colIndex === null;
    // S1: grips and ⇔ WIDE / ⇥ CARD toggles only while EDIT LAYOUT is on
    // (setup-drawer's layout-edit event) -- otherwise they're clutter on
    // every card for an action taken once in a while.
    if (!this._layoutEditMode) {
      return html`<div class="section-shell" data-section-id=${id}>${this._sectionNode(id)}</div>`;
    }
    return html`
      <div
        class="section-shell editing ${isWide ? "" : "has-grip"} ${this._dragId === id ? "dragging" : ""}"
        data-section-id=${id}
        @dragover=${isWide ? () => {} : (ev) => this._onShellDragOver(ev, colIndex, id)}
      >
        ${isWide
          ? ""
          : html`<button
              class="grip mono"
              draggable="true"
              aria-label="move ${SECTION_LABELS[id] ?? id}"
              title="drag ${SECTION_LABELS[id] ?? id} to reorder"
              @dragstart=${(ev) => this._onGripDragStart(ev, id)}
              @dragend=${() => this._onDragEnd()}
              @keydown=${(ev) => this._onGripKeydown(ev, id, colIndex)}
              @focus=${() => (this._focusedGripId = id)}
              @blur=${() => this._onGripBlur()}
            >⋮⋮</button>`}
        <button
          class="wide-toggle mono"
          aria-label="show ${SECTION_LABELS[id] ?? id} as ${isWide ? "a card" : "a wide row"}"
          title="${isWide ? "⇥ show as card" : "⇔ show as wide row"}"
          @click=${() => this._toggleWide(id)}
        >${isWide ? "⇥ CARD" : "⇔ WIDE"}</button>
        ${this._sectionNode(id)}
      </div>
    `;
  }

  /** Sections flagged WIDE (layout.wide) render full-width, in wide order,
   * above the 3-column grid -- see .wide-band in styles.css. */
  _renderWideBand() {
    const ids = this._visibleWideIds();
    if (!ids.length) return "";
    return html`<div class="wide-band">${ids.map((id) => this._sectionShell(id, null))}</div>`;
  }

  _renderColumn(colIndex) {
    const ids = this._visibleIdsForColumn(colIndex);
    const drop = this._dropTarget;
    const nodes = [];
    for (const id of ids) {
      if (drop && drop.col === colIndex && drop.beforeId === id) {
        nodes.push(html`<div class="drop-indicator"></div>`);
      }
      nodes.push(this._sectionShell(id, colIndex));
    }
    if (drop && drop.col === colIndex && drop.beforeId === null) {
      nodes.push(html`<div class="drop-indicator"></div>`);
    }
    return html`
      <div
        class="section-shell-col"
        data-col-index=${colIndex}
        @dragover=${(ev) => this._onColumnDragOver(ev, colIndex)}
        @drop=${(ev) => this._onColumnDrop(ev, colIndex)}
      >
        ${nodes}
      </div>
    `;
  }

  _onGripDragStart(ev, id) {
    this._dragId = id;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", id);
  }

  _onDragEnd() {
    this._dragId = null;
    this._dropTarget = null;
  }

  /** Fires while dragging over a section-shell: picks before/after this
   * section from which half of its rect the pointer is in, then stops the
   * event so the column's own dragover (empty-space case) doesn't also fire. */
  _onShellDragOver(ev, colIndex, id) {
    if (!this._dragId) return;
    ev.preventDefault();
    ev.stopPropagation();
    const rect = ev.currentTarget.getBoundingClientRect();
    const before = ev.clientY < rect.top + rect.height / 2;
    let beforeId;
    if (before) {
      beforeId = id;
    } else {
      const ids = this._visibleIdsForColumn(colIndex);
      beforeId = ids[ids.indexOf(id) + 1] ?? null;
    }
    if (this._dropTarget?.col === colIndex && this._dropTarget?.beforeId === beforeId) return;
    this._dropTarget = { col: colIndex, beforeId };
  }

  /** Fires over empty column space (below the last shell, or an empty
   * column) -- any shell's own dragover already claimed the event otherwise. */
  _onColumnDragOver(ev, colIndex) {
    if (!this._dragId) return;
    ev.preventDefault();
    if (this._dropTarget?.col === colIndex && this._dropTarget?.beforeId === null) return;
    this._dropTarget = { col: colIndex, beforeId: null };
  }

  _onColumnDrop(ev, colIndex) {
    ev.preventDefault();
    const id = this._dragId;
    const target = this._dropTarget;
    this._dragId = null;
    this._dropTarget = null;
    if (!id || !target || target.col !== colIndex || target.beforeId === id) return;
    this._moveSection(id, target.col, target.beforeId);
  }

  _moveSection(id, toCol, beforeId) {
    const columns = (
      this.layout?.columns ?? DEFAULT_WORKSPACE_COLUMNS[this.workspace] ?? []
    ).map((c) => [...c]);
    const fromCol = columns.findIndex((c) => c.includes(id));
    if (fromCol === -1) return;
    columns[fromCol] = columns[fromCol].filter((x) => x !== id);
    const target = columns[toCol];
    const insertAt = beforeId != null ? target.indexOf(beforeId) : -1;
    target.splice(insertAt === -1 ? target.length : insertAt, 0, id);
    this._setLayout(columns);
  }

  /** Optimistic re-render, then a debounced PATCH so a drag (or a run of
   * SETUP's LAYOUT ▲▼◀▶ clicks) doesn't spam /api/settings. Only touches
   * columns -- current layout.wide (server value or a not-yet-saved local
   * toggle) is kept as-is. */
  _setLayout(columns) {
    this.layout = { ...this.layout, columns };
    this._layoutUnsaved = true;
    this._layoutEdit = (this._layoutEdit ?? 0) + 1;
    clearTimeout(this._layoutSaveTimer);
    this._layoutSaveTimer = setTimeout(() => this._saveLayout(), LAYOUT_SAVE_MS);
  }

  /** ⇔ WIDE / ⇥ CARD toggle (see _sectionShell): moves `id` between
   * layout.wide and the grid. Immediate PATCH -- like a preset, this is a
   * single deliberate click, not a run of drags -- and, per the WIDE/CARD
   * contract, only sends `wide`; layout.columns (which still keeps `id`'s
   * slot) is untouched. */
  _toggleWide(id) {
    const current = this.layout?.wide ?? DEFAULT_WORKSPACE_WIDE[this.workspace] ?? [];
    const wide = current.includes(id) ? current.filter((x) => x !== id) : [...current, id];
    this.layout = { ...this.layout, wide };
    this._layoutUnsaved = true;
    this._layoutEdit = (this._layoutEdit ?? 0) + 1;
    this._saveWide(wide);
  }

  /** LAYOUT ▾ menu (instrument-panel.js), setup-drawer.js's preset buttons,
   * and the command palette's "layout: …" actions all funnel here -- an
   * immediate PATCH is fine for a preset (unlike a drag, it's a single,
   * deliberate action, not a run of clicks). */
  /** The checklist lives at the top of the section grid, so leave the
   * Library/SETUP first; then ask the component to show itself. */
  async _showGettingStarted() {
    this.setupOpen = false;
    await this.updateComplete;
    this.querySelector("getting-started")?.show();
    window.scrollTo({ top: 0 });
  }

  _applyLayoutPreset(key) {
    const preset = LAYOUT_PRESETS[key];
    if (!preset) return;
    this._setLayout(preset.columns.map((c) => [...c]));
  }

  /** Keyboard alternative to dragging a grip: ArrowUp/Down swaps with the
   * visible neighbor within the column, ArrowLeft/Right moves to the
   * adjacent column (same index, or the end if the target column is
   * shorter). Both go through _moveSection/_setLayout, same as a drop. */
  _onGripKeydown(ev, id, colIndex) {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)) return;
    ev.preventDefault();
    const col = this._visibleIdsForColumn(colIndex);
    const idx = col.indexOf(id);
    if (idx === -1) return;
    this._focusedGripId = id;
    if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
      const delta = ev.key === "ArrowUp" ? -1 : 1;
      const neighborIdx = idx + delta;
      if (neighborIdx < 0 || neighborIdx >= col.length) return;
      const beforeId = delta === -1 ? col[neighborIdx] : (col[neighborIdx + 1] ?? null);
      this._moveSection(id, colIndex, beforeId);
      this._announceMove(id, colIndex, neighborIdx);
    } else {
      const toCol = colIndex + (ev.key === "ArrowLeft" ? -1 : 1);
      if (toCol < 0 || toCol > 2) return;
      const target = this._visibleIdsForColumn(toCol);
      const insertAt = Math.min(idx, target.length);
      const beforeId = insertAt < target.length ? target[insertAt] : null;
      this._moveSection(id, toCol, beforeId);
      this._announceMove(id, toCol, insertAt);
    }
  }

  /** Text for the visually-hidden aria-live region -- both indices are
   * shown 1-based to match the on-screen COLUMN N labels. */
  _announceMove(id, colIndex, idx) {
    this._layoutAnnouncement = `${SECTION_LABELS[id] ?? id} moved to column ${colIndex + 1}, position ${idx + 1}`;
  }

  async _saveLayout() {
    // Marks the debounce window as closed *before* awaiting -- _onPageHide
    // only needs to step in while this timer is still pending.
    this._layoutSaveTimer = null;
    const edit = this._layoutEdit;
    const ws = this.workspace;
    try {
      // U2: a preset/drag/grip-move edits the *active* workspace's own entry
      // -- see format.js's WORKSPACE_SECTIONS and Settings.workspaces
      // (msdr/settings.py). Cache the response so a later _switchWorkspace
      // (before the next full _loadSettings) sees this edit.
      this.settings = await api.patchSettings({ workspaces: { [ws]: { columns: this.layout.columns } } });
      // Only clean if no newer edit arrived while this PATCH was in flight.
      if (edit === this._layoutEdit) this._layoutUnsaved = false;
      this._action("layout saved");
    } catch (err) {
      this._status(err.message);
    }
  }

  /** Immediate PATCH for a WIDE/CARD toggle (see _toggleWide) -- sends only
   * `wide`, same shape as _saveLayout sending only `columns`. */
  async _saveWide(wide) {
    const edit = this._layoutEdit;
    const ws = this.workspace;
    try {
      this.settings = await api.patchSettings({ workspaces: { [ws]: { wide } } });
      if (edit === this._layoutEdit) this._layoutUnsaved = false;
      this._action("layout saved");
    } catch (err) {
      this._status(err.message);
    }
  }

  /** LISTEN/DISCOVER/REVIEW/LIBRARY/SATELLITES tab strip (see render(), directly
   * under the instrument panel) -- switches which workspace's Layout the
   * grid renders/edits, PATCHing the change so it's remembered server-side
   * (see Settings.workspace, msdr/settings.py). A pending debounced column
   * drag for the *old* workspace is flushed first so it isn't lost. */
  _updateBandScanDraft(patch) {
    this._bandScanDraft = { ...this._bandScanDraft, ...patch };
  }

  async _startBandScanFromReceiver() {
    await this._openBandScanControls();
    const view = this.querySelector("survey-view");
    if (!view) return;
    await view._startBandScan();
    if (view._bandScanError) return;
    this._bandScanStatus = view._bandScan;
    const details = view.querySelector("details.survey-band-scan");
    if (!details) return;
    details.classList.remove("band-scan-start-glow");
    void details.offsetWidth;
    details.classList.add("band-scan-start-glow");
    details.querySelector("summary")?.focus();
    setTimeout(() => details.classList.remove("band-scan-start-glow"), 2200);
  }

  async _openBandScanControls() {
    await this._switchWorkspace("discover");
    this.discoverView = "spectrum";
    await this.updateComplete;
    const view = this.querySelector("survey-view");
    await view?.updateComplete;
    const details = view?.querySelector("details.survey-band-scan");
    if (details) {
      details.open = true;
      details.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  async _openBandScanReport(id) {
    this._reportScanId = id;
    await this._switchWorkspace("band_scans");
  }

  /** Status-strip lamps (Wi-Fi/BLE receivers, SURVEY owner): jump to that
   * Discover tab. Wi-Fi opens the live receiver, not a previously inspected
   * saved session. */
  async _openDiscoverView(view) {
    if (view === "wifi") this._wifiInspectSource = null;
    await this._switchWorkspace("discover");
    this.discoverView = view;
  }

  /** review-view's "Inspect" (wifi-sessions-panel.js): open Discover's Wi-Fi
   * tab scoped to that saved session instead of the live receiver. */
  async _openWifiInspect(id) {
    this._wifiInspectSource = `session:${id}`;
    await this._switchWorkspace("discover");
    this.discoverView = "wifi";
  }

  /** wifi-view.js's "Open Setup" empty-state link -- same focus pattern as
   * the CATALOG "Manage sources" shortcut (setup-drawer.js's focusCatalog). */
  _openWifiSetup() {
    this.setupOpen = true;
    this.updateComplete.then(() => this.querySelector("setup-drawer")?.focusWifi());
  }

  /** ble-view.js's "Open Setup" empty-state link -- same focus pattern as
   * _openWifiSetup above. */
  _openBleSetup() {
    this.setupOpen = true;
    this.updateComplete.then(() => this.querySelector("setup-drawer")?.focusBle());
  }

  /** DECODE chip target: the deck lives in the Listen panel, so leave
   * workspaces without it, then bring the deck into view. */
  async _openDecode() {
    if (!this._usesListenWorkbench()) await this._switchWorkspace("listen");
    await this.updateComplete;
    this.querySelector(".listen-decode-controls")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async _switchWorkspace(id) {
    if (!WORKSPACE_IDS.includes(id) || id === this.workspace) return;
    this._workspaceChosen = true;
    if (this._layoutSaveTimer != null) {
      clearTimeout(this._layoutSaveTimer);
      this._layoutSaveTimer = null;
      await this._saveLayout();
    }
    this.workspace = id;
    // Re-list recordings on every visit so one finished meanwhile shows up.
    if (id === "review") this._reviewRefreshAt = Date.now();
    this.layout = resolveWorkspaceLayout(this.settings?.workspaces, id);
    try {
      this.settings = await api.patchSettings({ workspace: id });
    } catch (err) {
      this._status(err.message);
    }
  }

  /** open-review {name, path} (recordings-view's ▶ REVIEW, the strip's
   * source chip): go to REVIEW, and start replaying only when a path is
   * given -- the source chip just navigates to the replay already running. */
  async _openReview(detail) {
    this.recordingsOpen = false;
    await this._switchWorkspace("review");
    if (detail?.path) await this._doSurveyReplay({ path: detail.path, loop: false });
  }

  /** SETUP's VALIDATION group (see updated()) -- a fetch failure just leaves
   * the group showing its own "loading…"/error state rather than a
   * failure-note over the whole drawer. */
  async _loadValidation() {
    try {
      this.validation = await api.getValidation();
    } catch (err) {
      this._status(err.message);
    }
  }

  /** layout-edit {on} from setup-drawer's EDIT LAYOUT checkbox. */
  _setLayoutEdit(on) {
    this._layoutEditMode = !!on;
    writeLocal("msdr.layoutEdit", this._layoutEditMode ? "1" : "0");
  }

  // --- keyboard ------------------------------------------------------------

  /** Closes only the topmost overlay/view -- palette and drawer/setup are
   * true overlays over everything else, Library is a full-view swap under
   * them, so it must not also close when e.g. the palette was opened on top
   * of it. */
  _onKeydown(ev) {
    if (ev.defaultPrevented) return;
    if (ev.key === "Escape") {
      // Scanner setup is a modal nested inside instrument-panel (not
      // top-level state app.js already tracks), so it's reached the same
      // way focusFrequencyEdit/toggleRecording are -- see scanner-deck.js.
      const scannerDeck = this.querySelector("scanner-deck");
      const decodeDeck = this.querySelector("decode-deck");
      if (scannerDeck?.setupOpen) scannerDeck.closeSetup();
      else if (decodeDeck?.setupOpen) decodeDeck.closeSetup();
      else if (this.diagOpen) this.diagOpen = false;
      else if (this.paletteOpen) this.paletteOpen = false;
      else if (this.setupOpen) this.setupOpen = false;
      else if (this.decodeViewOpen) this.decodeViewOpen = false;
      else if (this._signalTarget) this._signalTarget = null;
      else if (this._freqDetailsOpen) this._closeFrequencyDetails();
      else if (this.selected) this.selected = null;
      return;
    }

    // Modal controls own keyboard interaction; never tune or record behind them.
    if (this.paletteOpen || this.setupOpen || this.decodeViewOpen ||
        this.selected || this.querySelector('[aria-modal="true"], dialog[open]')) return;
    const mod = ev.metaKey || ev.ctrlKey;
    // Cmd/Ctrl-Z: allowed while typing would undo a bulk library op from
    // inside a text field, which reads as "undo my typing" -- skip it there,
    // same as every other shortcut below.
    if (mod && !isTyping() && ev.key.toLowerCase() === "z") {
      ev.preventDefault();
      if (ev.shiftKey) this._doRedo();
      else this._doUndo();
      return;
    }
    if (isTyping()) return;

    if (mod && ev.key === "k") {
      ev.preventDefault();
      this.paletteOpen = true;
    } else if (!mod && ev.key === ",") {
      ev.preventDefault();
      this.setupOpen = true;
    } else if (!mod && ev.key === "l") {
      ev.preventDefault();
      this._switchWorkspace("library");
    } else if (mod && ev.key === "[") {
      ev.preventDefault();
      this._doBack();
    } else if (mod && ev.key === "]") {
      ev.preventDefault();
      this._doForward();
    } else if (!mod && ev.key === "s") {
      ev.preventDefault();
      this._doCapture();
    } else if (!mod && ev.key === "e") {
      ev.preventDefault();
      if (this.lastTuned) this.selected = this.lastTuned;
    } else if (!mod && ev.key === "i") {
      ev.preventDefault();
      if (this.features.ident && this.lastTuned) this.selected = { ...this.lastTuned, identify: true };
    } else if (!mod && ev.key === "f") {
      ev.preventDefault();
      if (this.state?.survey?.replay) return;
      this.querySelector("instrument-panel")?.focusFrequencyEdit();
    } else if (!mod && ev.key === "r" && this.features.recorder) {
      ev.preventDefault();
      this.querySelector("instrument-panel")?.toggleRecording();
    } else if (!mod && (ev.key === "ArrowLeft" || ev.key === "ArrowRight") && !this._blocksGlobalStep(ev)) {
      ev.preventDefault();
      // Global step keys target SDRconnect; while replay is selected they
      // must not retune the live receiver behind the REPLAY source label.
      if (this.state?.survey?.replay) return;
      const multiplier = ev.shiftKey ? 10 : ev.altKey ? 0.1 : 1;
      this._doStepFrequency(ev.key === "ArrowRight" ? 1 : -1, multiplier);
    } else if (!mod && ["1", "2", "3", "4", "5", "6", "7", "8"].includes(ev.key)) {
      // Workspace switch (including BAND SCANS between MAP and REVIEW, in that
      // order -- see WORKSPACE_IDS) -- only outside a text field, same as
      // every other single-key shortcut above (isTyping() already returned
      // early otherwise).
      ev.preventDefault();
      this._switchWorkspace(WORKSPACE_IDS[Number(ev.key) - 1]);
    }
  }

  /** Inside .app-body so it can dock as a right column at >=1100px (see
   * .app-body in styles.css); below that it's the usual overlay sheet. */
  _renderSignalPanel(wide = false) {
    const open = wide ? this._freqDetailsOpen : !!this._signalTarget;
    const target = wide ? this._freqDetailsTarget : this._signalTarget;
    const close = wide ? () => this._closeFrequencyDetails() : () => (this._signalTarget = null);
    return html`
      <signal-panel
        ?wide=${wide}
        .open=${open}
        .target=${target}
        .savedSignals=${this.signals}
        .favorites=${this.favorites}
        .tz=${this.state?.timezone ?? null}
        .surveyActive=${!!this.state?.survey?.active}
        .replayActive=${!!this.state?.survey?.replay}
        .latestDecodeEvent=${this.decodeEvents[0] ?? null}
        .jurisdiction=${this.settings?.allocation_jurisdiction ?? "US"}
        @close=${close}
        @signal-detail-loaded=${(ev) => { if (wide) this._freqDetailsTuning = ev.detail; }}
        @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "signal")}
        @tune-frequency=${(ev) =>
          this._doTuneFrequency(ev.detail.hz, ev.detail.demodulator, ev.detail.filter_bandwidth_hz, "signal")}
        @replay-tune=${(ev) => this._doReviewTune(ev.detail.frequency_hz)}
        @survey-decode=${(ev) => this._doSurveyDecode(ev.detail)}
        @optimize-decode=${async (ev) => {
          const tuned = await this._doTuneFrequency(ev.detail.frequency_hz, undefined, undefined, "signal");
          if (tuned) await this._doDecoderStart([ev.detail.decoder], ev.detail.frequency_hz);
        }}
        @decode-start=${(ev) => this._doDecoderStart(ev.detail.decoders, ev.detail.frequency_hz)}
        @open-messages=${(ev) => this._doOpenSurveyMessages(ev.detail.frequency_hz)}
        @refresh=${() => {
          this._loadInbox();
          this._loadRecent();
          this._loadSignals();
        }}
        @favorites-changed=${() => this._loadFavorites()}
        @select-signal=${(ev) => (this.selected = ev.detail)}
        @action=${(ev) => this._action(ev.detail.message)}
        @api-error=${(ev) => this._apiError("signal", ev.detail.message)}
      ></signal-panel>
    `;
  }

  render() {
    return html`
      <instrument-panel
        .state=${this.state}
        .wsDown=${this.wsDown}
        .profiles=${this.profiles}
        .tunedColor=${this.tunedColor}
        .tags=${this.tags}
        .detectFeature=${this.features.detect}
        .jurisdiction=${this.settings?.allocation_jurisdiction ?? "US"}
        .failure=${this._failureFor("instrument")}
        .lastDecodeEvent=${this.decodeEvents[0] ?? null}
        @open-decode=${() => this._openDecode()}
        .stepHz=${this._stepOverrideHz ?? defaultStepHz(this.state?.props?.demodulator)}
        .recorderFeature=${this.features.recorder}
        .antennaLocked=${!!this.settings?.antenna_locked}
        @antenna-lock=${(ev) => this._setReceiverLock("antenna_locked", "antenna", ev.detail.locked)}
        .modeLocked=${!!this.settings?.demodulator_locked}
        @mode-lock=${(ev) => this._setReceiverLock("demodulator_locked", "mode", ev.detail.locked)}
        .currentFavorite=${this.favorites.some((s) =>
          Math.abs(s.frequency_hz - (this.state?.props?.device_vfo_frequency ?? 0)) <= 1 &&
          s.demodulator === this.state?.props?.demodulator)}
        .savedFrequencyMatches=${savedFrequencyMatches(
          this.state?.props?.device_vfo_frequency, this.signals, this.favorites
        )}
        .recorderStatus=${this.recorder}
        .scannerDeck=${this.features.scanner ? this._sectionNode("scan") : null}
        .bandScanDraft=${this._bandScanDraft}
        .bandScanStatus=${this._bandScanStatus}
        .bandScanSampleRates=${this.state?.survey?.sample_rates_hz ?? []}
        .surveySettings=${this.settings?.survey}
        .peakBusy=${this._surveyPeakBusy}
        @survey-peak-threshold=${(ev) => this._doSurveyPeakThreshold(ev.detail.db)}
          @survey-marker-settings=${(ev) => this._doSurveyMarkerSettings(ev.detail)}
        .replayActive=${!!this.state?.survey?.replay}
        .interruptMessage=${this._interruptConfirm?.message ?? null}
        @owner-stop=${(ev) => this._doStatusStop(ev.detail?.kind)}
        @interrupt-continue=${() => this._resolveInterrupt(true)}
        @interrupt-cancel=${() => this._resolveInterrupt(false)}
        @open-band-scan=${() => this._openBandScanControls()}
        @open-discover=${(ev) => this._openDiscoverView(ev.detail.view)}
        @start-band-scan=${() => this._startBandScanFromReceiver()}
        @stop-band-scan=${() => this._doStatusStop("band_scan")}
        @band-scan-draft-change=${(ev) => this._updateBandScanDraft(ev.detail)}
        @open-recordings=${() => (this.recordingsOpen = true)}
        @open-decode-view=${() => (this.decodeViewOpen = true)}
        @recorder-arm=${(ev) => this._doRecorderArm(ev.detail.on)}
        @recorder-rec=${() => this._doRecorderRec()}
        @recorder-stop=${() => this._doRecorderStop()}
        @recorder-reveal=${(ev) => this._doRecorderReveal(ev.detail.name)}
        @recorder-analyze=${(ev) => this._doRecorderAnalyze(ev.detail.name)}
        @open-review=${(ev) => this._openReview(ev.detail)}
        @capture=${() => this._doCapture()}
        @favorite-current=${() => this._doFavoriteCurrent()}
        @back=${() => this._doBack()}
        @forward=${() => this._doForward()}
        @find=${() => (this.paletteOpen = true)}
        @new-profile=${(ev) => this._newProfile(ev.detail?.fromCurrent)}
        @refresh=${() => this._loadProfiles()}
        @api-error=${(ev) => this._status(ev.detail.message)}
        @open-profile=${(ev) => this._openProfile(ev.detail.id)}
        @apply-profile=${(ev) => this._doApplyProfile(ev.detail.id)}
        @setup=${() => (this.setupOpen = true)}
        @toggle-library=${() => this._switchWorkspace("library")}
        @toggle-diagnostics=${() => (this.diagOpen = !this.diagOpen)}
        @dismiss-failure=${() => this._clearFailure("instrument")}
        @apply-layout=${(ev) => this._applyLayoutPreset(ev.detail.key)}
        @tune-frequency=${(ev) =>
          this._doTuneFrequency(ev.detail.hz, ev.detail.demodulator, ev.detail.filterBandwidthHz, "instrument",
            { manualMode: ev.detail.manualMode })}
        @step-frequency=${(ev) => this._doStepFrequency(
          ev.detail.direction, ev.detail.multiplier, ev.detail.fixedStepHz)}
        @cycle-step=${() => this._cycleStep()}
        @set-antenna=${(ev) => this._doSetAntenna(ev.detail.antenna)}
        .frequencyDetailsOpen=${this._freqDetailsOpen}
        @frequency-details-toggle=${() => this.toggleFrequencyDetails()}
      ></instrument-panel>

      <!-- ⓘ current-frequency details: full-width panel directly below the receiver. -->
      ${this._renderSignalPanel(true)}

      <div class="workspace-switch" role="tablist" aria-label="workspace">
        ${WORKSPACE_IDS.map(
          (id) => html`<button
            role="tab"
            aria-selected=${this.workspace === id}
            data-workspace=${id}
            class="workspace-tab ${this.workspace === id ? "active" : ""}"
            data-help=${WORKSPACE_HELP[id] ?? ""}
            @click=${() => this._switchWorkspace(id)}
          >${WORKSPACE_LABELS[id]}</button>`
        )}
      </div>

      <receiver-lost-bar
        .alerts=${this._lostAlerts}
        .notify=${this._notifyLost}
        @retry=${(ev) => this._retryLost(ev.detail.kind)}
        @dismiss=${(ev) => (this._lostAlerts = this._lostAlerts.filter((a) => a.kind !== ev.detail.kind))}
        @notify-pref=${(ev) => this._setNotifyLost(ev.detail.on)}
      ></receiver-lost-bar>

      <div class="visually-hidden" aria-live="polite">${this._layoutAnnouncement}</div>

      <!-- S1: .app-body is a flex row so the docked <signal-panel> (>=1100px)
           takes a right column and the workspace content reflows beside it. -->
      <div class="app-body ${this._signalTarget ? "inspector-open" : ""}">
      <div class="app-main">
      ${this.workspace === "discover" ? html`<nav class="geo-toolbar discover-views" role="tablist" aria-label="Discover view">
        ${["spectrum", "inbox", "table", "wifi", "ble", "remoteid", "timeline", "rules", "beacons"].filter((view) => view !== "beacons" || sectionVisible("beacons", this.features)).map(view => html`<button
          role="tab" aria-selected=${this.discoverView === view} data-help=${DISCOVER_VIEW_HELP[view] ?? ""} @click=${() => this.discoverView = view}>${({spectrum: "Live survey", inbox: "Discovery inbox", table: "Saved signals / decodes", wifi: "Wi-Fi", ble: "BLE", remoteid: "Remote ID", timeline: "Timeline", rules: "Watch rules", beacons: "Beacons"})[view]}</button>`)}
      </nav>` : ""}
      ${this.workspace === "discover" && this.discoverView === "wifi" ? html`<wifi-view
        .source=${this._wifiInspectSource}
        .settings=${this.settings?.wifi ?? null}
        @open-wifi-setup=${() => this._openWifiSetup()}
      ></wifi-view>` : ""}
      ${this.workspace === "discover" && this.discoverView === "ble" ? html`<ble-view
        .settings=${this.settings?.ble ?? null}
        @open-ble-setup=${() => this._openBleSetup()}
        @settings-saved=${() => this._onSettingsSaved()}
      ></ble-view>` : ""}
      ${this.workspace === "discover" && this.discoverView === "remoteid" ? html`<remoteid-view></remoteid-view>` : ""}
      ${this.workspace === "discover" && this.discoverView === "timeline" ? html`<timeline-view
        @open-wifi-setup=${() => this._openWifiSetup()}
        @open-ble-setup=${() => this._openBleSetup()}
      ></timeline-view>` : ""}
      ${this.workspace === "discover" && this.discoverView === "rules" ? html`<rules-view></rules-view>` : ""}
      ${this.workspace === "discover" && this.discoverView === "beacons" ? this._sectionNode("beacons") : ""}
      ${this.workspace === "map" || (this.workspace === "discover" && this.discoverView === "table") ? html`<discover-map
        .events=${this.decodeEvents} .signals=${this.signals} .location=${this.settings?.location}
        .view=${this.workspace === "map" ? "map" : "table"} .canTune=${!!this.state?.can_control && !this.state?.survey?.replay}
        @tune-signal=${ev => this._doTuneSignal(ev.detail.id, "library")}
        @tune-catalog=${ev => this._doTuneCatalog(ev.detail.id, ev.detail.label, "catalog")}
        @refresh-map=${() => { this._loadDecodeEvents(); this._loadSignals(); }}
      ></discover-map>` : ""}
      ${this.workspace === "discover" && this.discoverView === "inbox" ? html`
        <section class="discovery-inbox" aria-label="Discovery inbox">
          <div class="panel">
            <h2>Discovery inbox</h2>
            <p>Review scanner detections and name saved frequencies here. Keep useful signals in your Library; inspect recordings and observations in Review.</p>
            <button @click=${() => this._switchWorkspace("review")}>Review recordings</button>
            <button @click=${() => this._switchWorkspace("library")}>Open Library</button>
            <button @click=${() => this._switchWorkspace("band_scans")}>Band scan reports</button>
          </div>
          ${sectionVisible("finds", this.features) ? this._sectionNode("finds") : ""}
          ${this._sectionNode("inbox")}
        </section>` : ""}
      ${this.workspace === "band_scans" ? html`${sectionVisible("scans", this.features) ? this._sectionNode("scans") : ""}<band-scan-report .selectedId=${this._reportScanId}></band-scan-report>` : ""}
      ${this.workspace === "library" ? html`<nav class="geo-toolbar" aria-label="Library review">
        <button @click=${() => { this.discoverView = "inbox"; this._switchWorkspace("discover"); }}>Discovery inbox · ${this.inbox.length} unidentified saves</button>
      </nav>` : ""}
      ${this._usesListenWorkbench() && !this._surveyFirst() ? this._listenWorkbench() : ""}
      ${this._showPrimarySpectrum() && (this.workspace !== "discover" || this.discoverView === "spectrum")
        ? this._surveyFirst()
          ? html`<div class="survey-first-workspace">
              <div class="primary-spectrum">${this._sectionNode("survey")}</div>
              <aside class="survey-first-sidebar" aria-label="Listening and decoding">${this._listenWorkbench()}</aside>
            </div>`
          : html`<div class="primary-spectrum">${this._sectionNode("survey")}</div>`
        : ""}
      ${this._showPrimarySpectrum() && (this.workspace !== "discover" || this.discoverView === "spectrum") ? this._renderScope() : ""}

      <diag-banner
        .state=${this.state}
        .wsDown=${this.wsDown}
        .open=${this.diagOpen}
        @close=${() => (this.diagOpen = false)}
        @action=${(ev) => this._action(ev.detail.message)}
        @api-error=${(ev) => this._apiError("instrument", ev.detail.message)}
      ></diag-banner>

      ${this.workspace === "band_scans" ? "" : this.workspace === "satellites" ? this._renderSatellites() : html`${this._renderWideBand()}
      <div class="grid">
        <getting-started
          .state=${this.state}
          .settings=${this.settings}
          .signals=${this.signals}
          .wsDown=${this.wsDown}
          @setup=${() => (this.setupOpen = true)}
          @capture=${() => this._doCapture()}
          @action=${(ev) => this._action(ev.detail.message)}
          @api-error=${(ev) => this._apiError("instrument", ev.detail.message)}
        ></getting-started>
        ${[0, 1, 2].map((colIndex) => this._renderColumn(colIndex))}
      </div>`}
      </div>
      ${this._renderSignalPanel()}
      </div>

      <msdr-palette
        .open=${this.paletteOpen}
        .tagsById=${this.tagsById}
        .tz=${this.state?.timezone ?? null}
        @close=${() => (this.paletteOpen = false)}
        @tune-frequency=${(ev) => this._doTuneFrequency(ev.detail.hz)}
        @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id)}
        @tune-catalog=${(ev) => this._doTuneCatalog(ev.detail.id, ev.detail.label)}
        @apply-profile=${(ev) => this._doApplyProfile(ev.detail.id)}
        @capture=${() => this._doCapture()}
        @back=${() => this._doBack()}
        @forward=${() => this._doForward()}
        @export=${() => this._doExport()}
        @apply-layout=${(ev) => this._applyLayoutPreset(ev.detail.key)}
        @scan-stop=${() => this._doScanStop()}
        @scan-pause=${() => this._doScanPause()}
        @scan-resume=${() => this._doScanResume()}
        @show-getting-started=${() => this._showGettingStarted()}
        @api-error=${(ev) => this._status(ev.detail.message)}
      ></msdr-palette>

      <msdr-drawer
        .selected=${this.selected}
        .profiles=${this.profiles}
        .tags=${this.tags}
        .passes=${this.passes}
        .state=${this.state}
        .identFeature=${this.features.ident}
        .trackingFeature=${this.features.tracking}
        .decoders=${this.decoders}
        .failure=${this._failureFor("drawer")}
        @close=${() => (this.selected = null)}
        @refresh=${() => {
          this._loadInbox();
          this._loadRecent();
          this._loadProfiles();
          this._loadTags();
          this._loadSignals();
        }}
        @tune-signal=${(ev) => this._doTuneSignal(ev.detail.id, "drawer")}
        @apply-profile=${(ev) => this._doApplyProfile(ev.detail.id, "drawer")}
        @track-toggle=${(ev) => this._doTrackToggle(ev.detail.active, ev.detail.id, "drawer")}
        @api-error=${(ev) => this._apiError("drawer", ev.detail.message)}
        @action=${(ev) => this._action(ev.detail.message)}
        @dismiss-failure=${() => this._clearFailure("drawer")}
      ></msdr-drawer>


      <setup-drawer
        .open=${this.setupOpen}
        .state=${this.state}
        .failure=${this._failureFor("setup")}
        @close=${() => (this.setupOpen = false)}
        @refresh-passes=${() => this._doRefreshPasses()}
        @settings-saved=${() => this._onSettingsSaved()}
        @settings-undoable=${(ev) =>
          this._rememberSettingsUndo(ev.detail.patch, ev.detail.previous, ev.detail.label, ev.detail.at)}
        @show-getting-started=${() => this._showGettingStarted()}
        .layoutEdit=${this._layoutEditMode}
        .decoders=${this.decoders}
        .validation=${this.validation}
        @layout-edit=${(ev) => this._setLayoutEdit(ev.detail.on)}
        @api-error=${(ev) => this._apiError("setup", ev.detail.message)}
        @action=${(ev) => this._action(ev.detail.message)}
        @dismiss-failure=${() => this._clearFailure("setup")}
      ></setup-drawer>

      <decode-view
        .open=${this.decodeViewOpen}
        .decoders=${this.decoders}
        .decodeEvents=${this.decodeEvents}
        .presetFrequencyHz=${this.decodeViewPresetFrequencyHz}
        .failure=${this._failureFor("decoder")}
        @close=${() => {
          this.decodeViewOpen = false;
          this.decodeViewPresetFrequencyHz = null;
        }}
        @action=${(ev) => this._action(ev.detail.message)}
        @api-error=${(ev) => this._apiError("decoder", ev.detail.message)}
        @dismiss-failure=${() => this._clearFailure("decoder")}
      ></decode-view>

      <recordings-view
        .open=${this.recordingsOpen}
        @close=${() => (this.recordingsOpen = false)}
        @action=${(ev) => this._action(ev.detail.message)}
        @open-review=${(ev) => this._openReview(ev.detail)}
      ></recordings-view>

      <restore-trail
        .restore=${this.restore}
        @dismiss=${() => this._dismissRestore()}
      ></restore-trail>

      <status-line
        .connected=${!!this.state?.connected}
        .started=${this.state?.started ?? true}
        .message=${this.error ?? this.lastAction}
        .error=${!!this.error}
        .version=${this.state?.version ?? null}
        @dismiss=${() => this._dismissError()}
      ></status-line>
    `;
  }
}

customElements.define("msdr-app", MsdrApp);
