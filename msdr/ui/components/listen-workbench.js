import { LitElement, html } from "../vendor/lit.js";
import "./nearby-list.js";
import "./recent-list.js";
import "./listen-view.js";
import "./journal-view.js";
import "./decode-deck.js";
import "./signal-workbench.js";

const TABS = [
  ["recent", "Recent"],
  ["favorites", "Favorites"],
  ["nearby", "Nearby"],
  ["suggestions", "Suggestions"],
  ["watch", "Watch"],
  ["log", "Log"],
];
const TAB_KEY = "msdr.listen.tab";

function loadTab() {
  try {
    const key = localStorage.getItem(TAB_KEY);
    return TABS.some(([id]) => id === key) ? key : "recent";
  } catch {
    return "recent";
  }
}

/** One Listen destination panel for the five quick radio workflows. Existing
 * list components remain the source of row behavior; the tabs only change
 * which view is mounted. Decode stays visible as a compact, collapsible deck. */
export class ListenWorkbench extends LitElement {
  static properties = {
    nearby: { type: Array },
    recent: { type: Array },
    favorites: { type: Array },
    vfoHz: { type: Number },
    tagsById: { type: Object },
    catalogFeature: { type: Boolean },
    identFeature: { type: Boolean },
    settingsSavedAt: { type: Number },
    tz: { type: Object },
    nearbyFailure: { type: Object },
    recentFailure: { type: Object },
    suggestionsFailure: { type: Object },
    journal: { type: Array },
    journalDay: { type: String },
    journalDays: { type: Array },
    decodeStatus: { type: Object },
    decoderFeature: { type: Boolean },
    decoders: { type: Array },
    receiverProps: { type: Object },
    lastDecodeEvent: { type: Object },
    decodeEvents: { type: Array },
    connected: { type: Boolean },
    started: { type: Boolean },
    canControl: { type: Boolean },
    surveyActive: { type: Boolean }, // forwarded to decode-deck's ⚗ / ◎
    decodeFailure: { type: Object },
    receiver: { type: Object },
    recorderArmed: { type: Boolean },
    _tab: { state: true },
    _panelHeight: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.nearby = [];
    this.recent = [];
    this.favorites = [];
    this.vfoHz = null;
    this.tagsById = {};
    this.catalogFeature = true;
    this.identFeature = true;
    this.settingsSavedAt = null;
    this.tz = null;
    this.nearbyFailure = null;
    this.recentFailure = null;
    this.suggestionsFailure = null;
    this.journal = [];
    this.journalDay = "";
    this.journalDays = [];
    this.decodeStatus = null;
    this.decoderFeature = true;
    this.decoders = [];
    this.receiverProps = null;
    this.lastDecodeEvent = null;
    this.decodeEvents = [];
    this.connected = false;
    this.started = true;
    this.canControl = false;
    this.decodeFailure = null;
    this.receiver = null;
    this.recorderArmed = false;
    this._tab = loadTab();
    let storedHeight = 0;
    try { storedHeight = Number(localStorage.getItem("msdr.listen.panelHeight")); } catch { /* optional preference */ }
    this._panelHeight = storedHeight >= 220 && storedHeight <= 900 ? storedHeight : 330;
    this._resizeStart = null;
  }

  _startResize(event) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    this._resizeStart = { y: event.clientY, height: this._panelHeight };
  }

  _moveResize(event) {
    if (!this._resizeStart) return;
    this._panelHeight = Math.max(220, Math.min(900, Math.round(this._resizeStart.height + event.clientY - this._resizeStart.y)));
  }

  _endResize() {
    if (!this._resizeStart) return;
    this._resizeStart = null;
    try { localStorage.setItem("msdr.listen.panelHeight", String(this._panelHeight)); } catch { /* optional preference */ }
  }

  _keyResize(event) {
    if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    this._panelHeight = Math.max(220, Math.min(900, this._panelHeight + (event.key === "ArrowDown" ? 20 : -20)));
    try { localStorage.setItem("msdr.listen.panelHeight", String(this._panelHeight)); } catch { /* optional preference */ }
  }

  _emit(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  _selectTab(tab) {
    this._tab = tab;
    try {
      localStorage.setItem(TAB_KEY, tab);
    } catch {
      // The tab still works when browser storage is unavailable.
    }
  }

  _onTabKeydown(ev) {
    const tabId = ev.target.closest('[role="tab"]')?.id?.replace("listen-tab-", "");
    const index = TABS.findIndex(([id]) => id === tabId);
    let next = null;
    if (ev.key === "ArrowRight") next = TABS[(index + 1) % TABS.length][0];
    else if (ev.key === "ArrowLeft") next = TABS[(index + TABS.length - 1) % TABS.length][0];
    else if (ev.key === "Home") next = TABS[0][0];
    else if (ev.key === "End") next = TABS[TABS.length - 1][0];
    if (next) {
      ev.preventDefault();
      this._selectTab(next);
      this.updateComplete.then(() => this.querySelector(`#listen-tab-${next}`)?.focus());
    }
  }

  _content() {
    switch (this._tab) {
      case "nearby":
        return html`<nearby-list
          .signals=${this.nearby}
          .vfoHz=${this.vfoHz}
          .tagsById=${this.tagsById}
          .failure=${this.nearbyFailure}
          @tune-signal=${(ev) => this._emit("tune-signal", ev.detail)}
          @dismiss-failure=${() => this._emit("dismiss-nearby-failure")}
        ></nearby-list>`;
      case "recent":
        return html`<p class="listen-favorite-hint">Select ☆ beside a station to add it to Favorites.</p><recent-list
          .scope=${"recent"}
          .signals=${this.recent}
          .tagsById=${this.tagsById}
          .identFeature=${this.identFeature}
          .failure=${this.recentFailure}
          @tune-signal=${(ev) => this._emit("tune-signal", ev.detail)}
          @select=${(ev) => this._emit("select-signal", ev.detail)}
          @api-error=${(ev) => this._emit("api-error", ev.detail)}
          @dismiss-failure=${() => this._emit("dismiss-recent-failure")}
        ></recent-list>`;
      case "favorites":
        return html`<recent-list
          .scope=${"favorites"}
          .signals=${this.favorites}
          .tagsById=${this.tagsById}
          .identFeature=${this.identFeature}
          .emptyMessage=${"No favorites yet — select ☆ on a recent station or ☆ FAVORITE beside the receiver frequency."}
          @tune-signal=${(ev) => this._emit("tune-signal", ev.detail)}
          @select=${(ev) => this._emit("select-signal", ev.detail)}
          @api-error=${(ev) => this._emit("api-error", ev.detail)}
        ></recent-list>`;
      case "suggestions":
        return html`<listen-view
          .vfoHz=${this.vfoHz}
          .catalogFeature=${this.catalogFeature}
          .settingsSavedAt=${this.settingsSavedAt}
          .tz=${this.tz}
          .failure=${this.suggestionsFailure}
          @tune-catalog=${(ev) => this._emit("tune-catalog", ev.detail)}
          @tune-signal=${(ev) => this._emit("tune-signal", ev.detail)}
          @tune-frequency=${(ev) => this._emit("tune-frequency", ev.detail)}
          @star-catalog=${(ev) => this._emit("star-catalog", ev.detail)}
          @dismiss-failure=${() => this._emit("dismiss-suggestions-failure")}
        ></listen-view>`;
      case "watch":
        // open-instruments bubbles (composed) from the workbench up to app.js.
        return html`<signal-workbench .receiver=${this.receiver} .recorderArmed=${this.recorderArmed}></signal-workbench>`;
      case "log":
        return html`<journal-view
          .entries=${this.journal}
          .day=${this.journalDay}
          .days=${this.journalDays}
          .tagsById=${this.tagsById}
          @prev-day=${() => this._emit("prev-day")}
          @next-day=${() => this._emit("next-day")}
          @tune-signal=${(ev) => this._emit("tune-signal", ev.detail)}
          @tune-frequency=${(ev) => this._emit("tune-frequency", ev.detail)}
        ></journal-view>`;
      default:
        return html``;
    }
  }

  render() {
    return html`<section class="panel listen-workbench listen-evidence-panel" aria-label="Listen workspace">
      <div class="listen-workbench-header">
      <div class="listen-workbench-left" style="--listen-panel-height:${this._panelHeight}px">
      <h2 class="listen-section-title">Listen</h2>
      <div class="listen-workbench-toolbar">
      <div class="listen-workbench-tabs" role="tablist" aria-label="Listen views" @keydown=${(ev) => this._onTabKeydown(ev)}>
        ${TABS.map(([id, label]) => html`<button
          id="listen-tab-${id}"
          role="tab"
          aria-selected=${this._tab === id}
          aria-controls="listen-panel-content"
          tabindex=${this._tab === id ? "0" : "-1"}
          class="listen-workbench-tab ${this._tab === id ? "active" : ""}"
          @click=${() => this._selectTab(id)}
        >${label}</button>`)}
      </div>
      </div>
      <div id="listen-panel-content" role="tabpanel" aria-labelledby="listen-tab-${this._tab}" class="listen-workbench-content">
        ${this._content()}
      </div>
      <button type="button" class="listen-resize-handle" aria-label="Resize Listen panel" title="Drag to resize Listen · Up/Down keys"
        @pointerdown=${(ev) => this._startResize(ev)}
        @pointermove=${(ev) => this._moveResize(ev)}
        @pointerup=${() => this._endResize()}
        @pointercancel=${() => this._endResize()}
        @lostpointercapture=${() => this._endResize()}
        @keydown=${(ev) => this._keyResize(ev)}>╱╱</button>
      </div>
      ${this.decoderFeature ? html`<div class="listen-decode-controls" aria-label="Decode controls">
        <decode-deck
          .frequencyHz=${this.vfoHz}
          .status=${this.decodeStatus}
          .decoders=${this.decoders}
          .receiverProps=${this.receiverProps}
          .lastEvent=${this.lastDecodeEvent}
          .decodeEvents=${this.decodeEvents}
          .connected=${this.connected}
          .started=${this.started}
          .canControl=${this.canControl}
          .surveyActive=${this.surveyActive}
          .failure=${this.decodeFailure}
          @decode-start=${(ev) => this._emit("decode-start", ev.detail)}
          @decode-stop=${() => this._emit("decode-stop")}
          @band-start=${(ev) => this._emit("band-start", ev.detail)}
          @band-stop=${() => this._emit("band-stop")}
          @band-save=${(ev) => this._emit("band-save", ev.detail)}
          @install-presets=${() => this._emit("install-presets")}
          @open-decode-view=${() => this._emit("open-decode-view")}
          @optimize-decode=${(ev) => this._emit("optimize-decode", ev.detail)}
          @dismiss-failure=${() => this._emit("dismiss-decode-failure")}
        ></decode-deck>
      </div>` : ""}
      </div>
    </section>`;
  }
}

customElements.define("listen-workbench", ListenWorkbench);
