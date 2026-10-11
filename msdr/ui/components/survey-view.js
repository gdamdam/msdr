import { LitElement, html } from "../vendor/lit.js";
import { parseFrequencyInput, mhz, bw, labelFor, evidenceClass, explain } from "../format.js";
import { nearestSavedFrequency } from "../saved-frequency.js";
import * as api from "../api.js";
import { decoderValidation, groupDecoders } from "../decoder-info.js";
import "./run-indicator.js";
import { buildPalette, dbToIndex, binToDb } from "../waterfall-palette.js";
import { EVIDENCE_SOURCES, evidenceBadges, clusteredEvidenceMarkers, mlWorkerPhase, surveyEvidence, matchesEvidenceFilter, compareEvidence } from "../survey-evidence.js";
import "./failure-note.js";
import { signalActivity } from "../signal-activity.js";

// Fraction of the canvas height given to the spectrum pane (grid + trace);
// the rest (minus the two occupancy strips and the frequency ruler) is the
// scrolling waterfall. SDR#-style layout, top to bottom: spectrum, occupancy
// strips, ruler, waterfall.
const SPECTRUM_FRACTION = 0.45;
const HEAT_STRIP_CSS_PX = 6;
const RULER_CSS_PX = 18;
const CANVAS_CSS_HEIGHT = 360;
// S1 contract's default surface heights at >=1280 css px wide (spectrum
// 100, waterfall 150, occupancy 24 -- the two heat strips, 12px each): a
// component-side fallback only, used when nothing else has given the canvas
// an explicit clientHeight (a layout drag / CSS still wins, so panes stay
// user-adjustable -- see _resizeCanvas). There's no settings.py home for
// this: SurveySettings.dimensions is the IQ-dimension filter list
// (activity/bandwidth/bandplan/catalog...), a different concept.
const WIDE_VIEWPORT_CSS_PX = 1280;
const WIDE_SPECTRUM_CSS_PX = 100;
const WIDE_WATERFALL_CSS_PX = 150;
const WIDE_HEAT_STRIP_CSS_PX = 12; // x2 strips = 24px occupancy band
const MIN_TICK_LABEL_GAP_CSS_PX = 70;
const TICK_STEPS_HZ = [10e3, 20e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6];
const WF_RANGE_STORAGE_KEY = "msdr.survey.wf";
// Sticky AUTO (default on): MIN/MAX follow the noise floor. A fixed range set
// on one band turned a quieter band's waterfall all black (floor under MIN).
const WF_AUTO_STORAGE_KEY = "msdr.survey.wfAuto";
const WF_AUTO_INTERVAL_MS = 1000;
const SNAP_STORAGE_KEY = "msdr.survey.snap";
const DEFAULT_HISTORY_ROWS = 300;
const DEFAULT_AVG_FACTOR = 0.5;

// S2: badge lane (chips under the waterfall, one per tracked peak/Label) and
// labels table -- see docs/superpowers/plans/2026-09-25-survey-s2.md Stream C.
const ACTIVITY_GLYPHS = { steady: "●", bursty: "▮▮", periodic: "↻", intermittent: "◌", new: "✦" };
// S3/S4: IQ analyzer fields (modulation/symbol_rate/family/voice) -- see
// docs/superpowers/plans/2026-09-25-survey-s3-s4.md Stream C.
const VOICE_GLYPHS = { voice: "🗣", data: "🔢", silence: "💤" };
const ANALYSIS_STATE_LABELS = {
  not_analyzed: "Not analyzed",
  waiting: "Waiting",
  current_result: "Current result",
  historical_result: "Historical result",
};
// bw_class values wide enough that Survey skips IQ feature extraction --
// mirrors msdr/survey/survey.py _WIDE_BW_CLASSES / families.py _WIDE_BW_CLASSES.
const WIDE_BW_CLASSES = new Set(["wfm", "wide_digital"]);
const BADGE_ROWS = 3;
const BADGE_ROW_CSS_PX = 20;
const BADGE_GAP_CSS_PX = 6; // minimum gap between two chips sharing a row
// Chip chrome (border + padding) not covered by the measured text width --
// padding:1px 5px + border:1px on each side = (1+5)*2 + 1*2.
const BADGE_CHROME_CSS_PX = 14;
/** Sortable survey-table columns: header click sorts by `value(label)`
 * (numbers numerically, text case-insensitively; missing values always last),
 * a second click on the same header flips the order. `desc`: the first click
 * sorts high-to-low (strength/recency), otherwise low-to-high. */
const SORT_COLUMNS = {
  frequency_hz: { value: (l) => l.frequency_hz },
  candidate: { value: (l) => { const name = candidateName(l); return name === "--" ? null : name; } },
  voice: { value: (l) => hasCwOutput(l) ? "CW" : l.voice?.kind },
  snr_db: { value: (l) => l.snr_db, desc: true },
  activity: { value: (l) => l.activity?.duty, desc: true },
  bw_class: { value: (l) => l.bw_class },
  modulation: { value: (l) => l.modulation?.kind },
  symbol_rate_hz: { value: (l) => l.symbol_rate_hz },
  family: { value: (l) => l.family?.name },
  bandplan: { value: (l) => l.bandplan?.name },
  catalog: {
    value: (l) =>
      [l.rds?.callsign, l.rds?.ps].filter(Boolean).join(" ") || l.catalog?.[0]?.name,
  },
  evidence: { value: (l) => surveyEvidence(l).rank, desc: true },
  last_seen: { value: (l) => l.last_seen, desc: true },
};

/** "KBRG · Amor": call letters then PS, once when the PS is the call sign
 * itself (KOIT sends "KOIT"). */
function rdsName(rds) {
  const parts = [rds.callsign, rds.ps?.trim()].filter(Boolean);
  return [...new Set(parts)].join(" · ");
}

// Signal measurements table: capped height with its own scrollbar, dragged
// taller or shorter via the ◢ handle (same gesture as the map's). The chosen
// height is per browser, like the TECH toggle below.
const TABLE_HEIGHT_KEY = "msdr.survey.tableHeight";
const TABLE_HEIGHT_DEFAULT = 360;
function clampTableHeight(value) {
  const h = Number(value);
  if (!Number.isFinite(h)) return TABLE_HEIGHT_DEFAULT;
  return Math.max(160, Math.min(1200, Math.round(h)));
}
function readTableHeight() {
  try {
    const v = localStorage.getItem(TABLE_HEIGHT_KEY);
    return v === null ? TABLE_HEIGHT_DEFAULT : clampTableHeight(v);
  } catch {
    return TABLE_HEIGHT_DEFAULT;
  }
}

// S1: TECH toggle (localStorage-backed) -- default table columns are
// Frequency/Candidate/Activity/Evidence/Last seen; SNR/BW/MOD/BAUD/DUTY and
// the older FAMILY/VOICE/BAND PLAN/CATALOG detail columns only show with
// TECH on (see _visibleColumns).
const TECH_STORAGE_KEY = "msdr.survey.tech";

function loadTechOn() {
  try {
    return localStorage.getItem(TECH_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveTechOn(on) {
  try {
    localStorage.setItem(TECH_STORAGE_KEY, on ? "1" : "0");
  } catch {
    // storage blocked (private window): the toggle just isn't remembered
  }
}

// S1 chrome-compaction pass: IDENTIFY/STATUS/DISPLAY collapse into a single
// "OPTIONS" disclosure (see _renderOptionsToggle), collapsed by default and
// remembered per-browser -- same try/catch pattern as the other survey.*
// localStorage flags above.
const OPTIONS_STORAGE_KEY = "msdr.survey.options";

function loadOptionsOpen() {
  try {
    return localStorage.getItem(OPTIONS_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveOptionsOpen(open) {
  try {
    localStorage.setItem(OPTIONS_STORAGE_KEY, open ? "1" : "0");
  } catch {
    // storage blocked (private window): the toggle just isn't remembered
  }
}

// Experimental ML controls default collapsed and remember an explicit open.
const ML_SECTION_STORAGE_KEY = "msdr.survey.ml_section_open";

function loadMlSectionOpen() {
  try {
    return localStorage.getItem(ML_SECTION_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveMlSectionOpen(open) {
  try {
    localStorage.setItem(ML_SECTION_STORAGE_KEY, open ? "1" : "0");
  } catch {
    // Optional preference; the disclosure still works without storage.
  }
}

// "Signals in view" disclosure -- collapsed by default and remembered.
const SHORTCUTS_STORAGE_KEY = "msdr.survey.shortcuts";

function loadShortcutsOpen() {
  try {
    return localStorage.getItem(SHORTCUTS_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveShortcutsOpen(open) {
  try {
    localStorage.setItem(SHORTCUTS_STORAGE_KEY, open ? "1" : "0");
  } catch {
    // storage blocked (private window): the toggle just isn't remembered
  }
}

// Short words for the collapsed OPTIONS summary line -- DIMENSION_LABELS
// (format.js) is the full column-header wording ("Bandwidth class"); this is
// the compact form ("bandwidth") so the one-line summary stays short.
const OPTIONS_DIMENSION_WORDS = {
  activity: "activity",
  bandwidth: "bandwidth",
  bandplan: "band plan",
  catalog: "catalog",
  modulation: "modulation",
  rds: "RDS",
  symbol_rate: "symbol rate",
  voice: "voice/data",
};

/** Best short display name for a label, in the same strength order as
 * _evidenceInfo below (RDS name > analyzer family > catalog match): the
 * CANDIDATE column's single best guess, not a full evidence breakdown. */
function hasCwOutput(l) {
  return l.confirmed?.decoder === "cw" && l.confirmed?.integrity !== "failed";
}

/** All services of a survey label's compact allocation summary (docs/
 * allocations.md "Backend integration": `{lo_hz, hi_hz, jurisdiction, status,
 * services:[{name, priority, table}]}`), primary first, secondary marked --
 * never truncated. "" when there's nothing to show. */
function allocationServicesText(allocation) {
  const services = allocation?.services ?? [];
  const primary = services.filter((s) => s.priority !== "secondary").map((s) => s.name);
  const secondary = services.filter((s) => s.priority === "secondary").map((s) => `${s.name} (secondary)`);
  const parts = [];
  if (primary.length) parts.push(primary.join(", "));
  if (secondary.length) parts.push(secondary.join(", "));
  return parts.join(" · ");
}

function candidateName(l) {
  if (hasCwOutput(l)) return "CW · decoder output";
  if (l.rds && (l.rds.callsign || l.rds.ps)) return rdsName(l.rds);
  if (l.family?.name) return shortFamilyName(l.family.name);
  if (l.last_classification) return `Last: ${shortFamilyName(l.last_classification.name)}`;
  if (l.catalog?.length) return l.catalog[0].name;
  return "--";
}

/** S1 contract's default ACTIVITY column: a glyph plus one of "voice"/
 * "data"/"quiet"/"keyed" when the analyzer has an opinion, else the
 * always-computed track activity kind (steady/bursty/periodic/...) --
 * never a duty-cycle percentage (that moved to the TECH-only DUTY column). */

const SNR_FILTER_STORAGE_KEY = "msdr.survey.snrFilter";
// A newly accepted row is marked ✦ this long.
const NEW_ROW_S = 15;

function loadSnrFilter() {
  try {
    const raw = JSON.parse(localStorage.getItem(SNR_FILTER_STORAGE_KEY) ?? "{}");
    return { min: raw.min ?? "", max: raw.max ?? "" };
  } catch {
    return { min: "", max: "" };
  }
}

function saveSnrFilter(min, max) {
  try {
    localStorage.setItem(SNR_FILTER_STORAGE_KEY, JSON.stringify({ min, max }));
  } catch {
    // storage blocked (private window): the filter just isn't remembered
  }
}

/** "POCSAG1200 / AX.25 AFSK" -> "POCSAG1200" -- the family table (families.py)
 * joins alternatives with " / "; badge chips only have room for the first. */
function shortFamilyName(name) {
  if (!name) return "";
  return name.split(" / ")[0];
}

/** "9600 Bd candidate (raw 9612, ±3 %)" -- shown once the analyzer reports
 * both a snapped and raw symbol rate (S4 §5: analyzer.symbol_rate_snapped_hz/
 * symbol_rate_raw_hz/symbol_rate_tolerance); null when either is missing (an
 * older row, or a signal with no measurable clock), so callers fall back to
 * the plain symbol_rate_hz text. */
function symbolRateCandidateText(l) {
  if (l.symbol_rate_snapped_hz == null || l.symbol_rate_raw_hz == null) return null;
  const snapped = Math.round(l.symbol_rate_snapped_hz);
  const raw = Math.round(l.symbol_rate_raw_hz);
  const tol = l.symbol_rate_tolerance != null ? Math.round(l.symbol_rate_tolerance * 100) : null;
  return `${snapped} Bd candidate (raw ${raw}${tol != null ? `, ±${tol} %` : ""})`;
}

/** "m:ss" (or "h:mm:ss" past an hour) -- the replay transport readout and
 * the msdr-recordings picker's duration column. Same shape as
 * recordings-view.js's local helper; kept separate rather than shared since
 * these two components otherwise have nothing to do with each other. */
function mmss(seconds) {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

/** SURVEY section (S1): span picker + one canvas holding the current-frame
 * spectrum trace, short/long occupancy heat strips and a scrolling
 * waterfall, driven by the "spectrum" (bins, up to settings.survey.spectrum_fps/s)
 * and "survey" (Survey.status(), ≤1/s) websocket pushes -- see app.js's
 * _onWsMessage and docs/superpowers/plans/2026-09-25-survey-s1.md Stream C.
 *
 * `status` mirrors Survey.status() (msdr/survey/survey.py); `frame` is the
 * latest decoded spectrum frame {bins: Uint8Array(512), center_hz,
 * sample_rate_hz, at, ref_level_db, base_db}; `ring` is app.js's newest-first
 * frame history (used to repaint the waterfall's backing store after a
 * resize or when this element is recreated, e.g. by a layout drag).
 *
 * `wfSettings` mirrors Settings.survey (msdr/settings.py) and supplies
 * SETUP-configured defaults for the waterfall/spectrum controls below --
 * wf_min_db/wf_max_db/wf_peak_hold seed the on-canvas MIN/MAX/PEAK controls
 * (a per-browser localStorage override for MIN/MAX still wins until RESET),
 * while wf_avg_factor/wf_palette/wf_history_rows/wf_show_grid have no
 * on-canvas override. */
export class SurveyView extends LitElement {
  static properties = {
    _tableHeight: { state: true }, // px, see TABLE_HEIGHT_KEY

    // S1 workspace-aware chrome: "listen" renders the compact toolbar (span,
    // PEAK/AVG, waterfall range, plain table); anything else (default
    // "discover") keeps the full toolbar exactly as before -- see render()/
    // _renderSource()/_visibleColumns(). The instance itself never changes
    // across a Listen<->Discover switch (app.js._inPrimarySlot), only this.
    mode: { type: String },
    status: { type: Object },
    receiverHz: { type: Number },
    frame: { type: Object },
    ring: { type: Array },
    connected: { type: Boolean },
    canControl: { type: Boolean },
    decoders: { type: Array },
    savedSignals: { type: Array },
    favorites: { type: Array },
    failure: { type: Object },
    wfSettings: { type: Object },
    mlBusy: { type: Boolean },
    peakBusy: { type: Boolean },
    showMlControls: { type: Boolean },
    _captureBusy: { state: true },
    _captureMessage: { state: true },
    _captureKind: { state: true },
    _captureDuration: { state: true },
    // Receiver filter bandwidth for the VFO shaded band; this component only
    // tracks the survey span, so the caller passes the current demod
    // bandwidth in. Defaults to a typical NFM channel width.
    filterBandwidthHz: { type: Number },
    _sampleRateHz: { state: true },
    _centerText: { state: true },
    _dirty: { state: true }, // true while the operator is editing the span draft (unapplied)
    _keepVfo: { state: true },
    _sortKey: { state: true },
    _sortOrder: { state: true },
    _activityFilter: { state: true }, // "all" or an activity kind -- table rows only
    _evidenceFilter: { state: true }, // all / validated / decoder_output / estimate / context / none
    _snrMin: { state: true }, // SNR filter bounds (dB, "" = no limit), localStorage-backed
    _snrMax: { state: true },
    _resortSeq: { state: true }, // bumped by update/re-sort: accepts the latest live rows
    _hoverLabelId: { state: true },
    _analyzerDropWarn: { state: true },
    _decodePopoverId: { state: true },
    _decodePopoverSelected: { state: true },
    _peakHold: { state: true },
    _avgSmooth: { state: true },
    _wfMin: { state: true },
    _wfMax: { state: true },
    _snapOn: { state: true }, // SNAP toggle (localStorage-backed) -- see _onCanvasClick/_nearestLabel
    _hoverHz: { state: true }, // frequency under the pointer while it's over the canvas, else null
    _hoverPoint: { state: true }, // viewport coordinates, including vertical-only movement
    _hoverNearest: { state: true }, // this._nearestLabel(_hoverHz) result, for the hover card + highlight
    _selectedLabelId: { state: true }, // canvas-click table-row/badge highlight -- see _onCanvasClick/_selectLabel
    _replayPickerOpen: { state: true }, // "⏵ REPLAY…" inline picker row toggle
    _displayOpen: { state: true }, // "Display ▾" popover (levels + zoom) open
    _replayPath: { state: true }, // picker's free-text path draft
    _replayLoop: { state: true }, // picker's loop checkbox draft
    _replayRecordings: { state: true }, // api.listRecordings() result, loaded lazily when the picker opens
    _replayDragging: { state: true }, // true while the operator drags the transport's seek slider
    _replayDragPositionS: { state: true }, // local echo of the slider while dragging (don't fight the drag)
    _techOn: { state: true }, // TECH toggle (localStorage-backed) -- see _visibleColumns
    _zoom: { state: true },
    _pan: { state: true },
    _spectrumHeight: { state: true },
    _spectrumGripTop: { state: true },
    _waterfallHeight: { state: true },
    _historyOverride: { state: true },
    _mlSectionOpen: { state: true }, // Entire ML area disclosure (localStorage-backed)
    _optionsOpen: { state: true }, // OPTIONS disclosure (localStorage-backed) -- see _renderOptionsToggle
    _shortcutsOpen: { state: true }, // "Signals in view" disclosure (localStorage-backed) -- see _renderBadgeLane
    _bandScan: { state: true },
    _bandScanError: { state: true },
    _bsStart: { state: true },
    _bsEnd: { state: true },
    _bsSpan: { state: true },
    _bsDwell: { state: true },
    _bsOverlap: { state: true },
    _bsStartDraft: { state: true },
    _bsEndDraft: { state: true },
    _bsDwellDraft: { state: true },
    _bsDurationPreset: { state: true },
    _bsOverlapDraft: { state: true },
    bandScanDraft: { attribute: false },
    receiverSampleRate: { attribute: false },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.mode = "discover";
    this.status = null;
    this.receiverHz = null;
    this.frame = null;
    this.ring = [];
    this.connected = false;
    this.canControl = false;
    this.decoders = [];
    this.savedSignals = [];
    this.favorites = [];
    this.failure = null;
    this.wfSettings = null;
    this.filterBandwidthHz = 12500;
    this._sampleRateHz = null;
    this._centerText = "";
    this._dirty = false;
    this._keepVfo = true;
    this._layout = null; // pixel breakdown of the canvas, set by _resizeCanvas
    this._waterfall = null; // offscreen backing canvas (bins wide x waterfall px tall)
    this._waterfallCtx = null;
    this._lastPushedAt = null; // frame.at last painted into the waterfall
    this._drawScheduled = false;
    this._resizeObserver = null;
    this._laneWidthPx = 640; // canvas CSS width, used to size the badge collision gap
    this._sortKey = "frequency_hz";
    this._sortOrder = "asc";
    this._activityFilter = "all";
    this._lastSpeechAt = new Map();
    this._evidenceFilter = "all";
    const snr = loadSnrFilter();
    this._snrMin = snr.min;
    this._snrMax = snr.max;
    this._resortSeq = 0;
    this._techOn = loadTechOn();
    this._mlSectionOpen = loadMlSectionOpen();
    this._optionsOpen = loadOptionsOpen();
    this._shortcutsOpen = loadShortcutsOpen();
    this._bandScan = { state: "idle", history: [] };
    this._bandScanError = "";
    this._bsStart = 136_000_000;
    this._bsEnd = 174_000_000;
    this._bsSpan = null;
    this._bsDwell = 3;
    this._bsOverlap = 0.25;
    this._bsStartDraft = "136.000";
    this._bsEndDraft = "174.000";
    this._bsDwellDraft = "10";
    this._bsDurationPreset = "10";
    this._bsDwellHoldTimer = null;
    this._bsOverlapDraft = "25";
    this.bandScanDraft = null;
    // Sticky table (see _tableView): accepted row ids, the sort/filter
    // signature, gone rows and the previous status' labels.
    this._rowOrder = [];
    this._orderSig = null;
    this._newIds = new Map(); // id -> epoch s it appeared (✦ marker)
    this._ghosts = new Map();
    this._prevLabels = new Map();
    this._viewStatus = null;
    this._viewSpan = null;
    this._viewVfoHz = undefined;
    this._awaitingFirstRows = true;
    this._hoverLabelId = null;
    this._analyzerDropWarn = false;
    this._prevAnalyzerDropped = null;
    this._decodePopoverId = null;
    this._decodePopoverSelected = new Set();
    this._peakHold = false; // "PEAK" toggle, default off
    this._avgSmooth = true; // "AVG" toggle (exponential smoothing), default on
    this._smoothBins = null; // Float32Array, retained across frames
    this._peakBins = null; // Float32Array, retained across frames while PEAK is on
    this._wfMin = null; // dB floor for waterfall/spectrum y-axis (localStorage-backed)
    this._wfMax = null; // dB ceiling, ditto
    this._wfOverridden = false; // true once MIN/MAX came from localStorage or a slider/AUTO edit
    this._wfDefaultsSeeded = false; // wf_peak_hold seeded from wfSettings once (like _syncSpanDraft)
    this._paletteName = null; // last palette built into _waterfall, to detect a SETUP change
    this._zoom = 1;
    this._pan = 0.5;
    this._canvasGesture = null;
    this._dragPreviewHz = null;
    this._suppressCanvasClick = false;
    this._spectrumHeight = 0;
    this._spectrumGripTop = 100;
    try {
      const height = Number(localStorage.getItem("msdr.spectrum.height"));
      if (height >= 80 && height <= 800) this._spectrumHeight = height;
    } catch { /* Optional preference. */ }
    this._waterfallHeight = 0;
    try {
      const height = Number(localStorage.getItem("msdr.waterfall.height"));
      if (height >= 100 && height <= 1200) this._waterfallHeight = height;
    } catch { /* Optional preference. */ }
    this._historyOverride = null;
    try {
      const saved = Number(localStorage.getItem("msdr.waterfall.history"));
      if ([300, 1000, 2000].includes(saved)) this._historyOverride = saved;
    } catch { /* Storage may be unavailable. */ }
    this._historyRowsBuilt = null; // last wf_history_rows the backing store was sized for
    this._alphaCache = null; // { color, rgb } memo for _withAlpha
    this._snapOn = this._loadSnapPref(); // SNAP toggle, default on
    this._hoverHz = null;
    this._hoverPoint = null;
    this._hoverNearest = null;
    this._hoverRafScheduled = false; // rAF throttle for pointermove hover work
    this._pendingHoverClientX = null;
    this._pendingHoverCanvas = null;
    this._selectedLabelId = null;
    this._replayPickerOpen = false;
    this._displayOpen = false;
    this._replayPath = "";
    this._replayLoop = true;
    this._replayRecordings = null;
    this._replayDragging = false;
    this._replayDragPositionS = 0;
    this._loadWfRange();
    this._wfAuto = true;
    this._wfAutoAt = 0;
    try {
      this._wfAuto = localStorage.getItem(WF_AUTO_STORAGE_KEY) !== "0";
    } catch {
      // storage unavailable: default on
    }
  }

  connectedCallback() {
    super.connectedCallback();
    this._resizeObserver = new ResizeObserver(() => {
      this._resizeCanvas();
      this._scheduleDraw();
    });
    // Esc clears the canvas-click selection (Task B) -- a document listener
    // since focus is usually on the canvas/table, not a form control here.
    this._onKeyDown = (ev) => {
      if (ev.key === "Escape" && this._selectedLabelId != null) this._selectLabel(null);
    };
    document.addEventListener("keydown", this._onKeyDown);
    // Display popover: outside click / Escape close it (same pattern as the
    // receiver DSP chip popovers in instrument-panel.js).
    this._onDisplayDocClick = (ev) => {
      if (!ev.composedPath().some((n) => n?.classList?.contains?.("survey-display-wrap"))) this._setDisplayOpen(false);
    };
    this._onDisplayKey = (ev) => {
      if (ev.key !== "Escape") return;
      this._setDisplayOpen(false);
      this.querySelector(".survey-display-toggle")?.focus();
    };
    this._refreshBandScan();
    this._bandScanTimer = setInterval(() => this._refreshBandScan(), 1500);
    this._activityTimer = setInterval(() => { this.requestUpdate(); this._scheduleDraw(); }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._resizeObserver?.disconnect();
    document.removeEventListener("keydown", this._onKeyDown);
    this._setDisplayOpen(false);
    clearInterval(this._bandScanTimer);
    clearInterval(this._activityTimer);
    this._stopBandScanDwellHold();
  }

  /** Open/close the Display popover; document listeners only live while it is open. */
  _setDisplayOpen(open) {
    this._displayOpen = open;
    document.removeEventListener("click", this._onDisplayDocClick);
    document.removeEventListener("keydown", this._onDisplayKey);
    if (open) {
      document.addEventListener("click", this._onDisplayDocClick);
      document.addEventListener("keydown", this._onDisplayKey);
    }
  }

  async _refreshBandScan() {
    if (this._bandScanRefreshing) return;
    this._bandScanRefreshing = true;
    const revision = this._bandScanRevision ?? 0;
    try {
      const status = await api.getSurveyBandScan();
      if (revision === (this._bandScanRevision ?? 0)) this._bandScan = status;
    } catch (err) {
      if (revision === (this._bandScanRevision ?? 0)) this._bandScanError = err.message;
    } finally { this._bandScanRefreshing = false; }
  }

  async _bandScanAction(action, body) {
    this._bandScanRevision = (this._bandScanRevision ?? 0) + 1;
    this._bandScanError = "";
    try {
      this._bandScan = await action(body);
      this._bandScanRevision++;
    } catch (err) {
      this._bandScanError = err.message;
    }
  }

  _startBandScan() {
    if (!(Number(this.receiverSampleRate) > 0)) {
      this._bandScanError = "Wait for the current SDRconnect span before starting a scan.";
      return;
    }
    return this._bandScanAction(api.startSurveyBandScan, {
      start_hz: Math.round(Number(this._bsStartDraft) * 1e6),
      end_hz: Math.round(Number(this._bsEndDraft) * 1e6),
      span_hz: Number(this.receiverSampleRate), dwell_s: Number(this._bsDwellDraft),
      overlap: Number(this._bsOverlapDraft) / 100,
    });
  }

  _publishBandScanDraft(patch) {
    this.dispatchEvent(new CustomEvent("band-scan-draft-change", { detail: patch }));
  }

  _adjustBandScanDwell(direction, heldMs = 0) {
    const current = Number(this._bsDwellDraft) || 10;
    const amount = heldMs >= 3000 ? 5 : heldMs >= 1200 ? 2 : 1;
    this._bsDwellDraft = String(Math.max(0.25, Math.min(300, current + direction * amount)));
    this._bsDurationPreset = "custom"; // the Duration select follows the hand-set value
    this._publishBandScanDraft({ dwell: this._bsDwellDraft, preset: "custom" });
  }

  _stopBandScanDwellHold() {
    clearTimeout(this._bsDwellHoldTimer);
    this._bsDwellHoldTimer = null;
  }

  _startBandScanDwellHold(event, direction) {
    if (this._bsDurationPreset !== "custom") return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    this._stopBandScanDwellHold();
    this._adjustBandScanDwell(direction);
    const started = performance.now();
    const repeat = () => {
      const heldMs = performance.now() - started;
      this._adjustBandScanDwell(direction, heldMs);
      this._bsDwellHoldTimer = setTimeout(repeat, heldMs >= 3000 ? 55 : heldMs >= 1200 ? 95 : 170);
    };
    this._bsDwellHoldTimer = setTimeout(repeat, 450);
  }

  _renderBandScan() {
    const scan = this._bandScan?.current;
    const active = scan && ["running", "paused"].includes(scan.state);
    const progress = scan ? `${scan.completed_count} / ${scan.total}` : "";
    return html`<details class="survey-band-scan" ?open=${!!active}
      @toggle=${event => { if (active) event.currentTarget.open = true; }}>

      <summary @click=${event => { if (active) event.preventDefault(); }}>Band scan <span class="dim">${scan ? `${scan.state} ${progress}` : "configure a frequency range"}</span></summary>
      <div class="survey-band-scan-controls">
        <label>Start MHz<input type="number" step="0.001" .value=${this._bsStartDraft} ?disabled=${active}
          @input=${e => { this._bsStartDraft = e.target.value; this._publishBandScanDraft({ start: e.target.value }); }}></label>
        <label>End MHz<input type="number" step="0.001" .value=${this._bsEndDraft} ?disabled=${active}
          @input=${e => { this._bsEndDraft = e.target.value; this._publishBandScanDraft({ end: e.target.value }); }}></label>
        <label>Span<input aria-label="Band scan span" readonly title="Follows the current SDRconnect span"
          .value=${this._bsSpan ? `${this._bsSpan / 1e6} MHz` : "Waiting for SDRconnect"}></label>
        <label>Duration<select aria-label="Listening time preset" ?disabled=${active}
          .value=${this._bsDurationPreset}
          @change=${e => { this._bsDurationPreset = e.target.value;
            if (e.target.value !== "custom") this._bsDwellDraft = e.target.value;
            this._publishBandScanDraft({ preset: this._bsDurationPreset, dwell: this._bsDwellDraft }); }}>
          <option value="3">Quick · 3 s</option><option value="10">Normal · 10 s</option>
          <option value="30">Detailed · 30 s</option><option value="60">Deep · 60 s</option>
          <option value="custom">Custom</option>
        </select></label>
        <label><span class="survey-bs-label">Listen per step (s) <span class="help-mark" tabindex="0" role="note" aria-label="About listen time" @click=${(e) => e.preventDefault()} title="Listen time is per span. Longer stops collect more fresh spectrum frames and may catch intermittent signals; the full scan takes longer. Voice and protocol analysis also depend on enabled analyzers and usable IQ." data-help="Listen time is per span. Longer stops collect more fresh spectrum frames and may catch intermittent signals; the full scan takes longer. Voice and protocol analysis also depend on enabled analyzers and usable IQ.">?</span></span><span class="survey-band-scan-dwell-stepper">
          <button type="button" aria-label="Shorter listening time" title="Shorter; hold to accelerate"
            ?disabled=${active}
            @pointerdown=${e => this._startBandScanDwellHold(e, -1)}
            @pointerup=${() => this._stopBandScanDwellHold()}
            @pointercancel=${() => this._stopBandScanDwellHold()}
            @lostpointercapture=${() => this._stopBandScanDwellHold()}
            @click=${e => { if (e.detail === 0) this._adjustBandScanDwell(-1); }}>−</button>
          <input aria-label="Seconds listening per band-scan step" type="number" min="0.25" max="300" step="0.25"
            .value=${this._bsDwellDraft} ?disabled=${active}
            @input=${e => { this._bsDwellDraft = e.target.value; this._bsDurationPreset = "custom"; this._publishBandScanDraft({ dwell: this._bsDwellDraft, preset: "custom" }); }}>
          <button type="button" aria-label="Longer listening time" title="Longer; hold to accelerate"
            ?disabled=${active}
            @pointerdown=${e => this._startBandScanDwellHold(e, 1)}
            @pointerup=${() => this._stopBandScanDwellHold()}
            @pointercancel=${() => this._stopBandScanDwellHold()}
            @lostpointercapture=${() => this._stopBandScanDwellHold()}
            @click=${e => { if (e.detail === 0) this._adjustBandScanDwell(1); }}>+</button>
        </span></label>
        <label>Overlap %<input type="number" min="0" max="89" step="5" .value=${this._bsOverlapDraft} ?disabled=${active}
          @input=${e => { this._bsOverlapDraft = e.target.value; this._publishBandScanDraft({ overlap: e.target.value }); }}></label>
        ${!active ? html`<button class="run-start" ?disabled=${!this.connected} @click=${() => this._startBandScan()}>▶ SCAN RANGE</button>` : ""}
        ${scan?.state === "running" ? html`<button @click=${() => this._bandScanAction(api.pauseSurveyBandScan)}>Ⅱ PAUSE</button>` : ""}
        ${scan?.state === "paused" ? html`<button @click=${() => this._bandScanAction(api.resumeSurveyBandScan)}>▶ RESUME</button>` : ""}
        ${active ? html`<button class="run-stop" @click=${() => this._bandScanAction(api.stopSurveyBandScan)}>■ STOP</button>` : ""}
        ${scan?.id ? html`<button class="band-scan-report-link" @click=${() => this.dispatchEvent(
          new CustomEvent("open-band-scan-report", { detail: { id: scan.id } })
        )}>View report →</button>` : ""}
      </div>
      ${this._bandScanError ? html`<div class="survey-band-scan-error" role="alert">${this._bandScanError}</div>` : ""}
      ${scan?.capture_degraded ? html`<div class="survey-band-scan-error" role="alert">Degraded capture: ${scan.capture_degraded}</div>` : ""}
      ${scan ? html`<div class="survey-band-scan-progress">
        <span>${scan.covered_count} covered</span>
        <span>${scan.uncovered_count} uncovered</span>
        <span>${scan.result_count} signals</span>
        <progress max=${scan.total} value=${scan.completed_count}></progress>
      </div>` : ""}
    </details>`;
  }

  firstUpdated() {
    const canvas = this.querySelector("canvas.survey-canvas");
    if (canvas) this._resizeObserver.observe(canvas);
    this._resizeCanvas();
    this._scheduleDraw();
  }

  willUpdate(changed) {
    const activitySpan = `${this.status?.generation}|${this.status?.span?.center_hz}|${this.status?.span?.sample_rate_hz}`;
    if (activitySpan !== this._activitySpan) {
      this._activitySpan = activitySpan;
      this._lastSpeechAt = new Map();
    }
    if (changed.has("bandScanDraft") && this.bandScanDraft) {
      const draft = this.bandScanDraft;
      this._bsStartDraft = draft.start;
      this._bsEndDraft = draft.end;
      this._bsDwellDraft = draft.dwell;
      this._bsDurationPreset = draft.preset;
      this._bsOverlapDraft = draft.overlap;
    }
    const receiverRate = Number(this.receiverSampleRate);
    this._bsSpan = Number.isFinite(receiverRate) && receiverRate > 0 ? receiverRate : null;
    if (!changed.has("status")) return;
    const previousHz = changed.get("status")?.span?.vfo_hz;
    const currentHz = this.status?.span?.vfo_hz;
    // A manual canvas/table selection belongs to the frequency at which it
    // was made. Once the receiver moves, let the new tuned row take over.
    if (previousHz != null && previousHz !== currentHz) {
      this._selectedLabelId = null;
    }
  }

  updated(changed) {
    if (changed.has("wfSettings") || changed.has("peakBusy")) {
      const peakSelect = this.querySelector('select[aria-label="DSP peak threshold, dB over noise floor"]');
      if (peakSelect) peakSelect.value = String(this.wfSettings?.peak_threshold_db ?? 10);
    }
    this._positionHoverCard();
    if (changed.has("status")) this._syncSpanDraft();
    if (changed.has("status")) this._trackAnalyzerDropped();
    // Once replay actually starts (status confirms it), close the picker --
    // left open on a 409 (bad path) so the operator can fix and retry.
    if (changed.has("status") && this.status?.replay && this._replayPickerOpen) this._replayPickerOpen = false;
    if (changed.has("frame") && this.frame) {
      const oldFrame = changed.get("frame");
      if (oldFrame && (oldFrame.center_hz !== this.frame.center_hz || oldFrame.sample_rate_hz !== this.frame.sample_rate_hz)) {
        this._zoom = 1;
        this._pan = 0.5;
        // Retained PEAK/AVG bins are per-frequency: after a span/center/preset
        // change they would show old maxima at the wrong frequencies.
        this._peakBins = null;
        this._smoothBins = null;
      }
      this._ensureWfRange(this.frame);
      if (this._wfAuto && performance.now() - this._wfAutoAt > WF_AUTO_INTERVAL_MS) {
        this._wfAutoAt = performance.now();
        this._autoTrack(this.frame);
      }
      if (!this._waterfall) this._resizeCanvas();
      this._updateTraceState(this.frame);
      this._pushWaterfallRow(this.frame);
    }
    if (changed.has("wfSettings")) this._onWfSettingsChanged();
    if (changed.has("frame") || changed.has("status") || changed.has("wfSettings")) this._scheduleDraw();
  }

  /** Reacts to a SETUP change (or the settings first arriving): seeds
   * wf_peak_hold once, re-seeds MIN/MAX from wf_min_db/wf_max_db unless the
   * operator already has a localStorage override, and rebuilds the waterfall
   * backing store when the palette or history-row depth changed. */
  _onWfSettingsChanged() {
    if (!this._wfDefaultsSeeded && this.wfSettings) {
      this._peakHold = !!this.wfSettings.wf_peak_hold;
      this._wfDefaultsSeeded = true;
    }
    if (!this._wfOverridden) {
      this._wfMin = null;
      this._wfMax = null;
      this._ensureWfRange(this.frame ?? this.ring?.[0]);
    }
    // Rebuilds the backing store when wf_palette/wf_history_rows changed
    // (no-op otherwise); always repaints since wf_min_db/wf_max_db may have
    // just changed the color mapping even when neither of those did.
    this._resizeCanvas();
    this._repaintWaterfallFromRing();
  }

  /** wf_history_rows (SETUP), falling back to DEFAULT_HISTORY_ROWS before
   * settings have loaded or when unset. */
  _historyRows() {
    const n = this._historyOverride ?? this.wfSettings?.wf_history_rows;
    return Number.isFinite(n) && n > 0 ? Math.round(n) : DEFAULT_HISTORY_ROWS;
  }

  _setHistoryRows(value) {
    this._historyOverride = [300, 1000, 2000].includes(Number(value)) ? Number(value) : null;
    try {
      if (this._historyOverride == null) localStorage.removeItem("msdr.waterfall.history");
      else localStorage.setItem("msdr.waterfall.history", String(this._historyOverride));
    } catch { /* Keep the control usable without storage. */ }
    this._resizeCanvas();
    this._repaintWaterfallFromRing();
    this._scheduleDraw();
  }

  /** wf_avg_factor (SETUP)'s EMA weight for the AVG-smoothed trace. */
  _avgFactor() {
    const f = this.wfSettings?.wf_avg_factor;
    return typeof f === "number" ? f : DEFAULT_AVG_FACTOR;
  }

  /** 256x[r,g,b] LUT for wf_palette (SETUP), cached by waterfall-palette.js. */
  _currentPalette() {
    return buildPalette(this.wfSettings?.wf_palette ?? "sdrsharp");
  }

  // --- waterfall/spectrum dB range (MIN/MAX sliders, AUTO) ------------------

  _loadWfRange() {
    try {
      const raw = localStorage.getItem(WF_RANGE_STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (typeof parsed.min === "number") this._wfMin = parsed.min;
      if (typeof parsed.max === "number") this._wfMax = parsed.max;
      if (this._wfMin != null || this._wfMax != null) this._wfOverridden = true;
    } catch {
      // ignore malformed/unavailable storage
    }
  }

  _saveWfRange() {
    try {
      localStorage.setItem(WF_RANGE_STORAGE_KEY, JSON.stringify({ min: this._wfMin, max: this._wfMax }));
    } catch {
      // ignore unavailable storage
    }
  }

  /** Seeds _wfMin/_wfMax the first time both are unset: from wf_min_db/
   * wf_max_db (SETUP defaults) where set, else from the frame's
   * base_db/ref_level_db -- unless a localStorage override already loaded
   * them, or the operator already set them via the slider/AUTO/RESET. */
  _ensureWfRange(frame) {
    if (this._wfMin != null && this._wfMax != null) return;
    const baseDb = frame?.base_db ?? -145;
    const refDb = frame?.ref_level_db ?? -20;
    if (this._wfMin == null) this._wfMin = this.wfSettings?.wf_min_db ?? baseDb + 10;
    if (this._wfMax == null) this._wfMax = this.wfSettings?.wf_max_db ?? refDb - 10;
  }

  _setWfAuto(on) {
    this._wfAuto = on;
    try {
      localStorage.setItem(WF_AUTO_STORAGE_KEY, on ? "1" : "0");
    } catch {
      // ignore unavailable storage
    }
    if (on && this.frame) this._autoTrack(this.frame, true);
    this.requestUpdate();
  }

  /** Sticky AUTO: MIN a little under the noise floor (20th percentile bin),
   * MAX over the strongest signal but at least 45 dB above the floor. Only
   * moves (and repaints the history) when a bound drifts > 3 dB, so the
   * colours don't shimmer frame to frame. */
  _autoTrack(frame, force = false) {
    if (!frame?.bins?.length) return;
    const baseDb = frame.base_db ?? -145;
    const refDb = frame.ref_level_db ?? -20;
    const dbs = Array.from(frame.bins, (b) => binToDb(b, baseDb, refDb)).sort((a, b) => a - b);
    const pct = (p) => dbs[Math.max(0, Math.min(dbs.length - 1, Math.round(p * (dbs.length - 1))))];
    const floor = pct(0.2);
    const min = Math.round(floor - 8);
    const max = Math.round(Math.max(pct(0.995) + 5, floor + 45));
    if (!force && this._wfMin != null && Math.abs(min - this._wfMin) <= 3 && Math.abs(max - this._wfMax) <= 3) return;
    this._wfMin = min;
    this._wfMax = max;
    this._repaintWaterfallFromRing();
    this._scheduleDraw();
  }

  _setWfMin(v) {
    if (this._wfAuto) this._setWfAuto(false);
    this._wfMin = Math.min(v, (this._wfMax ?? 0) - 1);
    this._wfOverridden = true;
    this._saveWfRange();
    this._repaintWaterfallFromRing();
    this._scheduleDraw();
  }

  _setWfMax(v) {
    if (this._wfAuto) this._setWfAuto(false);
    this._wfMax = Math.max(v, (this._wfMin ?? -160) + 1);
    this._wfOverridden = true;
    this._saveWfRange();
    this._repaintWaterfallFromRing();
    this._scheduleDraw();
  }

  /** RESET (next to AUTO): clears the localStorage MIN/MAX override so
   * SETUP's wf_min_db/wf_max_db (or frame-derived auto, absent those) apply
   * again. */
  _resetWfRange() {
    try {
      localStorage.removeItem(WF_RANGE_STORAGE_KEY);
    } catch {
      // ignore unavailable storage
    }
    this._wfOverridden = false;
    this._wfMin = null;
    this._wfMax = null;
    this._ensureWfRange(this.frame ?? this.ring?.[0]);
    this._repaintWaterfallFromRing();
    this._scheduleDraw();
  }

  // --- snap-to-signal (canvas click + hover) --------------------------------

  _loadSnapPref() {
    try {
      const raw = localStorage.getItem(SNAP_STORAGE_KEY);
      return raw === null ? true : raw !== "0";
    } catch {
      return true;
    }
  }

  _toggleSnap() {
    this._snapOn = !this._snapOn;
    try {
      localStorage.setItem(SNAP_STORAGE_KEY, this._snapOn ? "1" : "0");
    } catch {
      // ignore unavailable storage
    }
  }

  /** Closest status.labels entry to `hz` by center frequency, plus whether it
   * falls within the snap/hover radius: max(2 bins, label.width_hz/2 + 1 bin)
   * -- shared by _onCanvasClick (snap-to-tune / shift-click) and the hover
   * card/highlight. Returns null when there are no labels. */
  _nearestLabel(hz) {
    const labels = this._spectrumLabels();
    if (!labels.length || hz == null) return null;
    let best = null;
    let bestDist = Infinity;
    for (const l of labels) {
      const d = Math.abs((l.peak_frequency_hz ?? l.frequency_hz) - hz);
      if (d < bestDist) {
        bestDist = d;
        best = l;
      }
    }
    if (!best) return null;
    const binHz = this._analyzerBinToleranceHz();
    const radiusHz = Math.max(2 * binHz, (best.width_hz ?? 0) / 2 + binHz);
    return { label: best, distanceHz: bestDist, radiusHz, withinRadius: bestDist <= radiusHz };
  }

  _tunedLabelId(labels) {
    if (this.status?.replay) return null;
    const hz = this.status?.span?.vfo_hz;
    if (hz == null) return null;
    const binHz = this._analyzerBinToleranceHz();
    const nearest = labels.reduce((best, label) =>
      !best || Math.abs(label.frequency_hz - hz) < Math.abs(best.frequency_hz - hz) ? label : best, null);
    if (!nearest) return null;
    const radiusHz = Math.max(2 * binHz, (nearest.width_hz ?? 0) / 2 + binHz);
    return Math.abs(nearest.frequency_hz - hz) <= radiusHz ? nearest.id : null;
  }

  _highlightedLabelId(labels) {
    // Inspection/hover must never replace the live receiver's listening mark.
    if (!this.status?.replay && this.status?.span?.vfo_hz != null) return this._tunedLabelId(labels);
    return labels.some((label) => label.id === this._selectedLabelId) ? this._selectedLabelId : null;
  }

  /** mousemove over the canvas: rAF-throttled so the nearest-label search
   * (and the resulting Lit re-render for the hover card) runs at most once
   * per frame no matter how fast mousemove fires. */
  _onCanvasPointerMove(ev) {
    this._pendingHoverClientX = ev.clientX;
    this._pendingHoverClientY = ev.clientY;
    this._pendingHoverCanvas = ev.currentTarget;
    if (this._hoverRafScheduled) return;
    this._hoverRafScheduled = true;
    requestAnimationFrame(() => {
      this._hoverRafScheduled = false;
      this._processHover();
    });
  }

  _processHover() {
    const canvas = this._pendingHoverCanvas;
    const rect = canvas?.getBoundingClientRect();
    const frame = this._viewFrame();
    if (!rect?.width || !frame.sample_rate_hz || frame.center_hz == null) {
      this._hoverHz = null;
      this._hoverNearest = null;
      return;
    }
    const markerLabel = this._markerLabelAtPoint(
      this._pendingHoverClientX, this._pendingHoverClientY, canvas
    );
    const frac = Math.max(0, Math.min(1, (this._pendingHoverClientX - rect.left) / rect.width));
    this._hoverPoint = { x: this._pendingHoverClientX, y: this._pendingHoverClientY };
    this._hoverHz = markerLabel?.frequency_hz ?? Math.round(
      frame.center_hz - frame.sample_rate_hz / 2 + frac * frame.sample_rate_hz
    );
    this._hoverNearest = markerLabel
      ? { label: markerLabel, withinRadius: true }
      : this._nearestLabel(this._hoverHz);
    this._scheduleDraw();
  }

  /** Map a displaced evidence tag back to its signal's real frequency anchor.
   * Plain clicks elsewhere on the spectrum keep their existing frequency map. */
  _markerLabelAtPoint(clientX, clientY, canvas) {
    const rect = canvas?.getBoundingClientRect();
    const layout = this._layout;
    const frame = this._viewFrame();
    if (!rect?.width || !layout?.w || !frame.sample_rate_hz || frame.center_hz == null) return null;
    const scale = layout.w / rect.width;
    const x = (clientX - rect.left) * scale;
    const y = (clientY - rect.top) * scale;
    const low = frame.center_hz - frame.sample_rate_hz / 2;
    const maxSources = Math.max(0, Math.floor((layout.specH / layout.dpr - 4) / 13));
    const markers = clusteredEvidenceMarkers(
      this._spectrumLabels(), low, frame.sample_rate_hz, layout.w, 28 * layout.dpr
    );
    for (const marker of markers) {
      const shown = marker.sources.slice(0, maxSources);
      if (Math.abs(x - marker.x) > 10 * layout.dpr) continue;
      const row = shown.findIndex((_source, index) => {
        const top = layout.specTop + (3 + index * 13) * layout.dpr;
        return y >= top && y <= top + 12 * layout.dpr;
      });
      if (row < 0) continue;
      const anchorHz = marker.anchor_frequencies_hz.reduce((best, frequency) => {
        const anchorX = (frequency - low) / frame.sample_rate_hz * layout.w;
        return Math.abs(anchorX - marker.x) < Math.abs(
          (best - low) / frame.sample_rate_hz * layout.w - marker.x
        ) ? frequency : best;
      }, marker.anchor_frequencies_hz[0]);
      return this._spectrumLabels().find(label => label.frequency_hz === anchorHz) ?? null;
    }
    return null;
  }

  _onCanvasPointerLeave() {
    this._pendingHoverCanvas = null; // a queued animation frame must not reopen the card
    this._hoverPoint = null;
    this._hoverHz = null;
    this._hoverNearest = null;
    this._scheduleDraw();
  }

  _positionHoverCard() {
    const card = this.querySelector(".survey-hover-card");
    const canvas = this.querySelector("canvas.survey-canvas");
    if (!card || !canvas || !this._hoverPoint) return;
    const rect = canvas.getBoundingClientRect();
    const point = this._hoverPoint;
    const place = (cursor, start, end, size) => {
      const after = cursor + 12;
      const preferred = after + size <= end - 4 ? after : cursor - size - 12;
      return Math.max(start + 4, Math.min(preferred, end - size - 4));
    };
    card.style.left = `${place(point.x, Math.max(rect.left, 0), Math.min(rect.right, innerWidth), card.offsetWidth) - rect.left}px`;
    card.style.top = `${place(point.y, Math.max(rect.top, 0), Math.min(rect.bottom, innerHeight), card.offsetHeight) - rect.top}px`;
  }

  _togglePeak() {
    this._peakHold = !this._peakHold;
    this._peakBins = null;
    if (this._peakHold && this.frame) this._updateTraceState(this.frame);
    this._scheduleDraw();
  }

  _toggleAvg() {
    this._avgSmooth = !this._avgSmooth;
    this._smoothBins = null;
    if (this._avgSmooth && this.frame) this._updateTraceState(this.frame);
    this._scheduleDraw();
  }

  /** Advances the exponential-smoothing and peak-hold bin arrays by one
   * frame; called once per incoming frame (not per draw, so redraws that
   * don't carry a new frame -- e.g. a 1/s occupancy tick -- don't skew it). */
  _updateTraceState(frame) {
    if (!frame?.bins?.length) return;
    const n = frame.bins.length;
    if (this._avgSmooth) {
      if (!this._smoothBins || this._smoothBins.length !== n) this._smoothBins = Float32Array.from(frame.bins);
      else {
        const a = this._avgFactor();
        for (let i = 0; i < n; i++) this._smoothBins[i] = this._smoothBins[i] * a + frame.bins[i] * (1 - a);
      }
    }
    if (this._peakHold) {
      if (!this._peakBins || this._peakBins.length !== n) this._peakBins = Float32Array.from(frame.bins);
      else for (let i = 0; i < n; i++) if (frame.bins[i] > this._peakBins[i]) this._peakBins[i] = frame.bins[i];
    }
  }

  /** Resolves a CSS color (hex or any canvas-fillStyle-parseable value) to an
   * rgba() string at the given alpha, via a 1x1 probe canvas so gradients and
   * translucent fills can use theme colors without per-pixel JS math. Memoized
   * on the last color since a draw pass reuses the same accent/fg repeatedly. */
  _withAlpha(color, alpha) {
    if (this._alphaCache?.color !== color) {
      const probe = this._alphaProbe || (this._alphaProbe = document.createElement("canvas"));
      probe.width = 1;
      probe.height = 1;
      const pctx = probe.getContext("2d");
      pctx.clearRect(0, 0, 1, 1);
      pctx.fillStyle = color;
      pctx.fillRect(0, 0, 1, 1);
      const [r, g, b] = pctx.getImageData(0, 0, 1, 1).data;
      this._alphaCache = { color, rgb: [r, g, b] };
    }
    const [r, g, b] = this._alphaCache.rgb;
    return `rgba(${r},${g},${b},${alpha})`;
  }

  /** Smallest step from TICK_STEPS_HZ (10 kHz..1 MHz) whose label spacing is
   * at least MIN_TICK_LABEL_GAP_CSS_PX apart, given the current span. */
  _tickStepHz(spanHz, cssWidth) {
    for (const step of TICK_STEPS_HZ) {
      if (cssWidth * (step / spanHz) >= MIN_TICK_LABEL_GAP_CSS_PX) return step;
    }
    return TICK_STEPS_HZ[TICK_STEPS_HZ.length - 1];
  }

  /** Major (labelled) tick marks for the frequency ruler and the spectrum
   * pane's vertical grid, in device pixels. */
  _mhzTicks(frame, w, dpr) {
    const spanHz = frame.sample_rate_hz;
    const low = frame.center_hz - spanHz / 2;
    const high = frame.center_hz + spanHz / 2;
    const step = this._tickStepHz(spanHz, w / dpr);
    const precision = step >= 1e6 ? 0 : step < 100e3 ? 3 : 2;
    const ticks = [];
    const start = Math.ceil(low / step) * step;
    for (let hz = start; hz <= high; hz += step) {
      ticks.push({ x: ((hz - low) / spanHz) * w, hz, label: (hz / 1_000_000).toFixed(precision) });
    }
    return ticks;
  }

  /** Mirrors decode-deck's dropped-frames tracking (status.band.iq.dropped)
   * for status.analyzer.iq.dropped: sticky for the session once it's seen to
   * *increase*, so a nonzero-but-flat count from before this session doesn't
   * alarm. */
  _trackAnalyzerDropped() {
    const dropped = this.status?.analyzer?.iq?.dropped;
    if (typeof dropped !== "number") return;
    if (this._prevAnalyzerDropped !== null && dropped > this._prevAnalyzerDropped) this._analyzerDropWarn = true;
    this._prevAnalyzerDropped = dropped;
  }

  /** Keeps the sample-rate/centre drafts mirroring status.span whenever it
   * changes (another client changing the span, the 1/s occupancy tick, or
   * this component's own APPLY/preset taking effect) -- except while the
   * operator is mid-edit (_dirty), so a status push doesn't clobber an
   * unapplied draft. Once status catches up to a dirty draft (it now
   * reports what the operator asked for), _dirty clears and sync resumes. */
  _syncSpanDraft() {
    const span = this.status?.span;
    if (!span || span.sample_rate_hz == null || span.center_hz == null) return;
    const matchesDraft =
      span.sample_rate_hz === this._sampleRateHz && (span.center_hz / 1_000_000).toFixed(6) === this._centerText;
    if (this._dirty) {
      if (!matchesDraft) return;
      this._dirty = false;
    }
    this._sampleRateHz = span.sample_rate_hz;
    this._centerText = (span.center_hz / 1_000_000).toFixed(6);
  }

  _cssVar(name, fallback) {
    const v = getComputedStyle(this).getPropertyValue(name).trim();
    return v || fallback;
  }

  // --- canvas sizing --------------------------------------------------------

  _resizeCanvas() {
    const canvas = this.querySelector("canvas.survey-canvas");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || canvas.parentElement?.clientWidth || 640;
    // S1 contract default (>=1280 css px wide): spectrum 100 / waterfall 150
    // / occupancy 24 -- only when nothing has given the canvas an explicit
    // CSS height of its own (a layout drag or external CSS still wins). The
    // canvas has no CSS height of its own (render() leaves it unset, only
    // width:100% -- see below); this component is the one that sets
    // canvas.style.height, so "explicit" means someone *else* changed it
    // since our own last write, not our own previous pass's value (which
    // would otherwise make the very next resize see its own fallback as
    // "already explicit" and get stuck on it).
    const ownHeightPx = this._canvasHeightPx != null ? `${this._canvasHeightPx}px` : "";
    const explicitHeight = !!canvas.style.height && canvas.style.height !== ownHeightPx;
    // Decided on the *viewport* width, not the canvas width: with the
    // inspector docked the canvas is ~840 px at a 1280 px window, and the
    // budget is about the screen, not the column.
    const wide = !explicitHeight && window.innerWidth >= WIDE_VIEWPORT_CSS_PX;
    const wideHeatStripPx = wide ? WIDE_HEAT_STRIP_CSS_PX : HEAT_STRIP_CSS_PX;
    const baseHeight = this._waterfallHeight
      ? WIDE_SPECTRUM_CSS_PX + wideHeatStripPx * 2 + RULER_CSS_PX + this._waterfallHeight
      : explicitHeight
      ? canvas.clientHeight
      : wide
        ? WIDE_SPECTRUM_CSS_PX + wideHeatStripPx * 2 + RULER_CSS_PX + WIDE_WATERFALL_CSS_PX
        : CANVAS_CSS_HEIGHT;
    const defaultSpectrumH = (wide || this._waterfallHeight)
      ? WIDE_SPECTRUM_CSS_PX : baseHeight * SPECTRUM_FRACTION;
    const spectrumCssH = this._spectrumHeight || defaultSpectrumH;
    const cssHeight = baseHeight + spectrumCssH - defaultSpectrumH;
    this._spectrumGripTop = spectrumCssH;
    canvas.style.height = `${cssHeight}px`;
    this._canvasHeightPx = cssHeight;
    this._laneWidthPx = cssWidth; // badge lane is a same-width HTML overlay below the canvas
    const w = Math.max(1, Math.round(cssWidth * dpr));
    const h = Math.max(1, Math.round(cssHeight * dpr));
    const bins = this.frame?.bins?.length || this.ring?.[0]?.bins?.length || 512;
    const historyRows = this._historyRows();
    if (
      canvas.width === w &&
      canvas.height === h &&
      this._waterfall &&
      this._waterfall.width === bins &&
      this._waterfall.height === historyRows &&
      this._paletteName === (this.wfSettings?.wf_palette ?? "sdrsharp")
    )
      return;
    canvas.width = w;
    canvas.height = h;

    const specH = Math.max(1, Math.round(spectrumCssH * dpr));
    const heatH = Math.max(1, Math.round(wideHeatStripPx * dpr));
    const rulerH = Math.max(1, Math.round(RULER_CSS_PX * dpr));
    const waterfallH = Math.max(1, h - specH - heatH * 2 - rulerH);
    this._layout = {
      w,
      h,
      dpr,
      specH,
      heatH,
      rulerH,
      waterfallH,
      specTop: 0,
      shortTop: specH,
      longTop: specH + heatH,
      rulerTop: specH + heatH * 2,
      waterfallTop: specH + heatH * 2 + rulerH,
    };

    // Backing store height is wf_history_rows (SETUP), not the on-screen
    // waterfall pane's pixel height -- _draw() below scales it to fit via
    // drawImage, so this is the actual scroll depth kept, independent of the
    // panel's on-screen size.
    this._waterfall = document.createElement("canvas");
    this._waterfall.width = bins;
    this._waterfall.height = historyRows;
    this._waterfallCtx = this._waterfall.getContext("2d");
    this._historyRowsBuilt = historyRows;
    this._paletteName = this.wfSettings?.wf_palette ?? "sdrsharp";
    this._ensureWfRange(this.frame ?? this.ring?.[0]);
    this._repaintWaterfallFromRing();
  }

  // --- waterfall (offscreen: shift one row, paint the new one in) ----------

  _paintRowInto(ctx, frame, rowIndex, width) {
    const bins = frame?.bins;
    if (!bins || !bins.length) return;
    const n = Math.min(width, bins.length);
    const baseDb = frame.base_db ?? -145;
    const refDb = frame.ref_level_db ?? -20;
    const minDb = this._wfMin ?? baseDb + 10;
    const maxDb = this._wfMax ?? refDb - 10;
    const palette = this._currentPalette();
    const imgData = ctx.createImageData(width, 1);
    const data = imgData.data;
    for (let i = 0; i < n; i++) {
      const idx = dbToIndex(binToDb(bins[i], baseDb, refDb), minDb, maxDb) * 3;
      const o = i * 4;
      data[o] = palette[idx];
      data[o + 1] = palette[idx + 1];
      data[o + 2] = palette[idx + 2];
      data[o + 3] = 255;
    }
    ctx.putImageData(imgData, 0, rowIndex);
  }

  /** Bulk-repaints the offscreen waterfall from app.js's ring (newest-first,
   * capped at 2000) -- on (re)allocation, and whenever the MIN/MAX dB range
   * changes (so history recolors instead of only new rows). */
  _repaintWaterfallFromRing() {
    if (!this._waterfallCtx) return;
    const w = this._waterfall.width;
    const h = this._waterfall.height;
    this._waterfallCtx.fillStyle = "#000";
    this._waterfallCtx.fillRect(0, 0, w, h);
    const ring = this.ring ?? [];
    const n = Math.min(h, ring.length);
    for (let r = 0; r < n; r++) this._paintRowInto(this._waterfallCtx, ring[r], r, w);
    this._lastPushedAt = ring[0]?.at ?? null;
  }

  /** Newest row on top: shifts the existing backing store down by one row
   * (cheap self-drawImage), then paints just the new row at row 0. Skips a
   * frame already folded in by _repaintWaterfallFromRing (same `at`). */
  _pushWaterfallRow(frame) {
    if (!this._waterfallCtx || !frame?.bins?.length) return;
    if (this._lastPushedAt === frame.at) return;
    const w = this._waterfall.width;
    const h = this._waterfall.height;
    if (h > 1) this._waterfallCtx.drawImage(this._waterfall, 0, 0, w, h - 1, 0, 1, w, h - 1);
    this._paintRowInto(this._waterfallCtx, frame, 0, w);
    this._lastPushedAt = frame.at;
  }

  // --- drawing ---------------------------------------------------------------

  _scheduleDraw() {
    if (this._drawScheduled) return;
    this._drawScheduled = true;
    requestAnimationFrame(() => {
      this._drawScheduled = false;
      this._draw();
    });
  }

  _viewFrame() {
    const source = this.frame ?? this.status?.span ?? {};
    const n = source.bins?.length || 512;
    const count = Math.max(8, Math.round(n / this._zoom));
    const start = Math.max(0, Math.min(n - count, Math.round(this._pan * n - count / 2)));
    return {...source, _start: start, _count: count, _total: n,
      center_hz: source.center_hz + ((start + count / 2) / n - 0.5) * source.sample_rate_hz,
      sample_rate_hz: source.sample_rate_hz * count / n,
      bins: source.bins?.slice(start, start + count)};
  }

  _setZoom(zoom) {
    if (this._canvasGesture) this._cancelCanvasGesture();
    this._zoom = Math.max(1, Math.min(16, zoom));
    this._onCanvasPointerLeave();
    this._scheduleDraw();
  }

  /** Zoom around the pointer's frequency while keeping that frequency under
   * the pointer. `_pan` remains normalized to the acquired capture, so this
   * never changes the receiver's tuned frequency or survey span. */
  _zoomAt(clientX, canvas, factor) {
    if (this._canvasGesture) this._cancelCanvasGesture();
    const rect = canvas?.getBoundingClientRect();
    const source = this.frame ?? this.status?.span;
    const n = source?.bins?.length || 512;
    if (!rect?.width || !source?.sample_rate_hz) return;
    const x = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const visible = this._viewFrame();
    const oldCount = visible._count;
    const anchorBin = Math.max(0, Math.min(n, visible._start + x * oldCount));
    const nextZoom = Math.max(1, Math.min(16, this._zoom * factor));
    const nextCount = Math.max(8, Math.round(n / nextZoom));
    this._zoom = nextZoom;
    this._pan = Math.max(nextCount / (2 * n), Math.min(1 - nextCount / (2 * n),
      (anchorBin - x * nextCount + nextCount / 2) / n));
    this._onCanvasPointerLeave();
    this._scheduleDraw();
  }

  _onCanvasWheel(ev) {
    ev.preventDefault();
    const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? (ev.currentTarget.clientHeight || 360) : 1;
    this._zoomAt(ev.clientX, ev.currentTarget, Math.exp(-ev.deltaY * unit * 0.002));
  }

  _canvasPointHz(clientX, canvas) {
    const rect = canvas?.getBoundingClientRect();
    const frame = this._viewFrame();
    if (!rect?.width || !frame.sample_rate_hz || frame.center_hz == null) return null;
    const x = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return Math.round(frame.center_hz - frame.sample_rate_hz / 2 + x * frame.sample_rate_hz);
  }

  _onCanvasPointerDown(ev) {
    if (ev.button !== 0 || ev.target !== ev.currentTarget) return;
    const canvas = ev.currentTarget;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height || !this._layout) return;
    const x = (ev.clientX - rect.left) / rect.width * this._layout.w;
    const y = (ev.clientY - rect.top) / rect.height * this._layout.h;
    const frame = this._viewFrame();
    const markerX = this._hzToX(this.status?.span?.vfo_hz, frame, this._layout.w);
    const onSpectrum = y >= this._layout.specTop && y <= this._layout.specTop + this._layout.specH;
    const onWaterfall = y >= this._layout.waterfallTop && y <= this._layout.waterfallTop + this._layout.waterfallH;
    const isMarker = this.connected && this.canControl && !this.status?.replay && markerX != null &&
      (onSpectrum || onWaterfall) &&
      Math.abs(x - markerX) <= 9 * this._layout.dpr;
    // Pan starts in the spectrum pane and on the waterfall. Keep the ruler
    // and occupancy strips available for ordinary pointer use.
    const inPlot = (y >= this._layout.specTop && y <= this._layout.specTop + this._layout.specH) ||
      (y >= this._layout.waterfallTop && y <= this._layout.waterfallTop + this._layout.waterfallH);
    if (!isMarker && !inPlot) return;
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    this._canvasGesture = { pointerId: ev.pointerId, kind: isMarker ? "marker" : "pan",
      canvas, startX: ev.clientX, lastX: ev.clientX, startPan: this._pan,
      startZoom: this._zoom, startHz: this.status?.span?.vfo_hz,
      moved: false, altKey: ev.altKey, shiftKey: ev.shiftKey };
    if (isMarker) this._dragPreviewHz = this._canvasPointHz(ev.clientX, canvas);
  }

  _onCanvasPointerMoveGesture(ev) {
    const gesture = this._canvasGesture;
    if (!gesture || gesture.pointerId !== ev.pointerId) return;
    const delta = ev.clientX - gesture.startX;
    if (!gesture.moved && Math.abs(delta) < 4) return;
    gesture.moved = true;
    gesture.lastX = ev.clientX;
    if (gesture.kind === "marker") {
      this._dragPreviewHz = this._canvasPointHz(ev.clientX, gesture.canvas);
    } else {
      const rect = gesture.canvas.getBoundingClientRect();
      const n = this.frame?.bins?.length || 512;
      const visible = this._viewFrame();
      const count = visible._count;
      const startPan = (visible._start + count / 2) / n;
      this._pan = Math.max(count / (2 * n), Math.min(1 - count / (2 * n),
        startPan - delta / rect.width * count / n));
    }
    this._onCanvasPointerLeave();
    this._scheduleDraw();
  }

  _onCanvasPointerUp(ev) {
    const gesture = this._canvasGesture;
    if (!gesture || gesture.pointerId !== ev.pointerId) return;
    this._canvasGesture = null;
    if (gesture.kind === "marker" && gesture.moved) {
      const hz = this._dragPreviewHz;
      this._dragPreviewHz = null;
      this._suppressCanvasClick = { x: ev.clientX, y: ev.clientY, until: performance.now() + 500 };
      if (hz != null && gesture.shiftKey) {
        const nearest = this._nearestLabel(hz);
        if (nearest?.withinRadius) this._openSignal(nearest.label);
      } else if (hz != null && this.connected && this.canControl && !this.status?.replay) {
        const nearest = gesture.altKey ? null : this._nearestLabel(hz);
        const tuneHz = gesture.altKey ? hz : this._snapOn
          ? (nearest?.withinRadius ? nearest.label.frequency_hz : Math.round(hz / 100) * 100)
          : hz;
        this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz: tuneHz } }));
      }
    } else if (gesture.kind === "pan" && gesture.moved) {
      this._suppressCanvasClick = { x: ev.clientX, y: ev.clientY, until: performance.now() + 500 };
    } else {
      this._dragPreviewHz = null;
    }
    this._scheduleDraw();
  }

  _onCanvasPointerCancel(ev) {
    if (!this._canvasGesture || this._canvasGesture.pointerId !== ev.pointerId) return;
    this._cancelCanvasGesture();
  }

  _cancelCanvasGesture() {
    const gesture = this._canvasGesture;
    if (!gesture) return;
    if (gesture.kind === "pan") this._pan = gesture.startPan;
    this._canvasGesture = null;
    this._dragPreviewHz = null;
    try {
      if (gesture.canvas?.hasPointerCapture?.(gesture.pointerId)) gesture.canvas.releasePointerCapture(gesture.pointerId);
    } catch { /* Capture may already have been released by the browser. */ }
    this._scheduleDraw();
  }

  _onCanvasKeyDown(ev) {
    if (ev.target !== ev.currentTarget) return;
    if (ev.key === "Home") {
      ev.preventDefault(); this._pan = 0.5; this._setZoom(1); return;
    }
    if (["+", "="].includes(ev.key)) { ev.preventDefault(); this._setZoom(this._zoom * 2); return; }
    if (ev.key === "-") { ev.preventDefault(); this._setZoom(this._zoom / 2); return; }
    // Shift+Arrow is the app-wide receiver step command. Leave it unhandled
    // here so the global serial tuning handler can apply its configured step.
    if (ev.shiftKey && ["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
    if (["ArrowLeft", "ArrowRight"].includes(ev.key)) {
      ev.preventDefault();
      const n = this.frame?.bins?.length || 512;
      const count = Math.max(8, Math.round(n / this._zoom));
      const step = count / n / 10;
      this._pan = Math.max(count / (2 * n), Math.min(1 - count / (2 * n),
        this._pan + (ev.key === "ArrowRight" ? step : -step)));
      this._scheduleDraw();
    }
  }

  _startWaterfallResize(ev, spectrum = false) {
    ev.preventDefault();
    const field = spectrum ? "_spectrumHeight" : "_waterfallHeight";
    const key = spectrum ? "msdr.spectrum.height" : "msdr.waterfall.height";
    const min = spectrum ? 80 : 100;
    const max = spectrum ? 800 : 1200;
    const handle = ev.currentTarget;
    handle.setPointerCapture(ev.pointerId);
    const startY = ev.clientY;
    const height = (spectrum ? this._layout.specH : this._layout.waterfallH) / this._layout.dpr;
    const move = (e) => {
      this[field] = Math.round(Math.max(min, Math.min(max, height + e.clientY - startY)));
      this._resizeCanvas();
      this._scheduleDraw();
    };
    const end = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      try { localStorage.setItem(key, String(this[field])); } catch {}
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }

  _hzToX(hz, frame, w) {
    if (hz == null || !frame?.sample_rate_hz || frame.center_hz == null) return null;
    const low = frame.center_hz - frame.sample_rate_hz / 2;
    const frac = (hz - low) / frame.sample_rate_hz;
    if (frac < 0 || frac > 1) return null;
    return frac * w;
  }

  /** Spectrum pane: dark-navy background, 10 dB horizontal grid + labels,
   * MHz vertical grid, the (optionally smoothed) trace with a translucent
   * gradient fill under it, and an optional dimmer peak-hold line. */
  _drawSpectrum(ctx, frame, top, height, w, dpr) {
    ctx.fillStyle = "#0b1220";
    ctx.fillRect(0, top, w, height);

    const minDb = this._wfMin ?? -145;
    const maxDb = this._wfMax ?? -20;
    const range = maxDb - minDb || 1;
    const line = this._cssVar("--line", "#2b323d");
    const ink = this._cssVar("--ink", "#9aa4b0");

    // wf_show_grid (SETUP), settings-only -- no on-canvas override.
    if (this.wfSettings?.wf_show_grid ?? true) {
      // Horizontal dB grid every 10 dB, labelled on the left.
      ctx.strokeStyle = line;
      ctx.fillStyle = ink;
      ctx.font = `${Math.round(10 * dpr)}px ${this._cssVar("--mono", "monospace")}`;
      ctx.textAlign = "left";
      ctx.lineWidth = 1;
      const gridStart = Math.ceil(minDb / 10) * 10;
      for (let db = gridStart; db <= maxDb; db += 10) {
        const y = top + height - ((db - minDb) / range) * height;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
        ctx.textBaseline = y < top + 8 * dpr ? "top" : y > top + height - 8 * dpr ? "bottom" : "middle";
        ctx.fillText(`${db}`, 4, y - 1);
      }

      // Vertical grid at the MHz ticks (shared with the ruler below).
      if (frame?.sample_rate_hz && frame.center_hz != null) {
        ctx.strokeStyle = line;
        for (const t of this._mhzTicks(frame, w, dpr)) {
          ctx.beginPath();
          ctx.moveTo(t.x, top);
          ctx.lineTo(t.x, top + height);
          ctx.stroke();
        }
      }
    }

    if (!frame?.bins?.length) return;
    const bins = this._avgSmooth && this._smoothBins ? this._smoothBins.slice(frame._start, frame._start + frame._count) : frame.bins;
    const peakBins = this._peakBins?.slice(frame._start, frame._start + frame._count);
    const n = bins.length;
    const stepX = w / n;
    const baseDb = frame.base_db ?? minDb;
    const refDb = frame.ref_level_db ?? maxDb;
    const yFor = (v) => {
      const t = Math.max(0, Math.min(1, (binToDb(v, baseDb, refDb) - minDb) / range));
      return top + height - t * height;
    };
    const trace = this._cssVar("--trace", "#3399ff");

    // Translucent gradient fill under the trace (one gradient fill, no
    // per-pixel loop): trace color at ~35% alpha at the top, fading to 0 at
    // the pane's bottom.
    const grad = ctx.createLinearGradient(0, top, 0, top + height);
    grad.addColorStop(0, this._withAlpha(trace, 0.35));
    grad.addColorStop(1, this._withAlpha(trace, 0));
    ctx.beginPath();
    ctx.moveTo(0, yFor(bins[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(i * stepX, yFor(bins[i]));
    ctx.lineTo(w, top + height);
    ctx.lineTo(0, top + height);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Peak-hold: thin, dimmer line, drawn under the live trace.
    if (this._peakHold && this._peakBins) {
      ctx.strokeStyle = this._withAlpha(trace, 0.45);
      ctx.lineWidth = Math.max(1, Math.round(0.75 * dpr));
      ctx.beginPath();
      ctx.moveTo(0, yFor(peakBins[0]));
      for (let i = 1; i < n; i++) ctx.lineTo(i * stepX, yFor(peakBins[i]));
      ctx.stroke();
    }

    // Live trace, 1.5 px in SDRconnect's azure blue (--trace).
    ctx.strokeStyle = trace;
    ctx.lineWidth = Math.max(1, 1.5 * dpr);
    ctx.beginPath();
    ctx.moveTo(0, yFor(bins[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(i * stepX, yFor(bins[i]));
    ctx.stroke();
  }

  /** Occupancy strip: same accent hue as the trace but alpha-only ("heat"),
   * not the multi-color waterfall palette, so it reads as a summary rather
   * than a second waterfall. */
  _drawHeatStrip(ctx, values, top, height, w) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, top, w, height);
    if (!values || !values.length) return;
    const n = values.length;
    const colW = w / n;
    const accent = this._cssVar("--accent", "#3ddbd9");
    for (let i = 0; i < n; i++) {
      const frac = Math.max(0, Math.min(1, Number(values[i]) || 0));
      if (frac <= 0) continue;
      ctx.fillStyle = this._withAlpha(accent, 0.15 + frac * 0.65);
      ctx.fillRect(Math.floor(i * colW), top, Math.ceil(colW) + 1, height);
    }
  }

  /** Frequency ruler between the occupancy strips and the waterfall: minor
   * ticks plus labelled major ticks, spaced so labels stay >=70 css px apart. */
  _drawRuler(ctx, frame, top, height, w, dpr) {
    ctx.fillStyle = this._cssVar("--panel-well", "#0c0e11");
    ctx.fillRect(0, top, w, height);
    if (!frame?.sample_rate_hz || frame.center_hz == null) return;
    const spanHz = frame.sample_rate_hz;
    const low = frame.center_hz - spanHz / 2;
    const step = this._tickStepHz(spanHz, w / dpr);
    const minorStep = step / 5;
    const line = this._cssVar("--line", "#2b323d");
    ctx.strokeStyle = line;
    ctx.lineWidth = 1;
    const minorStart = Math.ceil(low / minorStep) * minorStep;
    for (let hz = minorStart; hz <= low + spanHz; hz += minorStep) {
      const x = ((hz - low) / spanHz) * w;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + height * 0.4);
      ctx.stroke();
    }
    ctx.fillStyle = this._cssVar("--ink", "#9aa4b0");
    ctx.font = `${Math.round(10 * dpr)}px ${this._cssVar("--mono", "monospace")}`;
    ctx.textBaseline = "middle";
    for (const t of this._mhzTicks(frame, w, dpr)) {
      ctx.beginPath();
      ctx.moveTo(t.x, top);
      ctx.lineTo(t.x, top + height);
      ctx.stroke();
      ctx.textAlign = t.x < 20 * dpr ? "left" : t.x > w - 20 * dpr ? "right" : "center";
      ctx.fillText(t.label, t.x, top + height / 2);
    }
    ctx.textAlign = "left";
  }

  /** VFO marker: a --fg line plus a translucent shaded band for the current
   * filter bandwidth, drawn across both the spectrum pane and the waterfall
   * (skipping the occupancy strips/ruler in between). */
  _drawVfoMarker(ctx, frame, w, layout) {
    const vfoHz = this._dragPreviewHz ?? this.status?.span?.vfo_hz;
    const x = this._hzToX(vfoHz, frame, w);
    if (x == null) return;
    const fg = this._cssVar("--fg", "#c9d1d9");
    const bwHz = this.filterBandwidthHz || 12500;
    const halfW = (bwHz / frame.sample_rate_hz) * w / 2;

    const drawSeg = (top, height) => {
      if (halfW > 0) {
        ctx.fillStyle = this._withAlpha(fg, 0.12);
        ctx.fillRect(x - halfW, top, halfW * 2, height);
      }
      ctx.strokeStyle = fg;
      ctx.lineWidth = Math.max(1, Math.round(window.devicePixelRatio || 1));
      ctx.beginPath();
      ctx.moveTo(x + 0.5, top);
      ctx.lineTo(x + 0.5, top + height);
      ctx.stroke();
    };

    drawSeg(layout.specTop, layout.specH);
    drawSeg(layout.waterfallTop, layout.waterfallH);
  }

  /** Hover feedback: a low-alpha vertical band across the spectrum pane +
   * waterfall spanning the hovered label's width (when the cursor is near
   * one), plus a thin cursor line at the exact hovered frequency. Mirrors
   * _drawVfoMarker's two-segment draw (skipping the occupancy strips/ruler). */
  _drawHoverOverlay(ctx, frame, w, layout) {
    if (this._hoverHz == null) return;
    const trace = this._cssVar("--trace", "#3399ff");
    const nearest = this._hoverNearest;
    if (nearest?.withinRadius) {
      const label = nearest.label;
      const binHz = this._analyzerBinToleranceHz();
      const halfHz = Math.max((label.width_hz ?? 0) / 2, binHz / 2);
      const loX = this._hzToX(label.frequency_hz - halfHz, frame, w);
      const hiX = this._hzToX(label.frequency_hz + halfHz, frame, w);
      if (loX != null && hiX != null) {
        ctx.fillStyle = this._withAlpha(trace, 0.12);
        const drawBand = (top, height) => ctx.fillRect(loX, top, Math.max(1, hiX - loX), height);
        drawBand(layout.specTop, layout.specH);
        drawBand(layout.waterfallTop, layout.waterfallH);
      }
    }
    const x = this._hzToX(this._hoverHz, frame, w);
    if (x != null) {
      ctx.strokeStyle = this._withAlpha(trace, 0.6);
      ctx.lineWidth = 1;
      const drawLine = (top, height) => {
        ctx.beginPath();
        ctx.moveTo(x + 0.5, top);
        ctx.lineTo(x + 0.5, top + height);
        ctx.stroke();
      };
      drawLine(layout.specTop, layout.specH);
      drawLine(layout.waterfallTop, layout.waterfallH);
    }
  }

  _draw() {
    const canvas = this.querySelector("canvas.survey-canvas");
    if (!canvas || !this._layout) return;
    const ctx = canvas.getContext("2d");
    const layout = this._layout;
    const { w, h, dpr, specH, heatH, rulerH, waterfallH, specTop, shortTop, longTop, rulerTop, waterfallTop } = layout;
    ctx.clearRect(0, 0, w, h);

    const frame = this._viewFrame();

    this._drawSpectrum(ctx, frame, specTop, specH, w, dpr);
    this._drawHeatStrip(ctx, this.status?.occupancy?.short?.slice(frame._start, frame._start + frame._count), shortTop, heatH, w);
    this._drawHeatStrip(ctx, this.status?.occupancy?.long?.slice(frame._start, frame._start + frame._count), longTop, heatH, w);
    this._drawRuler(ctx, frame, rulerTop, rulerH, w, dpr);
    if (this._waterfall) {
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(this._waterfall, frame._start, 0, frame._count, this._waterfall.height, 0, waterfallTop, w, waterfallH);
    }
    this._drawCenterExclusion(ctx, frame, w, layout);
    this._drawEvidenceMarkers(ctx, frame, w, layout);
    this._drawVfoMarker(ctx, frame, w, layout);
    this._drawHoverOverlay(ctx, frame, w, layout);
  }

  _drawCenterExclusion(ctx, frame, w, {specTop, specH, dpr}) {
    const exclusion = this.status?.peak_detection?.center_exclusion;
    if (!exclusion || !frame?.sample_rate_hz) return;
    const low = frame.center_hz - frame.sample_rate_hz / 2;
    const left = Math.max(0, (exclusion.start_hz - low) / frame.sample_rate_hz * w);
    const right = Math.min(w, (exclusion.end_hz - low) / frame.sample_rate_hz * w);
    if (right <= left) return;
    ctx.save();
    ctx.fillStyle = "rgba(255,196,107,.16)";
    ctx.fillRect(left, specTop, right - left, specH);
    ctx.strokeStyle = "#ffc46b";
    ctx.setLineDash([3 * dpr, 3 * dpr]);
    ctx.strokeRect(left, specTop, right - left, specH);
    ctx.restore();
  }

  _drawEvidenceMarkers(ctx, frame, w, {specTop, specH, dpr}) {
    if (!Number.isFinite(frame.center_hz) || !(frame.sample_rate_hz > 0)) return;
    const low = frame.center_hz - frame.sample_rate_hz / 2;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, specTop, w, specH);
    ctx.clip();
    ctx.font = `${9 * dpr}px monospace`;
    ctx.textAlign = "center";
    const maxSources = Math.max(0, Math.floor((specH / dpr - 4) / 13));
    for (const marker of clusteredEvidenceMarkers(this._spectrumLabels(), low, frame.sample_rate_hz, w, 28 * dpr)) {
      const shown = marker.sources.slice(0, maxSources);
      if (!shown.length) continue;
      ctx.globalAlpha = marker.opacity ?? 1;
      const bottom = specTop + (16 + (shown.length - 1) * 13) * dpr;
      const minDb = this._wfMin ?? -145, maxDb = this._wfMax ?? -20;
      const bins = this._avgSmooth && this._smoothBins
        ? this._smoothBins.slice(frame._start, frame._start + frame._count) : frame.bins;
      const yAt = x => {
        if (!bins?.length) return Math.min(specTop + specH - 3 * dpr, bottom + 8 * dpr);
        const i = Math.max(0, Math.min(bins.length - 1, Math.round(x / w * bins.length)));
        const db = binToDb(bins[i], frame.base_db ?? minDb, frame.ref_level_db ?? maxDb);
        return specTop + specH * (1 - Math.max(0, Math.min(1, (db - minDb) / (maxDb - minDb || 1))));
      };
      ctx.strokeStyle = "#7c929c";
      ctx.lineWidth = dpr;
      ctx.setLineDash([]);
      for (const [index, anchorX] of marker.anchors.entries()) {
        const anchor = marker.anchor_styles?.[index];
        ctx.globalAlpha = anchor?.opacity ?? 1;
        const anchorY = anchor?.retained && Number.isFinite(anchor.level)
          ? specTop + specH * (1 - Math.max(0, Math.min(1,
              (binToDb(anchor.level, frame.base_db ?? minDb, frame.ref_level_db ?? maxDb) - minDb) / (maxDb - minDb || 1))))
          : yAt(anchorX);
        ctx.setLineDash(anchor?.retained ? [2 * dpr, 3 * dpr] : []);
        ctx.beginPath();
        ctx.moveTo(marker.x, bottom);
        ctx.lineTo(anchorX, anchorY);

        ctx.stroke();
        ctx.fillStyle = "#3ddbd9";
        ctx.setLineDash([]);
        ctx.fillRect(anchorX - 2 * dpr, anchorY - 2 * dpr, 4 * dpr, 4 * dpr);
      }
      ctx.globalAlpha = marker.opacity ?? 1;
      shown.forEach((badge, index) => {
        const x = marker.x;
        const y = specTop + (12 + index * 13) * dpr;
        ctx.fillStyle = "#101419";
        ctx.fillRect(x - 10 * dpr, y - 9 * dpr, 20 * dpr, 12 * dpr);
        ctx.fillStyle = badge.color;
        ctx.fillText(badge.code, x, y);
        ctx.fillRect(x - 8 * dpr, y + 2 * dpr, 16 * dpr, dpr);
      });
    }
    ctx.restore();
  }

  _renderEvidenceBadges(label) {
    const badges = evidenceBadges(label);
    return badges.length ? html`<span class="survey-evidence-badges">${badges.map(b => html`
      <span class="survey-evidence-badge" data-evidence-source=${b.source}
        style=${`--evidence-color:${b.color}`} title=${b.title}>${b.symbol} ${b.text}</span>
    `)}</span>` : "—";
  }

  _renderEvidenceLegend() {
    return html`<details class="survey-evidence-legend"><summary>Evidence colors / spectrum markers</summary>
      ${Object.entries(EVIDENCE_SOURCES).map(([source, b]) => html`
        <span class="survey-evidence-badge" data-evidence-source=${source}
          style=${`--evidence-color:${b.color}`}>${b.code} · ${b.text}</span>
      `)}
      <div class="dim">✓ passed checks · ? tentative / context · ⚠ disagreement or failed check.
        Allocation and schedule do not confirm reception.
        Hover a signal for evidence details. Waterfall color shows signal strength.</div>
    </details>`;
  }

  _proposalMatch(candidate) {
    const labels = this.status?.labels ?? [];
    const center = (candidate.low_hz + candidate.high_hz) / 2;
    return labels.filter(label => {
      const halfWidth = Math.max(0, Number(label.width_hz) || Number(this.status?.resolution_hz) || 0) / 2;
      return label.frequency_hz + halfWidth >= candidate.low_hz && label.frequency_hz - halfWidth <= candidate.high_hz;
    }).sort((a, b) => Math.abs(a.frequency_hz - center) - Math.abs(b.frequency_hz - center))[0] ?? null;
  }

  _renderMlProposals() {
    const detector = this.status?.analyzer?.rfml_detector;
    if (!detector || detector.state === "disabled") return "";
    const result = detector.result;
    const candidates = (this.status?.ml_proposals?.map(p => p.rfml_detection) ?? result?.candidates ?? []).filter(c =>
      Number.isFinite(c.low_hz) && Number.isFinite(c.high_hz) && c.low_hz > 0 && c.high_hz > c.low_hz);
    const age = Number.isFinite(detector.result_age_s)
      ? `${Math.round(detector.result_age_s)} s old${detector.result_stale ? " · stale" : ""}`
      : "age unavailable";
    const sampled = Number.isFinite(detector.sample_coverage) ? `${(detector.sample_coverage * 100).toFixed(1)}% IQ sampled` : null;
    const frequencyCoverage = Number.isFinite(result?.frequency_coverage) ? `${(result.frequency_coverage * 100).toFixed(1)}% of span analyzed` : null;
    const resolution = Number.isFinite(result?.bin_width_hz) ? `${Math.round(result.bin_width_hz)} Hz/bin` : null;
    return html`<details class="survey-ml-proposals">
      <summary>ML proposals · ${candidates.length} · unvalidated</summary>
      <div class="dim survey-ml-meta">Experimental detector · raw scores are uncalibrated, not identity probabilities · ${age}
        ${sampled ? html` · ${sampled}` : ""}${frequencyCoverage ? html` · ${frequencyCoverage}` : ""}${resolution ? html` · ${resolution}` : ""}
      </div>
      ${candidates.length ? html`<div class="lib-table-wrap"><table class="lib-table mono survey-ml-table">
        <thead><tr><th>RANGE MHz</th><th>PROPOSAL</th><th>WIDTH</th><th>RAW SCORE</th><th>DSP ROW</th><th>ACTIONS</th></tr></thead>
        <tbody>${candidates.map(candidate => {
          const center = (candidate.low_hz + candidate.high_hz) / 2;
          const match = this._proposalMatch(candidate);
          const label = candidate.evidence?.label ?? "signal";
          return html`<tr>
            <td>${(candidate.low_hz / 1e6).toFixed(3)}–${(candidate.high_hz / 1e6).toFixed(3)}</td>
            <td title="Grouped frequency region; fragments do not imply continuous activity. No RF field validation.">${label} · unvalidated${candidate.member_count > 1 ? ` · ${candidate.member_count} fragments` : ""}</td>
            <td>${Math.round(candidate.high_hz - candidate.low_hz)} Hz</td>
            <td title="Uncalibrated source score, not a probability">${Number.isFinite(candidate.score) ? candidate.score.toFixed(3) : "—"}</td>
            <td>${match ? html`<button title="Open overlapping measured signal" @click=${() => this._openSignal(match)}>${mhz(match.frequency_hz)} · measured</button>` : "—"}</td>
            <td><button title="Inspect proposal in signal detail" @click=${() => this._openSignal(match ?? {frequency_hz: center})}>INSPECT</button>
              <button title="Tune proposal center frequency" ?disabled=${!!this.status?.replay} @click=${() => this._onBadgeClick(center)}>TUNE</button></td>
          </tr>`;
        })}</tbody>
      </table></div>` : html`<div class="dim survey-ml-meta">${result ? "No ML proposals in the latest window" : "No recent detector result"}</div>`}
    </details>`;
  }

  _renderMlStatusStrip() {
    const classifier = this.status?.analyzer?.rfml;
    const detector = this.status?.analyzer?.rfml_detector;
    const config = this.status?.ml_config ?? {};
    const classifierResult = (this.status?.labels ?? []).some(label => label.rfml && label.rfml.status === "ok" && !label.rfml.stale);
    const rows = [
      ["Classifier", classifier, classifierResult, classifier?.last_result_age_s, "survey.ml.enabled", config.classifier_enabled],
      ["Detector", detector, !!detector?.result, detector?.result_age_s, "survey.ml.detector.enabled", config.detector_enabled],
    ];
    return html`<p class="dim">Hiding these controls does not stop enabled models.</p><div class="survey-ml-status" role="status" aria-label="ML worker status">
      ${rows.map(([name, worker, hasResult, ageSeconds, configKey, configured]) => {
        const phase = mlWorkerPhase(worker, hasResult);
        const details = [
          worker?.model_id ? `model ${worker.model_id}` : null,
          worker?.device ? `backend ${worker.device}` : null,
          Number.isFinite(ageSeconds) ? `last result ${Math.round(ageSeconds)} s ago` : null,
          Number.isFinite(worker?.sample_coverage) ? `${(worker.sample_coverage * 100).toFixed(1)}% IQ sampled` : null,
          name === "Detector" && Number.isFinite(worker?.effective_window_s)
            ? `${Math.round(worker.effective_window_s * 1000)} ms window` : null,
          name === "Detector" && Number.isFinite(worker?.missed_temporal_fraction)
            ? `${(worker.missed_temporal_fraction * 100).toFixed(1)}% IQ not inferred` : null,
          name === "Detector" && Number.isFinite(worker?.processing_duty)
            ? `${(worker.processing_duty * 100).toFixed(1)}% worker busy` : null,
          name === "Detector" && Number.isFinite(worker?.latency_ms)
            ? `${Math.round(worker.latency_ms)} ms processing` : null,
          Number.isFinite(worker?.inference_coverage) ? `${(worker.inference_coverage * 100).toFixed(1)}% IQ inferred` : null,
          name === "Detector" && Number.isFinite(worker?.result?.frequency_coverage)
            ? `${(worker.result.frequency_coverage * 100).toFixed(1)}% span covered` : null,
          name === "Detector" && Number.isFinite(worker?.result?.bin_width_hz)
            ? `${Math.round(worker.result.bin_width_hz)} Hz/bin` : null,
        ].filter(Boolean);
        return html`<div class="survey-ml-worker" data-ml-worker=${name.toLowerCase()}>
          <label class="survey-ml-toggle">
            <input type="checkbox" role="switch" aria-label=${`Enable ML ${name.toLowerCase()}`}
              .checked=${!!configured} ?disabled=${!!this.mlBusy || !this.status}
              @change=${(event) => {
                const enabled = event.target.checked;
                event.target.checked = !!configured;
                this.dispatchEvent(new CustomEvent("survey-ml", {
                  detail: { [name === "Classifier" ? "classifier_enabled" : "detector_enabled"]: enabled },
                }));
              }} />
            <strong>${name}</strong>
          </label><span class=${`survey-ml-phase phase-${phase.toLowerCase().replaceAll(" ", "-")}`}>${phase}</span>
          ${details.length ? html`<span class="dim">${details.join(" · ")}</span>` : ""}
          ${worker?.device_note ? html`<span class="dim" title=${worker.device_note}>CPU safety fallback</span>` : ""}
          ${phase === "ERROR" && worker?.error ? html`<span class="dim" title=${worker.error}>${worker.error}</span>` : ""}
          ${phase === "OFF" ? html`<span class="dim">${configured ? "Enabled · start Survey to run ML" : "Switch on to enable"}</span>` : ""}
        </div>`;
      })}
      <label class="survey-ml-help">Detector coverage
        <select aria-label="Detector coverage" .value=${String(config.detector_interval_s ?? 2)}
          ?disabled=${!!this.mlBusy || !this.status}
          @change=${(event) => {
            const interval = Number(event.target.value);
            event.target.value = String(config.detector_interval_s ?? 2);
            this.dispatchEvent(new CustomEvent("survey-ml", {detail: {detector_interval_s: interval}}));
          }}>
          <option value="2">Low CPU · 2 s interval</option>
          <option value="0.5">Balanced · 0.5 s interval</option>
          <option value="0.25">Higher coverage · 0.25 s interval</option>
          ${![2, 0.5, 0.25].includes(config.detector_interval_s ?? 2)
            ? html`<option value=${String(config.detector_interval_s)}>Custom · ${config.detector_interval_s} s interval</option>` : ""}
        </select>
      </label>
      ${this.mlBusy ? html`<span class="dim" role="status">Saving ML settings…</span>` : ""}
      <details class="survey-ml-help"><summary>ML setup and status details</summary>
        <p>If a worker reports missing dependencies or weights, run these once from the msdr checkout, in the same Python environment as msdr:</p>
        <pre>python -m pip install '.[ml,ml-detector]'
msdr-rfml download
msdr-rfml download-detector</pre>
        <span>Classifier checks tracked narrowband signals; detector proposes wideband regions. Both are experimental and unvalidated. Scores are raw and uncalibrated. Use the switches to enable them independently. Changes are saved and applied immediately; no server restart is needed. Missing dependencies or model files appear as ERROR. Detector windows default to 100 ms. IQ sampled/inferred measures temporal coverage of IQ delivered to detector capture since the last reset; IQ not inferred includes skipped or pending samples and excludes upstream losses. Shorter coverage intervals use more CPU; actual coverage depends on capture time and processing speed. Settings apply immediately. Worker busy is processing wall time, not CPU utilization. Span covered measures frequency coverage of the latest result.</span>
      </details>
    </div>`;
  }

  async _captureValidation(event) {
    event.preventDefault();
    if (this._captureBusy) return;
    const values = new FormData(event.target);
    values.set("duration_s", this._captureKind === "channel" ? "0" : String(this._captureDuration ?? 0));
    this._captureBusy = true;
    this._captureMessage = "Waiting for a contiguous IQ window (up to 30 seconds)…";
    try {
      const saved = await api.captureValidation({ ...Object.fromEntries(values), duration_s: Number(values.get("duration_s")) });
      this._captureMessage = `Saved ${saved.duration_s.toFixed(4)} s · ${saved.path}. Review labels before evaluation.`;
    } catch (error) {
      this._captureMessage = `Capture failed: ${error.message}`;
    } finally {
      this._captureBusy = false;
    }
  }

  _renderValidationCapture() {
    return html`<details class="survey-validation">
      <summary><span class="survey-validation-title">◇ IQ validation sample</span><span class="survey-validation-purpose">${this._captureBusy ? "Capturing IQ…" : this._captureMessage || "Save IQ for DSP, ML, or decoder checks"}</span></summary>
      <p class="survey-validation-help">Capture what the receiver sees now and record what you heard or observed. Review this sample later against DSP, ML, and protocol decoders.</p>
      <form @submit=${event => this._captureValidation(event)}>
        <label>Capture <select name="kind" aria-label="Validation input" @change=${e => { this._captureKind = e.target.value; }}>
          <option value="wideband">Full visible span · next IQ frame</option>
          <option value="channel">Analyzed signal · up to 3 windows</option>
        </select></label>
        <label>Duration <select aria-label="Validation duration" ?disabled=${this._captureKind === "channel"}
          .value=${String(this._captureKind === "channel" ? 0 : this._captureDuration ?? 0)}
          @change=${e => { this._captureDuration = Number(e.target.value); }}>
          <option value="0">Quick snapshot</option><option value="1">1 second</option>
          <option value="3">3 seconds</option><option value="5">5 seconds</option>
        </select></label>
        <label>Your observation <select name="label" aria-label="Validation label">
          <option value="unlabeled">Not labeled yet</option><option value="voice">Voice</option>
          <option value="digital">Digital</option><option value="noise">Suspected noise</option>
          <option value="interference">Suspected interference</option>
          <option value="uncertain">Uncertain</option>
        </select></label>
        <label class="survey-validation-notes">Notes <span class="dim">(optional)</span><input name="notes" maxlength="2000" placeholder="Frequency, signal, or uncertainty…"></label>
        <button class="survey-validation-save" type="submit" ?disabled=${this._captureBusy || !this.status?.active}>
          ${this._captureBusy ? "Capturing…" : "Save sample"}
        </button>
      </form>
      ${this._captureDuration && this._captureKind !== "channel" ? html`<p class="dim survey-validation-note">About ${Math.ceil((this.status?.span?.sample_rate_hz ?? this.frame?.sample_rate_hz ?? 6000000) * this._captureDuration * 8 / 1000000)} MB of IQ before compression. Keep the receiver tuned until capture finishes.</p>` : ""}
      <p class="dim survey-validation-note">IQ analysis must be running. Saves locally (512 MiB limit); brief captures may miss bursts.</p>
      ${this._captureMessage ? html`<div class="survey-validation-result" role="status">${this._captureMessage}</div>` : ""}
    </details>`;
  }

  // --- span picker / controls ------------------------------------------------

  _toggle() {
    if (this.status?.active) this.dispatchEvent(new CustomEvent("survey-stop"));
    else this.dispatchEvent(new CustomEvent("survey-start"));
  }

  _stepSpan(direction) {
    const span = this.status?.span;
    const sampleRateHz = this._dirty && this._sampleRateHz != null
      ? this._sampleRateHz
      : span?.sample_rate_hz ?? this._sampleRateHz ?? this.status?.sample_rates_hz?.[0];
    const baseCenterHz = span?.center_hz ?? this.receiverHz;
    if (this.status?.replay || !this.connected || !Number.isFinite(baseCenterHz)
      || !Number.isFinite(sampleRateHz) || sampleRateHz <= 0) return;
    const draftHz = parseFrequencyInput(this._centerText, "mhz");
    if (this._dirty && this._centerText.trim() && draftHz == null) {
      const input = this.querySelector('[aria-label="Survey center frequency (MHz)"]');
      input?.setCustomValidity("Enter a positive center frequency in MHz, kHz, or Hz");
      input?.reportValidity();
      return;
    }
    const currentHz = this._dirty && draftHz != null ? draftHz : baseCenterHz;
    const centerHz = Math.round(currentHz + direction * sampleRateHz / 2);
    if (centerHz <= 0) return;
    this._centerText = (centerHz / 1_000_000).toFixed(6);
    this._sampleRateHz = sampleRateHz;
    this._dirty = true;
    this.dispatchEvent(new CustomEvent("survey-span", {
      detail: { sample_rate_hz: sampleRateHz, center_hz: centerHz, keep_vfo: this._keepVfo },
    }));
  }

  _setPeakThreshold(db) {
    if (this.peakBusy || !Number.isFinite(db)) return;
    this.dispatchEvent(new CustomEvent("survey-peak-threshold", {
      detail: { db: Math.max(0, Math.min(60, db)) },
    }));
  }

  _spectrumLabels() {
    const rich = new Map((this.status?.labels ?? []).map(label => [label.id, label]));
    const markers = this.status?.spectrum_markers ?? this.status?.labels ?? [];
    const hold = this.wfSettings?.marker_hold_s ?? 15;
    const threshold = this.wfSettings?.peak_threshold_db ?? 10;
    const now = Date.now() / 1000;
    const tracked = markers.flatMap(marker => {
      const retained = marker.active_now === false || this.status?.active === false || this.status?.stream?.connected === false;
      const age = Number.isFinite(marker.last_seen) ? Math.max(0, now - marker.last_seen) : Infinity;
      if (retained && (!(hold > 0) || age >= hold)) return [];
      if (!retained && Number.isFinite(marker.snr_db) && marker.snr_db <= threshold) return [];
      return [{...rich.get(marker.id), ...marker, active_now: retained ? false : marker.active_now,
        _markerRetained: retained, _markerAge: retained ? age : 0,
        _markerOpacity: retained ? Math.max(.15, .65 * (1 - age / hold)) : 1,
      }];
    });
    // RFML detector proposals with no tracked spectrum peak (ml_only) have no
    // marker of their own -- surface them here too (table/_nearestLabel/
    // _labelAtMarker share this list), skipping any that overlap a tracked
    // label's frequency span so the same signal isn't shown twice.
    const mlOnly = (this.status?.ml_proposals ?? []).filter(proposal => proposal.ml_only
      && !tracked.some(label => {
        const halfWidth = Math.max(0, Number(label.width_hz) || Number(this.status?.resolution_hz) || 0) / 2;
        return label.frequency_hz + halfWidth >= proposal.low_hz && label.frequency_hz - halfWidth <= proposal.high_hz;
      }));
    return [...tracked, ...mlOnly];
  }

  _applySpan() {
    const input = this.querySelector('[aria-label="Survey center frequency (MHz)"]');
    const rawCenter = this._centerText.trim();
    const centerHz = rawCenter
      ? parseFrequencyInput(rawCenter, "mhz")
      : this.status?.span?.center_hz ?? this.receiverHz;
    const sampleRateHz = this._sampleRateHz ?? this.status?.span?.sample_rate_hz ?? this.status?.sample_rates_hz?.[0];
    const reason = this.status?.replay
      ? "Survey span cannot be changed during replay"
      : !this.connected
        ? "Connect the receiver before applying a survey span"
        : !Number.isFinite(centerHz) || centerHz <= 0
          ? "Enter a positive center frequency in MHz, kHz, or Hz"
          : !Number.isFinite(sampleRateHz) || sampleRateHz <= 0
            ? "Choose a valid survey sampling rate"
            : "";
    input?.setCustomValidity(reason);
    if (reason) {
      input?.reportValidity();
      return false;
    }
    this._dirty = false;
    this._centerText = (centerHz / 1_000_000).toFixed(6);
    this.dispatchEvent(
      new CustomEvent("survey-span", {
        detail: { sample_rate_hz: sampleRateHz, center_hz: centerHz, keep_vfo: this._keepVfo },
      })
    );
    return true;
  }

  _applyPreset(name) {
    const preset = (this.status?.presets ?? []).find((p) => p.name === name);
    if (!preset) return;
    this._sampleRateHz = preset.sample_rate_hz;
    this._centerText = (preset.center_hz / 1_000_000).toFixed(6);
    this._dirty = false;
    this.dispatchEvent(
      new CustomEvent("survey-span", {
        detail: {
          sample_rate_hz: preset.sample_rate_hz,
          center_hz: preset.center_hz,
          keep_vfo: this._keepVfo,
        },
      })
    );
  }

  /** Canvas click-to-tune. Snap on (default, SNAP button/localStorage) tunes
   * to the nearest label's frequency_hz when one is within the snap radius
   * (_nearestLabel), else to the clicked frequency rounded to 100 Hz.
   * Alt/Option-click always tunes to the exact clicked frequency, bypassing
   * snap. Shift-click opens the signal panel for the nearest label instead of
   * tuning at all.
   *
   * The first click acts immediately; a following double-click also opens
   * inspection after that first tune. Shift-click opens inspection without
   * tuning. Every click (any modifier) also updates the selection highlight:
   * a nearby label gets selected (table row + badge), a click with nothing
   * nearby clears it. While replaying (status.replay set) tuning is skipped
   * entirely -- replay isn't the live radio, so a click only selects. */
  _queueCanvasClick(ev) {
    if (this._suppressCanvasClick && performance.now() <= this._suppressCanvasClick.until &&
        Math.abs(ev.clientX - this._suppressCanvasClick.x) < 8 &&
        Math.abs(ev.clientY - this._suppressCanvasClick.y) < 8) {
      this._suppressCanvasClick = null;
      return;
    }
    if (ev.detail > 1) return; // the first click already tuned; dblclick below inspects
    const click = {currentTarget: ev.currentTarget, clientX: ev.clientX,
      clientY: ev.clientY, markerLabel: this._markerLabelAtPoint(ev.clientX, ev.clientY, ev.currentTarget),
      shiftKey: ev.shiftKey, altKey: ev.altKey};
    // Tune on the first click for immediate feedback. A following double
    // click opens inspection as well; Shift-click remains tune-free.
    this._onCanvasClick(click);
  }

  _onCanvasDoubleClick(ev) {
    this._onCanvasClick({currentTarget: ev.currentTarget, clientX: ev.clientX,
      clientY: ev.clientY, markerLabel: this._markerLabelAtPoint(ev.clientX, ev.clientY, ev.currentTarget),
      shiftKey: true, altKey: false});
  }

  _onCanvasClick(ev) {
    const canvas = ev.currentTarget;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const frame = this._viewFrame();
    if (!frame.sample_rate_hz || frame.center_hz == null) return;
    const frac = (ev.clientX - rect.left) / rect.width;
    const clickedHz = Math.round(frame.center_hz - frame.sample_rate_hz / 2 + frac * frame.sample_rate_hz);
    const markerLabel = ev.markerLabel ?? this._markerLabelAtPoint(ev.clientX, ev.clientY, canvas);
    const hz = markerLabel && !ev.altKey ? markerLabel.frequency_hz : clickedHz;
    const nearest = markerLabel && !ev.altKey
      ? { label: markerLabel, withinRadius: true }
      : this._nearestLabel(hz);
    this._selectLabel(nearest?.withinRadius ? nearest.label.id : null);

    if (ev.shiftKey) {
      if (nearest?.withinRadius) this._openSignal(nearest.label);
      return;
    }

    if (this.status?.replay || !this.connected || !this.canControl) return; // replay/read-only/disconnected: selection only

    if (ev.altKey) {
      this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
      return;
    }

    if (this._snapOn) {
      const snappedHz = nearest?.withinRadius ? nearest.label.frequency_hz : Math.round(hz / 100) * 100;
      this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz: snappedHz } }));
      return;
    }

    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
  }

  /** Select the table row and badge without moving the page or keyboard focus.
   * Cleared by a click with nothing nearby or Esc (see connectedCallback). */
  _selectLabel(id) {
    this._selectedLabelId = id;
  }

  // --- badge lane + labels table (S2) -----------------------------------
  // Label shape: msdr/survey/survey.py Survey.status()["labels"] (see the
  // plan's Contracts section) -- frequency_hz, width_hz, snr_db, active_now,
  // activity{kind,duty,period_s,...}, bw_class, bandplan{name,...}, catalog[],
  // first_seen, last_seen (epoch seconds, unlike the ISO timestamps elsewhere
  // in this app).

  /** Current span (low/high Hz), from the latest frame or, before one has
   * arrived, the status's span -- same fallback _onCanvasClick uses. */
  _currentSpan() {
    const frame = this._viewFrame();
    if (!frame.sample_rate_hz || frame.center_hz == null) return null;
    return {
      low: frame.center_hz - frame.sample_rate_hz / 2,
      high: frame.center_hz + frame.sample_rate_hz / 2,
    };
  }

  /** Offscreen 2D context reused for canvas.measureText chip-width
   * estimates (memoized -- no visible canvas needed). */
  _measureCtx() {
    if (!this._measureCanvasCtx) this._measureCanvasCtx = document.createElement("canvas").getContext("2d");
    return this._measureCanvasCtx;
  }

  /** Chip label text -- MHz + activity glyph, plus bw class only once
   * classification has actually resolved (survey.py's classify.py can
   * report "unresolved"; showing that in a tiny chip added noise for no
   * information), plus family/voice when known. Shorter text also means
   * narrower chips, which is most of the collision fix. */
  _chipText(label) {
    if (hasCwOutput(label)) return `${(label.frequency_hz / 1_000_000).toFixed(3)} · CW · decoder output`;
    const glyph = ACTIVITY_GLYPHS[label.activity?.kind] ?? "";
    const family = label.family?.name ? shortFamilyName(label.family.name) : null;
    const voiceGlyph = this._signalActivity(label).speechDetected ? VOICE_GLYPHS.voice : null;
    const resolvedBw = label.bw_class && label.bw_class !== "unresolved" ? label.bw_class : null;
    let text = `${(label.frequency_hz / 1_000_000).toFixed(3)} · ${glyph}`;
    if (resolvedBw) text += ` · ${resolvedBw}`;
    if (family) text += ` · ${family}`;
    if (voiceGlyph) text += ` ${voiceGlyph}`;
    if (label.decoding) text += " ⟳";
    return text;
  }

  /** Chip width in CSS px: canvas.measureText against the chip's actual
   * font (the lane is light-DOM, so it inherits this component's computed
   * font-size; the chip itself is 0.72em) plus its border/padding chrome
   * and the confirmed-checkmark prefix when present. */
  _chipWidthPx(text, confirmed) {
    const ctx = this._measureCtx();
    const basePx = parseFloat(getComputedStyle(this).fontSize) || 13;
    ctx.font = `${(basePx * 0.72).toFixed(2)}px ${this._cssVar("--mono", "monospace")}`;
    const prefix = confirmed ? "✓ " : "";
    return ctx.measureText(prefix + text).width + BADGE_CHROME_CSS_PX;
  }

  /** Labels whose frequency falls inside the current span, positioned by
   * fraction-of-span (0..1, so the lane stays aligned with the canvas
   * across a resize) and greedily packed into up to BADGE_ROWS rows by
   * actual measured chip width: a chip goes in the first row whose
   * previous chip's right edge, plus a gap, clears this chip's left edge;
   * a chip that fits no row is dropped and counted into hiddenCount. */
  _layoutBadgeRows() {
    const span = this._currentSpan();
    const labels = this.status?.labels ?? [];
    const width = span ? span.high - span.low : 0;
    if (!span || !width) return { items: [], hiddenCount: 0, rowsUsed: 1 };
    const laneWidthPx = this._laneWidthPx || 640;
    const items = labels
      .filter((l) => l.frequency_hz >= span.low && l.frequency_hz <= span.high)
      .map((l) => {
        const text = this._chipText(l);
        const frac = (l.frequency_hz - span.low) / width;
        return { label: l, text, frac, centerPx: frac * laneWidthPx, widthPx: this._chipWidthPx(text, !!l.confirmed) };
      })
      .sort((a, b) => a.centerPx - b.centerPx);
    const rowRightEdge = new Array(BADGE_ROWS).fill(-Infinity);
    const placed = [];
    let hiddenCount = 0;
    for (const item of items) {
      const left = item.centerPx - item.widthPx / 2;
      const right = item.centerPx + item.widthPx / 2;
      const row = rowRightEdge.findIndex((edge) => edge + BADGE_GAP_CSS_PX <= left);
      if (row === -1) {
        hiddenCount++;
        continue;
      }
      rowRightEdge[row] = right;
      placed.push({ ...item, row });
    }
    const rowsUsed = placed.reduce((max, it) => Math.max(max, it.row + 1), 0);
    return { items: placed, hiddenCount, rowsUsed: Math.max(1, rowsUsed) };
  }

  /** "+N more" indicator click: scrolls the labels table into view so the
   * operator can find the chips the lane had no room for. */
  _scrollToLabelsTable() {
    this.querySelector("#survey-labels-table")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** SNR (dB) -> chip opacity, clamped to [0.5, 1]; 30 dB is treated as
   * "fully readable" (assumption -- the plan/spec don't pin an exact curve). */
  _snrOpacity(snrDb) {
    const t = Math.max(0, Math.min(1, (Number(snrDb) || 0) / 30));
    return (0.5 + t * 0.5).toFixed(2);
  }

  /** Label.last_seen is an epoch-seconds float (Stream A contract), unlike
   * the ISO timestamps format.js's helpers expect -- hence this local one. */
  _secondsAgo(epochS) {
    if (epochS === null || epochS === undefined) return "--";
    const s = Math.max(0, Math.round(Date.now() / 1000 - epochS));
    // Human units, two at most: "45 s", "3 min", "1 h 12 min", "2 d 3 h".
    if (s < 60) return `${s} s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} h${m % 60 ? ` ${m % 60} min` : ""} ago`;
    const d = Math.floor(h / 24);
    return `${d} d${h % 24 ? ` ${h % 24} h` : ""} ago`;
  }

  _onBadgeClick(hz) {
    if (this.status?.replay) return; // replay: no tuning the live radio (also the row TUNE button, which calls this)
    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
  }

  /** S5: ✓ chip click on a confirmed label -- opens the decoded-messages
   * modal (decode-view.js) preset to this frequency. Stops propagation so it
   * doesn't also fire the chip's own tune-frequency click. */
  _onConfirmedClick(hz) {
    this.dispatchEvent(new CustomEvent("survey-open-messages", { detail: { frequency_hz: hz } }));
  }

  /** U3: opens the signal-detail panel for this label's frequency -- the
   * badge chip's ⓘ prefix, and the labels table row's own click (excluding
   * its TUNE/DECODE/SAVE action cells, see _renderLabelRow). */
  _openSignal(label) {
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: label.frequency_hz },
        bubbles: true,
        composed: true,
      })
    );
  }

  /** → DECODE row action (S5): pins a band-decoder channel on this label's
   * frequency (POST /api/survey/decode via app.js's _doSurveyDecode).
   * `decoders` is null to take the label's suggested decoders server side, or
   * an explicit list from the manual-pick popover (_renderDecodePopover) when
   * none were suggested. */
  _onDecodeClick(hz, decoders) {
    this.dispatchEvent(new CustomEvent("survey-decode", { detail: { frequency_hz: hz, decoders } }));
  }

  /** ■ row action (S5, shown when label.decoding): unpins the channel. */
  _onUndecodeClick(hz) {
    this.dispatchEvent(new CustomEvent("survey-undecode", { detail: { frequency_hz: hz } }));
  }

  /** SAVE row action (S5, always enabled): saves the label as a memory using
   * the server-side identity summary. */
  _onSaveClick(hz) {
    this.dispatchEvent(new CustomEvent("survey-save", { detail: { frequency_hz: hz } }));
  }

  /** NFM-family decoders (from getDecoders(), passed in as `.decoders`) --
   * the only ones the band channelizer can run, same qualifying rule as
   * decode-deck.js's Band decode section. */
  _nfmDecoders() {
    return (this.decoders ?? []).filter((d) => (d.demodulators ?? []).includes("NFM"));
  }

  _openDecodePopover(labelId) {
    this._decodePopoverId = labelId;
    this._decodePopoverSelected = new Set();
  }

  _closeDecodePopover() {
    this._decodePopoverId = null;
    this._decodePopoverSelected = new Set();
  }

  _toggleDecodePopoverDecoder(id) {
    const next = new Set(this._decodePopoverSelected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._decodePopoverSelected = next;
  }

  _confirmDecodePopover(hz) {
    if (!this._decodePopoverSelected.size) return;
    this._onDecodeClick(hz, Array.from(this._decodePopoverSelected));
    this._closeDecodePopover();
  }

  /** Manual decoder picker (S5): shown from the → DECODE button when a label
   * has no family.suggested_decoders, so decoding still isn't blocked on the
   * family classifier having an opinion. */
  _renderDecodePopover(l) {
    const nfm = this._nfmDecoders();
    const candidates = l.decoder_candidates ?? [];
    return html`
      <div
        class="panel"
        style="position:absolute;z-index:10;top:100%;right:0;margin-top:2px;padding:6px;min-width:260px;max-height:60vh;overflow:auto;
          background:var(--panel-well);border:1px solid var(--line)"
        @click=${(e) => e.stopPropagation()}
      >
        ${candidates.length ? html`<div class="dim">Tentative decoder candidates</div>
          ${candidates.map((candidate) => {
            const spec = (this.decoders ?? []).find((d) => d.id === candidate.id);
            const basis = candidate.evidence_kind === "allocation" ? "frequency context" : "IQ estimate";
            return html`<div style="max-width:340px;white-space:normal;margin:6px 0">
              <button ?disabled=${!spec?.available || !!this.status?.replay}
                title=${spec?.available ? candidate.next_action : spec?.install_hint ?? "Decoder unavailable"}
                @click=${() => { this._onDecodeClick(l.frequency_hz, [candidate.id]); this._closeDecodePopover(); }}
              >TRY ${candidate.label}</button>
              <div class="dim">${basis}: ${candidate.reason}</div>
              <div class="dim">${candidate.limitations}</div>
            </div>`;
          })}<hr />` : ""}
        ${nfm.length
          ? groupDecoders(nfm).map(({category, decoders}) => html`
              <section class="decoder-category" aria-label=${category}>
                <h3 class="decoder-category-title">${category}</h3>
                ${decoders.map(d => html`
                  <label class="row mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em"
                    title=${decoderValidation(d).title}>
                    <input type="checkbox" .checked=${this._decodePopoverSelected.has(d.id)}
                      @change=${() => this._toggleDecodePopoverDecoder(d.id)} />
                    ${d.label}<span class="dim">${decoderValidation(d).text}</span>
                  </label>`)}
              </section>`)
          : html`<div class="dim mono" style="font-size:0.85em">no NFM decoders</div>`}
        <div class="control-row" style="margin-top:4px;gap:4px">
          <button
            style="font-size:0.8em"
            ?disabled=${!this._decodePopoverSelected.size}
            @click=${() => this._confirmDecodePopover(l.frequency_hz)}
          >
            GO
          </button>
          <button style="font-size:0.8em" @click=${() => this._closeDecodePopover()}>CANCEL</button>
        </div>
      </div>
    `;
  }

  _toggleOptions() {
    this._optionsOpen = !this._optionsOpen;
    saveOptionsOpen(this._optionsOpen);
  }

  /** Collapsed-by-default OPTIONS row (IDENTIFY/STATUS/DISPLAY moved behind
   * it, see render()): a disclosure button plus, while collapsed, a
   * one-line summary of what's enabled so the operator doesn't have to
   * expand it just to check -- "identify: activity, bandwidth, band plan,
   * catalog · analyzer: off · display: snap, avg". When the analyzer is off
   * (no narrowband dimension ticked), the one-click START ANALYZER
   * affordance rides along on this same line so it's still one click away
   * with OPTIONS collapsed. Expanded, the three rows render exactly as
   * before (render() below). */
  _renderOptionsToggle() {
    const available = this.status?.available_dimensions ?? [];
    const selected = new Set(this.status?.dimensions ?? []);
    const identify = available.filter((d) => selected.has(d.id)).map((d) => OPTIONS_DIMENSION_WORDS[d.id] ?? d.id);

    const analyzer = this.status?.analyzer;
    let analyzerWord = null;
    let analyzerOff = false;
    if (analyzer) {
      const dims = this.status?.dimensions ?? [];
      const narrowband = ["modulation", "symbol_rate", "voice"].some((d) => dims.includes(d));
      if (narrowband) {
        const queued = analyzer.queue?.length ?? 0;
        const cpu = analyzer.iq?.cpu_load != null ? `${Math.round(analyzer.iq.cpu_load * 100)}%` : "--";
        analyzerWord = `${queued} queued, cpu ${cpu}`;
        if (this._analyzerDropWarn) analyzerWord += ", dropped frames";
        const rxRate = analyzer.iq?.rate_hz;
        const spanRate = this.status?.span?.sample_rate_hz;
        if (rxRate != null && spanRate && rxRate < 0.95 * spanRate) {
          analyzerWord += `, RX ${(rxRate / 1e6).toFixed(1)}/${(spanRate / 1e6).toFixed(1)} MS/s`;
        }
      } else {
        analyzerWord = "off";
        analyzerOff = true;
      }
    }

    const display = [];
    if (this._snapOn) display.push("snap");
    if (this._peakHold) display.push("peak");
    if (this._avgSmooth) display.push("avg");

    const parts = [`identify: ${identify.length ? identify.join(", ") : "none"}`];
    if (analyzerWord != null) parts.push(`analyzer: ${analyzerWord}`);
    parts.push(`display: ${display.length ? display.join(", ") : "none"}`);

    return html`
      <div class="control-row survey-options-line" style="gap:8px;align-items:center">
        <button
          style="font-size:0.85em"
          aria-expanded=${this._optionsOpen}
          title="IDENTIFY / STATUS / DISPLAY controls"
          @click=${() => this._toggleOptions()}
        >
          OPTIONS ${this._optionsOpen ? "▴" : "▾"}
        </button>
        ${!this._optionsOpen
          ? html`<span class="dim mono survey-options-summary" style="font-size:0.85em" title=${parts.join(" · ")}
              >${parts.join(" · ")}</span
            >`
          : ""}
        ${!this._optionsOpen && analyzerOff
          ? html`<button
              class="run-start"
              style="font-size:0.85em;padding:2px 8px;min-height:22px"
              title="Tick Modulation, Symbol rate and Voice: fills MOD / BAUD / FAMILY / VOICE for narrowband signals"
              @click=${() => this._startAnalyzer()}
            >
              ▶ START ANALYZER
            </button>`
          : ""}
        <span class="spacer"></span>
      </div>
    `;
  }

  _mlStatusSummary() {
    const classifier = mlWorkerPhase(this.status?.analyzer?.rfml,
      (this.status?.labels ?? []).some(label => label.rfml?.status === "ok" && !label.rfml.stale));
    const detector = mlWorkerPhase(this.status?.analyzer?.rfml_detector,
      !!this.status?.analyzer?.rfml_detector?.result);
    return ` · Classifier ${classifier} · Detector ${detector}`;
  }

  _toggleDimension(id) {
    const selected = new Set(this.status?.dimensions ?? []);
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    this.dispatchEvent(new CustomEvent("survey-dimensions", { detail: { dimensions: [...selected] } }));
  }

  /** Inline "START ANALYZER" affordance for the merged MOD/BAUD/FAMILY/VOICE
   * column: selects the three IQ dimensions on top of whatever's already
   * selected, same event _toggleDimension uses. */
  _startAnalyzer() {
    const selected = new Set(this.status?.dimensions ?? []);
    selected.add("modulation");
    selected.add("symbol_rate");
    selected.add("voice");
    this.dispatchEvent(new CustomEvent("survey-dimensions", { detail: { dimensions: [...selected] } }));
  }

  /** Frequency tolerance for matching a label to status.analyzer.targets --
   * one spectrum bin's width, derived from the live frame when available. */
  _analyzerBinToleranceHz() {
    const sr = this.frame?.sample_rate_hz ?? this.status?.span?.sample_rate_hz;
    const bins = this.frame?.bins?.length || 512;
    return sr ? sr / bins : 1000;
  }

  _isAnalyzerTarget(freqHz) {
    const targets = this.status?.analyzer?.targets ?? [];
    const tol = this._analyzerBinToleranceHz();
    return targets.some((t) => Math.abs((t?.frequency_hz ?? NaN) - freqHz) <= tol);
  }

  _analysisState(label) {
    const state = label?.analysis_state?.state;
    if (ANALYSIS_STATE_LABELS[state]) return state;
    // Older servers do not include explicit per-label freshness. Never call
    // their unqualified stored result current.
    return label?.analyzed_at || label?.last_classification
      ? "historical_result"
      : "not_analyzed";
  }

  _renderAnalysisState(label) {
    const state = this._analysisState(label);
    const previous = label?.analysis_state?.previous_name;
    const detail = state === "waiting" && previous
      ? `Previous estimate: ${previous} (historical)`
      : state === "historical_result"
        ? label?.analysis_state?.name || label?.last_classification?.name || "Last successful analysis"
        : state === "current_result" && label?.analysis_state?.refresh_pending
          ? "Refresh queued"
          : "";
    return html`<small class="survey-analysis-state dim" data-analysis-state=${state}
      title=${detail || ANALYSIS_STATE_LABELS[state]}>${ANALYSIS_STATE_LABELS[state]}
      ${detail ? html`<span class="survey-analysis-detail"> · ${detail}</span>` : ""}</small>`;
  }

  _mlClassificationState(label) {
    if (label?.rfml) return label.rfml.stale ? "historical_result" : "current_result";
    if (!this.status?.ml_config?.classifier_enabled) return "not_analyzed";
    const analyzer = this.status?.analyzer ?? {};
    if (["disabled", "error", "unavailable", "stopped"].includes(analyzer.rfml?.state)) {
      return "not_analyzed";
    }
    const tolerance = Math.max(this._analyzerBinToleranceHz(), 6000);
    const matches = (frequency) => frequency != null && Math.abs(frequency - label.frequency_hz) <= tolerance;
    const eligible = [...(analyzer.candidate_frequencies_hz ?? []),
      ...(analyzer.targets ?? []).map(target => target.frequency_hz)];
    return eligible.some(matches) ? "waiting" : "not_analyzed";
  }

  _renderMlClassifications() {
    const labels = this.status?.labels ?? [];
    if (!labels.length) return html`<p class="dim">Not analyzed · no tracked signals.</p>`;
    return labels.map(label => {
      const state = this._mlClassificationState(label);
      const result = label.rfml;
      const detail = result
        ? `${result.classification ?? "UNKNOWN"} · ${result.status} · raw score ${Number.isFinite(result.score) ? result.score.toFixed(3) : "—"}`
        : state === "waiting" ? "No classifier result yet" : "No classifier result";
      return html`<div class="mono" data-analysis-state=${state}>
        ${mhz(label.frequency_hz)} · ${ANALYSIS_STATE_LABELS[state]} · ${detail}
      </div>`;
    });
  }

  _renderDimensions() {
    const available = this.status?.available_dimensions ?? [];
    if (!available.length) return "";
    const selected = new Set(this.status?.dimensions ?? []);
    const box = (d) => html`
      <label
        class="dim mono"
        style="display:flex;align-items:center;gap:4px;font-size:0.85em"
        title=${d.needs_iq ? "starts the IQ analyzer (~32 MB/s at 8 MS/s)" : ""}
      >
        <input type="checkbox" .checked=${selected.has(d.id)} @change=${() => this._toggleDimension(d.id)} />
        ${labelFor("dimension", d.id)}
      </label>
    `;
    // Two kinds of dimension: read off the spectrum stream (free), or needing
    // the IQ analyzer (costs IQ bandwidth + CPU) -- grouped so that's visible.
    const spectrum = available.filter((d) => !d.needs_iq);
    const iq = available.filter((d) => d.needs_iq);
    return html`
      <div class="control-row survey-dims">
        <span class="survey-subgroup">spectrum</span>
        ${spectrum.map(box)}
        ${iq.length
          ? html`<span class="survey-divider" aria-hidden="true"></span>
              <span class="survey-subgroup">IQ analyzer</span>
              ${iq.map(box)}`
          : ""}
      </div>
    `;
  }

  /** S3/S4: `ANALYZER · n queued · analyzing MHz · cpu %` line under the
   * dimension toggles, plus a dropped-frames warning -- mirrors decode-deck's
   * band-decode status line (status.band.iq). status.analyzer is
   * msdr/survey/analyzer.py Analyzer.status(); absent entirely when the
   * survey/analyzer packages aren't available server side. Null fields
   * (nothing analyzing, IQ not connected yet) render as "--". */
  _renderAnalyzerStatus() {
    const analyzer = this.status?.analyzer;
    if (!analyzer) return "";
    const queued = analyzer.queue?.length ?? 0;
    const analyzing = analyzer.analyzing != null ? mhz(analyzer.analyzing) : "--";
    const cpu = analyzer.iq?.cpu_load != null ? `${Math.round(analyzer.iq.cpu_load * 100)}%` : "--";
    // MOD/BAUD/FAMILY/VOICE need a narrowband IQ dimension; RDS alone runs
    // the analyzer but fills none of them -- offer the one-click start here,
    // not only in the collapsed table row (gone once RDS started the analyzer).
    const dims = new Set(this.status?.dimensions ?? []);
    const narrowband = ["modulation", "symbol_rate", "voice"].some((d) => dims.has(d));
    // Delivered IQ rate vs the span's: warn when SDRconnect falls >5% short.
    const rate = analyzer.iq?.rate_hz;
    const spanRate = this.status?.span?.sample_rate_hz;
    const short = rate != null && spanRate && rate < 0.95 * spanRate;
    return html`
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span>
          ANALYZER · ${queued} queued · analyzing ${analyzing} · cpu ${cpu}
          ${this._analyzerDropWarn ? html` <span style="color:var(--danger)">⚠ dropped frames</span>` : ""}
          ${short
            ? html` · <span
                  style="color:var(--danger)"
                  title="Measured IQ throughput over a 2-second window is below the configured span rate. This is not a settings mismatch or proof of lost samples: buffering, stream delivery or processing load may contribute. A sustained shortfall can impair analysis; compare dropped-frame counts."
                  >⚠ IQ RX ${(rate / 1e6).toFixed(1)} / target ${(spanRate / 1e6).toFixed(1)} MS/s</span
                >`
            : ""}
        </span>
        ${narrowband
          ? ""
          : html`<button
              class="run-start"
              style="font-size:0.85em;padding:2px 8px;min-height:22px"
              title="Tick Modulation, Symbol rate and Voice: fills MOD / BAUD / FAMILY / VOICE for narrowband signals"
              @click=${() => this._startAnalyzer()}
            >
              ▶ START ANALYZER
            </button>`}
      </div>
      ${this._renderRdsStatus(analyzer.rds)}
    `;
  }

  /** RDS progress: a full round visits every FM station for ~6 s, so names
   * take minutes to fill in -- show where it is and what it has so far. */
  _renderRdsStatus(rds) {
    if (!rds?.active) return "";
    const stations = Object.values(rds.stations ?? {});
    const locked = stations.filter((s) => s.pi || s.pty_name).length;
    const named = stations.filter((s) => s.ps || s.callsign).length;
    const targets = rds.targets?.length ?? 0;
    const roundS = Math.round(targets * (rds.dwell_s ?? 6));
    return html`
      <div>
        RDS · ${targets} stations · ${locked} locked · ${named} named ·
        ${rds.analyzing != null ? `listening ${mhz(rds.analyzing)}` : "idle"} · full round ≈
        ${Math.floor(roundS / 60)}:${String(roundS % 60).padStart(2, "0")}
        ${rds.paused ? html` · <span style="color:var(--accent)">paused (cpu)</span>` : ""}
      </div>
    `;
  }

  _renderBadgeLane() {
    const span = this._currentSpan();
    const labels = (this.status?.labels ?? []).filter(l => !span ||
      (l.frequency_hz >= span.low && l.frequency_hz <= span.high));
    const highlightedId = this._highlightedLabelId(labels);
    return html`
      <details class="survey-signal-shortcuts" aria-label="Signals in view" ?open=${this._shortcutsOpen}
        @toggle=${(e) => {
          this._shortcutsOpen = e.target.open;
          saveShortcutsOpen(this._shortcutsOpen);
        }}>
        <summary class="survey-signal-shortcuts-heading">
          <span class="survey-signal-shortcuts-title"><strong>Signals in view <span class="dim">(${labels.length})</span></strong></span>
        </summary>
        <div class="survey-badge-lane">
          <span class="dim">MHz · ● active / ○ quiet · ⓘ details</span>
          ${labels.length ? labels.map(label => {
            const selected = label.id === highlightedId;
            const family = hasCwOutput(label) ? "CW · output"
              : label.family?.name?.startsWith("CW") ? "CW?"
              : label.family?.name?.includes("speech-like") ? "Voice?"
              : label.family?.name ? shortFamilyName(label.family.name)
              : label.voice?.kind === "data" ? "Data?" : "Unclassified";
            return html`<div class="survey-badge${selected ? " selected" : ""}"
              @mouseenter=${() => (this._hoverLabelId = label.id)}
              @mouseleave=${() => (this._hoverLabelId = null)}>
              <button class="survey-signal-tune" aria-pressed=${selected}
                title=${`Tune ${(label.frequency_hz / 1e6).toFixed(6)} MHz · ${family} · ${label.active_now ? "Active" : "Quiet"}${selected ? " · Selected" : ""}`}
                aria-label=${`${(label.frequency_hz / 1e6).toFixed(3)} MHz · ${family} · ${label.active_now ? "Active" : "Quiet"}`}
                @click=${() => this._onBadgeClick(label.frequency_hz)}>
                <span aria-hidden="true" class=${label.active_now ? "signal-active" : "dim"}>${label.active_now ? "●" : "○"}</span>
                <b class="mono">${(label.frequency_hz / 1e6).toFixed(3)}</b>
                <span class="survey-signal-family dim">${family === "Unclassified" ? "—" : family}</span>
              </button>
              <button class="survey-signal-info" aria-label=${`Details for ${(label.frequency_hz / 1e6).toFixed(3)} MHz`}
                @click=${() => this._openSignal(label)}>ⓘ</button>
            </div>`;
          }) : html`<span class="dim">No signals detected in this view yet.</span>`}
        </div>
      </details>
    `;
  }

  _signalActivity(label) {
    this._lastSpeechAt ??= new Map();
    const now = Date.now() / 1000;
    for (const [id, at] of this._lastSpeechAt) {
      if (now - at > 3) this._lastSpeechAt.delete(id);
    }
    if (label.active_now !== true) this._lastSpeechAt.delete(label.id);
    const activity = signalActivity(label, now, this._lastSpeechAt.get(label.id));
    if (Number.isFinite(activity.speechAt)) this._lastSpeechAt.set(label.id, activity.speechAt);
    return activity;
  }

  /** Labels passing the activity and SNR filters. */
  _filterLabels(labels) {
    const filter = this._activityFilter;
    const min = this._snrMin === "" ? null : Number(this._snrMin);
    const max = this._snrMax === "" ? null : Number(this._snrMax);
    return labels.filter(
      (l) =>
        (filter === "all" || (filter === "speech" ? this._signalActivity(l).speechDetected : l?.activity?.kind === filter)) &&
        matchesEvidenceFilter(l, this._evidenceFilter) &&
        (min == null || !Number.isFinite(min) || (l.snr_db ?? -Infinity) >= min) &&
        (max == null || !Number.isFinite(max) || (l.snr_db ?? Infinity) <= max),
    );
  }

  _sortLabels(labels) {
    return [...labels].sort(this._comparator());
  }

  /** The current header sort as a comparator (missing values always last). */
  _comparator() {
    if (this._sortKey === "evidence") {
      return (a, b) =>
        compareEvidence(a, b, this._sortOrder === "desc" ? "strongest" : "weakest");
    }
    const value = (SORT_COLUMNS[this._sortKey] ?? SORT_COLUMNS.frequency_hz).value;
    const sign = this._sortOrder === "desc" ? -1 : 1;
    return (a, b) => {
      const av = value(a);
      const bv = value(b);
      const aMissing = av == null || av === "";
      const bMissing = bv == null || bv === "";
      if (aMissing || bMissing) return aMissing - bMissing;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv), undefined, { sensitivity: "base" });
      return sign * cmp;
    };
  }

  /** Session rows survive quiet periods. New signals append; only explicit
   * sorting/filtering reorders them. A changed span/reset starts a new list. */
  _tableView() {
    const measured = this.status?.labels ?? [];
    const proposals = [];
    for (const proposal of this.status?.ml_proposals ?? []) {
      if (this._proposalMatch(proposal)) continue;
      // One table row per frequency region, not per short time fragment.
      if (proposals.some(other => {
        const overlap = Math.max(0, Math.min(other.high_hz, proposal.high_hz) - Math.max(other.low_hz, proposal.low_hz));
        const union = Math.max(other.high_hz, proposal.high_hz) - Math.min(other.low_hz, proposal.low_hz);
        return union > 0 && overlap / union >= 0.5;
      })) continue;
      proposals.push(proposal);
    }
    const live = [
      ...measured,
      ...proposals.map(proposal => ({
        ...proposal,
        ml_only: true,
        rfml_detection: proposal.rfml_detection,
        rfml: proposal.rfml,
      })),
    ];
    const span = this.status?.span;
    // A new span or a server-side reset (clear/restart: "generation"): nothing
    // from before is "just gone" -- start the table's memory clean.
    const spanSig = `${span?.center_hz}|${span?.sample_rate_hz}|${this.status?.generation}`;
    const now = Date.now() / 1000;
    // VFO changes update the highlight without rebuilding the session list.
    this._viewVfoHz = span?.vfo_hz;
    if (spanSig !== this._viewSpan) {
      this._viewSpan = spanSig;
      this._ghosts = new Map();
      this._prevLabels = new Map();
      this._rowOrder = [];
      this._newIds = new Map();
      this._orderSig = null;
      this._awaitingFirstRows = !live.length;
    }
    if (this._viewStatus !== this.status) {
      // Once per status update (render can run more often than that).
      this._viewStatus = this.status;
      const liveIds = new Set(live.map((l) => l.id));
      const previousRows = [...this._prevLabels, ...[...this._ghosts].map(([id, ghost]) => [id, ghost.label])];
      const binHz = this.status?.resolution_hz ?? 0;
      // Same signal? The tracker re-creates a track (new id) for a wideband
      // signal that splits/merges; the old id then vanishes while a new one
      // covers the same frequency -- that's a re-track, not a gone signal.
      const covers = (a, b) =>
        Math.abs(a.frequency_hz - b.frequency_hz) <= Math.max(binHz, Math.min(12500, Math.min(a.width_hz ?? 0, b.width_hz ?? 0) / 2));
      for (const [id, label] of previousRows) {
        if (liveIds.has(id)) continue;
        const successor = live.find((l) => covers(l, label));
        if (successor) {
          // Hand the slot to a new successor. If the successor already has a
          // visible row (two tracks merged), keep this slot as a dimmed ghost
          // throughout the session instead of making the table shrink mid-read.
          const i = this._rowOrder.indexOf(id);
          if (i >= 0 && !this._rowOrder.includes(successor.id)) {
            this._rowOrder[i] = successor.id;
            if (this._newIds.has(id)) {
              this._newIds.set(successor.id, this._newIds.get(id));
              this._newIds.delete(id);
            }
            this._ghosts.delete(id);
          } else if (i >= 0 && !this._ghosts.has(id)) {
            this._ghosts.set(id, { label, goneAt: now });
          }
        } else if (this._rowOrder.includes(id) && !this._ghosts.has(id)) {
          this._ghosts.set(id, { label, goneAt: now });
        }
      }
      for (const [id, ghost] of this._ghosts) {
        if (liveIds.has(id)) {
          this._ghosts.delete(id);
        }
      }
      this._prevLabels = new Map(live.map((l) => [l.id, l]));
    }
    const all = [...live, ...[...this._ghosts.values()].map((g) => ({ ...g.label, active_now: false, activity: { ...g.label.activity, kind: "quiet" }, _goneAt: g.goneAt }))];
    const shown = this._filterLabels(all);
    const sorted = this._sortLabels(shown);
    const sig = [this._sortKey, this._sortOrder, this._activityFilter, this._evidenceFilter, this._snrMin, this._snrMax, this._resortSeq].join("|");
    // Initial results appear automatically; only explicit controls re-sort.
    const firstResults = this._awaitingFirstRows && live.length > 0;
    if (sig !== this._orderSig || firstResults) {
      const previousIds = new Set(this._rowOrder);
      const hadSnapshot = this._orderSig !== null;
      this._orderSig = sig;
      this._rowOrder = sorted.map((l) => l.id);
      this._newIds = new Map(hadSnapshot
        ? this._rowOrder.filter((id) => !previousIds.has(id)).map((id) => [id, now])
        : []);
      this._awaitingFirstRows = false;
    }
    // Keep an accepted row even when its live SNR/activity/evidence crosses a
    // filter threshold; the next explicit update/filter edit reapplies it.
    const byId = new Map(all.map((l) => [l.id, l]));
    const speechIds = this._activityFilter === "speech" ? new Set(shown.map(label => label.id)) : null;
    this._rowOrder = this._rowOrder.filter((id) => byId.has(id) && (!speechIds || speechIds.has(id)));
    for (const [id, at] of this._newIds) {
      if (!byId.has(id) || now - at > NEW_ROW_S) this._newIds.delete(id);
    }
    const existing = new Set(this._rowOrder);
    for (const label of shown) {
      if (!existing.has(label.id)) {
        this._rowOrder.push(label.id);
        existing.add(label.id);
        this._newIds.set(label.id, now);
      }
    }
    const rows = this._rowOrder.map((id) => byId.get(id));
    const accepted = new Set(this._rowOrder);
    const pending = this._filterLabels(live).filter((l) => !accepted.has(l.id)).length;
    const gone = rows.filter((l) => l._goneAt).length;
    const stale = pending > 0 || gone > 0 || rows.length !== sorted.length ||
      rows.some((l, i) => l.id !== sorted[i]?.id);
    return { rows, total: all.length, stale, pending, gone };
  }

  /** A clickable column header: sorts by `key`, flips the order when it's
   * already the sort column; ▲/▼ marks the current one. */
  _sortHeader(key, text) {
    const active = this._sortKey === key;
    const onClick = () => {
      if (active) {
        this._sortOrder = this._sortOrder === "asc" ? "desc" : "asc";
      } else {
        this._sortKey = key;
        this._sortOrder = SORT_COLUMNS[key].desc ? "desc" : "asc";
      }
    };
    return html`<th
      class="sortable"
      style="cursor:pointer;user-select:none;${active ? "color:var(--accent)" : ""}"
      title="Sort by ${text}"
      aria-sort=${active ? (this._sortOrder === "asc" ? "ascending" : "descending") : "none"}
      @click=${onClick}
    >${text}${active ? (this._sortOrder === "asc" ? " ▲" : " ▼") : ""}</th>`;
  }

  _renderEvidenceHeader() {
    const active = this._sortKey === "evidence";
    const sort = () => {
      if (active) {
        this._sortOrder = this._sortOrder === "asc" ? "desc" : "asc";
      } else {
        this._sortKey = "evidence";
        this._sortOrder = "desc"; // strongest evidence first
      }
    };
    return html`<th
      class="sortable"
      style="cursor:pointer;user-select:none;${active ? "color:var(--accent)" : ""}"
      title="Sort by evidence strength: validated, decoder output, estimate, context, none"
      aria-sort=${active ? (this._sortOrder === "asc" ? "ascending" : "descending") : "none"}
      @click=${sort}
    >
      <span>EVIDENCE${active ? (this._sortOrder === "asc" ? " ▲" : " ▼") : ""}</span>
      <select
        aria-label="Filter survey rows by evidence"
        title="Show rows by evidence category"
        style="width:78px;margin-left:4px;font-size:0.85em"
        .value=${this._evidenceFilter}
        @click=${(event) => event.stopPropagation()}
        @change=${(event) => this._setEvidenceFilter(event.target.value)}
      >
        <option value="all">All</option>
        <option value="validated">Validated</option>
        <option value="decoder_output">Output</option>
        <option value="estimate">Estimate</option>
        <option value="context">Context</option>
        <option value="none">None</option>
      </select>
    </th>`;
  }

  _setSnrFilter(min, max) {
    this._snrMin = min;
    this._snrMax = max;
    saveSnrFilter(min, max);
  }

  _setEvidenceFilter(value) {
    this._evidenceFilter = value;
  }

  /** Reset every table filter (activity, evidence, SNR range) -- the empty
   * state's "clear filters" action, so a no-match table never dead-ends. */
  _clearFilters() {
    this._activityFilter = "all";
    this._setEvidenceFilter("all");
    this._setSnrFilter("", "");
  }

  _toggleTech() {
    this._techOn = !this._techOn;
    saveTechOn(this._techOn);
  }

  /** Table detail toggle is reachable in both workspaces, independent of OPTIONS. */
  _renderTableActions(view) {
    return html`
        ${view.rows.length !== view.total
          ? html`<span class="dim mono" style="font-size:0.85em">${view.rows.length} of ${view.total}</span>`
          : ""}
        <span class="spacer"></span>
        <button
          style="font-size:0.8em;padding:2px 8px"
          aria-pressed=${this._techOn}
          title="Show technical columns (SNR, BW, MOD, BAUD, DUTY and analyzer detail)"
          @click=${() => this._toggleTech()}
        >
          TECH ${this._techOn ? "ON" : "OFF"}
        </button>
    `;
  }

  _renderTableControls(view) {
    return html`
      <div style="display:flex;align-items:center;gap:6px;margin-top:8px">
        <span class="dim mono" style="font-size:0.85em">show</span>
        <select aria-label="filter labels by activity" @change=${(e) => (this._activityFilter = e.target.value)}>
          <option value="all" ?selected=${this._activityFilter === "all"}>All</option>
          <option value="speech" ?selected=${this._activityFilter === "speech"}>Speech only (recent)</option>
          ${Object.keys(ACTIVITY_GLYPHS).map(
            (kind) => html`<option value=${kind} ?selected=${kind === this._activityFilter}>
              ${ACTIVITY_GLYPHS[kind]} ${labelFor("activity", kind)}
            </option>`,
          )}
        </select>
        <span class="dim mono" style="font-size:0.85em;margin-left:8px">SNR</span>
        <input
          type="number"
          step="1"
          aria-label="minimum SNR dB"
          placeholder="min"
          style="width:64px"
          .value=${String(this._snrMin)}
          @input=${(e) => this._setSnrFilter(e.target.value, this._snrMax)}
        />
        <span class="dim">–</span>
        <input
          type="number"
          step="1"
          aria-label="maximum SNR dB"
          placeholder="max"
          style="width:64px"
          .value=${String(this._snrMax)}
          @input=${(e) => this._setSnrFilter(this._snrMin, e.target.value)}
        />
        <span class="dim mono" style="font-size:0.85em">dB</span>
      </div>
    `;
  }

  /** No-result-yet text for an IQ-derived cell (MOD/BAUD/FAMILY/VOICE): if
   * the analyzer is actively working this label's frequency, "analyzing…";
   * otherwise "--". Matches status.analyzer.targets by frequency ±1 bin. */
  _iqCellPlaceholder(freqHz, analyzerActive) {
    return analyzerActive && this._isAnalyzerTarget(freqHz) ? "analyzing…" : "--";
  }

  /** Table-cell tooltip: "value — explanation" when format.js's explain()
   * had something to add for this value, else just the value text -- so a
   * mouseover on fm_fsk/psk_like/an evidence kind/etc. says what it means. */
  _cellTitle(text, explanation) {
    return explanation ? `${text} — ${explanation}` : text;
  }

  /** CATALOG cell: RDS PS (bold) + PTY name when the FM decoder has resolved
   * one, radiotext as a title tooltip plus a truncated (40 char) muted
   * second line; falls back to the existing catalog-match content when
   * there's no RDS PS yet. */
  _renderCatalogCell(l, catalog, catalogText) {
    const rds = l.rds;
    if (rds?.ps || rds?.callsign) {
      // Call letters (from the PI) first: stable, while many US stations
      // scroll song titles through PS.
      const name = rdsName(rds);
      const rt = rds.rt || null;
      const rtShort = rt && rt.length > 40 ? `${rt.slice(0, 40)}…` : rt;
      return html`
        <span title=${[rds.pi && `PI ${rds.pi}`, rt].filter(Boolean).join(" · ")}>
          <b>${name}</b>${rds.pty_name ? ` · ${rds.pty_name}` : ""}
          ${rt ? html`<div class="dim" style="font-size:0.85em">${rtShort}</div>` : ""}
        </span>
      `;
    }
    if (rds && (rds.pi || rds.pty_name)) {
      // Partial RDS (PI/PTY before the 4 PS segments are confirmed): show it so
      // the user can see decoding is progressing instead of a bare "--".
      return html`<span class="dim" title="RDS locked; station name still being assembled">
        ${rds.pty_name ?? ""}${rds.pty_name && rds.pi ? " · " : ""}${rds.pi ? `PI ${rds.pi}` : ""}
        · name…</span>`;
    }
    return catalog.length ? html`<span class=${evidenceClass("scheduled")}>${catalogText}</span>` : "--";
  }

  _renderMlOnlyLabelRow(l, cols, highlightedId) {
    const detection = l.rfml_detection ?? {};
    const name = typeof detection.evidence?.label === "string" && detection.evidence.label.trim()
      ? detection.evidence.label.trim() : "signal";
    const historical = !!l._goneAt || detection.stale === true;
    const age = !l._goneAt && Number.isFinite(detection.age_s) ? ` · ${Math.round(detection.age_s)} s old` : "";
    const freshness = historical ? `Historical ML proposal${age}` : `Current ML proposal${age}`;
    const center = Number.isFinite(l.frequency_hz) ? l.frequency_hz : (l.low_hz + l.high_hz) / 2;
    const range = `${(l.low_hz / 1e6).toFixed(3)}–${(l.high_hz / 1e6).toFixed(3)} MHz`;
    const rowClasses = [
      "survey-ml-only-row",
      historical ? "gone" : "",
      l.id === highlightedId ? "selected" : "",
    ].filter(Boolean).join(" ");
    const showDecode = this.mode !== "listen";
    return html`<tr class=${rowClasses} data-label-id=${l.id} aria-current=${l.id === highlightedId ? "true" : "false"}
      title="Experimental detector proposal · no DSP activity or SNR measurement for this proposal"
      style="cursor:pointer" @click=${(ev) => {
        if (ev.target.closest("button")) return;
        this._selectLabel(l.id);
        this._openSignal(l);
      }}>
      <td title="${freshness} · proposed range ${range}">
        ${l.id === highlightedId && !this.status?.replay && this.status?.span?.vfo_hz != null
          ? html`<span class="survey-listening-mark" aria-label="Listening" title="Receiver is listening here">▶ </span>` : ""}${(center / 1e6).toFixed(3)}</td>
      <td title="The detector's label is not a modulation or station classification.">ML proposal · ${name}<br>
        <small class="survey-analysis-state dim" data-analysis-state=${historical ? "historical_result" : "current_result"}>${historical ? "Historical result" : "Current result"}</small></td>
      <td title="No DSP activity measurement">—</td>
      ${cols.snr ? html`<td title="No DSP SNR measurement">—</td>` : ""}
      ${cols.bw_class ? html`<td title="No DSP bandwidth measurement">—</td>` : ""}
      ${cols.modulation ? html`<td title="Detector output is not a modulation classification">—</td>` : ""}
      ${cols.symbol_rate ? html`<td>—</td>` : ""}
      ${cols.duty ? html`<td title="No DSP activity measurement">—</td>` : ""}
      ${cols.family ? html`<td>—</td>` : ""}
      ${cols.voice ? html`<td>—</td>` : ""}
      ${cols.bandplan ? html`<td>—</td>` : ""}
      ${cols.catalog ? html`<td>—</td>` : ""}
      <td title="${freshness}; raw score ${Number.isFinite(detection.score) ? detection.score.toFixed(3) : "unavailable"} is uncalibrated, not a probability.">
        <div class="survey-evidence-viewport" tabindex="0" aria-label="Signal evidence; scroll for more badges">${this._renderEvidenceBadges(l)}</div></td>
      <td title=${freshness}>${historical ? "ML historical" : "ML current"}</td>
      <td><button class="tune" title="Tune ML proposal center frequency" aria-label="tune ML proposal ${(center / 1e6).toFixed(3)} MHz"
        style="font-size:0.8em;padding:2px 8px;min-height:28px" ?disabled=${!!this.status?.replay}
        @click=${() => this._onBadgeClick(center)}>▸</button></td>
      ${showDecode ? html`<td>—</td>` : ""}
      <td>—</td>
    </tr>`;
  }

  _renderLabelRow(l, cols, analyzerActive, highlightedId) {
    if (l.ml_only) return this._renderMlOnlyLabelRow(l, cols, highlightedId);
    const activity = l.activity ?? {};
    const duty = activity.duty != null ? `${Math.round(activity.duty * 100)}%` : null;
    const period = activity.period_s != null ? `${Math.round(activity.period_s)}s` : null;
    const catalog = l.catalog ?? [];
    const catalogText = catalog.length ? `${catalog[0].name}${catalog.length > 1 ? ` +${catalog.length - 1}` : ""}` : "--";
    const hovered = this._hoverLabelId != null && this._hoverLabelId === l.id;
    // S3/S4: modulation/symbol_rate/family/voice -- None (dimension off, or
    // not yet analyzed) renders as "analyzing…" while the analyzer is
    // working this frequency, else "--".
    const wide = WIDE_BW_CLASSES.has(l.bw_class);
    const mod = l.modulation;
    let modText;
    let modKindForTitle = null; // format.js's explain("modulation", ...) key, when modText is plain text
    if (wide) {
      // A wide label's MOD is its bandwidth class, never an analyzer result
      // (wide signals skip the analyzer) -- so it's "wideband" whenever the
      // modulation dimension is on; with it off (e.g. RDS as the only IQ
      // dimension) say so instead of a misleading "not analysed".
      if (this.status?.dimensions?.includes("modulation")) {
        modText = "wideband";
        modKindForTitle = "wideband";
      } else {
        modText = html`<span class="dim" title="tick modulation above to show MOD">--</span>`;
      }
    } else if (mod?.kind) {
      // TECH-only cell: analyzer score is unitless ("score 0.NN"), never a
      // percentage (S1 contract).
      modText = `${mod.kind}${mod.confidence != null ? ` · score ${mod.confidence.toFixed(2)}` : ""}`;
      modKindForTitle = mod.kind;
    } else {
      modText = this._iqCellPlaceholder(l.frequency_hz, analyzerActive);
    }
    // Explanation tooltips (this._cellTitle -- "value — explanation") for
    // every column whose raw value isn't self-evident (fm_fsk, psk_like,
    // an evidence kind, ...); see format.js's explain().
    const modTitle =
      typeof modText === "string"
        ? this._cellTitle(
            modText,
            [explain("modulation", modKindForTitle), mod?.kind && mod.confidence != null ? explain("confidence") : ""]
              .filter(Boolean)
              .join(" "),
          )
        : "";
    const symbolCandidateText = symbolRateCandidateText(l);
    const baudText =
      symbolCandidateText ??
      (l.symbol_rate_hz != null ? `${Math.round(l.symbol_rate_hz)}` : this._iqCellPlaceholder(l.frequency_hz, analyzerActive));
    const baudTitle = l.symbol_rate_hz != null ? this._cellTitle(baudText, explain("symbol_rate")) : "";
    const familyText = hasCwOutput(l) ? "CW decoder output" : l.family?.name ?? this._iqCellPlaceholder(l.frequency_hz, analyzerActive);
    const familyTitle = l.family?.name ? this._cellTitle(l.family.name, explain("evidence", "analyzer")) : "";
    // Table cell is glyph-only (no word) -- the title carries the full
    // explanation, including confidence when the analyzer reported one.
    const activityInfo = this._signalActivity(l);
    const voiceText = hasCwOutput(l) ? "CW"
      : activityInfo.speechDetected ? VOICE_GLYPHS.voice
      : activityInfo.state === "carrier_only" ? VOICE_GLYPHS.silence
      : activityInfo.dataDetected ? VOICE_GLYPHS.data : "—";
    const voiceTitle = activityInfo.text;
    const decoders = l.family?.suggested_decoders ?? [];
    const activityTitle = `${activityInfo.text}. RF occupancy and speech are separate; speech estimates use sampled IQ, not continuous monitoring.`;
    const dutyText = duty ?? "--";
    const dutyTitle = duty ? this._cellTitle([duty, period].filter(Boolean).join(" · "), explain("duty")) : "";
    // bw_source ("spectrum" or "iq"): the analyzer measured this bandwidth
    // from IQ rather than the (coarser) spectrum bins -- flagged with a
    // small "ᴵQ" marker in the cell as well as the tooltip.
    const bwLabel = labelFor("bw_class", l.bw_class);
    const bwTitle = this._cellTitle(
      bwLabel,
      [explain("bw_class", l.bw_class), explain("bw_source", l.bw_source)].filter(Boolean).join(" "),
    );
    const snrText = l.snr_db != null ? l.snr_db.toFixed(1) : "--";
    const snrTitle = l.snr_db != null ? this._cellTitle(`${snrText} dB`, explain("snr")) : "";
    const lastSeenTitle = l._goneAt
      ? ""
      : this._cellTitle(
          l.active_now ? "now" : this._secondsAgo(l.last_seen),
          explain("last_seen", l.active_now ? "now" : null),
        );
    // Plain-text version of the CATALOG cell for the hover tooltip (cells crop).
    const rdsText = [l.rds ? rdsName(l.rds) : "", l.rds?.pty_name].filter(Boolean).join(" · ");
    const scheduledExplain = catalog.length && !(l.rds?.ps || l.rds?.callsign) ? explain("evidence", "scheduled") : "";
    const catalogTitle = [rdsText, l.rds?.rt, catalog.map((c) => c.name).join(", "), scheduledExplain]
      .filter(Boolean)
      .join(" — ");
    // l.allocation (regulatory, every service) wins over the older l.bandplan
    // (channel/application hint) -- saved reports predating allocations only
    // ever have the latter.
    const allocationText = allocationServicesText(l.allocation);
    const bandplanTitle = allocationText
      ? this._cellTitle(allocationText, explain("evidence", "allocation"))
      : l.bandplan?.name ? this._cellTitle(l.bandplan.name, explain("evidence", "bandplan")) : "";
    // CANDIDATE column (S1 contract, always shown): best single name guess,
    // strongest evidence first (RDS name > analyzer family > catalog match).
    const candidateText = candidateName(l);
    const candidateTitle = [rdsText, l.rds?.rt, l.family?.name, catalog.map((c) => c.name).join(", ")]
      .filter(Boolean)
      .join(" — ");
    // EVIDENCE column: only an explicit passing decoder integrity result is
    // validated; output without that check remains decoder output.
    const evidence = surveyEvidence(l);
    const evidenceTitle =
      evidence.category === "validated"
        ? "A decoder integrity check explicitly passed. This confirms the decoded content, not a station identity."
        : evidence.category === "decoder_output"
          ? "A decoder produced output, but this event has no explicit passing integrity check."
          : evidence.kind
            ? explain("evidence", evidence.kind)
            : "No decoder, analyzer, catalog or allocation evidence is available.";
    // Replay mode isn't the live radio: TUNE/DECODE are disabled (SAVE stays
    // enabled -- it just writes a memory, see the caller).
    const replaying = !!this.status?.replay;
    const rowClasses = [
      l._goneAt ? "gone" : l.active_now ? "" : "quiet",
      l.id === highlightedId ? "selected" : "",
    ]
      .filter(Boolean)
      .join(" ");
    return html`
      <tr
        class=${rowClasses}
        data-label-id=${l.id}
        aria-current=${l.id === highlightedId ? "true" : "false"}
        style="cursor:pointer;${hovered && l.id !== highlightedId ? "background:var(--panel-well);" : ""}"
        title=${l._goneAt ? "quiet — retained in this session; last measurements shown" : "click for signal detail"}
        @mouseenter=${() => (this._hoverLabelId = l.id)}
        @mouseleave=${() => (this._hoverLabelId = null)}
        @click=${(ev) => {
          if (ev.target.closest("button")) return;
          this._openSignal(l);
        }}
      >
        <td title=${`${mhz(l.frequency_hz)}${this._newIds.has(l.id) ? " · new signal" : ""}`}>
          ${l.id === highlightedId && !replaying && this.status?.span?.vfo_hz != null
            ? html`<span class="survey-listening-mark" aria-label="Listening" title="Receiver is listening here">▶ </span>`
            : this._newIds.has(l.id) ? html`<span style="color:var(--accent)">✦ </span>` : ""}${(l.frequency_hz / 1e6).toFixed(3)}
        </td>
        <td title=${candidateTitle}>${candidateText}<br>${this._renderAnalysisState(l)}
          ${l.decoder_candidates?.length && this.mode !== "listen"
            ? html`<button style="font-size:0.75em" title="Tentative decoder options and supporting evidence"
                @click=${() => this._openDecodePopover(l.id)}>${l.decoder_candidates.length} decoder options</button>`
            : ""}
        </td>
        <td title=${activityTitle} data-signal-activity=${activityInfo.state} style="white-space:normal;line-height:1.35;text-overflow:clip;overflow-wrap:normal">${activityInfo.carrierPresent ? "●" : "○"} ${activityInfo.text}</td>
        ${cols.snr ? html`<td title=${snrTitle}>${snrText}</td>` : ""}
        ${cols.bw_class
          ? html`<td title=${bwTitle}>
              ${bwLabel}${l.bw_source === "iq"
                ? html`<span class="dim" style="font-size:0.75em" title=${explain("bw_source", "iq")}> ᴵQ</span>`
                : ""}
            </td>`
          : ""}
        ${cols.modulation ? html`<td title=${modTitle}>${modText}</td>` : ""}
        ${cols.symbol_rate ? html`<td title=${baudTitle}>${baudText}</td>` : ""}
        ${cols.duty ? html`<td title=${dutyTitle}>${dutyText}</td>` : ""}
        ${cols.family
          ? html`<td title=${familyTitle}>
              ${l.family?.name ? html`<span class=${evidenceClass("analyzer")}>${familyText}</span>` : familyText}
            </td>`
          : ""}
        ${cols.voice ? html`<td title=${voiceTitle} style="text-align:center">${voiceText}</td>` : ""}
        ${cols.bandplan
          ? html`<td title=${bandplanTitle}>
              ${allocationText ? html`<span class=${evidenceClass("allocation")}>${allocationText}</span>`
                : l.bandplan?.name ? html`<span class=${evidenceClass("bandplan")}>${l.bandplan.name}</span>` : "--"}
            </td>`
          : ""}
        ${cols.catalog ? html`<td title=${catalogTitle}>${this._renderCatalogCell(l, catalog, catalogText)}</td>` : ""}
        <td title=${evidenceTitle}>
          <div class="survey-evidence-viewport" tabindex="0" aria-label="Signal evidence; scroll for more badges">${this._renderEvidenceBadges(l)}</div>
        </td>
        <td title=${lastSeenTitle}>
          ${l._goneAt
            ? `quiet ${Math.round(Date.now() / 1000 - l._goneAt)}s`
            : l.active_now
              ? html`<span style="color:var(--ok)">● now</span>`
              : this._secondsAgo(l.last_seen)}
        </td>
        <td>
          <button
            class="tune"
            title=${replaying ? "live radio only" : "Tune"}
            aria-label="tune ${mhz(l.frequency_hz)}"
            style="font-size:0.8em;padding:2px 8px;min-height:28px"
            ?disabled=${replaying}
            @click=${() => {
              // Tuning a row also selects it, same as a waterfall click.
              this._selectLabel(l.id);
              this._onBadgeClick(l.frequency_hz);
            }}
          >
            ▸
          </button>
        </td>
        ${this.mode === "listen"
          ? ""
          : html`
              <td style="position:relative">
                ${l.decoding
                  ? html`
                      <button
                        class="tune"
                        title=${replaying ? "live radio only" : "Stop decode"}
                        aria-label="undecode ${mhz(l.frequency_hz)}"
                        style="font-size:0.8em;padding:2px 8px;min-height:28px"
                        ?disabled=${replaying}
                        @click=${() => this._onUndecodeClick(l.frequency_hz)}
                      >
                        ■
                      </button>
                    `
                  : html`
                      <button
                        class="tune"
                        title=${replaying ? "live radio only" : "Decode"}
                        aria-label="decode ${mhz(l.frequency_hz)}"
                        style="font-size:0.8em;padding:2px 8px;min-height:28px"
                        ?disabled=${replaying}
                        @click=${() =>
                          decoders.length
                            ? this._onDecodeClick(l.frequency_hz, null)
                            : this._openDecodePopover(l.id)}
                      >
                        → DECODE
                      </button>
                    `}
                ${this._decodePopoverId === l.id ? this._renderDecodePopover(l) : ""}
              </td>
            `}
        <td>
          <button
            class="tune"
            title="Save as memory"
            aria-label="save ${mhz(l.frequency_hz)}"
            style="font-size:0.8em;padding:2px 8px;min-height:28px"
            @click=${() => this._onSaveClick(l.frequency_hz)}
          >
            SAVE
          </button>
        </td>
      </tr>
    `;
  }

  /** Which optional (TECH-only) table columns to show, each still following
   * its IDENTIFY dimension so an unticked dimension doesn't leave a column
   * of "--" (FAMILY and CATALOG also carry RDS station names). Default
   * columns (Frequency/Candidate/Activity/Evidence/Last seen) are always on
   * and not part of this set -- see _renderTable/_renderLabelRow. */
  _visibleColumns() {
    const dims = new Set(this.status?.dimensions ?? []);
    const has = (...ids) => ids.some((id) => dims.has(id));
    // The explicit TECH preference applies in both Listen and Discover.
    const tech = this._techOn;
    return {
      snr: tech,
      bw_class: tech && has("bandwidth"),
      modulation: tech && has("modulation"),
      symbol_rate: tech && has("symbol_rate"),
      duty: tech,
      family: tech && has("modulation", "symbol_rate", "voice", "rds"),
      voice: tech && has("voice"),
      bandplan: tech && has("bandplan"),
      catalog: tech && has("catalog", "rds"),
    };
  }

  _setTableInteraction(kind, active) {
    this[kind] = active;
    this.requestUpdate();
  }

  _resizeTable(height) {
    this._tableHeight = clampTableHeight(height);
    // _renderTable() is memoised on a key that deliberately excludes the
    // height (re-rendering the table per pointer move is far too costly), so
    // push the new cap straight to the wrap for live feedback while dragging.
    const wrap = this.querySelector?.("#survey-labels-table");
    if (wrap) wrap.style.maxHeight = `${this._tableHeight}px`;
    try { localStorage.setItem(TABLE_HEIGHT_KEY, String(this._tableHeight)); } catch { /* best effort */ }
  }
  _startTableResize(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    this._tableDrag = { y: event.clientY, height: this._tableHeight ?? readTableHeight() };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  _moveTableResize(event) {
    const start = this._tableDrag;
    if (start) this._resizeTable(start.height + event.clientY - start.y);
  }
  _keyTableResize(event) {
    if (event.key === "Home") { event.preventDefault(); this._resizeTable(TABLE_HEIGHT_DEFAULT); return; }
    const delta = { ArrowUp: -20, ArrowDown: 20 }[event.key];
    if (!delta) return;
    event.preventDefault();
    this._resizeTable((this._tableHeight ?? readTableHeight()) + delta);
  }

  _renderTable() {
    const paused = this._activityFilter !== "speech" && !!(this._tablePointer || this._tableFocus);
    // Explicit actions remain usable while live updates are paused.
    const key = JSON.stringify([this._sortKey, this._sortOrder, this._activityFilter,
      this._evidenceFilter, this._snrMin, this._snrMax, this._resortSeq,
      this._decodePopoverId, this._selectedLabelId, this._techOn, this._optionsOpen,
      this.mode, this.status?.generation, this.status?.span?.center_hz,
      this.status?.span?.sample_rate_hz, this.status?.span?.vfo_hz, !!this.status?.replay]);
    if (!paused || !this._tableMarkup || key !== this._tableMarkupKey) {
      this._tableMarkup = this._renderLiveTable();
      this._tableMarkupKey = key;
    }
    return html`<section
      @mouseenter=${() => this._setTableInteraction("_tablePointer", true)}
      @mouseleave=${() => this._setTableInteraction("_tablePointer", false)}
      @focusin=${() => this._setTableInteraction("_tableFocus", true)}
      @focusout=${(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) this._setTableInteraction("_tableFocus", false);
      }}>
      <div class="dim" style="height:22px;font-size:0.8em" role="status">${paused
        ? "Table paused while interacting · spectrum stays live"
        : this._activityFilter === "speech" ? "Speech filter updates live" : "Quiet signals stay listed"}</div>
      ${this._activityFilter === "speech" ? html`<p class="dim" style="font-size:.8em">Recent tentative speech only · 3 s hold. IQ analysis is sampled and may miss short speech; all RF peaks remain visible above.
        ${!(this.status?.dimensions ?? []).includes("voice") ? html`<button @click=${() => this.dispatchEvent(new CustomEvent("survey-dimensions", {detail: {dimensions: [...(this.status?.dimensions ?? []), "voice"]}}))}>Enable voice analysis</button>` : ""}</p>` : ""}
      ${this._tableMarkup}
    </section>`;
  }

  _renderLiveTable() {
    const view = this._tableView();
    const labels = view.rows;
    const highlightedId = this._highlightedLabelId(labels);
    const analyzerActive = !!this.status?.analyzer?.active;
    const cols = this._visibleColumns();
    // "listen" mode drops the → DECODE column entirely (S1 contract: keeps
    // only frequency/candidate/evidence/TUNE/SAVE plus the always-on
    // activity/last-seen columns) -- see _renderLabelRow for the matching cell.
    const showDecode = this.mode !== "listen";
    // Always-on: MHz, CANDIDATE, ACTIVITY, EVIDENCE, LAST SEEN, TUNE, (DECODE), SAVE.
    const nCols = (showDecode ? 8 : 7) + Object.values(cols).filter(Boolean).length;
    const col = (on, width) => (on ? html`<col style=${width ? `width:${width}px` : ""} />` : "");
    const head = (on, key, text) => (on ? this._sortHeader(key, text) : "");
    return html`
      ${this._optionsOpen ? this._renderTableControls(view) : ""}
      <div class="control-row" style="justify-content:flex-end;gap:8px;margin-top:4px;min-height:28px">
        <label class="dim" style="font-size:.8em"><input type="checkbox" aria-label="Speech only"
          .checked=${this._activityFilter === "speech"}
          @change=${event => { this._activityFilter = event.target.checked ? "speech" : "all"; }}> Speech only</label>
        ${this._renderTableActions(view)}
        <span class="dim mono" style="font-size:0.8em">
          ${view.pending || view.gone
            ? `${[view.pending ? `${view.pending} new` : "", view.gone ? `${view.gone} quiet` : ""].filter(Boolean).join(" · ")} in this session`
            : view.stale ? "Values changed since last sort" : "Rows stay in place while values update"}
        </span>
        <button style="font-size:0.8em;padding:2px 8px" title="Sort session signals by current values"
          @click=${() => (this._resortSeq += 1)}>Re-sort rows</button>
      </div>
      <div class="survey-table-frame">
      <div id="survey-labels-table" class="lib-table-wrap" style=${`margin-top:4px;max-height:${this._tableHeight ?? readTableHeight()}px;overflow-y:auto`}>
        <table class="lib-table mono survey-table">
          <colgroup>
            <col style="width:104px" />
            <col style="width:120px" />
            <col style="width:90px" />
            ${col(cols.snr, 56)}
            ${col(cols.bw_class, 84)}
            ${col(cols.modulation)}
            ${col(cols.symbol_rate, 56)}
            ${col(cols.duty, 56)}
            ${col(cols.family)}
            ${col(cols.voice, 72)}
            ${col(cols.bandplan)}
            ${col(cols.catalog)}
            <col style="width:144px" />
            <col style="width:96px" />
            <col style="width:56px" />
            ${col(showDecode, 100)}
            <col style="width:64px" />
          </colgroup>
          <thead>
            <tr>
              ${this._sortHeader("frequency_hz", "MHz")}
              ${this._sortHeader("candidate", "CANDIDATE")}
              ${this._sortHeader("activity", "ACTIVITY")}
              ${head(cols.snr, "snr_db", "SNR")}
              ${head(cols.bw_class, "bw_class", "BW CLASS")}
              ${head(cols.modulation, "modulation", "MOD")}
              ${head(cols.symbol_rate, "symbol_rate_hz", "BAUD")}
              ${cols.duty ? html`<th title="Percent of the observation window this signal was active">DUTY</th>` : ""}
              ${head(cols.family, "family", "FAMILY")}
              ${head(cols.voice, "voice", "VOICE")}
              ${head(cols.bandplan, "bandplan", "BAND PLAN")}
              ${head(cols.catalog, "catalog", "CATALOG")}
              ${this._renderEvidenceHeader()}
              ${this._sortHeader("last_seen", "LAST SEEN")}
              <th>TUNE</th>
              ${showDecode ? html`<th>DECODE</th>` : ""}
              <th>SAVE</th>
            </tr>
          </thead>
          <tbody>
            ${labels.length
              ? labels.map((l) => this._renderLabelRow(l, cols, analyzerActive, highlightedId))
              : html`<tr>
                  <td colspan=${nCols} class="dim">
                    ${view.total > 0
                      ? html`No signals match the selected filters ·
                          <button type="button" class="link" @click=${() => this._clearFilters()}>clear filters</button>`
                      : this.status?.active
                        ? "No signals tracked yet — give it a few seconds"
                        : "Start SURVEY to see activity on this span"}
                  </td>
                </tr>`}
          </tbody>
        </table>
      </div>
      <button type="button" class="table-resize-bar" aria-label="Resize signal table"
        title="Drag to change the table height; ↑/↓ adjust; double-click or Home resets"
        @pointerdown=${(event) => this._startTableResize(event)}
        @pointermove=${(event) => this._moveTableResize(event)}
        @pointerup=${() => { this._tableDrag = null; }}
        @pointercancel=${() => { this._tableDrag = null; }}
        @lostpointercapture=${() => { this._tableDrag = null; }}
        @keydown=${(event) => this._keyTableResize(event)}
        @dblclick=${() => this._resizeTable(TABLE_HEIGHT_DEFAULT)}>╱╱ drag to resize</button>
      </div>
    `;
  }

  /** PEAK/AVG toggles plus the MIN/MAX dB contrast sliders and AUTO button
   * (S6 restyle): min/max drive both the waterfall palette mapping and the
   * spectrum pane's y-axis, and persist to localStorage across sessions. */
  _renderSpectrumControls() {
    const span = this.status?.span;
    const sampleRateHz = this._dirty && this._sampleRateHz != null
      ? this._sampleRateHz
      : span?.sample_rate_hz ?? this._sampleRateHz ?? this.status?.sample_rates_hz?.[0];
    const centerHz = span?.center_hz ?? this.receiverHz;
    const canStep = this.connected && !this.status?.replay && Number.isFinite(centerHz)
      && Number.isFinite(sampleRateHz) && sampleRateHz > 0;
    const halfSpanMHz = (Number.isFinite(sampleRateHz) ? sampleRateHz / 2_000_000 : 0).toFixed(3);
    const peakDb = this.wfSettings?.peak_threshold_db ?? 10;
    const peakPresets = [[0, "0 · Minimum"], [6, "6 · Sensitive"], [10, "10 · Default"],
      [14, "14 · Balanced"], [20, "20 · Strong"], [30, "30 · Very strong"], [60, "60 · Maximum"]];
    return html`
      <div class="control-row spectrum-toolbar">
        <div class="survey-row survey-row-sweep">
          <span class="survey-row-label">Sweep</span>
          <div class="survey-row-body">
        <div class="survey-band-step" role="group" aria-label="Shift survey span">
          <button aria-label="Step survey left by half the sampled span"
            title=${`Move survey center left by ${halfSpanMHz} MHz (50% overlap)`}
            ?disabled=${!canStep || centerHz <= sampleRateHz / 2}
            @click=${() => this._stepSpan(-1)}>←</button>
          <label class="survey-band-step-center">
            <input type="text" inputmode="decimal" aria-label="Survey center frequency (MHz)"
              title="Edit survey center frequency in MHz (explicit kHz or Hz units are accepted); press Enter to apply"
              .value=${this._centerText || (centerHz != null
                ? (centerHz / 1_000_000).toFixed(6)
                : Number.isFinite(this.receiverHz) ? (this.receiverHz / 1_000_000).toFixed(6) : "")}
              ?disabled=${!!this.status?.replay}
              @input=${(e) => { this._centerText = e.target.value; this._dirty = true; e.target.setCustomValidity(""); }}
              @keydown=${(e) => { if (e.key === "Enter") { e.preventDefault(); if (this._applySpan()) e.currentTarget.blur(); } }} />
            <span>MHz</span>
          </label>
          <button aria-label="Step survey right by half the sampled span"
            title=${`Move survey center right by ${halfSpanMHz} MHz (50% overlap)`}
            ?disabled=${!canStep}
            @click=${() => this._stepSpan(1)}>→</button>
          <span class="survey-step-size" title="Each ← / → moves the survey centre by half the sampled span (50% overlap)">step ${halfSpanMHz} MHz</span>
        </div>
${!this.status?.replay ? this._renderPresetControls() : ""}
          ${!this.status?.replay ? html`<label class="survey-sampling-control">
            <span>Rate</span>
            <select aria-label="Survey sample rate / width"
              ?disabled=${!this.connected || !!this.status?.replay}
              @change=${(e) => { this._sampleRateHz = Number(e.target.value); this._dirty = true; this._applySpan(); }}>
              ${(this.status?.sample_rates_hz ?? []).map((hz) => html`<option value=${hz}
                ?selected=${hz === (this._sampleRateHz ?? this.status?.span?.sample_rate_hz)}>
                ${(hz / 1_000_000).toFixed(1)} MS/s
              </option>`)}
            </select>
          </label>` : ""}
${!this.status?.replay ? this._renderKeepVfo() : ""}
          </div>
        </div>
        <div class="survey-row survey-row-detect">
          <span class="survey-row-label">Detect</span>
          <div class="survey-row-body">
        <div class="survey-peak-threshold" role="group" aria-label="DSP peak sensitivity"
          title="Threshold above the measured noise floor, 0–60 dB. Higher values show fewer peaks. Survey history stays in the table.">
          <span class="survey-peak-label">Peak ≥</span>
          <button aria-label="Decrease DSP peak threshold" ?disabled=${!this.wfSettings || this.peakBusy || peakDb <= 0}
            @click=${() => this._setPeakThreshold(peakDb - 1)}>−</button>
          <select aria-label="DSP peak threshold, dB over noise floor" ?disabled=${!this.wfSettings || this.peakBusy}
            @change=${(e) => {
              this._setPeakThreshold(Number(e.target.value));
              e.target.value = String(peakDb);
            }}>
            ${!peakPresets.some(([db]) => db === peakDb)
              ? html`<option value=${peakDb} selected>${peakDb} · Custom</option>` : ""}
            ${peakPresets.map(([db, name]) => html`<option value=${db} ?selected=${db === peakDb}>${name}</option>`)}
          </select>
          <button aria-label="Increase DSP peak threshold" ?disabled=${!this.wfSettings || this.peakBusy || peakDb >= 60}
            @click=${() => this._setPeakThreshold(peakDb + 1)}>+</button>
          <span>${this.peakBusy ? "Saving…" : "dB"}</span>
        </div>
        <div class="survey-trace-controls" role="group" aria-label="Spectrum trace controls">
          <span class="survey-group-label">Trace</span>
          <span class="survey-seg">
            <button aria-pressed=${this._peakHold} title="Peak hold: keep the highest level seen in each bin" @click=${() => this._togglePeak()}>Peak</button>
            <button aria-pressed=${this._avgSmooth} title="Average: smooth the trace over successive frames" @click=${() => this._toggleAvg()}>Avg</button>
          </span>
          <button
            class="survey-snap"
            title="Snap clicks to the nearest detected signal (Alt/Option-click bypasses, Shift-click opens details)"
            aria-pressed=${this._snapOn}
            @click=${() => this._toggleSnap()}
          >Snap</button>
        </div>
        <span class="survey-display-wrap">
          <button type="button" class="survey-display-toggle"
            aria-expanded=${this._displayOpen} aria-haspopup="dialog"
            title="Display: spectrum/waterfall levels and zoom"
            @click=${() => this._setDisplayOpen(!this._displayOpen)}>Display ▾</button>
          ${this._displayOpen ? html`<div class="survey-display-popover" role="dialog" aria-label="Display settings">
            <div class="survey-toolbar-group survey-level-controls" role="group" aria-label="Spectrum levels">
              ${this._renderDbRangeControls()}
            </div>
            <div class="survey-toolbar-group" role="group" aria-label="Spectrum zoom">
              <span class="survey-group-label">Zoom</span>
              <button aria-label="Zoom spectrum out" @click=${() => this._setZoom(this._zoom / 2)}>−</button>
              <span class="mono">${Number(this._zoom.toFixed(2))}×</span>
              <button aria-label="Zoom spectrum in" @click=${() => this._setZoom(this._zoom * 2)}>+</button>
              <button @click=${() => { this._pan = 0.5; this._setZoom(1); }}>Full span</button>
              ${this._zoom > 1 ? html`<label class="dim mono">PAN
                <input aria-label="Pan spectrum" type="range" min=${0.5 / this._zoom} max=${1 - 0.5 / this._zoom} step="0.001"
                  .value=${String(this._pan)} @input=${(e) => { this._pan = Number(e.target.value); this._scheduleDraw(); }} />
              </label>` : ""}
            </div>
          </div>` : ""}
        </span>
          </div>
        </div>
      </div>
    `;
  }

  _renderWaterfallControls() {
    return html`<label class="dim mono">DSP MARK HOLD
      <select aria-label="DSP marker hold" ?disabled=${this.peakBusy}
        .value=${String(this.wfSettings?.marker_hold_s ?? 15)}
        @change=${event => this.dispatchEvent(new CustomEvent("survey-marker-settings", {detail: {marker_hold_s: Number(event.target.value)}}))}>
        ${[...new Set([0,5,15,30,60,this.wfSettings?.marker_hold_s ?? 15])].sort((a,b)=>a-b).map(seconds => html`<option value=${seconds} ?selected=${seconds === (this.wfSettings?.marker_hold_s ?? 15)}>${seconds ? `${seconds} s` : "Off"}</option>`)}
      </select></label>
      <label class="dim mono" title="Retain more waterfall frames to see older bursts. Duration depends on incoming frame rate; older history fills as data arrives.">
      HISTORY
      <select aria-label="Waterfall history" .value=${String(this._historyOverride ?? "setup")}
        @change=${(e) => this._setHistoryRows(e.target.value)}>
        <option value="setup">Setup default</option>
        <option value="300">Short · 300 frames</option>
        <option value="1000">Medium · 1,000 frames</option>
        <option value="2000">Long · 2,000 frames</option>
      </select>
    </label>`;
  }

  _renderDbRangeControls() {
    const wfMin = Math.round(this._wfMin ?? -145);
    const wfMax = Math.round(this._wfMax ?? -20);
    const btnStyle = (on) => `font-size:0.8em${on ? ";background:var(--accent);color:#0b1220;border-color:var(--accent)" : ""}`;
    return html`<span class="survey-group-label" title="Display contrast for spectrum and waterfall">LEVELS</span>
        <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.8em">
          MIN
          <input
            type="range"
            min="-160"
            max="0"
            step="1"
            .value=${String(wfMin)}
            @input=${(e) => this._setWfMin(Number(e.target.value))}
          />
          ${wfMin} dB
        </label>
        <span style="width:6px" aria-hidden="true"></span>
        <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.8em">
          MAX
          <input
            type="range"
            min="-160"
            max="0"
            step="1"
            .value=${String(wfMax)}
            @input=${(e) => this._setWfMax(Number(e.target.value))}
          />
          ${wfMax} dB
        </label>
        <button
          title="AUTO: keep MIN/MAX following the noise floor (moving a slider turns it off)"
          aria-pressed=${this._wfAuto}
          style=${btnStyle(this._wfAuto)}
          @click=${() => this._setWfAuto(!this._wfAuto)}
        >
          AUTO
        </button>
        <button
          title="Clear the local MIN/MAX override so SETUP's values apply again"
          style="font-size:0.8em"
          @click=${() => this._resetWfRange()}
        >
          RESET
        </button>
        <span class="survey-divider" aria-hidden="true"></span>
`;
  }

  /** Hover card beside the pointer, flipped/clamped at visible canvas edges:
   * the nearest label's details when the cursor is
   * within its snap radius, else just the cursor frequency. See
   * _nearestLabel for the radius rule and _onCanvasPointerMove for how
   * _hoverHz/_hoverNearest get set. */
  _renderHoverCard() {
    if (this._hoverHz == null) return "";
    let nearest = this._hoverNearest?.withinRadius ? this._hoverNearest.label : null;
    if (nearest && Array.isArray(this.status?.spectrum_markers)) {
      nearest = this._spectrumLabels().find(label => label.id === nearest.id) ?? null;
    }
    const cardStyle =
      "position:absolute;top:0;left:0;z-index:5;pointer-events:none;max-width:360px;white-space:normal;overflow-wrap:anywhere;" +
      "background:var(--panel-well);border:1px solid var(--line);border-radius:3px;padding:4px 8px;" +
      "font-size:0.78em;line-height:1.4";
    const exclusion = this.status?.peak_detection?.center_exclusion;
    if (exclusion && this._hoverHz >= exclusion.start_hz && this._hoverHz <= exclusion.end_hz) {
      return html`<div class="survey-hover-card mono" style=${cardStyle}><b>${(this._hoverHz / 1_000_000).toFixed(3)} MHz</b><div>Center/DC exclusion: peak detection is disabled here.</div><div>Offset the receiver center to inspect this frequency.</div></div>`;
    }
    if (!nearest) {
      return html`<div class="survey-hover-card mono" style=${cardStyle}><b>${(this._hoverHz / 1_000_000).toFixed(3)} MHz</b><div>No detected peak at this frequency.</div></div>`;
    }
    const l = nearest;
    const saved = nearestSavedFrequency(l.frequency_hz, this.savedSignals, this.favorites);
    const cw = hasCwOutput(l);
    const identity = cw ? "CW · decoder output (unchecked)" : l.rds?.ps || l.rds?.callsign
      ? rdsName(l.rds)
      : l.confirmed ? `${(l.confirmed.decoder ?? "").toUpperCase()} · decoder output`
      : l.family?.name ? shortFamilyName(l.family.name) : "Unclassified signal";
    const action = cw ? "Verify Morse text" : l.family?.suggested_decoders?.length
      ? `Try ${l.family.suggested_decoders.join(" / ").toUpperCase()} decoding`
      : "Inspect signal";
    return html`
      <div class="survey-hover-card mono" style=${cardStyle}>
        ${allocationServicesText(l.allocation)
          ? html`<div class="survey-report-bandplan" style="color:#f2c94c;font-size:0.85em;margin-bottom:5px">ALLOCATION · ${allocationServicesText(l.allocation)} <span class="dim">(allocation only)</span></div>`
          : l.bandplan?.name ? html`<div class="survey-report-bandplan" style="color:#f2c94c;font-size:0.85em;margin-bottom:5px">BAND PLAN · ${l.bandplan.name} <span class="dim">(allocation only)</span></div>` : ""}
        <div style="display:flex;justify-content:space-between;gap:16px"><b>${(l.frequency_hz / 1_000_000).toFixed(3)} MHz</b><span class="dim">SNR ${l.snr_db != null ? `${l.snr_db.toFixed(1)} dB` : "—"}</span></div>
        ${saved ? html`<div class="nearby-saved-pill" title="Frequency proximity to an operator-saved channel; not a signal identification">
          ${saved.distanceHz > 1 ? "Nearby saved" : "Saved channel"}${saved.favorite ? " ★" : ""} · ${saved.name} · ${(saved.frequencyHz / 1_000_000).toFixed(6)} MHz${saved.distanceHz > 1 ? ` · ${bw(saved.distanceHz)} away` : ""}
        </div>` : ""}
        <div style="margin:5px 0;font-weight:600">${identity}</div>
        ${l.last_classification ? html`<div style="color:#c6a0ff;font-size:.85em">Last detected: ${shortFamilyName(l.last_classification.name)} · ${Math.max(0, Math.round(Date.now() / 1000 - l.last_classification.at))}s ago <span class="dim">(estimate)</span></div>` : ""}
        ${l._markerRetained ? html`<div>Last seen ${Math.floor(l._markerAge)} seconds ago · retained marker, no current signal</div>` : ""}
        <div>${this._signalActivity(l).text}</div>
        <div class="survey-analysis-freshness">DSP analysis · ${ANALYSIS_STATE_LABELS[this._analysisState(l)]}</div>
        ${cw && l.confirmed.sample ? html`<div style="max-height:2.8em;overflow:hidden">${String(l.confirmed.sample).slice(0, 100)}</div>` : ""}
        <div class="dim" style="font-size:0.85em;margin-top:5px">${action} · Double-click / Shift-click for details</div>
      </div>
    `;
  }

  /** Row 1 "Source" control: Live (default) vs a replayed recording. The
   * REPLAY… button opens the file picker; while replaying, the transport
   * (play/seek/loop/stop) takes its place. */
  _renderSource() {
    const status = this.status ?? {};
    if (status.replay) return this._renderReplayTransport(status.replay);
    return html`
      <div class="survey-source" role="group" aria-label="Survey source">
        <span class="survey-source-live" title="Surveying the live SDRconnect stream">Live</span>
        ${this.mode === "listen"
          ? ""
          : html`<button
              class="survey-replay-button"
              aria-pressed=${this._replayPickerOpen}
              title="Survey a recorded SigMF file instead of the live SDRconnect stream"
              @click=${() => this._toggleReplayPicker()}
            >⏵ REPLAY…</button>`}
      </div>
    `;
  }

  /** Row 2 span preset (hidden while replaying a file). */
  _renderPresetControls() {
    const presets = this.status?.presets ?? [];
    return html`
      <select aria-label="Survey span preset" @change=${(e) => this._applyPreset(e.target.value)}>
        <option value="">Preset…</option>
        ${presets.map((p) => html`<option value=${p.name}>${p.name}</option>`)}
      </select>
    `;
  }

  /** Row 2 keep-VFO: leave the receiver's VFO alone when the span changes. */
  _renderKeepVfo() {
    return html`<label class="dim mono survey-keep-vfo"
      title="When the sweep centre or rate changes, put the tuned VFO back where it was if it is still inside the new span">
      <input type="checkbox" .checked=${this._keepVfo} @change=${(e) => (this._keepVfo = e.target.checked)} />
      keep VFO
    </label>`;
  }

  _toggleReplayPicker() {
    this._replayPickerOpen = !this._replayPickerOpen;
    if (this._replayPickerOpen && this._replayRecordings == null) this._loadReplayRecordings();
  }

  /** Reads msdr's own recordings list directly (like recordings-view.js
   * does) -- purely a lookup for the picker's <select>, not a mutation, so
   * it doesn't need to round-trip through app.js. */
  async _loadReplayRecordings() {
    try {
      this._replayRecordings = await api.listRecordings();
    } catch {
      this._replayRecordings = []; // the free-text path still works
    }
  }

  _startReplay() {
    const path = this._replayPath.trim();
    if (!path) return;
    this.dispatchEvent(new CustomEvent("survey-replay", { detail: { path, loop: this._replayLoop } }));
  }

  /** Inline "⏵ REPLAY…" picker row: a free-text file path, an optional pick
   * from msdr's own recordings (GET /api/recordings), a loop checkbox and
   * ▶ PLAY -- POST /api/survey/replay (see app.js's _doSurveyReplay). */
  _renderReplayPicker() {
    const recordings = this._replayRecordings ?? [];
    return html`
      <div class="control-row" style="flex-wrap:wrap;margin-top:4px">
        <input
          type="text"
          placeholder="SDRconnect_IQ_…_<Hz>HZ.wav or an msdr recording folder"
          style="width:420px"
          .value=${this._replayPath}
          @input=${(e) => (this._replayPath = e.target.value)}
        />
        <select
          aria-label="msdr recordings"
          @change=${(e) => {
            if (e.target.value) this._replayPath = e.target.value;
            e.target.value = "";
          }}
        >
          <option value="">msdr recordings…</option>
          ${recordings.map(
            (r) => html`<option value=${r.path}>
              ${r.name} · ${r.center_hz != null ? mhz(r.center_hz) : "--"} · ${mmss(r.duration_s)}
            </option>`,
          )}
        </select>
        <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
          <input type="checkbox" .checked=${this._replayLoop} @change=${(e) => (this._replayLoop = e.target.checked)} />
          loop
        </label>
        <button class="run-start" ?disabled=${!this._replayPath.trim()} @click=${() => this._startReplay()}>
          ▶ PLAY
        </button>
      </div>
    `;
  }

  _replayControl(body) {
    this.dispatchEvent(new CustomEvent("survey-replay-control", { detail: body }));
  }

  /** Transport shown instead of the span picker while status.replay is set:
   * file/centre/rate readout, ⏸/▶, a seek slider (only reflects
   * status.position_s while the operator isn't dragging it, so a status push
   * mid-drag doesn't yank the handle back -- mirrors _syncSpanDraft's _dirty
   * guard), loop toggle and ⏹ STOP REPLAY. */
  _renderReplayTransport(replay) {
    const duration = replay.duration_s ?? 0;
    const position = this._replayDragging ? this._replayDragPositionS : (replay.position_s ?? 0);
    return html`
      <div class="control-row" style="flex-wrap:wrap;gap:8px">
        <span class="dim mono" style="font-size:0.85em">${replay.name ?? replay.path}</span>
        <span class="dim mono" style="font-size:0.85em">
          ${replay.center_hz != null ? mhz(replay.center_hz) : "--"} ·
          ${replay.sample_rate_hz != null ? `${(replay.sample_rate_hz / 1e6).toFixed(1)} MS/s` : "--"}
        </span>
        <button
          style="font-size:0.85em"
          title=${replay.playing ? "Pause" : "Play"}
          @click=${() => this._replayControl({ playing: !replay.playing })}
        >
          ${replay.playing ? "⏸" : "▶"}
        </button>
        <input
          type="range"
          min="0"
          max=${duration}
          step="0.1"
          style="flex:1;min-width:120px"
          .value=${String(position)}
          @input=${(e) => {
            this._replayDragging = true;
            this._replayDragPositionS = Number(e.target.value);
          }}
          @change=${(e) => {
            this._replayDragging = false;
            this._replayControl({ position_s: Number(e.target.value) });
          }}
        />
        <span class="dim mono" style="font-size:0.85em">${mmss(position)} / ${mmss(duration)}</span>
        <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
          <input
            type="checkbox"
            .checked=${!!replay.loop}
            @change=${(e) => this._replayControl({ loop: e.target.checked })}
          />
          loop
        </label>
        <button class="run-stop" style="font-size:0.85em" @click=${() => this._stopReplay()}>⏹ STOP REPLAY</button>
      </div>
    `;
  }

  _stopReplay() {
    this.dispatchEvent(new CustomEvent("survey-replay-stop"));
  }

  render() {
    const status = this.status ?? {};
    const active = !!status.active;
    const fps = status.stream?.fps;
    const listening = this.mode === "listen";
    const analyzerStatus = listening ? "" : this._renderAnalyzerStatus();
    return html`
      <div class="panel">
        <div class="survey-visual">
        <div class="survey-topbar">
          <h2 style="margin:0;border-bottom:none;padding-bottom:0">Survey</h2>
          ${status.replay
            ? html`<span
                class="mono"
                style="font-size:0.7em;padding:1px 6px;border:1px solid var(--accent);color:var(--accent)"
                title="Surveying a recorded file, not the live SDRconnect stream"
                >REPLAY</span
              >`
            : ""}
          <button class=${active ? "run-stop" : "run-start"} ?disabled=${!this.connected} @click=${() => this._toggle()}>
            ${active ? "■ STOP" : "▶ START"}
          </button>
          ${active ? html`<run-indicator .since=${status.since ?? null}></run-indicator>` : ""}
          <button
            class="survey-clear-button"
            title="Forget everything found on this span (signals, occupancy, analyzer and RDS results) and start fresh"
            ?disabled=${!this.connected}
            @click=${() => this.dispatchEvent(new CustomEvent("survey-clear"))}
          >
            ⟲ CLEAR
          </button>
          <div class="survey-tune"><span class="survey-group-label">Source</span>${this._renderSource()}</div>
          <span class="spacer"></span>
          <span class="dim mono" style="font-size:0.85em" title="spectrum frames per second from SDRconnect">
            ${fps != null ? `${fps.toFixed(1)} fps` : "-- fps"}
          </span>
        </div>
        ${this.mode !== "listen" && !status.replay && this._replayPickerOpen ? this._renderReplayPicker() : ""}
        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}
        <div class="survey-toolbar-scroll" aria-label="Spectrum display controls">
          ${this._renderSpectrumControls()}
        </div>
        ${this.mode === "discover" ? this._renderBandScan() : ""}
        <div style="position:relative;margin-top:3px">
          <canvas
            class="survey-canvas"
            role="application"
            tabindex="0"
            aria-label="Spectrum and waterfall. Click to tune. Shift-click to inspect without tuning. Double-click opens inspection after the first click tunes. Drag the plot to pan; drag the tuning line to tune on release. Use the wheel to zoom, plus or minus to zoom, arrows to pan, Shift plus arrows to step the receiver, and Home to reset."
            style="width:100%;display:block;background:var(--panel-well);cursor:grab;touch-action:none;user-select:none"
            @click=${(e) => this._queueCanvasClick(e)}
            @dblclick=${(e) => this._onCanvasDoubleClick(e)}
            @wheel=${(e) => this._onCanvasWheel(e)}
            @pointerdown=${(e) => this._onCanvasPointerDown(e)}
            @pointermove=${(e) => { this._onCanvasPointerMoveGesture(e); this._onCanvasPointerMove(e); }}
            @pointerup=${(e) => this._onCanvasPointerUp(e)}
            @pointercancel=${(e) => this._onCanvasPointerCancel(e)}
            @lostpointercapture=${(e) => this._onCanvasPointerCancel(e)}
            @pointerleave=${() => this._onCanvasPointerLeave()}
            @keydown=${(e) => this._onCanvasKeyDown(e)}
          ></canvas>
          <button aria-label="Resize spectrum" title="Drag up or down to resize the spectrum; arrow keys adjust height"
            class="survey-resize-handle survey-resize-spectrum"
            style=${`top:${Math.max(0, this._spectrumGripTop - 24)}px`}
            @pointerdown=${(e) => this._startWaterfallResize(e, true)}
            @click=${(e) => e.stopPropagation()}
            @keydown=${(e) => {
              if (!["ArrowUp", "ArrowDown"].includes(e.key)) return;
              e.preventDefault();
              this._spectrumHeight = Math.max(80, Math.min(800, this._spectrumGripTop + (e.key === "ArrowDown" ? 25 : -25)));
              try { localStorage.setItem("msdr.spectrum.height", String(this._spectrumHeight)); } catch {}
              this._resizeCanvas(); this._scheduleDraw();
            }}><svg aria-hidden="true" width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M4 17L17 4M10 17L17 10M16 17L17 16" />
            </svg></button>
          ${!status.stream?.connected
            ? html`<div
                style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--ink);font-family:var(--mono);pointer-events:none"
              >
                no frames
              </div>`
            : ""}
          ${this._renderHoverCard()}
          <button aria-label="Resize waterfall" title="Drag up or down to stretch the waterfall; arrow keys adjust height"
            class="survey-resize-handle survey-resize-waterfall"
            @pointerdown=${(e) => this._startWaterfallResize(e)}
            @click=${(e) => e.stopPropagation()}
            @keydown=${(e) => {
              if (!["ArrowUp", "ArrowDown"].includes(e.key)) return;
              e.preventDefault();
              this._waterfallHeight = Math.max(100, Math.min(1200, (this._waterfallHeight || 150) + (e.key === "ArrowDown" ? 25 : -25)));
              try { localStorage.setItem("msdr.waterfall.height", String(this._waterfallHeight)); } catch {}
              this._resizeCanvas(); this._scheduleDraw();
            }}><svg aria-hidden="true" width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M4 17L17 4M10 17L17 10M16 17L17 16" />
            </svg></button>
        </div>
        <div aria-label="Waterfall display controls" style="display:flex;flex-wrap:wrap;justify-content:flex-end;align-items:center;gap:8px;padding-top:3px;font-size:0.85em">
          ${this._renderWaterfallControls()}
        <label class="dim mono" style="display:flex;align-items:center;gap:6px;font-size:0.75em">
          Height
          <select aria-label="Waterfall height" .value=${String(this._waterfallHeight)}
            @change=${(e) => {
              this._waterfallHeight = Number(e.target.value);
              try { localStorage.setItem("msdr.waterfall.height", String(this._waterfallHeight)); } catch {}
              this._resizeCanvas();
              this._scheduleDraw();
            }}>
            ${this._waterfallHeight && ![150, 300, 500, 700].includes(this._waterfallHeight)
              ? html`<option value=${String(this._waterfallHeight)}>Custom · ${this._waterfallHeight}px</option>` : ""}
            <option value="0">Auto</option>
            <option value="150">Compact</option>
            <option value="300">Medium</option>
            <option value="500">Tall</option>
            <option value="700">Extra tall</option>
          </select>
        </label>
        </div>
        ${this.status?.peak_detection?.center_exclusion ? html`<p class="dim" style="font-size:.78em;margin:4px 0">Amber band: center/DC exclusion — peaks here are not detected. Offset the receiver center to inspect a signal in this band. DSP markers show measured RF, not confirmed speech.</p>` : ""}
        ${this._renderEvidenceLegend()}
        ${this._renderBadgeLane()}
        </div>
        <div class="survey-results">
          <h3 class="survey-results-title">Signal measurements</h3>
        ${this._renderTable()}
        ${!listening ? html`<div class="survey-controls">
                <span class="survey-group-label">Options</span>
                ${this._renderOptionsToggle()}
                ${this._optionsOpen
                  ? html`
                      <span class="survey-group-label">Identify</span>
                      ${this._renderDimensions()}
                      ${analyzerStatus
                        ? html`<span class="survey-group-label">Status</span>
                            <div class="survey-status mono">${analyzerStatus}</div>`
                        : ""}
                    `
                  : ""}
              </div>` : ""}
        ${this._renderValidationCapture()}
        ${this.showMlControls !== false && this.status?.ml_config?.available !== false ? html`
          <details class="survey-ml-section" ?open=${this._mlSectionOpen}
            @toggle=${(event) => {
              if (event.target !== event.currentTarget) return;
              this._mlSectionOpen = event.currentTarget.open;
              saveMlSectionOpen(this._mlSectionOpen);
            }}>
            <summary>Experimental ML · unvalidated results${this._mlStatusSummary()}</summary>
            <div class="survey-ml-section-content">
              ${this._renderMlStatusStrip()}
              ${this._renderMlProposals()}
              <details class="survey-ml-classifications">
                <summary>ML classifications · unvalidated</summary>
                ${this._renderMlClassifications()}
              </details>
            </div>
          </details>` : ""}
        </div>
      </div>
    `;
  }
}

customElements.define("survey-view", SurveyView);
