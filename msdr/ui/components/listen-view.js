import { LitElement, html } from "../vendor/lit.js";
import { mhz, withLocalWindow } from "../format.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./failure-note.js";

const STORAGE_KEY = "msdr.listen.preset";
const POLL_MS = 60_000;
const VFO_DEBOUNCE_MS = 2000;
const VFO_THRESHOLD_HZ = 100_000;
const PASS_WINDOW_MS = 15 * 60 * 1000;

// Order/labels match the palette + LISTEN chip row. `needsCatalog` feeds the
// client-side gate (hidden behind a dim hint when features.catalog is off);
// `countdown` presets show a live T-mm:ss to `when` on each row.
const PRESETS = [
  { key: "on_air_my_languages", label: "NOW·MY LANGS", needsCatalog: true },
  { key: "starting_soon", label: "SOON", needsCatalog: true, countdown: true },
  { key: "unheard", label: "UNHEARD", needsCatalog: true },
  { key: "heard_this_hour", label: "THIS HOUR", needsCatalog: false },
  { key: "decodable", label: "DECODABLE", needsCatalog: true },
  { key: "beacons", label: "BEACONS", needsCatalog: true },
  { key: "passes_compatible", label: "PASSES", needsCatalog: false, countdown: true },
];

function loadStoredPreset() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return PRESETS.some((p) => p.key === v) ? v : PRESETS[0].key;
  } catch {
    return PRESETS[0].key;
  }
}

function storePreset(key) {
  try {
    localStorage.setItem(STORAGE_KEY, key);
  } catch {
    // private mode / storage disabled -- selection just won't persist
  }
}

/** ISO instant -> "T-mm:ss" countdown, or null once past/missing/invalid. */
function countdown(iso, now) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const diff = Math.round((t - now) / 1000);
  if (diff < 0) return null;
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return `T-${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** on-air/active LED rule: pass items light amber within 15 min of `when`;
 * everything else lights when `when` is null (always-on) or already past. */
function isLit(item, now) {
  const t = item.when ? Date.parse(item.when) : null;
  const valid = t !== null && !Number.isNaN(t);
  if (item.kind === "pass") {
    if (!valid) return false;
    const diff = t - now;
    return diff >= 0 && diff <= PASS_WINDOW_MS;
  }
  return !valid || t <= now;
}

/** LISTEN: "what should I tune to right now" -- a preset selector over
 * GET /api/listen plus rows that tune per each item's `tune` target
 * (catalog/signal/recipe -- see msdr/api/schemas.py's ListenTune).
 * Degrades to a dim line when the route 404s (backend not landed yet) or a
 * preset needs the catalog while features.catalog is off. */
export class ListenView extends LitElement {
  static properties = {
    vfoHz: { type: Number },
    catalogFeature: { type: Boolean },
    settingsSavedAt: { type: Number },
    tz: { type: Object }, // state.timezone -- see withLocalWindow
    _preset: { state: true },
    _items: { state: true },
    _error: { state: true },
    _now: { state: true },
    failure: { type: Object },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.vfoHz = null;
    this.catalogFeature = true;
    this.settingsSavedAt = null;
    this.tz = null;
    this._preset = loadStoredPreset();
    this._items = [];
    this._error = null;
    this._now = Date.now();
    this.failure = null;
    this._tickTimer = null;
    this._pollTimer = null;
    this._vfoDebounce = null;
    this._lastVfoHz = undefined;
  }

  connectedCallback() {
    super.connectedCallback();
    this._lastVfoHz = this.vfoHz;
    this._tickTimer = setInterval(() => (this._now = Date.now()), 1000);
    this._pollTimer = setInterval(() => this._maybeLoad(), POLL_MS);
    this._maybeLoad();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._tickTimer);
    clearInterval(this._pollTimer);
    clearTimeout(this._vfoDebounce);
  }

  updated(changed) {
    if (changed.has("settingsSavedAt") && changed.get("settingsSavedAt") !== undefined) {
      this._maybeLoad();
    }
    if (changed.has("vfoHz")) {
      const prev = this._lastVfoHz;
      const cur = this.vfoHz;
      this._lastVfoHz = cur;
      if (prev != null && cur != null && Math.abs(cur - prev) > VFO_THRESHOLD_HZ) {
        clearTimeout(this._vfoDebounce);
        this._vfoDebounce = setTimeout(() => this._maybeLoad(), VFO_DEBOUNCE_MS);
      }
    }
  }

  get _current() {
    return PRESETS.find((p) => p.key === this._preset) ?? PRESETS[0];
  }

  get _blocked() {
    return this._current.needsCatalog && !this.catalogFeature;
  }

  async _maybeLoad() {
    if (this._blocked) return;
    // A preset switch races an in-flight load; only the latest may write.
    const seq = (this._reqSeq = (this._reqSeq || 0) + 1);
    try {
      const res = await api.listen(this._preset, 30);
      if (seq !== this._reqSeq) return;
      this._items = res.items ?? [];
      this._error = null;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._items = [];
      this._error = err.message;
    }
  }

  _selectPreset(key) {
    if (key === this._preset) return;
    this._preset = key;
    storePreset(key);
    this._maybeLoad();
  }

  /** Dispatches purely from item.tune (see msdr/api/schemas.py's ListenTune)
   * instead of guessing from `kind`/id shape -- fixes beacons/passes tuning
   * by a synthetic id the backend doesn't recognize (404). */
  _tune(item) {
    const t = item.tune;
    if (t.kind === "catalog") {
      this.dispatchEvent(new CustomEvent("tune-catalog", { detail: { id: t.id, label: item.title } }));
    } else if (t.kind === "signal") {
      this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: t.id } }));
    } else {
      // "recipe" (e.g. a beacon): no catalog/signal row to restore from, so
      // tune by frequency + demodulator/bandwidth instead of leaving the
      // receiver in whatever mode it was already in.
      this.dispatchEvent(
        new CustomEvent("tune-frequency", {
          detail: { hz: t.frequency_hz, demodulator: t.demodulator, filter_bandwidth_hz: t.filter_bandwidth_hz },
        })
      );
    }
  }

  /** Saves like CATALOG's ★ -- only shown for catalog-kind rows (see
   * `_row`), whose `id` is a real catalog entry id the save endpoint can
   * look up. */
  _star(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("star-catalog", { detail: { id: item.id, label: item.title } }));
  }

  render() {
    const current = this._current;
    return html`
      <div class="panel">
        <h2>Listen</h2>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
          ${PRESETS.map((p) => this._chip(p))}
        </div>
        ${this._blocked
          ? html`<p class="empty">enable CATALOG in SETUP to use this preset</p>`
          : this._error
            ? html`<p class="empty">${this._error}</p>`
            : this._items.length
              ? html`<div class="list">${this._items.map((it) => this._row(it, current))}</div>`
              : html`<p class="empty">nothing for this preset right now</p>`}
      </div>
    `;
  }

  _chip(p) {
    const active = this._preset === p.key;
    const dim = p.needsCatalog && !this.catalogFeature;
    return html`
      <button
        class="tag-chip mono ${active ? "selected" : ""}"
        aria-pressed=${active}
        style="opacity:${dim ? 0.4 : 1};border-color:${active ? "var(--accent)" : "transparent"};${active
          ? "color:var(--accent)"
          : ""}"
        @click=${() => this._selectPreset(p.key)}
      >${p.label}</button>
    `;
  }

  _row(item, current) {
    const lit = isLit(item, this._now);
    const cd = current.countdown ? countdown(item.when, this._now) : null;
    return html`
      <div class="row" style="flex-direction:column;align-items:stretch;gap:2px" @click=${() => this._tune(item)}>
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot color="var(--accent)" .lit=${lit}></led-dot>
          <span class="mono">${mhz(item.frequency_hz)}</span>
          <span>${item.title}</span>
          <span class="spacer"></span>
          ${cd ? html`<span class="mono dim">${cd}</span>` : ""}
          <button
            class="tune"
            title="Tune"
            aria-label="tune ${item.title}"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => {
              ev.stopPropagation();
              this._tune(item);
            }}
          >
            ▸
          </button>
          ${item.kind === "catalog"
            ? html`<button
                title="Save as signal"
                style="font-size:0.8em;padding:2px 8px"
                @click=${(ev) => this._star(ev, item)}
              >
                ★
              </button>`
            : ""}
        </div>
        ${item.subtitle
          ? html`<div class="mono dim" style="padding-left:16px">${withLocalWindow(item.subtitle, this.tz)}</div>`
          : ""}
      </div>
    `;
  }
}

customElements.define("listen-view", ListenView);
