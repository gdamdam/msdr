import { LitElement, html, svg } from "../vendor/lit.js";
import * as api from "../api.js";
import { bucketCount, indexByTime, missingCoverage, xForIndex, autoBucketS } from "./device-history-chart.js";
import "./device-label-editor.js";

let tlHatchSeq = 0; // distinct from device-history-chart.js's own counter
const POLL_MS = 30000;
const FILTER_DEBOUNCE_MS = 300;
const ROW_H = 18;
const GRID_W = 760;
const DETAIL_HOURS = 24; // matches ble-view.js's "History (24 h)" section

export const RANGE_OPTIONS = [
  { value: "1h", label: "1 h", hours: 1 },
  { value: "6h", label: "6 h", hours: 6 },
  { value: "24h", label: "24 h", hours: 24 },
  { value: "7d", label: "7 d", hours: 168 },
];
const DEFAULT_RANGE = "6h"; // GET /api/history/timeline's own default
const DEFAULT_HIDE = ["ignore"]; // docs/history.md: ignore hidden by default
const RECEIVERS = ["wifi", "ble"];
// docs/history.md labels: the class is the operator's own note about a device.
const CLASS_HELP = {
  mine: "Mine: a device you own, labelled by you.",
  known: "Known: a device you recognise but do not own, such as a neighbour's router.",
  ignore: "Ignore: noise you chose to dismiss. Hidden by default.",
};
const TIP_W = 240; // rough tooltip width for the flip-left test
export const LIMIT = 500; // the dedicated timeline wants the full cap, docs/history.md's "up to 500 rows"

export function hoursForRange(range) {
  return RANGE_OPTIONS.find((r) => r.value === range)?.hours ?? 6;
}

// --- Compare mode (docs/history.md "Capture comparison", development notes Phase 5 /
// "Long listening and local knowledge") ------------------------------------
//
// Picking the two ranges by dragging on the time axis would need hit-testing
// against the coverage band's time<->pixel mapping and a second drag mode
// that conflicts with the existing click-to-select-a-row handler on the same
// canvas; the preset plus two explicit start/end fields below reach the same
// "pick two ranges" outcome far more simply, per the task's own "if simple"
// qualifier on dragging.

/** "Last 10 min vs previous 10 min": window B is now-10m..now, window A is
 * the 10 minutes immediately before that -- the complement so the two
 * windows never overlap. */
export function lastTenVsPreviousPreset(nowS = Date.now() / 1000) {
  return { aStart: nowS - 1200, aEnd: nowS - 600, bStart: nowS - 600, bEnd: nowS };
}

/** Compare-filter state -> GET /api/history/compare query params
 * (docs/history.md "API"). */
export function buildCompareParams({ aStart, aEnd, bStart, bEnd, receivers, includePrivate }) {
  return {
    a_start: aStart, a_end: aEnd, b_start: bStart, b_end: bEnd,
    receivers: receivers?.length && receivers.length < RECEIVERS.length ? receivers : undefined,
    include_private: includePrivate || undefined,
  };
}

/** epoch seconds -> a `<input type="datetime-local">` value in local time,
 * "" when there is nothing to show yet. */
export function epochToLocalInputValue(epochS) {
  if (epochS === null || epochS === undefined || Number.isNaN(epochS)) return "";
  const d = new Date(epochS * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The reverse of epochToLocalInputValue: a datetime-local field's value ->
 * epoch seconds, or null when it can't be parsed (an emptied field). */
export function localInputValueToEpoch(value) {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** Toggle `value` in `arr`, returning a new array (never mutates) -- shared
 * by the receivers checkboxes and the hide-class checkboxes. */
export function toggleInArray(arr, value) {
  const list = arr ?? [];
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** Filter state -> GET /api/history/timeline query params (docs/history.md
 * "API"). `receivers`/`hide` are comma lists server-side; api.js's
 * getTimeline() does that join, so arrays are passed through untouched. */
export function buildTimelineParams({ range, receivers, includePrivate, hideClasses, q }) {
  return {
    hours: hoursForRange(range),
    receivers: receivers?.length && receivers.length < RECEIVERS.length ? receivers : undefined,
    include_private: includePrivate || undefined,
    hide: hideClasses?.length ? hideClasses : undefined,
    q: q || undefined,
    limit: LIMIT,
  };
}

/** Client-side re-application of the hide/search filters, used right after
 * an optimistic label edit (device-label-editor's `label-changed`) so a
 * device just marked `ignore` disappears immediately instead of waiting for
 * the next 30s poll -- the server already applies the same two filters on
 * every fetch, so this only ever narrows what the server already sent. */
export function applyClientFilters(devices, { hideClasses, q }) {
  const needle = (q ?? "").trim().toLowerCase();
  return (devices ?? []).filter((d) => {
    if (d.label?.class && (hideClasses ?? []).includes(d.label.class)) return false;
    if (!needle) return true;
    const hay = [d.ident, d.name, d.label?.name].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(needle);
  });
}

/** What the rows area shows instead of the grid, or null to render normally.
 * Unlike ble-view/wifi-view this isn't keyed off a receiver's run state --
 * the timeline has no single receiver to poll -- so the message states the
 * general precondition instead (docs/history.md "Shared timeline (UI)"). */
export function emptyStateFor(deviceCount, loading) {
  if (loading) return { text: "Loading timeline…", setup: false };
  if (deviceCount > 0) return null;
  return { text: "No device history yet. History is recorded while the Wi-Fi or BLE receiver runs.", setup: true };
}

/** Evenly spaced axis tick labels across [start, end): `n` ticks at x = i/n
 * (0..1 of the grid width; the end edge itself is not labelled so the last
 * label never overflows). Local HH:MM, with the date added once the range
 * spans more than a day so "02:00" is never ambiguous. */
export function axisTicks(start, end, n = 5) {
  const span = end - start;
  const count = Math.max(1, n);
  const pad = (v) => String(v).padStart(2, "0");
  const ticks = [];
  for (let i = 0; i < count; i++) {
    const d = new Date((start + (span * i) / count) * 1000);
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const label = span > 86400
      ? `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${hm}`
      : hm;
    ticks.push({ x: i / count, label });
  }
  return ticks;
}

/** Hit-test the canvas grid for the live tooltip, with the same bucket maths
 * as _drawGrid. `x01` is the pointer's 0..1 fraction of the grid width, `y`
 * its pixel offset from the grid top. `coverageGaps` maps receiver -> array
 * of column indexes where that receiver was not listening. Null when the
 * pointer is outside the rows or columns. */
export function cellAt(rows, coverageGaps, x01, y, start, bucketS, count) {
  const row = rows?.[Math.floor(y / ROW_H)];
  if (!row || !(x01 >= 0 && x01 < 1)) return null;
  const i = Math.floor(x01 * count);
  const cell = (row.cells ?? []).find((c) => Math.round((c[0] - start) / bucketS) === i);
  return {
    receiver: row.receiver,
    ident: row.ident,
    name: row.label?.name ?? null,
    t0: start + i * bucketS,
    t1: start + (i + 1) * bucketS,
    reports: cell?.[1] ?? 0,
    signal: cell?.[2] ?? null,
    signalKind: row.signal_kind ?? null,
    notListening: (coverageGaps?.[row.receiver] ?? []).includes(i),
  };
}

/** Per-receiver min/max of the cells' mean signal (`cells[i][2]`), for the
 * "signal" colour mode. One domain per receiver because Wi-Fi may report an
 * uncalibrated code while BLE reports dBm -- never one scale across both.
 * A receiver with no signal values maps to null. */
export function signalDomains(devices) {
  const out = {};
  for (const d of devices ?? []) {
    for (const c of d.cells ?? []) {
      const v = c[2];
      if (v === null || v === undefined) continue;
      const dom = out[d.receiver] ?? (out[d.receiver] = { min: v, max: v });
      if (v < dom.min) dom.min = v;
      if (v > dom.max) dom.max = v;
    }
  }
  for (const r of ["wifi", "ble"]) if (!(r in out)) out[r] = null;
  return out;
}

/** Cell opacity 0..1. "reports": the cell's count over the device's own
 * busiest cell (`max`). "signal": the mean signal's position in the
 * receiver's `domain`; a cell heard with no signal value stays faint rather
 * than vanishing, so "heard" and "not heard" never look the same. */
export function cellAlpha(cell, colour, max, domain) {
  if (colour === "signal") {
    const v = cell[2];
    if (v === null || v === undefined || !domain) return 0.15;
    const span = domain.max - domain.min;
    return span > 0 ? 0.25 + 0.75 * ((v - domain.min) / span) : 1;
  }
  return Math.min(1, 0.25 + 0.75 * ((cell[1] ?? 0) / Math.max(1, max)));
}

/** "-62 dBm" or "signal code 31 (uncalibrated)" per the device's signal_kind. */
export function signalText(value, signalKind) {
  if (value === null || value === undefined) return null;
  return signalKind === "raw" ? `signal code ${value.toFixed(0)} (uncalibrated)` : `${value.toFixed(0)} dBm`;
}

function hhmm(epochS) {
  const d = new Date(epochS * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Per-device grid cell colour, by receiver (docs/history.md "row ... colour
 * (--context-wifi, --context-ble)"). Reads the CSS custom properties off
 * `host` so a theme change is picked up without touching this file. */
function receiverColor(host, receiver) {
  const style = getComputedStyle(host);
  const fallback = receiver === "wifi" ? "#ff9466" : "#b48cff";
  return style.getPropertyValue(`--context-${receiver}`).trim() || fallback;
}

/** Discover -> Timeline (`timeline-view`, docs/history.md "Shared timeline
 * (UI)"): one time axis across both receivers' recorded device history.
 * The cell grid is a single <canvas> -- up to 500 rows x 360 columns is
 * 180,000 cells, far too many for one DOM node each, so only the row labels
 * and the bounded (<=360-column) coverage bands are real elements. */
export class TimelineView extends LitElement {
  static properties = {
    _receivers: { state: true },
    _range: { state: true },
    _colour: { state: true }, // "reports" | "signal" -- what a cell's darkness means
    _includePrivate: { state: true },
    _hideClasses: { state: true },
    _q: { state: true },
    _devices: { state: true },
    _deviceTotal: { state: true },
    _truncated: { state: true },
    _start: { state: true },
    _end: { state: true },
    _bucketS: { state: true },
    _coverage: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _selected: { state: true }, // { receiver, ident } or null
    _tip: { state: true }, // { x, y, flip, cell } for the grid tooltip, or null
    _clearing: { state: true },
    _confirmClear: { state: true }, // inline "Clear all history?" prompt replaces the button
    _detail: { state: true },
    _detailError: { state: true },
    _mode: { state: true }, // "timeline" | "compare"
    _comparePreset: { state: true }, // "last10" | "custom"
    _compareA: { state: true }, // { start, end } epoch seconds
    _compareB: { state: true },
    _compareResult: { state: true },
    _compareLoading: { state: true },
    _compareError: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this._receivers = [...RECEIVERS];
    this._range = DEFAULT_RANGE;
    this._colour = "reports";
    this._includePrivate = false;
    this._hideClasses = [...DEFAULT_HIDE];
    this._q = "";
    this._devices = [];
    this._deviceTotal = 0;
    this._truncated = false;
    this._start = 0;
    this._end = 0;
    this._bucketS = 60;
    this._coverage = { wifi: [], ble: [] };
    this._loading = true;
    this._error = null;
    this._selected = null;
    this._tip = null;
    this._clearing = false;
    this._confirmClear = false;
    this._detail = null;
    this._detailError = null;
    this._mode = "timeline";
    this._comparePreset = "last10";
    this._compareA = null;
    this._compareB = null;
    this._compareResult = null;
    this._compareLoading = false;
    this._compareError = null;
    this._reqSeq = 0;
    this._detailSeq = 0;
    this._filterTimer = null;
    this._pollTimer = null;
    this._onVisibility = () => this._syncPolling();
    this._hatchId = `tl-hatch-${++tlHatchSeq}`;
  }

  connectedCallback() {
    super.connectedCallback();
    document.addEventListener("visibilitychange", this._onVisibility);
    this._syncPolling();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    clearInterval(this._pollTimer);
    clearTimeout(this._filterTimer);
    super.disconnectedCallback();
  }

  updated(changed) {
    // Leaving Compare discards the grid canvas; the new one starts blank, so
    // a switch back to the timeline must redraw rather than wait for the poll.
    const backToTimeline = changed.has("_mode") && this._mode === "timeline";
    if (!changed.has("_devices") && !changed.has("_selected") && !changed.has("_colour") && !backToTimeline) return;
    this._drawGrid();
  }

  // Auto-refresh every 30s, but only `while visible` (docs/history.md) --
  // same pattern as sky-view.js's _syncPolling.
  _syncPolling() {
    clearInterval(this._pollTimer);
    this._pollTimer = null;
    if (document.hidden) return;
    this._refreshRows();
    this._pollTimer = setInterval(() => this._refreshRows(), POLL_MS);
  }

  _filters() {
    return { range: this._range, receivers: this._receivers, includePrivate: this._includePrivate,
      hideClasses: this._hideClasses, q: this._q };
  }

  /** Every request carries its own sequence token so a slow response for a
   * filter the operator already changed away from can't clobber newer data
   * (same pattern as ble-view.js/wifi-view.js's _refreshRows). */
  async _refreshRows() {
    this._confirmClear = false; // a refresh invalidates the pending prompt
    const seq = ++this._reqSeq;
    try {
      const data = await api.getTimeline(buildTimelineParams(this._filters()));
      if (seq !== this._reqSeq) return;
      this._devices = data.devices ?? [];
      this._deviceTotal = data.device_total ?? this._devices.length;
      this._truncated = !!data.truncated;
      this._start = data.start ?? 0;
      this._end = data.end ?? 0;
      this._bucketS = data.bucket_s ?? 60;
      this._coverage = data.coverage ?? { wifi: [], ble: [] };
      this._error = null;
      this._loading = false;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._error = err.message;
      this._loading = false;
    }
  }

  _visibleDevices() {
    // Both receivers unchecked has no representation in the request's
    // `receivers` comma list (an empty one is indistinguishable from
    // "omitted", which the server takes as "both") -- enforced client-side
    // instead so unchecking both really does show nothing.
    const byReceiver = this._devices.filter((d) => this._receivers.includes(d.receiver));
    return applyClientFilters(byReceiver, { hideClasses: this._hideClasses, q: this._q });
  }

  _setQuery(value) {
    this._q = value;
    clearTimeout(this._filterTimer);
    this._filterTimer = setTimeout(() => this._refreshRows(), FILTER_DEBOUNCE_MS);
  }

  _toggleReceiver(r) {
    this._receivers = toggleInArray(this._receivers, r);
    this._refreshRows();
  }

  _toggleHideClass(cls) {
    this._hideClasses = toggleInArray(this._hideClasses, cls);
    this._refreshRows();
  }

  _toggleIncludePrivate() {
    this._includePrivate = !this._includePrivate;
    this._refreshRows();
  }

  _setRange(range) {
    this._range = range;
    this._refreshRows();
  }

  _select(receiver, ident) {
    const same = this._selected?.receiver === receiver && this._selected?.ident === ident;
    this._selected = same ? null : { receiver, ident };
    this._detail = null;
    this._detailError = null;
    if (this._selected) this._fetchDetail(receiver, ident);
  }

  async _fetchDetail(receiver, ident) {
    const seq = ++this._detailSeq;
    try {
      const data = await api.getDeviceHistory(receiver, ident, {
        hours: DETAIL_HOURS, bucket: autoBucketS(DETAIL_HOURS * 3600),
      });
      if (seq !== this._detailSeq) return;
      this._detail = data;
      this._detailError = null;
    } catch (err) {
      if (seq !== this._detailSeq) return;
      this._detail = null;
      this._detailError = err.message;
    }
  }

  /** device-label-editor's `label-changed`: patch the row in place (name
   * shown in bold once labelled, class chip, hide-class re-evaluated) and
   * the detail pane's own copy, without waiting for the next poll. */
  _onLabelChanged(e) {
    const label = e.detail;
    const sel = this._selected;
    if (!sel) return;
    this._devices = this._devices.map((d) => (d.receiver === sel.receiver && d.ident === sel.ident
      ? { ...d, label } : d));
    if (this._detail?.device) this._detail = { ...this._detail, device: { ...this._detail.device, label } };
  }

  /** POST /api/history/clear wipes every receiver's history (docs/history.md
   * "deletes all history (not labels)"), so this is not scoped to the
   * current filters and says so in the inline confirm (not window.confirm,
   * which blocks the poll loop and is unstyled). */
  async _clearHistory() {
    this._confirmClear = false;
    this._clearing = true;
    this._error = null;
    try {
      await api.clearHistory();
      this._selected = null; this._detail = null; this._tip = null;
      this._compareResult = null;
      await this._refreshRows();
    } catch (err) {
      this._error = `Clear failed: ${err.message}`;
    } finally {
      this._clearing = false;
    }
  }

  _openWifiSetup() { this.dispatchEvent(new CustomEvent("open-wifi-setup", { bubbles: true, composed: true })); }
  _openBleSetup() { this.dispatchEvent(new CustomEvent("open-ble-setup", { bubbles: true, composed: true })); }

  _setMode(mode) {
    this._mode = mode;
    if (mode === "compare" && !this._compareA) this._applyComparePreset("last10");
  }

  render() {
    return html`<section class="panel timeline-view" aria-label="Device timeline">
      <h2>Timeline</h2>
      <p class="timeline-header">One time axis for Wi-Fi and BLE device history, recorded while each receiver runs.</p>
      <div class="timeline-tabs" role="tablist" aria-label="Timeline view">
        ${[["timeline", "Timeline"], ["compare", "Compare"]].map(([id, label]) => html`<button role="tab"
          aria-selected=${this._mode === id ? "true" : "false"} @click=${() => this._setMode(id)}>${label}</button>`)}
      </div>
      ${this._mode === "compare" ? this._renderCompare() : this._renderTimeline()}
    </section>`;
  }

  _renderTimeline() {
    const rows = this._visibleDevices();
    const empty = emptyStateFor(this._devices.length, this._loading);
    return html`
      <div class="timeline-filters control-row">
        <fieldset class="timeline-receivers"
          data-help="Which receivers' devices to list. Unchecking both shows nothing.">
          <legend>Receivers</legend>
          ${RECEIVERS.map((r) => html`<label><input type="checkbox" ?checked=${this._receivers.includes(r)}
            @change=${() => this._toggleReceiver(r)} /> ${r === "wifi" ? "Wi-Fi" : "BLE"}</label>`)}
        </fieldset>
        <label data-help="How far back to look. Longer ranges use wider time slices, so each cell covers more minutes."
          >Range <select @change=${(e) => this._setRange(e.target.value)}>
          ${RANGE_OPTIONS.map((o) => html`<option value=${o.value} ?selected=${o.value === this._range}>${o.label}</option>`)}
        </select></label>
        <label data-help="What a cell's darkness means. Reports: how often the device was heard in that slice, relative to its own busiest slice. Signal: the mean signal strength in that slice, darker = stronger, scaled per receiver (BLE in dBm; Wi-Fi in dBm or this adapter's uncalibrated code)."
          >Colour <select @change=${(e) => { this._colour = e.target.value; }}>
          <option value="reports" ?selected=${this._colour === "reports"}>reports</option>
          <option value="signal" ?selected=${this._colour === "signal"}>signal strength</option>
        </select></label>
        <label data-help="BLE devices that rotate their address look like many different devices. Off by default so they do not flood the list."
          ><input type="checkbox" ?checked=${this._includePrivate} @change=${() => this._toggleIncludePrivate()} />
          include private BLE addresses</label>
        <fieldset class="timeline-hide"
          data-help="Hide devices you labelled with these classes. Ignore is hidden by default so noise you have dismissed stays out of the way.">
          <legend>Hide</legend>
          ${["mine", "known", "ignore"].map((cls) => html`<label><input type="checkbox"
            ?checked=${this._hideClasses.includes(cls)} @change=${() => this._toggleHideClass(cls)} /> ${cls}</label>`)}
        </fieldset>
        <input type="search" data-help="Filter rows by address, device name or your own label." placeholder="search address / name / label…" .value=${this._q}
          @input=${(e) => this._setQuery(e.target.value)} />
        ${this._confirmClear
          ? html`<span class="timeline-clear" role="alert">Clear all history? Labels are kept; this cannot be undone.
              <button class="link" ?disabled=${this._clearing} @click=${() => this._clearHistory()}>Yes, clear</button>
              <button class="link" @click=${() => { this._confirmClear = false; }}>Keep</button></span>`
          : html`<button class="timeline-clear" ?disabled=${this._clearing} @click=${() => { this._confirmClear = true; }}
          data-help="Delete all recorded device history, for both receivers and every range. Labels are kept. Asks first.">Clear history…</button>`}
      </div>

      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}

      <div class=${`timeline-body${this._selected ? " has-detail" : ""}`}>
        ${empty
          ? html`<p class="empty">${empty.text}${empty.setup ? html`
              <button class="link" @click=${() => this._openWifiSetup()}>Open Wi-Fi Setup</button>
              <button class="link" @click=${() => this._openBleSetup()}>Open BLE Setup</button>` : ""}</p>`
          : html`
            ${this._renderCoverage()}
            <div class="timeline-rows">
              <div class="timeline-labels">
                ${rows.map((d) => this._renderLabel(d))}
              </div>
              <div class="timeline-grid-wrap" style="height:${rows.length * ROW_H}px">
                <canvas class="timeline-grid" width=${GRID_W} height=${Math.max(rows.length * ROW_H, 1)}
                  @click=${(e) => this._onGridClick(e, rows)}
                  @mousemove=${(e) => this._onGridMove(e, rows)}
                  @mouseleave=${() => { this._tip = null; }}></canvas>
              </div>
            </div>
            <div class="timeline-axis">
              <div class="timeline-axis-spacer"></div>
              <div class="timeline-axis-ticks">
                ${axisTicks(this._start, this._end, 5).map((t) => html`<span class="timeline-axis-tick"
                  style="left:${(t.x * 100).toFixed(2)}%">${t.label}</span>`)}
              </div>
            </div>
            ${this._colour === "signal" ? html`<p class="dim timeline-legend"
              data-help="Each row is one device. A cell's darkness is the mean signal strength in that time slice, on a scale from the weakest to the strongest value seen for that receiver in this range. A faint cell with a dotted look was heard but carried no signal value. Hatched columns in the bands above mean that receiver was not listening.">Darker = stronger mean signal in that time slice (scale per receiver: weakest to strongest seen in this range). Faintest = heard, no signal value. Hatched = receiver not listening.</p>`
            : html`<p class="dim timeline-legend"
              data-help="Each row is one device. A cell's darkness is its report count in that time slice divided by the same device's busiest slice, so a faint cell on one row can still be busier than a dark cell on another. Hatched columns in the bands above mean that receiver was not listening, so an empty cell there says nothing about the device.">Darker = more reports in that time slice, relative to the device's own busiest slice. Hatched = receiver not listening.</p>`}
            ${this._tip ? this._renderTip(this._tip) : ""}
            ${this._truncated ? html`<p class="dim" data-help="The server caps the timeline at ${LIMIT} devices, busiest first. Narrow the range or filters to see the rest.">showing ${this._devices.length} of ${this._deviceTotal}</p>` : ""}
          `}
        ${this._selected ? this._renderDetail() : ""}
      </div>

      <p class="dim timeline-footer"
        data-help="Co-occurrence only: two rows that start and stop together may be the same physical thing, but msdr never claims that, and nothing links Wi-Fi and BLE rows.">Rows that start and stop together are shown side by side; msdr never links them.</p>
    `;
  }

  _renderLabel(d) {
    const selected = this._selected?.receiver === d.receiver && this._selected?.ident === d.ident;
    const name = d.label?.name;
    return html`<div class=${`timeline-label${selected ? " selected" : ""}`}
      style="height:${ROW_H}px" @click=${() => this._select(d.receiver, d.ident)}>
      <span class="chip timeline-receiver-chip timeline-receiver-${d.receiver}"
        data-help=${d.receiver === "wifi" ? "Heard by the Wi-Fi receiver." : "Heard by the BLE receiver."}>${d.receiver}</span>
      ${name ? html`<strong>${name}</strong>` : html`<span class="mono">${d.ident}</span>`}
      ${d.label?.class ? html`<span class="chip timeline-class-chip timeline-class-${d.label.class}"
        data-help=${CLASS_HELP[d.label.class] ?? ""}>${d.label.class}</span>` : ""}
    </div>`;
  }

  _onGridClick(e, rows) {
    const rect = e.target.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const row = rows[Math.floor(y / ROW_H)];
    if (row) this._select(row.receiver, row.ident);
  }

  _onGridMove(e, rows) {
    const rect = e.target.getBoundingClientRect();
    const count = bucketCount(this._start, this._end, this._bucketS || 60);
    const bucketS = this._bucketS || 60;
    const gaps = {};
    for (const r of RECEIVERS) gaps[r] = missingCoverage(this._coverage?.[r] ?? [], this._start, bucketS, count);
    const cell = cellAt(rows, gaps, (e.clientX - rect.left) / rect.width, e.clientY - rect.top, this._start, bucketS, count);
    // Flip to the left of the pointer near the right edge so it stays on screen.
    this._tip = cell ? { x: e.clientX, y: e.clientY, flip: e.clientX + TIP_W + 16 > window.innerWidth, cell } : null;
  }

  _renderTip({ x, y, flip, cell }) {
    const pos = flip ? `right:${window.innerWidth - x + 12}px` : `left:${x + 12}px`;
    return html`<div class="timeline-tooltip" style="${pos};top:${y + 12}px">
      <strong>${cell.name ?? cell.ident}</strong> <span class="dim">${cell.receiver}</span><br>
      ${hhmm(cell.t0)}–${hhmm(cell.t1)}: ${cell.reports} report${cell.reports === 1 ? "" : "s"}${signalText(cell.signal, cell.signalKind) ? `, mean ${signalText(cell.signal, cell.signalKind)}` : ""}
      ${cell.notListening ? html`<br><em>receiver not listening</em>` : ""}
    </div>`;
  }

  /** Paints every row's cells onto the single shared canvas -- this is the
   * only place that draws per-cell detail, so a 500x360 grid never becomes
   * 180,000 DOM nodes. */
  _drawGrid() {
    const canvas = this.querySelector("canvas.timeline-grid");
    if (!canvas || typeof canvas.getContext !== "function") return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const rows = this._visibleDevices();
    const count = bucketCount(this._start, this._end, this._bucketS || 60);
    const domains = this._colour === "signal" ? signalDomains(rows) : null;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    rows.forEach((d, ri) => {
      const color = receiverColor(this, d.receiver);
      const max = Math.max(1, ...((d.cells ?? []).map((c) => c[1] ?? 0)));
      for (const cell of d.cells ?? []) {
        const i = Math.round((cell[0] - this._start) / (this._bucketS || 60));
        if (i < 0 || i >= count) continue;
        const x = xForIndex(i, count) * GRID_W;
        const w = Math.max(GRID_W / count, 1);
        ctx.globalAlpha = cellAlpha(cell, this._colour, max, domains?.[d.receiver]);
        ctx.fillStyle = color;
        ctx.fillRect(x, ri * ROW_H + 2, w, ROW_H - 4);
      }
    });
    ctx.globalAlpha = 1;
  }

  /** Receiver coverage bands (docs/history.md: "Top: one coverage band per
   * receiver. Wi-Fi shows the tuned channel(s) per column; a column where
   * the receiver was not listening is hatched"). Bounded to <=360 columns
   * (the server's own bucket auto-selection), so plain SVG elements are
   * fine here -- this is not the per-row cell grid. */
  _renderCoverage() {
    const count = bucketCount(this._start, this._end, this._bucketS || 60);
    const colW = GRID_W / count;
    const bands = this._receivers.map((r) => {
      const rows = this._coverage?.[r] ?? [];
      const gaps = missingCoverage(rows, this._start, this._bucketS || 60, count);
      const byT = r === "wifi" ? indexByTime(rows) : null;
      return html`<div class="timeline-coverage-band"
        data-help=${r === "wifi"
          ? "When the Wi-Fi receiver was listening. Hatched = not listening, so an empty cell there does not mean the device was absent. Small digits are the channel it was tuned to in that slice."
          : "When the BLE receiver was listening. Hatched = not listening, so an empty cell there does not mean the device was absent."}>
        <span class="chip timeline-receiver-chip timeline-receiver-${r}">${r === "wifi" ? "Wi-Fi" : "BLE"}</span>
        <svg viewBox="0 0 ${GRID_W} 14" width="100%" height="14" role="img" aria-label=${`${r} receiver coverage`}>
          <defs>
            <pattern id=${`${this._hatchId}-${r}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="6" height="6" fill="var(--panel-well)"></rect>
              <line x1="0" y1="0" x2="0" y2="6" stroke="var(--line)" stroke-width="2"></line>
            </pattern>
          </defs>
          ${gaps.map((i) => svg`<rect x=${(xForIndex(i, count) * GRID_W).toFixed(1)} y="0"
            width=${Math.max(colW, 1).toFixed(1)} height="14" class="timeline-coverage-gap"
            fill=${`url(#${this._hatchId}-${r})`}></rect>`)}
          ${r === "wifi" ? Array.from({ length: count }, (_, i) => {
              const t = this._start + i * (this._bucketS || 60);
              const row = byT.get(t);
              const channels = row?.[2];
              if (!channels?.length) return "";
              return svg`<text x=${(xForIndex(i, count) * GRID_W + colW / 2).toFixed(1)} y="11"
                text-anchor="middle" font-size="8" class="timeline-coverage-channel">${channels[0]}</text>`;
            }) : ""}
        </svg>
      </div>`;
    });
    return html`<div class="timeline-coverage">${bands}</div>`;
  }

  _renderDetail() {
    const sel = this._selected;
    if (!sel) return "";
    if (this._detailError) return html`<aside class="timeline-detail panel">
      <p class="ble-problem" role="alert">Could not load history for ${sel.ident}: ${this._detailError}</p></aside>`;
    if (!this._detail) return html`<aside class="timeline-detail panel"><p class="dim">Loading…</p></aside>`;
    const d = this._detail.device ?? {};
    return html`<aside class="timeline-detail panel" aria-label="Device details">
      <header class="timeline-detail-head">
        <span class="chip timeline-receiver-chip timeline-receiver-${sel.receiver}">${sel.receiver}</span>
        <h3 class="mono">${sel.ident}</h3>
        <button class="timeline-detail-close" aria-label="Close details" @click=${() => this._select(sel.receiver, sel.ident)}>×</button>
      </header>
      <dl class="ble-facts">
        <dt>Kind</dt><dd>${d.kind ?? "unknown"}</dd>
        ${d.name ? html`<dt>Name</dt><dd>${d.name}</dd>` : ""}
        <dt>Reports</dt><dd>${d.reports ?? 0}</dd>
        <dt>First/last seen</dt><dd>${this._epoch(d.first_seen)} – ${this._epoch(d.last_seen)}</dd>
      </dl>
      <device-label-editor .receiver=${sel.receiver} .ident=${sel.ident} .kind=${d.kind}
        .label=${d.label ?? null} @label-changed=${(e) => this._onLabelChanged(e)}></device-label-editor>
      <h4>History (${DETAIL_HOURS} h)</h4>
      <device-history-chart .cells=${this._detail.cells ?? []} .coverage=${this._detail.coverage ?? []}
        .bucketS=${this._detail.bucket_s ?? 60} .start=${this._detail.start ?? 0} .end=${this._detail.end ?? 0}
        .signalKind=${d.signal_kind ?? "dbm"}></device-history-chart>
    </aside>`;
  }

  _epoch(ts) {
    if (ts === null || ts === undefined) return "--";
    return new Date(ts * 1000).toLocaleString();
  }

  // --- Compare mode (docs/history.md "Capture comparison") -----------------

  _applyComparePreset(preset) {
    this._comparePreset = preset;
    if (preset === "last10") {
      const w = lastTenVsPreviousPreset();
      this._compareA = { start: w.aStart, end: w.aEnd };
      this._compareB = { start: w.bStart, end: w.bEnd };
      this._runCompare();
    }
  }

  _setCompareBound(which, field, value) {
    const epoch = localInputValueToEpoch(value);
    if (epoch === null) return;
    this._comparePreset = "custom";
    const target = which === "A" ? this._compareA : this._compareB;
    const next = { ...(target ?? {}), [field]: epoch };
    if (which === "A") this._compareA = next; else this._compareB = next;
  }

  async _runCompare() {
    if (!this._compareA?.start || !this._compareA?.end || !this._compareB?.start || !this._compareB?.end) return;
    this._compareLoading = true;
    this._compareError = null;
    try {
      const params = buildCompareParams({
        aStart: this._compareA.start, aEnd: this._compareA.end,
        bStart: this._compareB.start, bEnd: this._compareB.end,
        receivers: this._receivers, includePrivate: this._includePrivate,
      });
      this._compareResult = await api.getHistoryCompare(params);
      this._compareError = null;
    } catch (err) {
      this._compareError = err.message;
    } finally {
      this._compareLoading = false;
    }
  }

  _renderCompare() {
    const a = this._compareA ?? {};
    const b = this._compareB ?? {};
    return html`
      <p class="timeline-header">Two listening windows — "device off" vs "device on", or an antenna/placement
        test — shown as appeared, disappeared and changed devices, normalized by each window's own coverage.</p>
      <div class="control-row compare-presets">
        <button aria-pressed=${this._comparePreset === "last10" ? "true" : "false"}
          @click=${() => this._applyComparePreset("last10")}>Last 10 min vs previous 10 min</button>
        <span class="dim">or set custom ranges below, then Compare</span>
      </div>
      <div class="control-row compare-ranges">
        <fieldset>
          <legend>Window A</legend>
          <label>Start <input type="datetime-local" .value=${epochToLocalInputValue(a.start)}
            @change=${(e) => this._setCompareBound("A", "start", e.target.value)} /></label>
          <label>End <input type="datetime-local" .value=${epochToLocalInputValue(a.end)}
            @change=${(e) => this._setCompareBound("A", "end", e.target.value)} /></label>
        </fieldset>
        <fieldset>
          <legend>Window B</legend>
          <label>Start <input type="datetime-local" .value=${epochToLocalInputValue(b.start)}
            @change=${(e) => this._setCompareBound("B", "start", e.target.value)} /></label>
          <label>End <input type="datetime-local" .value=${epochToLocalInputValue(b.end)}
            @change=${(e) => this._setCompareBound("B", "end", e.target.value)} /></label>
        </fieldset>
        <button @click=${() => this._runCompare()}>Compare</button>
      </div>
      ${this._compareError ? html`<p role="alert">${this._compareError}</p>` : ""}
      ${this._compareLoading ? html`<p class="dim">Comparing…</p>` : this._renderCompareResult()}
    `;
  }

  _renderCompareResult() {
    const r = this._compareResult;
    if (!r) return html`<p class="empty">Pick a preset or custom ranges, then Compare.</p>`;
    return html`
      ${r.notes.map((n) => html`<p class="dim compare-note">${n}</p>`)}
      <div class="compare-coverage">
        ${r.receivers.map((rec) => html`<span class=${`chip timeline-receiver-chip timeline-receiver-${rec}`}>
          ${rec}: ${r.comparable[rec] ? "comparable" : "not comparable"}</span>`)}
      </div>
      <div class="compare-lists">
        <section><h4>Appeared <span class="dim">(${r.appeared.length})</span></h4>${this._renderCompareList(r.appeared)}</section>
        <section><h4>Disappeared <span class="dim">(${r.disappeared.length})</span></h4>${this._renderCompareList(r.disappeared)}</section>
        <section><h4>Changed <span class="dim">(${r.changed.length})</span></h4>${this._renderCompareChanged(r.changed)}</section>
      </div>
    `;
  }

  _renderCompareList(rows) {
    if (!rows.length) return html`<p class="dim">none</p>`;
    return html`<ul class="compare-list">${rows.map((d) => html`<li class=${d.comparable ? "" : "not-comparable"}>
      <span class="chip timeline-receiver-chip timeline-receiver-${d.receiver}">${d.receiver}</span>
      ${d.name ? html`<strong>${d.name}</strong>` : html`<span class="mono">${d.ident}</span>`}
      <span class="dim">${d.reports} reports${d.rate_per_min != null ? `, ${d.rate_per_min}/min` : ""}${d.mean_signal != null ? `, ${d.mean_signal} dBm` : ""}</span>
      ${!d.comparable ? html`<span class="chip compare-flag">not comparable</span>` : ""}
    </li>`)}</ul>`;
  }

  _renderCompareChanged(rows) {
    if (!rows.length) return html`<p class="dim">none</p>`;
    return html`<ul class="compare-list">${rows.map((d) => html`<li class=${d.comparable ? "" : "not-comparable"}>
      <span class="chip timeline-receiver-chip timeline-receiver-${d.receiver}">${d.receiver}</span>
      ${d.name ? html`<strong>${d.name}</strong>` : html`<span class="mono">${d.ident}</span>`}
      <span class="dim">${d.reports_a}→${d.reports_b} reports, ${d.mean_signal_a ?? "—"}→${d.mean_signal_b ?? "—"} dBm${
        d.signal_change != null ? ` (${d.signal_change > 0 ? "+" : ""}${d.signal_change})` : ""}</span>
      ${!d.comparable ? html`<span class="chip compare-flag">not comparable</span>` : ""}
    </li>`)}</ul>`;
  }
}

customElements.define("timeline-view", TimelineView);
