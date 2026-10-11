import { LitElement, html } from "../vendor/lit.js";
import { fetchJson } from "../api.js";
import "./native-audio-dsp.js";
import "./scope-panel.js";

const post = (path, body) => fetchJson(path, {method: "POST", body});
// msdr/engine/workbench.py LiveInspectionConfig.demodulator.
const DEMODULATORS = ["NFM", "AM", "WFM", "USB", "LSB", "CW"];
const DEFAULT_WIDTH = { NFM: 12500, AM: 10000, WFM: 200000, USB: 2700, LSB: 2700, CW: 500 };
const mhz = (hz) => (hz / 1e6).toFixed(6).replace(/0{1,3}$/, "");

/** Suggested tuning from the signal detail, coerced to what live inspection
 * accepts: an unknown or missing mode falls back to NFM, and the width to that
 * mode's default when it is missing or outside the 100 Hz–250 kHz limits. */
export function liveTuning(demodulator, bandwidthHz) {
  const mode = DEMODULATORS.includes(String(demodulator ?? "").toUpperCase())
    ? String(demodulator).toUpperCase() : "NFM";
  const width = Number(bandwidthHz);
  return { demodulator: mode, bandwidth: width >= 100 && width <= 250000 ? Math.round(width) : DEFAULT_WIDTH[mode] };
}

/** Where live inspection stands relative to the frequency on screen:
 * "off", "here" (within 1 Hz) or "elsewhere" (a watch's Follow, or an
 * inspection started before a retune, owns the software receiver). */
export function liveRelation(live, frequencyHz) {
  if (!live) return "off";
  if (live.frequency_hz == null || Math.abs(live.frequency_hz - frequencyHz) <= 1) return "here";
  return "elsewhere";
}

/** Scope row under the waterfall: one software receiver on the receiver's
 * current frequency, with trace, audio, DSP and carrier drift. "Live
 * inspection" in the API; "Scope" to the operator. Uses the same live
 * inspection API as the channel watch, so only one runs at a time. */
export class SignalInstruments extends LitElement {
  static properties = {
    frequencyHz: { type: Number },
    suggestedDemodulator: { type: String },
    suggestedBandwidth: { type: Number },
    // The receiver's own mode/width: the fallback when no signal suggests one.
    receiverDemodulator: { type: String },
    receiverBandwidth: { type: Number },
    replayActive: { type: Boolean },
    _live: { state: true }, _busy: { state: true }, _error: { state: true },
    _demodulator: { state: true }, _bandwidth: { state: true }, _help: { state: true }, _open: { state: true },
  };
  createRenderRoot() { return this; }
  constructor() {
    super();
    this.frequencyHz = 0; this.replayActive = false; this.receiverDemodulator = ""; this.receiverBandwidth = 0;
    this._live = null; this._busy = false; this._error = "";
    this._demodulator = "NFM"; this._bandwidth = 12500; this._tuningFor = null; this._help = false;
    // Collapsed by default; the bench (and its 250 ms scope polling) renders only when open.
    this._open = false;
  }
  connectedCallback() { super.connectedCallback(); this._refresh(); this._timer = setInterval(() => this._refresh(), 1000); }
  disconnectedCallback() { clearInterval(this._timer); super.disconnectedCallback(); }
  willUpdate(changed) {
    // Follow the suggestion (it arrives after the frequency, once the signal
    // detail loads) until the operator edits mode or width for this frequency.
    if (changed.has("frequencyHz") && this._tuningFor !== this.frequencyHz) {
      this._tuningFor = this.frequencyHz; this._edited = false;
    }
    if (!this._edited && (changed.has("frequencyHz") || changed.has("suggestedDemodulator") || changed.has("suggestedBandwidth")
        || changed.has("receiverDemodulator") || changed.has("receiverBandwidth"))) {
      const t = liveTuning(this.suggestedDemodulator || this.receiverDemodulator, this.suggestedBandwidth || this.receiverBandwidth);
      this._demodulator = t.demodulator; this._bandwidth = t.bandwidth;
    }
  }
  async _refresh() {
    if (this._fetching) return;
    this._fetching = true;
    try {
      const data = await fetchJson("/api/workbench");
      if (this.isConnected) { this._live = data.live_receiver ?? null; if (this._pollFailed) { this._error = ""; this._pollFailed = false; } }
    } catch (e) { this._error = e.message; this._pollFailed = true; }
    finally { this._fetching = false; }
  }
  async _run(label, action) {
    this._busy = true; this._error = ""; this._pollFailed = false;
    try { await action(); await this._refresh(); }
    catch (e) { this._error = `${label} failed: ${e.message}`; }
    finally { this._busy = false; }
  }
  /** Open the bench (Channel watch's "Open Live inspection"). */
  expand() { this._open = true; }

  _start() {
    return this._run("Live inspection", () => post("/api/inspection/live/start", {
      frequency_hz: this.frequencyHz, demodulator: this._demodulator, bandwidth_hz: Number(this._bandwidth) }));
  }
  _stop() { return this._run("Stopping live inspection", () => post("/api/inspection/live/stop")); }

  _status(relation) {
    const live = this._live;
    if (relation === "here") {
      return html`<span class="instruments-state on">● Live · ${live.demodulator ?? ""}${live.bandwidth_hz ? ` · ${live.bandwidth_hz} Hz` : ""}</span>`;
    }
    if (relation === "elsewhere") {
      return html`<span class="instruments-state elsewhere">● Live on <span class="mono">${mhz(live.frequency_hz)} MHz</span></span>`;
    }
    return html`<span class="instruments-state">Off</span>`;
  }

  _controls(relation) {
    if (this.replayActive) return "";
    return html`<div class="instruments-controls">
      <span class="instruments-field-group" title="Software channel for the scope; the receiver's own mode and bandwidth are unchanged">
      <label class="instruments-field">Channel
        <select aria-label="Scope channel mode" .value=${this._demodulator} ?disabled=${this._busy}
          @change=${(e) => { this._edited = true; this._demodulator = e.target.value; this._bandwidth = DEFAULT_WIDTH[this._demodulator]; }}>
          ${DEMODULATORS.map((m) => html`<option value=${m} ?selected=${m === this._demodulator}>${m}</option>`)}
        </select></label>
      <label class="instruments-field">
        <input aria-label="Scope channel width Hz" type="number" min="100" max="250000" step="100" ?disabled=${this._busy}
          .value=${String(this._bandwidth)} @change=${(e) => { this._edited = true; this._bandwidth = Number(e.target.value); }}><small>Hz</small></label>
      </span>
      ${relation === "here"
        ? html`<button ?disabled=${this._busy} @click=${() => this._start()}>Apply</button>`
        : html`<button class="instruments-start" ?disabled=${this._busy || !this.frequencyHz} @click=${() => this._start()}>
            ${this._busy ? "Starting…" : relation === "elsewhere" ? "▶ Move here" : "▶ Start"}</button>`}
      ${relation !== "off" ? html`<button class="instruments-stop" ?disabled=${this._busy} @click=${() => this._stop()}>Stop</button>` : ""}
    </div>`;
  }

  _hint(relation) {
    if (this.replayActive) return "Unavailable while reviewing a recording; Review has its own Scope.";
    if (relation === "elsewhere") return "The trace shows the channel that is live now, not the receiver frequency. Move here to follow the receiver.";
    return "A software channel on the receiver frequency; the radio is not retuned.";
  }

  /** The help <dialog> is rendered only while open (.overlay-backdrop's
   * display would otherwise show a closed dialog); showModal() gives the
   * backdrop, Esc and focus trap. */
  updated() {
    const dialog = this.querySelector("dialog.instruments-help");
    if (dialog && !dialog.open) { this._returnFocus = document.activeElement; dialog.showModal(); }
  }

  _renderHelp() {
    const close = () => {
      this._help = false;
      if (this._returnFocus?.isConnected) this._returnFocus.focus();
    };
    const row = (term, text) => html`<dt>${term}</dt><dd>${text}</dd>`;
    return html`<dialog class="overlay-backdrop instruments-help" aria-labelledby="instruments-help-title"
      style="margin:0;padding:0;border:0;width:100vw;height:100vh;max-width:none;max-height:none;color:inherit"
      @cancel=${(e) => { e.preventDefault(); close(); }} @click=${close}>
      <div class="panel instruments-help-panel" role="document" @click=${(e) => e.stopPropagation()}>
        <header class="instruments-help-head">
          <div>
            <h2 id="instruments-help-title">Scope</h2>
            <p class="dim">Look at one signal closely: hear it, see its shape, measure its timing.</p>
          </div>
          <button type="button" aria-label="close help" @click=${close}>&times;</button>
        </header>
        <div class="instruments-help-grid">
          <section class="instruments-help-card instruments-help-start">
            <h3>Get started</h3>
            <ol>
              <li>Check <strong>Channel</strong> mode and width. They follow the receiver, or the saved signal's suggested tuning.</li>
              <li>Press <strong>▶ Start</strong>. A software channel opens on the receiver frequency; the radio is not retuned.</li>
              <li>Press <strong>Play audio</strong> to listen, then pick a scope <strong>View</strong>.</li>
              <li>Press <strong>Stop</strong> when done. It also stops by itself if the receiver span changes.</li>
            </ol>
          </section>
          <section class="instruments-help-card">
            <h3>Scope views</h3>
            <dl>
              ${row("Audio waveform", "The demodulated sound. Voice looks irregular; data and tones look regular.")}
              ${row("Signal envelope", "Signal strength over time. Shows bursts, keying and the start and end of data frames.")}
              ${row("I / Q", "The raw baseband components. Shows how the signal is modulated.")}
              ${row("Audio spectrum", "Which tones are in the audio, such as sub-audible CTCSS or data tones.")}
            </dl>
          </section>
          <section class="instruments-help-card">
            <h3>Controls</h3>
            <dl>
              ${row("Time window", "How much time fits across the screen: 1 ms for detail, 5 s for an overview.")}
              ${row("Trigger · Level", "Starts each trace where the signal crosses Level, so repeating shapes stand still.")}
              ${row("Freeze", "Holds the trace so you can study it.")}
              ${row("Cursors A · B", "Mark two points. Δt is the time between them; 1/Δt turns one cycle or one symbol into Hz or baud.")}
            </dl>
          </section>
          <section class="instruments-help-card">
            <h3>Readout</h3>
            <dl>
              ${row("Plotted resolution", "The smallest time step on screen; cursor values are only this precise.")}
              ${row("Sample rate", "Samples per second reaching the scope.")}
              ${row("Gaps", "Dropped samples. Above 0, the trace has a break and timing across it is unreliable.")}
            </dl>
          </section>
          <section class="instruments-help-card">
            <h3>Carrier drift</h3>
            <p>For a steady, unmodulated carrier such as a beacon or a held CW key. <strong>Measure carrier offset</strong> shows how far it sits from centre; <strong>Apply correction</strong> follows it in this software channel only.</p>
          </section>
          <section class="instruments-help-card">
            <h3>Audio processing</h3>
            <p>AGC, noise reduction, notch and filters for msdr's own listening. Decoders and recordings are not affected.</p>
          </section>
        </div>
        <p class="dim instruments-help-foot">Amplitudes are uncalibrated baseband samples, not the RF carrier and not dBm.</p>
      </div>
    </dialog>`;
  }

  render() {
    const live = this._live;
    const relation = liveRelation(live, this.frequencyHz);
    return html`<section class="panel live-inspection${this._open ? " open" : ""}" aria-labelledby="live-inspection-title">
      <header class="live-inspection-bar">
        <button class="live-inspection-toggle" aria-expanded=${this._open ? "true" : "false"} aria-controls="live-inspection-body"
          @click=${() => { this._open = !this._open; }}>
          <span class="live-inspection-caret" aria-hidden="true">${this._open ? "▾" : "▸"}</span>
          <h2 id="live-inspection-title">Scope</h2>
          <span class="mono live-inspection-freq">${this.frequencyHz ? `${mhz(this.frequencyHz)} MHz` : "—"}</span>
          ${this._status(relation)}
        </button>
        ${this._open ? html`${this._controls(relation)}
          <button class="instruments-help-btn" aria-haspopup="dialog" title="What these instruments do and how to use them"
            @click=${() => { this._help = true; }}>? Help</button>` : ""}
      </header>
      ${this._open ? html`<div id="live-inspection-body">
        <p class="dim instruments-hint">${this._hint(relation)}</p>
        ${this._error ? html`<p role="alert" class="instruments-error">${this._error}</p>` : ""}
        <scope-panel mode="live" embedded .active=${!!live}></scope-panel>
        ${live ? html`<details class="live-inspection-dsp"><summary>Audio processing</summary>
          <native-audio-dsp mode="live" .settings=${live.native_audio} .demodulator=${live.demodulator}></native-audio-dsp>
        </details>` : ""}
      </div>` : ""}
      ${this._help ? this._renderHelp() : ""}
    </section>`;
  }
}
customElements.define("signal-instruments", SignalInstruments);
