import { LitElement, html } from "../vendor/lit.js";
import { mhz, timeHM } from "../format.js";
import "./led-dot.js";
import "./tag-chips.js";

const GLYPH = {
  capture: "★",
  tune: "▸",
  back: "◂",
  forward: "▸▸",
  profile: "⚙",
  identified: "✓",
  observed: "◦",
};

export class JournalView extends LitElement {
  static properties = {
    entries: { type: Array },
    day: { type: String },
    days: { type: Array },
    tagsById: { type: Object },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.entries = [];
    this.day = "";
    this.days = [];
    this.tagsById = {};
  }

  _neighbor(offset) {
    const sorted = [...this.days].sort();
    const idx = sorted.indexOf(this.day);
    if (idx === -1) return null;
    return sorted[idx + offset] ?? null;
  }

  _click(entry) {
    if (entry.signal_id) {
      this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: entry.signal_id } }));
    } else if (entry.state) {
      this.dispatchEvent(
        new CustomEvent("tune-frequency", { detail: { hz: entry.state.vfo_hz } })
      );
    }
  }

  render() {
    const prev = this._neighbor(-1);
    const next = this._neighbor(1);
    return html`
      <div class="panel">
        <div style="display:flex;align-items:center;gap:8px">
          <h2 style="margin:0;flex:1;border-bottom:none;padding-bottom:0">Log</h2>
          <button ?disabled=${!prev} @click=${() => this.dispatchEvent(new CustomEvent("prev-day"))}>
            ◂
          </button>
          <span class="mono dim">${this.day}</span>
          <button ?disabled=${!next} @click=${() => this.dispatchEvent(new CustomEvent("next-day"))}>
            ▸
          </button>
        </div>
        ${this.entries.length
          ? html`
              <div class="list" style="margin-top:8px">
                ${this.entries.map((e) => this._row(e))}
              </div>
            `
          : html`<p class="empty">nothing logged this day</p>`}
      </div>
    `;
  }

  _row(entry) {
    const label = entry.signal_name ?? (entry.state ? mhz(entry.state.vfo_hz) : entry.kind);
    return html`
      <div class="row ${entry.kind === "observed" ? "dim" : ""}" @click=${() => this._click(entry)}>
        <span class="dim mono">${timeHM(entry.at)}</span>
        <led-dot .color=${entry.signal_color ?? null}></led-dot>
        <span>${GLYPH[entry.kind] ?? "?"}</span>
        <span>${label}</span>
        <span class="dim">${entry.state?.demodulator ?? ""}</span>
        <tag-chips .tagIds=${entry.tag_ids ?? []} .tagsById=${this.tagsById}></tag-chips>
        <span class="spacer"></span>
        ${entry.signal_power_db != null
          ? html`<span class="mono dim" style="font-size:0.85em"
              >${entry.signal_power_db.toFixed(0)} dB${entry.signal_snr_db != null
                ? ` / ${entry.signal_snr_db.toFixed(0)} snr`
                : ""}</span
            >`
          : ""}
      </div>
    `;
  }
}

customElements.define("journal-view", JournalView);
