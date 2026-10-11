import { LitElement, html } from "../vendor/lit.js";
import { fetchJson } from "../api.js";
import "./audio-player.js";

const fmt = (n, digits = 2) => Number.isFinite(n) ? n.toFixed(digits) : "—";

/** One view for recorded and explicitly started live, selected-channel inspection. */
export class ScopePanel extends LitElement {
  static properties = { mode: {type: String}, active: {type: Boolean},
    // Embedded (the ⓘ panel's Instruments): always open, no disclosure.
    embedded: {type: Boolean}, expanded: {state: true},
    frozen: {state: true}, stream: {state: true}, windowS: {state: true}, data: {state: true},
    error: {state: true}, trigger: {state: true}, level: {state: true},
    cursorA: {state: true}, cursorB: {state: true}, tracking: {state: true},
    correcting: {state: true}, maxOffset: {state: true} };
  createRenderRoot() { return this; }
  constructor() {
    super(); this.mode = "review"; this.active = false; this.embedded = false; this.expanded = false;
    this.frozen = false; this.stream = "audio"; this.windowS = 0.1; this.data = null;
    this.error = ""; this.trigger = "none"; this.level = 0; this.cursorA = 25;
    this.cursorB = 75; this.tracking = false; this.correcting = false; this.maxOffset = 500;
    this._loading = false; this._revision = 0;
  }
  connectedCallback() { super.connectedCallback(); this._timer = setInterval(() => this._refresh(), 250); }
  disconnectedCallback() { clearInterval(this._timer); this._revision++; super.disconnectedCallback(); }
  willUpdate(changed) {
    if (changed.has("embedded") && this.embedded) this.expanded = true;
  }
  updated(changed) {
    if (changed.has("active") || changed.has("mode")) {
      this._revision++; this.data = null; this.frozen = false;
      this.tracking = false; this.correcting = false;
    }
  }
  async _refresh() {
    if (!this.active || !this.expanded || this.frozen || this._loading) return;
    const revision = this._revision;
    this._loading = true;
    try {
      const data = await fetchJson(`/api/inspection/${this.mode}?window_s=${this.windowS}&points=512`);
      if (this.isConnected && revision === this._revision && !this.frozen) {
        this.data = data;
        this.tracking = Boolean(data.drift?.tracking);
        this.correcting = Boolean(data.drift?.correction_enabled);
        this.error = "";
      }
    } catch (e) { if (revision === this._revision) { this.error = e.message; this.data = null; } }
    finally { this._loading = false; }
  }
  async _configure() {
    try {
      await fetchJson(`/api/inspection/${this.mode}/configure`, {method: "POST", body: {
        stream: this.stream === "audio_spectrum" ? "audio" : this.stream,
        trigger_mode: this.trigger, trigger_level: this.trigger === "none" ? null : Number(this.level),
      }});
      this.frozen = false; this._refresh(); this.error = "";
    } catch (e) { this.error = e.message; }
  }
  async _drift(enabled) {
    try {
      const status = await fetchJson(`/api/inspection/${this.mode}/drift`, {method: "POST", body: {
        enabled, max_offset_hz: Number(this.maxOffset), kind: "carrier",
      }});
      this.tracking = Boolean(status.tracking);
      this.correcting = Boolean(status.correction_enabled);
      this.error = ""; this.frozen = false; this._refresh();
    } catch (e) { this.error = e.message; }
  }
  /** Trace geometry and cursor readouts shared by the disclosure layout and
   * the embedded bench; null until samples arrive. */
  _traceModel() {
    const spectrum = this.stream === "audio_spectrum";
    const scope = this.data?.streams?.[this.stream];
    const values = spectrum ? this.data?.audio_spectrum?.power_dbfs : scope?.values;
    if (!values?.length) return null;
    const mins = spectrum ? values : scope.min_values ?? values;
    const maxs = spectrum ? values : scope.max_values ?? values;
    const lo = spectrum ? Math.min(-100, ...values) : Math.min(0, ...mins);
    const hi = spectrum ? Math.max(0, ...values) : Math.max(0.01, ...maxs);
    const range = Math.max(0.001, hi - lo);
    const y = v => 174 - (v - lo) / range * 160;
    const path = values.map((v, i) => `${i ? "L" : "M"}${i / Math.max(1, values.length - 1) * 800},${y(v)}`).join(" ");
    const peaks = spectrum ? "" : mins.map((v, i) => `M${i / Math.max(1, values.length - 1) * 800},${y(v)}L${i / Math.max(1, values.length - 1) * 800},${y(maxs[i])}`).join(" ");
    const xs = spectrum ? this.data.audio_spectrum.frequency_hz : scope.time_s;
    const first = xs?.[0] ?? 0, last = xs?.at(-1) ?? 0;
    const delta = Math.abs(this.cursorB - this.cursorA) / 100 * (last - first);
    const plottedResolution = Math.abs(last - first) / Math.max(1, (xs?.length ?? 1) - 1);
    const timeDigits = plottedResolution * 1000 >= 1 ? 2 : 3;
    const frequencyDigits = plottedResolution >= 100 ? 0 : 1;
    const svg = html`<svg class="scope-plot" viewBox="0 0 800 190" preserveAspectRatio="none" role="img" aria-label=${`${this.stream} oscilloscope trace`} style="display:block;width:100%;height:190px;background:var(--bg,#111);border:1px solid var(--border,#555)">
      <path d=${`M0,${y(0)}H800`} stroke="currentColor" opacity="0.25" vector-effect="non-scaling-stroke"></path>
      <path d=${peaks} fill="none" stroke="var(--accent,#60d4b1)" opacity="0.4" stroke-width="1" vector-effect="non-scaling-stroke"></path>
      <path d=${path} fill="none" stroke="var(--accent,#60d4b1)" stroke-width="1" vector-effect="non-scaling-stroke"></path>
      <path d=${`M${this.cursorA * 8},0V190 M${this.cursorB * 8},0V190`} stroke="#eab866" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"></path>
    </svg>`;
    return {
      spectrum, scope, svg,
      axis: `${fmt(first, spectrum ? 0 : 4)}–${fmt(last, spectrum ? 0 : 4)} ${spectrum ? "Hz" : "s (source clock)"} · amplitude ${fmt(lo)} to ${fmt(hi)} ${spectrum ? "dBFS" : "normalized"}`,
      readout: spectrum
        ? [["Δf", `≈${fmt(delta, frequencyDigits)} Hz`], ["Bin spacing", `≈${fmt(plottedResolution, frequencyDigits)} Hz`]]
        : [["Δt", `≈${fmt(delta * 1000, timeDigits)} ms`], ["Plotted resolution", `≈${fmt(plottedResolution * 1000, timeDigits)} ms`], ["1/Δt", `≈${delta > 0 ? fmt(1 / delta, 0) : "—"} Hz`]],
    };
  }
  _cursors() {
    return html`<label>Cursor A <input aria-label="Scope cursor A" type="range" min="0" max="100" step="0.1" .value=${String(this.cursorA)} @input=${e => {this.cursorA = Number(e.target.value); this.frozen = true;}}></label>
      <label>Cursor B <input aria-label="Scope cursor B" type="range" min="0" max="100" step="0.1" .value=${String(this.cursorB)} @input=${e => {this.cursorB = Number(e.target.value); this.frozen = true;}}></label>`;
  }
  _plot() {
    const t = this._traceModel();
    if (!t) return html`<p class="dim">${this.active ? "Waiting for selected-channel samples…" : "Start playback or live inspection to collect samples."}</p>`;
    const { scope } = t;
    return html`${t.svg}<div class="mono dim">${t.axis}</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap">
      ${this._cursors()}
      <span class="mono">${t.readout.map(([k, v]) => `${k} ${v}`).join(" · ")}</span>
    </div>
    ${scope ? html`<p class="dim mono">${fmt(scope.sample_rate_hz, 0)} samples/s · generation ${scope.generation} · gaps ${scope.gap_count} · trigger ${scope.trigger_status ?? "free running"}</p>` : ""}`;
  }
  _driftLabel(drift) {
    if (!drift) return "Waiting for a selected channel.";
    if (!drift.tracking) return "Carrier tracking is off.";
    const confidence = `${Math.round((drift.confidence ?? 0) * 100)}% confidence`;
    const reason = {
      below_signal_gate: "signal below level gate",
      low_coherence: "carrier is not stable enough",
      offset_limit: "offset exceeds the configured limit",
      unsupported_signal: "signal type is unsupported",
      insufficient_samples: "collecting more samples",
      not_configured: "waiting for carrier tracking",
    }[drift.reason];
    if (drift.accepted && drift.smoothed_hz != null) {
      const correction = drift.correction_enabled
        ? `Correction on · ${fmt(drift.correction_hz, 1)} Hz`
        : "Measurement only";
      return `${correction} · carrier offset ${fmt(drift.smoothed_hz, 1)} Hz · ${confidence}`;
    }
    if (drift.correction_enabled && drift.held_last_valid && drift.smoothed_hz != null) {
      return `Holding last valid correction · ${fmt(drift.correction_hz, 1)} Hz · ${reason ?? "signal gate closed"} · ${confidence}`;
    }
    const measured = drift.offset_hz == null ? "No carrier estimate" : `Offset ${fmt(drift.offset_hz, 1)} Hz`;
    return `${drift.correction_enabled ? "Correction waiting" : "Measurement only"} · ${measured} · ${reason ?? drift.reason ?? "waiting for a stable carrier"} · ${confidence}`;
  }
  _toolbar() {
    return html`<label>View <select aria-label="Scope view" .value=${this.stream} @change=${e => {this.stream = e.target.value; this._configure();}}>
          <option value="audio">Audio waveform</option><option value="iq_envelope">Signal envelope</option><option value="iq_i">I oscilloscope</option><option value="iq_q">Q oscilloscope</option><option value="audio_spectrum">Audio spectrum</option>
        </select></label>
        <label>Time window <select aria-label="Scope time window" .value=${String(this.windowS)} @change=${e => {this.windowS = Number(e.target.value);this.frozen = false;this._refresh();}}>
          ${[0.001,0.005,0.02,0.1,0.5,1,5].map(s => html`<option value=${String(s)}>${s < 1 ? `${s * 1000} ms` : `${s} s`}</option>`)}
        </select></label>
        <label>Trigger <select aria-label="Scope trigger" .value=${this.trigger} @change=${e => {this.trigger = e.target.value;this._configure();}}><option value="none">Free running</option><option value="rising">Rising edge</option><option value="falling">Falling edge</option></select></label>
        <label>Level <input aria-label="Scope trigger level" type="number" step="0.01" .value=${String(this.level)} @change=${e => {this.level = Number(e.target.value);this._configure();}} style="width:80px"></label>
        <button ?disabled=${!this.data} @click=${() => {this.frozen = !this.frozen;this._refresh();}}>${this.frozen ? "Resume scope" : "Freeze scope"}</button>`;
  }
  _driftControls(hint) {
    const drift = this.data?.drift;
    return html`<p class="dim">${hint}</p>
        <button @click=${() => this._drift(false)}>Measure carrier offset</button>
        <label>Maximum correction Hz <input aria-label="Maximum carrier correction" type="number" min="1" max="5000" .value=${String(this.maxOffset)} @change=${e => {this.maxOffset = Number(e.target.value); if(this.tracking)this._drift(this.correcting);}}></label>
        <label><input type="checkbox" .checked=${this.correcting} @change=${e => this._drift(e.target.checked)}> Apply correction (carrier signals only)</label>
        <p class="mono" aria-live="polite">${this._driftLabel(drift)}</p>`;
  }
  /** Embedded bench: trace with its toolbar and cursors on the left; numeric
   * readouts, audio and carrier drift as labelled cards on the right. */
  _bench() {
    const t = this._traceModel();
    const scope = t?.scope;
    const rows = [
      ...(t?.readout ?? []),
      ...(scope ? [["Sample rate", `${fmt(scope.sample_rate_hz, 0)} samples/s`], ["Gaps", String(scope.gap_count)],
        ["Generation", String(scope.generation)], ["Trigger", scope.trigger_status ?? "free running"]] : []),
    ];
    return html`<div class="scope-panel scope-panel--embedded scope-bench">
      <div class="scope-bench-main">
        <div class="scope-toolbar">${this._toolbar()}</div>
        ${this.error ? html`<p role="alert">${this.error}</p>` : ""}
        ${t ? t.svg : html`<div class="scope-plot-empty" role="img" aria-label="Empty scope trace">${this.active ? "Waiting for selected-channel samples…" : "Press ▶ Start to draw the trace."}</div>`}
        <div class="scope-axis mono dim">${t ? t.axis : "Amplitudes are uncalibrated baseband samples, not the RF carrier waveform."}</div>
        <div class="scope-cursors">${this._cursors()}</div>
      </div>
      <aside class="scope-bench-side">
        <section class="scope-card">
          <h3>Readout</h3>
          ${rows.length ? html`<dl class="scope-readout">${rows.map(([k, v]) => html`<dt>${k}</dt><dd class="mono">${v}</dd>`)}</dl>`
            : html`<p class="dim">Cursor and sample readouts appear with the trace.</p>`}
        </section>
        ${this.mode === "live" ? html`<section class="scope-card"><h3>Audio</h3>
          <audio-player .active=${this.active} endpoint="/ws/live-audio"></audio-player></section>` : ""}
        <section class="scope-card scope-drift"><h3>Carrier drift</h3>
          ${this._driftControls("For a selected unmodulated carrier. Correction affects only this software channel.")}
        </section>
      </aside>
    </div>`;
  }
  render() {
    if (this.embedded) return this._bench();
    const body = html`<p class="dim">Selected-channel audio waveform, IQ envelope and oscilloscope traces. Amplitudes are uncalibrated; these are baseband samples, not the RF carrier waveform.</p>
      ${this.error ? html`<p role="alert">${this.error}</p>` : ""}
      <div class="control-row" style="flex-wrap:wrap">${this._toolbar()}</div>
      ${this._plot()}
      <details><summary>Carrier drift tracking</summary>
        ${this._driftControls("For an explicitly selected unmodulated carrier. If the signal gate closes, the last valid correction is held until tracking resets. Correction affects only this software channel; saved IQ stays unchanged.")}
      </details>
      ${this.mode === "live" ? html`<audio-player .active=${this.active} endpoint="/ws/live-audio"></audio-player>` : ""}`;
    return html`<details class="scope-panel" @toggle=${e => { this.expanded = e.target.open; if (this.expanded) this._refresh(); }}>
      <summary>Scope · ${this.mode === "review" ? "recording inspection" : "live inspection"}</summary>
      ${body}
    </details>`;
  }
}
customElements.define("scope-panel", ScopePanel);
