import { LitElement, html } from "../vendor/lit.js";
import { timeHM } from "../format.js";
import { DEFAULT_PARAMS, loadParams, buildConfig } from "../scanner-config.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./failure-note.js";
import "./run-indicator.js";
import { RANGE_PRESETS, hitRow } from "./scanner-view.js"; // also registers <scanner-view>, used by the ⚙ modal

const HITS_SHOWN = 20;
const EXPANDED_KEY = "msdr.scannerDeck.expanded";

/** localStorage may throw (private browsing, quota, disabled) -- degrade to
 * collapsed-by-default rather than breaking the deck. */
function loadExpanded() {
  try {
    return localStorage.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

function saveExpanded(expanded) {
  try {
    localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    /* ignore */
  }
}

/** Compact scanner deck in the receiver's right-hand operations zone. Its
 * START uses the last-saved setup (see scanner-config.js); ⚙
 * opens a modal with the full scanner-view form to change source/params, and
 * ▾ opens a dropdown of recent hits -- both reuse scanner-view rather than
 * duplicating it. `scan`/`connected`/`started`/`canControl` mirror exactly
 * what app.js fed the old <scanner-view> section (see msdr/ui/app.js). */
export class ScannerDeck extends LitElement {
  static properties = {
    scan: { type: Object },
    connected: { type: Boolean },
    started: { type: Boolean },
    canControl: { type: Boolean },
    centerHz: { type: Number },
    sampleRateHz: { type: Number },
    vfoHz: { type: Number },
    failure: { type: Object },
    _lists: { state: true },
    _setupOpen: { state: true },
    _hitsOpen: { state: true },
    _expanded: { state: true },
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
    this.centerHz = null;
    this.sampleRateHz = null;
    this.vfoHz = null;
    this.failure = null;
    this._lists = [];
    this._setupOpen = false;
    this._hitsOpen = false;
    // U1: collapses to one line (lamp + state + primary START/STOP) when
    // idle; remembered in localStorage, but overridden while a scan is
    // actually running/holding/paused (see render's `expanded`).
    this._expanded = loadExpanded();
  }

  _toggleExpanded() {
    this._expanded = !this._expanded;
    saveExpanded(this._expanded);
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadLists();
  }

  async _loadLists() {
    try {
      this._lists = await api.listLists();
    } catch {
      this._lists = [];
    }
  }

  /** Exposed for app.js's Escape priority (querySelector("scanner-deck"),
   * same pattern as instrument-panel's focusFrequencyEdit/toggleRecording)
   * -- the setup modal is nested inside instrument-panel, not a top-level
   * overlay app.js already tracks state for. */
  get setupOpen() {
    return this._setupOpen;
  }

  closeSetup() {
    this._setupOpen = false;
  }

  get _ready() {
    return this.connected && (this.started ?? true) && this.canControl;
  }

  /** "RANGE Airband" / "LIST Evening SW" / "SPECTRUM 94–104 MHz" -- from the
   * running/last scan's config when there is one, else from the saved setup
   * params a START would use. */
  _sourceSummary() {
    const src = this.scan?.config?.source;
    if (src) return this._describeSource(src);
    const saved = { ...DEFAULT_PARAMS, ...loadParams() };
    const config = buildConfig(saved);
    return config ? this._describeSource(config.source) : "no source set — open Scanner setup";
  }

  _describeSource(src) {
    if (src.type === "list") {
      const list = this._lists.find((l) => l.id === src.list_id);
      return `LIST ${list ? list.name : (src.list_id ?? "?")}`;
    }
    if (src.type === "range") {
      const preset = RANGE_PRESETS.find(
        (p) => p.startHz === src.start_hz && p.stopHz === src.stop_hz && p.stepHz === src.step_hz
      );
      // Presets carry their frequency span in the label ("Airband 118-137
      // MHz") -- strip it back off for the deck's short form.
      if (preset) return `RANGE ${preset.label.replace(/\s+[\d.]+-[\d.]+\s*MHz$/i, "")}`;
      return `RANGE ${(src.start_hz / 1e6).toFixed(3)}–${(src.stop_hz / 1e6).toFixed(3)} MHz`;
    }
    if (src.type === "spectrum") {
      if (src.start_hz && src.stop_hz) {
        return `SPECTRUM ${(src.start_hz / 1e6).toFixed(1)}–${(src.stop_hz / 1e6).toFixed(1)} MHz`;
      }
      return "SPECTRUM current span";
    }
    return "";
  }

  /** START (or RESUME when paused). Idle: starts from the last-saved setup,
   * or opens the ⚙ modal when nothing usable is saved. Paused: resumes --
   * no config needed, same as scanner-view's own PAUSE/RESUME toggle. */
  _primaryAction() {
    const state = this.scan?.state ?? "idle";
    if (state === "paused") {
      this.dispatchEvent(new CustomEvent("scan-resume"));
      return;
    }
    const saved = { ...DEFAULT_PARAMS, ...loadParams() };
    const config = buildConfig(saved);
    if (!config) {
      this._setupOpen = true;
      return;
    }
    this.dispatchEvent(new CustomEvent("scan-start", { detail: config }));
  }

  /** A hit's tune target: the bound signal for a list hit, else the raw
   * frequency/recipe from the scan's config -- mirrors scanner-view.js's
   * _tuneHit exactly so a tune from the deck's last-hit line or ▾ dropdown
   * behaves identically to one from the full form. */
  _tuneHit(hit) {
    if (hit.signal_id) {
      this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: hit.signal_id } }));
      return;
    }
    const cfg = this.scan?.config ?? {};
    this.dispatchEvent(
      new CustomEvent("tune-frequency", {
        detail: { hz: hit.frequency_hz, demodulator: cfg.demodulator, filter_bandwidth_hz: cfg.filter_bandwidth_hz },
      })
    );
  }

  _captureHit(hit) {
    this.dispatchEvent(new CustomEvent("star-hit", { detail: { hit } }));
  }

  /** U3: opens the signal-detail panel for a hit -- signal_id when it's a
   * bound list hit (same test _tuneHit uses), else just its frequency. */
  _openSignal(hit) {
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: hit.frequency_hz, signal_id: hit.signal_id ?? undefined },
        bubbles: true,
        composed: true,
      })
    );
  }

  _snrBar(snr, thresholdDb) {
    const MIN = -10;
    const MAX = 40;
    const pct = (v) => ((Math.min(MAX, Math.max(MIN, v)) - MIN) / (MAX - MIN)) * 100;
    const snrPct = snr != null ? pct(snr) : 0;
    const over = snr != null && thresholdDb != null && snr >= thresholdDb;
    return html`
      <div class="scanner-deck-snr mono dim">
        <span>SNR</span>
        <div class="scanner-deck-snr-track">
          <div
            class="scanner-deck-snr-fill"
            style="width:${snrPct}%;background:${over ? "var(--accent)" : "var(--led-unlit)"}"
          ></div>
        </div>
        <span>${snr != null ? `${snr.toFixed(1)} dB` : "--"}</span>
      </div>
    `;
  }

  /** Inline reason shown under START when it's disabled and idle: receiver
   * stopped takes priority over a missing source (matches _ready's own
   * connected/started/canControl -> source-config check order). */
  _primaryDisabledReason() {
    if (!this.connected || !(this.started ?? true)) return "Press Play in SDRconnect";
    if (!this.canControl) return "";
    const saved = { ...DEFAULT_PARAMS, ...loadParams() };
    return buildConfig(saved) ? "" : "Pick a source in Scanner setup";
  }

  _controls(state) {
    const idle = state === "idle";
    const paused = state === "paused";
    // Same readiness/idle gating scanner-view._controls uses today: START
    // needs the receiver actually controllable, PAUSE/STOP/SKIP/HOLD just
    // need a scan in progress (paused counts).
    const primaryDisabled = idle ? !this._ready : !paused;
    const reason = idle && primaryDisabled ? this._primaryDisabledReason() : "";
    return html`
      <div class="scanner-deck-controls">
        <button class="run-start" ?disabled=${primaryDisabled} @click=${() => this._primaryAction()}>
          ${paused ? "▶ RESUME" : "▶ START"}
        </button>
        <button
          ?disabled=${idle || paused}
          @click=${() => this.dispatchEvent(new CustomEvent("scan-pause"))}
        >
          ⏸ PAUSE
        </button>
        <button class="run-stop" ?disabled=${idle} @click=${() => this.dispatchEvent(new CustomEvent("scan-stop"))}>
          ■ STOP
        </button>
        ${idle
          ? ""
          : html`<run-indicator
              class=${paused ? "paused" : ""}
              label=${paused ? "PAUSED" : "SCANNING"}
            ></run-indicator>`}
        <button
          ?disabled=${idle}
          @click=${() => this.dispatchEvent(new CustomEvent("scan-skip", { detail: { persist: false } }))}
        >
          » SKIP
        </button>
        <button
          ?disabled=${idle}
          aria-pressed=${state === "holding"}
          @click=${() => this.dispatchEvent(new CustomEvent("scan-hold"))}
        >
          HOLD
        </button>
        <button title="Scanner setup" aria-label="Scanner setup" @click=${() => (this._setupOpen = true)}>Scanner setup</button>
        <button
          title="Recent hits"
          aria-haspopup="true"
          aria-expanded=${this._hitsOpen}
          @click=${() => (this._hitsOpen = !this._hitsOpen)}
        >
          ▾
        </button>
      </div>
      ${reason ? html`<div class="hint">${reason}</div>` : ""}
    `;
  }

  /** "no hits yet" replacement: names the next step -- pick a source when
   * none is configured, prompt START with the source summary when idle, or
   * just confirm the scan is running when there's nothing to show yet. */
  _emptyHitsText(state) {
    if (state === "running" || state === "holding") return "Scanning… no hits yet";
    const summary = this._sourceSummary();
    if (summary.startsWith("no source set")) return "No scan source — open Scanner setup to pick a list or range";
    return `Press START to scan ${summary}`;
  }

  _lastHitLine(scan, state) {
    const hit = (scan.hits ?? [])[0];
    if (!hit) return html`<span class="dim mono">${this._emptyHitsText(state)}</span>`;
    const mhzText = (hit.frequency_hz / 1e6).toFixed(3);
    const snrText = hit.peak_snr_db != null ? `${Math.round(hit.peak_snr_db)} dB` : "--";
    return html`<span class="dim mono">last hit ${timeHM(hit.at)} ${mhzText} ${snrText}</span>`;
  }

  _hitsDropdown(scan, state) {
    if (!this._hitsOpen) return "";
    const hits = (scan.hits ?? []).slice(0, HITS_SHOWN);
    return html`
      <div class="panel scanner-deck-hits">
        ${hits.length
          ? html`<div class="list">
              ${hits.map((h) =>
                hitRow(h, {
                  onTune: (hit) => this._tuneHit(hit),
                  onCapture: (hit) => this._captureHit(hit),
                  onOpen: (hit) => this._openSignal(hit),
                })
              )}
            </div>`
          : html`<p class="empty">${this._emptyHitsText(state)}</p>`}
      </div>
    `;
  }

  _setupModal() {
    if (!this._setupOpen) return "";
    const close = () => (this._setupOpen = false);
    // START from the modal both forwards the config up (same as the deck's
    // own START) and closes the modal, per the v0.14.2 build spec.
    const forward = (type) => (ev) => this.dispatchEvent(new CustomEvent(type, { detail: ev.detail }));
    return html`
      <div class="overlay-backdrop" @click=${close}>
        <div
          class="panel"
          id="scanner-setup-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="scanner-setup-title"
          style="margin:60px auto 0;width:min(720px,92vw);max-height:80vh;overflow:auto;align-self:flex-start"
          @click=${(e) => e.stopPropagation()}
        >
          <div style="display:flex;align-items:center">
            <h2 id="scanner-setup-title" style="flex:1">Scanner setup</h2>
            <button title="close" aria-label="close scanner setup" @click=${close}>&times;</button>
          </div>
          <scanner-view
            mode="setup"
            .scan=${this.scan}
            .connected=${this.connected}
            .started=${this.started}
            .canControl=${this.canControl}
            .centerHz=${this.centerHz}
            .sampleRateHz=${this.sampleRateHz}
            .vfoHz=${this.vfoHz}
            .failure=${this.failure}
            @scan-start=${(ev) => {
              this.dispatchEvent(new CustomEvent("scan-start", { detail: ev.detail }));
              this._setupOpen = false;
            }}
            @scan-pause=${forward("scan-pause")}
            @scan-resume=${forward("scan-resume")}
            @scan-stop=${forward("scan-stop")}
            @scan-hold=${forward("scan-hold")}
            @scan-skip=${forward("scan-skip")}
            @tune-frequency=${forward("tune-frequency")}
            @tune-signal=${forward("tune-signal")}
            @star-hit=${forward("star-hit")}
            @dismiss-failure=${forward("dismiss-failure")}
          ></scanner-view>
        </div>
      </div>
    `;
  }

  /** Collapsed one-line primary control (U1): START/RESUME when idle/paused,
   * STOP once running/holding -- mirrors _controls' primary button exactly,
   * without the rest of the control row. */
  _collapsedPrimary(state) {
    const idle = state === "idle";
    const paused = state === "paused";
    const primaryDisabled = idle ? !this._ready : !paused;
    return html`<button
      class=${idle || paused ? "run-start" : "run-stop"}
      style="font-size:0.8em;padding:4px 8px"
      ?disabled=${primaryDisabled}
      @click=${() =>
        idle || paused ? this._primaryAction() : this.dispatchEvent(new CustomEvent("scan-stop"))}
    >
      ${idle ? "▶ START" : paused ? "▶ RESUME" : "■ STOP"}
    </button>`;
  }

  render() {
    const scan = this.scan ?? {};
    const state = scan.state ?? "idle";
    const isActive = state !== "idle";
    // Remembered per-deck (localStorage), but a running/holding/paused scan
    // always shows expanded regardless of that preference -- collapsing
    // again only takes effect once the scan goes idle.
    const expanded = this._expanded || isActive;
    const channel = scan.channel;
    // lifecycle case: a pass that tuned nothing because every channel is
    // ignored must say so, not look like a scan that finds nothing.
    const channelLabel = channel
      ? channel.label || `${(channel.frequency_hz / 1e6).toFixed(3)} MHz`
      : scan.all_ignored
        ? "all channels ignored"
        : "--";
    const isSpectrum = scan.config?.source?.type === "spectrum";
    const positionLabel = isSpectrum
      ? `cand ${scan.spectrum?.candidates?.length ?? 0}`
      : `${scan.position ?? 0}/${scan.total ?? 0}`;
    return html`
      <div class="scanner-deck" style="position:relative">
        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}
        <div class="scanner-deck-line">
          <span class="label">Scanner</span>
          <led-dot
            color="var(--accent)"
            .lit=${state === "running" || state === "holding"}
            class=${state === "holding" ? "led-blink" : ""}
          ></led-dot>
          <span class="mono">${state.toUpperCase()}</span>
          ${scan.reason ? html`<span class="dim" style="font-size:0.85em">${scan.reason}</span>` : ""}
          <span class="spacer"></span>
          ${expanded ? html`<span class="dim mono" style="font-size:0.85em">${this._sourceSummary()}</span>` : html`
            ${this._collapsedPrimary(state)}
            <button title="Scanner setup" aria-label="Scanner setup" @click=${() => (this._setupOpen = true)}>Scanner setup</button>
          `}
          <button
            title=${expanded ? "Collapse" : "Expand"}
            aria-expanded=${expanded}
            style="font-size:0.8em;padding:2px 6px"
            @click=${() => this._toggleExpanded()}
          >
            ${expanded ? "▾" : "▸"}
          </button>
        </div>
        ${expanded
          ? html`
              <div class="scanner-deck-line mono dim">
                <span>${channelLabel}${channel?.demodulator ? ` ${channel.demodulator}` : ""}</span>
                <span>${positionLabel}</span>
                ${this._snrBar(scan.snr_db, scan.config?.threshold_snr_db)}
              </div>
              ${this._controls(state)}
              <div class="scanner-deck-line">${this._lastHitLine(scan, state)}</div>
              ${this._hitsDropdown(scan, state)}
            `
          : ""}
      </div>
      ${this._setupModal()}
    `;
  }
}

customElements.define("scanner-deck", ScannerDeck);
