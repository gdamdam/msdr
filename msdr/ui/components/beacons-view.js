import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import { mhz, epochHM } from "../format.js";
import "./failure-note.js";
import "./run-indicator.js";

// Mirrors msdr/survey/beacons.py's NCDXF_BANDS_HZ/NCDXF_BEACONS (Stream A) --
// the grid's default columns/rows before any hit has ever named a wider set
// (an NDB session's own frequencies/idents extend both, see _gridBands/_gridBeacons).
const NCDXF_BANDS_HZ = [14_100_000, 18_110_000, 21_150_000, 24_930_000, 28_200_000];
const NCDXF_BEACONS = [
  "4U1UN", "VE8AT", "W6WX", "KH6RS", "ZL6B", "VK6RBP", "JA2IGY", "RR9O", "VR2B",
  "4S7B", "ZS6DN", "5Z4B", "4X6TU", "OH2B", "CS3B", "LU4AA", "OA4B", "YV5B",
];

const NCDXF_BANDS_MHZ = [14.1, 18.11, 21.15, 24.93, 28.2];
const NCDXF_CYCLE_S = 180;
const NCDXF_SLOT_S = 10;

export const NCDXF_LOCATIONS = {
  "4U1UN": "United Nations NY", VE8AT: "Canada", W6WX: "USA CA", KH6RS: "Hawaii",
  ZL6B: "New Zealand", VK6RBP: "Australia", JA2IGY: "Japan", RR9O: "Russia",
  VR2B: "Hong Kong", "4S7B": "Sri Lanka", ZS6DN: "South Africa", "5Z4B": "Kenya",
  "4X6TU": "Israel", OH2B: "Finland", CS3B: "Madeira", LU4AA: "Argentina",
  OA4B: "Peru", YV5B: "Venezuela",
};

const OFFSET_KEY = "msdr.beacons.clockOffsetS";
const OFFSET_MAX_S = 30;

/** Position in the 180 s cycle; offsetS shifts it so a skewed clock can be compensated. */
function cyclePos(epochMs, offsetS) {
  const s = Math.floor(epochMs / 1000) + offsetS;
  return ((s % NCDXF_CYCLE_S) + NCDXF_CYCLE_S) % NCDXF_CYCLE_S;
}

/** The IBP schedule: beacon i starts on 14.100 at second i*10 and steps up one
 * band every 10 s, so band k carries index (slot - k) mod 18. */
export function ncdxfOnAir(epochMs, offsetS = 0) {
  const p = cyclePos(epochMs, offsetS);
  const slot = Math.floor(p / NCDXF_SLOT_S);
  const n = NCDXF_BEACONS.length;
  return NCDXF_BANDS_MHZ.map((band_mhz, k) => {
    const index = (((slot - k) % n) + n) % n;
    return { band_mhz, index, beacon: NCDXF_BEACONS[index], secondsLeft: NCDXF_SLOT_S - (p % NCDXF_SLOT_S) };
  });
}

/** Band the beacon is transmitting on right now, or null while it is silent. */
export function beaconFrequencyNow(epochMs, offsetS, beaconIndex) {
  return ncdxfOnAir(epochMs, offsetS).find((e) => e.index === beaconIndex)?.band_mhz ?? null;
}

function loadOffset() {
  try {
    const v = parseInt(localStorage.getItem(OFFSET_KEY), 10);
    return Number.isFinite(v) ? Math.max(-OFFSET_MAX_S, Math.min(OFFSET_MAX_S, v)) : 0;
  } catch {
    return 0;
  }
}

const HITS_WINDOW_S = 24 * 60 * 60;
const HITS_POLL_MS = 60_000;
const HITS_SHOWN = 200;

/** Seconds since epoch -> the age bucket a grid cell is coloured by (last-heard
 * age, see the plan's UI contract): < 10 min bright, < 1 h mid, else dim; null
 * (never heard) is its own (dimmest) bucket. */
function ageBucket(lastHeard) {
  if (lastHeard === null || lastHeard === undefined) return "none";
  const ageS = Date.now() / 1000 - lastHeard;
  if (ageS < 600) return "bright";
  if (ageS < 3600) return "mid";
  return "dim";
}

const AGE_STYLE = {
  bright: "color:var(--accent);font-weight:600",
  mid: "color:var(--accent);opacity:0.6",
  dim: "color:var(--ink);opacity:0.5",
  none: "color:var(--ink);opacity:0.25",
};

/** BEACONS section: NCDXF/NDB beacon monitor -- start/stop controls, a
 * beacon x band grid coloured by last-heard age, a 24h hit-tick strip per
 * band, and a hits table with CLEAR. `status` mirrors BeaconMonitor.status()
 * (msdr/survey/beacons.py), pushed on change (<=1/s) via the "beacons" ws
 * message and merged into state.beacons by app.js, same as "survey"/"decoder".
 * Hits (for the strip and table) aren't part of that push -- this component
 * fetches them itself on connect and every 60s (same self-polling pattern as
 * sky-view.js), rather than routing through app.js. */
export class BeaconsView extends LitElement {
  static properties = {
    status: { type: Object },
    connected: { type: Boolean },
    ndbConfigured: { type: Boolean }, // settings.beacons.ndb_frequencies_hz non-empty
    failure: { type: Object },
    // Proactive START gating (mirrors AppService._check_beacons_refusal --
    // a beacon session retunes the VFO every 3 min, so it's refused while
    // any of these hold the receiver): survey IQ analyzer, BAND DECODE, or
    // an in-progress recording.
    analyzerActive: { type: Boolean },
    bandDecodeActive: { type: Boolean },
    recording: { type: Boolean },
    _hits: { state: true },
    _now: { state: true },
    _offsetS: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.status = null;
    this.connected = false;
    this.failure = null;
    this.analyzerActive = false;
    this.bandDecodeActive = false;
    this.recording = false;
    this._hits = [];
    this._pollTimer = null;
    this._tickTimer = null;
    this._now = Date.now();
    this._offsetS = loadOffset();
  }

  get _blocked() {
    return !!(this.analyzerActive || this.bandDecodeActive || this.recording);
  }

  /** START's inline disabled reason while blocked: prefers the server's own
   * 409 detail (this.failure, from the last refused attempt) over the
   * generic fallback so the operator sees exactly what the API said. */
  _startBlockedReason() {
    if (!this._blocked) return "";
    return this.failure?.message || "stop BAND DECODE / analyzer / recording first";
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadHits();
    this._pollTimer = setInterval(() => this._loadHits(), HITS_POLL_MS);
    // 1 s tick drives the "Now" countdown.
    this._tickTimer = setInterval(() => (this._now = Date.now()), 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._pollTimer);
    clearInterval(this._tickTimer);
  }

  _setOffset(v) {
    this._offsetS = Math.max(-OFFSET_MAX_S, Math.min(OFFSET_MAX_S, v));
    try {
      localStorage.setItem(OFFSET_KEY, String(this._offsetS));
    } catch {
      // storage unavailable -- the offset just won't persist
    }
  }

  async _loadHits() {
    try {
      this._hits = await api.listBeaconHits({
        since: Date.now() / 1000 - HITS_WINDOW_S,
        limit: HITS_SHOWN,
      });
    } catch {
      // route not there yet / feature off -- keep whatever we already have
    }
  }

  async _clearHits() {
    try {
      await api.clearBeaconHits();
      this._hits = [];
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "beacon hits cleared" } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _start(mode) {
    this.dispatchEvent(new CustomEvent("beacons-start", { detail: { mode } }));
  }

  _stop() {
    this.dispatchEvent(new CustomEvent("beacons-stop"));
  }

  /** Summary rows keyed "BEACON@band_hz", same key shape as status.misses. */
  _summaryMap() {
    const map = new Map();
    for (const row of this.status?.summary ?? []) map.set(`${row.beacon}@${row.band_hz}`, row);
    return map;
  }

  /** Grid columns: NCDXF's 5 bands plus any other band the summary has heard
   * on (an NDB session's own frequencies), sorted ascending. */
  _gridBands() {
    const bands = new Set(NCDXF_BANDS_HZ);
    for (const row of this.status?.summary ?? []) bands.add(row.band_hz);
    return [...bands].sort((a, b) => a - b);
  }

  /** Grid rows: the 18 NCDXF call signs in schedule order, plus any other
   * beacon name the summary knows about (NDB idents), appended alphabetically. */
  _gridBeacons() {
    const known = new Set(NCDXF_BEACONS);
    const extra = new Set();
    for (const row of this.status?.summary ?? []) {
      if (!known.has(row.beacon)) extra.add(row.beacon);
    }
    return [...NCDXF_BEACONS, ...[...extra].sort()];
  }

  /** Beacon name with its location beside it (NCDXF stations only). */
  _beaconLabel(beacon) {
    const loc = NCDXF_LOCATIONS[beacon];
    return loc ? html`<span class="mono">${beacon}</span> <span class="dim">${loc}</span>` : html`<span class="mono">${beacon}</span>`;
  }

  /** NCDXF: the two tables from ncdxf.org/beacon -- who is on each band
   * right now, and where each beacon is (or "silent") -- plus how often each
   * was heard in the last 24 h, so the schedule and the receiver's own
   * evidence sit side by side. The 180 s IBP cycle is computed from the
   * clock (+ offset), never from reception. */
  _renderSchedule() {
    const off = this._offsetS;
    const onAir = ncdxfOnAir(this._now, off);
    const summaryMap = this._summaryMap();
    const heard = (beacon) => {
      let hits = 0, last = null, snr = null;
      for (const hz of this._gridBands()) {
        const row = summaryMap.get(`${beacon}@${hz}`);
        if (!row) continue;
        hits += row.hits ?? 0;
        if (row.last_heard != null && (last == null || row.last_heard > last)) { last = row.last_heard; snr = row.last_snr_db; }
      }
      return { hits, last, snr };
    };
    return html`
      <div class="beacon-schedule">
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead><tr><th>FREQUENCY</th><th>BEACON · NOW</th><th>NEXT</th></tr></thead>
            <tbody>
              ${onAir.map((e) => html`<tr>
                <td class="mono beacon-now-band">${e.band_mhz.toFixed(3)}</td>
                <td>${this._beaconLabel(e.beacon)}</td>
                <td class="mono dim">${e.secondsLeft} s</td>
              </tr>`)}
            </tbody>
          </table>
          <p class="dim beacon-schedule-note">Updates every second from the IBP schedule.
            <span class="beacon-now-offset mono"
              title="Clock compensation: shift the schedule if your clock or the beacon network is off by a few seconds">
              <button @click=${() => this._setOffset(off - 1)} ?disabled=${off <= -OFFSET_MAX_S}>−</button>
              <span>${off} s</span>
              <button @click=${() => this._setOffset(off + 1)} ?disabled=${off >= OFFSET_MAX_S}>+</button>
            </span></p>
        </div>
        <div class="lib-table-wrap">
          <table class="lib-table">
            <thead><tr><th>BEACON</th><th>FREQUENCY</th><th>HEARD · 24 H</th></tr></thead>
            <tbody>
              ${NCDXF_BEACONS.map((beacon, i) => {
                const hz = beaconFrequencyNow(this._now, off, i);
                const h = heard(beacon);
                return html`<tr class=${hz ? "on-air" : ""}>
                  <td>${this._beaconLabel(beacon)}</td>
                  <td class="mono">${hz ? hz.toFixed(3) : html`<span class="dim">silent</span>`}</td>
                  <td class="mono" title=${h.last ? `last ${epochHM(h.last)}${h.snr != null ? ` · ${h.snr.toFixed(1)} dB` : ""}` : "not heard"}>${h.hits || html`<span class="dim">—</span>`}</td>
                </tr>`;
              })}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  _renderCell(beacon, bandHz, summaryMap, onAir) {
    const row = summaryMap.get(`${beacon}@${bandHz}`);
    const bucket = ageBucket(row?.last_heard);
    const glyph = row?.decoded ? "✓" : row?.last_heard != null ? "·" : "";
    const isCurrent =
      !!this.status?.active &&
      this.status?.current?.beacon === beacon &&
      this.status?.band_hz === bandHz;
    const title = row?.last_heard
      ? `${beacon} @ ${mhz(bandHz)} -- last heard ${epochHM(row.last_heard)}, ${
          row.last_snr_db != null ? row.last_snr_db.toFixed(1) + " dB" : "no SNR"
        }, ${row.hits} hit${row.hits === 1 ? "" : "s"}`
      : `${beacon} @ ${mhz(bandHz)} -- not heard`;
    return html`
      <td
        class="mono${onAir?.has(`${beacon}@${bandHz}`) ? " on-air" : ""}"
        style="text-align:center;${AGE_STYLE[bucket]}${isCurrent
          ? ";outline:2px solid var(--accent);outline-offset:-2px"
          : ""}"
        title=${onAir?.has(`${beacon}@${bandHz}`) ? `${title} -- on air now` : title}
      >
        ${glyph}
      </td>
    `;
  }

  _renderGrid() {
    const bands = this._gridBands();
    const beacons = this._gridBeacons();
    const summaryMap = this._summaryMap();
    const onAir =
      this.status?.mode === "ncdxf"
        ? new Set(
            ncdxfOnAir(this._now, this._offsetS).map((e) => `${e.beacon}@${Math.round(e.band_mhz * 1e6)}`)
          )
        : null;
    return html`
      <div class="lib-table-wrap" style="margin-top:8px">
        <table class="lib-table mono">
          <thead>
            <tr>
              <th>BEACON</th>
              ${bands.map((hz) => html`<th style="text-align:center">${(hz / 1_000_000).toFixed(3)}</th>`)}
            </tr>
          </thead>
          <tbody>
            ${beacons.map(
              (beacon) => html`
                <tr>
                  <td>${this._beaconLabel(beacon)}</td>
                  ${bands.map((hz) => this._renderCell(beacon, hz, summaryMap, onAir))}
                </tr>
              `
            )}
          </tbody>
        </table>
      </div>
    `;
  }

  /** One 24h strip per grid band: a tick per hit at its time-of-day fraction
   * over the trailing 24h window, bright for a decoded hit, dim for a
   * probable-only one. Plain positioned divs (not canvas) -- a tick doesn't
   * need per-pixel painting and this stays legible at any width. */
  _renderStrips() {
    const bands = this._gridBands();
    const now = Date.now() / 1000;
    const windowStart = now - HITS_WINDOW_S;
    return html`
      <div style="display:flex;flex-direction:column;gap:4px;margin-top:10px">
        ${bands.map((hz) => {
          const ticks = (this._hits ?? []).filter((h) => h.frequency_hz === hz && h.ts >= windowStart);
          return html`
            <div style="display:flex;align-items:center;gap:6px">
              <span class="dim mono" style="width:70px;font-size:0.8em;text-align:right">${(hz / 1_000_000).toFixed(3)}</span>
              <div
                style="position:relative;flex:1;height:14px;background:var(--panel-well);border:1px solid var(--line)"
                title="24h hit strip -- ${mhz(hz)}"
              >
                ${ticks.map((h) => {
                  const frac = Math.max(0, Math.min(1, (h.ts - windowStart) / HITS_WINDOW_S));
                  const color = h.decoded ? "var(--accent)" : "var(--ink)";
                  return html`<div
                    style="position:absolute;top:0;bottom:0;left:${(frac * 100).toFixed(2)}%;width:2px;background:${color}"
                    title="${h.beacon} ${epochHM(h.ts)}${h.decoded ? " (decoded)" : h.probable ? " (probable)" : ""}"
                  ></div>`;
                })}
              </div>
            </div>
          `;
        })}
      </div>
    `;
  }

  _renderHitsTable() {
    const hits = [...(this._hits ?? [])].sort((a, b) => b.ts - a.ts);
    return html`
      <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
        <span class="dim mono" style="font-size:0.85em">HITS (24h)</span>
        <span class="spacer"></span>
        <button style="font-size:0.85em" @click=${() => this._clearHits()}>CLEAR</button>
      </div>
      <div class="lib-table-wrap" style="margin-top:4px">
        <table class="lib-table mono">
          <thead>
            <tr>
              <th>TIME</th>
              <th>BEACON</th>
              <th>BAND</th>
              <th>SNR</th>
              <th>STATUS</th>
            </tr>
          </thead>
          <tbody>
            ${hits.length
              ? hits.map(
                  (h) => html`
                    <tr>
                      <td>${epochHM(h.ts)}</td>
                      <td>${this._beaconLabel(h.beacon)}</td>
                      <td>${mhz(h.frequency_hz)}</td>
                      <td>${h.snr_db != null ? h.snr_db.toFixed(1) : "--"}</td>
                      <td>${h.decoded ? "decoded" : h.probable ? "probable" : "--"}</td>
                    </tr>
                  `
                )
              : html`<tr>
                  <td colspan="5" class="empty">No beacons heard — START NCDXF (needs an HF antenna)</td>
                </tr>`}
          </tbody>
        </table>
      </div>
    `;
  }

  _renderHeader() {
    const status = this.status ?? {};
    const active = !!status.active;
    const currentLine = active
      ? `${status.mode ?? "--"} · ${status.band_hz != null ? mhz(status.band_hz) : "--"} · slot ${
          status.slot ?? "--"
        } · cycle ${status.cycle ?? 0}${
          status.current ? ` · ${status.current.beacon} until ${epochHM(status.current.until)}` : ""
        }`
      : status.since
        ? "idle"
        : "never started";
    const blocked = this._blocked;
    const reason = active ? "" : this._startBlockedReason();
    return html`
      <div class="control-row">
        <button
          class="run-start"
          ?disabled=${!this.connected || active || blocked}
          @click=${() => this._start("ncdxf")}
        >
          ▶ START NCDXF
        </button>
        <button
          class="run-start"
          ?disabled=${!this.connected || active || blocked || this.ndbConfigured === false}
          title=${this.ndbConfigured === false ? "No NDB frequencies configured — add them in Setup → Beacons (settings.beacons.ndb_frequencies_hz)" : "Cycle through the configured NDB frequencies"}
          @click=${() => this._start("ndb")}
        >
          ▶ START NDB
        </button>
        <button class="run-stop" ?disabled=${!active} @click=${() => this._stop()}>■ STOP</button>
        ${active ? html`<run-indicator .since=${status.since ?? null} label="MONITORING"></run-indicator>` : ""}
        <span class="spacer"></span>
        <span class="dim mono" style="font-size:0.85em">${currentLine}</span>
      </div>
      ${reason ? html`<div class="hint">${reason}</div>` : ""}
    `;
  }

  render() {
    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Beacons</h2>
        </div>
        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}
        ${this._renderHeader()}
        <details class="beacon-detail">
          <summary>${this.status?.mode === "ndb" ? "Beacon matrix and 24-hour activity" : "Beacon schedule and 24-hour activity"} <span class="dim">· 18 scheduled NCDXF stations</span></summary>
          ${this.status?.mode === "ndb" ? html`${this._renderGrid()}${this._renderStrips()}` : this._renderSchedule()}
        </details>
        <details class="beacon-detail">
          <summary>Hits in the last 24 hours <span class="dim">· ${this._hits.length}</span></summary>
          ${this._renderHitsTable()}
        </details>
      </div>
    `;
  }
}

customElements.define("beacons-view", BeaconsView);
