import { LitElement, html } from "../vendor/lit.js";

/** Footer status line: link state + last action on the left (replaces
 * toasts -- an API error shows here in red for a few seconds), version on
 * the right. */
export class StatusLine extends LitElement {
  static properties = {
    connected: { type: Boolean },
    started: { type: Boolean },
    message: { type: String },
    error: { type: Boolean },
    version: { type: String },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.connected = false;
    this.started = true;
    this.message = "";
    this.error = false;
    this.version = null;
  }

  render() {
    // Stopped (Play not pressed in SDRconnect) takes over the link text so
    // it stays visible even when there's no lastAction/error to show it.
    const linkText = !this.connected
      ? "link down"
      : !this.started
        ? "press Play in SDRconnect"
        : "link ok";
    const text = this.error ? this.message : this.message ? `${linkText} · ${this.message}` : linkText;
    return html`
      <div id="status-line">
        <span class="${this.error ? "status-error" : "dim"}">${text}</span>
        ${this.error
          ? html`<button
              title="dismiss"
              style="font-size:0.75em;padding:1px 6px"
              @click=${() => this.dispatchEvent(new CustomEvent("dismiss"))}
            >
              ×
            </button>`
          : ""}
        <span class="spacer"></span>
        <span class="dim">msdr${this.version ? ` v${this.version}` : ""}</span>
      </div>
    `;
  }
}

customElements.define("status-line", StatusLine);
