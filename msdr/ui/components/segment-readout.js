import { LitElement, html } from "../vendor/lit.js";
import { parseFrequencyInput } from "../format.js";

const HINT = "Enter to tune · Esc to cancel · e.g. 160.000 MHz, 145800 kHz";

// A single click is deferred by this long before it actually steps the
// frequency, so a second click landing within the window (making the pair a
// dblclick) can cancel it -- see _onReadoutClick/_onReadoutDblClick.
const DBLCLICK_DELAY_MS = 250;
const FINE_HOLD_DELAY_MS = 450;
const FINE_REPEAT_MS = 150;

export function fineStepHz(heldMs) {
  return heldMs >= 3000 ? 1000 : heldMs >= 1200 ? 500 : 100;
}

/** Segmented-display frequency readout: amber digits over a static ghost
 * layer of unlit digits, same font/metrics, absolutely positioned -- reads as
 * a 7-segment display without shipping a font. When `editable`, clicking the
 * upper/lower half of a digit steps the frequency up/down by that digit's
 * place value (plain Hz arithmetic, so carries/borrows propagate and it never
 * goes below 0); double-click (or a caller invoking startEdit(), e.g. app.js's
 * "f" key) swaps it for a mono text input pre-filled with the current MHz --
 * Enter parses the text (see parseFrequencyInput) and emits tune-frequency,
 * Esc/blur cancels. Both step and typed-entry emit the same tune-frequency
 * detail shape. */
export class SegmentReadout extends LitElement {
  static properties = {
    hz: { type: Number },
    unit: { type: String },
    editable: { type: Boolean },
    editControl: { attribute: false },
    infoControl: { attribute: false },
    compact: { type: Boolean }, // smaller receiver-strip digits
    fineTune: { type: Boolean }, // receiver's adjacent +/- fine controls
    _editing: { state: true },
    _text: { state: true },
    _invalid: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.hz = null;
    this.unit = "MHz";
    this.editable = false;
    this.editControl = null;
    this.infoControl = null;
    this.compact = false;
    this.fineTune = false;
    this._editing = false;
    this._text = "";
    this._invalid = false;
    this._clickTimer = null;
    this._fineTimer = null;
    this._finePointerId = null;
    this._onFineBlur = () => this._stopFineHold();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearTimeout(this._clickTimer);
    this._stopFineHold();
  }

  /** Public entry point for a caller (instrument-panel's "f" keyboard
   * shortcut) that doesn't have a click event to work with. No-op when not
   * editable, e.g. disconnected/not controllable. */
  startEdit() {
    if (!this.editable || this._editing) return;
    clearTimeout(this._clickTimer);
    this._text = this.hz === null || this.hz === undefined ? "" : (this.hz / 1_000_000).toFixed(6);
    this._invalid = false;
    this._editing = true;
    this.updateComplete.then(() => this.querySelector("input")?.select());
  }

  _cancelEdit() {
    this._editing = false;
    this._invalid = false;
  }

  _commitEdit() {
    const hz = parseFrequencyInput(this._text);
    if (hz === null) {
      this._invalid = true;
      return;
    }
    this._editing = false;
    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
  }

  _onEditKeydown(ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      this._commitEdit();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      this._cancelEdit();
    }
  }

  /** Derive place values from the actual displayed frequency, including HF/GHz. */
  _stepAt(ev) {
    const rect = ev.currentTarget.getBoundingClientRect();
    if (!rect.width || !Number.isFinite(this.hz)) return null;
    const text = (this.hz / 1_000_000).toFixed(6);
    const idx = Math.floor(((ev.clientX - rect.left) / rect.width) * text.length);
    if (idx < 0 || idx >= text.length || !/\d/.test(text[idx])) return null;
    const remainingDigits = text.slice(idx + 1).replace(/\D/g, "").length;
    const place = 10 ** remainingDigits;
    return ev.clientY - rect.top < rect.height / 2 ? place : -place;
  }

  _onReadoutClick(ev) {
    if (!this.editable) return;
    clearTimeout(this._clickTimer);
    const delta = this._stepAt(ev);
    if (delta === null) return;
    // Deferred so a following click within the window (i.e. this click
    // turning out to be half of a dblclick) can cancel the step instead of
    // stepping the frequency on the way into text-edit mode.
    this._clickTimer = setTimeout(() => this._step(delta), DBLCLICK_DELAY_MS);
  }

  _onReadoutDblClick() {
    if (!this.editable) return;
    clearTimeout(this._clickTimer);
    this.startEdit();
  }

  _step(deltaHz) {
    const hz = Math.max(0, (this.hz ?? 0) + deltaHz);
    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: { hz } }));
  }

  _emitFineStep(direction, heldMs = 0) {
    if (!this.editable) return;
    this.dispatchEvent(new CustomEvent("step-frequency", {
      detail: { direction, fixedStepHz: fineStepHz(heldMs) },
    }));
  }

  _stopFineHold(ev) {
    if (ev && this._finePointerId !== ev.pointerId) return;
    clearTimeout(this._fineTimer);
    this._fineTimer = null;
    this._finePointerId = null;
    window.removeEventListener("blur", this._onFineBlur);
  }

  _startFineHold(ev, direction) {
    if (!this.editable || ev.button !== 0 || this._finePointerId !== null) return;
    ev.preventDefault();
    this._finePointerId = ev.pointerId;
    ev.currentTarget.setPointerCapture(ev.pointerId);
    window.addEventListener("blur", this._onFineBlur);
    this._emitFineStep(direction);
    const started = performance.now();
    const repeat = () => {
      if (this._finePointerId !== ev.pointerId || !this.editable) {
        this._stopFineHold();
        return;
      }
      this._emitFineStep(direction, performance.now() - started);
      this._fineTimer = setTimeout(repeat, FINE_REPEAT_MS);
    };
    this._fineTimer = setTimeout(repeat, FINE_HOLD_DELAY_MS);
  }

  _fineButtons() {
    if (!this.fineTune) return "";
    return html`<span class="seg-fine-tune" role="group" aria-label="Fine tuning">
      ${[-1, 1].map(direction => html`<button type="button"
        aria-label=${direction < 0 ? "Fine tune down" : "Fine tune up"}
        title=${direction < 0 ? "−100 Hz; hold to tune faster" : "+100 Hz; hold to tune faster"}
        ?disabled=${!this.editable}
        @pointerdown=${ev => this._startFineHold(ev, direction)}
        @pointerup=${ev => this._stopFineHold(ev)}
        @pointercancel=${ev => this._stopFineHold(ev)}
        @lostpointercapture=${ev => this._stopFineHold(ev)}
        @click=${ev => { if (ev.detail === 0) this._emitFineStep(direction); }}
      >${direction < 0 ? "−" : "+"}</button>`)}
    </span>`;
  }

  render() {
    if (this._editing) {
      return html`
        <div class="seg-wrap ${this.compact ? "compact" : ""}">
          <input
            class="mono seg-input"
            type="text"
            inputmode="decimal"
            .value=${this._text}
            @input=${(ev) => {
              this._text = ev.target.value;
              this._invalid = false;
            }}
            @keydown=${(ev) => this._onEditKeydown(ev)}
            @blur=${() => this._cancelEdit()}
          />
          <span class="seg-unit label ${this._invalid ? "invalid" : ""}">${this._invalid ? "invalid" : HINT}</span>
        </div>
      `;
    }
    const value = this.hz === null || this.hz === undefined ? "--" : (this.hz / 1_000_000).toFixed(6);
    return html`
      <div class="seg-wrap ${this.compact ? "compact" : ""}">
        <div
          class="seg-readout ${this.editable ? "editable" : ""}"
          title=${this.editable
            ? "Click the upper/lower half of a digit to step it; double-click or press F to type a frequency"
            : ""}
          @click=${(ev) => this._onReadoutClick(ev)}
          @dblclick=${() => this._onReadoutDblClick()}
        >
          <span class="seg-ghost" aria-hidden="true">${value.replace(/\d/g, "8")}</span>
          <span class="seg-value">${value}</span>
        </div>
        <span class="seg-unit-wrap">
          ${this.infoControl ?? ""}
          <span class="seg-unit label">${this.unit}</span>
        </span>
        ${this._fineButtons()}
        ${this.editControl ?? ""}
      </div>
    `;
  }
}

customElements.define("segment-readout", SegmentReadout);
