import { LitElement, html } from "../vendor/lit.js";

const REFRESH_MS = 10_000;

/** Fax image viewer modal (Decoders S7, wefax `image` events -- see
 * docs/superpowers/plans/2026-09-25-survey-s7-s10-hf.md Stream C). Opened
 * from decode-view's image rows with the DecodeEvent (`id`, `text`,
 * `fields`); the PNG at `GET /api/decoder/images/<id>.png` is written
 * progressively server-side while the fax is in progress (`text === "fax
 * started"`), so this refreshes the <img> src on a timer with a
 * cache-busting query param until closed. */
export class FaxViewer extends LitElement {
  static properties = {
    open: { type: Boolean },
    event: { type: Object },
    _zoom: { state: true },
    _bust: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.event = null;
    this._zoom = 100;
    this._bust = Date.now();
    this._timer = null;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  updated(changed) {
    if (changed.has("open") || changed.has("event")) {
      this._zoom = 100;
      this._bust = Date.now();
      this._syncTimer();
    }
  }

  _syncTimer() {
    clearInterval(this._timer);
    this._timer = null;
    if (this.open && this.event?.text === "fax started") {
      this._timer = setInterval(() => (this._bust = Date.now()), REFRESH_MS);
    }
  }

  get _baseSrc() {
    return this.event ? `/api/decoder/images/${this.event.id}.png` : "";
  }

  get _src() {
    if (!this.event) return "";
    return this.event.text === "fax started" ? `${this._baseSrc}?t=${this._bust}` : this._baseSrc;
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  render() {
    if (!this.open || !this.event) return html``;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="fax-viewer-title"
          style="margin:40px auto 0;width:min(900px,96vw);max-height:90vh;overflow:auto;align-self:flex-start;display:flex;flex-direction:column;gap:10px"
          @click=${(ev) => ev.stopPropagation()}
        >
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <h2 id="fax-viewer-title" style="flex:1;margin:0">${this.event.text ?? "Fax"}</h2>
            <button ?disabled=${this._zoom === 50} @click=${() => (this._zoom = 50)}>50%</button>
            <button ?disabled=${this._zoom === 100} @click=${() => (this._zoom = 100)}>100%</button>
            <button ?disabled=${this._zoom === 200} @click=${() => (this._zoom = 200)}>200%</button>
            <a
              href=${this._baseSrc}
              download
              title="download fax image"
              style="padding:6px 10px;border:1px solid var(--line);color:var(--fg);text-decoration:none"
              >DOWNLOAD</a
            >
            <button title="close" aria-label="close fax viewer" @click=${() => this._close()}>&times;</button>
          </div>
          <div style="overflow:auto;max-height:75vh;border:1px solid var(--line)">
            <img src=${this._src} style="width:${this._zoom}%;display:block" alt="fax image" />
          </div>
        </div>
      </div>
    `;
  }
}

customElements.define("fax-viewer", FaxViewer);
