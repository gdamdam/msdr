import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import { mhz } from "../format.js";
import "./allocation-summary.js";
import "./band-popover.js";

// Frequency allocation reference chart (v0.53.0, docs/allocations.md): one
// horizontal row per NTIA wall-chart decade (3 kHz-300 GHz), each a stack of
// lanes (ITU Region 1/2/3, US Federal, US Non-Federal). Bands come from
// api.allocationBands for a whole decade at a time (cheap relative to zoom,
// which is purely client-side repositioning of the same records) and are
// cached by (lo_hz, hi_hz, jurisdiction, column) for the component's life.

// --- pure helpers (exported for tests/ui/test_allocation_chart.py) --------

const _FREQ_SUFFIX_MULT = { ghz: 1e9, mhz: 1e6, khz: 1e3, hz: 1, g: 1e9, m: 1e6, k: 1e3 };
const _FREQ_PATTERN = /^(\d+\.?\d*|\.\d+)\s*(ghz|mhz|khz|hz|g|m|k)?$/i;

/** Parses the chart's "Jump to frequency" input to whole Hz, or null if
 * unparseable/non-positive. Accepts an explicit unit suffix (case-insensitive,
 * "GHz"/"MHz"/"kHz"/"Hz" or their one-letter forms, with or without a space --
 * "2.4 GHz", "2.4GHz", "2.4G") or a bare number, which falls back to the same
 * heuristic as format.js's parseFrequencyInput: <1000 -> MHz, 1000..999999 ->
 * kHz, otherwise Hz (so "5.264" is 5.264 MHz but "5264000" is already Hz). */
export function parseJumpFrequency(text) {
  if (typeof text !== "string") return null;
  const cleaned = text.trim().replace(/,/g, "");
  if (!cleaned) return null;
  const match = _FREQ_PATTERN.exec(cleaned);
  if (!match) return null;
  const num = Number(match[1]);
  if (!Number.isFinite(num) || num <= 0) return null;
  const suffix = match[2]?.toLowerCase();
  let multiplier;
  if (suffix) {
    multiplier = _FREQ_SUFFIX_MULT[suffix];
  } else if (num < 1000) {
    multiplier = 1e6;
  } else if (num < 1_000_000) {
    multiplier = 1e3;
  } else {
    multiplier = 1;
  }
  const hz = Math.round(num * multiplier);
  return hz > 0 ? hz : null;
}

// Ordered so the legend reads sensibly; "other" is deliberately last and
// excluded from the legend itself (see _renderLegend) -- it's a fallback for
// service names this table doesn't recognize, not a real NTIA category.
// Colours reuse the app's existing CSS custom properties (styles.css's
// --led-*/--tag-*/--context-* categorical sets) rather than a bespoke
// palette, so the chart stays in step with the rest of the UI's theming
// (dark/light, future reskins) instead of carrying its own hardcoded hexes.
export const SERVICE_FAMILIES = [
  ["fixed", "Fixed", "var(--led-blue)"],
  ["mobile", "Mobile", "var(--led-orange)"],
  ["maritime", "Maritime", "var(--context-survey)"],
  ["aeronautical", "Aeronautical", "var(--context-decode)"],
  ["broadcasting", "Broadcasting", "var(--danger)"],
  ["amateur", "Amateur", "var(--accent)"],
  ["radiolocation", "Radiolocation", "var(--tag-violet)"],
  ["radionavigation", "Radionavigation", "var(--ok)"],
  ["satellite", "Satellite", "var(--led-magenta)"],
  ["space_research", "Space research", "var(--context-listen)"],
  ["radio_astronomy", "Radio astronomy", "var(--tag-gray)"],
  ["standard_frequency", "Standard frequency/time", "var(--led-yellow)"],
  ["other", "Other", "var(--context-receiver)"],
];

const _FAMILY_COLOR = Object.fromEntries(SERVICE_FAMILIES.map(([key, , color]) => [key, color]));
const _FAMILY_LABEL = Object.fromEntries(SERVICE_FAMILIES.map(([key, label]) => [key, label]));

/** Maps one Service's *name* (not its qualifier/raw text -- a qualifier like
 * "except aeronautical mobile" would otherwise false-match "aeronautical")
 * to a stable family key, most-specific keyword first: satellite variants
 * (FIXED-SATELLITE, MOBILE-SATELLITE, ...) before their plain counterparts,
 * and the MARITIME/AERONAUTICAL mobile/radionavigation qualifiers before
 * plain MOBILE/RADIONAVIGATION. Unrecognized names fall back to "other". */
export function serviceFamily(name) {
  const n = String(name ?? "").toUpperCase();
  if (!n) return "other";
  if (n.includes("SATELLITE")) return "satellite";
  if (n.includes("RADIO ASTRONOMY")) return "radio_astronomy";
  if (n.includes("SPACE RESEARCH") || n.includes("SPACE OPERATION")) return "space_research";
  if (n.includes("STANDARD FREQUENCY")) return "standard_frequency";
  if (n.includes("MARITIME")) return "maritime";
  if (n.includes("AERONAUTICAL")) return "aeronautical";
  if (n.includes("RADIONAVIGATION")) return "radionavigation";
  if (n.includes("RADIOLOCATION")) return "radiolocation";
  if (n.includes("BROADCASTING")) return "broadcasting";
  if (n.includes("AMATEUR")) return "amateur";
  if (n.includes("MOBILE")) return "mobile";
  if (n.includes("FIXED")) return "fixed";
  return "other";
}

/** Stable family -> colour (hex), used for both the chart's band fills and
 * the legend swatches. Unknown families fall back to "other"'s grey. */
export function familyColor(family) {
  return _FAMILY_COLOR[family] ?? _FAMILY_COLOR.other;
}

export function familyLabel(family) {
  return _FAMILY_LABEL[family] ?? _FAMILY_LABEL.other;
}

/** A CSS background value for a band's primary services: one family's solid
 * colour, or an even split across several (shared bands). */
export function familyGradient(families) {
  const colors = (families.length ? families : ["other"]).map(familyColor);
  if (colors.length === 1) return colors[0];
  const stops = colors.map((c, i) => `${c} ${(i / colors.length) * 100}%, ${c} ${((i + 1) / colors.length) * 100}%`);
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

// --- chart layout (not pure/tested -- DOM-shape only) ----------------------

// NTIA wall-chart decades, 3 kHz to 300 GHz.
const DECADES = [
  [3e3, 3e4], [3e4, 3e5], [3e5, 3e6], [3e6, 3e7],
  [3e7, 3e8], [3e8, 3e9], [3e9, 3e10], [3e10, 3e11],
];

const ALL_LANES = ["itu_r1", "itu_r2", "itu_r3", "us_federal", "us_non_federal"];
const LANE_LABELS = {
  itu_r1: "ITU R1",
  itu_r2: "ITU R2",
  itu_r3: "ITU R3",
  us_federal: "US Federal",
  us_non_federal: "US Non-Federal",
};

function regionForJurisdiction(jurisdiction) {
  if (jurisdiction === "ITU1") return "itu_r1";
  if (jurisdiction === "ITU3") return "itu_r3";
  return "itu_r2"; // US and ITU2 both read as Region 2
}

function defaultLanes(jurisdiction) {
  return new Set(["us_federal", "us_non_federal", regionForJurisdiction(jurisdiction)]);
}

function freqLabel(hz) {
  const trim = (n) => Number(n.toFixed(6)).toString();
  if (hz >= 1e9) return `${trim(hz / 1e9)} GHz`;
  if (hz >= 1e6) return `${trim(hz / 1e6)} MHz`;
  if (hz >= 1e3) return `${trim(hz / 1e3)} kHz`;
  return `${trim(hz)} Hz`;
}

function decadeLabel([lo, hi]) {
  return `${freqLabel(lo)} – ${freqLabel(hi)}`;
}

/** The [lo_hz, hi_hz] window a decade row currently shows, given its
 * {zoom, pan} view state -- zoom 1 is the full decade; pan (0..1) is where
 * the window's centre sits across the decade's own log range. Purely a
 * client-side repositioning of already-fetched band data (see _ensureBands). */
function windowFor(decadeIndex, view) {
  const [lo, hi] = DECADES[decadeIndex];
  const logLo = Math.log10(lo);
  const logHi = Math.log10(hi);
  const span = (logHi - logLo) / view.zoom;
  const center = logLo + view.pan * (logHi - logLo);
  let wLo = center - span / 2;
  let wHi = center + span / 2;
  if (wLo < logLo) {
    wHi += logLo - wLo;
    wLo = logLo;
  }
  if (wHi > logHi) {
    wLo -= wHi - logHi;
    wHi = logHi;
  }
  wLo = Math.max(logLo, wLo);
  wHi = Math.min(logHi, wHi);
  return { lo: Math.pow(10, wLo), hi: Math.pow(10, wHi) };
}

function leftPct(hz, win) {
  const logLo = Math.log10(win.lo);
  const logHi = Math.log10(win.hi);
  if (logHi === logLo) return 0;
  return ((Math.log10(hz) - logLo) / (logHi - logLo)) * 100;
}

const OFFICIAL_CHART_URL = "https://www.ntia.gov/sites/default/files/2025-09/ntia-us-frequency-allocations.pdf";

/** Rows-per-decade allocation chart -- see docs/allocations.md's "UI" bullet
 * for the full contract. `tunedHz` is the live receiver frequency (set by
 * app.js from state.props.device_vfo_frequency); `inspectHz` is set once,
 * from outside, when the chart is opened to inspect a particular frequency
 * (app.js's "open-allocation-chart" handler) -- the chart jumps to it and
 * runs the same lookup a band click would. */
export class AllocationChart extends LitElement {
  static properties = {
    tunedHz: { type: Number },
    inspectHz: { type: Number },
    jurisdiction: { type: String },
    _lanes: { state: true },
    _views: { state: true },
    _active: { state: true },
    _jumpText: { state: true },
    _jumpError: { state: true },
    _listView: { state: true },
    _sources: { state: true },
    _inspecting: { state: true },
    _anchor: { state: true }, // {el, rect} of the band the popover points at; null = centre over the chart
    _bandGen: { state: true }, // bumped to force re-render when the cache (a plain Map) gains an entry
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.tunedHz = null;
    this.inspectHz = null;
    this.jurisdiction = "US";
    this._lanes = defaultLanes(this.jurisdiction);
    this._views = DECADES.map(() => ({ zoom: 1, pan: 0.5 }));
    this._active = 0;
    this._jumpText = "";
    this._jumpError = null;
    this._listView = false;
    this._sources = [];
    this._inspecting = null;
    this._anchor = null;
    this._bandGen = 0;
    this._bandCache = new Map();
    this._onKeydown = this._onKeydown.bind(this);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadSources();
    this._syncBandRequests();
  }

  updated(changed) {
    if (changed.has("jurisdiction")) {
      this._lanes = defaultLanes(this.jurisdiction);
      this._syncBandRequests();
    }
    if (changed.has("inspectHz") && this.inspectHz != null && changed.get("inspectHz") !== this.inspectHz) {
      this._jumpTo(this.inspectHz);
      this._inspect(this.inspectHz);
    }
  }

  /** Anchor for a click: the clicked band. Rects are viewport-relative; the
   * placement helper makes them container-relative. */
  _anchorFromEl(el) {
    return el ? { el } : null;
  }

  /** Programmatic opens have no click target: find the band that covers hz. */
  _bandElFor(hz) {
    for (const el of this.querySelectorAll("button.allocation-band[data-lo]")) {
      if (hz >= Number(el.dataset.lo) && hz <= Number(el.dataset.hi)) return el;
    }
    return null;
  }

  async _loadSources() {
    try {
      this._sources = await api.allocationSources();
    } catch {
      this._sources = [];
    }
  }

  _syncBandRequests() {
    for (let i = 0; i < DECADES.length; i++) {
      for (const lane of this._lanes) this._ensureBands(i, lane);
    }
  }

  async _ensureBands(i, lane) {
    const [lo, hi] = DECADES[i];
    const key = `${lo}:${hi}:${this.jurisdiction}:${lane}`;
    if (this._bandCache.has(key)) return;
    this._bandCache.set(key, { status: "loading" });
    this._bandGen++;
    try {
      const bands = await api.allocationBands(lo, hi, this.jurisdiction, lane);
      this._bandCache.set(key, { status: "ok", bands: bands ?? [] });
    } catch (err) {
      this._bandCache.set(key, { status: "error", error: err.message });
    }
    this._bandGen++;
  }

  _bandsFor(i, lane) {
    const [lo, hi] = DECADES[i];
    return this._bandCache.get(`${lo}:${hi}:${this.jurisdiction}:${lane}`);
  }

  // --- interaction ---------------------------------------------------------

  _setView(i, view) {
    const next = [...this._views];
    next[i] = view;
    this._views = next;
  }

  _zoom(i, factor) {
    const view = this._views[i];
    const zoom = Math.min(2000, Math.max(1, view.zoom * factor));
    this._setView(i, { ...view, zoom });
    this._active = i;
  }

  _resetView(i) {
    this._setView(i, { zoom: 1, pan: 0.5 });
    this._active = i;
  }

  _onWheel(ev, i) {
    ev.preventDefault();
    this._active = i;
    const factor = Math.exp(-ev.deltaY * 0.0015);
    this._zoom(i, factor);
  }

  _onPointerDown(ev, i) {
    const el = ev.currentTarget;
    const start = {
      x: ev.clientX,
      pan: this._views[i].pan,
      zoom: this._views[i].zoom,
      width: el.getBoundingClientRect().width || 1,
    };
    this._active = i;
    const move = (mv) => {
      const win = windowFor(i, { zoom: start.zoom, pan: start.pan });
      const windowLogSpan = Math.log10(win.hi) - Math.log10(win.lo);
      const [lo, hi] = DECADES[i];
      const fullLogSpan = Math.log10(hi) - Math.log10(lo);
      const dx = mv.clientX - start.x;
      const deltaPan = -(dx / start.width) * (windowLogSpan / fullLogSpan);
      const pan = Math.min(1, Math.max(0, start.pan + deltaPan));
      this._setView(i, { zoom: start.zoom, pan });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  _onKeydown(ev) {
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (this._active == null) return;
    const i = this._active;
    const view = this._views[i];
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      const step = 0.02 / view.zoom;
      const pan = Math.min(1, Math.max(0, view.pan + (ev.key === "ArrowLeft" ? -step : step)));
      this._setView(i, { ...view, pan });
    } else if (ev.key === "+" || ev.key === "=") {
      ev.preventDefault();
      this._zoom(i, 1.3);
    } else if (ev.key === "-" || ev.key === "_") {
      ev.preventDefault();
      this._zoom(i, 1 / 1.3);
    }
  }

  _jumpTo(hz) {
    const i = DECADES.findIndex(([lo, hi]) => hz >= lo && hz <= hi);
    if (i === -1) {
      this._jumpError = "That frequency is outside this chart's range (3 kHz–300 GHz).";
      return;
    }
    this._jumpError = null;
    const [lo, hi] = DECADES[i];
    const pan = (Math.log10(hz) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo));
    this._setView(i, { zoom: 40, pan });
    this._active = i;
    this.updateComplete.then(() => {
      this.querySelector(`[data-decade="${i}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  _onJumpSubmit(ev) {
    ev.preventDefault();
    const hz = parseJumpFrequency(this._jumpText);
    if (hz == null) {
      this._jumpError = 'Enter a frequency like "5.264 MHz", "146.52", or "2.4 GHz".';
      return;
    }
    this._jumpTo(hz);
  }

  _toggleLane(lane, on) {
    const next = new Set(this._lanes);
    if (on) next.add(lane);
    else next.delete(lane);
    this._lanes = next;
    this._syncBandRequests();
  }

  async _inspect(hz, target = null) {
    this._anchor = this._anchorFromEl(target);
    this._inspecting = { hz, loading: true, result: null, error: null };
    try {
      const result = await api.allocationLookup(hz, this.jurisdiction);
      if (this._inspecting?.hz !== hz) return; // superseded by a later inspect
      // Bands for a jump target render after the jump; look the element up now that they exist.
      if (!this._anchor) this._anchor = this._anchorFromEl(this._bandElFor(hz));
      this._inspecting = { hz, loading: false, result, error: null };
    } catch (err) {
      if (this._inspecting?.hz !== hz) return;
      this._inspecting = { hz, loading: false, result: null, error: err.message };
    }
  }

  _closeInspect() {
    // <band-popover> returns focus to the band; the band may have re-rendered away.
    this._inspecting = null;
    this._anchor = null;
  }

  // --- rendering -----------------------------------------------------------

  render() {
    return html`
      <div class="allocation-chart" @keydown=${this._onKeydown}>
        ${this._renderHeader()}
        ${this._renderControls()}
        ${this._renderLegend()}
        ${this._listView
          ? this._renderListView()
          : html`<div class="allocation-rows">${DECADES.map((_, i) => this._renderRow(i))}</div>`}
        ${this._renderInspect()}
      </div>
    `;
  }

  _renderHeader() {
    const ntia = this._sources.find((s) => s.id === "ntia_table");
    const ecfr = this._sources.find((s) => s.id === "ecfr_2_106");
    const edition = ntia?.edition ?? "2025";
    const effective = ecfr?.effective_date ?? ntia?.effective_date ?? "date unavailable";
    return html`
      <div class="allocation-header">
        <h2>Allocations</h2>
        <p class="allocation-generated-note">
          Generated from the US Table of Frequency Allocations (NTIA Redbook ${edition}, 47 CFR §2.106
          effective ${effective}).
        </p>
        <p class="allocation-official-link">
          <a href=${OFFICIAL_CHART_URL} target="_blank" rel="noopener">Official chart (PDF, September 2025)</a>
          <span class="dim"
            >&nbsp;&mdash; this generated view is built from the normalized records and can differ from the
            published chart.</span
          >
        </p>
      </div>
    `;
  }

  _renderControls() {
    return html`
      <form class="allocation-controls" @submit=${(ev) => this._onJumpSubmit(ev)}>
        <label class="allocation-jump-label">
          Jump to frequency
          <input
            name="allocation-jump"
            placeholder='5.264 MHz, 146.52, 2.4 GHz'
            autocomplete="off"
            .value=${this._jumpText}
            @input=${(ev) => (this._jumpText = ev.target.value)}
          />
        </label>
        <button type="submit">Go</button>
        ${this._jumpError ? html`<span role="alert" class="allocation-jump-error">${this._jumpError}</span>` : ""}
        <span class="spacer"></span>
        <fieldset class="allocation-lane-toggles">
          <legend>Lanes</legend>
          ${ALL_LANES.map(
            (lane) => html`
              <label
                ><input
                  type="checkbox"
                  .checked=${this._lanes.has(lane)}
                  @change=${(ev) => this._toggleLane(lane, ev.target.checked)}
                />
                ${LANE_LABELS[lane]}</label
              >
            `
          )}
        </fieldset>
        <button type="button" aria-pressed=${this._listView} @click=${() => (this._listView = !this._listView)}>
          ${this._listView ? "Chart view" : "List view"}
        </button>
      </form>
    `;
  }

  _renderLegend() {
    return html`
      <div class="allocation-legend" aria-label="Service family colour legend">
        ${SERVICE_FAMILIES.filter(([key]) => key !== "other").map(
          ([key, label, color]) => html`
            <span class="allocation-legend-item"
              ><span class="allocation-legend-swatch" style="background:${color}"></span>${label}</span
            >
          `
        )}
        <span class="allocation-legend-item"
          ><span class="allocation-legend-swatch allocation-legend-hatch"></span>Secondary service</span
        >
      </div>
    `;
  }

  _renderRow(i) {
    const view = this._views[i];
    const win = windowFor(i, view);
    const lanes = ALL_LANES.filter((lane) => this._lanes.has(lane));
    const label = decadeLabel(DECADES[i]);
    return html`
      <section
        class="allocation-row ${this._active === i ? "active" : ""} ${view.zoom > 1 ? "expanded" : ""}"
        data-decade=${i}
      >
        <div class="allocation-row-header">
          <span class="allocation-row-label">${label}</span>
          <span class="allocation-row-window mono dim">${freqLabel(win.lo)} – ${freqLabel(win.hi)}</span>
          <div class="allocation-row-controls">
            <button type="button" aria-label="zoom in ${label}" @click=${() => this._zoom(i, 1.5)}>+</button>
            <button type="button" aria-label="zoom out ${label}" @click=${() => this._zoom(i, 1 / 1.5)}>−</button>
            <button type="button" aria-label="reset ${label} zoom" @click=${() => this._resetView(i)}>Reset</button>
          </div>
        </div>
        <div
          class="allocation-lanes"
          role="group"
          tabindex="0"
          aria-label="${label} allocation bands; arrow keys pan, plus/minus zoom"
          @focusin=${() => (this._active = i)}
          @wheel=${(ev) => this._onWheel(ev, i)}
          @pointerdown=${(ev) => this._onPointerDown(ev, i)}
        >
          ${lanes.map((lane) => this._renderLane(i, lane, win))}
          ${this._renderMarker(win, this.tunedHz, "tuned", "tuned frequency")}
          ${this._renderMarker(win, this.inspectHz, "inspect", "inspected frequency")}
        </div>
      </section>
    `;
  }

  _renderLane(i, lane, win) {
    const entry = this._bandsFor(i, lane);
    const bands = entry?.status === "ok" ? entry.bands : [];
    const visible = bands.filter((b) => b.hi_hz > win.lo && b.lo_hz < win.hi);
    return html`
      <div class="allocation-lane" data-lane=${lane}>
        <span class="allocation-lane-label mono">${LANE_LABELS[lane]}</span>
        <div class="allocation-lane-track">
          ${entry?.status === "loading" ? html`<span class="dim">loading…</span>` : ""}
          ${entry?.status === "error" ? html`<span class="dim">${entry.error}</span>` : ""}
          ${visible.map((b) => this._renderBand(b, win))}
        </div>
      </div>
    `;
  }

  _renderBand(band, win) {
    const lo = Math.max(band.lo_hz, win.lo);
    const hi = Math.min(band.hi_hz, win.hi);
    const left = leftPct(lo, win);
    const width = Math.max(leftPct(hi, win) - left, 0.4);
    const services = band.services ?? [];
    const primary = services.filter((s) => s.priority !== "secondary");
    const secondary = services.filter((s) => s.priority === "secondary");
    const families = [...new Set((primary.length ? primary : services).map((s) => serviceFamily(s.name)))];
    const fill = familyGradient(families);
    const names = services.map((s) => s.name).join(", ");
    const label = band.unallocated ? "not allocated" : names || "unspecified";
    const center = Math.round(Math.sqrt(Math.max(band.lo_hz, 1) * Math.max(band.hi_hz, 1)));
    return html`
      <button
        type="button"
        class="allocation-band ${band.unallocated ? "unallocated" : ""} ${secondary.length ? "has-secondary" : ""}"
        style="left:${left}%;width:${width}%;--band-fill:${fill}"
        title="${mhz(band.lo_hz)} – ${mhz(band.hi_hz)}: ${label}"
        aria-label="${mhz(band.lo_hz)} to ${mhz(band.hi_hz)}, ${label}"
        data-lo=${band.lo_hz}
        data-hi=${band.hi_hz}
        @click=${(ev) => this._inspect(center, ev.currentTarget)}
      ></button>
    `;
  }

  _renderMarker(win, hz, kind, label) {
    if (hz == null || hz < win.lo || hz > win.hi) return "";
    return html`<div
      class="allocation-marker allocation-marker-${kind}"
      style="left:${leftPct(hz, win)}%"
      title=${`${label}: ${mhz(hz)}`}
    ></div>`;
  }

  _listRangeIndex() {
    if (this._active != null) return this._active;
    const byTuned = this.tunedHz != null ? DECADES.findIndex(([lo, hi]) => this.tunedHz >= lo && this.tunedHz <= hi) : -1;
    return byTuned === -1 ? 0 : byTuned;
  }

  _renderListView() {
    const i = this._listRangeIndex();
    const win = windowFor(i, this._views[i]);
    const lanes = ALL_LANES.filter((lane) => this._lanes.has(lane));
    const rows = [];
    for (const lane of lanes) {
      const entry = this._bandsFor(i, lane);
      if (entry?.status !== "ok") continue;
      for (const band of entry.bands) {
        if (band.hi_hz <= win.lo || band.lo_hz >= win.hi) continue;
        rows.push({ lane, band });
      }
    }
    rows.sort((a, b) => a.band.lo_hz - b.band.lo_hz);
    return html`
      <table class="allocation-table">
        <caption>${decadeLabel(DECADES[i])} — ${freqLabel(win.lo)} to ${freqLabel(win.hi)}</caption>
        <thead>
          <tr>
            <th scope="col">Band</th>
            <th scope="col">Lane</th>
            <th scope="col">Services</th>
          </tr>
        </thead>
        <tbody>
          ${rows.length
            ? rows.map(
                ({ lane, band }) => html`
                  <tr>
                    <td class="mono">${mhz(band.lo_hz)} – ${mhz(band.hi_hz)}</td>
                    <td>${LANE_LABELS[lane]}</td>
                    <td>
                      ${band.unallocated
                        ? "not allocated"
                        : (band.services ?? []).map(
                            (s) =>
                              html`<span class="allocation-service-chip" data-priority=${s.priority ?? "primary"}
                                >${s.name}${s.qualifier ? ` (${s.qualifier})` : ""}</span
                              >`
                          )}
                    </td>
                  </tr>
                `
              )
            : html`<tr>
                <td colspan="3">No allocation data loaded for this range yet.</td>
              </tr>`}
        </tbody>
      </table>
    `;
  }

  _renderInspectContent() {
    return html`
      ${this._inspecting.loading ? html`<p class="dim">Looking up ${mhz(this._inspecting.hz)}…</p>` : ""}
      ${this._inspecting.error ? html`<p role="alert">${this._inspecting.error}</p>` : ""}
      ${this._inspecting.result
        ? html`<allocation-summary .data=${this._inspecting.result}></allocation-summary>`
        : ""}
    `;
  }

  _renderInspect() {
    if (!this._inspecting) return "";
    return html`<band-popover
      open
      label="Band details"
      .anchor=${this._anchor?.el ?? null}
      .container=${this.querySelector(".allocation-chart")}
      .content=${this._renderInspectContent()}
      @close=${() => this._closeInspect()}
    ></band-popover>`;
  }
}

customElements.define("allocation-chart", AllocationChart);
