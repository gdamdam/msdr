import { LitElement, html } from "../vendor/lit.js";
import { mhz, catalogOnAir, catalogWindowLabel } from "../format.js";
import { palette as searchPalette, recent } from "../api.js";
import { containModalFocus, restoreModalFocus, trapModalTab } from "./modal-focus.js";
import "./led-dot.js";
import "./tag-chips.js";

const GROUP_LABEL = {
  tune_frequency: "Tune",
  signal: "Signals",
  catalog: "Catalog",
  profile: "Profiles",
  action: "Actions",
};
const GROUP_ORDER = ["tune_frequency", "signal", "catalog", "profile", "action"];

/** A catalog palette result only carries {title, subtitle, frequency_hz, id}
 * per the contract; window fields are optional extras -- the LED only lights
 * up (on-air or not) when they're actually present. */
function hasWindow(r) {
  return r.start_utc !== undefined || r.stop_utc !== undefined || r.days !== undefined;
}

export class Palette extends LitElement {
  static properties = {
    open: { type: Boolean },
    tagsById: { type: Object },
    tz: { type: Object }, // state.timezone -- see catalogWindowLabel
    _query: { state: true },
    _results: { state: true },
    _index: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.tagsById = {};
    this.tz = null;
    this._query = "";
    this._results = [];
    this._index = 0;
    this._debounce = null;
    this._requestId = 0;
    this._searching = false;
    this._returnFocus = null;
    this._focusInHandler = (ev) => this._containFocus(ev);
  }

  updated(changed) {
    if (changed.has("open") && this.open) {
      this._returnFocus = this.ownerDocument.activeElement;
      this.ownerDocument.addEventListener("focusin", this._focusInHandler, true);
      this._query = "";
      this._index = 0;
      this._loadEmpty();
      queueMicrotask(() => this.querySelector("input")?.focus());
    } else if (changed.has("open") && !this.open) {
      clearTimeout(this._debounce);
      this._debounce = null;
      this._requestId += 1;
      this._searching = false;
      this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
      restoreModalFocus(this._returnFocus);
      this._returnFocus = null;
    }
  }

  disconnectedCallback() {
    this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
    super.disconnectedCallback();
  }

  get _orderedResults() {
    return GROUP_ORDER.flatMap((kind) => this._results.filter((r) => r.kind === kind));
  }

  async _loadEmpty() {
    const requestId = ++this._requestId;
    try {
      const signals = await recent();
      if (!this.open || requestId !== this._requestId) return;
      this._results = signals.map((s) => ({
        kind: "signal",
        id: s.id,
        title: s.name,
        subtitle: mhz(s.frequency_hz),
        frequency_hz: s.frequency_hz,
        color: s.color ?? null,
        tag_ids: s.tag_ids ?? [],
      }));
    } catch (err) {
      if (this.open && requestId === this._requestId) {
        this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
      }
    }
  }

  _onInput(ev) {
    this._query = ev.target.value;
    this._index = 0;
    clearTimeout(this._debounce);
    if (!this._query) {
      this._searching = false;
      this._loadEmpty();
      return;
    }
    const query = this._query;
    const requestId = ++this._requestId;
    // Until this query's results land, _results belongs to an older query;
    // Enter must not execute from it (see _onKeydown).
    this._searching = true;
    this._debounce = setTimeout(() => this._search(query, requestId), 80);
  }

  async _search(query, requestId) {
    try {
      const results = await searchPalette(query);
      if (!this.open || requestId !== this._requestId || this._query !== query) return;
      this._results = results;
      this._searching = false;
      return true;
    } catch (err) {
      if (this.open && requestId === this._requestId && this._query === query) {
        this._searching = false;
        this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
      }
    }
    return false;
  }

  _onKeydown(ev) {
    const dialog = this.querySelector('[role="dialog"]');
    trapModalTab(ev, dialog);
    if (ev.key === "Escape") {
      ev.preventDefault();
      this.dispatchEvent(new CustomEvent("close"));
    } else if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (this._orderedResults.length) {
        this._index = Math.min(this._index + 1, this._orderedResults.length - 1);
      }
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      this._index = Math.max(this._index - 1, 0);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (this._searching) this._enterAfterSearch();
      else if (this._orderedResults.length) this._execute(this._orderedResults[this._index]);
    }
  }

  /** Enter while the typed query's results are still pending: run the search
   * now (skipping the rest of the debounce) and execute its first result. */
  async _enterAfterSearch() {
    clearTimeout(this._debounce);
    const query = this._query;
    const requestId = this._requestId;
    const fresh = await this._search(query, requestId);
    if (!fresh || requestId !== this._requestId || this._query !== query) return;
    if (this._orderedResults.length) this._execute(this._orderedResults[0]);
  }

  _execute(result) {
    if (!result) return;
    switch (result.kind) {
      case "tune_frequency":
        this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz: result.frequency_hz } }));
        break;
      case "signal":
        this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: result.id } }));
        break;
      case "catalog":
        this.dispatchEvent(new CustomEvent("tune-catalog", { detail: { id: result.id, label: result.title } }));
        break;
      case "profile":
        this.dispatchEvent(new CustomEvent("apply-profile", { detail: { id: result.id } }));
        break;
      case "action":
        // back | forward | capture | export | scan-* dispatch as themselves; a
        // "layout-<key>" action (see msdr/library/search.py ACTIONS) instead
        // maps to app.js's single apply-layout handler with the preset key.
        if (result.id.startsWith("layout-")) {
          this.dispatchEvent(new CustomEvent("apply-layout", { detail: { key: result.id.slice(7) } }));
        } else {
          this.dispatchEvent(new CustomEvent(result.id));
        }
        break;
    }
    this.dispatchEvent(new CustomEvent("close"));
  }

  render() {
    if (!this.open) return html``;
    const ordered = this._orderedResults;
    return html`
      <div
        class="overlay-backdrop"
        @click=${() => this.dispatchEvent(new CustomEvent("close"))}
      >
        <div
          class="panel"
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
          tabindex="-1"
          style="margin:80px auto 0;width:min(560px,90vw);max-height:70vh;display:flex;flex-direction:column;gap:8px"
          @click=${(ev) => ev.stopPropagation()}
          @keydown=${(ev) => this._onKeydown(ev)}
        >
          <input
            name="palette-query"
            style="font-size:1.1em"
            placeholder="Tune, search signals/profiles, or run an action…"
            .value=${this._query}
            @input=${this._onInput}
          />
          <div class="list" style="max-height:60vh">
            ${GROUP_ORDER.map((kind) => {
              const rows = this._results.filter((r) => r.kind === kind);
              if (!rows.length) return "";
              return html`
                <div class="label" style="margin-top:4px">${GROUP_LABEL[kind]}</div>
                ${rows.map((r) => {
                  const rowIndex = ordered.indexOf(r);
                  const active = rowIndex === this._index;
                  return html`
                    <div
                      class="row"
                      style=${active ? "background:var(--panel-well)" : ""}
                      @mouseenter=${() => (this._index = rowIndex)}
                      @click=${() => this._execute(r)}
                    >
                      ${r.kind === "signal" ? html`<led-dot .color=${r.color ?? null}></led-dot>` : ""}
                      ${r.kind === "catalog"
                        ? html`<led-dot color="var(--accent)" .lit=${hasWindow(r) ? catalogOnAir(r) : false}></led-dot>`
                        : ""}
                      <span>${r.title}</span>
                      ${r.kind === "signal"
                        ? html`<tag-chips .tagIds=${r.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips>`
                        : ""}
                      ${r.kind === "catalog" && hasWindow(r)
                        ? html`<span class="dim mono" style="font-size:0.85em">${catalogWindowLabel(r, this.tz)}</span>`
                        : ""}
                      <span class="spacer"></span>
                      <span class="dim mono">${r.subtitle}</span>
                    </div>
                  `;
                })}
              `;
            })}
            ${!this._results.length ? html`<p class="empty">no matches</p>` : ""}
          </div>
        </div>
      </div>
    `;
  }

  _containFocus(ev) {
    containModalFocus(ev, this.querySelector('[role="dialog"]'), this.querySelector("input"));
  }
}

customElements.define("msdr-palette", Palette);
