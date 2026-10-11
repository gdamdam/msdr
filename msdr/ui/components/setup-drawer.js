import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import { containModalFocus, restoreModalFocus, trapModalTab } from "./modal-focus.js";
import {
  resolveFeatures,
  resolveWorkspaceLayout,
  sectionVisible,
  SECTION_LABELS,
  LAYOUT_PRESETS,
  WORKSPACE_LABELS,
  WORKSPACE_SECTIONS,
  DEFAULT_WORKSPACE_COLUMNS,
  utcOffsetLabel,
  readinessLabels,
  readout,
} from "../format.js";
import "./led-dot.js";
import "./failure-note.js";
import "./integration-panel.js";
import "./backup-control.js";
import "./wifi-setup-section.js";
import "./ble-setup-section.js";

// Must match msdr/providers/nextpass.py SATELLITES (see contract.md); same
// list as drawer.js's Memory Edit satellite field.
const SATELLITES = ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"];

const DATA_PATH_HINT = "~/Library/Application Support/msdr";

// docs/allocations.md "Backend integration": Settings.allocation_jurisdiction,
// a separate setting from `region` (which keeps its current meaning for
// identification).
const ALLOCATION_JURISDICTIONS = [
  ["US", "US"],
  ["ITU1", "ITU Region 1"],
  ["ITU2", "ITU Region 2"],
  ["ITU3", "ITU Region 3"],
];

// Official source pages help operators locate new schedules. These are
// update/reference links, not promises that the source refreshes itself.
// GET /api/catalog/sources `available` rows' `kind` (docs/reference-catalogs.md
// "API and UI"): a word plus a one-line plain-English explanation for each
// CATALOG source row -- "live" especially needs the caveat since it's easy to
// misread a live metadata list as proof of a current transmission.
const CATALOG_KIND_LABELS = { static: "static", refreshable: "refreshable", live: "live" };
const CATALOG_KIND_EXPLANATIONS = {
  static: "Dated bundled snapshot.",
  refreshable: "Downloaded on refresh.",
  live: "Live metadata list, not a transmission indicator.",
};

/** {text, title} for a catalog source's `kind`, or null when missing (an
 * older backend response that predates the field). An unrecognized kind
 * still renders its raw value rather than disappearing. */
/** Beacons NDB picker. isNdbRow: the FAA importer tags NDB-family rows with
 * component "NDB"/"MARINE NDB"; the frequency band is the fallback for rows
 * from another source. mergeNdbFrequencies edits the textarea's one-Hz-per-line
 * text, keeping manual entries, de-duplicated and numerically sorted. */
export function isNdbRow(row) {
  if (/NDB/i.test(row?.component ?? "")) return true;
  const hz = row?.frequency_hz;
  return Number.isFinite(hz) && hz >= 190000 && hz <= 535000;
}

export function mergeNdbFrequencies(text, hz, checked) {
  const set = new Set(
    String(text ?? "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map(Number)
      .filter(Number.isFinite)
      .map(Math.round)
  );
  const v = Math.round(hz);
  if (checked) set.add(v);
  else set.delete(v);
  return [...set].sort((a, b) => a - b).join("\n");
}

export function catalogSourceKindInfo(kind) {
  if (!kind) return null;
  return { text: CATALOG_KIND_LABELS[kind] ?? kind, title: CATALOG_KIND_EXPLANATIONS[kind] ?? "" };
}

const CATALOG_SOURCE_INFO = {
  eibi: {
    url: "https://eibispace.de/",
    label: "EiBi season downloads",
    builtinNote: "Season URL selected automatically at refresh time",
  },
  aoki: { url: "https://www1.s2.starcat.ne.jp/ndxc/", label: "Aoki schedule index" },
  hfcc: { url: "https://new.hfcc.org/data/", label: "HFCC schedule index" },
  ndxc: {
    url: "https://ndxc.info/wordpress/wp-content/uploads/freqdb/sked.csv",
    label: "NDXC schedule file",
    builtin: "https://ndxc.info/wordpress/wp-content/uploads/freqdb/sked.csv",
  },
  noaa_radiofax: {
    url: "https://www.weather.gov/marine/radiofax",
    label: "NOAA Radiofax information",
    builtinNote: "Bundled reference table · updated with msdr releases",
  },
  sigidwiki: {
    url: "https://www.sigidwiki.com/wiki/Signal_Identification_Guide",
    label: "SigIDWiki reference",
    builtinNote: "Bundled reference table · updated with msdr releases",
  },
  priyom: {
    url: "https://priyom.org/number-stations/station-schedule",
    label: "Priyom schedule",
    builtin: "https://calendar.priyom.org/events",
    builtinNote: "To use the official live calendar, clear this URL and Save; refresh then uses the official API.",
  },
  satnogs: {
    url: "https://db.satnogs.org/",
    label: "SatNOGS DB",
    builtin: "https://db.satnogs.org/api/transmitters/?format=json&alive=true",
  },
};

// Order/labels for the FEATURES group -- keys must match state.features
// (see msdr/ui/format.js DEFAULT_FEATURES).
const FEATURE_FLAGS = [
  ["detect", "DETECT"],
  ["ident", "IDENT"],
  ["nearby", "NEARBY"],
  ["journal_passive", "PASSIVE JOURNAL"],
  ["passes", "PASSES"],
  ["scans", "ANALYZE RECORDING"],
  ["sky", "SKY"],
  ["tracking", "TRACKING"],
  ["catalog", "CATALOG"],
  ["telemetry", "TELEMETRY"],
  ["scanner", "CHANNEL SCANNER (VFO / LIST)"],
  ["recorder", "RECORDER"],
  ["decoder", "DECODER"],
  ["survey", "WATCH SPECTRUM"],
  ["beacons", "BEACONS"],
  ["finds", "FINDS"],
  ["ml_controls", "SHOW EXPERIMENTAL ML CONTROLS"],
];

/** True when every key in both string dicts has the same value (missing =
 * ""), order-independent -- used to detect an edited catalog_sources dict
 * without assuming a fixed set of keys (see PART B: sources are data-driven). */
function sameStringDict(a, b) {
  const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  for (const k of keys) {
    if ((a?.[k] ?? "") !== (b?.[k] ?? "")) return false;
  }
  return true;
}

function draftFromSettings(s) {
  const loc = s.location ?? null;
  return {
    evidencePanelStyle: s.evidence_panel_style ?? "tinted",
    host: s.host ?? "",
    recorderRoot: s.recorder_root ?? "",
    validationSamplesDir: s.validation_samples_dir ?? "",
    port: s.port ?? "",
    dwellSeconds: s.dwell_seconds ?? "",
    nearbyKhz: s.nearby_hz != null ? s.nearby_hz / 1000 : "",
    region: s.region ?? "US",
    allocationJurisdiction: s.allocation_jurisdiction ?? "US",
    // Settings.timezone: null = auto-detect (state.timezone reports the
    // effective name/offset either way, see _timezoneEffectiveLabel).
    timezoneAuto: s.timezone == null,
    timezoneName: s.timezone ?? "",
    passesDays: s.passes_days ?? "",
    passesMinElevation: s.passes_min_elevation ?? "",
    passesHorizon: s.passes_horizon ?? "",
    passesTop: s.passes_top ?? "",
    satellites: new Set(s.passes_satellites ?? []),
    extraSatellites: (s.passes_extra_satellites ?? []).map((e) => ({ ...e })),
    newExtraLabel: "",
    newExtraNorad: "",
    visibleOnly: !!s.passes_visible_only,
    overrideLocation: !!loc,
    lat: loc?.lat ?? "",
    lon: loc?.lon ?? "",
    altitudeM: loc?.altitude_m ?? "",
    timezone: loc?.timezone ?? "",
    features: resolveFeatures(s.features),
    // U2: the LAYOUT group always edits the *active* workspace (the one
    // app.js's tab strip currently has selected) -- see _layoutGroup/
    // _patchLayout below and Settings.workspace (msdr/settings.py).
    workspace: s.workspace ?? "listen",
    layout: resolveWorkspaceLayout(s.workspaces, s.workspace ?? "listen"),
    catalogSources: { ...(s.catalog_sources ?? {}) },
    catalogLanguages: (s.catalog_languages ?? []).join(", "),
    importPath: "",
    importFormat: "sdrpp",
    decoderBinDirsText: (s.decoder?.bin_dirs ?? []).join("\n"),
    decoderAudioSampleRate: s.decoder?.audio_sample_rate ?? "",
    loraSpreadingFactor: s.decoder?.lora_spreading_factor ?? 11,
    loraBandwidth: s.decoder?.lora_bandwidth_hz ?? 250000,
    loraCodingRate: s.decoder?.lora_coding_rate ?? 1,
    loraProtocol: s.decoder?.lora_protocol ?? "lora",
    surveyThresholdDb: s.survey?.threshold_db ?? 8,
    surveySpectrumFps: s.survey?.spectrum_fps ?? 10,
    surveyPeakThresholdDb: s.survey?.peak_threshold_db ?? 10,
    surveyCenterExclusion: s.survey?.center_exclusion_enabled ?? false,
    surveyLabelTtlS: s.survey?.label_ttl_s ?? 120,
    // blank = keep recognised signals for the whole session (recognized_ttl_s: null)
    surveyRecognizedTtlS: s.survey?.recognized_ttl_s != null ? s.survey.recognized_ttl_s : "",
    surveyMaxAnalyzed: s.survey?.max_analyzed ?? 6,
    surveyAnalyzeDwellS: s.survey?.analyze_dwell_s ?? 2.0,
    surveyRdsOddChannels: !!s.survey?.rds_odd_channels,
    // Waterfall group -- blank min/max text means "auto" (wf_min_db/wf_max_db: null).
    wfMinDb: s.survey?.wf_min_db != null ? s.survey.wf_min_db : "",
    wfMaxDb: s.survey?.wf_max_db != null ? s.survey.wf_max_db : "",
    wfAvgFactor: s.survey?.wf_avg_factor ?? 0.5,
    wfPeakHold: !!s.survey?.wf_peak_hold,
    wfPalette: s.survey?.wf_palette ?? "sdrsharp",
    wfHistoryRows: s.survey?.wf_history_rows ?? 300,
    wfShowGrid: s.survey?.wf_show_grid ?? true,
    beaconsSnrThresholdDb: s.beacons?.snr_threshold_db ?? 6.0,
    beaconsNdbFrequenciesText: (s.beacons?.ndb_frequencies_hz ?? []).map((hz) => Math.round(hz)).join("\n"),
  };
}

/** Right-side drawer for radio/passes settings, same visual pattern as
 * Memory Edit (drawer.js): overlay backdrop + notched panel. Loads from
 * GET /api/settings when opened and PATCHes only the fields the operator
 * actually changed. The passes_* / location fields may not exist on the
 * backend yet -- a 422 (or any other PATCH failure) surfaces in the status
 * line and leaves the drawer open rather than losing the draft. */
export class SetupDrawer extends LitElement {
  static properties = {
    open: { type: Boolean },
    state: { type: Object },
    failure: { type: Object },
    decoders: { type: Array },
    // EDIT LAYOUT checkbox: app.js owns the localStorage persistence
    // (msdr.layoutEdit) and the drag-grip/WIDE-toggle gating -- this is just
    // the current value, echoed back via the layout-edit event on toggle.
    layoutEdit: { type: Boolean },
    // S5: GET /api/validation, fetched by app.js when SETUP opens -- see the
    // VALIDATION group below. null while unfetched/loading.
    validation: { type: Object },
    _draft: { state: true },
    _orig: { state: true },
    _loading: { state: true },
    _sources: { state: true },
    _available: { state: true },
    _allocationSources: { state: true },
    _timezones: { state: true },
    _timezoneError: { state: true },
    _sourceErrors: { state: true },
    _ndbRows: { state: true },
    _ndbError: { state: true },
    _ndbRadiusKm: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.state = null;
    this.failure = null;
    this.decoders = [];
    this.layoutEdit = false;
    this.validation = null;
    this._draft = null;
    this._ndbRows = null;
    this._ndbError = null;
    this._ndbRadiusKm = 300;
    this._orig = null;
    this._loading = false;
    this._sources = [];
    this._available = [];
    this._allocationSources = [];
    this._timezones = [];
    this._timezoneError = null;
    this._sourceErrors = {};
    this._layoutTimer = null;
    this._focusCatalogOnLoad = false;
    this._focusWifiOnLoad = false;
    this._focusBleOnLoad = false;
    this._returnFocus = null;
    this._focusInHandler = (ev) => this._containFocus(ev);
  }

  _toggleEditLayout(on) {
    this.dispatchEvent(new CustomEvent("layout-edit", { detail: { on } }));
  }

  updated(changed) {
    if (changed.has("open") && this.open) {
      this._returnFocus = this.ownerDocument.activeElement;
      this.ownerDocument.addEventListener("focusin", this._focusInHandler, true);
      this._load();
      queueMicrotask(() => this.querySelector(".setup-close")?.focus());
    } else if (changed.has("open") && !this.open) {
      this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
      restoreModalFocus(this._returnFocus);
      this._returnFocus = null;
    }
  }

  disconnectedCallback() {
    this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
    super.disconnectedCallback();
  }

  _onModalKeydown(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      this._close();
      return;
    }
    trapModalTab(ev, this.querySelector('[role="dialog"]'));
  }

  _containFocus(ev) {
    containModalFocus(ev, this.querySelector('[role="dialog"]'), this.querySelector(".setup-close"));
  }

  focusCatalog() {
    this._focusCatalogOnLoad = true;
    if (this._draft && !this._loading) this._scrollToCatalog();
  }

  async _scrollToCatalog() {
    if (!this._focusCatalogOnLoad) return;
    this._focusCatalogOnLoad = false;
    await this.updateComplete;
    this._jumpToSection("setup-catalog");
  }

  /** Opened from the Wi-Fi Discover tab's "helper not installed"/"stopped"
   * empty states (wifi-view.js's `open-wifi-setup`), same pattern as
   * focusCatalog() above. */
  focusWifi() {
    this._focusWifiOnLoad = true;
    if (this._draft && !this._loading) this._scrollToWifi();
  }

  async _scrollToWifi() {
    if (!this._focusWifiOnLoad) return;
    this._focusWifiOnLoad = false;
    await this.updateComplete;
    this._jumpToSection("setup-wifi");
  }

  /** Opened from the BLE Discover tab's empty states (ble-view.js's
   * `open-ble-setup`), same pattern as focusWifi() above. */
  focusBle() {
    this._focusBleOnLoad = true;
    if (this._draft && !this._loading) this._scrollToBle();
  }

  async _scrollToBle() {
    if (!this._focusBleOnLoad) return;
    this._focusBleOnLoad = false;
    await this.updateComplete;
    this._jumpToSection("setup-ble");
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  _set(key, value) {
    this._draft = { ...this._draft, [key]: value };
  }

  _toggleSat(name) {
    const next = new Set(this._draft.satellites);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    this._set("satellites", next);
  }

  _toggleFeature(key) {
    this._set("features", { ...this._draft.features, [key]: !this._draft.features[key] });
  }

  _addExtraSatellite() {
    const label = this._draft.newExtraLabel.trim();
    const norad = Number(this._draft.newExtraNorad);
    if (!label || !Number.isInteger(norad) || norad <= 0) return;
    if (this._draft.extraSatellites.some((e) => e.label === label)) return;
    this._set("extraSatellites", [...this._draft.extraSatellites, { label, norad_id: norad }]);
    this._set("newExtraLabel", "");
    this._set("newExtraNorad", "");
  }

  _removeExtraSatellite(label) {
    this._set(
      "extraSatellites",
      this._draft.extraSatellites.filter((e) => e.label !== label)
    );
  }

  _toggleVisibleOnly() {
    this._set("visibleOnly", !this._draft.visibleOnly);
  }

  _setTimezoneName(value) {
    this._timezoneError = null;
    this._set("timezoneName", value);
  }

  _setTimezoneAuto(auto) {
    this._timezoneError = null;
    this._set("timezoneAuto", auto);
  }

  /** "Europe/Rome · UTC+02:00" -- the effective zone either way (auto-detect
   * or an explicit name), reported live via state.timezone. */
  _timezoneEffectiveLabel() {
    const tz = this.state?.timezone;
    if (!tz?.name) return "";
    return `${tz.name} · ${utcOffsetLabel(tz.utc_offset_minutes)}`;
  }

  _setCatalogSource(key, value) {
    this._set("catalogSources", { ...this._draft.catalogSources, [key]: value });
  }

  _jumpToSection(id) {
    const target = this.querySelector(`#${id}`);
    if (!target) return;
    this.querySelectorAll(".setup-section").forEach((section) => {
      section.open = section === target;
    });
    target.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  _sectionSummary(id, label, isCatalog = false) {
    return html`<summary
      ?data-catalog-settings=${isCatalog}
      @click=${(e) => {
        e.preventDefault();
        this._jumpToSection(id);
      }}
    ><span class="setup-section-title" role="heading" aria-level="2">${label}</span></summary>`;
  }

  // --- layout (LAYOUT group -- keyboard/a11y alternative to app.js's drag
  // & drop) -- each button PATCHes right away (debounced 300ms so mashing
  // ▲/▼ doesn't spam the API) rather than waiting for the drawer's own Save,
  // matching the immediate-apply behaviour of a drag in the main grid. ---

  /** Reordering only ever moves `id` relative to the *visible* (this
   * workspace's own, see _visibleLayoutColumns) neighbors in the raw
   * columns array -- so a move never targets another workspace's ids
   * riding along, invisible, in the same raw column (see WORKSPACE_SECTIONS). */
  _moveWithinColumn(colIndex, id, delta) {
    const visible = this._visibleLayoutColumns(this._draft)[colIndex];
    const idx = visible.indexOf(id);
    const next = idx + delta;
    if (idx === -1 || next < 0 || next >= visible.length) return;
    const beforeId = delta < 0 ? visible[next] : (visible[next + 1] ?? null);
    this._moveTo(colIndex, id, colIndex, beforeId);
  }

  _moveToColumn(colIndex, id, delta) {
    const toCol = colIndex + delta;
    if (toCol < 0 || toCol > 2) return;
    this._moveTo(colIndex, id, toCol, null);
  }

  /** Removes `id` from `fromCol` and re-inserts it in `toCol`, right before
   * `beforeId` (or at the end when null) -- operates on the raw columns
   * (this workspace's own ids plus any backfilled foreign tail), so a
   * foreign id's position is left untouched either way. */
  _moveTo(fromCol, id, toCol, beforeId) {
    const columns = this._draft.layout.columns.map((c) => [...c]);
    columns[fromCol] = columns[fromCol].filter((x) => x !== id);
    const target = columns[toCol];
    const insertAt = beforeId != null ? target.indexOf(beforeId) : -1;
    target.splice(insertAt === -1 ? target.length : insertAt, 0, id);
    this._setLayout(columns);
  }

  _resetLayout() {
    const ws = this._draft.workspace;
    const columns = DEFAULT_WORKSPACE_COLUMNS[ws] ?? DEFAULT_WORKSPACE_COLUMNS.listen;
    this._setLayout(columns.map((c) => [...c]));
  }

  _applyLayoutPreset(key) {
    const preset = LAYOUT_PRESETS[key];
    if (!preset) return;
    this._setLayout(preset.columns.map((c) => [...c]));
  }

  _setLayout(columns) {
    this._set("layout", { ...this._draft.layout, columns });
    clearTimeout(this._layoutTimer);
    this._layoutTimer = setTimeout(() => this._patchLayout(columns), 300);
  }

  /** U2: PATCHes into the active workspace's own entry (see draftFromSettings
   * above and Settings.workspaces, msdr/settings.py) -- same partial-merge
   * shape as app.js's _saveLayout. */
  async _patchLayout(columns) {
    const ws = this._draft.workspace;
    try {
      const updated = await api.patchSettings({ workspaces: { [ws]: { columns } } });
      this._orig = updated;
      this._set("layout", resolveWorkspaceLayout(updated.workspaces, ws));
      this.dispatchEvent(new CustomEvent("settings-saved"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "layout saved" } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _load() {
    this._loading = true;
    this._timezoneError = null;
    try {
      const s = await api.getSettings();
      this._orig = s;
      this._draft = draftFromSettings(s);
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._loading = false;
    }
    if (this._draft) this._scrollToCatalog();
    if (this._draft) this._scrollToWifi();
    if (this._draft) this._scrollToBle();
    this._loadSources();
    this._loadAllocationSources();
    this._loadTimezones();
  }

  /** GET /api/allocations/sources (via api.allocationSources()): one row per
   * loaded allocation dataset (docs/allocations.md "Source status"). Degrades
   * to an empty list rather than breaking the drawer -- the route may not
   * exist yet. */
  async _loadAllocationSources() {
    try {
      this._allocationSources = await api.allocationSources();
    } catch {
      this._allocationSources = [];
    }
  }

  async _refreshAllocationSource(id) {
    this.dispatchEvent(new CustomEvent("action", { detail: { message: `${id}: refreshing…` } }));
    try {
      await api.refreshAllocationSource(id);
      await this._loadAllocationSources();
      this.dispatchEvent(new CustomEvent("action", { detail: { message: `${id}: refreshed` } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _saveIntegration(ev) {
    const { source, config } = ev.detail;
    const patch = { integrations: { [source]: config } };
    const previous = JSON.parse(JSON.stringify(this._orig ?? {}));
    try {
      const at = Date.now();
      this._orig = await api.patchSettings(patch);
      this.querySelector("integration-panel")?.saveComplete();
      this.dispatchEvent(new CustomEvent("settings-undoable", {
        detail: { patch, previous, label: `${source} integration`, at },
      }));
      this.dispatchEvent(new CustomEvent("settings-saved"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: `${source} integration saved` } }));
    } catch (err) {
      this.querySelector("integration-panel")?.saveFailed(err.message);
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _saveWifi(ev) {
    const { config } = ev.detail;
    const patch = { wifi: config };
    const previous = JSON.parse(JSON.stringify(this._orig ?? {}));
    try {
      const at = Date.now();
      this._orig = await api.patchSettings(patch);
      this.querySelector("wifi-setup-section")?.saveComplete();
      this.dispatchEvent(new CustomEvent("settings-undoable", {
        detail: { patch, previous, label: "Wi-Fi receiver", at },
      }));
      this.dispatchEvent(new CustomEvent("settings-saved"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "Wi-Fi settings saved" } }));
    } catch (err) {
      this.querySelector("wifi-setup-section")?.saveFailed(err.message);
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  async _saveBle(ev) {
    const { config } = ev.detail;
    const patch = { ble: config };
    const previous = JSON.parse(JSON.stringify(this._orig ?? {}));
    try {
      const at = Date.now();
      this._orig = await api.patchSettings(patch);
      this.querySelector("ble-setup-section")?.saveComplete();
      this.dispatchEvent(new CustomEvent("settings-undoable", {
        detail: { patch, previous, label: "BLE receiver", at },
      }));
      this.dispatchEvent(new CustomEvent("settings-saved"));
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "BLE settings saved" } }));
    } catch (err) {
      this.querySelector("ble-setup-section")?.saveFailed(err.message);
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  /** GET /api/timezones: sorted IANA names for the Timezone field's datalist
   * -- fetched once per drawer open (a static list, no need to refetch on
   * every reopen within the same session). */
  async _loadTimezones() {
    if (this._timezones.length) return;
    try {
      this._timezones = await api.getTimezones();
    } catch {
      this._timezones = [];
    }
  }

  /** GET /api/catalog/sources: `sources` is the fetched state (entry-count/
   * fetched-date per source that's ever been fetched), `available` is every
   * known source incl. never-fetched ones -- the CATALOG group renders one
   * row per `available` entry. Degrades to empty lists (no rows) rather than
   * breaking the drawer -- the route may not exist yet. */
  async _loadSources() {
    try {
      const res = await api.catalogSources();
      this._sources = res.sources ?? [];
      this._available = res.available ?? [];
    } catch {
      this._sources = [];
      this._available = [];
    }
  }

  async _refreshSource(key) {
    const current = this._draft?.catalogSources?.[key] ?? "";
    const saved = this._orig?.catalog_sources?.[key] ?? "";
    if (current !== saved) {
      const message = `${key}: save the URL before refreshing`;
      this._sourceErrors = { ...this._sourceErrors, [key]: message };
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message } }));
      return;
    }
    this._sourceErrors = { ...this._sourceErrors, [key]: "" };
    this.dispatchEvent(new CustomEvent("action", { detail: { message: `${key}: refreshing…` } }));
    try {
      const result = await api.refreshCatalog(key);
      if (result?.error) {
        this._sourceErrors = { ...this._sourceErrors, [key]: result.error };
        this.dispatchEvent(new CustomEvent("api-error", { detail: { message: `${key}: ${result.error}` } }));
        return;
      }
      await this._loadSources();
      this._sourceErrors = { ...this._sourceErrors, [key]: "" };
      const info = this._sources.find((s) => s.source === key);
      const count = result?.entries ?? info?.entry_count;
      const message = count != null ? `${key}: ${count} entries refreshed` : `${key}: refreshed`;
      this.dispatchEvent(new CustomEvent("action", { detail: { message } }));
    } catch (err) {
      this._sourceErrors = { ...this._sourceErrors, [key]: err.message };
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  // Server-side band filter (190-535 kHz) keeps the 60 nearest to NDBs only;
  // a plain navaid search would spend the limit on nearby VORs/DMEs. The
  // server sorts by the saved receiver location, so no `near` param is sent.
  async _loadNearbyNdbs() {
    this._ndbError = null;
    try {
      const res = await api.searchCatalog({
        ref_type: "navaid", lo_hz: 190000, hi_hz: 535000, sort: "distance", limit: 60,
      });
      this._ndbRows = (res.entries ?? []).filter(isNdbRow);
    } catch (err) {
      this._ndbError = err.message;
    }
  }

  _ndbPicker(d) {
    const loc = this._orig?.location;
    let body;
    if (!loc) {
      body = html`<p class="setup-help">Set your location in Setup → Passes to list nearby NDBs.</p>`;
    } else {
      const radius = Number(this._ndbRadiusKm);
      const rows = (this._ndbRows ?? []).filter(
        (r) => !Number.isFinite(radius) || r.distance_km == null || r.distance_km <= radius
      );
      const have = new Set(
        d.beaconsNdbFrequenciesText.split("\n").map((l) => Math.round(Number(l.trim()))).filter(Number.isFinite)
      );
      body = html`
        <div class="setup-ndb-toolbar">
          <label>within
            <input name="ndb-radius-km" type="number" min="0" .value=${String(this._ndbRadiusKm)}
              @input=${(e) => { this._ndbRadiusKm = e.target.value; }} /> km</label>
          <button @click=${() => this._loadNearbyNdbs()}>Refresh</button>
        </div>
        ${this._ndbError ? html`<p class="setup-catalog-error">${this._ndbError}</p>` : ""}
        ${this._ndbRows
          ? html`<table class="setup-ndb-table mono">
              ${rows.map((r) => html`<tr>
                <td><input type="checkbox" .checked=${have.has(Math.round(r.frequency_hz))}
                  @change=${(e) => this._set("beaconsNdbFrequenciesText",
                    mergeNdbFrequencies(d.beaconsNdbFrequenciesText, r.frequency_hz, e.target.checked))} /></td>
                <td>${r.station_id}</td>
                <td>${r.location ?? ""}</td>
                <td>${(r.frequency_hz / 1000).toFixed(1)} kHz</td>
                <td>${r.distance_km == null ? "--" : `${Math.round(r.distance_km)} km`}</td>
              </tr>`)}
            </table>`
          : ""}`;
    }
    return html`<details class="setup-ndb-picker"
      @toggle=${(e) => { if (e.target.open && loc && !this._ndbRows) this._loadNearbyNdbs(); }}>
      <summary>Pick nearby NDBs</summary>
      ${body}
    </details>`;
  }

  async _doImport() {
    const path = this._draft.importPath.trim();
    if (!path) return;
    this.dispatchEvent(new CustomEvent("action", { detail: { message: "importing…" } }));
    try {
      const res = await api.importCatalog(path, this._draft.importFormat);
      await this._loadSources();
      const count = res.count ?? res.imported ?? "?";
      this.dispatchEvent(new CustomEvent("action", { detail: { message: `imported ${count} entries` } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  /** Only the fields that differ from what GET /api/settings returned --
   * PATCH is meant to be a partial update, so untouched fields are never
   * sent even though the draft holds a value for every one of them. */
  _buildPatch() {
    const d = this._draft;
    const o = this._orig ?? {};
    const patch = {};

    if (d.evidencePanelStyle !== (o.evidence_panel_style ?? "tinted")) {
      patch.evidence_panel_style = d.evidencePanelStyle;
    }

    if (d.host !== (o.host ?? "")) patch.host = d.host;
    if (d.recorderRoot.trim() !== (o.recorder_root ?? "")) patch.recorder_root = d.recorderRoot.trim();
    if (d.validationSamplesDir.trim() !== (o.validation_samples_dir ?? "")) patch.validation_samples_dir = d.validationSamplesDir.trim();

    const port = d.port === "" ? null : Number(d.port);
    if (port !== null && port !== (o.port ?? null)) patch.port = port;

    const dwell = d.dwellSeconds === "" ? null : Number(d.dwellSeconds);
    if (dwell !== null && dwell !== (o.dwell_seconds ?? null)) patch.dwell_seconds = dwell;

    const nearbyHz = d.nearbyKhz === "" ? null : Math.round(Number(d.nearbyKhz) * 1000);
    if (nearbyHz !== null && nearbyHz !== (o.nearby_hz ?? null)) patch.nearby_hz = nearbyHz;

    if (d.region !== (o.region ?? "US")) patch.region = d.region;
    if (d.allocationJurisdiction !== (o.allocation_jurisdiction ?? "US")) {
      patch.allocation_jurisdiction = d.allocationJurisdiction;
    }

    // timezone: null = auto-detect. Only sent when it actually changed, same
    // partial-patch rule as every other top-level field here.
    const origTimezone = o.timezone ?? null;
    if (d.timezoneAuto) {
      if (origTimezone !== null) patch.timezone = null;
    } else {
      const tzName = d.timezoneName.trim();
      if (tzName && tzName !== origTimezone) patch.timezone = tzName;
    }

    const passesDays = d.passesDays === "" ? null : Number(d.passesDays);
    if (passesDays !== null && passesDays !== (o.passes_days ?? null)) patch.passes_days = passesDays;

    const minElev = d.passesMinElevation === "" ? null : Number(d.passesMinElevation);
    if (minElev !== null && minElev !== (o.passes_min_elevation ?? null)) patch.passes_min_elevation = minElev;

    const horizon = d.passesHorizon === "" ? null : Number(d.passesHorizon);
    if (horizon !== null && horizon !== (o.passes_horizon ?? null)) patch.passes_horizon = horizon;

    const top = d.passesTop === "" ? null : Number(d.passesTop);
    if (top !== null && top !== (o.passes_top ?? null)) patch.passes_top = top;

    const origSats = new Set(o.passes_satellites ?? []);
    const sameSats =
      origSats.size === d.satellites.size && [...origSats].every((sat) => d.satellites.has(sat));
    if (!sameSats) patch.passes_satellites = Array.from(d.satellites);

    const origExtra = o.passes_extra_satellites ?? [];
    const sameExtra =
      origExtra.length === d.extraSatellites.length &&
      origExtra.every(
        (e, i) => e.label === d.extraSatellites[i]?.label && e.norad_id === d.extraSatellites[i]?.norad_id
      );
    if (!sameExtra) patch.passes_extra_satellites = d.extraSatellites;

    if (d.visibleOnly !== !!(o.passes_visible_only ?? false)) patch.passes_visible_only = d.visibleOnly;

    const origLoc = o.location ?? null;
    if (!d.overrideLocation) {
      if (origLoc !== null) patch.location = null;
    } else {
      const loc = {
        lat: d.lat === "" ? 0 : Number(d.lat),
        lon: d.lon === "" ? 0 : Number(d.lon),
        altitude_m: d.altitudeM === "" ? 0 : Number(d.altitudeM),
        timezone: d.timezone || "UTC",
      };
      const same =
        origLoc &&
        origLoc.lat === loc.lat &&
        origLoc.lon === loc.lon &&
        origLoc.altitude_m === loc.altitude_m &&
        origLoc.timezone === loc.timezone;
      if (!same) patch.location = loc;
    }

    // features: a partial dict of only the flags the operator flipped, not
    // the whole object -- PATCH /api/settings merges it in (see contract).
    const origFeatures = resolveFeatures(o.features);
    const changedFeatures = {};
    for (const [key] of FEATURE_FLAGS) {
      if (d.features[key] !== origFeatures[key]) changedFeatures[key] = d.features[key];
    }
    if (Object.keys(changedFeatures).length) patch.features = changedFeatures;

    const origSources = o.catalog_sources ?? {};
    if (!sameStringDict(d.catalogSources, origSources)) patch.catalog_sources = { ...d.catalogSources };

    const origLangs = (o.catalog_languages ?? []).join(", ");
    if (d.catalogLanguages !== origLangs) {
      patch.catalog_languages = d.catalogLanguages
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }

    // decoder: Settings.decoder is a whole DecoderSettings object (not a
    // partial-patch class like features), so an edit here must send the full
    // object -- spread the original over unedited fields (audio_channels,
    // history_limit) rather than letting them silently reset to defaults.
    const origDecoder = o.decoder ?? {};
    const binDirs = d.decoderBinDirsText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const origBinDirsText = (origDecoder.bin_dirs ?? []).join("\n");
    const binDirsChanged = d.decoderBinDirsText !== origBinDirsText;
    const rate = d.decoderAudioSampleRate === "" ? null : Number(d.decoderAudioSampleRate);
    const rateChanged = rate !== null && rate !== (origDecoder.audio_sample_rate ?? null);
    const loraSettings = { lora_spreading_factor: Number(d.loraSpreadingFactor),
      lora_bandwidth_hz: Number(d.loraBandwidth), lora_coding_rate: Number(d.loraCodingRate),
      lora_protocol: d.loraProtocol };
    const loraDefaults = {lora_spreading_factor: 11, lora_bandwidth_hz: 250000, lora_coding_rate: 1, lora_protocol: "lora"};
    const loraChanged = Object.entries(loraSettings).some(([key, value]) => value !== (origDecoder[key] ?? loraDefaults[key]));
    if (binDirsChanged || rateChanged || loraChanged) {
      patch.decoder = {
        ...origDecoder,
        ...loraSettings,
        bin_dirs: binDirsChanged ? binDirs : (origDecoder.bin_dirs ?? []),
        audio_sample_rate: rateChanged ? rate : (origDecoder.audio_sample_rate ?? 48000),
      };
    }

    // survey: same whole-object rule as decoder above -- Settings.survey also
    // carries presets/occupancy_windows_s untouched by this drawer, so an
    // edit here must spread the original over them.
    const origSurvey = o.survey ?? {};
    const centerExclusionChanged = d.surveyCenterExclusion !== (origSurvey.center_exclusion_enabled ?? false);
    const thresholdDb = Number(d.surveyThresholdDb);
    const thresholdChanged = Number.isFinite(thresholdDb) && thresholdDb !== (origSurvey.threshold_db ?? 8);
    const spectrumFps = Number(d.surveySpectrumFps);
    const fpsChanged = Number.isFinite(spectrumFps) && spectrumFps !== (origSurvey.spectrum_fps ?? 10);
    const peakThresholdDb = Number(d.surveyPeakThresholdDb);
    const peakThresholdChanged =
      Number.isFinite(peakThresholdDb) && peakThresholdDb !== (origSurvey.peak_threshold_db ?? 10);
    const labelTtlS = Number(d.surveyLabelTtlS);
    const labelTtlChanged = Number.isFinite(labelTtlS) && labelTtlS !== (origSurvey.label_ttl_s ?? 120);
    const recognizedTtlS = d.surveyRecognizedTtlS === "" ? null : Number(d.surveyRecognizedTtlS);
    const origRecognizedTtlS = origSurvey.recognized_ttl_s ?? null;
    const recognizedTtlChanged =
      d.surveyRecognizedTtlS === ""
        ? origRecognizedTtlS !== null
        : Number.isFinite(recognizedTtlS) && recognizedTtlS !== origRecognizedTtlS;
    const maxAnalyzed = Number(d.surveyMaxAnalyzed);
    const maxAnalyzedChanged = Number.isFinite(maxAnalyzed) && maxAnalyzed !== (origSurvey.max_analyzed ?? 6);
    const analyzeDwellS = Number(d.surveyAnalyzeDwellS);
    const analyzeDwellChanged =
      Number.isFinite(analyzeDwellS) && analyzeDwellS !== (origSurvey.analyze_dwell_s ?? 2.0);

    // Waterfall group -- blank MIN/MAX text means "auto" (null); wf_avg_factor/
    // wf_history_rows treat "" the same way the analyzer fields above do (no
    // edit rather than coercing to 0).
    const wfMinDb = d.wfMinDb === "" ? null : Number(d.wfMinDb);
    const origWfMinDb = origSurvey.wf_min_db ?? null;
    const wfMinChanged = d.wfMinDb === "" ? origWfMinDb !== null : Number.isFinite(wfMinDb) && wfMinDb !== origWfMinDb;
    const wfMaxDb = d.wfMaxDb === "" ? null : Number(d.wfMaxDb);
    const origWfMaxDb = origSurvey.wf_max_db ?? null;
    const wfMaxChanged = d.wfMaxDb === "" ? origWfMaxDb !== null : Number.isFinite(wfMaxDb) && wfMaxDb !== origWfMaxDb;
    const wfAvgFactor = Number(d.wfAvgFactor);
    const wfAvgFactorChanged = Number.isFinite(wfAvgFactor) && wfAvgFactor !== (origSurvey.wf_avg_factor ?? 0.5);
    const wfPeakHoldChanged = d.wfPeakHold !== !!(origSurvey.wf_peak_hold ?? false);
    const wfPaletteChanged = d.wfPalette !== (origSurvey.wf_palette ?? "sdrsharp");
    const wfHistoryRows = Number(d.wfHistoryRows);
    const wfHistoryRowsChanged =
      Number.isFinite(wfHistoryRows) && wfHistoryRows !== (origSurvey.wf_history_rows ?? 300);
    const wfShowGridChanged = d.wfShowGrid !== (origSurvey.wf_show_grid ?? true);
    const rdsOddChannelsChanged = d.surveyRdsOddChannels !== !!(origSurvey.rds_odd_channels ?? false);

    if (
      thresholdChanged ||
      fpsChanged ||
      peakThresholdChanged ||
      centerExclusionChanged ||
      labelTtlChanged ||
      recognizedTtlChanged ||
      maxAnalyzedChanged ||
      analyzeDwellChanged ||
      wfMinChanged ||
      wfMaxChanged ||
      wfAvgFactorChanged ||
      wfPeakHoldChanged ||
      wfPaletteChanged ||
      wfHistoryRowsChanged ||
      wfShowGridChanged ||
      rdsOddChannelsChanged
    ) {
      patch.survey = {
        ...origSurvey,
        center_exclusion_enabled: d.surveyCenterExclusion,
        threshold_db: thresholdChanged ? thresholdDb : (origSurvey.threshold_db ?? 8),
        spectrum_fps: fpsChanged ? spectrumFps : (origSurvey.spectrum_fps ?? 10),
        peak_threshold_db: peakThresholdChanged ? peakThresholdDb : (origSurvey.peak_threshold_db ?? 10),
        label_ttl_s: labelTtlChanged ? labelTtlS : (origSurvey.label_ttl_s ?? 120),
        recognized_ttl_s: recognizedTtlChanged ? recognizedTtlS : origRecognizedTtlS,
        max_analyzed: maxAnalyzedChanged ? maxAnalyzed : (origSurvey.max_analyzed ?? 6),
        analyze_dwell_s: analyzeDwellChanged ? analyzeDwellS : (origSurvey.analyze_dwell_s ?? 2.0),
        wf_min_db: wfMinChanged ? wfMinDb : origWfMinDb,
        wf_max_db: wfMaxChanged ? wfMaxDb : origWfMaxDb,
        wf_avg_factor: wfAvgFactorChanged ? wfAvgFactor : (origSurvey.wf_avg_factor ?? 0.5),
        wf_peak_hold: wfPeakHoldChanged ? d.wfPeakHold : !!(origSurvey.wf_peak_hold ?? false),
        wf_palette: wfPaletteChanged ? d.wfPalette : (origSurvey.wf_palette ?? "sdrsharp"),
        wf_history_rows: wfHistoryRowsChanged ? wfHistoryRows : (origSurvey.wf_history_rows ?? 300),
        wf_show_grid: wfShowGridChanged ? d.wfShowGrid : (origSurvey.wf_show_grid ?? true),
        rds_odd_channels: rdsOddChannelsChanged
          ? d.surveyRdsOddChannels
          : !!(origSurvey.rds_odd_channels ?? false),
      };
    }

    // beacons: same whole-object rule as decoder/survey above -- Settings.beacons
    // also carries ndb_dwell_s/band_order_hz untouched by this drawer.
    const origBeacons = o.beacons ?? {};
    const beaconsSnrThresholdDb = Number(d.beaconsSnrThresholdDb);
    const beaconsSnrChanged =
      Number.isFinite(beaconsSnrThresholdDb) && beaconsSnrThresholdDb !== (origBeacons.snr_threshold_db ?? 6.0);
    const ndbFrequenciesHz = d.beaconsNdbFrequenciesText
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean)
      .map(Number)
      .filter(Number.isFinite)
      .map(Math.round);
    const origNdbFrequenciesText = (origBeacons.ndb_frequencies_hz ?? []).map((hz) => Math.round(hz)).join("\n");
    const ndbFrequenciesChanged = d.beaconsNdbFrequenciesText !== origNdbFrequenciesText;
    if (beaconsSnrChanged || ndbFrequenciesChanged) {
      patch.beacons = {
        ...origBeacons,
        snr_threshold_db: beaconsSnrChanged ? beaconsSnrThresholdDb : (origBeacons.snr_threshold_db ?? 6.0),
        ndb_frequencies_hz: ndbFrequenciesChanged ? ndbFrequenciesHz : (origBeacons.ndb_frequencies_hz ?? []),
      };
    }

    return patch;
  }

  async _save() {
    const patch = this._buildPatch();
    if (Object.keys(patch).length === 0) {
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "setup saved" } }));
      this._close();
      return;
    }
    try {
      const previous = JSON.parse(JSON.stringify(this._orig ?? {}));
      const at = Date.now();
      const updated = await api.patchSettings(patch);
      this.dispatchEvent(new CustomEvent("settings-undoable", {
        detail: { patch, previous, label: "setup changes", at },
      }));
      this._orig = updated;
      this._timezoneError = null;
      const hostPortChanged = "host" in patch || "port" in patch;
      const message = hostPortChanged
        ? "setup saved — restart msdr to apply host/port"
        : "setup saved";
      this.dispatchEvent(new CustomEvent("action", { detail: { message } }));
      this.dispatchEvent(new CustomEvent("settings-saved"));
      this._close();
    } catch (err) {
      // 422 (or the routes not existing yet) lands here -- report it and
      // keep the drawer open with the draft intact so nothing is lost. An
      // unknown timezone name also gets its own inline note next to the field.
      if ("timezone" in patch) this._timezoneError = err.message;
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _field(label, node) {
    return html`<label style="display:flex;flex-direction:column;gap:4px;font-size:0.85em"
      ><span class="dim">${label}</span>${node}</label
    >`;
  }

  _group(label, node) {
    return html`<div style="display:flex;flex-direction:column;gap:4px;font-size:0.85em">
      <span class="dim">${label}</span>${node}
    </div>`;
  }

  /** LED-style switch row: an led-dot lit amber when the flag is on, the
   * engraved label, and an ON/OFF readout -- no rounded pill, matches the
   * satellite/tag chip toggles elsewhere in the drawer. */
  _featureRow(key, label, d) {
    const on = !!d.features[key];
    return html`
      <div class="row" style="cursor:pointer;padding:4px 2px" @click=${() => this._toggleFeature(key)}>
        <led-dot color="var(--accent)" .lit=${on}></led-dot>
        <span class="label">${label}</span>
        <span class="spacer"></span>
        <span class="dim mono" style="font-size:0.8em">${on ? "ON" : "OFF"}</span>
      </div>
    `;
  }

  _featuresGroup(d) {
    // The ML controls toggle is meaningless while RF ML is dormant
    // (survey status ml_config.available === false, see msdr/rfml/__init__.py).
    const mlDormant = this.state?.survey?.ml_config?.available === false;
    const rows = FEATURE_FLAGS.filter(([key]) => !(key === "ml_controls" && mlDormant));
    return html`<div>${rows.map(([key, label]) => this._featureRow(key, label, d))}</div>`;
  }

  _layoutRow(id, colIndex, idx, colLen, d) {
    const hidden = !sectionVisible(id, d.features);
    return html`
      <div class="row" style="cursor:default;padding:2px 4px">
        <span class="label" style="flex:1">${SECTION_LABELS[id] ?? id}${hidden ? " (off)" : ""}</span>
        <button
          title="move up"
          ?disabled=${idx === 0}
          @click=${() => this._moveWithinColumn(colIndex, id, -1)}
        >
          ▲
        </button>
        <button
          title="move down"
          ?disabled=${idx === colLen - 1}
          @click=${() => this._moveWithinColumn(colIndex, id, 1)}
        >
          ▼
        </button>
        <button
          title="move to previous column"
          ?disabled=${colIndex === 0}
          @click=${() => this._moveToColumn(colIndex, id, -1)}
        >
          ◀
        </button>
        <button
          title="move to next column"
          ?disabled=${colIndex === 2}
          @click=${() => this._moveToColumn(colIndex, id, 1)}
        >
          ▶
        </button>
      </div>
    `;
  }

  _layoutColumn(col, colIndex, d) {
    return html`
      <div style="display:flex;flex-direction:column;gap:2px">
        <span class="dim mono" style="font-size:0.75em">COLUMN ${colIndex + 1}</span>
        ${col.length
          ? col.map((id, idx) => this._layoutRow(id, colIndex, idx, col.length, d))
          : html`<span class="dim" style="font-size:0.8em">empty</span>`}
      </div>
    `;
  }

  /** This workspace's own raw columns (see draftFromSettings), filtered to
   * WORKSPACE_SECTIONS[d.workspace] -- drops the other workspaces' ids
   * backfilled into the last column by the backend's Layout validator, which
   * this group neither shows nor lets the operator touch (switch workspaces
   * to edit those instead). */
  _visibleLayoutColumns(d) {
    const members = new Set(WORKSPACE_SECTIONS[d.workspace] ?? []);
    return d.layout.columns.map((col) => col.filter((id) => members.has(id)));
  }

  _layoutGroup(d) {
    const columns = this._visibleLayoutColumns(d);
    return html`
      <div style="display:flex;flex-direction:column;gap:10px">
        <span class="dim mono" style="font-size:0.75em"
          >EDITING ${WORKSPACE_LABELS[d.workspace] ?? d.workspace}</span
        >
        <label class="row mono" style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:0.85em">
          <input
            type="checkbox"
            name="edit-layout"
            .checked=${this.layoutEdit}
            @change=${(e) => this._toggleEditLayout(e.target.checked)}
          />
          EDIT LAYOUT
        </label>
        ${columns.map((col, colIndex) => this._layoutColumn(col, colIndex, d))}
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          ${Object.entries(LAYOUT_PRESETS).map(
            ([key, preset]) =>
              html`<button @click=${() => this._applyLayoutPreset(key)}>${preset.label.toUpperCase()}</button>`
          )}
          <button @click=${() => this._resetLayout()}>RESET LAYOUT</button>
        </div>
      </div>
    `;
  }

  _extraSatellitesGroup(d) {
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        ${d.extraSatellites.map(
          (e) => html`
            <div class="row" style="cursor:default;padding:2px 4px">
              <span class="mono">${e.label}</span>
              <span class="dim mono" style="font-size:0.8em">NORAD ${e.norad_id}</span>
              <span class="spacer"></span>
              <button title="Remove ${e.label}" @click=${() => this._removeExtraSatellite(e.label)}>×</button>
            </div>
          `
        )}
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <input
            style="flex:1;min-width:80px"
            placeholder="label"
            .value=${d.newExtraLabel}
            @input=${(e) => this._set("newExtraLabel", e.target.value)}
          />
          <input
            class="mono"
            style="width:90px"
            type="number"
            min="1"
            placeholder="NORAD id"
            .value=${d.newExtraNorad}
            @input=${(e) => this._set("newExtraNorad", e.target.value)}
          />
          <button @click=${() => this._addExtraSatellite()}>ADD</button>
        </div>
      </div>
    `;
  }

  _catalogSourceRow(entry, d) {
    const info = this._sources.find((s) => s.source === entry.source);
    const stale = !!info?.stale;
    const sourceInfo = CATALOG_SOURCE_INFO[entry.source] ?? {};
    const sourceError = this._sourceErrors[entry.source];
    const savedUrl = this._orig?.catalog_sources?.[entry.source] ?? entry.configured_url ?? "";
    const currentUrl = savedUrl || (entry.static ? "Bundled reference data" : sourceInfo.builtin || (entry.source === "eibi" ? "Seasonal URL selected at refresh time" : "No URL configured"));
    const urlPending = (d.catalogSources[entry.source] ?? "") !== (this._orig?.catalog_sources?.[entry.source] ?? "");
    const fetchedLine = info
      ? `${info.entry_count} entries · ${info.fetched_at ?? "fetched, date unknown"}`
      : "Not fetched yet";
    const kindInfo = catalogSourceKindInfo(entry.kind);
    return html`
      <article class="setup-catalog-source catalog-source-${entry.source}" data-source=${entry.source}>
        <div class="setup-catalog-source-head">
          <div class="setup-catalog-source-copy">
            <span class="label">${entry.label}</span>
            <span class="dim">${entry.description}</span>
            ${kindInfo
              ? html`<span class="dim setup-catalog-kind" title=${kindInfo.title}>${kindInfo.text} — ${kindInfo.title}</span>`
              : ""}
            ${entry.coverage ? html`<span class="dim setup-catalog-coverage">${entry.coverage}</span>` : ""}
          </div>
          <span class="setup-catalog-state ${sourceError ? "is-error" : stale ? "is-stale" : ""}">
            ${sourceError ? "ERROR" : stale ? "STALE" : info ? "READY" : "NEW"}
          </span>
        </div>
        <div class="setup-catalog-reference">
          <span class="label">Current source</span>
          <span class="setup-catalog-url-value mono" title=${currentUrl}>${currentUrl}</span>
          ${sourceInfo.url ? html`<a href=${sourceInfo.url} target="_blank" rel="noopener noreferrer">${sourceInfo.label}</a>` : ""}
        </div>
        ${entry.static
          ? html`<div class="setup-catalog-static dim">${sourceInfo.builtinNote ?? "Bundled reference data · updated with msdr releases"}</div>`
          : html`<label class="setup-catalog-url">
              <span class="label">Source URL</span>
              <input
                aria-label="${entry.label} source URL"
                placeholder=${entry.has_default_url ? "Blank uses the built-in URL" : "Paste a source URL"}
                .value=${d.catalogSources[entry.source] ?? ""}
                @input=${(e) => this._setCatalogSource(entry.source, e.target.value)}
              />
            </label>${sourceInfo.builtinNote ? html`<div class="setup-catalog-static dim">${sourceInfo.builtinNote}</div>` : ""}`}
        <div class="setup-catalog-source-foot">
          <span class="dim mono">${sourceError ? html`<span class="setup-catalog-error">${sourceError}</span>` : `${fetchedLine}${stale ? " · stale season" : ""}`}</span>
          <button class="setup-catalog-refresh" ?disabled=${urlPending} title=${urlPending ? "Save the URL before refreshing this source" : `Refresh ${entry.label}`} @click=${() => this._refreshSource(entry.source)}>
            ↻ <span>${urlPending ? "Save URL first" : entry.static ? "Reload" : "Refresh"}</span>
          </button>
        </div>
      </article>
    `;
  }

  /** Allocation sources group (SETUP's Catalog section, docs/allocations.md
   * "Backend integration"/"UI"): one row per GET /api/allocations/sources
   * entry (title/publisher/edition/effective date/last successful update/
   * range/status/stale), reusing _catalogSourceRow's visual pattern. */
  _allocationSourceRow(entry) {
    const errored = entry.status === "error";
    const stale = !!entry.stale;
    const range = `${readout(entry.lo_hz)}–${readout(entry.hi_hz)} Hz`;
    return html`
      <article class="setup-catalog-source allocation-source-${entry.id}" data-source=${entry.id}>
        <div class="setup-catalog-source-head">
          <div class="setup-catalog-source-copy">
            <span class="label">${entry.title}</span>
            <span class="dim">${entry.publisher ?? ""}${entry.edition ? ` · ${entry.edition}` : ""}</span>
          </div>
          <span class="setup-catalog-state ${errored ? "is-error" : stale ? "is-stale" : ""}">
            ${errored ? "ERROR" : stale ? "STALE" : "READY"}
          </span>
        </div>
        <div class="setup-catalog-reference">
          <span class="label">Range</span>
          <span class="setup-catalog-url-value mono" title=${range}>${range}</span>
        </div>
        <div class="setup-catalog-source-foot">
          <span class="dim mono">
            ${errored
              ? html`<span class="setup-catalog-error">${entry.error ?? "unavailable"}</span>`
              : `effective ${entry.effective_date ?? "unknown"} · last updated ${entry.imported_at ?? "never"}`}
          </span>
          <button class="setup-catalog-refresh" title=${`Refresh ${entry.title}`} @click=${() => this._refreshAllocationSource(entry.id)}>
            ↻ <span>Refresh</span>
          </button>
        </div>
      </article>
    `;
  }

  _allocationSourcesGroup() {
    return html`
      <div class="setup-group setup-catalog-group">
        <p class="setup-help">Allocation sources supply the regulatory frequency table (NTIA/eCFR) and its footnote text. Refresh re-runs the importer; a failed refresh leaves the previous dataset in place.</p>
        ${this._allocationSources.length
          ? this._allocationSources.map((entry) => this._allocationSourceRow(entry))
          : html`<p class="dim" style="font-size:0.85em">no allocation sources reported</p>`}
      </div>
    `;
  }

  /** Waterfall group (SETUP's Survey section): seeds survey-view.js's
   * on-canvas MIN/MAX/PEAK controls as defaults (a per-browser localStorage
   * override still wins there until RESET) and fully drives palette/history
   * rows/grid visibility, which have no on-canvas override. Blank MIN/MAX
   * means auto (wf_min_db/wf_max_db: null). */
  _waterfallGroup(d) {
    return html`
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;gap:8px">
          ${this._field(
            "min dB (blank = auto)",
            html`<input
              name="wf-min-db"
              type="number"
              step="1"
              placeholder="auto"
              .value=${d.wfMinDb}
              @input=${(e) => this._set("wfMinDb", e.target.value)}
            />`
          )}
          ${this._field(
            "max dB (blank = auto)",
            html`<input
              name="wf-max-db"
              type="number"
              step="1"
              placeholder="auto"
              .value=${d.wfMaxDb}
              @input=${(e) => this._set("wfMaxDb", e.target.value)}
            />`
          )}
        </div>
        ${this._field(
          "avg factor (0-0.95)",
          html`<input
            name="wf-avg-factor"
            type="number"
            step="0.05"
            min="0"
            max="0.95"
            .value=${d.wfAvgFactor}
            @input=${(e) => this._set("wfAvgFactor", e.target.value)}
          />`
        )}
        <label class="row mono" style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:0.85em">
          <input
            type="checkbox"
            name="wf-peak-hold"
            .checked=${d.wfPeakHold}
            @change=${(e) => this._set("wfPeakHold", e.target.checked)}
          />
          peak hold (default)
        </label>
        ${this._field(
          "palette",
          html`<select name="wf-palette" @change=${(e) => this._set("wfPalette", e.target.value)}>
            <option value="sdrsharp" ?selected=${d.wfPalette === "sdrsharp"}>SDR#</option>
            <option value="satdump" ?selected=${d.wfPalette === "satdump"}>SatDump</option>
            <option value="gray" ?selected=${d.wfPalette === "gray"}>Grayscale</option>
          </select>`
        )}
        ${this._field(
          "history rows (50-2000)",
          html`<input
            name="wf-history-rows"
            type="number"
            min="50"
            max="2000"
            .value=${d.wfHistoryRows}
            @input=${(e) => this._set("wfHistoryRows", e.target.value)}
          />`
        )}
        <label class="row mono" style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:0.85em">
          <input
            type="checkbox"
            name="wf-show-grid"
            .checked=${d.wfShowGrid}
            @change=${(e) => this._set("wfShowGrid", e.target.checked)}
          />
          show grid
        </label>
      </div>
    `;
  }

  /** DEPENDENCIES group (SETUP's Decoder section): one row per GET
   * /api/decoders entry using the same three-fact readinessLabels split as
   * decode-view.js's ⚙ modal -- label · installed · tested (+"(stale)")
   * · install hint -- so setup surfaces the same install/test truth without
   * duplicating its logic. */
  _dependencyRow(dec) {
    const labels = readinessLabels(dec);
    return html`
      <div class="row" style="cursor:default;padding:2px 4px;align-items:flex-start">
        <span class="label" style="flex:1">${dec.label ?? dec.id}</span>
        <span class="dim mono" style="font-size:0.8em" title=${labels.installed.title}
          >${labels.installed.text}</span
        >
        <span class="dim mono" style="font-size:0.8em" title=${labels.tested.title}
          >${labels.tested.text}</span
        >
      </div>
      ${labels.installed.title
        ? html`<p class="dim" style="font-size:0.75em;margin:0 0 4px">${labels.installed.title}</p>`
        : ""}
    `;
  }

  _dependenciesGroup() {
    if (!this.decoders?.length) return html`<p class="dim" style="font-size:0.85em">no decoders registered</p>`;
    return html`<div>${this.decoders.map((dec) => this._dependencyRow(dec))}</div>`;
  }

  /** VALIDATION group (S5 contract, §3): GET /api/validation (msdr/validation/
   * status.py), fetched by app.js each time SETUP opens. Deliberately never
   * renders a green "all validated" summary -- the header names the gap
   * (field gates) up front, and OUTSTANDING GATES always lists HF field
   * validation as unresolved even before the fetch lands or if the server's
   * own list ever omitted it (it hasn't -- msdr/validation/status.py's gate
   * list is fixed -- this is just belt-and-braces so the UI can't accidentally
   * imply otherwise). */
  _validationGroup() {
    const v = this.validation;
    // GET /api/validation (msdr/validation/status.py): corpus_cases,
    // decoders, scorecard, outstanding_gates (plain sentences).
    const corpus = v?.corpus_cases ?? v?.corpus ?? [];
    const decoders = v?.decoders ?? [];
    const scorecard = v?.scorecard ?? null;
    const gates = (v?.outstanding_gates ?? []).map((g) => {
      if (typeof g !== "string") return g;
      // "Title: detail sentence" -> short label row + dim detail line.
      const cut = g.indexOf(":");
      const title = cut > 0 ? g.slice(0, cut) : g;
      const detail = cut > 0 ? g.slice(cut + 1).trim() : "";
      return { id: title, title, detail, status: "open" };
    });
    const hasHfGate = gates.some((g) => `${g?.id ?? ""} ${g?.title ?? ""}`.toLowerCase().includes("hf"));
    const allGates = hasHfGate
      ? gates
      : [
          ...gates,
          {
            id: "hf_field_validation",
            title: "HF field validation",
            status: "unresolved",
            detail: "no off-air HF recordings",
          },
        ];
    const detectors = scorecard?.detectors ?? {};
    const decoderCounts = scorecard?.decoders ?? {};
    return html`
      <div style="display:flex;flex-direction:column;gap:10px">
        <p class="dim" style="font-size:0.85em;margin:0">
          Validation status (synthetic + reference fixtures; field gates listed below)
        </p>
        ${!v ? html`<p class="dim" style="font-size:0.85em">loading…</p>` : ""}
        <div>
          <span class="dim" style="font-size:0.8em">corpus</span>
          ${corpus.length
            ? html`<div>
                ${corpus.map(
                  (c) => html`
                    <div class="row" style="cursor:default">
                      <span class="label" style="flex:1">${c.id ?? "--"}${c.class ? ` · ${c.class}` : ""}</span>
                      <span class="dim mono" style="font-size:0.8em">${c.status ?? "unknown"}</span>
                      ${c.files_located != null
                        ? html`<span class="dim mono" style="font-size:0.8em">${c.files_located} files</span>`
                        : ""}
                    </div>
                  `
                )}
              </div>`
            : html`<p class="empty">no corpus cases reported</p>`}
        </div>
        <div>
          <span class="dim" style="font-size:0.8em">decoders</span>
          ${decoders.length
            ? html`<div>
                ${decoders.map(
                  (dec) => html`
                    <div class="row" style="cursor:default">
                      <span class="label" style="flex:1">${dec.label ?? dec.id}</span>
                      <span class="dim mono" style="font-size:0.8em">${dec.validation_state ?? "unknown"}</span>
                      ${(dec.validation_stale ?? dec.stale) ? html`<span class="chip">stale</span>` : ""}
                    </div>
                  `
                )}
              </div>`
            : html`<p class="empty">no decoder validation reported</p>`}
        </div>
        ${scorecard
          ? html`<p class="dim mono" style="font-size:0.8em;margin:0">
              scorecard generated ${scorecard.generated_at ?? "--"} · detectors: correct
              ${detectors.correct ?? 0}, incorrect ${detectors.incorrect ?? 0}, abstained ${detectors.abstained ?? 0},
              unavailable ${detectors.unavailable ?? 0} · decoders: valid ${decoderCounts.valid ?? 0}, invalid
              ${decoderCounts.invalid ?? 0}, unknown ${decoderCounts.unknown ?? 0}, false output
              ${decoderCounts.false_output ?? 0}
            </p>`
          : ""}
        <div>
          <span class="dim" style="font-size:0.8em">outstanding gates</span>
          <div>
            ${allGates.map(
              (g) => html`
                <div class="row" style="cursor:default;align-items:flex-start">
                  <span class="label" style="flex:1">${g.title ?? g.id}</span>
                  <span class="dim mono" style="font-size:0.8em">${g.status ?? "unresolved"}</span>
                </div>
                ${g.detail ? html`<p class="dim" style="font-size:0.75em;margin:0 0 4px">${g.detail}</p>` : ""}
              `
            )}
          </div>
        </div>
      </div>
    `;
  }

  _catalogGroup(d) {
    return html`
      <div class="setup-group setup-catalog-group">
        <p class="setup-help">Catalog sources supply frequency and station data. Leave a URL blank to use the built-in source when one is available. Refresh updates that source now; Save stores URL and language changes.</p>
        ${this._available.map((entry) => this._catalogSourceRow(entry, d))}
        ${this._field(
          "languages (comma-separated)",
          html`<input
            .value=${d.catalogLanguages}
            @input=${(e) => this._set("catalogLanguages", e.target.value)}
          />`
        )}
        ${this._group(
          "import list",
          html`<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
            <input
              style="flex:1;min-width:120px"
              placeholder="path"
              .value=${d.importPath}
              @input=${(e) => this._set("importPath", e.target.value)}
            />
            <select @change=${(e) => this._set("importFormat", e.target.value)}>
              <option value="sdrpp" ?selected=${d.importFormat === "sdrpp"}>SDR++ JSON</option>
              <option value="gqrx" ?selected=${d.importFormat === "gqrx"}>GQRX CSV</option>
            </select>
            <button @click=${() => this._doImport()}>IMPORT LIST</button>
          </div>`
        )}
      </div>
    `;
  }

  render() {
    if (!this.open) return html``;
    const d = this._draft;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="setup-drawer panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="setup-dialog-title"
          tabindex="-1"
          @click=${(ev) => ev.stopPropagation()}
          @keydown=${(ev) => this._onModalKeydown(ev)}
        >
          <div class="setup-drawer-header">
            <div>
              <h2 id="setup-dialog-title" class="setup-title">Setup</h2>
              <p class="setup-help">Configure the receiver and the tools shown in your workspaces. Most changes apply after Save.</p>
            </div>
            <button class="setup-close" aria-label="Close setup" title="Close setup" @click=${() => this._close()}>×</button>
          </div>
          ${d ? html`<nav class="setup-nav" aria-label="Setup sections">
            ${[
              ["setup-radio", "Radio"], ["setup-features", "Features"],
              ["setup-recordings", "Recordings"], ["setup-decoder", "Decoder"],
              ["setup-wifi", "Wi-Fi"], ["setup-ble", "BLE"],
              ["setup-integrations", "Integrations"],
              ["setup-survey", "Survey"], ["setup-beacons", "Beacons"],
              ["setup-passes", "Passes"], ["setup-catalog", "Catalog"],
              ["setup-appearance", "Appearance"], ["setup-layout", "Layout"],
              ["setup-about", "About"],
            ].map(([id, label]) => html`<button data-setup-section=${id} @click=${() => this._jumpToSection(id)}>${label}</button>`)}
          </nav>` : ""}
          ${this.failure
            ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
            : ""}
          ${!d ? html`<p class="dim">${this._loading ? "loading…" : ""}</p>` : this._form(d)}
        </div>
      </div>
    `;
  }

  _form(d) {
    return html`
      <div class="setup-form">
      <details class="setup-section" id="setup-radio" open>
      ${this._sectionSummary("setup-radio", "Radio")}
      ${this._field(
        "sdrconnect host",
        html`<input name="host" .value=${d.host} @input=${(e) => this._set("host", e.target.value)} />`
      )}
      ${this._field(
        "port",
        html`<input
          name="port"
          type="number"
          .value=${d.port}
          @input=${(e) => this._set("port", e.target.value)}
        />`
      )}
      ${this._field(
        "dwell (s)",
        html`<input
          name="dwell-seconds"
          type="number"
          step="0.5"
          .value=${d.dwellSeconds}
          @input=${(e) => this._set("dwellSeconds", e.target.value)}
        />`
      )}
      ${this._field(
        "nearby (kHz)",
        html`<input
          name="nearby-khz"
          type="number"
          .value=${d.nearbyKhz}
          @input=${(e) => this._set("nearbyKhz", e.target.value)}
        />`
      )}
      ${this._field(
        "region",
        html`<select name="region" @change=${(e) => this._set("region", e.target.value)}>
          <option value="US" ?selected=${d.region === "US"}>US</option>
        </select>`
      )}
      ${this._field(
        "allocation jurisdiction",
        html`<select name="allocation_jurisdiction" @change=${(e) => this._set("allocationJurisdiction", e.target.value)}>
          ${ALLOCATION_JURISDICTIONS.map(
            ([value, label]) => html`<option value=${value} ?selected=${d.allocationJurisdiction === value}>${label}</option>`
          )}
        </select>`
      )}
      ${this._field(
        "timezone",
        html`
          <div style="display:flex;flex-direction:column;gap:4px">
            <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
              <input
                name="timezone-name"
                list="timezone-options"
                style="flex:1;min-width:140px"
                placeholder="e.g. Europe/Rome"
                ?disabled=${d.timezoneAuto}
                .value=${d.timezoneName}
                @input=${(e) => this._setTimezoneName(e.target.value)}
              />
              <datalist id="timezone-options">
                ${this._timezones.map((name) => html`<option value=${name}></option>`)}
              </datalist>
              <label
                class="row mono"
                style="cursor:pointer;display:flex;align-items:center;gap:4px;font-size:0.85em"
              >
                <input
                  type="checkbox"
                  .checked=${d.timezoneAuto}
                  @change=${(e) => this._setTimezoneAuto(e.target.checked)}
                />
                auto-detect
              </label>
            </div>
            <span class="dim mono" style="font-size:0.8em">${this._timezoneEffectiveLabel()}</span>
            ${this._timezoneError
              ? html`<span style="color:var(--danger);font-size:0.8em">${this._timezoneError}</span>`
              : ""}
          </div>
        `
      )}

      </details>
      <details class="setup-section" id="setup-features">
      ${this._sectionSummary("setup-features", "Features")}
      ${this._featuresGroup(d)}

      </details>
      <details class="setup-section" id="setup-recordings">
      ${this._sectionSummary("setup-recordings", "Recordings")}
      ${this._field("Recording output folder", html`<input
        name="recorder-root" type="text" .value=${d.recorderRoot}
        placeholder="~/Recordings/msdr"
        @input=${e => this._set("recorderRoot", e.target.value)} />`)}
      <p class="dim">Use an absolute path or ~/ on the msdr server. New IQ recordings go in
        recordings/, validation examples in rfml-validation/, and default clips in clips/.
        Disarm recording and stop replay before changing this folder. Existing files stay in their original location.</p>

      ${this._field("Validation samples folder", html`<input
        name="validation-samples-dir" type="text" .value=${d.validationSamplesDir}
        placeholder="Default: recording output folder/rfml-validation"
        @input=${e => this._set("validationSamplesDir", e.target.value)} />`)}
      <p class="dim">Use an absolute path or ~/. Empty uses the recording folder. Existing samples stay where they are.</p>
      </details>
      <details class="setup-section" id="setup-decoder">
      ${this._sectionSummary("setup-decoder", "Decoder & validation")}
      ${this._field(
        "binary search dirs (one per line)",
        html`<textarea
          name="decoder-bin-dirs"
          rows="3"
          .value=${d.decoderBinDirsText}
          @input=${(e) => this._set("decoderBinDirsText", e.target.value)}
        ></textarea>`
      )}
      ${this._field(
        "audio sample rate (Hz)",
        html`<input
          name="decoder-audio-sample-rate"
          type="number"
          .value=${d.decoderAudioSampleRate}
          @input=${(e) => this._set("decoderAudioSampleRate", e.target.value)}
        />`
      )}

      <h3>LoRa / MeshCore / Meshtastic IQ</h3>
      <p class="dim">Uses SDRconnect IQ or recordings through the built-in LoRa decoder. Match the sender's RF profile; selecting a mesh protocol does not decrypt its payload. No companion devices are required.</p>
      <div class="control-row" style="flex-wrap:wrap">
        <label>Spreading factor <select name="lora-spreading-factor" .value=${String(d.loraSpreadingFactor)} @change=${e => this._set("loraSpreadingFactor", Number(e.target.value))}>${[7,8,9,10,11,12].map(n => html`<option value=${String(n)}>SF${n}</option>`)}</select></label>
        <label>Bandwidth <select name="lora-bandwidth" .value=${String(d.loraBandwidth)} @change=${e => this._set("loraBandwidth", Number(e.target.value))}>${[62500,125000,250000,500000].map(n => html`<option value=${String(n)}>${n / 1000} kHz</option>`)}</select></label>
        <label>Coding rate <select name="lora-coding-rate" .value=${String(d.loraCodingRate)} @change=${e => this._set("loraCodingRate", Number(e.target.value))}>${[1,2,3,4].map(n => html`<option value=${String(n)}>4/${4+n}</option>`)}</select></label>
        <label>Payload protocol <select name="lora-protocol" .value=${d.loraProtocol} @change=${e => this._set("loraProtocol", e.target.value)}><option value="lora">Raw LoRa</option><option value="meshtastic">Meshtastic</option><option value="meshcore">MeshCore</option></select></label>
      </div>
      <p class="dim">SF11 / 250 kHz / 4/5 is the Meshtastic Long Fast profile. Other presets and MeshCore networks may use different parameters. MeshCore US uses SF7 / 62.5 kHz / 4/5 at 910.525 MHz.</p>

      <h2>Dependencies</h2>
      ${this._dependenciesGroup()}

      <h2>Validation</h2>
      ${this._validationGroup()}

      </details>
      <details class="setup-section" id="setup-wifi">
      ${this._sectionSummary("setup-wifi", "Wi-Fi")}
      <wifi-setup-section
        .settings=${this._orig?.wifi ?? {}}
        @save-wifi=${(ev) => this._saveWifi(ev)}
      ></wifi-setup-section>
      </details>
      <details class="setup-section" id="setup-ble">
      ${this._sectionSummary("setup-ble", "BLE")}
      <ble-setup-section
        .settings=${this._orig?.ble ?? {}}
        @save-ble=${(ev) => this._saveBle(ev)}
      ></ble-setup-section>
      </details>
      <details class="setup-section" id="setup-integrations">
      ${this._sectionSummary("setup-integrations", "Integrations")}
      <integration-panel
        .settings=${this._orig?.integrations ?? {}}
        @save-integration=${(ev) => this._saveIntegration(ev)}
      ></integration-panel>
      </details>
      <details class="setup-section" id="setup-survey">
      ${this._sectionSummary("setup-survey", "Survey")}
      ${this._field(
        "threshold (dB over floor)",
        html`<input
          name="survey-threshold-db"
          type="number"
          step="0.5"
          .value=${d.surveyThresholdDb}
          @input=${(e) => this._set("surveyThresholdDb", e.target.value)}
        />`
      )}
      ${this._field(
        "spectrum fps",
        html`<input
          name="survey-spectrum-fps"
          type="number"
          min="1"
          max="23"
          .value=${d.surveySpectrumFps}
          @input=${(e) => this._set("surveySpectrumFps", e.target.value)}
        />`
      )}
      ${this._field(
        "peak threshold (dB over floor)",
        html`<input
          name="survey-peak-threshold-db"
          type="number"
          step="0.5"
          .value=${d.surveyPeakThresholdDb}
          @input=${(e) => this._set("surveyPeakThresholdDb", e.target.value)}
        />`
      )}
      <label class="row mono" style="display:flex;align-items:center;gap:6px;font-size:.85em">
        <input type="checkbox" aria-label="Exclude receiver center"
          .checked=${d.surveyCenterExclusion}
          @change=${event => this._set("surveyCenterExclusion", event.target.checked)}>
        Exclude receiver center (DC spike)</label>
      <p class="dim" style="font-size:.8em">Off by default. Enable only if your receiver produces a center spike. Exclusion also skips real signals in the center bins.</p>
      ${this._field(
        "label TTL (s)",
        html`<input
          name="survey-label-ttl-s"
          type="number"
          min="1"
          .value=${d.surveyLabelTtlS}
          @input=${(e) => this._set("surveyLabelTtlS", e.target.value)}
        />`
      )}
      ${this._field(
        "recognised label TTL (s, blank = keep for the session)",
        html`<input
          name="survey-recognized-ttl-s"
          type="number"
          step="10"
          min="0"
          placeholder="keep"
          title="A signal the analyzer named (family, RDS station, confirmed decode) stays in the table this long after it was last seen; blank keeps it until the survey is cleared or the span changes"
          .value=${d.surveyRecognizedTtlS}
          @input=${(e) => this._set("surveyRecognizedTtlS", e.target.value)}
        />`
      )}
      ${this._field(
        "max analyzed (IQ analyzer targets)",
        html`<input
          name="survey-max-analyzed"
          type="number"
          min="1"
          .value=${d.surveyMaxAnalyzed}
          @input=${(e) => this._set("surveyMaxAnalyzed", e.target.value)}
        />`
      )}
      ${this._field(
        "analyze dwell (s)",
        html`<input
          name="survey-analyze-dwell-s"
          type="number"
          step="0.5"
          min="0"
          .value=${d.surveyAnalyzeDwellS}
          @input=${(e) => this._set("surveyAnalyzeDwellS", e.target.value)}
        />`
      )}
      <label
        class="row mono"
        style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:0.85em"
        title="Americas: FM stations sit on odd tenths only (88.1, 88.3 …); even-tenth peaks are HD Radio sidebands and are folded into the station next to them instead of costing an RDS dwell"
      >
        <input
          type="checkbox"
          name="survey-rds-odd-channels"
          .checked=${d.surveyRdsOddChannels}
          @change=${(e) => this._set("surveyRdsOddChannels", e.target.checked)}
        />
        RDS: FM channels on odd tenths only (US / Americas)
      </label>

      ${this._group("Waterfall", this._waterfallGroup(d))}

      </details>
      <details class="setup-section" id="setup-beacons">
      ${this._sectionSummary("setup-beacons", "Beacons")}
      ${this._field(
        "SNR threshold (dB)",
        html`<input
          name="beacons-snr-threshold-db"
          type="number"
          step="0.5"
          .value=${d.beaconsSnrThresholdDb}
          @input=${(e) => this._set("beaconsSnrThresholdDb", e.target.value)}
        />`
      )}
      ${this._field(
        "NDB frequencies (Hz, one per line)",
        html`<textarea
          name="beacons-ndb-frequencies-hz"
          rows="4"
          .value=${d.beaconsNdbFrequenciesText}
          @input=${(e) => this._set("beaconsNdbFrequenciesText", e.target.value)}
        ></textarea>`
      )}
      ${this._ndbPicker(d)}

      </details>
      <details class="setup-section" id="setup-appearance">
      ${this._sectionSummary("setup-appearance", "Appearance")}
      ${this._field(
        "Context and evidence panels",
        html`<select
          name="evidence-panel-style"
          .value=${d.evidencePanelStyle}
          @change=${(e) => this._set("evidencePanelStyle", e.target.value)}
        >
          <option value="outline">B · Colored outline only</option>
          <option value="tinted">C · Colored outline + subtle tint</option>
        </select>`
      )}

      </details>
      <details class="setup-section" id="setup-layout">
      ${this._sectionSummary("setup-layout", "Layout")}
      ${this._layoutGroup(d)}

      </details>
      <details class="setup-section" id="setup-passes">
      ${this._sectionSummary("setup-passes", "Passes")}
      ${this._field(
        "days",
        html`<input
          name="passes-days"
          type="number"
          min="1"
          max="14"
          .value=${d.passesDays}
          @input=${(e) => this._set("passesDays", e.target.value)}
        />`
      )}
      ${this._field(
        "min elevation (°)",
        html`<input
          name="passes-min-elevation"
          type="number"
          min="0"
          max="90"
          .value=${d.passesMinElevation}
          @input=${(e) => this._set("passesMinElevation", e.target.value)}
        />`
      )}
      ${this._field(
        "horizon (°)",
        html`<input
          name="passes-horizon"
          type="number"
          min="0"
          max="45"
          .value=${d.passesHorizon}
          @input=${(e) => this._set("passesHorizon", e.target.value)}
        />`
      )}
      ${this._field(
        "top",
        html`<input
          name="passes-top"
          type="number"
          min="1"
          max="200"
          .value=${d.passesTop}
          @input=${(e) => this._set("passesTop", e.target.value)}
        />`
      )}
      ${this._group(
        "satellites",
        html`
          <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
            ${SATELLITES.map(
              (sat) => html`
                <span
                  class="tag-chip mono ${d.satellites.has(sat) ? "selected" : ""}"
                  style="cursor:pointer;border-color:${d.satellites.has(sat)
                    ? "var(--accent)"
                    : "transparent"};${d.satellites.has(sat) ? "color:var(--accent)" : ""}"
                  @click=${() => this._toggleSat(sat)}
                  >[${sat}]</span
                >
              `
            )}
          </div>
          ${d.satellites.size === 0
            ? html`<p class="dim" style="font-size:0.8em;margin:4px 0 0">from bound memories, else all</p>`
            : ""}
        `
      )}
      ${this._group("extra satellites (by NORAD id, additive)", this._extraSatellitesGroup(d))}
      ${this._field(
        "optically visible passes only",
        html`<button class="mono" style="width:max-content" @click=${() => this._toggleVisibleOnly()}>
          ${d.visibleOnly ? "ON" : "OFF"}
        </button>`
      )}
      <p class="setup-help">Sunlit satellite in dark sky during any part of the pass. Leave OFF for radio reception; daylight and shadow do not prevent radio passes.</p>
      ${this._field(
        "location",
        html`<button
          class="mono"
          style="width:max-content"
          @click=${() => this._set("overrideLocation", !d.overrideLocation)}
        >
          ${d.overrideLocation ? "OVERRIDE: ON" : "OVERRIDE: OFF"}
        </button>`
      )}
      ${d.overrideLocation
        ? html`
            ${this._field(
              "lat",
              html`<input
                name="lat"
                type="number"
                step="any"
                .value=${d.lat}
                @input=${(e) => this._set("lat", e.target.value)}
              />`
            )}
            ${this._field(
              "lon",
              html`<input
                name="lon"
                type="number"
                step="any"
                .value=${d.lon}
                @input=${(e) => this._set("lon", e.target.value)}
              />`
            )}
            ${this._field(
              "altitude (m)",
              html`<input
                name="altitude-m"
                type="number"
                .value=${d.altitudeM}
                @input=${(e) => this._set("altitudeM", e.target.value)}
              />`
            )}
            ${this._field(
              "timezone",
              html`<input
                name="timezone"
                .value=${d.timezone}
                @input=${(e) => this._set("timezone", e.target.value)}
              />`
            )}
          `
        : html`<p class="dim" style="font-size:0.8em;margin:0">using nextpass location file</p>`}
      <div>
        <button
          title="Refresh TLEs"
          @click=${() => this.dispatchEvent(new CustomEvent("refresh-passes"))}
        >
          ↻ TLE
        </button>
      </div>

      </details>
      <details class="setup-section" id="setup-catalog">
      ${this._sectionSummary("setup-catalog", "Catalog", true)}
      ${this._catalogGroup(d)}
      ${this._group("Allocation sources", this._allocationSourcesGroup())}

      </details>
      <backup-control></backup-control>
      <details class="setup-section" id="setup-about">
      ${this._sectionSummary("setup-about", "About")}
      <div class="readout">
        <span class="label">Version</span>
        <span class="value mono">${this.state?.version ?? "--"}</span>
      </div>
      <p class="dim mono" style="font-size:0.85em;margin:0">${DATA_PATH_HINT}</p>
      <div style="margin-top:6px">
        <button @click=${() => this.dispatchEvent(new CustomEvent("show-getting-started"))}>
          SHOW GETTING STARTED
        </button>
      </div>
      </div>
      </details>
      </div>
      <div class="setup-savebar">
        <span class="spacer"></span>
        <button @click=${() => this._save()}>Save</button>
      </div>
    `;
  }
}

customElements.define("setup-drawer", SetupDrawer);
