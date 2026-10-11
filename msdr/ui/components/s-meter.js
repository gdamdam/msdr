import { LitElement, html } from "../vendor/lit.js";

// Classic S-meter mapping (SDRconnect/SDRuno style): S9 = -73 dBm, 6 dB per
// S-unit below that, 3 dB per bar segment. 16 segments cover S1..S9 (48 dB),
// the remaining 20 cover +60 dB over S9.
const SEG_DB = 3;
const GREEN_SEGMENTS = 16;
const RED_SEGMENTS = 20;
const TOTAL_SEGMENTS = GREEN_SEGMENTS + RED_SEGMENTS;
const S9_DBM = -73;
const S1_DBM = -121;

/** Power in dBm -> count of lit segments (0..36), 0 when power is missing. */
function litCount(power) {
  if (power === null || power === undefined) return 0;
  return Math.min(TOTAL_SEGMENTS, Math.max(0, Math.floor((power - S1_DBM) / SEG_DB)));
}

/** Power in dBm -> S-reading text, e.g. "S0", "S7", "S9+20"; "--" when missing. */
function sReading(power) {
  if (power === null || power === undefined) return "--";
  if (power < S1_DBM) return "S0";
  if (power <= S9_DBM) {
    const unit = Math.min(9, 1 + Math.floor((power - S1_DBM) / 6));
    return `S${unit}`;
  }
  const over = Math.min(60, Math.round((power - S9_DBM) / 10) * 10);
  return `S9+${over}`;
}

/** Classic analog-style S-meter: SNR/PWR text line, S reading, and a
 * segmented bar (green S1..S9 region, red +60dB-over-S9 region) with a
 * tick-marked scale beneath. */
export class SMeter extends LitElement {
  static properties = {
    power: { type: Number },
    snr: { type: Number },
    compact: { type: Boolean }, // S1 receiver strip's row 2 -- shorter bar, smaller text, no tick scale
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.power = null;
    this.snr = null;
    this.compact = false;
  }

  render() {
    const lit = litCount(this.power);
    const snrText = this.snr != null ? `SNR ${this.snr.toFixed(1)} dB` : "SNR --";
    const pwrText = this.power != null ? `PWR ${this.power.toFixed(1)} dBm` : "PWR --";

    const greenSegs = Array.from(
      { length: GREEN_SEGMENTS },
      (_, i) => html`<span class="smeter-seg ${i < lit ? "lit green" : ""}"></span>`
    );
    const redSegs = Array.from({ length: RED_SEGMENTS }, (_, i) => {
      const idx = GREEN_SEGMENTS + i;
      return html`<span class="smeter-seg ${idx < lit ? "lit red" : ""}"></span>`;
    });

    return html`
      <div class="smeter ${this.compact ? "compact" : ""}">
        <div class="smeter-text mono">
          <span class="dim">${snrText}&nbsp;&nbsp;&nbsp;${pwrText}</span>
          <span class="smeter-sreading">${sReading(this.power)}</span>
        </div>
        <div class="smeter-bar">
          <div class="smeter-zone smeter-zone-green">${greenSegs}</div>
          <div class="smeter-zone smeter-zone-red">${redSegs}</div>
        </div>
        <div class="smeter-scale mono">
          <div class="smeter-zone smeter-zone-green">
            <span class="smeter-tick">S1</span>
            <span class="smeter-tick">S3</span>
            <span class="smeter-tick">S5</span>
            <span class="smeter-tick">S7</span>
            <span class="smeter-tick">S9</span>
          </div>
          <div class="smeter-zone smeter-zone-red">
            <span class="smeter-tick red">+20</span>
            <span class="smeter-tick red">+40</span>
            <span class="smeter-tick red">+60</span>
          </div>
        </div>
      </div>
    `;
  }
}

customElements.define("s-meter", SMeter);
