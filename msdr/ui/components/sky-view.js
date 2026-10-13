import { LitElement, html, svg } from "../vendor/lit.js";
import * as api from "../api.js?v=0.62.2";
import { passTime, passDate } from "../satellite-format.js";
import { khzDelta, signalForSatellite, timeHM } from "../format.js";

const POLL_MS = 5000;
const SIZE = 240;
const CENTER = SIZE / 2;
const R_OUTER = 96; // leaves room for cardinal labels near the edge

// Mirrors msdr/library/models.py's Satellite literal (see drawer.js/
// setup-drawer.js, which duplicate the same list) -- anything else is a
// passes_extra_satellites custom entry, which Signal.satellite can't hold.
const SATELLITES = ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"];

/** Azimuth/elevation -> chart point. North up, east right; r = (90-el)/90 *
 * R_OUTER, clamped so anything at or below the horizon sits on the rim. */
function polar(azimuthDeg, elevationDeg) {
  if (azimuthDeg === null || azimuthDeg === undefined || elevationDeg === null || elevationDeg === undefined) {
    return null;
  }
  const ratio = Math.min(1, Math.max(0, (90 - elevationDeg) / 90));
  const r = R_OUTER * ratio;
  const rad = (azimuthDeg * Math.PI) / 180;
  return { x: CENTER + r * Math.sin(rad), y: CENTER - r * Math.cos(rad) };
}

/** Polar chart of a satellite's sky track and live position, section SKY:
 * rings at el 0/30/60, the selected pass's track as a polyline with rise/set
 * markers and peak, and the live position as an LED dot -- lit amber above
 * the horizon, dim at the rim otherwise. Polls getSkyFor(label) every 5s
 * while visible; degrades to a dim line if /api/sky isn't there yet. */
export class SkyView extends LitElement {
  static properties = {
    passes: { type: Object }, // PassesResult, for the default satellite pick
    signals: { type: Array },
    selectedPass: { type: Object }, // { satellite, rise } from a PASSES row click, or SKY's own selector (rise:null)
    tracking: { type: Object }, // state.tracking or null
    connected: { type: Boolean },
    started: { type: Boolean },
    hasLocation: { type: Boolean }, // !!settings.location -- gates the "set your location" empty state
    trackingFeature: { type: Boolean }, // state.features.tracking -- hides the TRACK button when off
    _sky: { state: true }, // SkyResult from getSkyFor, or null
    _unavailable: { state: true }, // /api/sky route isn't there yet
    _tleStatus: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.passes = null;
    this.signals = [];
    this.selectedPass = null;
    this.tracking = null;
    this.connected = false;
    this.started = true;
    this.hasLocation = true;
    this.trackingFeature = true;
    this._decode = false;
    this._sky = null;
    this._unavailable = false;
    this._tleStatus = null;
    this._lastSat = undefined;
    this._pollTimer = null;
    this._onVisibility = () => this._syncPolling();
  }

  connectedCallback() {
    super.connectedCallback();
    document.addEventListener("visibilitychange", this._onVisibility);
    this._syncPolling();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener("visibilitychange", this._onVisibility);
    clearInterval(this._pollTimer);
  }

  updated() {
    const selection = this._selectionKey;
    if (selection !== this._lastSat) {
      this._lastSat = selection;
      this._sky = null;
      this._loadSky();
    }
  }

  /** Default = the satellite of the soonest upcoming pass, or the first
   * bound satellite; an explicit choice (selectedPass, from a PASSES row
   * click or SKY's own header selector) overrides both. */
  get _selectedSatellite() {
    if (this.selectedPass?.satellite) return this.selectedPass.satellite;
    const soonest = this._soonestSatellite();
    if (soonest) return soonest;
    const bound = (this.signals ?? []).find((s) => s.satellite);
    return bound ? bound.satellite : null;
  }

  _soonestSatellite() {
    const now = Date.now();
    let best = null;
    let bestRise = Infinity;
    for (const p of this.passes?.passes ?? []) {
      const rise = Date.parse(p.rise);
      if (Number.isNaN(rise) || rise <= now) continue;
      if (rise < bestRise) {
        bestRise = rise;
        best = p.satellite;
      }
    }
    return best;
  }

  /** Distinct satellites for the header <select>: current passes (in their
   * given order), plus any bound-memory satellites not already covered. */
  _satelliteOptions() {
    const seen = new Set();
    const options = [];
    for (const p of this.passes?.passes ?? []) {
      if (!seen.has(p.satellite)) {
        seen.add(p.satellite);
        options.push(p.satellite);
      }
    }
    for (const s of this.signals ?? []) {
      if (s.satellite && !seen.has(s.satellite)) {
        seen.add(s.satellite);
        options.push(s.satellite);
      }
    }
    return options;
  }

  /** Selecting a satellite in SKY's own header must not tune anything -- it
   * reuses the select-pass event (already just sets app.js's selectedPass)
   * with rise:null rather than a PASSES row's tune-satellite pairing. */
  _selectSatellite(satellite) {
    this.dispatchEvent(new CustomEvent("select-pass", { detail: { satellite, rise: null } }));
  }

  _passFor(sat) {
    return (this.passes?.passes ?? []).find((p) => p.satellite === sat) ?? null;
  }

  /** Next future rise (ISO string) for `sat`, for the "below horizon" TRACK
   * reason -- same soonest-first scan as passes-view's countdown. */
  _nextRiseForSatellite(sat) {
    const now = Date.now();
    let best = null;
    let bestMs = Infinity;
    for (const p of this.passes?.passes ?? []) {
      if (p.satellite !== sat) continue;
      const rise = Date.parse(p.rise);
      if (Number.isNaN(rise) || rise <= now) continue;
      if (rise < bestMs) {
        bestMs = rise;
        best = p.rise;
      }
    }
    return best;
  }

  /** First failing reason TRACK is disabled, in order: connection -> bound
   * memory -> horizon. null once none apply (TRACK should be enabled). */
  _trackReason(sat, bound, above) {
    if (!sat) return null;
    if (!this.connected) return { text: "not connected" };
    if (!this.started) return { text: "receiver stopped — press Play in SDRconnect" };
    if (!bound) {
      if (!SATELLITES.includes(sat)) return { text: "custom satellites can't be bound yet" };
      return { text: `no memory bound to ${sat}`, bind: true };
    }
    if (!above) {
      const rise = this._nextRiseForSatellite(sat);
      return { text: rise ? `below horizon — rises ${timeHM(rise)}` : "below horizon" };
    }
    return null;
  }

  /** BIND: create a memory for the satellite's pass downlink and bind it,
   * so TRACK's "no memory bound" reason resolves after @refresh reloads
   * signals. Mirrors the createSignal shape used elsewhere (e.g. app.js's
   * _doStarEvent/_doStarHit) rather than opening the drawer. */
  async _doBind(sat) {
    const downlink = this._passFor(sat)?.downlink;
    if (!downlink) {
      this.dispatchEvent(
        new CustomEvent("api-error", {
          detail: { message: "no downlink known — capture while tuned to it, then set its satellite" },
        })
      );
      return;
    }
    const mode = String(downlink.mode ?? "").toUpperCase();
    const demodulator = mode === "FM" ? "NFM" : mode;
    try {
      await api.createSignal({
        name: `${sat} ${downlink.label || "downlink"}`,
        frequency_hz: downlink.frequency_hz,
        demodulator,
        status: "identified",
        satellite: sat,
      });
      this.dispatchEvent(new CustomEvent("refresh"));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _syncPolling() {
    clearInterval(this._pollTimer);
    this._pollTimer = null;
    if (document.hidden) return;
    this._loadSky();
    this._pollTimer = setInterval(() => this._loadSky(), POLL_MS);
  }

  get _selectedInterval() {
    const selected = this.selectedPass;
    if (selected?.rise && selected?.set) return selected;
    return [...(this.passes?.passes ?? [])]
      .filter(p => p.satellite === this._selectedSatellite && Date.parse(p.set) > Date.now())
      .sort((a, b) => Date.parse(a.rise) - Date.parse(b.rise))[0] ?? null;
  }

  get _selectionKey() {
    const p = this._selectedInterval;
    return `${this._selectedSatellite}:${p?.rise ?? ""}:${p?.set ?? ""}`;
  }

  async _loadSky() {
    const sat = this._selectedSatellite;
    const key = this._selectionKey;
    const request = this._request = (this._request ?? 0) + 1;
    const interval = this._selectedInterval;
    if (!sat) {
      this._sky = null;
      this._unavailable = false;
      return;
    }
    try {
      const sky = await api.getSkyFor(sat, 24, interval?.rise, interval?.set);
      if (key !== this._selectionKey || request !== this._request) return;
      const matches = !interval || !sky.track || (
        Math.abs(Date.parse(sky.track.rise) - Date.parse(interval.rise)) <= 1000 &&
        Math.abs(Date.parse(sky.track.set) - Date.parse(interval.set)) <= 1000
      );
      this._sky = matches ? sky : { ...sky, track: null,
        error: "The server returned a different pass. Restart msdr and reload this page." };
      this._unavailable = false;
    } catch {
      if (key !== this._selectionKey || request !== this._request) return;
      this._sky = null;
      this._unavailable = true;
    }
  }

  async _refreshTle() {
    this._tleStatus = "refreshing…";
    try {
      const res = await api.refreshTle();
      const updated = res.updated ?? [];
      const errCount = Object.keys(res.errors ?? {}).length;
      this._tleStatus = errCount
        ? `updated ${updated.length}, ${errCount} error${errCount === 1 ? "" : "s"}`
        : `updated ${updated.length}`;
    } catch (err) {
      this._tleStatus = err.message;
    }
    this._loadSky();
  }

  /** U1: forwards to app.js's _doTrackToggle (via a "track-toggle" event),
   * same as drawer.js's own TRACK button -- keeps the "will this interrupt
   * the current owner" gate (confirmInterrupt) in one place. */
  _toggleTrack(bound, active) {
    this.dispatchEvent(new CustomEvent("track-toggle", { detail: { active, id: bound?.id ?? null, decode: this._decode } }));
  }

  render() {
    if (!this.hasLocation) {
      return html`
        <div class="panel satellite-charts" id="sky-view" tabindex="-1">
          <h2 style="margin:0;border-bottom:none;padding-bottom:0">Pass charts</h2>
          <p class="empty">
            Set your location in SETUP to predict passes
            <button
              style="margin-left:6px"
              @click=${() => this.dispatchEvent(new CustomEvent("setup"))}
            >
              SETUP
            </button>
          </p>
        </div>
      `;
    }
    const sat = this._selectedSatellite;
    const sky = this._sky;
    const degrade = this._unavailable ? "tracking unavailable" : sky?.error ?? null;
    const options = this._satelliteOptions();

    return html`
      <div class="panel satellite-charts" id="sky-view" tabindex="-1">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Pass charts</h2>
          ${options.length
            ? html`<select
                aria-label="sky satellite"
                .value=${sat ?? ""}
                @change=${(ev) => this._selectSatellite(ev.target.value)}
              >
                ${options.map((o) => html`<option value=${o} ?selected=${o === sat}>${o}</option>`)}
              </select>`
            : sat
              ? html`<span class="dim mono" style="font-size:0.85em">${sat}</span>`
              : ""}
          <button title="Refresh TLEs" @click=${() => this._refreshTle()}>↻ TLE</button>
        </div>
        ${this._passHeading(sky, sat)}
        <div class="satellite-chart-grid">
          <div><h3>Sky path</h3>${this._chart(sky, sat)}</div>
          <div><h3>Elevation over time</h3>${this._elevationChart(sky)}</div>
        </div>
        <p class="satellite-help">Path: selected pass · Dot and readouts: live position now</p>
        ${degrade ? html`<p class="empty">${degrade}</p>` : this._readouts(sky, sat)}
        ${this._tleStatus ? html`<p class="dim mono" style="font-size:0.8em;margin:6px 0 0">${this._tleStatus}</p>` : ""}
      </div>
    `;
  }

  _passHeading(sky, sat) {
    const p = this._selectedInterval ?? sky?.track;
    if (!p) return html`<p class="satellite-help">${sat ? "No upcoming track in the next 24 hours." : "Select a pass from the list."}</p>`;
    const time = iso => passTime(iso, this.passes?.timezone);
    return html`<p class="satellite-selected-heading"><strong>${sat}</strong> · ${passDate(p.rise, this.passes?.timezone)} · ${this.passes?.timezone ?? "local time"}<br>
      Rise ${time(p.rise)} → Peak ${time(p.peak ?? sky?.track?.peak)} → Set ${time(p.set)}</p>`;
  }

  _elevationChart(sky) {
    const points = sky?.track?.points ?? [];
    if (points.length < 2) return html`<p class="empty">${this._selectedSatellite ? "Waiting for pass track…" : "Select a pass"}</p>`;
    const first = Date.parse(points[0].at), last = Date.parse(points.at(-1).at);
    const x = p => 38 + (Date.parse(p.at) - first) / Math.max(1, last - first) * 294;
    const y = p => 178 - Math.max(0, p.elevation_deg) / 90 * 150;
    const line = points.map(p => `${x(p)},${y(p)}`).join(" ");
    const peak = points.reduce((a, b) => a.elevation_deg > b.elevation_deg ? a : b);
    return svg`<svg viewBox="0 0 370 220" role="img" aria-label="Selected pass elevation over time" class="satellite-elevation">
      ${[0, 30, 60, 90].map(el => svg`<line x1="38" y1=${178-el/90*150} x2="332" y2=${178-el/90*150} stroke="var(--line)"/>
        <text x="30" y=${182-el/90*150} text-anchor="end">${el}°</text>`)}
      <polyline points=${line} fill="none" stroke="var(--accent)" stroke-width="2"/>
      <circle cx=${x(peak)} cy=${y(peak)} r="3" fill="var(--accent)"/>
      <text x=${x(peak)} y=${Math.max(14, y(peak)-10)} text-anchor="middle">${Math.round(peak.elevation_deg)}°</text>
      <text x="38" y="204">${passTime(points[0].at, this.passes?.timezone)}</text>
      <text x="332" y="204" text-anchor="end">${passTime(points.at(-1).at, this.passes?.timezone)}</text>
    </svg>`;
  }

  _chart(sky, sat) {
    const track = sky?.track;
    const pos = sky?.position;
    const points = track?.points ?? [];

    const polylinePts = points
      .map((p) => polar(p.azimuth_deg, p.elevation_deg))
      .filter(Boolean)
      .map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`)
      .join(" ");
    const rise = points.length ? polar(points[0].azimuth_deg, points[0].elevation_deg) : null;
    const setPt = points.length
      ? polar(points[points.length - 1].azimuth_deg, points[points.length - 1].elevation_deg)
      : null;
    const peakPoint = points.length
      ? points.reduce((a, b) => (b.elevation_deg > a.elevation_deg ? b : a))
      : null;
    const peak = peakPoint ? polar(peakPoint.azimuth_deg, peakPoint.elevation_deg) : null;

    const live = pos ? polar(pos.azimuth_deg, pos.elevation_deg) : null;
    const liveAbove = !!pos && pos.elevation_deg != null && pos.elevation_deg >= 0;

    return svg`
      <svg
        viewBox="0 0 ${SIZE} ${SIZE}"
        width="240"
        height="240"
        style="width:100%;max-width:240px;min-width:200px;height:auto"
        role="img"
        aria-label="Sky position${sat ? ` of ${sat}` : ""}"
      >
        <circle cx=${CENTER} cy=${CENTER} r=${R_OUTER} fill="none" stroke="var(--line)" stroke-width="1"></circle>
        <circle cx=${CENTER} cy=${CENTER} r=${(R_OUTER * 60) / 90} fill="none" stroke="var(--line)" stroke-width="1"></circle>
        <circle cx=${CENTER} cy=${CENTER} r=${(R_OUTER * 30) / 90} fill="none" stroke="var(--line)" stroke-width="1"></circle>
        <line x1=${CENTER - R_OUTER} y1=${CENTER} x2=${CENTER + R_OUTER} y2=${CENTER} stroke="var(--line)" stroke-width="1"></line>
        <line x1=${CENTER} y1=${CENTER - R_OUTER} x2=${CENTER} y2=${CENTER + R_OUTER} stroke="var(--line)" stroke-width="1"></line>
        <text x=${CENTER} y=${CENTER - R_OUTER - 6} text-anchor="middle" class="sky-cardinal">N</text>
        <text x=${CENTER} y=${CENTER + R_OUTER + 15} text-anchor="middle" class="sky-cardinal">S</text>
        <text x=${CENTER + R_OUTER + 11} y=${CENTER + 4} text-anchor="middle" class="sky-cardinal">E</text>
        <text x=${CENTER - R_OUTER - 11} y=${CENTER + 4} text-anchor="middle" class="sky-cardinal">W</text>
        ${polylinePts
          ? svg`<polyline points=${polylinePts} fill="none" stroke="var(--accent)" stroke-width="1.5"></polyline>`
          : ""}
        ${rise ? svg`<text x=${rise.x} y=${rise.y - 5} text-anchor="middle" class="sky-marker">↑</text>` : ""}
        ${setPt ? svg`<text x=${setPt.x} y=${setPt.y - 5} text-anchor="middle" class="sky-marker">↓</text>` : ""}
        ${peak ? svg`<circle cx=${peak.x} cy=${peak.y} r="2.5" fill="var(--accent)"></circle>` : ""}
        ${live
          ? svg`<circle
              cx=${live.x}
              cy=${live.y}
              r=${liveAbove ? 4 : 3}
              fill=${liveAbove ? "var(--accent)" : "var(--led-unlit)"}
              stroke=${liveAbove ? "none" : "var(--ink)"}
              style=${liveAbove ? "filter:drop-shadow(0 0 3px var(--accent))" : ""}
            ></circle>`
          : ""}
      </svg>
    `;
  }

  _readouts(sky, sat) {
    const pos = sky?.position ?? null;
    const az = pos?.azimuth_deg != null ? `${Math.round(pos.azimuth_deg)}°` : "--";
    const el = pos?.elevation_deg != null ? `${pos.elevation_deg.toFixed(1)}°` : "--";
    const range = pos?.range_km != null ? `${Math.round(pos.range_km)} km` : "--";
    const doppler = this.tracking?.active ? khzDelta(this.tracking.doppler_hz) : "—";
    const tleAge = sky?.tle_age_days != null ? `tle ${sky.tle_age_days.toFixed(1)} d` : "";

    const bound = signalForSatellite(this.signals, sat);
    const above = pos?.elevation_deg != null && pos.elevation_deg >= 0;
    const trackActive = !!(this.tracking?.active && bound && this.tracking.signal_id === bound.id);
    const trackEnabled = trackActive || (this.connected && this.started && above && !!bound);
    const reason = this.trackingFeature && !trackEnabled ? this._trackReason(sat, bound, above) : null;

    return html`
      <div style="display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;margin-top:10px">
        <div class="readout"><span class="label">Az</span><span class="value mono">${az}</span></div>
        <div class="readout"><span class="label">El</span><span class="value mono">${el}</span></div>
        <div class="readout"><span class="label">Range</span><span class="value mono">${range}</span></div>
        <div class="readout"><span class="label">Doppler</span><span class="value mono">${doppler}</span></div>
        <span class="dim mono" style="font-size:0.8em">${tleAge}</span>
        <span class="spacer"></span>
        ${this.trackingFeature && !trackActive
          ? html`<label class="dim mono" style="font-size:0.8em" title="Also start the matching AX.25 decoder (ISS APRS, GRBAlpha 9600) and tag frames with this pass">
              <input type="checkbox" .checked=${this._decode}
                @change=${(e) => { this._decode = e.target.checked; this.requestUpdate(); }} /> decode
            </label>`
          : ""}
        ${this.trackingFeature
          ? html`<button
              title="Track (Doppler-follow)"
              ?disabled=${!trackEnabled}
              style=${trackActive ? "color:var(--accent);border-color:var(--accent)" : ""}
              @click=${() => this._toggleTrack(bound, trackActive)}
            >
              [ ⦿ TRACK ]
            </button>`
          : ""}
      </div>
      ${reason
        ? html`<p class="dim mono" style="font-size:0.8em;margin:4px 0 0">
            ${reason.text}${reason.bind
              ? html`<button style="margin-left:6px" @click=${() => this._doBind(sat)}>BIND</button>`
              : ""}
          </p>`
        : ""}
    `;
  }
}

customElements.define("sky-view", SkyView);
