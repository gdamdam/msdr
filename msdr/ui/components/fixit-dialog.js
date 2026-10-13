import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

/** Inline "How to fix" link for an error text. Looks the text up in the
 * server's fix-it table (msdr/fixit.py) and renders nothing when there is no
 * match, so it can sit next to any error string without noise. Clicking opens
 * a <fixit-dialog>. */
export class FixitLink extends LitElement {
  static properties = { text: { type: String }, _fix: { state: true }, _open: { state: true } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.text = "";
    this._fix = null;
    this._open = false;
  }

  willUpdate(changed) {
    if (!changed.has("text")) return;
    const asked = this.text;
    this._fix = null;
    if (!asked) return;
    api.fixitFor(asked).then((fix) => {
      if (asked === this.text) this._fix = fix;
    });
  }

  render() {
    if (!this._fix) return html``;
    return html`
      <button class="fixit-link" style="font-size:0.8em;padding:0 6px" @click=${() => (this._open = true)}>
        How to fix
      </button>
      ${this._open
        ? html`<fixit-dialog .fix=${this._fix} @close=${() => (this._open = false)}></fixit-dialog>`
        : ""}
    `;
  }
}

/** Modal showing a fix-it entry: why, numbered steps and up to three action
 * buttons (copy a command, open a link, copy diagnostics). */
export class FixitDialog extends LitElement {
  static properties = { fix: { type: Object }, _note: { state: true } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.fix = null;
    this._note = "";
  }

  firstUpdated() {
    const dlg = this.querySelector("dialog");
    if (dlg && !dlg.open) dlg.showModal();
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  async _copy(text, done) {
    try {
      await navigator.clipboard.writeText(text);
      this._note = done;
    } catch {
      this._note = "Copy failed: select the text and copy it manually.";
    }
  }

  async _run(action) {
    if (action.kind === "open_url") {
      window.open(action.value, "_blank", "noopener");
    } else if (action.kind === "copy_command") {
      await this._copy(action.value, "Command copied.");
    } else if (action.kind === "copy_diagnostics") {
      await this._copy(await api.diagnosticsText(), "Diagnostics copied.");
    }
  }

  render() {
    const f = this.fix;
    if (!f) return html``;
    return html`
      <dialog class="panel" style="max-width:34em" @close=${() => this._close()}>
        <h3 style="margin-top:0">${f.title}</h3>
        <p>${f.why}</p>
        <ol>
          ${f.steps.map((s) => html`<li>${s}</li>`)}
        </ol>
        <div class="control-row">
          ${f.actions.map((a) => html`<button @click=${() => this._run(a)}>${a.label}</button>`)}
          <span class="spacer"></span>
          <button @click=${(e) => e.target.closest("dialog").close()}>Close</button>
        </div>
        <p class="dim" role="status">${this._note}</p>
      </dialog>
    `;
  }
}

customElements.define("fixit-link", FixitLink);
customElements.define("fixit-dialog", FixitDialog);
