import { LitElement, html } from "../vendor/lit.js";
import { khzDelta } from "../format.js";
import "./led-dot.js";
import "./tag-chips.js";
import "./failure-note.js";

export class NearbyList extends LitElement {
  static properties = {
    signals: { type: Array },
    vfoHz: { type: Number },
    tagsById: { type: Object },
    failure: { type: Object },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.signals = [];
    this.vfoHz = null;
    this.tagsById = {};
    this.failure = null;
  }

  _tune(id) {
    this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id } }));
  }

  /** U3: opens the signal-detail panel -- a separate ⓘ button next to the
   * existing tune button so the row's own click (also tune) keeps working. */
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

  _dismiss() {
    this.dispatchEvent(new CustomEvent("dismiss-failure"));
  }

  render() {
    if (!this.signals.length) {
      return html`<div class="panel">
        <h2>Nearby</h2>
        ${this.failure ? html`<failure-note .failure=${this.failure} @dismiss=${() => this._dismiss()}></failure-note>` : ""}
        <p class="empty">No memories near this frequency — SAVE it to start one</p>
      </div>`;
    }
    return html`
      <div class="panel">
        <h2>Nearby</h2>
        ${this.failure ? html`<failure-note .failure=${this.failure} @dismiss=${() => this._dismiss()}></failure-note>` : ""}
        <div class="list">
          ${this.signals.map(
            (s) => html`
              <div class="row" @click=${() => this._tune(s.id)}>
                <led-dot .color=${s.color ?? null}></led-dot>
                <span>${s.name}</span>
                <tag-chips .tagIds=${s.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips>
                <span class="spacer"></span>
                <span class="mono dim">${khzDelta(s.frequency_hz - (this.vfoHz ?? s.frequency_hz))}</span>
                <span class="dim">${s.demodulator ?? ""}</span>
                <button
                  title="Signal detail"
                  aria-label="signal detail for ${s.name}"
                  style="font-size:0.8em;padding:2px 8px"
                  @click=${(ev) => this._openSignal(ev, s)}
                >
                  ⓘ
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
              </div>
            `
          )}
        </div>
      </div>
    `;
  }
}

customElements.define("nearby-list", NearbyList);
