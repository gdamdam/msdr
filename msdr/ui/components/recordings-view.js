import { LitElement, html } from "../vendor/lit.js";
import { mhz, sampleRate, dateTime } from "../format.js";
import * as api from "../api.js";
import { recordingHealth, formatUnknown } from "../recording-health.js";
import "./failure-note.js";

function mmss(seconds) {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fileSize(bytes) {
  if (bytes == null) return "--";
  const gb = bytes / 1e9;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

/** RECORDINGS view (U1 item 4): a modal opened from recorder-control.js's
 * "recordings…" link (forwarded through instrument-panel.js), listing every
 * finished pre-roll recorder capture under the recorder's root (GET
 * /api/recordings) with per-row OPEN FOLDER (reveal)/ANALYZE (iqscan)/DELETE
 * (confirm) -- same open-boolean + overlay-backdrop modal pattern as
 * decode-view.js, table styling reused from the LIBRARY view (.lib-table). */
export class RecordingsView extends LitElement {
  static properties = {
    open: { type: Boolean },
    _recordings: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _confirmDelete: { state: true },
    _busy: { state: true },
    _recoverable: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this._recordings = [];
    this._recoverable = [];
    this._loading = false;
    this._error = null;
    this._confirmDelete = null;
    this._busy = null;
  }

  updated(changed) {
    if (changed.has("open") && this.open) this._load();
  }

  async _load() {
    this._loading = true;
    try {
      [this._recordings, this._recoverable] = await Promise.all([api.listRecordings(), api.fetchJson("/api/recorder/recovery")]);
      this._error = null;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  async _repair(name) {
    this._busy = name;
    try {
      await api.fetchJson(`/api/recorder/recovery/${encodeURIComponent(name)}`, {method: "POST"});
      await this._load();
    } catch (e) { this._error = e.message; }
    finally { this._busy = null; }
  }

  _close() {
    this._confirmDelete = null;
    this.dispatchEvent(new CustomEvent("close"));
  }

  /** ▶ REVIEW: app.js switches to the review workspace and
   * starts the replay itself through the existing _doSurveyReplay, same as
   * survey-view.js's own REPLAY picker -- this only names the file. */
  _review(r) {
    this.dispatchEvent(
      new CustomEvent("open-review", { bubbles: true, composed: true, detail: { name: r.name, path: r.path } }),
    );
  }

  async _reveal(name) {
    this._busy = name;
    try {
      const res = await api.revealRecording(name);
      if (res && res.ok === false) this._error = res.error || res.path || "could not open folder";
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = null;
    }
  }

  async _analyze(name) {
    this._busy = name;
    try {
      await api.analyzeRecording(name);
      this.dispatchEvent(new CustomEvent("action", { detail: { message: `analyzing ${name}` } }));
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = null;
    }
  }

  async _delete(name) {
    this._busy = name;
    try {
      await api.deleteRecording(name);
      this._confirmDelete = null;
      await this._load();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = null;
    }
  }

  /** Actions for one recording; delete asks for confirmation in place. */
  _actions(r, damaged) {
    const busy = this._busy === r.name;
    if (this._confirmDelete === r.name) {
      return html`<span class="dim">Delete this recording?</span>
        <button class="rec-danger" ?disabled=${busy} @click=${() => this._delete(r.name)}>Delete</button>
        <button ?disabled=${busy} @click=${() => (this._confirmDelete = null)}>Cancel</button>`;
    }
    return html`
      ${damaged ? "" : html`<button ?disabled=${busy} title="Switch to REVIEW and start replaying this recording"
        @click=${() => this._review(r)}>▶ Review</button>
        <button ?disabled=${busy} @click=${() => this._analyze(r.name)}>Analyze</button>`}
      <button ?disabled=${busy} @click=${() => this._reveal(r.name)}>Open folder</button>
      <button class="rec-danger" ?disabled=${busy} @click=${() => (this._confirmDelete = r.name)}>Delete</button>`;
  }

  /** One recording as a two-line entry: name and time, then the facts.
   * A list (not a wide table) so nothing overflows the modal. */
  _row(r) {
    const health = recordingHealth(r);
    const created = r.created ? dateTime(new Date(r.created * 1000).toISOString()) : "unknown date";
    const facts = health.damaged
      ? html`<span class="rec-problem">Damaged: ${health.reason}</span>`
      : html`
          <span>${r.center_hz != null ? mhz(r.center_hz) : "-- MHz"}</span>
          <span>${r.sample_rate != null ? sampleRate(r.sample_rate) : "rate unknown"}</span>
          <span>${formatUnknown(r.duration_s, mmss)}</span>
          <span>${fileSize(r.size_bytes)}</span>
          <span>${formatUnknown(r.gaps, (g) => `${g} gap${g === 1 ? "" : "s"}`)}</span>`;
    return html`<li class="rec-item${health.damaged ? " rec-item--damaged" : ""}">
      <div class="rec-main">
        <div class="rec-name mono" title=${r.name}>${r.name}</div>
        <div class="rec-facts mono">${facts}</div>
      </div>
      <div class="rec-side">
        <span class="rec-created dim">${created}</span>
        <div class="rec-actions">${this._actions(r, health.damaged)}</div>
      </div>
    </li>`;
  }

  render() {
    if (!this.open) return html``;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="recordings-view-title"
          style="margin:40px auto 0;width:min(880px,94vw);max-height:85vh;overflow:auto;align-self:flex-start;display:flex;flex-direction:column;gap:10px"
          @click=${(ev) => ev.stopPropagation()}
        >
          <div style="display:flex;align-items:center">
            <h2 id="recordings-view-title" style="flex:1">Recordings</h2>
            <button title="close" aria-label="close recordings" @click=${() => this._close()}>&times;</button>
          </div>
          ${this._error
            ? html`<failure-note
                .failure=${{ subject: "recordings", status: "error", detail: this._error }}
                @dismiss=${() => (this._error = null)}
              ></failure-note>`
            : ""}
          ${this._recoverable.length ? html`<section aria-label="Interrupted recordings">
            <h3>Interrupted recordings</h3><p>Repair retained segments into a verified recording.</p>
            ${this._recoverable.map(r => html`<p class="mono">${r.name} · ${r.segments} segments · ${fileSize(r.bytes)}
              <button ?disabled=${this._busy != null} @click=${() => this._repair(r.name)}>Repair</button></p>`)}
          </section>` : ""}
          ${this._loading
            ? html`<p class="empty">loading…</p>`
            : this._recordings.length
              ? html`
                  <ul class="rec-list" aria-label="Recordings">
                    ${this._recordings.map((r) => this._row(r))}
                  </ul>
                `
              : html`<p class="empty">No recordings yet — REC from the instrument panel to make one</p>`}
        </div>
      </div>
    `;
  }
}

customElements.define("recordings-view", RecordingsView);
