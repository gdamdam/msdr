import { LitElement, html } from "../vendor/lit.js";

/** Where to put a popover card, in coordinates relative to the container:
 * below the anchor if the card fits above the container's bottom
 * edge, otherwise above. Left is clamped so the card never overflows the
 * container; `notch` is the pointer's x offset inside the card. */
export function popoverPlacement(anchorRect, cardSize, containerRect, gap = 10) {
  const below = anchorRect.bottom + gap + cardSize.height <= containerRect.bottom;
  const top = (below ? anchorRect.bottom + gap : anchorRect.top - gap - cardSize.height) - containerRect.top;
  const maxLeft = Math.max(containerRect.right - containerRect.left - cardSize.width, 0);
  const anchorX = (anchorRect.left + anchorRect.right) / 2 - containerRect.left;
  const left = Math.min(Math.max(anchorX - cardSize.width / 2, 0), maxLeft);
  const notch = Math.min(Math.max(anchorX - left, 14), Math.max(cardSize.width - 14, 14));
  return { top, left, notch, side: below ? "below" : "above" };
}

/** Anchored details card (wide screens) / native modal <dialog> (< 700 px).
 * `anchor` is the element it points at, `content` the TemplateResult shown
 * inside (light DOM has no slots), `label` its aria-label. With `container`
 * (a positioned ancestor) the card is placed inside it, like the allocation
 * chart; without, it is fixed to the viewport. Fires "close" on x / Esc /
 * outside click / dialog cancel, and sets `open` false itself. Styles live in
 * allocation-chart.css (shared class names). */
export class BandPopover extends LitElement {
  static properties = {
    anchor: { attribute: false },
    open: { type: Boolean, reflect: true },
    label: { type: String },
    content: { attribute: false },
    container: { attribute: false },
    _cardSize: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.anchor = null;
    this.open = false;
    this.label = "Band details";
    this.content = null;
    this.container = null;
    this._cardSize = { width: 360, height: 240 }; // first-paint guess; re-measured in updated()
    this._onDocKeydown = this._onDocKeydown.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    // Esc must close the popover even if focus has wandered off the card.
    document.addEventListener("keydown", this._onDocKeydown);
  }

  disconnectedCallback() {
    document.removeEventListener("keydown", this._onDocKeydown);
    super.disconnectedCallback();
  }

  _onDocKeydown(ev) {
    if (ev.key === "Escape" && this.open) this._close();
  }

  _close() {
    this.open = false;
    this.dispatchEvent(new CustomEvent("close"));
  }

  _isNarrow() {
    return window.matchMedia("(max-width: 699px)").matches;
  }

  updated(changed) {
    if (changed.has("open")) {
      if (this.open) {
        this._returnFocus = document.activeElement;
        this.querySelector(".allocation-inspect-close")?.focus();
      } else if (changed.get("open") !== undefined) {
        // Prefer the band/pill we pointed at; fall back to whoever had focus.
        const el = this._openAnchor?.isConnected ? this._openAnchor : this._returnFocus;
        if (el?.isConnected) el.focus();
      }
    }
    if (!this.open) return;
    if (this.anchor) this._openAnchor = this.anchor;
    const dialog = this.querySelector("dialog.allocation-modal");
    if (dialog && !dialog.open) dialog.showModal();
    const card = this.querySelector(".allocation-popover");
    if (card) {
      const width = card.offsetWidth;
      const height = card.offsetHeight;
      // Content arrival changes the card's height; re-place so "below/above" stays right.
      if (Math.abs(width - this._cardSize.width) > 1 || Math.abs(height - this._cardSize.height) > 1) {
        this._cardSize = { width, height };
      }
    }
  }

  _renderBody() {
    return html`
      <button type="button" class="allocation-inspect-close" aria-label="Close band details" @click=${() => this._close()}>
        ×
      </button>
      ${this.content}
    `;
  }

  render() {
    if (!this.open) return "";
    if (this._isNarrow()) {
      return html`
        <dialog
          class="overlay-backdrop allocation-modal"
          aria-label=${this.label}
          style="margin:0;padding:0;border:0;width:100vw;height:100vh;max-width:none;max-height:none;color:inherit"
          @cancel=${(e) => {
            e.preventDefault();
            this._close();
          }}
          @click=${() => this._close()}
        >
          <div class="panel allocation-modal-panel" role="document" @click=${(e) => e.stopPropagation()}>
            <header class="allocation-modal-head">
              <div aria-live="polite">${this._renderBody()}</div>
            </header>
          </div>
        </dialog>
      `;
    }
    const fixed = !this.container;
    let style = "top:50%;left:50%;transform:translate(-50%,-50%)"; // no anchor: centre over the container
    let side = "centre";
    if (this.anchor?.isConnected) {
      let containerRect;
      if (this.container) {
        const c = this.container.getBoundingClientRect();
        // Cap "room below" at the viewport: the container can be taller than the screen.
        containerRect = { top: c.top, left: c.left, right: c.right, bottom: Math.min(c.bottom, window.innerHeight) };
      } else {
        containerRect = { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight };
      }
      const p = popoverPlacement(this.anchor.getBoundingClientRect(), this._cardSize, containerRect);
      style = `top:${p.top}px;left:${p.left}px;--notch-left:${p.notch}px`;
      side = p.side;
    }
    return html`
      <div class="allocation-popover-scrim${fixed ? " allocation-popover--fixed" : ""}" @click=${() => this._close()}></div>
      <div
        class="allocation-inspect allocation-popover${fixed ? " allocation-popover--fixed" : ""}"
        data-side=${side}
        role="dialog"
        aria-modal="false"
        aria-label=${this.label}
        aria-live="polite"
        style=${style}
      >
        ${this._renderBody()}
      </div>
    `;
  }
}

customElements.define("band-popover", BandPopover);
