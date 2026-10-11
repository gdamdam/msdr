import { LitElement, html } from "../vendor/lit.js";
import { mhz, timeHM, parseFrequencyInput, DEMODULATORS, BW_PRESETS } from "../format.js";
import { DEFAULT_PARAMS, MAX_CHANNELS, loadParams, saveParams, buildConfig } from "../scanner-config.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./failure-note.js";
import "./spectrum-strip.js";

const HITS_SHOWN = 50;

// Step select offered by the RANGE source (msdr/ui build spec) -- "custom"
// opens a plain kHz number entry alongside it.
const STEP_OPTIONS_HZ = [5000, 6250, 8330, 10000, 12500, 25000, 100000];

// Grid step select offered by the SPECTRUM source -- a plain list (no custom
// entry; a candidate is a bin, not a memory channel, so odd steps don't
// matter the way they do for RANGE).
const SPECTRUM_STEP_OPTIONS_HZ = [5000, 6250, 8330, 10000, 12500, 25000, 50000, 100000];
const SPECTRUM_DEFAULT_THRESHOLD = 20;

function stepLabel(hz) {
  const khz = hz / 1000;
  return `${Number.isInteger(khz) ? khz : khz.toFixed(2)} kHz`;
}

// RANGE presets: start/stop frequency plus the channel step and demodulator
// a sweep of that band conventionally uses. Bandwidth defaults to the mode's
// first BW_PRESETS entry when a preset is applied. Exported so scanner-deck.js
// can turn a running/saved range config back into a short label ("Airband")
// for its source-summary line.
export const RANGE_PRESETS = [
  { key: "airband", label: "Airband 118-137 MHz", startHz: 118_000_000, stopHz: 137_000_000, stepHz: 25_000, mode: "AM" },
  { key: "marine", label: "Marine VHF 156-162.025 MHz", startHz: 156_000_000, stopHz: 162_025_000, stepHz: 25_000, mode: "NFM" },
  { key: "2m", label: "2 m 144-146 MHz", startHz: 144_000_000, stopHz: 146_000_000, stepHz: 12_500, mode: "NFM" },
  { key: "70cm", label: "70 cm 430-440 MHz", startHz: 430_000_000, stopHz: 440_000_000, stepHz: 12_500, mode: "NFM" },
  { key: "pmr446", label: "PMR446 446.00625-446.19375 MHz", startHz: 446_006_250, stopHz: 446_193_750, stepHz: 12_500, mode: "NFM" },
  { key: "cb", label: "CB 26.965-27.405 MHz", startHz: 26_965_000, stopHz: 27_405_000, stepHz: 10_000, mode: "AM" },
  { key: "sw49m", label: "SW broadcast 49 m 5.9-6.2 MHz", startHz: 5_900_000, stopHz: 6_200_000, stepHz: 5_000, mode: "AM" },
];

/** ISO instant -> elapsed "MM:SS" (or "H:MM:SS" past an hour) against `now`. */
function elapsedLabel(startedAt, now) {
  const start = Date.parse(startedAt ?? "");
  if (Number.isNaN(start)) return "--:--";
  const secs = Math.max(0, Math.round((now - start) / 1000));
  const h = Math.floor(secs / 3600);
  const m = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
  const s = String(secs % 60).padStart(2, "0");
  return h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
}

/** SCANNER: sweeps a LIST (a saved signal list, incl. built-in Favorites) or
 * a frequency RANGE, dwelling on each channel and holding when its SNR
 * crosses a threshold. Status/hits are pushed down from app.js's `scan`
 * state (loaded via GET /api/scan, kept live over the "scan"/"scan_hit"
 * websocket messages) -- this view only owns the unsubmitted form fields. */
export class ScannerView extends LitElement {
  static properties = {
    scan: { type: Object },
    connected: { type: Boolean },
    started: { type: Boolean },
    canControl: { type: Boolean },
    failure: { type: Object },
    // "full" (default, standalone) | "setup" (embedded in scanner-deck's ⚙
    // modal -- deck already shows its own title/status/hit list, so this
    // hides the title, idle blurb and hit log here to avoid duplicating them).
    mode: { type: String },
    centerHz: { type: Number },
    sampleRateHz: { type: Number },
    vfoHz: { type: Number },
    _lists: { state: true },
    _source: { state: true },
    _listId: { state: true },
    _rangePreset: { state: true },
    _startMhz: { state: true },
    _stopMhz: { state: true },
    _stepHz: { state: true },
    _stepCustomKhz: { state: true },
    _spectrumStartMhz: { state: true },
    _spectrumStopMhz: { state: true },
    _spectrumStepHz: { state: true },
    _spectrumThresholdDb: { state: true },
    _mode: { state: true },
    _bwHz: { state: true },
    _dwellMs: { state: true },
    _thresholdDb: { state: true },
    _hangS: { state: true },
    _stopOnHit: { state: true },
    _captureHits: { state: true },
    _logFinds: { state: true },
    _keepScanning: { state: true },
    _findsDwellS: { state: true },
    _skipIgnored: { state: true },
    _now: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.scan = null;
    this.connected = false;
    this.started = true;
    this.canControl = false;
    this.failure = null;
    this.mode = "full";
    this.centerHz = null;
    this.sampleRateHz = null;
    this.vfoHz = null;
    this._lists = [];
    const p = { ...DEFAULT_PARAMS, ...loadParams() };
    this._source = p.source;
    this._listId = p.listId;
    this._rangePreset = p.rangePreset;
    this._startMhz = p.startMhz;
    this._stopMhz = p.stopMhz;
    this._stepHz = p.stepHz;
    this._stepCustomKhz = p.stepCustomKhz;
    this._spectrumStartMhz = p.spectrumStartMhz;
    this._spectrumStopMhz = p.spectrumStopMhz;
    this._spectrumStepHz = p.spectrumStepHz;
    this._spectrumThresholdDb = p.spectrumThresholdDb;
    this._mode = p.mode;
    this._bwHz = p.bwHz;
    this._dwellMs = p.dwellMs;
    this._thresholdDb = p.thresholdDb;
    this._hangS = p.hangS;
    this._stopOnHit = p.stopOnHit;
    this._captureHits = p.captureHits;
    this._logFinds = p.logFinds;
    this._keepScanning = p.keepScanning;
    this._findsDwellS = p.findsDwellS;
    this._skipIgnored = p.skipIgnored;
    this._now = Date.now();
    this._tickTimer = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._tickTimer = setInterval(() => (this._now = Date.now()), 1000);
    this._loadLists();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._tickTimer);
  }

  async _loadLists() {
    try {
      this._lists = await api.listLists();
      if (!this._listId || !this._lists.some((l) => l.id === this._listId)) {
        this._listId = this._lists.find((l) => l.builtin)?.id ?? this._lists[0]?.id ?? "";
      }
    } catch {
      this._lists = [];
    }
  }

  get _ready() {
    return this.connected && (this.started ?? true) && this.canControl;
  }

  /** Current form fields as a scanner-config.js DEFAULT_PARAMS-shaped object
   * -- the one snapshot both _persist() (localStorage) and _buildConfig()
   * (the scan/start body) build from. */
  _paramsSnapshot() {
    return {
      source: this._source,
      listId: this._listId,
      rangePreset: this._rangePreset,
      startMhz: this._startMhz,
      stopMhz: this._stopMhz,
      stepHz: this._stepHz,
      stepCustomKhz: this._stepCustomKhz,
      spectrumStartMhz: this._spectrumStartMhz,
      spectrumStopMhz: this._spectrumStopMhz,
      spectrumStepHz: this._spectrumStepHz,
      spectrumThresholdDb: this._spectrumThresholdDb,
      mode: this._mode,
      bwHz: this._bwHz,
      dwellMs: this._dwellMs,
      thresholdDb: this._thresholdDb,
      hangS: this._hangS,
      stopOnHit: this._stopOnHit,
      captureHits: this._captureHits,
      logFinds: this._logFinds,
      keepScanning: this._keepScanning,
      findsDwellS: this._findsDwellS,
      skipIgnored: this._skipIgnored,
    };
  }

  _persist() {
    saveParams(this._paramsSnapshot());
  }

  get _effectiveStepHz() {
    if (this._stepHz === "custom") {
      const khz = Number(this._stepCustomKhz);
      return Number.isFinite(khz) && khz > 0 ? Math.round(khz * 1000) : null;
    }
    const hz = Number(this._stepHz);
    return Number.isFinite(hz) && hz > 0 ? hz : null;
  }

  /** Channel count a RANGE source would sweep, or null while the fields
   * don't yet describe a valid range -- also used to block a >10,000-channel
   * submission client-side (the server 422s the same case, see api contract). */
  get _rangeChannelCount() {
    const start = parseFrequencyInput(this._startMhz);
    const stop = parseFrequencyInput(this._stopMhz);
    const step = this._effectiveStepHz;
    if (!start || !stop || !step || stop <= start) return null;
    return Math.floor((stop - start) / step) + 1;
  }

  /** Current tuner span "157.428–167.428 MHz" from device_center_frequency /
   * device_sample_rate, shown as the SPECTRUM source's implicit start/stop
   * when those fields are left empty. Null until the device props land. */
  get _visibleSpanLabel() {
    if (this.centerHz == null || !this.sampleRateHz) return null;
    const lowMhz = (this.centerHz - this.sampleRateHz / 2) / 1_000_000;
    const highMhz = (this.centerHz + this.sampleRateHz / 2) / 1_000_000;
    return `${lowMhz.toFixed(3)}–${highMhz.toFixed(3)} MHz`;
  }

  /** Delegates to scanner-config.js's buildConfig -- shared with
   * scanner-deck.js so a START from the deck (built from the same saved
   * params) validates exactly the same way as one from this form. */
  _buildConfig() {
    return buildConfig(this._paramsSnapshot());
  }

  _selectSource(source) {
    if (source === this._source) return;
    this._source = source;
    this._persist();
  }

  _applyRangePreset(key) {
    const preset = RANGE_PRESETS.find((p) => p.key === key);
    if (!preset) return;
    this._rangePreset = key;
    this._startMhz = String(preset.startHz / 1_000_000);
    this._stopMhz = String(preset.stopHz / 1_000_000);
    this._stepHz = preset.stepHz;
    this._mode = preset.mode;
    this._bwHz = BW_PRESETS[preset.mode]?.[0] ?? this._bwHz;
    this._persist();
  }

  _bwOptions() {
    const presets = BW_PRESETS[this._mode] ?? [];
    return presets.includes(this._bwHz) || !this._bwHz ? presets : [this._bwHz, ...presets];
  }

  _start() {
    const config = this._buildConfig();
    if (!config) return;
    this.dispatchEvent(new CustomEvent("scan-start", { detail: config }));
  }

  _skip(persist) {
    this.dispatchEvent(new CustomEvent("scan-skip", { detail: { persist } }));
  }

  _tuneHit(hit) {
    if (hit.signal_id) {
      this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: hit.signal_id } }));
      return;
    }
    const cfg = this.scan?.config ?? {};
    this.dispatchEvent(
      new CustomEvent("tune-frequency", {
        detail: {
          hz: hit.frequency_hz,
          demodulator: cfg.demodulator,
          filter_bandwidth_hz: cfg.filter_bandwidth_hz,
        },
      })
    );
  }

  _captureHit(hit) {
    this.dispatchEvent(new CustomEvent("star-hit", { detail: { hit } }));
  }

  render() {
    const scan = this.scan ?? {};
    const state = scan.state ?? "idle";
    const idle = state === "idle";
    const setup = this.mode === "setup";
    return html`
      <div class="panel">
        ${setup ? "" : html`<h2>Scanner</h2>`}
        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}
        ${idle && !setup
          ? html`<p class="empty">
              Sweeps a saved list or a frequency range, dwelling on each channel and holding
              automatically when its SNR crosses the threshold.
            </p>`
          : ""}
        ${this._sourceForm(state)} ${this._paramsForm(state)} ${this._controls(scan, state)}
        ${!idle ? this._liveBlock(scan) : ""}
        <!-- setup mode (scanner-deck's ⚙ modal) owns its own hit list (▾),
             so this form's hit log would just be a duplicate. -->
        ${setup ? "" : this._hitLog(scan)}
      </div>
    `;
  }

  _sourceForm(state) {
    const locked = state !== "idle";
    return html`
      <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
        <div style="display:flex;gap:8px">
          <button
            class="tag-chip mono ${this._source === "list" ? "selected" : ""}"
            aria-pressed=${this._source === "list"}
            ?disabled=${locked}
            @click=${() => this._selectSource("list")}
          >
            LIST
          </button>
          <button
            class="tag-chip mono ${this._source === "range" ? "selected" : ""}"
            aria-pressed=${this._source === "range"}
            ?disabled=${locked}
            @click=${() => this._selectSource("range")}
          >
            RANGE
          </button>
          <button
            class="tag-chip mono ${this._source === "spectrum" ? "selected" : ""}"
            aria-pressed=${this._source === "spectrum"}
            ?disabled=${locked}
            @click=${() => this._selectSource("spectrum")}
          >
            SPECTRUM
          </button>
        </div>
        ${this._source === "list"
          ? this._listForm(locked)
          : this._source === "range"
            ? this._rangeForm(locked)
            : this._spectrumForm(locked)}
      </div>
    `;
  }

  _listForm(locked) {
    return html`
      <select
        aria-label="scan list"
        ?disabled=${locked}
        .value=${this._listId}
        @change=${(e) => {
          this._listId = e.target.value;
          this._persist();
        }}
      >
        ${this._lists.length
          ? this._lists.map(
              (l) =>
                html`<option value=${l.id} ?selected=${l.id === this._listId}>
                  ${l.builtin ? "★ " : ""}${l.name} (${l.count})
                </option>`
            )
          : html`<option value="">No scan lists — create one in LIBRARY</option>`}
      </select>
    `;
  }

  _rangeForm(locked) {
    const count = this._rangeChannelCount;
    return html`
      <select
        aria-label="range preset"
        ?disabled=${locked}
        @change=${(e) => this._applyRangePreset(e.target.value)}
      >
        <option value="">Custom…</option>
        ${RANGE_PRESETS.map((p) => html`<option value=${p.key}>${p.label}</option>`)}
      </select>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input
          name="scan-start-mhz"
          class="mono"
          style="width:110px"
          placeholder="start MHz"
          aria-label="range start"
          ?disabled=${locked}
          .value=${this._startMhz}
          @input=${(e) => {
            this._startMhz = e.target.value;
            this._rangePreset = "";
            this._persist();
          }}
        />
        <span class="dim">–</span>
        <input
          name="scan-stop-mhz"
          class="mono"
          style="width:110px"
          placeholder="stop MHz"
          aria-label="range stop"
          ?disabled=${locked}
          .value=${this._stopMhz}
          @input=${(e) => {
            this._stopMhz = e.target.value;
            this._rangePreset = "";
            this._persist();
          }}
        />
        <select
          aria-label="step"
          ?disabled=${locked}
          .value=${String(this._stepHz)}
          @change=${(e) => {
            this._stepHz = e.target.value === "custom" ? "custom" : Number(e.target.value);
            this._rangePreset = "";
            this._persist();
          }}
        >
          ${STEP_OPTIONS_HZ.map(
            (hz) => html`<option value=${hz} ?selected=${hz === this._stepHz}>${stepLabel(hz)}</option>`
          )}
          <option value="custom" ?selected=${this._stepHz === "custom"}>Custom…</option>
        </select>
        ${this._stepHz === "custom"
          ? html`<input
              name="scan-step-custom-khz"
              class="mono"
              style="width:80px"
              placeholder="kHz"
              aria-label="custom step kHz"
              ?disabled=${locked}
              .value=${this._stepCustomKhz}
              @input=${(e) => {
                this._stepCustomKhz = e.target.value;
                this._persist();
              }}
            />`
          : ""}
        <select
          aria-label="scan mode"
          ?disabled=${locked}
          .value=${this._mode}
          @change=${(e) => {
            this._mode = e.target.value;
            this._bwHz = BW_PRESETS[this._mode]?.[0] ?? this._bwHz;
            this._rangePreset = "";
            this._persist();
          }}
        >
          ${DEMODULATORS.filter((m) => m !== "RAW").map(
            (m) => html`<option value=${m} ?selected=${m === this._mode}>${m}</option>`
          )}
        </select>
        <select
          aria-label="scan bandwidth"
          ?disabled=${locked}
          .value=${String(this._bwHz)}
          @change=${(e) => {
            this._bwHz = Number(e.target.value);
            this._persist();
          }}
        >
          ${this._bwOptions().map(
            (hz) => html`<option value=${hz} ?selected=${hz === this._bwHz}>${(hz / 1000).toFixed(1)} kHz</option>`
          )}
        </select>
      </div>
      ${count != null && count > MAX_CHANNELS
        ? html`<p class="empty" style="color:var(--danger)">
            ${count.toLocaleString()} channels exceeds the ${MAX_CHANNELS.toLocaleString()} max -- narrow the range or widen the step
          </p>`
        : ""}
    `;
  }

  _spectrumForm(locked) {
    const span = this._visibleSpanLabel;
    return html`
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input
          name="scan-spectrum-start-mhz"
          class="mono"
          style="width:110px"
          placeholder=${span ? span.split("–")[0].trim() : "start MHz"}
          aria-label="spectrum start"
          ?disabled=${locked}
          .value=${this._spectrumStartMhz}
          @input=${(e) => {
            this._spectrumStartMhz = e.target.value;
            this._persist();
          }}
        />
        <span class="dim">–</span>
        <input
          name="scan-spectrum-stop-mhz"
          class="mono"
          style="width:110px"
          placeholder=${span ? span.split("–")[1].trim() : "stop MHz"}
          aria-label="spectrum stop"
          ?disabled=${locked}
          .value=${this._spectrumStopMhz}
          @input=${(e) => {
            this._spectrumStopMhz = e.target.value;
            this._persist();
          }}
        />
        <select
          aria-label="spectrum step"
          ?disabled=${locked}
          .value=${String(this._spectrumStepHz)}
          @change=${(e) => {
            this._spectrumStepHz = Number(e.target.value);
            this._persist();
          }}
        >
          ${SPECTRUM_STEP_OPTIONS_HZ.map(
            (hz) => html`<option value=${hz} ?selected=${hz === this._spectrumStepHz}>${stepLabel(hz)}</option>`
          )}
        </select>
        <label class="dim mono" style="display:flex;align-items:center;gap:4px" title="above noise floor"
          >LEVEL
          <input
            name="scan-spectrum-threshold"
            class="mono"
            type="number"
            min="5"
            max="100"
            style="width:60px"
            aria-label="spectrum level threshold"
            ?disabled=${locked}
            .value=${String(this._spectrumThresholdDb)}
            @input=${(e) => {
              this._spectrumThresholdDb = e.target.value;
              this._persist();
            }}
          />
        </label>
        <select
          aria-label="scan mode"
          ?disabled=${locked}
          .value=${this._mode}
          @change=${(e) => {
            this._mode = e.target.value;
            this._bwHz = BW_PRESETS[this._mode]?.[0] ?? this._bwHz;
            this._persist();
          }}
        >
          ${DEMODULATORS.filter((m) => m !== "RAW").map(
            (m) => html`<option value=${m} ?selected=${m === this._mode}>${m}</option>`
          )}
        </select>
        <select
          aria-label="scan bandwidth"
          ?disabled=${locked}
          .value=${String(this._bwHz)}
          @change=${(e) => {
            this._bwHz = Number(e.target.value);
            this._persist();
          }}
        >
          ${this._bwOptions().map(
            (hz) => html`<option value=${hz} ?selected=${hz === this._bwHz}>${(hz / 1000).toFixed(1)} kHz</option>`
          )}
        </select>
      </div>
      <p class="dim mono" style="font-size:0.85em">
        empty start/stop = current visible span${span ? html` (${span})` : ""} · level is above noise floor
      </p>
    `;
  }

  /** TIMING sub-group (S5 contract, §3): DWELL/HANG/FINDS DWELL and the
   * KEEP SCANNING toggle, relabeled for what they actually do -- same state
   * vars, @input/@click handlers and _persist() calls as before, no engine
   * change (the POST /api/scan/start body -- scanner-config.js's buildConfig
   * -- is untouched). */
  _timingGroup(locked) {
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="survey-group-label">TIMING</div>
        <p class="dim" style="font-size:0.8em;margin:0">
          Dwell measures each channel briefly; Hang keeps holding after activity ends; with Monitor only, a hit is
          logged for Finds dwell and the scan moves on instead of holding.
        </p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <label class="dim mono" style="display:flex;align-items:center;gap:4px"
            >Dwell (measure)
            <input
              name="scan-dwell-ms"
              class="mono"
              style="width:70px"
              ?disabled=${locked}
              .value=${String(this._dwellMs)}
              @input=${(e) => {
                this._dwellMs = e.target.value;
                this._persist();
              }}
            />ms</label
          >
          <label class="dim mono" style="display:flex;align-items:center;gap:4px"
            >Hang (stay after activity ends)
            <input
              name="scan-hang-s"
              class="mono"
              style="width:50px"
              ?disabled=${locked}
              .value=${String(this._hangS)}
              @input=${(e) => {
                this._hangS = e.target.value;
                this._persist();
              }}
            />s</label
          >
          <button
            class="mono"
            aria-pressed=${this._keepScanning}
            ?disabled=${locked}
            @click=${() => {
              this._keepScanning = !this._keepScanning;
              this._persist();
            }}
          >
            Monitor only (log finds, don't hold)
          </button>
          ${this._keepScanning
            ? html`<label class="dim mono" style="display:flex;align-items:center;gap:4px"
                >Finds dwell (log then move on)
                <input
                  name="scan-finds-dwell-s"
                  class="mono"
                  style="width:50px"
                  ?disabled=${locked}
                  .value=${String(this._findsDwellS)}
                  @input=${(e) => {
                    this._findsDwellS = e.target.value;
                    this._persist();
                  }}
                />s</label
              >`
            : ""}
        </div>
      </div>
    `;
  }

  _paramsForm(state) {
    const locked = state !== "idle";
    return html`
      ${this._timingGroup(locked)}
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">
        <label class="dim mono" style="display:flex;align-items:center;gap:4px"
          >THRESHOLD
          <input
            name="scan-threshold-db"
            class="mono"
            style="width:60px"
            ?disabled=${locked}
            .value=${String(this._thresholdDb)}
            @input=${(e) => {
              this._thresholdDb = e.target.value;
              this._persist();
            }}
          />dB</label
        >
        <button
          class="mono"
          aria-pressed=${this._stopOnHit}
          ?disabled=${locked}
          @click=${() => {
            this._stopOnHit = !this._stopOnHit;
            this._persist();
          }}
        >
          STOP ON HIT
        </button>
        ${this._source === "range"
          ? html`<button
              class="mono"
              aria-pressed=${this._captureHits}
              ?disabled=${locked}
              @click=${() => {
                this._captureHits = !this._captureHits;
                this._persist();
              }}
            >
              CAPTURE HITS
            </button>`
          : ""}
        <button
          class="mono"
          aria-pressed=${this._logFinds}
          ?disabled=${locked}
          @click=${() => {
            this._logFinds = !this._logFinds;
            this._persist();
          }}
        >
          LOG FINDS
        </button>
        <button
          class="mono"
          aria-pressed=${this._skipIgnored}
          ?disabled=${locked}
          @click=${() => {
            this._skipIgnored = !this._skipIgnored;
            this._persist();
          }}
        >
          SKIP IGNORED
        </button>
      </div>
    `;
  }

  _controls(scan, state) {
    const idle = state === "idle";
    const paused = state === "paused";
    const configValid = idle ? !!this._buildConfig() : true;
    return html`
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        <button class="run-start" ?disabled=${!this._ready || !idle || !configValid} @click=${() => this._start()}>
          ▶ START
        </button>
        <button
          ?disabled=${idle}
          @click=${() =>
            this.dispatchEvent(new CustomEvent(paused ? "scan-resume" : "scan-pause"))}
        >
          ${paused ? "RESUME" : "PAUSE"}
        </button>
        <button class="run-stop" ?disabled=${idle} @click=${() => this.dispatchEvent(new CustomEvent("scan-stop"))}>
          ■ STOP
        </button>
        <button ?disabled=${idle} @click=${() => this._skip(false)}>SKIP</button>
        ${this._source === "list"
          ? html`<button ?disabled=${idle} @click=${() => this._skip(true)}>skip always</button>`
          : ""}
        <button
          ?disabled=${idle}
          aria-pressed=${state === "holding"}
          @click=${() => this.dispatchEvent(new CustomEvent("scan-hold"))}
        >
          HOLD
        </button>
      </div>
    `;
  }

  _snrBar(snr, thresholdDb) {
    const MIN = -10;
    const MAX = 40;
    const pct = (v) => (Math.min(MAX, Math.max(MIN, v)) - MIN) / (MAX - MIN) * 100;
    const snrPct = snr != null ? pct(snr) : 0;
    const threshPct = thresholdDb != null ? pct(thresholdDb) : null;
    const over = snr != null && thresholdDb != null && snr >= thresholdDb;
    return html`
      <div class="mono dim" style="display:flex;align-items:center;gap:8px">
        <span style="width:70px">SNR</span>
        <div style="position:relative;flex:1;height:10px;background:var(--panel-well);border:1px solid var(--line)">
          <div
            style="position:absolute;top:0;bottom:0;left:0;width:${snrPct}%;background:${over
              ? "var(--accent)"
              : "var(--led-unlit)"}"
          ></div>
          ${threshPct != null
            ? html`<div
                style="position:absolute;top:-2px;bottom:-2px;left:${threshPct}%;width:1px;background:var(--danger)"
              ></div>`
            : ""}
        </div>
        <span style="width:70px;text-align:right">${snr != null ? `${snr.toFixed(1)} dB` : "--"}</span>
      </div>
    `;
  }

  _liveBlock(scan) {
    const channel = scan.channel;
    const channelLabel = channel ? channel.label || mhz(channel.frequency_hz) : "--";
    const isSpectrum = scan.config?.source?.type === "spectrum";
    const spectrum = scan.spectrum;
    return html`
      <div style="display:flex;flex-direction:column;gap:4px;margin-top:8px;border-top:1px solid var(--line);padding-top:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot
            color="var(--accent)"
            .lit=${scan.state === "running" || scan.state === "holding"}
            class=${scan.state === "holding" ? "led-blink" : ""}
          ></led-dot>
          <span class="mono">${(scan.state ?? "idle").toUpperCase()}</span>
          ${scan.reason ? html`<span class="dim">${scan.reason}</span>` : ""}
          <span class="spacer"></span>
          <span class="dim mono">${elapsedLabel(scan.started_at, this._now)}</span>
        </div>
        <div class="mono dim" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span>${channelLabel}${channel?.demodulator ? ` ${channel.demodulator}` : ""}</span>
          <span class="spacer"></span>
          ${isSpectrum
            ? html`<span
                >candidates: ${spectrum?.candidates?.length ?? 0} · locked out:
                ${spectrum?.locked_out?.length ?? 0}</span
              >`
            : html`<span>${scan.position ?? 0} / ${scan.total ?? 0}</span>`}
        </div>
        ${isSpectrum && spectrum
          ? html`<spectrum-strip
              .bins=${spectrum.bins ?? []}
              .floor=${spectrum.floor ?? 0}
              .thresholdDb=${scan.config?.source?.level_threshold ?? SPECTRUM_DEFAULT_THRESHOLD}
              .candidates=${spectrum.candidates ?? []}
              .lockedOut=${spectrum.locked_out ?? []}
              .centerHz=${spectrum.center_hz}
              .sampleRateHz=${spectrum.sample_rate_hz}
              .vfoHz=${this.vfoHz}
            ></spectrum-strip>`
          : ""}
        ${this._snrBar(scan.snr_db, scan.config?.threshold_snr_db)}
      </div>
    `;
  }

  _hitLog(scan) {
    const hits = (scan.hits ?? []).slice(0, HITS_SHOWN);
    if (!hits.length) return "";
    return html`
      <div style="margin-top:8px">
        <div class="label">Hits</div>
        <div class="list">
          ${hits.map((h) =>
            hitRow(h, { onTune: (hit) => this._tuneHit(hit), onCapture: (hit) => this._captureHit(hit) })
          )}
        </div>
      </div>
    `;
  }
}

customElements.define("scanner-view", ScannerView);

/** One hit row: time, frequency, label, peak SNR, duration, ▸ tune, ★
 * capture -- exported so scanner-deck.js's ▾ hit dropdown renders identical
 * rows instead of a second copy of this markup (msdr/ui build spec v0.14.2). */
/** `onOpen` is optional (only scanner-deck's ▾ hits dropdown passes it, U3) --
 * omitting it just skips the ⓘ button, so scanner-view's own hit log keeps
 * its exact prior markup. */
export function hitRow(hit, { onTune, onCapture, onOpen }) {
  return html`
    <div class="row" style="flex-direction:column;align-items:stretch;gap:2px" @click=${() => onTune(hit)}>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="mono dim">${timeHM(hit.at)}</span>
        <span class="mono">${mhz(hit.frequency_hz)}</span>
        <span>${hit.label ?? ""}</span>
        <span class="spacer"></span>
        <span class="mono dim">${hit.peak_snr_db != null ? `${hit.peak_snr_db.toFixed(1)} dB` : "--"}</span>
        <span class="mono dim">${hit.duration_s != null ? `${hit.duration_s.toFixed(1)}s` : "--"}</span>
        <button
          class="tune"
          title="Tune"
          aria-label="tune ${hit.label || mhz(hit.frequency_hz)}"
          style="font-size:0.8em;padding:2px 8px"
          @click=${(ev) => {
            ev.stopPropagation();
            onTune(hit);
          }}
        >
          ▸
        </button>
        <button
          title="Save as signal"
          aria-label="capture ${hit.label || mhz(hit.frequency_hz)}"
          style="font-size:0.8em;padding:2px 8px"
          @click=${(ev) => {
            ev.stopPropagation();
            onCapture(hit);
          }}
        >
          ★
        </button>
        ${onOpen
          ? html`<button
              title="Signal detail"
              aria-label="signal detail for ${hit.label || mhz(hit.frequency_hz)}"
              style="font-size:0.8em;padding:2px 8px"
              @click=${(ev) => {
                ev.stopPropagation();
                onOpen(hit);
              }}
            >
              ⓘ
            </button>`
          : ""}
      </div>
    </div>
  `;
}
