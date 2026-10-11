import { LitElement, html } from "../vendor/lit.js";

/** "● RUNNING m:ss" beside a running START/STOP control -- the green half of
 * the START/STOP traffic light (styles.css: button.run-start / run-stop).
 * `since`: epoch seconds the job started (elapsed is shown only when known);
 * `label`: the word (RUNNING, SCANNING, REC ...). Ticks once a second only
 * while it has a `since`. */
export class RunIndicator extends LitElement {
  static properties = {
    since: { type: Number },
    label: { type: String },
  };

  constructor() {
    super();
    this.since = null;
    this.label = "RUNNING";
    this._timer = null;
  }

  createRenderRoot() {
    return this;
  }

  connectedCallback() {
    super.connectedCallback();
    this._timer = setInterval(() => {
      if (this.since) this.requestUpdate();
    }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    this._timer = null;
  }

  render() {
    let elapsed = "";
    if (this.since) {
      const s = Math.max(0, Math.floor(Date.now() / 1000 - this.since));
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const ss = String(s % 60).padStart(2, "0");
      elapsed = h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
    }
    return html`<span class="run-dot" aria-hidden="true">●</span><span role="status"
        >${this.label}${elapsed ? ` ${elapsed}` : ""}</span
      >`;
  }
}

customElements.define("run-indicator", RunIndicator);
