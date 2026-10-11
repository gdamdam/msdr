import { LitElement, html } from "../vendor/lit.js";
import { mhz } from "../format.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./tag-chips.js";
import "./failure-note.js";

const FAVORITES_ID = "favorites";
const COLOR_ORDER = ["red", "orange", "yellow", "green", "blue", "magenta", "cyan", "white"];
const byName = (a, b) => (a.name ?? "").localeCompare(b.name ?? "", undefined, { sensitivity: "base" });


export class RecentList extends LitElement {
  static properties = {
    signals: { type: Array },
    scope: { type: String },
    _query: { state: true },
    _sort: { state: true },
    tagsById: { type: Object },
    identFeature: { type: Boolean }, // state.features.ident -- hides the "∿" button when off
    failure: { type: Object },
    emptyMessage: { type: String },
    _favoriteIds: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.signals = [];
    this.scope = "recent";
    this._query = "";
    this._sort = "default";
    this.tagsById = {};
    this.identFeature = true;
    this.failure = null;
    this.emptyMessage = "No recent stations — tune something and it will appear here";
    this._favoriteIds = new Set();
  }

  connectedCallback() {
    super.connectedCallback();
    this._loadFavorites();
  }

  /** app.js replaces `signals` with a fresh array on every ws "inbox" push,
   * so this doubles as "refresh favorite state on the ws inbox message". */
  updated(changed) {
    if (changed.has("signals")) this._loadFavorites();
    if (changed.has("scope") && changed.get("scope") !== undefined) {
      this._query = "";
      this._sort = "default";
    }
  }

  /** v0.11.0 "Lists": rows carry `list_ids` only from the /api/library
   * endpoint (library-view.js) -- /api/signals/recent items don't, so fall
   * back to fetching the Favorites list's ids once per refresh. */
  async _loadFavorites() {
    const request = this._favoritesRequest = (this._favoritesRequest ?? 0) + 1;
    if (this.signals.length && this.signals.every((s) => s.list_ids !== undefined)) {
      this._favoriteIds = new Set(this.signals.filter((s) => this._hasFavoriteId(s)).map((s) => s.id));
      return;
    }
    try {
      const items = await api.listListItems(FAVORITES_ID);
      if (request !== this._favoritesRequest) return;
      this._favoriteIds = new Set(items.map((s) => s.id));
    } catch {
      // Keep the last known state when a background refresh fails.
    }
  }

  _hasFavoriteId(s) {
    return (s.list_ids ?? []).includes(FAVORITES_ID);
  }

  _isFavorite(s) {
    return this._favoriteIds.has(s.id);
  }

  async _toggleFavorite(ev, s) {
    ev.stopPropagation();
    const undoAt = Date.now();
    const fav = this._isFavorite(s);
    try {
      if (fav) await api.removeListItems(FAVORITES_ID, [s.id]);
      else await api.addListItems(FAVORITES_ID, [s.id]);
      this.dispatchEvent(new CustomEvent("library-undoable", {
        detail: { at: undoAt }, bubbles: true, composed: true,
      }));
      this._favoritesRequest = (this._favoritesRequest ?? 0) + 1;
      const next = new Set(this._favoriteIds);
      if (fav) next.delete(s.id);
      else next.add(s.id);
      this._favoriteIds = next;
      this.dispatchEvent(new CustomEvent("favorites-changed", { bubbles: true, composed: true }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _tune(id) {
    this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id } }));
  }

  _edit(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("select", { detail: { kind: "signal", item } }));
  }

  _identify(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("select", { detail: { kind: "signal", item, identify: true } }));
  }

  /** U3: opens the signal-detail panel -- a separate ⓘ button next to the
   * existing tune/favorite/identify/edit buttons so row click (tune) stays. */
  _openSignal(ev, s) {
    ev.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("signal-open", {
        detail: { frequency_hz: s.frequency_hz, signal_id: s.id },
        bubbles: true,
        composed: true,
      })
    );
  }

  _visibleSignals() {
    const query = this._query.trim().toLocaleLowerCase();
    const matches = this.signals.filter((s) => !query || [
      s.name, s.frequency_hz, Number.isFinite(s.frequency_hz) ? mhz(s.frequency_hz) : "",
      s.color, s.demodulator,
    ].some((value) => String(value ?? "").toLocaleLowerCase().includes(query)));
    if (this._sort === "default") return matches;
    return matches.sort((a, b) => {
      if (this._sort === "frequency-asc") return a.frequency_hz - b.frequency_hz || byName(a, b);
      if (this._sort === "frequency-desc") return b.frequency_hz - a.frequency_hz || byName(a, b);
      if (this._sort === "name-asc") return byName(a, b) || a.frequency_hz - b.frequency_hz;
      if (this._sort === "name-desc") return byName(b, a) || a.frequency_hz - b.frequency_hz;
      if (this._sort === "color") {
        const colorRank = (s) => {
          const index = COLOR_ORDER.indexOf(s.color);
          return index < 0 ? COLOR_ORDER.length : index;
        };
        return colorRank(a) - colorRank(b) || byName(a, b);
      }
      if (this._sort === "mode") return String(a.demodulator ?? "").localeCompare(
        String(b.demodulator ?? ""), undefined, { sensitivity: "base" }
      ) || byName(a, b);
      return 0;
    });
  }

  _controls() {
    return html`<div class="listen-list-controls">
      <input type="search" aria-label="Search ${this.scope}" placeholder="Search name or frequency"
        .value=${this._query} @input=${(ev) => { this._query = ev.target.value; }}>
      <select aria-label="Sort ${this.scope}" .value=${this._sort}
        @change=${(ev) => { this._sort = ev.target.value; }}>
        <option value="default">Original order</option>
        <option value="frequency-asc">Frequency ↑</option>
        <option value="frequency-desc">Frequency ↓</option>
        <option value="name-asc">Name A–Z</option>
        <option value="name-desc">Name Z–A</option>
        <option value="color">Color</option>
        <option value="mode">Mode</option>
      </select>
    </div>`;
  }

  render() {
    const visible = this._visibleSignals();
    if (!this.signals.length) {
      return html`<div class="panel">
        <h2>${this.scope === "favorites" ? "Favorites" : "Recent"}</h2>
        ${this._controls()}
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        <p class="empty">${this.emptyMessage}</p>
      </div>`;
    }
    return html`
      <div class="panel">
        <h2>${this.scope === "favorites" ? "Favorites" : "Recent"}</h2>
        ${this._controls()}
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        ${visible.length ? html`<div class="list">
          ${visible.map(
            (s) => html`
              <div class="row station-row" @click=${() => this._tune(s.id)}>
                <led-dot .color=${s.color ?? null}></led-dot>
                <div class="station-identity" title=${`${s.name} · ${mhz(s.frequency_hz)}`}>
                  <span class="station-name">${s.name}</span>
                  <span class="mono dim station-frequency">${mhz(s.frequency_hz)}</span>
                  <tag-chips .tagIds=${s.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips>
                </div>
                <span class="spacer"></span>
                <button
                  title=${this._isFavorite(s) ? "Remove from Favorites" : "Add to Favorites"}
                  aria-label=${this._isFavorite(s) ? `remove ${s.name} from favorites` : `add ${s.name} to favorites`}
                  aria-pressed=${this._isFavorite(s)}
                  style="font-size:0.85em;padding:2px 6px;color:${this._isFavorite(s) ? "var(--accent)" : "var(--ink)"}"
                  @click=${(ev) => this._toggleFavorite(ev, s)}
                >
                  ${this._isFavorite(s) ? "★" : "☆"}
                </button>
                <button
                  class="tune"
                  title="Tune"
                  aria-label="tune ${s.name}"
                  style="font-size:0.8em;padding:2px 8px"
                  @click=${(ev) => {
                    ev.stopPropagation();
                    this._tune(s.id);
                  }}
                >
                  ▸
                </button>
                <button
                  title="Signal detail"
                  aria-label="signal detail for ${s.name}"
                  style="font-size:0.8em;padding:2px 8px"
                  @click=${(ev) => this._openSignal(ev, s)}
                >
                  ⓘ
                </button>
                ${this.identFeature
                  ? html`<button
                      title="Identify (I)" aria-label="Identify ${s.name}"
                      style="font-size:0.8em;padding:2px 8px"
                      @click=${(ev) => this._identify(ev, s)}
                    >
                      ∿
                    </button>`
                  : ""}
                <button title="Edit station" aria-label="Edit ${s.name}" style="font-size:0.8em;padding:2px 8px" @click=${(ev) => this._edit(ev, s)}>
                  ✎
                </button>
              </div>
            `
          )}
        </div>` : html`<p class="empty">No matching entries
          ${this._query ? html` · <button type="button" class="link" @click=${() => { this._query = ""; }}>clear search</button>` : ""}
        </p>`}
      </div>
    `;
  }
}

customElements.define("recent-list", RecentList);
