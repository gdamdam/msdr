import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import "./led-dot.js";

const DISMISS_KEY = "msdr.gettingStarted.dismissed";
const CATALOG_POLL_MS = 30_000;

function loadDismissed() {
  try {
    return localStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

function storeDismissed(on = true) {
  try {
    if (on) localStorage.setItem(DISMISS_KEY, "1");
    else localStorage.removeItem(DISMISS_KEY);
  } catch {
    // private mode / storage disabled -- hide still works for this session
  }
}

/** First-run checklist, sitting at the top of the grid until every
 * *required* step is done (location is a nice-to-have, not gated on) or the
 * operator hides it for good. "catalog loaded" is the one check with no
 * existing prop to read it from, so this polls GET /api/catalog/sources
 * itself while it could still be the reason the checklist is showing. */
export class GettingStarted extends LitElement {
  static properties = {
    state: { type: Object },
    settings: { type: Object },
    signals: { type: Array },
    wsDown: { type: Boolean },
    _dismissed: { state: true },
    _forced: { state: true },
    _catalogLoaded: { state: true },
    _catalogRefreshing: { state: true },
    _catalogRefreshMsg: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.state = null;
    this.settings = null;
    this.signals = [];
    this.wsDown = false;
    this._dismissed = loadDismissed();
    this._catalogLoaded = false;
    this._catalogRefreshing = false;
    this._catalogRefreshMsg = null;
    this._pollTimer = null;
  }

  connectedCallback() {
    super.connectedCallback();
    if (!this._dismissed) this._checkCatalog();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearTimeout(this._pollTimer);
  }

  async _checkCatalog() {
    clearTimeout(this._pollTimer);
    try {
      const res = await api.catalogSources();
      this._catalogLoaded = (res.sources ?? []).some((s) => (s.entry_count ?? 0) > 0);
    } catch {
      this._catalogLoaded = false;
    }
    if (!this._catalogLoaded && !this._dismissed) {
      this._pollTimer = setTimeout(() => this._checkCatalog(), CATALOG_POLL_MS);
    }
  }

  get _steps() {
    const connected = !!this.state?.connected;
    const started = connected && (this.state?.started ?? true);
    return [
      { key: "connected", label: "CONNECTED TO SDRCONNECT", done: connected, action: "RETRY", required: true },
      { key: "started", label: "RECEIVER RUNNING", done: started, action: "OPEN SETUP", required: true },
      {
        key: "catalog",
        label: "CATALOG LOADED",
        done: this._catalogLoaded,
        action: this._catalogRefreshing ? "REFRESHING…" : "REFRESH CATALOG",
        required: true,
      },
      {
        key: "memory",
        label: "FIRST MEMORY SAVED",
        done: this.signals.length > 0,
        action: "SAVE",
        required: true,
      },
      {
        key: "location",
        label: "LOCATION SET",
        done: this.state?.location_ready ?? !!this.settings?.location,
        action: "OPEN SETUP",
        required: false,
      },
    ];
  }

  get _allRequiredDone() {
    return this._steps.filter((s) => s.required).every((s) => s.done);
  }

  async _retry() {
    try {
      await api.connectionRetry();
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "retry requested" } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _runAction(step) {
    if (step.key === "connected") this._retry();
    else if (step.key === "memory") this.dispatchEvent(new CustomEvent("capture"));
    else if (step.key === "catalog") this._refreshCatalog();
    else this.dispatchEvent(new CustomEvent("setup"));
  }

  /** REFRESH CATALOG (v0.18.2+): actually performs the refresh instead of
   * just opening SETUP -- iterates every known source (GET /api/catalog/
   * sources' `available` list, not just ones already fetched) since
   * POST /api/catalog/refresh takes one source at a time, showing progress
   * inline on the row and re-checking _catalogLoaded once done. */
  async _refreshCatalog() {
    if (this._catalogRefreshing) return;
    this._catalogRefreshing = true;
    this._catalogRefreshMsg = null;
    try {
      const res = await api.catalogSources();
      const sources = res.available ?? [];
      let entries = 0;
      const errors = [];
      for (const s of sources) {
        const label = s.label ?? s.source;
        this._catalogRefreshMsg = `refreshing ${label}…`;
        try {
          const r = await api.refreshCatalog(s.source);
          entries += r.entries ?? 0;
          if (r.error) errors.push(`${label}: ${r.error}`);
        } catch (err) {
          errors.push(`${label}: ${err.message}`);
        }
      }
      this._catalogRefreshMsg = errors.length
        ? `${entries} entries refreshed — ${errors.length} failed (${errors.join("; ")})`
        : `${entries} entries refreshed`;
    } catch (err) {
      this._catalogRefreshMsg = `refresh failed: ${err.message}`;
    } finally {
      this._catalogRefreshing = false;
      await this._checkCatalog();
    }
  }

  _hide() {
    storeDismissed();
    this._dismissed = true;
    this._forced = false;
    clearTimeout(this._pollTimer);
  }

  /** Brings the checklist back after HIDE (SETUP's SHOW GETTING STARTED, or
   * the palette's "getting started" action). Also shows it when every step
   * is already done -- the operator asked for it, so don't auto-hide it
   * again until they press HIDE. */
  show() {
    storeDismissed(false);
    this._dismissed = false;
    this._forced = true;
    this._checkCatalog();
  }

  render() {
    // wsDown means nothing on screen is current (see instrument-panel.js) --
    // no point in a checklist that reads state it can't trust.
    if (this._dismissed || (this._allRequiredDone && !this._forced) || this.wsDown) return html``;
    return html`
      <div class="panel" id="getting-started" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Getting Started</h2>
          <button title="hide" style="font-size:0.8em;padding:2px 8px" @click=${() => this._hide()}>HIDE</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
          ${this._steps.map((s) => this._row(s))}
        </div>
      </div>
    `;
  }

  _row(step) {
    const color = step.done ? "var(--ok)" : step.required ? "var(--accent)" : null;
    const lit = step.done || step.required;
    return html`
      <div class="row" style="cursor:default">
        <led-dot .color=${color} .lit=${lit}></led-dot>
        <span class="label">${step.label}${step.required ? "" : " (optional)"}</span>
        ${step.key === "catalog" && this._catalogRefreshMsg
          ? html`<span class="dim mono" style="font-size:0.8em;margin-left:8px">${this._catalogRefreshMsg}</span>`
          : ""}
        <span class="spacer"></span>
        ${step.key === "catalog"
          ? html`<button
              title="Catalog settings"
              style="font-size:0.75em;padding:2px 6px;margin-right:6px"
              @click=${() => this.dispatchEvent(new CustomEvent("setup"))}
            >
              settings
            </button>`
          : ""}
        ${step.done
          ? ""
          : html`<button
              style="font-size:0.8em;padding:2px 8px"
              ?disabled=${step.key === "catalog" && this._catalogRefreshing}
              @click=${() => this._runAction(step)}
            >
              ${step.action}
            </button>`}
      </div>
    `;
  }
}

customElements.define("getting-started", GettingStarted);
