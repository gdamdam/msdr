import { LitElement, html } from "../vendor/lit.js";
import { epochHM, mhz } from "../format.js";
import { cwTranscripts } from "../cw-transcript.js";

/** Shared, selectable CW text for the Listen deck and message history. */
export class CwTranscript extends LitElement {
  static properties = {
    events: { type: Array },
    since: { type: Number },
    frequencyHz: { type: Number },
    compact: { type: Boolean },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.events = [];
    this.since = null;
    this.frequencyHz = null;
    this.compact = false;
    this._followEnd = true;
  }

  willUpdate() {
    const box = this.querySelector(".cw-transcript-scroll");
    if (box) this._followEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }

  updated() {
    const box = this.querySelector(".cw-transcript-scroll");
    if (box && this._followEnd) box.scrollTop = box.scrollHeight;
  }

  render() {
    const groups = cwTranscripts(this.events, {
      since: this.since,
      frequencyHz: this.frequencyHz,
    });
    if (!groups.length) return html``;
    return html`<section class="cw-transcript ${this.compact ? "cw-transcript-compact" : ""}" aria-label="CW transcript">
      <div class="label">CW TRANSCRIPT</div>
      <div class="cw-transcript-scroll" tabindex="0">
        ${groups.map((group) => html`<div class="cw-transcript-group">
          <div class="dim mono cw-transcript-meta">${epochHM(group.firstTs)} · ${group.frequencyHz != null ? mhz(group.frequencyHz) : "unknown frequency"}${group.sourceId?.startsWith("replay:") ? " · replay" : ""}</div>
          <div class="mono cw-transcript-text">${group.text}</div>
        </div>`)}
      </div>
    </section>`;
  }
}

customElements.define("cw-transcript", CwTranscript);
