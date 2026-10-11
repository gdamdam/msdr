import { LitElement, html } from "../vendor/lit.js";
import { mhz, catalogOnAir, catalogWindowLabel, relativeOrDate, dateTime } from "../format.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./failure-note.js";

const DEBOUNCE_MS = 150;
const NOW_TICK_MS = 60_000;

const BANDS = [
  { value: "", label: "ALL" },
  { value: "lw_mw", label: "LW/MW <1.8 MHZ" },
  { value: "hf", label: "HF 1.8-30" },
  { value: "vhf", label: "VHF+" },
];

// Reference-catalog sources (docs/reference-catalogs.md): these, unlike the
// broadcast-schedule sources (eibi/aoki/hfcc/...), carry CatalogEntry.network
// values -- the NETWORK filter's options come from their labels, so "Any"
// plus whatever reference sources are actually imported.
// Source id -> CatalogEntry.network value written by that source's parser; the
// select sends the network value and shows the source label.
const REFERENCE_NETWORKS = {
  nist_time: "NIST",
  faa_nasr: "FAA NASR",
  ioos_hfradar: "IOOS HFRNet",
  superdarn: "SuperDARN",
};

const REF_TYPES = [
  { value: "", label: "Any type" },
  { value: "schedule", label: "Schedule" },
  { value: "station", label: "Station" },
  { value: "navaid", label: "Navaid" },
  { value: "radar_site", label: "Radar site" },
];

const _STALE_MS = 24 * 60 * 60 * 1000;

/** MHz text without mhz()'s fixed 6 decimals -- used by catalogFrequencyText
 * for an operating range, where "4.300000-4.600000 MHz" is noise the
 * contract's own example ("4.3-4.6 MHz range") doesn't want. */
function _mhzShort(hz) {
  return Number((hz / 1_000_000).toFixed(6)).toString();
}

/** Row's primary frequency text -- the three documented cases (contract's
 * "Row rendering" bullet, docs/reference-catalogs.md "API and UI"): a fixed
 * frequency (as today, via mhz()), an operating range when `frequency_hz` is
 * null and at least one of `freq_lo_hz`/`freq_hi_hz` is set, else "frequency
 * not published". Pure and exported so tests/ui can cover all three without
 * instantiating the component. */
export function catalogFrequencyText(entry) {
  if (entry?.frequency_hz != null) return mhz(entry.frequency_hz);
  const lo = entry?.freq_lo_hz;
  const hi = entry?.freq_hi_hz;
  if (lo != null && hi != null) return `${_mhzShort(lo)}–${_mhzShort(hi)} MHz range`;
  if (lo != null) return `${_mhzShort(lo)} MHz+ range`;
  if (hi != null) return `up to ${_mhzShort(hi)} MHz range`;
  return "frequency not published";
}

/** The separately styled "current <f> at <time>" badge -- null when
 * `current_freq_hz` isn't set. `stale` is true once `current_freq_at` is
 * older than 24h (or missing entirely, since an undated current frequency
 * can't be trusted as fresh either). */
export function catalogCurrentFreqBadge(entry, now = new Date()) {
  if (entry?.current_freq_hz == null) return null;
  const at = entry?.current_freq_at;
  const stale = !at || now.getTime() - Date.parse(at) > _STALE_MS;
  const rel = at ? relativeOrDate(at, now) : "unknown time";
  return { text: `current ${mhz(entry.current_freq_hz)} at ${rel}`, stale };
}

const _STATE_WORDS = {
  continuous: "continuous service",
  scheduled_on: "on air",
  scheduled_off: "off air",
  published_active: "published active",
  unknown: "state unknown",
};

/** State word from `on_air_state`, for non-schedule rows only -- schedule
 * rows keep their existing on/off-air wording (the led-dot plus the window
 * label), per the contract's "replacing any generic 'on air' wording for
 * non-schedule rows". Returns null for a schedule row (the default `ref_type`
 * when the field is missing, for an older/plain broadcast entry). */
export function catalogStateWord(entry) {
  const refType = entry?.ref_type ?? "schedule";
  if (refType === "schedule") return null;
  return _STATE_WORDS[entry?.on_air_state] ?? _STATE_WORDS.unknown;
}

/** Tune gating: `tunable` mirrors the API's derived field (falling back to
 * "has a fixed frequency" for an older response that predates it), and
 * `title` explains a disabled Tune control rather than leaving it a mystery. */
export function catalogTuneInfo(entry) {
  const tunable = entry?.tunable ?? entry?.frequency_hz != null;
  if (tunable) return { tunable: true, title: "Tune" };
  const hasRange = entry?.freq_lo_hz != null || entry?.freq_hi_hz != null;
  return {
    tunable: false,
    title: hasRange
      ? "No fixed frequency to tune -- only an operating range is published"
      : "No published frequency to tune",
  };
}

/** Does `text` look like a station identifier (contract: "<= 5 alphanumerics,
 * e.g. 'SFO', 'WWV', 'ACI1'")? Used to decide whether the search box's query
 * also goes out as `station_id` alongside the normal `q`. */
// Result orderings offered in the Sort select; NEARBY (distance) is its own
// toggle and replaces it. ON AIR ranks transmitting rows first under any of them.
const SORTS = [
  { value: "", label: "Relevance" },
  { value: "frequency", label: "Frequency" },
  { value: "time", label: "Start time (UTC)" },
  { value: "name", label: "Name" },
];

export function looksLikeStationId(text) {
  return /^[A-Za-z0-9]{1,5}$/.test((text ?? "").trim());
}

function bandRange(band) {
  if (band === "lw_mw") return { hi_hz: 1_800_000 };
  if (band === "hf") return { lo_hz: 1_800_000, hi_hz: 30_000_000 };
  if (band === "vhf") return { lo_hz: 30_000_000 };
  return {};
}

function frequencyRange(band, minMhz, maxMhz) {
  const base = bandRange(band);
  const parse = (value) => {
    const text = value.trim();
    if (!text) return null;
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) return NaN;
    const hz = Math.round(Number(text) * 1_000_000);
    return Number.isSafeInteger(hz) ? hz : NaN;
  };
  const min = parse(minMhz);
  const max = parse(maxMhz);
  if (Number.isNaN(min) || Number.isNaN(max)) {
    return { error: "Enter valid frequencies in MHz." };
  }
  if (min !== null && max !== null && min > max) {
    return { error: "Minimum frequency must be at or below maximum." };
  }
  return {
    lo_hz: min === null ? base.lo_hz : Math.max(base.lo_hz ?? 0, min),
    hi_hz: max === null ? base.hi_hz : Math.min(base.hi_hz ?? Infinity, max),
  };
}

function sourceClass(source) {
  return `catalog-source-${String(source ?? "").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

/** Search and page through imported catalog entries. Filters run server-side. */
export class CatalogView extends LitElement {
  static properties = {
    languages: { type: Array }, // settings.catalog_languages
    tz: { type: Object }, // state.timezone -- see catalogWindowLabel
    _collapsed: { state: true },
    _query: { state: true },
    _onAir: { state: true },
    _myLangs: { state: true },
    _band: { state: true },
    _minMhz: { state: true },
    _maxMhz: { state: true },
    _rangeError: { state: true },
    _entries: { state: true },
    _sources: { state: true },
    _catalogLanguages: { state: true },
    _language: { state: true },
    _selectedSources: { state: true },
    _combineSources: { state: true },
    _network: { state: true },
    _refType: { state: true },
    _nearby: { state: true },
    _sort: { state: true },
    _nearbyError: { state: true },
    _count: { state: true },
    _offset: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _catalogHasEntries: { state: true },
    _now: { state: true },
    failure: { type: Object },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.languages = [];
    this.tz = null;
    this._collapsed = false;
    this._query = "";
    this._onAir = false;
    this._myLangs = false;
    this._band = "";
    this._minMhz = "";
    this._maxMhz = "";
    this._rangeError = null;
    this._entries = [];
    this._sources = [];
    this._catalogLanguages = [];
    this._language = "";
    this._selectedSources = [];
    this._combineSources = false;
    this._network = "";
    this._refType = "";
    this._nearby = false;
    this._sort = "";
    this._nearbyError = null;
    this._count = 0;
    this._offset = 0;
    this._loading = false;
    this._error = null;
    this._catalogHasEntries = null;
    this._now = Date.now();
    this.failure = null;
    this._debounce = null;
    this._nowTimer = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._nowTimer = setInterval(() => this._refreshClock(), NOW_TICK_MS);
    this._loadSources();
    this._search();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._nowTimer);
    clearTimeout(this._debounce);
    this._requestId = (this._requestId ?? 0) + 1;
  }

  updated(changed) {
    if (changed.has("languages") && this.isConnected) {
      if (this._myLangs && !(this.languages ?? []).some(Boolean)) this._myLangs = false;
      this._offset = 0;
      this._search();
    }
  }

  _refreshClock() {
    this._now = Date.now();
    // On-air ordering is computed on the server at query time. Refresh the
    // same page and filters each minute so rows follow the current schedule.
    if (this._onAir) return this._search({ preserveScroll: true });
  }

  async _search({ preserveScroll = false, keepNearbyError = false } = {}) {
    clearTimeout(this._debounce);
    const request = (this._requestId ?? 0) + 1;
    this._requestId = request;
    this._loading = true;
    if (!keepNearbyError) this._nearbyError = null;
    const range = frequencyRange(this._band, this._minMhz, this._maxMhz);
    this._rangeError = range.error ?? null;
    if (range.error) {
      this._entries = [];
      this._count = 0;
      this._error = null;
      this._loading = false;
      return;
    }
    const languages = this._language ? [this._language]
      : this._myLangs ? (this.languages ?? []).filter(Boolean) : [];
    const query = this._query.trim();
    try {
      const res = await api.searchCatalog({
        q: query || undefined,
        station_id: looksLikeStationId(query) ? query : undefined,
        on_air: this._onAir ? 1 : undefined,
        lo_hz: range.lo_hz,
        hi_hz: range.hi_hz,
        sources: this._selectedSources,
        languages,
        network: this._network || undefined,
        ref_type: this._refType || undefined,
        sort: this._nearby ? "distance" : this._sort || undefined,
        limit: 100,
        offset: this._offset,
      });
      if (request !== this._requestId) return;
      const scrollY = preserveScroll ? (window.scrollY ?? document.documentElement.scrollTop) : null;
      this._entries = res.entries ?? [];
      this._count = res.count ?? 0;
      this._error = null;
      if (preserveScroll && scrollY !== null) {
        await this.updateComplete;
        if (request === this._requestId) window.scrollTo(window.scrollX ?? 0, scrollY);
      }
    } catch (error) {
      if (request !== this._requestId) return;
      // sort=distance's 400 ("no receiver location configured") is a normal,
      // expected response -- surface the server's own detail text inline and
      // reset the toggle rather than ever assuming a location, then redo the
      // search without it so the rest of the filters still produce results.
      if (this._nearby) {
        this._nearby = false;
        this._nearbyError = error?.message || "Unable to sort by distance.";
        this._loading = false;
        return this._search({ keepNearbyError: true });
      }
      this._entries = [];
      this._count = 0;
      this._error = `Catalog search failed${error?.message ? `: ${error.message}` : "."}`;
    } finally {
      if (request === this._requestId) this._loading = false;
    }
  }

  async _loadSources() {
    try {
      const res = await api.catalogSources();
      const details = new Map((res.available ?? []).map((source) => [source.source, source]));
      this._sources = (res.sources ?? [])
        .filter((source) => (source.entry_count ?? 0) > 0)
        .map((source) => ({ ...source, ...details.get(source.source) }));
      this._catalogLanguages = res.languages ?? [];
      this._catalogHasEntries = (res.sources ?? []).some((source) => (source.entry_count ?? 0) > 0);
      if (!this._catalogLanguages.some((item) => (item.language_filter ?? item.language) === this._language)) this._language = "";
      // Imported source ids are stable, so preserve valid selections after a refresh.
      const available = new Set(this._sources.map((source) => source.source));
      this._selectedSources = this._selectedSources.filter((source) => available.has(source));
      this._search();
    } catch {
      this._sources = [];
      this._catalogHasEntries = null;
    }
  }

  _onQueryInput(value) {
    this._requestId = (this._requestId ?? 0) + 1;
    this._loading = true;
    this._query = value;
    this._offset = 0;
    clearTimeout(this._debounce);
    this._debounce = setTimeout(() => this._search(), DEBOUNCE_MS);
  }

  _toggle(key) {
    this[key] = !this[key];
    if (key === "_myLangs" && this._myLangs) this._language = "";
    this._offset = 0;
    if (key === "_onAir" || key === "_myLangs") this._search();
  }

  _onLanguageChange(value) {
    this._language = value;
    if (value) this._myLangs = false;
    this._offset = 0;
    this._search();
  }

  _onBandChange(value) {
    this._band = value;
    this._offset = 0;
    this._search();
  }

  _onNetworkChange(value) {
    this._network = value;
    this._offset = 0;
    this._search();
  }

  _onRefTypeChange(value) {
    this._refType = value;
    this._offset = 0;
    this._search();
  }

  _onSortChange(value) {
    this._sort = value;
    this._offset = 0;
    this._search();
  }

  _toggleNearby() {
    this._nearbyError = null;
    this._nearby = !this._nearby;
    this._offset = 0;
    this._search();
  }

  /** NETWORK filter options: the labels of the reference-catalog sources
   * that are actually imported, deduplicated -- "Any" is rendered separately
   * by the <select> itself. */
  _networkOptions() {
    const seen = new Set();
    const options = [];
    for (const source of this._sources) {
      const value = REFERENCE_NETWORKS[source.source];
      if (!value || seen.has(value)) continue;
      seen.add(value);
      options.push({ value, label: source.label ?? value });
    }
    return options;
  }

  _onRangeInput(key, value) {
    this._requestId = (this._requestId ?? 0) + 1;
    this._loading = true;
    this[key] = value;
    this._offset = 0;
    clearTimeout(this._debounce);
    this._debounce = setTimeout(() => this._search(), DEBOUNCE_MS);
  }

  _toggleSource(source) {
    this._selectedSources = !this._combineSources ? [source]
      : this._selectedSources.includes(source)
      ? this._selectedSources.filter((item) => item !== source)
      : [...this._selectedSources, source];
    this._offset = 0;
    this._search();
  }

  _setCombineSources(enabled) {
    this._combineSources = enabled;
    if (!enabled && this._selectedSources.length > 1) {
      this._selectedSources = this._selectedSources.slice(-1);
      this._offset = 0;
      this._search();
    }
  }

  _page(direction) {
    this._offset = Math.max(0, Math.min(this._count - 1, this._offset + direction * 100));
    this._search();
  }

  _visibleEntries() {
    return this._entries;
  }

  _source(sourceKey) {
    return this._sources.find((source) => source.source === sourceKey)
      ?? { source: sourceKey, label: sourceKey, description: "Imported catalog source" };
  }

  _sourceTooltip(source) {
    return html`<span class="catalog-source-tooltip" role="tooltip">
      <strong>${source.label ?? source.source}</strong>
      <span>${source.description || "Imported catalog source"}</span>
      <span class="dim">${source.entry_count ?? 0} imported entries${source.fetched_at ? ` · updated ${new Date(source.fetched_at).toLocaleString()}` : ""}</span>
      <span class="dim">Catalog reference, not evidence of a received signal.</span>
    </span>`;
  }

  _tune(entry) {
    this.dispatchEvent(new CustomEvent("tune-catalog", { detail: { id: entry.id, label: entry.station } }));
  }

  _star(ev, entry) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("star-catalog", { detail: { id: entry.id, label: entry.station } }));
  }

  /** U3: opens the signal-detail panel -- a separate ⓘ button next to tune/★
   * so the row's own click (tune) keeps working. No signal_id: catalog
   * entries aren't memories -- the backend falls back to the nearest one. */
  _openSignal(ev, entry) {
    ev.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: entry.frequency_hz },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    const list = this._visibleEntries();
    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Catalog</h2>
          <button @click=${() => this.dispatchEvent(new CustomEvent("catalog-setup", { bubbles: true, composed: true }))}>
            Sources & imports
          </button>
          <button @click=${() => (this._collapsed = !this._collapsed)}>
            ${this._collapsed ? "▾" : "▴"} ${this._count} matches
          </button>
        </div>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        ${this._collapsed ? "" : this._body(list)}
      </div>
    `;
  }

  _chip(active, label, key) {
    const disabled = key === "_myLangs" && !(this.languages ?? []).some(Boolean);
    return html`
      <button class="tag-chip mono ${active ? "selected" : ""}" aria-pressed=${active}
        ?disabled=${disabled} title=${disabled ? "Set catalog languages in SETUP first" : ""}
        @click=${() => !disabled && this._toggle(key)}>${label}</button>
    `;
  }

  _body(list) {
    const filtersActive = !!this._query || this._onAir || this._myLangs || !!this._language
      || !!this._band || !!this._minMhz || !!this._maxMhz || this._selectedSources.length > 0
      || !!this._network || !!this._refType || this._nearby;
    const networkOptions = this._networkOptions();
    const first = this._count ? this._offset + 1 : 0;
    const last = Math.min(this._offset + 100, this._count);
    const scope = this._selectedSources.length
      ? `${this._selectedSources.length} source${this._selectedSources.length === 1 ? "" : "s"} selected`
      : "All imported sources";
    return html`
      <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
        <input
          name="catalog-query"
          placeholder="search station, frequency, country…"
          .value=${this._query}
          @input=${(e) => this._onQueryInput(e.target.value)}
        />
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          ${this._chip(this._onAir, "ON AIR", "_onAir")}
          ${this._chip(this._myLangs, "MY LANGUAGES", "_myLangs")}
          <label class="catalog-filter-label">Language
            <select aria-label="Catalog language" @change=${(e) => this._onLanguageChange(e.target.value)}>
              <option value="" ?selected=${!this._language}>All languages</option>
              ${this._catalogLanguages.map((item) => html`<option value=${item.language_filter ?? item.language} ?selected=${this._language === (item.language_filter ?? item.language)}>
                ${item.language} (${item.entry_count})</option>`)}
            </select>
          </label>
          <select aria-label="Catalog band" @change=${(e) => this._onBandChange(e.target.value)}>
            ${BANDS.map(
              (b) => html`<option value=${b.value} ?selected=${this._band === b.value}>${b.label}</option>`
            )}
          </select>
          <span class="catalog-range-filter" role="group" aria-label="Frequency range in MHz">
            <label>From <input aria-label="Minimum frequency MHz" inputmode="decimal"
              placeholder="min" .value=${this._minMhz}
              @input=${(e) => this._onRangeInput("_minMhz", e.target.value)} /></label>
            <span class="dim">–</span>
            <label>To <input aria-label="Maximum frequency MHz" inputmode="decimal"
              placeholder="max" .value=${this._maxMhz}
              @input=${(e) => this._onRangeInput("_maxMhz", e.target.value)} /></label>
            <span class="dim">MHz</span>
          </span>
          ${networkOptions.length
            ? html`<label class="catalog-filter-label">Network
                <select aria-label="Catalog network" @change=${(e) => this._onNetworkChange(e.target.value)}>
                  <option value="" ?selected=${!this._network}>Any</option>
                  ${networkOptions.map((o) => html`<option value=${o.value} ?selected=${this._network === o.value}>${o.label}</option>`)}
                </select>
              </label>`
            : ""}
          <label class="catalog-filter-label">Type
            <select aria-label="Catalog reference type" @change=${(e) => this._onRefTypeChange(e.target.value)}>
              ${REF_TYPES.map((t) => html`<option value=${t.value} ?selected=${this._refType === t.value}>${t.label}</option>`)}
            </select>
          </label>
          <label class="catalog-filter-label">Sort
            <select aria-label="Catalog sort" ?disabled=${this._nearby}
              title=${this._nearby ? "NEARBY sorts by distance" : ""}
              @change=${(e) => this._onSortChange(e.target.value)}>
              ${SORTS.map((o) => html`<option value=${o.value} ?selected=${this._sort === o.value}>${o.label}</option>`)}
            </select>
          </label>
          <button class="tag-chip mono ${this._nearby ? "selected" : ""}" aria-pressed=${this._nearby}
            title="Sort by distance from your configured receiver location"
            @click=${() => this._toggleNearby()}>NEARBY</button>
        </div>
        ${this._nearbyError ? html`<p class="catalog-range-error" role="alert">${this._nearbyError}</p>` : ""}
        ${this._rangeError ? html`<p class="catalog-range-error" role="alert">${this._rangeError}</p>` : ""}
        ${this._sources.length
          ? html`<div class="catalog-source-filters" aria-label="Filter by catalog source" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
              <span class="dim mono">SOURCES</span>
              <button class="catalog-source-clear" aria-pressed=${!this._selectedSources.length}
                @click=${() => { this._selectedSources = []; this._offset = 0; this._search(); }}>All sources</button>
              ${this._sources.map((source) => {
                const active = this._selectedSources.includes(source.source);
                return html`<span class="catalog-source-hint ${sourceClass(source.source)}">
                  <button class="tag-chip mono" data-source=${source.source} aria-pressed=${active}
                    @click=${() => this._toggleSource(source.source)}>${source.label ?? source.source}</button>
                  ${this._sourceTooltip(source)}
                </span>`;
              })}
              <label class="catalog-filter-label"><input type="checkbox" .checked=${this._combineSources}
                @change=${(e) => this._setCombineSources(e.target.checked)} /> Combine sources</label>
            </div>`
          : ""}
        <div class="catalog-results-status" aria-live="polite" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
          <span class="dim mono">${scope} · ${this._rangeError ? "Invalid frequency range" : this._loading ? "Searching…" : this._count ? `${first}–${last} of ${this._count} matches` : "0 matches"}</span>
          <span class="spacer"></span>
          <button ?disabled=${this._loading || this._offset <= 0} @click=${() => this._page(-1)}>Previous</button>
          <button ?disabled=${this._loading || last >= this._count} @click=${() => this._page(1)}>Next</button>
        </div>
        ${this._rangeError ? "" : this._error
          ? html`<div class="empty" role="alert"><p>${this._error}</p><button @click=${() => this._search()}>Retry</button></div>`
          : this._loading
            ? html`<p class="empty" role="status">Loading catalog entries…</p>`
          : !list.length
            ? html`<p class="empty">${filtersActive ? "no matches" : this._catalogHasEntries === false ? "no catalog loaded — refresh in SETUP ▸ CATALOG" : "no matches"}</p>`
            : html`<div class="list">${list.map((e) => this._row(e))}</div>`}
      </div>
    `;
  }

  _row(entry) {
    const refType = entry.ref_type ?? "schedule";
    const isSchedule = refType === "schedule";
    // on_air_state (when present) is authoritative -- a radar_site/unknown
    // reference row has no schedule window, so the client-side catalogOnAir
    // fallback (NULL window = always on) would be wrong for it.
    const on = entry.on_air_state
      ? ["continuous", "scheduled_on", "published_active"].includes(entry.on_air_state)
      : catalogOnAir(entry, new Date(this._now));
    const freqText = catalogFrequencyText(entry);
    const badge = catalogCurrentFreqBadge(entry, new Date(this._now));
    const stateWord = catalogStateWord(entry);
    const tuneInfo = catalogTuneInfo(entry);
    return html`
      <div
        class="row catalog-entry ${sourceClass(entry.source)}"
        data-source=${entry.source}
        data-ref-type=${refType}
        style="flex-direction:column;align-items:stretch;gap:2px"
        @click=${() => tuneInfo.tunable && this._tune(entry)}
      >
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot color="var(--accent)" .lit=${on}></led-dot>
          <span class="mono">${freqText}</span>
          ${badge
            ? html`<span class="tag-chip catalog-current-freq ${badge.stale ? "is-stale" : ""}"
                title=${badge.stale ? "Stale -- older than 24h" : "Current frequency"}>${badge.text}</span>`
            : ""}
          <span>${entry.station}</span>
          ${entry.station_id ? html`<span class="mono dim">${entry.station_id}</span>` : ""}
          ${entry.component ? html`<span class="dim">${entry.component}</span>` : ""}
          ${entry.distance_km != null ? html`<span class="dim mono">~${entry.distance_km.toFixed(0)} km</span>` : ""}
          <span class="spacer"></span>
          ${tuneInfo.tunable
            ? html`<button
                class="tune"
                title="Tune"
                aria-label="tune ${entry.station}"
                style="font-size:0.8em;padding:2px 8px"
                @click=${(ev) => {
                  ev.stopPropagation();
                  this._tune(entry);
                }}
              >
                ▸
              </button>`
            : html`<button
                class="tune"
                disabled
                title=${tuneInfo.title}
                aria-label="tune unavailable for ${entry.station}"
                style="font-size:0.8em;padding:2px 8px"
              >
                ▸
              </button>`}
          <button
            title="Save as signal"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => this._star(ev, entry)}
          >
            ★
          </button>
          <button
            title="Signal detail"
            aria-label="signal detail for ${entry.station}"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => this._openSignal(ev, entry)}
          >
            ⓘ
          </button>
        </div>
        <div class="mono dim" style="display:flex;align-items:center;gap:8px;padding-left:16px;flex-wrap:wrap">
          ${isSchedule
            ? html`
                ${entry.language ? html`<span class="tag-chip">[${entry.language}]</span>` : ""}
                ${entry.target ? html`<span class="tag-chip">[${entry.target}]</span>` : ""}
                ${entry.country ? html`<span class="tag-chip">[${entry.country}]</span>` : ""}
                <span>${catalogWindowLabel(entry, this.tz)}</span>
              `
            : html`
                ${stateWord ? html`<span class="tag-chip catalog-state-word">${stateWord}</span>` : ""}
                ${entry.network ? html`<span class="tag-chip">${entry.network}</span>` : ""}
                ${entry.operator ? html`<span>${entry.operator}</span>` : ""}
                ${entry.status ? html`<span class="tag-chip">${entry.status}</span>` : ""}
                ${entry.status_at ? html`<span>status as of ${dateTime(entry.status_at)}</span>` : ""}
              `}
          ${entry.decoder ? html`<span class="tag-chip catalog-decoder-chip">${entry.decoder}</span>` : ""}
          <span class="spacer"></span>
          ${entry.source_url
            ? html`<a class="catalog-source-url" href=${entry.source_url} target="_blank" rel="noopener noreferrer"
                @click=${(ev) => ev.stopPropagation()}>source</a>`
            : ""}
          <span class="catalog-entry-source catalog-source-hint" tabindex="0">
            ${this._source(entry.source).label ?? entry.source}
            ${this._sourceTooltip(this._source(entry.source))}
          </span>
        </div>
      </div>
    `;
  }
}

customElements.define("catalog-view", CatalogView);
