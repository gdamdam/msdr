import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import { mhz } from "../format.js";
import "./failure-note.js";

// Statuses per FindRepo (msdr/storage, Stream B): "new" is the default a
// logged hit lands in; ALL (null) is no filter.
const STATUS_FILTERS = [
  { value: null, label: "ALL" },
  { value: "new", label: "NEW" },
  { value: "reviewed", label: "REVIEWED" },
  { value: "saved", label: "SAVED" },
  { value: "ignored", label: "IGNORED" },
];

// Same glyph conventions as survey-view.js's badge lane/labels table (S2-S4)
// -- a find's `classification` is the same shape a survey label carries
// (activity/bw_class/modulation/family/voice/confirmed/bandplan), see the
// plan's Contracts section.
const ACTIVITY_GLYPHS = { steady: "●", bursty: "▮▮", periodic: "↻", intermittent: "◌", new: "✦" };
const VOICE_GLYPHS = { voice: "🗣", data: "⋯" };

/** "POCSAG1200 / AX.25 AFSK" -> "POCSAG1200" -- mirrors survey-view's
 * shortFamilyName; families.py joins alternatives with " / ". */
function shortFamilyName(name) {
  if (!name) return "";
  return name.split(" / ")[0];
}

/** Epoch seconds -> "3m ago" / "2h ago" / "5d ago"; null -> "--". */
function agoText(epochS) {
  if (epochS === null || epochS === undefined) return "--";
  const s = Math.max(0, Math.round(Date.now() / 1000 - epochS));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const STATUS_COLOR = {
  new: "var(--accent)",
  reviewed: "var(--ink)",
  saved: "var(--ok)",
  ignored: "var(--ink)",
};

const POLL_MS = 15_000;

/** FINDS section: the scanner's finds log (every logged hit, grouped by
 * frequency across runs -- see docs/superpowers/plans/2026-09-25-scanner-finds.md).
 * Self-fetches its list/counts (GET /api/finds), same pattern as
 * beacons-view's hits table -- `lastFind` is app.js's forwarded "find" ws
 * push (`{find, created}`, wrapped fresh on every message so a repeat find
 * still re-triggers `updated()`), merged in locally so a live hit doesn't
 * wait for the next poll. Row actions call their routes directly and
 * refresh the list, dispatching the shared "action"/"api-error" events for
 * the footer/failure-note (see app.js's `_sectionNode` "finds" case). Clips
 * are out of scope for this round -- see the footer line. */
export class FindsView extends LitElement {
  static properties = {
    lastFind: { type: Object },
    failure: { type: Object },
    _finds: { state: true },
    _counts: { state: true },
    _status: { state: true },
    _query: { state: true },
    _busyId: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.lastFind = null;
    this.failure = null;
    this._finds = [];
    this._counts = {};
    this._status = null;
    this._query = "";
    this._busyId = null;
    this._pollTimer = null;
    this._queryTimer = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._load();
    this._pollTimer = setInterval(() => this._load(), POLL_MS);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._pollTimer);
    clearTimeout(this._queryTimer);
  }

  updated(changed) {
    if (changed.has("lastFind") && this.lastFind) this._mergeFind(this.lastFind);
  }

  async _load() {
    // A status/query change races the poll; only the latest request may write.
    const seq = (this._reqSeq = (this._reqSeq || 0) + 1);
    try {
      const { finds, counts } = await api.listFinds({
        status: this._status ?? undefined,
        q: this._query || undefined,
        limit: 200,
      });
      if (seq !== this._reqSeq) return;
      this._finds = finds ?? [];
      this._counts = counts ?? {};
    } catch {
      // route not there yet / feature off -- keep whatever we already have
    }
  }

  /** Merges a `{find, created}` ws push into the currently-shown list:
   * updates an existing row in place regardless of filter (its seen_count/
   * last_ts/etc changed), inserts a brand-new one only when it matches the
   * active status filter and text query -- a row that doesn't match just shows up on the
   * next poll/filter change. `counts` mirrors the server's per-status
   * totals, so only a first-time create bumps a bucket (a merge leaves
   * status, hence its bucket, unchanged per FindRepo.add_or_merge). */
  _mergeFind({ find, created }) {
    if (!find) return;
    const list = [...this._finds];
    const idx = list.findIndex((f) => f.id === find.id);
    if (idx >= 0) list[idx] = find;
    else if (
      (this._status == null || this._status === find.status) &&
      // the server's q is a case-insensitive substring match on label
      (!this._query || (find.label ?? "").toLowerCase().includes(this._query.toLowerCase()))
    )
      list.unshift(find);
    this._finds = list;
    if (created) {
      this._counts = { ...this._counts, [find.status]: (this._counts[find.status] ?? 0) + 1 };
    }
  }

  _setStatusFilter(value) {
    if (value === this._status) return;
    this._status = value;
    this._load();
  }

  _onQueryInput(e) {
    this._query = e.target.value;
    clearTimeout(this._queryTimer);
    this._queryTimer = setTimeout(() => this._load(), 300);
  }

  /** Shared action wrapper: runs `fn` (the api call), reloads the list on
   * success, and dispatches the same "action"/"api-error" events beacons-view
   * uses for its own CLEAR -- app.js's "finds" section case routes these to
   * the footer/failure-note, same as every other panel. */
  async _act(id, message, fn) {
    this._busyId = id;
    try {
      await fn();
      await this._load();
      this.dispatchEvent(new CustomEvent("action", { detail: { message } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._busyId = null;
    }
  }

  _tune(f) {
    const label = f.label || mhz(f.frequency_hz);
    this._act(f.id, `tuned ${label}`, () => api.tuneFind(f.id));
  }

  _promote(f) {
    const label = f.label || mhz(f.frequency_hz);
    this._act(f.id, `promoted ${label}`, () => api.promoteFind(f.id, null));
  }

  _setStatus(f, status) {
    const label = f.label || mhz(f.frequency_hz);
    this._act(f.id, `${label} ${status}`, () => api.patchFind(f.id, { status }));
  }

  _delete(f) {
    const label = f.label || mhz(f.frequency_hz);
    if (!window.confirm(`Delete find ${label}?`)) return;
    this._act(f.id, `deleted ${label}`, () => api.deleteFind(f.id));
  }

  _renderClassification(c) {
    if (!c) return html`<span class="dim">--</span>`;
    const glyph = ACTIVITY_GLYPHS[c.activity?.kind] ?? "";
    const family = c.family?.name ? shortFamilyName(c.family.name) : (c.modulation?.kind ?? null);
    const voiceGlyph = VOICE_GLYPHS[c.voice?.kind] ?? null;
    return html`
      <span
        class="mono"
        style="font-size:0.85em;white-space:nowrap"
        title=${c.bandplan?.name ? `band plan: ${c.bandplan.name}` : ""}
      >
        ${glyph ? `${glyph} ` : ""}${c.bw_class ?? "--"}${family ? ` · ${family}` : ""}${
          voiceGlyph ? ` ${voiceGlyph}` : ""
        }${c.confirmed ? " ✓" : ""}${c.bandplan?.name ? ` · ${c.bandplan.name}` : ""}
      </span>
    `;
  }

  _renderStatusBadge(status) {
    const color = STATUS_COLOR[status] ?? "var(--ink)";
    return html`
      <span
        class="mono"
        style="font-size:0.8em;padding:1px 6px;border:1px solid var(--line);border-radius:3px;color:${color}"
      >
        ${status ?? "--"}
      </span>
    `;
  }

  /** U3: opens the signal-detail panel -- the row itself, since a find row
   * has no other click behavior of its own (only the per-cell action
   * buttons, which stopPropagation below so this doesn't double-fire). */
  _openSignal(f) {
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: f.frequency_hz, find_id: f.id, signal_id: f.signal_id ?? undefined },
        bubbles: true,
        composed: true,
      })
    );
  }

  _renderRow(f) {
    const busy = this._busyId === f.id;
    return html`
      <tr style="cursor:pointer" @click=${() => this._openSignal(f)}>
        <td>${mhz(f.frequency_hz)}</td>
        <td>seen × ${f.seen_count ?? 1}</td>
        <td>${agoText(f.first_ts)} / ${agoText(f.last_ts)}</td>
        <td>${f.peak_snr_db != null ? f.peak_snr_db.toFixed(1) : "--"}</td>
        <td>${f.demodulator ?? "--"}${f.bandwidth_hz ? ` ${(f.bandwidth_hz / 1000).toFixed(1)}k` : ""}</td>
        <td>${this._renderClassification(f.classification)}</td>
        <td>${this._renderStatusBadge(f.status)}</td>
        <td class="dim">${f.note ?? ""}</td>
        <td style="white-space:nowrap" @click=${(ev) => ev.stopPropagation()}>
          <button style="font-size:0.8em;padding:2px 6px" ?disabled=${busy} @click=${() => this._tune(f)}>
            TUNE
          </button>
          <button style="font-size:0.8em;padding:2px 6px" ?disabled=${busy} @click=${() => this._promote(f)}>
            PROMOTE
          </button>
          <button
            style="font-size:0.8em;padding:2px 6px"
            ?disabled=${busy}
            @click=${() => this._setStatus(f, "reviewed")}
          >
            REVIEWED
          </button>
          <button
            style="font-size:0.8em;padding:2px 6px"
            ?disabled=${busy}
            @click=${() => this._setStatus(f, "ignored")}
          >
            IGNORE
          </button>
          <button
            style="font-size:0.8em;padding:2px 6px"
            title="Delete"
            aria-label="delete ${mhz(f.frequency_hz)}"
            ?disabled=${busy}
            @click=${() => this._delete(f)}
          >
            ✕
          </button>
        </td>
      </tr>
    `;
  }

  _renderControls() {
    const total = Object.values(this._counts ?? {}).reduce((a, b) => a + b, 0);
    return html`
      <div class="control-row" style="flex-wrap:wrap;row-gap:4px">
        ${STATUS_FILTERS.map(({ value, label }) => {
          const count = value == null ? total : (this._counts?.[value] ?? 0);
          return html`
            <button
              class="mono"
              aria-pressed=${this._status === value}
              @click=${() => this._setStatusFilter(value)}
            >
              ${label} (${count})
            </button>
          `;
        })}
        <span class="spacer"></span>
        <input
          type="search"
          class="mono"
          style="width:140px"
          placeholder="search…"
          aria-label="search finds"
          .value=${this._query}
          @input=${(e) => this._onQueryInput(e)}
        />
      </div>
    `;
  }

  render() {
    const finds = this._finds ?? [];
    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Scanner detections</h2>
        </div>
        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}
        ${this._renderControls()}
        <div class="lib-table-wrap" style="margin-top:8px">
          <table class="lib-table mono">
            <thead>
              <tr>
                <th>MHz</th>
                <th>Seen</th>
                <th>First / Last</th>
                <th>Peak SNR</th>
                <th>Mode/BW</th>
                <th>Classification</th>
                <th>Status</th>
                <th>Note</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${finds.length
                ? finds.map((f) => this._renderRow(f))
                : html`<tr>
                    <td colspan="9" class="empty">No finds — enable Log finds in Scanner setup and start a scan</td>
                  </tr>`}
            </tbody>
          </table>
        </div>
        <div class="dim mono" style="font-size:0.85em;margin-top:6px">clips: not recorded (later option)</div>
      </div>
    `;
  }
}

customElements.define("finds-view", FindsView);
