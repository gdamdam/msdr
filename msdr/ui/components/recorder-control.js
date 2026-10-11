import { LitElement, html } from "../vendor/lit.js";

/** Pre-roll IQ recorder (see docs/superpowers/specs/2026-09-23-preroll-recorder-design.md):
 * ARM keeps a rolling on-disk buffer from its own SDRconnect stream; REC
 * promotes it into a recording (still carrying the pre-roll) until STOP,
 * which finalizes it into SigMF. Rendered by instrument-panel.js in the
 * grouped recording area, gated there by state.features.recorder,
 * same convention as detect-readout's detectFeature.
 *
 * U1: the readout is factual -- actual buffered_s (not the configured
 * preroll_s target), an incoming-data health flag (data_ok/frames/dropped,
 * present once Stream A ships them; omitted silently until then), a
 * finalize_progress bar while finalizing, and a "saved <name>" recap with
 * OPEN FOLDER/ANALYZE once last_recording is set (until the next ARM). The
 * full history lives behind "recordings…" -> recordings-view.js. */

const LOW_DISK_GB = 10;

function mmss(seconds) {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function recordingName(path) {
  if (!path) return "";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** Capture-health metrics. Rendered by instrument-panel.js as a full-width
 * panel below the operations row (not inside this component) so opening it
 * never moves ARM/REC or Channel scan. */
export function renderCaptureHealth(ch) {
  return html`<section class="capture-health-panel" aria-label="Capture health, recorder detail">
    <h4 class="capture-health-heading">Capture health · recorder detail</h4>
    <dl class="capture-health-metrics">
      <div><dt>IQ received</dt><dd>${((ch.iq_throughput_bps ?? 0) / 1e6).toFixed(2)} MB/s</dd></div>
      <div><dt>Disk written</dt><dd>${((ch.disk_write_bps ?? 0) / 1e6).toFixed(2)} MB/s</dd></div>
      <div class=${(ch.dropped_samples ?? 0) > 0 ? "warn" : ""}><dt>Dropped</dt><dd>${ch.dropped_samples ?? 0}</dd></div>
      <div><dt>Backlog</dt><dd>${(ch.backlog_s ?? 0).toFixed(2)} s</dd></div>
      <div><dt>Remaining</dt><dd>${ch.remaining_recording_s == null ? "—" : mmss(ch.remaining_recording_s)}</dd></div>
      <span class="help-mark" tabindex="0" role="note" aria-label="About these figures"
        title="Rates measure received and written IQ. Remaining time estimates available disk above the reserve and capture limit."
        data-help="Rates measure received and written IQ. Remaining time estimates available disk above the reserve and capture limit.">?</span>
    </dl>
  </section>`;
}

export class RecorderControl extends LitElement {
  static properties = {
    status: { type: Object },
    disabled: { type: Boolean },
    healthOpen: { type: Boolean },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.status = null;
    this.disabled = false;
  }

  /** app.js's "R" keyboard shortcut -- REC while armed, STOP while recording,
   * a no-op otherwise (off/finalizing). */
  toggleRec() {
    const state = this.status?.state ?? "off";
    if (state === "recording") this.dispatchEvent(new CustomEvent("recorder-stop"));
    else if (state === "armed") this.dispatchEvent(new CustomEvent("recorder-rec"));
  }

  /** "· data ● ok" / "· data ✕ no samples" -- only while armed/recording, and
   * only once the backend actually ships `data_ok` (undefined/null degrades
   * to nothing rather than a false "no samples" alarm). */
  _dataHealth(s, show) {
    if (!show || s.data_ok === undefined || s.data_ok === null) return "";
    return s.data_ok
      ? html`<span class="dim mono" style="font-size:0.85em">&nbsp;· data ● ok</span>`
      : html`<span class="mono" style="font-size:0.85em;color:var(--danger)">&nbsp;· data ✕ no samples</span>`;
  }

  _diskNote(s) {
    const lowDisk = s.free_gb != null && s.free_gb < (s.min_free_gb ?? LOW_DISK_GB) * 1.5;
    return lowDisk ? ` · low disk (${s.free_gb.toFixed(1)} GB)` : s.free_gb != null ? ` · ${s.free_gb.toFixed(1)} GB free` : "";
  }

  _readout(s) {
    if (s.error) return html`<span class="dim mono" style="font-size:0.85em">${s.error}</span>`;
    if (s.state === "finalizing") {
      return html`<span class="dim mono" style="font-size:0.85em">finalizing…</span>`;
    }
    if (s.state === "recording") {
      const dest = s.root ? `${s.root}/recordings/…` : "…";
      return html`<span class="dim mono" style="font-size:0.85em">${dest}</span
      >${this._dataHealth(s, true)}<span class="dim mono" style="font-size:0.85em">${this._diskNote(s)}</span>`;
    }
    if (s.state === "armed") {
      return html`<span class="dim mono" style="font-size:0.85em"
        >buffered ${mmss(s.buffered_s)} of ${mmss(s.preroll_s)}</span
      >${this._dataHealth(s, true)}<span class="dim mono" style="font-size:0.85em">${this._diskNote(s)}</span>`;
    }
    if (s.last_recording) {
      const name = recordingName(s.last_recording);
      return html`
        <span class="dim mono" style="font-size:0.85em">saved ${name}</span>
        <button
          style="font-size:0.8em;padding:4px 8px"
          @click=${() => this.dispatchEvent(new CustomEvent("recorder-reveal", { detail: { name } }))}
        >
          OPEN FOLDER
        </button>
        <button
          style="font-size:0.8em;padding:4px 8px"
          @click=${() => this.dispatchEvent(new CustomEvent("recorder-analyze", { detail: { name } }))}
        >
          ANALYZE
        </button>
      `;
    }
    return "";
  }

  _statusChip(s) {
    if (s.state === "recording") {
      return html`<span class="chip record-chip rec">Recorder · REC ${mmss(s.recording_s)}</span>`;
    }
    if (s.state === "armed") {
      return html`<span class="chip record-chip armed">Recorder · ARMED</span>`;
    }
    if (s.state === "finalizing") {
      return html`<span class="chip record-chip">Recorder · SAVING…</span>`;
    }
    return html`<span class="chip record-chip">Recorder · idle</span>`;
  }

  /** finalize_progress (0..1) as a bar -- reuses scanner-deck's SNR-bar
   * track/fill classes (generic layout, not scanner-specific) rather than
   * adding new CSS for a second progress bar. */
  _progressBar(progress) {
    const pct = progress != null ? Math.round(Math.max(0, Math.min(1, progress)) * 100) : null;
    return html`
      <div class="scanner-deck-snr" style="margin-top:6px;max-width:320px">
        <div class="scanner-deck-snr-track">
          <div class="scanner-deck-snr-fill" style="width:${pct ?? 0}%;background:var(--accent)"></div>
        </div>
        <span class="dim mono" style="font-size:0.8em">${pct != null ? `${pct}%` : "…"}</span>
      </div>
    `;
  }

  /** Explain why ARM is unavailable when SDRconnect is stopped. */
  _hintText() {
    if (this.disabled) return "Press Play in SDRconnect";
    return "";
  }

  render() {
    const s = this.status ?? { state: "off", armed: false, preroll_s: 300 };
    const armed = s.state === "armed";
    const recording = s.state === "recording";
    const finalizing = s.state === "finalizing";
    const canArm = !this.disabled && !recording && !finalizing;
    const canRec = !this.disabled && (armed || recording);
    const hint = this._hintText();

    return html`
      <div id="recorder-control">
        <div class="control-row" style="align-items:center">
          ${this._statusChip(s)}
          <button
            id="recorder-arm-button"
            title="Arm pre-roll buffer"
            aria-pressed=${armed || recording || finalizing}
            ?disabled=${!canArm}
            style=${armed || recording || finalizing
              ? "color:var(--accent);border-color:var(--accent)"
              : ""}
            @click=${() =>
              this.dispatchEvent(new CustomEvent("recorder-arm", { detail: { on: !armed } }))}
          >
            ARM
          </button>
          <button
            id="recorder-rec-button"
            title="REC / STOP (R)"
            class=${recording ? "run-stop" : "run-rec"}
            ?disabled=${!canRec}
            @click=${() => this.dispatchEvent(new CustomEvent(recording ? "recorder-stop" : "recorder-rec"))}
          >
            ${recording ? "■ STOP" : "● REC"}
          </button>
          ${this._readout(s)}
          <button
            style="font-size:0.8em;padding:4px 8px"
            title="Browse recordings"
            @click=${() => this.dispatchEvent(new CustomEvent("open-recordings"))}
          >
            Recordings
          </button>
          ${hint ? html`<span class="hint">${hint}</span>` : ""}
        </div>
        ${s.recoverable_count ? html`<p role="status">${s.recoverable_count} interrupted recording(s).
          Open Recordings to repair the retained IQ segments.</p>` : ""}
        ${s.capture_health ? html`<button type="button" class="capture-health-toggle"
          aria-expanded=${this.healthOpen ? "true" : "false"}
          title="Recorder capture health: rates, dropped samples, backlog"
          @click=${() => this.dispatchEvent(new CustomEvent("capture-health-toggle", { bubbles: true, detail: { open: !this.healthOpen } }))}
        >Capture health</button>` : ""}
        ${finalizing ? this._progressBar(s.finalize_progress) : ""}
      </div>
    `;
  }
}

customElements.define("recorder-control", RecorderControl);
