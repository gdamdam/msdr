import { LitElement, html } from "../vendor/lit.js";
import { mhz, timeHM } from "../format.js";
import * as api from "../api.js";
import "./led-dot.js";
import "./tag-chips.js";
import "./failure-note.js";

const FAVORITES_ID = "favorites";

export class InboxList extends LitElement {
  static properties = {
    signals: { type: Array },
    tagsById: { type: Object },
    identFeature: { type: Boolean }, // state.features.ident -- hides the "∿" button when off
    failure: { type: Object },
    _favoriteIds: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.signals = [];
    this.tagsById = {};
    this.identFeature = true;
    this.failure = null;
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
  }

  /** v0.11.0 "Lists": rows carry `list_ids` only from the /api/library
   * endpoint (library-view.js) -- /api/signals/inbox items don't, so fall
   * back to fetching the Favorites list's ids once per refresh. */
  async _loadFavorites() {
    if (this.signals.length && this.signals.every((s) => s.list_ids !== undefined)) {
      this._favoriteIds = new Set(this.signals.filter((s) => this._hasFavoriteId(s)).map((s) => s.id));
      return;
    }
    try {
      const items = await api.listListItems(FAVORITES_ID);
      this._favoriteIds = new Set(items.map((s) => s.id));
    } catch {
      this._favoriteIds = new Set();
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
      const next = new Set(this._favoriteIds);
      if (fav) next.delete(s.id);
      else next.add(s.id);
      this._favoriteIds = next;
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    }
  }

  _select(item) {
    this.dispatchEvent(new CustomEvent("select", { detail: { kind: "signal", item } }));
  }

  _identify(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("select", { detail: { kind: "signal", item, identify: true } }));
  }

  /** Re-listen to an inbox capture without opening the edit drawer -- row
   * click still opens the drawer as before. */
  _tune(ev, item) {
    ev.stopPropagation();
    this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: item.id } }));
  }

  /** U3: opens the signal-detail panel -- a separate ⓘ button next to the
   * existing tune/favorite/identify buttons so row click (drawer) keeps working. */
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

  render() {
    if (!this.signals.length) {
      return html`<div class="panel">
        <h2>Unidentified saves</h2>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        <p class="empty">No unidentified saves — SAVE a frequency you hear and name it later</p>
      </div>`;
    }
    return html`
      <div class="panel">
        <h2>Unidentified saves</h2>
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        <div class="list">
          ${this.signals.map(
            (s) => html`
              <div class="row" @click=${() => this._select(s)}>
                <led-dot .color=${s.color ?? null}></led-dot>
                <span class="mono">${mhz(s.frequency_hz)}</span>
                <span class="dim">${s.demodulator ?? ""}</span>
                <tag-chips .tagIds=${s.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips>
                <span class="spacer"></span>
                <span class="dim">${timeHM(s.first_heard_at)}</span>
                <button
                  title=${this._isFavorite(s) ? "Remove from Favorites" : "Add to Favorites"}
                  aria-label=${this._isFavorite(s) ? `remove ${s.name ?? ""} from favorites` : `add ${s.name ?? ""} to favorites`}
                  aria-pressed=${this._isFavorite(s)}
                  style="font-size:0.85em;padding:2px 6px;color:${this._isFavorite(s) ? "var(--accent)" : "var(--ink)"}"
                  @click=${(ev) => this._toggleFavorite(ev, s)}
                >
                  ${this._isFavorite(s) ? "★" : "☆"}
                </button>
                <button
                  class="tune"
                  title="Tune"
                  aria-label="tune ${s.name ?? ""}"
                  style="font-size:0.8em;padding:2px 8px"
                  @click=${(ev) => this._tune(ev, s)}
                >
                  ▸
                </button>
                <button
                  title="Signal detail"
                  aria-label="signal detail for ${s.name ?? ""}"
                  style="font-size:0.8em;padding:2px 8px"
                  @click=${(ev) => this._openSignal(ev, s)}
                >
                  ⓘ
                </button>
                ${this.identFeature
                  ? html`<button
                      title="Identify (I)"
                      style="font-size:0.8em;padding:2px 8px"
                      @click=${(ev) => this._identify(ev, s)}
                    >
                      ∿
                    </button>`
                  : ""}
              </div>
            `
          )}
        </div>
      </div>
    `;
  }
}

customElements.define("inbox-list", InboxList);
