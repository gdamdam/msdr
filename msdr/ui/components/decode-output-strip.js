import { LitElement, html } from "../vendor/lit.js";
import "./cw-transcript.js";
import { callLineText, epochHM, integrityBadge } from "../format.js";

/** Latest received decoder event, shown in Discover without implying that a
 * candidate is verified. Full history and details open in Decode messages. */
export class DecodeOutputStrip extends LitElement {
  static properties = {
    event: { type: Object },
    events: { type: Array },
    status: { type: Object },
    onOpen: { attribute: false },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.event = null;
    this.events = [];
    this.status = null;
    this.onOpen = null;
  }

  render() {
    const event = this.event;
    const kindLabel = event?.kind === "measurement"
      ? "MEASUREMENT"
      : event?.kind === "sync"
        ? "SYNC"
        : event?.kind === "time"
          ? "TIME DECODE"
          : "DECODED OUTPUT";
    const fieldsText = event?.fields
      ? Object.entries(event.fields).map(([key, value]) => `${key} ${value}`).join(" · ")
      : "";
    const text = event
      ? (event.kind === "call" ? callLineText(event) : "") || event.text || fieldsText || "decoder event"
      : "No decoded output yet — start a decode from the DECODE deck";
    const badge = event ? integrityBadge(event.integrity, event.integrity_note) : null;
    const replay = typeof event?.source_id === "string" && event.source_id.startsWith("replay:");
    return html`<section class="panel decode-output-strip" aria-label="Latest decoded output">
      <div class="decode-output-heading">
        <span class="label">LATEST DECODED OUTPUT</span>
        <span class="spacer"></span>
        <button class="decode-output-open" title="Open decoded messages" @click=${() => this.onOpen?.()}>DECODE</button>
      </div>
      ${event?.decoder === "cw"
        ? html`<cw-transcript .events=${this.events} .frequencyHz=${event.frequency_hz ?? null}
            .since=${this.status?.active ? this.status.since : null} .compact=${true}></cw-transcript>`
        : event
        ? html`<div class="decode-output-line">
            <span class="dim mono">${kindLabel} · ${epochHM(event.ts)} · ${(event.decoder ?? "").toUpperCase()}</span>
            <span class="mono decode-output-message" title=${text} aria-label=${text}>${text}</span>
            ${badge
              ? html`<span class="chip integrity-badge ${badge.cls}" title=${badge.title}>${badge.text}</span>`
              : ""}
            ${replay ? html`<span class="chip tag-replay">replay</span>` : ""}
          </div>`
        : html`<div class="decode-output-line dim">${text}</div>`}
    </section>`;
  }
}

customElements.define("decode-output-strip", DecodeOutputStrip);
