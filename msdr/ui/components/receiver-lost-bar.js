import { LitElement, html } from "../vendor/lit.js";

const HELP = "A receiver msdr was using went away. msdr only listens, so nothing was transmitted; retry once the hardware is reconnected.";

/** One entry per lost receiver. Busy/error per entry are set by app.js, which
 * owns the retry request, so a re-render never loses them. */
export class ReceiverLostBar extends LitElement {
  static properties = {
    alerts: { type: Array },
    notify: { type: Boolean },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.alerts = [];
    this.notify = false;
  }

  _emit(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  render() {
    if (!this.alerts?.length) return html``;
    return html`<div class="receiver-lost-bar" role="alert" data-help=${HELP}>
      ${this.alerts.map((a) => html`<div class="receiver-lost-entry">
        <div class="receiver-lost-text">
          <strong>${a.title}</strong>
          <span>${a.detail}</span>
          ${a.error ? html`<span class="receiver-lost-error">Retry failed: ${a.error}</span>` : ""}
        </div>
        <button ?disabled=${a.busy} @click=${() => this._emit("retry", { kind: a.kind })}>${a.busy ? "Retrying…" : "Retry"}</button>
        <button @click=${() => this._emit("dismiss", { kind: a.kind })}>Dismiss</button>
      </div>`)}
      ${typeof Notification === "undefined" ? "" : html`<label class="receiver-lost-notify">
        <input type="checkbox" .checked=${!!this.notify}
          @change=${(ev) => this._emit("notify-pref", { on: ev.target.checked })} />
        Also notify me when msdr isn't the front window
      </label>`}
    </div>`;
  }
}

customElements.define("receiver-lost-bar", ReceiverLostBar);
