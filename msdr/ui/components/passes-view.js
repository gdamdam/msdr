import { LitElement, html } from "../vendor/lit.js";
import { compassPoint, mhz } from "../format.js";
import { passesIcsUrl } from "../api.js";
import { passTime, passDate } from "../satellite-format.js";
import "./led-dot.js";
import "./failure-note.js";

/** Seconds -> "T-HH:MM" countdown, or null once the target has passed. */
function countdown(seconds) {
  if (seconds === null || seconds === undefined || seconds < 0) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `T-${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export class PassesView extends LitElement {
  static properties = {
    selectedPass: { type: Object },
    settings: { type: Object },
    busy: { type: Boolean },
    satelliteDefaults: { type: Array },
    visibleOnly: { type: Boolean },
    passes: { type: Object }, // PassesResult: { passes, generated_at, source, timezone, error }
    hasLocation: { type: Boolean }, // !!settings.location -- gates the "set your location" empty state
    _now: { state: true },
    failure: { type: Object },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.passes = null;
    this.hasLocation = true;
    this._now = Date.now();
    this.failure = null;
    this._timer = null;
  }

  _openSetup() {
    this.dispatchEvent(new CustomEvent("setup"));
  }

  connectedCallback() {
    super.connectedCallback();
    // Ticks the header countdown and the in-progress-pass LED once a second.
    this._timer = setInterval(() => (this._now = Date.now()), 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  _click(pass, open = false) {
    this.dispatchEvent(new CustomEvent("select-pass", {
      detail: { satellite: pass.satellite, rise: pass.rise, set: pass.set, peak: pass.peak, open },
    }));
  }

  _tune(pass, event) {
    event.stopPropagation();
    this.dispatchEvent(new CustomEvent("tune-satellite", {
      detail: { satellite: pass.satellite, downlink: pass.downlink ?? null },
    }));
  }

  _refresh() {
    this.dispatchEvent(new CustomEvent("refresh-passes"));
  }

  _nextRiseCountdown(list) {
    const now = this._now;
    let best = null;
    for (const p of list) {
      const rise = Date.parse(p.rise);
      if (Number.isNaN(rise) || rise <= now) continue;
      if (best === null || rise < best) best = rise;
    }
    if (best === null) return null;
    return countdown(Math.round((best - now) / 1000));
  }

  _changeSettings(patch) {
    this.dispatchEvent(new CustomEvent("passes-settings", { detail: patch }));
  }

  _satellites() {
    return ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"];
  }

  _chosenSatellites() {
    return this.settings?.passes_satellites?.length ? this.settings.passes_satellites
      : this.satelliteDefaults?.length ? this.satelliteDefaults : this._satellites();
  }

  _toggleSatellite(sat) {
    const selected = new Set(this._chosenSatellites());
    if (selected.has(sat)) selected.delete(sat); else selected.add(sat);
    // Empty settings means automatic selection, not "none". Keep at least one.
    if (selected.size) this._changeSettings({ passes_satellites: [...selected] });
  }

  _controls() {
    const chosen = this._chosenSatellites();
    return html`<div class="satellite-controls">
      <label>Days <select aria-label="Prediction days" ?disabled=${this.busy}
        .value=${String(this.settings?.passes_days ?? 2)}
        @change=${ev => this._changeSettings({ passes_days: Number(ev.target.value) })}>
        ${Array.from({length: 14}, (_, i) => i+1).map(day => html`<option value=${String(day)} ?selected=${day === (this.settings?.passes_days ?? 2)}>${day}</option>`)}
      </select></label>
      <label><input type="checkbox" ?disabled=${this.busy} .checked=${!!this.visibleOnly}
        @change=${ev => this._changeSettings({ passes_visible_only: ev.target.checked })}> Optically visible only</label>
      <div class="satellite-choices" role="group" aria-label="Satellites to predict">
        ${this._satellites().map(sat => html`<button aria-pressed=${chosen.includes(sat) ? "true" : "false"}
          ?disabled=${this.busy || (chosen.length === 1 && chosen.includes(sat))}
          @click=${() => this._toggleSatellite(sat)}>${sat}</button>`)}
        <button ?disabled=${this.busy} @click=${() => this._changeSettings({ passes_satellites: this._satellites() })}>All</button>
      </div>
      ${this.settings?.passes_extra_satellites?.length ? html`<span class="dim">Also tracking: ${this.settings.passes_extra_satellites.map(s => s.label).join(", ")}</span>` : ""}
      ${this.busy ? html`<span class="dim" role="status">Updating passes…</span>` : ""}
    </div>`;
  }

  render() {
    const result = this.passes ?? {};
    const list = [...(result.passes ?? [])].sort((a, b) => Date.parse(a.rise) - Date.parse(b.rise));
    const error = result.error;
    const engine = result.engine;
    const cd = list.length ? this._nextRiseCountdown(list) : null;

    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Passes</h2>
          ${engine ? html`<span class="mono dim">${engine}</span>` : ""}
          ${cd ? html`<span class="mono dim">Next ${cd}</span>` : ""}
          <a
            href=${passesIcsUrl()}
            title="Download predicted passes as an iCalendar file"
            style="font-size:0.85em"
            >↓ ICS</a
          >
          <button title="Refresh TLEs" @click=${() => this._refresh()}>↻ TLE</button>
        </div>
        ${this._controls()}
        <p class="satellite-help">${this.visibleOnly ? "Optically visible passes" : "All radio passes"} · ${result.timezone ?? "local time"}. Select a pass to inspect its charts.</p>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        ${!this.hasLocation
          ? html`<p class="empty">
              Set your location in SETUP to predict passes
              <button style="margin-left:6px" @click=${() => this._openSetup()}>SETUP</button>
            </p>`
          : html`
              ${error ? html`<p class="empty">${error}</p>` : ""}
              ${list.length
                ? html`<div class="list satellite-pass-list" style="margin-top:8px">${list.map((p) => this._row(p))}</div>`
                : error
                  ? ""
                  : html`<p class="empty">${this.visibleOnly ? "No optically visible passes in this window. Turn off Optically visible only to see radio passes." : "No passes match this window and elevation. Adjust Passes in Setup."}</p>`}
            `}
      </div>
    `;
  }

  // Two-line row: the column is narrow (a third of the grid) and this many
  // mono fields on one line would wrap mid-word rather than overflow.
  _row(pass) {
    const now = this._now;
    const rise = Date.parse(pass.rise);
    const set = Date.parse(pass.set);
    const active = !Number.isNaN(rise) && !Number.isNaN(set) && now >= rise && now <= set;
    const elev = pass.max_elevation_deg != null ? `${Math.round(pass.max_elevation_deg)}°` : "--";
    const az = `${compassPoint(pass.rise_azimuth_deg)}▸${compassPoint(pass.peak_azimuth_deg)}▸${compassPoint(pass.set_azimuth_deg)}`;
    const downlink = pass.downlink;
    const selected = this.selectedPass?.satellite === pass.satellite && this.selectedPass?.rise === pass.rise;
    const time = iso => passTime(iso, this.passes?.timezone);
    return html`
      <div class="row satellite-pass ${selected ? "selected" : ""}" role="button" tabindex="0"
        aria-label=${`${pass.satellite} ${passDate(pass.rise, this.passes?.timezone)} ${time(pass.rise)} pass charts`}
        aria-pressed=${selected ? "true" : "false"}
        @click=${() => this._click(pass)} @dblclick=${() => this._click(pass, true)}
        @keydown=${ev => { if (ev.target === ev.currentTarget && ["Enter", " "].includes(ev.key)) { ev.preventDefault(); this._click(pass, true); } }}>
        <div class="satellite-pass-date dim">${passDate(pass.rise, this.passes?.timezone)}</div>
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot color="var(--accent)" .lit=${active}></led-dot>
          <strong class="satellite-name">${pass.satellite}</strong>
          ${pass.visible ? html`<span title="Sunlit in dark sky during part of this pass">☼ Visible</span>` : pass.visible == null ? html`<span class="dim" title="Refresh TLEs with visible passes only enabled to fetch visibility data">Visibility unknown</span>` : ""}
          <span class="spacer"></span>
          <span class="satellite-quality" data-quality=${(pass.geometry ?? "").toLowerCase()} title="Pass quality based on maximum elevation">${pass.geometry ?? ""}</span>
        </div>
        <div class="mono dim" style="display:flex;align-items:center;gap:8px;padding-left:16px">
          <span>${time(pass.rise)} ▸ ${time(pass.peak)} ${elev} ▸ ${time(pass.set)}</span>
          <span class="spacer"></span>
          <span>${az}</span>
        </div>
        <div class="mono dim" style="padding-left:16px">
          ${downlink ? html`<span>${mhz(downlink.frequency_hz)} ${downlink.mode}</span>` : ""}
          <button class="satellite-tune" @click=${ev => this._tune(pass, ev)} @dblclick=${ev => ev.stopPropagation()}>Tune</button>
        </div>
      </div>
    `;
  }
}

customElements.define("passes-view", PassesView);
