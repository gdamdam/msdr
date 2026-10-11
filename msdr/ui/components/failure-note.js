import { LitElement, html } from "../vendor/lit.js";

/** Small inline "subject failed: detail  ×" note. Panels render one when
 * app.js's `failure` state names them as its source (see app.js's
 * _reportFailure/_failureFor) -- a persistent, section-local alternative to
 * the footer's single flash-then-vanish error, for the panel where the
 * failure actually happened. Dismissing only clears app.js's failure state
 * (via `dismiss`); the footer message is untouched. */
export class FailureNote extends LitElement {
  static properties = { failure: { type: Object } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.failure = null;
  }

  render() {
    if (!this.failure) return html``;
    const { subject, status, detail } = this.failure;
    const detailText = detail ? `: ${detail}` : "";
    return html`
      <div class="row" style="cursor:default;color:var(--danger);border:1px solid var(--danger)">
        <span class="mono" style="font-size:0.85em">${subject ?? "action"} ${status ?? "failed"}${detailText}</span>
        <span class="spacer"></span>
        <button
          title="dismiss"
          style="font-size:0.75em;padding:1px 6px"
          @click=${() => this.dispatchEvent(new CustomEvent("dismiss"))}
        >
          ×
        </button>
      </div>
    `;
  }
}

customElements.define("failure-note", FailureNote);
