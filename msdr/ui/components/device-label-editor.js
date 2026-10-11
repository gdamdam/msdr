import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

// docs/history.md "Labels": a private BLE address rotates, so a label on it
// would silently stop matching once the controller picks a new one -- the
// server refuses these with 422, and this editor doesn't even offer the
// form for them.
export const ROTATING_BLE_KINDS = ["rpa", "nrpa"];

export const CLASS_OPTIONS = [
  { value: "mine", label: "Mine" },
  { value: "known", label: "Known" },
  { value: "ignore", label: "Ignore" },
];

/** Why the label form is replaced by an explanatory note, or null when the
 * form should render normally. */
export function rotatingBlocker(receiver, kind) {
  if (receiver === "ble" && ROTATING_BLE_KINDS.includes(kind)) {
    return "This address rotates, so a label would stop matching. Rotation is a privacy protection, so msdr does not link rotating addresses.";
  }
  return null;
}

/** device-label-editor: the mine/known/ignore label form shown in the Wi-Fi
 * and BLE detail panes and in timeline-view's row detail (docs/history.md
 * "Labels" / "Shared timeline (UI)"). Emits `label-changed` with the saved
 * label object, or null after a remove, so the host can refresh its own
 * list without a full reload (ble-view.js's `listLabels` fetch, for one). */
export class DeviceLabelEditor extends LitElement {
  static properties = {
    receiver: { type: String },
    ident: { type: String },
    kind: { type: String },
    label: { type: Object },
    _name: { state: true },
    _note: { state: true },
    _class: { state: true },
    _busy: { state: true },
    _error: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.receiver = null;
    this.ident = null;
    this.kind = null;
    this.label = null;
    this._busy = false;
    this._error = null;
    this._resetForm();
  }

  _resetForm() {
    this._name = this.label?.name ?? "";
    this._note = this.label?.note ?? "";
    this._class = this.label?.class ?? "known";
  }

  updated(changed) {
    // A new device selected (or its label refetched) -- resync the form
    // from props instead of carrying over the previous device's draft.
    if (changed.has("ident") || changed.has("receiver") || changed.has("label")) {
      if (!this._busy) this._resetForm();
    }
  }

  async _save(e) {
    e.preventDefault();
    this._busy = true;
    this._error = null;
    try {
      const saved = await api.putLabel(this.receiver, this.ident, {
        name: this._name.trim(),
        note: this._note.trim() || undefined,
        class: this._class,
      });
      this.label = saved;
      this.dispatchEvent(new CustomEvent("label-changed", { bubbles: true, composed: true, detail: saved }));
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  async _remove() {
    this._busy = true;
    this._error = null;
    try {
      await api.deleteLabel(this.receiver, this.ident);
      this.label = null;
      this._resetForm();
      this.dispatchEvent(new CustomEvent("label-changed", { bubbles: true, composed: true, detail: null }));
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy = false;
    }
  }

  render() {
    const blocker = rotatingBlocker(this.receiver, this.kind);
    if (blocker) {
      return html`<section class="device-label-editor">
        <h4>Label</h4>
        <p class="dim">${blocker}</p>
      </section>`;
    }
    return html`<section class="device-label-editor">
      <h4>Label</h4>
      <form @submit=${(e) => this._save(e)}>
        <label class="device-label-field">Name
          <input type="text" .value=${this._name} maxlength="80" required
            @input=${(e) => { this._name = e.target.value; }} />
        </label>
        <label class="device-label-field">Note
          <textarea maxlength="500" rows="2" .value=${this._note}
            @input=${(e) => { this._note = e.target.value; }}></textarea>
        </label>
        <div class="device-label-class" role="radiogroup" aria-label="Class">
          ${CLASS_OPTIONS.map((c) => html`<label class="chip device-label-chip device-label-chip-${c.value}">
            <input type="radio" name="class" value=${c.value} ?checked=${this._class === c.value}
              @change=${() => { this._class = c.value; }} /> ${c.label}</label>`)}
        </div>
        <div class="control-row">
          <button type="submit" ?disabled=${this._busy || !this._name.trim()}>${this._busy ? "Saving…" : "Save"}</button>
          ${this.label ? html`<button type="button" ?disabled=${this._busy} @click=${() => this._remove()}>Remove label</button>` : ""}
        </div>
        ${this._error ? html`<p class="ble-problem" role="alert">${this._error}</p>` : ""}
      </form>
    </section>`;
  }
}

customElements.define("device-label-editor", DeviceLabelEditor);
