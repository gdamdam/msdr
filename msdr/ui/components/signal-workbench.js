import { LitElement, html } from "../vendor/lit.js";
import { fetchJson } from "../api.js";

const post = (path, body) => fetchJson(path, {method: "POST", body});
const BANDWIDTH_PRESETS = { NFM: [8000, 12500, 25000], AM: [5000, 10000],
  USB: [1800, 2700, 3600, 6000], LSB: [1800, 2700, 3600, 6000],
  CW: [250, 500, 1000], WFM: [200000] };
const DEFAULTS = { startDb: 10, stopDb: 6, minActive: 0.3, quiet: 5, maxDuration: 120, eventCap: 20, dwell: 10 };

export const mhz = (hz) => (hz / 1e6).toFixed(6).replace(/0{1,3}$/, "");
const db = (value) => (Number.isFinite(value) ? `${value.toFixed(1)} dB` : "—");
const bwLabel = (bw) => (bw >= 1000 ? `${bw / 1000} kHz` : `${bw} Hz`);

/** Parse the watch list. Returns {hz, error}; never throws, so the UI can
 * show the problem beside the field while the user types. */
export function parseFrequencies(text) {
  const parts = String(text ?? "").split(/[\s,;]+/).filter(Boolean);
  if (!parts.length) return { hz: [], error: "Enter at least one frequency in MHz." };
  const hz = [];
  for (const part of parts) {
    const value = Number(part);
    if (!Number.isFinite(value) || value <= 0) {
      return { hz: [], error: `“${part}” is not a frequency. Enter positive frequencies in MHz, separated by commas.` };
    }
    const rounded = Math.round(value * 1e6);
    if (!hz.includes(rounded)) hz.push(rounded);
  }
  if (hz.length > 64) return { hz: [], error: "Watch at most 64 frequencies." };
  return { hz, error: null };
}

/** Which watched frequencies sit inside the confirmed receiver span. */
export function spanCoverage(hzList, centerHz, sampleRateHz) {
  if (!centerHz || !sampleRateHz) return { known: false, inside: [], outside: [] };
  const lo = centerHz - sampleRateHz / 2, hi = centerHz + sampleRateHz / 2;
  const inside = hzList.filter((hz) => hz >= lo && hz <= hi);
  return { known: true, lo, hi, inside, outside: hzList.filter((hz) => !inside.includes(hz)) };
}

/** Why Start is unavailable, in words the operator can act on; null when it can start. */
export function startBlocker({ receiver, parsed, coverage, cycle, busy }) {
  if (busy) return "Working…";
  if (!receiver?.connected) return "Receiver not connected. Connect SDRconnect, then press Play.";
  if (!receiver?.started) return "Receiver stopped. Press Play in SDRconnect.";
  if (!receiver?.complete) return "Waiting for the receiver to report its settings.";
  if (parsed.error) return parsed.error;
  if (coverage.known && !coverage.inside.length && !cycle) {
    return "No listed frequency is inside the receiver span. Retune, or turn on span cycling.";
  }
  return null;
}

/** Coverage reasons and resolution warnings from msdr/survey/watch.py, in words. */
const COVERAGE_WORDS = {
  outside_span: "Outside receiver span",
  dc_guard: "Too close to the receiver centre (DC spike)",
  stale: "No fresh spectrum",
  awaiting_fresh_frame: "Waiting for fresh spectrum",
};
const RESOLUTION_WORDS = {
  channel_narrower_than_spectrum_bin: "narrower than one spectrum bin",
  overlaps_another_watched_window: "overlaps another watched channel",
};
export function coverageText(row) {
  if (row.covered) {
    const warn = RESOLUTION_WORDS[row.resolution_warning];
    return { ok: true, text: warn ? `Observed · ${warn}` : "Observed" };
  }
  return { ok: false, text: COVERAGE_WORDS[row.reason] ?? (row.reason ? row.reason.replaceAll("_", " ") : "Not observed") };
}

/** Capture trigger state, in plain words (never colour alone). */
export function captureState(trigger) {
  if (!trigger) return { word: "Off", detail: "" };
  const words = {
    disabled: "Off", armed: "Armed — waiting for activity", recording: "Recording",
    waiting_quiet: "Recording — waiting for quiet", capped: "Stopped — event limit reached",
    recording_capped: "Recording — last capture before the limit",
  };
  const word = words[trigger.state] ?? (trigger.enabled ? "Armed — waiting for activity" : "Off");
  return { word, detail: `${trigger.event_count ?? 0} of ${trigger.event_cap ?? "?"} captures` };
}

export class SignalWorkbench extends LitElement {
  static properties = {
    receiver: { type: Object }, // {connected, started, complete} from app state
    recorderArmed: { type: Boolean },
    data: {state: true}, frequencies: {state: true}, priority: {state: true},
    autoRecord: {state: true}, follow: {state: true}, cycle: {state: true},
    startDb: {state: true}, stopDb: {state: true}, minActive: {state: true}, quiet: {state: true},
    maxDuration: {state: true}, eventCap: {state: true}, dwell: {state: true}, bandwidth: {state: true},
    demodulator: {state: true}, liveHz: {state: true}, error: {state: true}, busy: {state: true},
    pending: {state: true}, _open: {state: true}, _help: {state: true},
  };
  createRenderRoot() { return this; }
  constructor() {
    super(); this.data = null; this.frequencies = ""; this.priority = "";
    this.autoRecord = false; this.follow = false; this.cycle = false;
    Object.assign(this, DEFAULTS);
    this.bandwidth = 12500; this.demodulator = "NFM"; this.liveHz = "";
    this.error = ""; this.busy = false; this.pending = ""; this.receiver = null; this.recorderArmed = false;
    this._fetching = false; this._open = false; this._help = false;
  }
  connectedCallback() { super.connectedCallback(); this._refresh(); this._timer = setInterval(() => this._refresh(), 1000); }
  disconnectedCallback() { clearInterval(this._timer); super.disconnectedCallback(); }
  async _refresh() {
    if (this._fetching) return;
    this._fetching = true;
    try {
      const data = await fetchJson("/api/workbench");
      if (!this.isConnected) return;
      // Open the panel when something starts running; never close it on stop.
      const running = !!(data.active || data.live_receiver);
      if (running && !this._wasRunning) this._open = true;
      this._wasRunning = running;
      this.data = data;
      // Clear only a poll failure; an action error ("Start failed") must stay until the next action.
      if (this._pollFailed) { this.error = ""; this._pollFailed = false; }
      if (!this.frequencies && data.tuned_hz) this.frequencies = mhz(data.tuned_hz);
    } catch (e) { this.error = e.message; this._pollFailed = true; }
    finally { this._fetching = false; }
  }
  async _run(label, action) {
    this.busy = true; this.pending = label; this.error = ""; this._pollFailed = false;
    try { await action(); await this._refresh(); }
    catch (e) { this.error = `${label} failed: ${e.message}`; }
    finally { this.busy = false; this.pending = ""; }
  }
  _start() {
    const parsed = parseFrequencies(this.frequencies);
    if (parsed.error) { this.error = parsed.error; this._pollFailed = false; return; }
    const priority = this.priority ? Number(this.priority) : null;
    return this._run(this.data?.active ? "Applying changes" : "Starting watch", () => post("/api/workbench/watch", {
      frequencies_hz: parsed.hz, priority_hz: priority && parsed.hz.includes(priority) ? priority : null,
      threshold_db: Number(this.startDb), stop_threshold_db: Number(this.stopDb),
      min_active_s: Number(this.minActive), quiet_hold_s: Number(this.quiet),
      max_recording_s: Number(this.maxDuration), event_cap: Number(this.eventCap),
      channel_width_hz: Number(this.bandwidth), demodulator: this.demodulator,
      auto_record: this.autoRecord, follow: this.follow, cycle_groups: this.cycle, dwell_s: Number(this.dwell),
    }));
  }
  _stopWatch() { return this._run("Stopping watch", () => post("/api/workbench/watch/stop")); }
  _stopLive() { return this._run("Stopping live inspection", () => post("/api/inspection/live/stop")); }
  _stopAll() { return this._run("Stopping", () => post("/api/workbench/stop")); }
  _disarm() { return this._run("Disarming recorder", () => post("/api/recorder/arm", { on: false })); }
  _startLive(hz) {
    return this._run("Starting live inspection", () => post("/api/inspection/live/start", {
      frequency_hz: hz, demodulator: this.demodulator, bandwidth_hz: Number(this.bandwidth)}));
  }

  _number(label, key, { min, max, step = 1, unit = "" }) {
    const id = `wb-${key}`;
    return html`<label class="wb-field" for=${id}>
      <span>${label}</span>
      <span class="wb-input-unit"><input id=${id} type="number" min=${min} max=${max} step=${step}
        .value=${String(this[key])} @change=${(e) => { this[key] = Number(e.target.value); }}>${unit ? html`<small>${unit}</small>` : ""}</span>
      <small class="dim">default ${DEFAULTS[key]}${unit ? ` ${unit}` : ""} · ${min}–${max}</small>
    </label>`;
  }

  _summaryText(d) {
    const parts = [];
    if (d?.active) parts.push(`watching ${d.watch?.measurements?.length ?? 0}`);
    const cap = captureState(d?.trigger);
    if (d?.active && d?.trigger?.enabled) parts.push(`capture ${cap.word.split(" — ")[0].toLowerCase()}`);
    if (d?.live_receiver) parts.push("live inspection on");
    return parts.length ? parts.join(" · ") : "idle";
  }

  _renderSetup(parsed, coverage, blocker, active) {
    const options = parsed.hz;
    return html`<section class="wb-step" aria-labelledby="wb-what">
      <h4 id="wb-what">What to watch</h4>
      <div class="wb-grid">
        <label class="wb-field wb-wide" for="wb-freqs">
          <span>Watch frequencies MHz</span>
          <input id="wb-freqs" aria-describedby="wb-freqs-help" .value=${this.frequencies} placeholder="145.500, 145.600"
            @input=${(e) => { this.frequencies = e.target.value; }}>
          <small id="wb-freqs-help" class=${parsed.error && this.frequencies ? "wb-problem" : "dim"}>${parsed.error && this.frequencies
            ? parsed.error
            : coverage.known
              ? html`Receiver span ${mhz(coverage.lo)}–${mhz(coverage.hi)} MHz · ${coverage.inside.length} of ${options.length} inside${coverage.outside.length ? html` · <strong>${coverage.outside.length} outside</strong>${this.cycle ? " (covered by span cycling)" : " (turn on span cycling to cover them)"}` : ""}`
              : "Comma-separated MHz. Coverage appears once the receiver reports its span."}</small>
        </label>
        <label class="wb-field" for="wb-priority">
          <span>Priority channel</span>
          <select id="wb-priority" title="Wins when several channels are active" .value=${this.priority} @change=${(e) => { this.priority = e.target.value; }}>
            <option value="">None</option>
            ${options.map((hz) => html`<option value=${String(hz)} ?selected=${this.priority === String(hz)}>${mhz(hz)} MHz</option>`)}
          </select>
        </label>
        <label class="wb-field" for="wb-mode">
          <span>Mode</span>
          <select id="wb-mode" aria-label="Watch demodulator" .value=${this.demodulator}
            @change=${(e) => { this.demodulator = e.target.value; this.bandwidth = BANDWIDTH_PRESETS[this.demodulator][0]; }}>
            ${Object.keys(BANDWIDTH_PRESETS).map((m) => html`<option value=${m} ?selected=${m === this.demodulator}>${m}</option>`)}
          </select>
        </label>
        <label class="wb-field" for="wb-bw">
          <span>Channel width</span>
          <span class="wb-input-unit"><input id="wb-bw" type="number" min="100" max="250000" step="100" list="wb-bw-presets"
            .value=${String(this.bandwidth)} @change=${(e) => { this.bandwidth = Number(e.target.value); }}><small>Hz</small></span>
          <datalist id="wb-bw-presets">${BANDWIDTH_PRESETS[this.demodulator].map((bw) => html`<option value=${bw}>${bwLabel(bw)}</option>`)}</datalist>
          <small class="dim">${BANDWIDTH_PRESETS[this.demodulator].map(bwLabel).join(" · ")}</small>
        </label>
      </div>
      <div class="wb-actions">
        <button class="wb-primary" ?disabled=${!!blocker} aria-describedby="wb-start-why" @click=${() => this._start()}>
          ${this.pending && (this.pending.startsWith("Starting") || this.pending.startsWith("Applying")) ? `${this.pending}…` : active ? "Apply changes" : "Start watching"}</button>
        <small id="wb-start-why" class=${blocker && blocker !== "Working…" ? "wb-problem" : "dim"}>${blocker && blocker !== "Working…"
          ? blocker
          : active ? "Restarts the watch with these settings." : "Measures each listed channel; records only if Capture is on."}</small>
      </div>
    </section>`;
  }

  _renderOptions() {
    return html`<section class="wb-step" aria-labelledby="wb-opts">
      <h4 id="wb-opts">While watching</h4>
      <label class="wb-check"><input type="checkbox" .checked=${this.follow} @change=${(e) => { this.follow = e.target.checked; }}>
        <span><strong>Follow</strong> the active channel in the Scope and audio
        <small class="dim">Priority channel when active, otherwise the strongest.
          <button class="wb-link" @click=${(e) => { e.preventDefault(); this.dispatchEvent(new CustomEvent("open-instruments", { bubbles: true, composed: true })); }}>Open Scope</button></small></span></label>
      <label class="wb-check"><input type="checkbox" .checked=${this.autoRecord} @change=${(e) => { this.autoRecord = e.target.checked; }}>
        <span><strong>Capture</strong> activity to an IQ recording
        <small class="dim">Arms the recorder; activity on the priority (or first) channel is saved with pre-roll.</small></span></label>
      <label class="wb-check"><input type="checkbox" .checked=${this.cycle} @change=${(e) => { this.cycle = e.target.checked; }}>
        <span><strong>Cycle spans</strong> to reach channels outside the current span
        <small class="dim">Retunes between groups; pauses while a channel is active or recording.</small></span></label>
      ${this.cycle ? html`<div class="wb-grid wb-indent">${this._number("Time on each span", "dwell", { min: 1, max: 600, unit: "s" })}</div>` : ""}
      <details class="wb-more">
        <summary>Detection and capture limits</summary>
        <p class="dim wb-hint">Levels are estimates above the local spectrum floor, not calibrated RF power. Each field shows its default and range.</p>
        <div class="wb-grid">
          ${this._number("Active above floor", "startDb", { min: 0, max: 80, unit: "dB" })}
          ${this._number("Quiet below floor", "stopDb", { min: -10, max: 80, unit: "dB" })}
          ${this._number("Must stay active for", "minActive", { min: 0, max: 60, step: 0.1, unit: "s" })}
          ${this._number("Quiet hold", "quiet", { min: 0.1, max: 120, step: 0.1, unit: "s" })}
          ${this._number("Longest capture", "maxDuration", { min: 1, max: 3600, unit: "s" })}
          ${this._number("Capture limit", "eventCap", { min: 1, max: 1000, unit: "captures" })}
        </div>
      </details>
    </section>`;
  }

  _renderRunning(d) {
    const watch = d?.watch, active = !!d?.active, live = d?.live_receiver;
    const cap = captureState(d?.trigger);
    const recorderLeftArmed = !active && this.recorderArmed;
    if (!active && !live && !recorderLeftArmed) return "";
    const groups = d?.groups ?? [];
    return html`<section class="wb-step wb-running" aria-labelledby="wb-now" aria-live="polite">
      <h4 id="wb-now">Running now</h4>
      <dl class="wb-status">
        ${active ? html`
          <dt>Watching</dt>
          <dd><span>${watch?.measurements?.length ?? 0} channels · ${d.fresh ? "live spectrum" : html`<strong>no fresh spectrum</strong>`}
            ${d.config?.cycle_groups && groups.length > 1 ? html` · cycling span ${d.group_index + 1} of ${groups.length}` : ""}
            ${watch?.resolution_hz ? html` · resolution ${watch.resolution_hz.toFixed(0)} Hz` : ""}</span>
            <button class="wb-stop" ?disabled=${this.busy} @click=${() => this._stopWatch()}>Stop watching</button></dd>
          <dt>Selected channel</dt>
          <dd><span>${watch?.decode_target_hz ? `${mhz(watch.decode_target_hz)} MHz${this.follow ? " · followed for audio and Scope" : ""}` : "None active"}</span></dd>
          <dt>Capture</dt>
          <dd><span>${cap.word}${d.trigger?.enabled ? html` · ${cap.detail}` : ""}${d.trigger?.error ? html` · <strong class="wb-problem">${d.trigger.error}</strong>` : ""}</span></dd>
        ` : ""}
        ${recorderLeftArmed ? html`
          <dt>Recorder</dt>
          <dd><span>Still armed after the watch stopped; it keeps a rolling pre-roll buffer.</span>
            <button ?disabled=${this.busy} @click=${() => this._disarm()}>Disarm recorder</button></dd>` : ""}
        ${live ? html`
          <dt>Live inspection</dt>
          <dd><span>${live.frequency_hz ? `${mhz(live.frequency_hz)} MHz` : "on"} · ${live.demodulator ?? ""}</span>
            <button class="wb-stop" ?disabled=${this.busy} @click=${() => this._stopLive()}>Stop live inspection</button></dd>` : ""}
      </dl>
      ${active && live ? html`<p class="wb-actions"><button class="wb-stop" ?disabled=${this.busy} @click=${() => this._stopAll()}>Stop everything</button></p>` : ""}
      ${watch?.measurements?.length ? html`<div class="lib-table-wrap"><table class="lib-table wb-table">
        <caption class="visually-hidden">Watched channels</caption>
        <thead><tr><th scope="col">MHz</th><th scope="col">Coverage</th><th scope="col">Above floor</th><th scope="col">Activity</th></tr></thead>
        <tbody>${watch.measurements.map((row) => html`<tr>
          <td class="mono">${mhz(row.frequency_hz)}${row.frequency_hz === watch.priority_frequency_hz ? html` <span class="chip" title="Priority channel">priority</span>` : ""}</td>
          <td>${(() => { const c = coverageText(row); return c.ok ? c.text : html`<span class="wb-problem">${c.text}</span>`; })()}</td>
          <td>${db(row.snr_db)}</td>
          <td>${!row.covered ? "Not measured" : !d.fresh ? "No fresh data" : row.active ? html`<strong>Active</strong>` : "quiet / unverified"}</td>
        </tr>`)}</tbody></table></div>` : ""}
    </section>`;
  }

  /** The help <dialog> is rendered only while open (the same pattern as
   * signal-instruments.js); showModal() gives backdrop, Esc and focus trap. */
  updated() {
    const dialog = this.querySelector("dialog.wb-help-dialog");
    if (dialog && !dialog.open) { this._returnFocus = document.activeElement; dialog.showModal(); }
  }

  _renderHelp() {
    const close = () => { this._help = false; if (this._returnFocus?.isConnected) this._returnFocus.focus(); };
    const row = (term, text) => html`<dt>${term}</dt><dd>${text}</dd>`;
    return html`<dialog class="overlay-backdrop wb-help-dialog" aria-labelledby="wb-help-title"
      style="margin:0;padding:0;border:0;width:100vw;height:100vh;max-width:none;max-height:none;color:inherit"
      @cancel=${(e) => { e.preventDefault(); close(); }} @click=${close}>
      <div class="panel instruments-help-panel" role="document" @click=${(e) => e.stopPropagation()}>
        <header class="instruments-help-head">
          <div>
            <h2 id="wb-help-title">Channel watch</h2>
            <p class="dim">Keep an eye on several channels at once and catch activity on them, without retuning by hand.</p>
          </div>
          <button type="button" aria-label="close help" @click=${close}>&times;</button>
        </header>
        <div class="instruments-help-grid">
          <section class="instruments-help-card instruments-help-start">
            <h3>Get started</h3>
            <ol>
              <li>Type the frequencies to watch, in MHz, separated by commas. The line under the field says how many are inside the receiver span.</li>
              <li>Pick a <strong>Priority channel</strong> if one matters most, and the <strong>Mode</strong> and <strong>Channel width</strong> they share.</li>
              <li>Press <strong>Start watching</strong>. The table under <em>Running now</em> shows each channel, whether it is observed, its level above the floor and whether it is active.</li>
              <li>Turn on <strong>Follow</strong> to hear and see the active channel, <strong>Capture</strong> to record it, <strong>Cycle spans</strong> to reach channels outside the span.</li>
            </ol>
          </section>
          <section class="instruments-help-card">
            <h3>What it measures</h3>
            <p>For each channel, the level above the local spectrum floor inside the channel width. A channel is <strong>active</strong> when it stays above <em>Active above floor</em> for <em>Must stay active for</em>, and quiet again after <em>Quiet hold</em> below <em>Quiet below floor</em>. Levels are estimates, not calibrated power.</p>
          </section>
          <section class="instruments-help-card">
            <h3>Options</h3>
            <dl>
              ${row("Follow", "Plays one channel in the Scope and audio: the priority channel when active, otherwise the strongest, held briefly so it does not flap.")}
              ${row("Capture", "Arms the IQ recorder. Activity on the priority (or first) channel saves a capture with pre-roll. Stopping the watch leaves the recorder armed until you disarm it.")}
              ${row("Cycle spans", "Groups the channels by receiver span and retunes between groups for Time on each span. Only the current group is observed; cycling pauses while a channel is active or recording.")}
            </dl>
          </section>
          <section class="instruments-help-card">
            <h3>Coverage words</h3>
            <dl>
              ${row("Observed", "Inside the span with fresh spectrum.")}
              ${row("Outside receiver span", "Not measured; retune or turn on Cycle spans.")}
              ${row("Too close to the centre", "Inside the DC spike guard; not measured.")}
              ${row("No fresh spectrum", "The receiver stopped sending frames.")}
              ${row("Narrower than one bin", "The channel width is below the spectrum resolution; levels are approximate.")}
            </dl>
          </section>
          <section class="instruments-help-card">
            <h3>Limits</h3>
            <dl>
              ${row("Longest capture", "A recording stops at this length even if the channel is still active.")}
              ${row("Capture limit", "The watch stops recording after this many captures in one session.")}
            </dl>
          </section>
        </div>
        <p class="dim instruments-help-foot">The watch never transmits and never changes the receiver except when Cycle spans retunes it.</p>
      </div>
    </dialog>`;
  }

  render() {
    const d = this.data, active = !!d?.active;
    const parsed = parseFrequencies(this.frequencies);
    const coverage = spanCoverage(parsed.hz, d?.watch?.center_hz ?? d?.center_hz, d?.watch?.sample_rate_hz ?? d?.sample_rate_hz);
    const blocker = startBlocker({ receiver: this.receiver ?? d?.receiver, parsed, coverage, cycle: this.cycle, busy: this.busy });
    const problem = this.error || d?.error;
    return html`<section class="panel signal-workbench" aria-label="Channel watch">
      <details .open=${this._open} @toggle=${(e) => { if (e.target === e.currentTarget) this._open = e.currentTarget.open; }}>
        <summary class="wb-summary">
          <span class="wb-caret" aria-hidden="true">${this._open ? "▾" : "▸"}</span>
          <span class="wb-title">Channel watch</span>
          <span class="dim wb-summary-state">${this._summaryText(d)}</span>
          ${this._open ? html`<button class="wb-help" aria-haspopup="dialog" title="What Channel watch does and how to use it"
            @click=${(e) => { e.preventDefault(); e.stopPropagation(); this._help = true; }}>? Help</button>` : ""}
        </summary>
        ${problem ? html`<p role="alert" class="wb-alert">${problem}</p>` : ""}
        ${this._renderRunning(d)}
        <div class="wb-columns">
          ${this._renderSetup(parsed, coverage, blocker, active)}
          ${this._renderOptions()}
        </div>
      </details>
      ${this._help ? this._renderHelp() : ""}
    </section>`;
  }
}
customElements.define("signal-workbench", SignalWorkbench);
