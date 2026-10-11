import { LitElement, html } from "../vendor/lit.js";
import { PRESETS } from "../presets.js";

/** Task presets modal (S1 contract, "Task presets"): opened from the
 * command palette ("presets…") and the strip's MORE area (PRESETS) --
 * app.js just flips `.open` via `presetOpen`. Same overlay-backdrop pattern
 * as setup-drawer.js/decode-view.js. Start is deliberately inert beyond
 * switching the view: no tune, no decoder, no recording, no receiver
 * ownership (see presets.js and app.js's _onPresetStart). */
export class PresetPicker extends LitElement {
  static properties = {
    open: { type: Boolean },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  _start(preset) {
    this.dispatchEvent(
      new CustomEvent("preset-start", {
        detail: { id: preset.id, workspace: preset.workspace, visible: preset.visible },
      }),
    );
    this.dispatchEvent(new CustomEvent("close"));
  }

  _onKeydown(ev) {
    if (ev.key === "Escape") this._close();
  }

  _presetCard(preset) {
    return html`
      <div class="row" style="cursor:default;flex-direction:column;align-items:stretch;gap:6px;padding:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <span class="label" style="flex:1;font-size:1em">${preset.label}</span>
          <button class="run-start" @click=${() => this._start(preset)}>START</button>
        </div>
        <ul style="margin:0;padding-left:18px;font-size:0.85em">
          ${preset.plan.map((line) => html`<li>${line}</li>`)}
        </ul>
        ${preset.recommended.length
          ? html`<p class="dim mono" style="font-size:0.8em;margin:0">
              recommended: ${preset.recommended.join(", ")}
            </p>`
          : ""}
      </div>
    `;
  }

  render() {
    if (!this.open) return html``;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel"
          role="dialog"
          aria-modal="true"
          aria-labelledby="preset-picker-title"
          tabindex="-1"
          style="margin:40px auto 0;width:min(560px,94vw);max-height:85vh;overflow:auto;align-self:flex-start;display:flex;flex-direction:column;gap:10px"
          @click=${(ev) => ev.stopPropagation()}
          @keydown=${(ev) => this._onKeydown(ev)}
        >
          <div style="display:flex;align-items:center">
            <h2 id="preset-picker-title" style="flex:1">Presets</h2>
            <button title="close" aria-label="close presets" @click=${() => this._close()}>&times;</button>
          </div>
          <p class="dim" style="font-size:0.85em;margin:0">
            Start only switches the view and visible controls; it never retunes, starts a decoder, arms recording or
            takes the receiver.
          </p>
          <div style="display:flex;flex-direction:column;gap:8px">${PRESETS.map((p) => this._presetCard(p))}</div>
        </div>
      </div>
    `;
  }
}

customElements.define("preset-picker", PresetPicker);
