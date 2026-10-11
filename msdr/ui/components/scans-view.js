import { LitElement, html } from "../vendor/lit.js";
import { mhz, bw, sampleRate, contrastDb, dateTime } from "../format.js";
import { getScan, scanReportUrl, scanClipUrl, runScan, scanJobs } from "../api.js";
import "./led-dot.js";

const JOBS_POLL_MS = 3000;

/** Server-side path's last segment, for a compact jobs-list label. */
function basename(path) {
  return String(path ?? "").split(/[\\/]/).pop() || path;
}

/** started_at/finished_at (ISO) -> elapsed "MM:SS", ticking against `now` for
 * a still-running job. */
function elapsedLabel(job, now) {
  const start = Date.parse(job.started_at ?? "");
  if (Number.isNaN(start)) return "--:--";
  const endRaw = job.finished_at ? Date.parse(job.finished_at) : now;
  const end = Number.isNaN(endRaw) ? now : endRaw;
  const secs = Math.max(0, Math.round((end - start) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const JOB_LED_COLOR = { done: "var(--ok)", failed: "var(--danger)" };

export class ScansView extends LitElement {
  static properties = {
    scans: { type: Object }, // { scans: [ScanSummary...], root } or { ..., error }
    _collapsed: { state: true },
    _openId: { state: true },
    _loadingId: { state: true },
    _events: { state: true },
    _scanPath: { state: true },
    _scanRate: { state: true },
    _scanCenter: { state: true },
    _scanThreshold: { state: true },
    _scanFormat: { state: true },
    _scanChannelClips: { state: true },
    _scanRedetect: { state: true },
    _submitting: { state: true },
    _jobs: { state: true },
    _explorerOpen: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.scans = null;
    this._collapsed = true;
    this._openId = null;
    this._loadingId = null;
    this._events = {};
    this._scanPath = "";
    this._scanRate = "";
    this._scanCenter = "";
    this._scanThreshold = "";
    this._scanFormat = "";
    this._scanChannelClips = false;
    this._scanRedetect = "";
    this._submitting = false;
    this._jobs = [];
    this._jobsSeeded = false; // first successful poll only seeds _jobs
    this._jobsTimer = null;
    this._pendingExpand = null;
    this._explorerOpen = false;
  }

  connectedCallback() {
    super.connectedCallback();
    this._pollJobs();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearTimeout(this._jobsTimer);
  }

  /** `scans` (the list itself) is replaced wholesale on every reload -- the
   * per-scan events cache would otherwise go stale silently, so drop it and,
   * if a row is expanded, refetch its events right away rather than waiting
   * for a close/reopen. Also resolves a pending "expand the new scan" once
   * the just-completed job's scan shows up in the refreshed list. */
  updated(changed) {
    if (!changed.has("scans")) return;
    this._events = {};
    if (this._pendingExpand) {
      const found = (this.scans?.scans ?? []).some((s) => s.id === this._pendingExpand);
      if (found) {
        this._openId = this._pendingExpand;
        this._collapsed = false;
      }
      this._pendingExpand = null;
    }
    if (this._openId != null) this._loadEvents(this._openId);
  }

  /** Self-scheduling: keeps polling every 3s only while a job is queued or
   * running, so it goes quiet once nothing is in flight. A completed job
   * with a scan_id asks app.js to reload the scans list (scan-completed),
   * and updated() expands it once that list arrives. */
  async _pollJobs() {
    clearTimeout(this._jobsTimer);
    const prev = this._jobs;
    try {
      const res = await scanJobs();
      this._jobs = res.jobs ?? [];
      // On the first poll after mount `prev` is empty, so jobs that finished
      // before we looked would all read as newly completed.
      const seeded = this._jobsSeeded;
      this._jobsSeeded = true;
      for (const job of seeded ? this._jobs : []) {
        const before = prev.find((j) => j.job_id === job.job_id);
        if (job.status === "done" && job.scan_id && before?.status !== "done") {
          this._pendingExpand = job.scan_id;
          this.dispatchEvent(new CustomEvent("scan-completed"));
        }
      }
    } catch {
      // route not landed yet or transient failure -- degrade quietly, retried
      // on the next RUN submission
      return;
    }
    const active = this._jobs.some((j) => j.status === "queued" || j.status === "running");
    if (active) this._jobsTimer = setTimeout(() => this._pollJobs(), JOBS_POLL_MS);
  }

  async _runScan() {
    const path = this._scanPath.trim();
    const redetect = this._scanRedetect.trim();
    if (!path && !redetect) return;
    this._submitting = true;
    try {
      // Exactly one of path/redetect -- see AppService.run_scan.
      const body = redetect ? { redetect } : { path };
      if (this._scanRate) body.sample_rate = Number(this._scanRate) * 1_000_000;
      if (this._scanCenter) body.center_hz = Number(this._scanCenter) * 1_000_000;
      if (this._scanThreshold) body.threshold = Number(this._scanThreshold);
      if (this._scanFormat) body.format = this._scanFormat;
      if (this._scanChannelClips) body.channel_clips = true;
      await runScan(body);
      this._scanPath = "";
      this._scanRate = "";
      this._scanCenter = "";
      this._scanThreshold = "";
      this._scanFormat = "";
      this._scanChannelClips = false;
      this._scanRedetect = "";
      this._pollJobs();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._submitting = false;
    }
  }

  async _copyExplorerCommand(root) {
    const command = `iqscan --serve --scan-root ${root}`;
    try {
      await navigator.clipboard.writeText(command);
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "command copied" } }));
    } catch {
      // clipboard API unavailable/denied -- the command is still shown inline to copy by hand
    }
  }

  async _loadEvents(id) {
    this._loadingId = id;
    try {
      const res = await getScan(id);
      this._events = { ...this._events, [id]: res.events ?? [] };
    } catch (err) {
      if (this._openId === id) this._openId = null;
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._loadingId = null;
    }
  }

  async _toggleScan(scan) {
    if (this._openId === scan.id) {
      this._openId = null;
      return;
    }
    this._openId = scan.id;
    if (this._events[scan.id]) return;
    await this._loadEvents(scan.id);
  }

  _tuneEvent(hz) {
    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
  }

  _starEvent(ev, scan, event) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("star-event", { detail: { scan, event } }));
  }

  render() {
    const result = this.scans ?? {};
    const list = result.scans ?? [];
    const error = result.error;
    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Scans</h2>
          <button @click=${() => (this._collapsed = !this._collapsed)}>
            ${this._collapsed ? "▾" : "▴"} ${list.length} scans
          </button>
        </div>
        ${this._collapsed
          ? ""
          : html`
              <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
                ${this._runForm()} ${this._explorerHint(result.root)} ${this._jobsList()}
                ${error
                  ? html`<div class="row" style="cursor:default">
                      <p class="empty" style="margin:0">${error}</p>
                      <span class="spacer"></span>
                      <button @click=${() => this.dispatchEvent(new CustomEvent("setup"))}>OPEN SETUP</button>
                    </div>`
                  : list.length
                    ? html`<div class="list">${list.map((s) => this._scanRow(s))}</div>`
                    : html`<p class="empty">no scans</p>`}
              </div>
            `}
      </div>
    `;
  }

  _runForm() {
    const canSubmit = this._scanPath.trim() || this._scanRedetect.trim();
    return html`
      <div style="display:flex;flex-direction:column;gap:6px;border-bottom:1px solid var(--line);padding-bottom:8px">
        <div class="label">Run Scan</div>
        <input
          name="scan-path"
          placeholder="IQ file path (.cu8, .cf32_be, .sigmf-meta, ...)"
          .value=${this._scanPath}
          @input=${(e) => (this._scanPath = e.target.value)}
        />
        <input
          name="scan-redetect"
          class="mono"
          placeholder="or: redetect from scan id (rerun detection, no new file)"
          .value=${this._scanRedetect}
          @input=${(e) => (this._scanRedetect = e.target.value)}
        />
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <input
            name="scan-rate"
            class="mono"
            style="width:100px"
            placeholder="rate MS/s"
            .value=${this._scanRate}
            @input=${(e) => (this._scanRate = e.target.value)}
          />
          <input
            name="scan-center"
            class="mono"
            style="width:100px"
            placeholder="centre MHz"
            .value=${this._scanCenter}
            @input=${(e) => (this._scanCenter = e.target.value)}
          />
          <input
            name="scan-threshold"
            class="mono"
            style="width:100px"
            placeholder="threshold dB"
            .value=${this._scanThreshold}
            @input=${(e) => (this._scanThreshold = e.target.value)}
          />
          <select name="scan-format" @change=${(e) => (this._scanFormat = e.target.value)}>
            <option value="" ?selected=${this._scanFormat === ""}>auto format</option>
            <option value="cu8" ?selected=${this._scanFormat === "cu8"}>cu8</option>
            <option value="cf32_be" ?selected=${this._scanFormat === "cf32_be"}>cf32_be</option>
          </select>
          <label style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            <input
              type="checkbox"
              .checked=${this._scanChannelClips}
              @change=${(e) => (this._scanChannelClips = e.target.checked)}
            />
            channel clips
          </label>
          <button ?disabled=${this._submitting || !canSubmit} @click=${() => this._runScan()}>
            RUN
          </button>
        </div>
      </div>
    `;
  }

  // iqscan itself must run the explorer server -- msdr only ever shows the
  // exact command (against this deployment's iqscan_scan_root) to copy and
  // run in a terminal.
  _explorerHint(root) {
    if (!root) return "";
    const command = `iqscan --serve --scan-root ${root}`;
    return html`
      <div class="row" style="cursor:default;flex-direction:column;align-items:stretch;gap:4px">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="label" style="flex:1">iqscan live explorer</span>
          <button @click=${() => (this._explorerOpen = !this._explorerOpen)}>
            ${this._explorerOpen ? "▴" : "▾"}
          </button>
        </div>
        ${this._explorerOpen
          ? html`<div style="display:flex;gap:8px;align-items:center">
              <code class="mono" style="flex:1;overflow-wrap:anywhere">${command}</code>
              <button title="Copy command" @click=${() => this._copyExplorerCommand(root)}>⧉</button>
            </div>`
          : ""}
      </div>
    `;
  }

  _jobsList() {
    if (!this._jobs.length) return "";
    return html`<div class="list">${this._jobs.map((j) => this._jobRow(j))}</div>`;
  }

  _jobRow(job) {
    const now = Date.now();
    const color = JOB_LED_COLOR[job.status] ?? "var(--accent)";
    return html`
      <div class="row dim" style="flex-direction:column;align-items:stretch;gap:2px">
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot color=${color} .lit=${true} class=${job.status === "running" ? "led-blink" : ""}></led-dot>
          <span class="mono">${basename(job.path)}</span>
          <span class="spacer"></span>
          <span class="label">${job.status}</span>
        </div>
        <div class="mono dim" style="padding-left:16px">
          ${elapsedLabel(job, now)}${job.error ? html` — ${job.error}` : ""}
        </div>
      </div>
    `;
  }

  // Rows stack two lines: the column is narrow (a third of the grid) and a
  // single line of mono fields wraps mid-word instead of overflowing, which
  // reads worse than a deliberate two-line row.
  _scanRow(scan) {
    const open = this._openId === scan.id;
    const events = this._events[scan.id] ?? [];
    return html`
      <div>
        <div class="row" style="flex-direction:column;align-items:stretch;gap:2px" @click=${() => this._toggleScan(scan)}>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="mono">${mhz(scan.center_frequency_hz)}</span>
            <span class="spacer"></span>
            ${scan.has_report
              ? html`<a
                  href=${scanReportUrl(scan.id, "report.html")}
                  target="_blank"
                  rel="noopener"
                  @click=${(ev) => ev.stopPropagation()}
                  >REPORT</a
                >`
              : ""}
          </div>
          <div class="mono dim" style="display:flex;align-items:center;gap:8px">
            <span>${dateTime(scan.created)}</span>
            <span>${sampleRate(scan.sample_rate)}</span>
            <span>${scan.event_count} events</span>
          </div>
        </div>
        ${open
          ? this._loadingId === scan.id
            ? html`<p class="empty">loading…</p>`
            : html`<div class="list" style="margin-left:16px">
                ${events.map((e) => this._eventRow(scan, e))}
              </div>`
          : ""}
      </div>
    `;
  }

  _eventRow(scan, event) {
    return html`
      <div
        class="row dim"
        style="flex-direction:column;align-items:stretch;gap:2px"
        @click=${() => this._tuneEvent(event.frequency_hz)}
      >
        <div style="display:flex;align-items:center;gap:8px">
          <led-dot .lit=${false}></led-dot>
          <span class="mono">${mhz(event.frequency_hz)}</span>
          <span class="spacer"></span>
          <button
            title="Save as signal"
            style="font-size:0.8em;padding:2px 8px"
            @click=${(ev) => this._starEvent(ev, scan, event)}
          >
            ★
          </button>
        </div>
        <div class="mono dim" style="display:flex;align-items:center;gap:8px;padding-left:16px">
          <span>${bw(event.bandwidth_hz)}</span>
          <span>${contrastDb(event.contrast_db)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding-left:16px;flex-wrap:wrap">
          <span>${event.kind}</span>
          ${event.hints?.length ? html`<span class="dim">${event.hints.join(" · ")}</span>` : ""}
          ${event.protocol
            ? html`<span title=${event.protocol_status ?? ""}>${event.protocol}${event.protocol_status && event.protocol_status !== "confirmed" ? "?" : ""}</span>`
            : ""}
        </div>
        ${event.protocol_evidence?.length
          ? html`<div class="dim" style="padding-left:16px;font-size:0.85em">
              ${event.protocol_evidence.join(" · ")}
            </div>`
          : ""}
        ${event.channel_clip
          ? html`<div style="padding-left:16px">
              <a
                href=${scanClipUrl(scan.id, event.channel_clip)}
                download
                title="Filtered, centered raw IQ (cf32) -- not playable audio; open in inspectrum/GNU Radio"
                @click=${(ev) => ev.stopPropagation()}
                >⬇ channel clip (cf32)</a
              >
            </div>`
          : ""}
      </div>
    `;
  }
}

customElements.define("scans-view", ScansView);
