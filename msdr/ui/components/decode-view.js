import { LitElement, html } from "../vendor/lit.js";
import { mhz, epochHM, callLineText, offsetColorVar, offsetText, integrityBadge } from "../format.js";
import * as api from "../api.js";
import "./failure-note.js";
import "./fax-viewer.js";
import "./cw-transcript.js";

import { trapModalTab, containModalFocus, restoreModalFocus } from "./modal-focus.js";

const SEARCH_DEBOUNCE_MS = 300;
const MESSAGE_PAGE_SIZE = 50;

/** Key fields worth calling out per-decoder (address/capcode for pager
 * decoders, from/callsign for APRS, event for EAS) -- see msdr/decoder/
 * backends.py's parsers for what ends up in `fields`. Anything else in
 * `fields` is left out of the compact line rather than guessed at. */
function keyFieldsText(ev) {
  const f = ev.fields ?? {};
  const parts = [];
  if (f.address != null) parts.push(`addr ${f.address}`);
  if (f.capcode != null) parts.push(`cap ${f.capcode}`);
  if (f.from) parts.push(`de ${f.from}`);
  else if (f.callsign) parts.push(`de ${f.callsign}`);
  if (f.event) parts.push(`${f.event}`);
  if (f.demod) parts.push(f.demod);
  return parts.join(" · ");
}

/** Decoded-message history/live view (Decoders P1): a modal, opened from
 * decode-deck's ⚙ modal ("MESSAGES"), following the same open-boolean +
 * overlay-backdrop pattern as setup-drawer.js. `decodeEvents` is app.js's
 * live-capped (500) array from the "decoder_event" websocket message --
 * used both to seed the unfiltered table and to prepend new events live
 * while a decoder/search filter is active (see _mergeLive). `decoders` is
 * the GET /api/decoders registry, for the filter <select>. */
export class DecodeView extends LitElement {
  static properties = {
    open: { type: Boolean },
    decoders: { type: Array },
    decodeEvents: { type: Array },
    presetFrequencyHz: { type: Number },
    failure: { type: Object },
    _events: { state: true },
    _visibleLimit: { state: true },
    _decoder: { state: true },
    _q: { state: true },
    _freq: { state: true },
    _groupByFreq: { state: true },
    _loading: { state: true },
    _playingId: { state: true },
    _faxEvent: { state: true },
    _held: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.decoders = [];
    this.decodeEvents = [];
    this.presetFrequencyHz = null;
    this.failure = null;
    this._events = [];
    this._visibleLimit = MESSAGE_PAGE_SIZE;
    this._returnFocus = null;
    this._focusInHandler = (event) => {
      const dialog = this.querySelector("[role=dialog]");
      if (dialog) containModalFocus(event, dialog);
    };
    this._decoder = "";
    this._q = "";
    this._freq = "";
    this._groupByFreq = false;
    this._loading = false;
    this._searchTimer = null;
    this._playingId = null;
    this._faxEvent = null;
    // Live events that arrived while the user was inspecting the list (see
    // _isHolding), newest first -- merged on the "N new" pill or once the
    // user is back at the top and idle, so rows never move under the cursor.
    this._held = [];
    this._hovering = false;
    // Expanded "Received fields" panels by event id: rows are rendered by
    // position, so an un-tracked <details open> would stay on its index and
    // end up showing a different message after a merge.
    this._openFields = new Set();
  }

  /** Toggles the inline call-audio player for event `id` -- only one player
   * open at a time (see `_row`'s kind==="call" branch). */
  _togglePlay(id) {
    this._playingId = this._playingId === id ? null : id;
    if (this._playingId == null) this._maybeFlush();
  }

  /** Opens the fax viewer modal (fax-viewer.js) for an `image`-kind event --
   * see `_row`'s kind==="image" branch. */
  _openFax(ev) {
    this._faxEvent = ev;
  }

  /** Frequencies present in the currently loaded events (BAND DECODE, P2,
   * feeds several channels at once -- see decode-deck.js's channels table),
   * for the frequency filter <select>. Client-side only: unlike decoder/q,
   * there's no server-side frequency filter on GET /api/decoder/events. */
  get _frequencies() {
    const seen = new Set();
    for (const ev of this._events) {
      if (ev.frequency_hz) seen.add(ev.frequency_hz);
    }
    // Keep the preset frequency selectable even before any message has come
    // in for it (e.g. a decode just pinned from survey-view).
    if (this.presetFrequencyHz != null) seen.add(this.presetFrequencyHz);
    return Array.from(seen).sort((a, b) => a - b);
  }

  get _visibleEvents() {
    let events = this._events;
    if (this._freq) events = events.filter((e) => String(e.frequency_hz) === this._freq);
    if (this._groupByFreq) {
      events = [...events].sort((a, b) => (a.frequency_hz ?? 0) - (b.frequency_hz ?? 0) || b.ts - a.ts);
    }
    return events;
  }

  get _displayEvents() {
    return this._visibleEvents.slice(0, this._visibleLimit);
  }

  updated(changed) {
    if (changed.has("open") && this.open) {
      this._visibleLimit = MESSAGE_PAGE_SIZE;
      this._returnFocus = this.ownerDocument.activeElement;
      this.ownerDocument.addEventListener("focusin", this._focusInHandler, true);
      // Focus must enter the modal before Safari traverses its accessibility
      // tree; leaving it behind in the live survey is extremely expensive.
      this.querySelector("[aria-label='close messages']")?.focus();
      this._load();
      // S5: a confirmed survey label's ✓ chip opens this modal preset to its
      // frequency (app.js's _doOpenSurveyMessages) -- applied whenever the
      // modal opens with one already set.
      if (this.presetFrequencyHz != null) this._freq = String(this.presetFrequencyHz);
    }
    if (changed.has("open") && !this.open) {
      clearTimeout(this._searchTimer);
      this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
      restoreModalFocus(this._returnFocus);
      this._returnFocus = null;
    }
    if (changed.has("_freq") || changed.has("_decoder") || changed.has("_q")) {
      this._visibleLimit = MESSAGE_PAGE_SIZE;
    }
    if (changed.has("presetFrequencyHz") && this.open && this.presetFrequencyHz != null) {
      this._freq = String(this.presetFrequencyHz);
    }
    if (changed.has("decodeEvents")) this._mergeLive();
  }

  disconnectedCallback() {
    clearTimeout(this._searchTimer);
    this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
    super.disconnectedCallback();
  }

  /** Only checks the newest live event against the current filters -- app.js
   * prepends one event per "decoder_event" message, so this keeps the open
   * table live without re-fetching or diffing the whole array on every push. */
  _mergeLive() {
    if (!this.open) return;
    const newest = this.decodeEvents?.[0];
    if (!newest || this._events.some((e) => e.id === newest.id)) return;
    if (this._decoder && newest.decoder !== this._decoder) return;
    if (this._q && !(newest.text ?? "").toLowerCase().includes(this._q.toLowerCase())) return;
    if (this._isHolding()) {
      if (!this._held.some((e) => e.id === newest.id)) this._held = [newest, ...this._held];
      return;
    }
    this._events = [newest, ...this._events].slice(0, 500);
  }

  /** True while the user is reading or interacting with the list: scrolled
   * away from the top, pointer over the rows, a fields panel expanded, or a
   * call playing. */
  _isHolding() {
    const panel = this.querySelector(".decode-evidence-panel");
    return (
      (panel?.scrollTop ?? 0) > 4 || this._hovering || this._openFields.size > 0 || this._playingId != null
    );
  }

  _flushHeld({ scrollTop = false } = {}) {
    if (this._held.length) {
      const known = new Set(this._events.map((e) => e.id));
      this._events = [...this._held.filter((e) => !known.has(e.id)), ...this._events].slice(0, 500);
      this._held = [];
    }
    if (scrollTop) this.querySelector(".decode-evidence-panel")?.scrollTo({ top: 0 });
  }

  /** Auto-merge once nothing holds the list any more (scrolled back to the
   * top, pointer left, panels closed). */
  _maybeFlush() {
    if (this._held.length && !this._isHolding()) this._flushHeld();
  }

  _onFieldsToggle(ev, details) {
    if (details.open) this._openFields.add(ev.id);
    else this._openFields.delete(ev.id);
    this._maybeFlush();
  }

  async _load() {
    // Decoder/search changes overlap loads; only the latest may write results
    // or clear _loading (an older finisher would hide the spinner too early).
    const seq = (this._reqSeq = (this._reqSeq || 0) + 1);
    this._loading = true;
    this._held = [];
    try {
      const events = await api.listDecodeEvents({
        limit: 500,
        decoder: this._decoder || undefined,
        q: this._q || undefined,
      });
      if (seq !== this._reqSeq) return;
      this._events = events;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      if (seq === this._reqSeq) this._loading = false;
    }
  }

  _onDecoderChange(value) {
    this._decoder = value;
    this._load();
  }

  _onSearchInput(value) {
    this._q = value;
    clearTimeout(this._searchTimer);
    this._searchTimer = setTimeout(() => this._load(), SEARCH_DEBOUNCE_MS);
  }

  async _clear() {
    if (!window.confirm("Clear all decoded message history? This cannot be undone.")) return;
    try {
      await api.clearDecodeEvents();
      this._events = [];
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "decode history cleared" } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  /** Content of a row's flex-1 text cell -- kind-specific: `call` gets the
   * compact callLineText line, `image` (wefax) a thumbnail + text that opens
   * the fax viewer, `time` (wwv/dcf77/msf) the UTC time plus a colour-coded
   * offset and station, everything else the plain event text. */
  _lineContent(ev) {
    if (ev.kind === "call") return callLineText(ev);
    if (ev.kind === "image") {
      return html`<span
        style="display:inline-flex;align-items:center;gap:8px;cursor:pointer"
        title="open fax viewer"
        @click=${() => this._openFax(ev)}
      >
        <img src="/api/decoder/images/${ev.id}.png" loading="lazy" style="max-height:48px" alt="fax thumbnail" />
        <span>${ev.text ?? ""}</span>
      </span>`;
    }
    if (ev.kind === "time") {
      const f = ev.fields ?? {};
      const color = offsetColorVar(f.offset_s);
      return html`<span class="mono">${f.utc ?? ev.text ?? ""}</span>
        <span class="mono" style=${color ? `color:${color}` : ""}>${offsetText(f.offset_s)}</span>
        ${f.station ? html` <span class="dim mono">${f.station}</span>` : ""}`;
    }
    return ev.text ?? "";
  }

  /** U3: opens the signal-detail panel for this event's frequency. */
  _openSignal(ev) {
    if (ev.frequency_hz == null) return;
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: ev.frequency_hz, signal_id: ev.signal_id ?? undefined },
        bubbles: true,
        composed: true,
      })
    );
  }

  _row(ev) {
    const isCall = ev.kind === "call";
    const badge = integrityBadge(ev.integrity, ev.integrity_note);
    const isReplay = typeof ev.source_id === "string" && ev.source_id.startsWith("replay:");
    return html`
      <div class="row" style="cursor:default">
        <span class="dim mono">${epochHM(ev.ts)}</span>
        <span class="mono">${ev.frequency_hz ? mhz(ev.frequency_hz) : "--"}</span>
        <span class="mono">${(ev.decoder ?? "").toUpperCase()}</span>
        <span class="chip integrity-badge ${badge.cls}" title=${badge.title}>${badge.text}</span>
        ${isReplay ? html`<span class="chip tag-replay">replay</span>` : ""}
        <div style="flex:1;min-width:0">
          ${this._lineContent(ev)}
          ${ev.fields && Object.keys(ev.fields).length
            ? html`<details
                class="decode-event-fields"
                ?open=${this._openFields.has(ev.id)}
                @toggle=${(e) => this._onFieldsToggle(ev, e.currentTarget)}
              >
                <summary>Received fields · ${ev.kind ?? "output"}</summary>
                <pre style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto">${JSON.stringify(ev.fields, null, 2)}</pre>
              </details>`
            : ""}
        </div>
        ${isCall && ev.fields?.wav
          ? html`<button title="play call audio" @click=${() => this._togglePlay(ev.id)}>▶</button>`
          : ""}
        ${ev.frequency_hz != null
          ? html`<button title="Signal detail" aria-label="signal detail" @click=${() => this._openSignal(ev)}>
              ⓘ
            </button>`
          : ""}
        <span class="dim mono" style="font-size:0.85em">${keyFieldsText(ev)}</span>
      </div>
      ${isCall && this._playingId === ev.id
        ? html`<div class="row" style="cursor:default">
            <audio controls preload="none" src="/api/decoder/calls/${ev.id}.wav" style="flex:1"></audio>
          </div>`
        : ""}
    `;
  }

  /** Rows for the visible (filtered/sorted) events; when grouping by
   * frequency, a header row precedes each new frequency's run of events. */
  _rows() {
    const events = this._displayEvents.filter((ev) => ev.decoder !== "cw");
    if (!this._groupByFreq) return events.map((ev) => this._row(ev));
    const out = [];
    let last;
    let first = true;
    for (const ev of events) {
      if (first || ev.frequency_hz !== last) {
        last = ev.frequency_hz;
        first = false;
        out.push(
          html`<div class="row dim" style="cursor:default;font-weight:bold">
            ${ev.frequency_hz ? mhz(ev.frequency_hz) : "no frequency"}
          </div>`
        );
      }
      out.push(this._row(ev));
    }
    return out;
  }

  render() {
    if (!this.open) return html``;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel decode-evidence-panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="decode-view-title"
          style="margin:40px auto 0;width:min(820px,94vw);max-height:85vh;overflow:auto;align-self:flex-start;display:flex;flex-direction:column;gap:10px"
          @click=${(ev) => ev.stopPropagation()}
          @scroll=${() => this._maybeFlush()}
          @keydown=${(ev) => trapModalTab(ev, ev.currentTarget)}
        >
          <div style="display:flex;align-items:center">
            <h2 id="decode-view-title" style="flex:1">Decoded messages</h2>
            <button title="close" aria-label="close messages" @click=${() => this._close()}>&times;</button>
          </div>
          ${this.failure
            ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
            : ""}
          <div class="decode-message-filters">
            <select @change=${(e) => this._onDecoderChange(e.target.value)}>
              <option value="" ?selected=${!this._decoder}>all decoders</option>
              ${this.decoders.map(
                (d) => html`<option value=${d.id} ?selected=${d.id === this._decoder}>${d.label}</option>`
              )}
            </select>
            <input
              placeholder="search text…"
              .value=${this._q}
              @input=${(e) => this._onSearchInput(e.target.value)}
            />
            <select @change=${(e) => (this._freq = e.target.value)}>
              <option value="" ?selected=${!this._freq}>all frequencies</option>
              ${this._frequencies.map(
                (f) => html`<option value=${f} ?selected=${String(f) === this._freq}>${mhz(f)}</option>`
              )}
            </select>
            <button
              title="Group rows by frequency (BAND DECODE channels)"
              style=${this._groupByFreq ? "border-color:var(--accent);color:var(--accent)" : ""}
              @click=${() => (this._groupByFreq = !this._groupByFreq)}
            >
              GROUP BY FREQ
            </button>
            <button @click=${() => this._clear()}>CLEAR</button>
          </div>
          ${this._visibleEvents.length
            ? html`
                <cw-transcript .events=${this._displayEvents}></cw-transcript>
                <!-- Always rendered, zero-height: the pill overlays the list
                     instead of pushing it down when it appears. -->
                <div class="decode-new-pill-anchor">
                  ${this._held.length
                    ? html`<button class="decode-new-pill" @click=${() => this._flushHeld({ scrollTop: true })}>
                        ↑ ${this._held.length} new message${this._held.length === 1 ? "" : "s"}
                      </button>`
                    : ""}
                </div>
                <div
                  class="list"
                  @mouseenter=${() => (this._hovering = true)}
                  @mouseleave=${() => {
                    this._hovering = false;
                    this._maybeFlush();
                  }}
                >
                  ${this._rows()}
                </div>
                ${this._visibleEvents.length > this._visibleLimit
                  ? html`<button @click=${() => (this._visibleLimit += MESSAGE_PAGE_SIZE)}>Show more messages</button>`
                  : ""}
              `
            : html`<p class="empty">
                ${this._loading ? "loading…" : "No messages — start a decode from the DECODE deck"}
              </p>`}
        </div>
      </div>
      <fax-viewer
        .open=${!!this._faxEvent}
        .event=${this._faxEvent}
        @close=${() => (this._faxEvent = null)}
      ></fax-viewer>
    `;
  }
}

customElements.define("decode-view", DecodeView);
