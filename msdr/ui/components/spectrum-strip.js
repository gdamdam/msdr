import { LitElement, html } from "../vendor/lit.js";

const BACKING_WIDTH = 640;
const BACKING_HEIGHT = 64;

/** Live spectrum-activity strip for the SPECTRUM scanner source: draws the
 * 128-bin power sweep (see `scan.spectrum` in the "scan" websocket status)
 * as a bar graph, with the noise floor, hold threshold, candidate/locked-out
 * ticks and the current VFO position overlaid. Cheap redraw on every status
 * update -- no animation loop. */
export class SpectrumStrip extends LitElement {
  static properties = {
    bins: { type: Array },
    floor: { type: Number },
    thresholdDb: { type: Number },
    candidates: { type: Array },
    lockedOut: { type: Array },
    centerHz: { type: Number },
    sampleRateHz: { type: Number },
    vfoHz: { type: Number },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.bins = [];
    this.floor = 0;
    this.thresholdDb = 20;
    this.candidates = [];
    this.lockedOut = [];
    this.centerHz = null;
    this.sampleRateHz = null;
    this.vfoHz = null;
  }

  updated() {
    this._draw();
  }

  _hzToX(hz) {
    if (this.centerHz == null || !this.sampleRateHz) return null;
    const lowHz = this.centerHz - this.sampleRateHz / 2;
    const frac = (hz - lowHz) / this.sampleRateHz;
    if (frac < 0 || frac > 1) return null;
    return frac * BACKING_WIDTH;
  }

  _cssVar(name, fallback) {
    const v = getComputedStyle(this).getPropertyValue(name).trim();
    return v || fallback;
  }

  _draw() {
    const canvas = this.querySelector("canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;

    const well = this._cssVar("--panel-well", "#0c0e11");
    const accent = this._cssVar("--accent", "#ffb000");
    const unlit = this._cssVar("--unlit", "#3a2f18");
    const danger = this._cssVar("--danger", "#ff5b5b");

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = well;
    ctx.fillRect(0, 0, width, height);

    const bins = this.bins ?? [];
    const n = bins.length;
    if (n) {
      const barW = width / n;
      ctx.fillStyle = accent;
      for (let i = 0; i < n; i++) {
        const v = Math.max(0, Math.min(255, Number(bins[i]) || 0));
        const barH = (v / 255) * height;
        ctx.fillRect(i * barW, height - barH, Math.max(1, barW - 1), barH);
      }
    }

    if (this.floor != null) {
      const y = height - (Math.max(0, Math.min(255, this.floor)) / 255) * height;
      ctx.strokeStyle = unlit;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(width, y + 0.5);
      ctx.stroke();
    }

    if (this.floor != null && this.thresholdDb != null) {
      const level = Math.max(0, Math.min(255, this.floor + this.thresholdDb));
      const y = height - (level / 255) * height;
      ctx.strokeStyle = danger;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(width, y + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    ctx.fillStyle = accent;
    for (const hz of this.candidates ?? []) {
      const x = this._hzToX(hz);
      if (x == null) continue;
      ctx.fillRect(Math.max(0, x - 1), 0, 2, 6);
    }

    ctx.fillStyle = unlit;
    for (const hz of this.lockedOut ?? []) {
      const x = this._hzToX(hz);
      if (x == null) continue;
      ctx.fillRect(Math.max(0, x - 1), 0, 2, 6);
    }

    if (this.vfoHz != null) {
      const x = this._hzToX(this.vfoHz);
      if (x != null) {
        ctx.strokeStyle = accent;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x + 0.5, 0);
        ctx.lineTo(x + 0.5, height);
        ctx.stroke();
      }
    }
  }

  get _ariaLabel() {
    const n = this.candidates?.length ?? 0;
    const floorLabel = this.floor != null ? Math.round(this.floor) : "--";
    return `spectrum: ${n} candidate${n === 1 ? "" : "s"}, floor ${floorLabel}`;
  }

  render() {
    const haveSpan = this.centerHz != null && this.sampleRateHz;
    const lowMhz = haveSpan ? (this.centerHz - this.sampleRateHz / 2) / 1_000_000 : null;
    const highMhz = haveSpan ? (this.centerHz + this.sampleRateHz / 2) / 1_000_000 : null;
    return html`
      <div style="display:flex;flex-direction:column;gap:2px">
        <canvas
          width=${BACKING_WIDTH}
          height=${BACKING_HEIGHT}
          role="img"
          aria-label=${this._ariaLabel}
          style="width:100%;height:64px;display:block;background:var(--panel-well)"
        ></canvas>
        <div class="mono dim" style="display:flex;justify-content:space-between;font-size:0.75em">
          <span>${lowMhz != null ? `${lowMhz.toFixed(3)} MHz` : ""}</span>
          <span>${highMhz != null ? `${highMhz.toFixed(3)} MHz` : ""}</span>
        </div>
      </div>
    `;
  }
}

customElements.define("spectrum-strip", SpectrumStrip);
