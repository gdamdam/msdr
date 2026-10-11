import { LitElement, html } from "../vendor/lit.js";
import { mhz } from "../format.js";
import * as api from "../api.js";

/** Persisted sweep sessions and their accumulated frequency observations. */
export class BandScanInbox extends LitElement {
  static properties = { scans: { state: true }, error: { state: true }, details: { state: true } };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.scans = [];
    this.error = "";
    this.details = {};
  }

  connectedCallback() {
    super.connectedCallback();
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 3000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this.timer);
  }

  async refresh() {
    try {
      const status = await api.getSurveyBandScan();
      this.scans = status.history ?? [];
      this.error = "";
      const openIds = [...this.querySelectorAll("details.band-scan-session[open]")]
        .map(element => element.dataset.scanId).filter(Boolean);
      for (const id of openIds) {
        const summary = this.scans.find(scan => scan.id === id);
        if (summary && this.details[id]?.updated_at !== summary.updated_at) {
          await this._fetchDetails(id);
        }
      }
    } catch (error) {
      this.error = error.message;
    }
  }

  async _loadDetails(event, id) {
    if (!event.currentTarget.open || this.details[id]) return;
    await this._fetchDetails(id);
  }

  async _fetchDetails(id) {
    try {
      const detail = await api.fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}/summary`);
      this.details = { ...this.details, [id]: detail };
    } catch (error) {
      this.error = error.message;
    }
  }

  _open(hz) {
    this.dispatchEvent(new CustomEvent("signal-open", {
      detail: { frequency_hz: hz }, bubbles: true, composed: true,
    }));
  }

  render() {
    return html`<section class="panel band-scan-inbox" aria-label="Survey band scan history">
      <h2>Survey band scans</h2>
      <p class="dim">Saved sweep results across spans. Uncovered spans include failed tunes, missing fresh frames, retunes, and interrupted dwells.</p>
      ${this.error ? html`<p role="alert">${this.error}</p>` : ""}
      ${this.scans.length ? this.scans.map(scan => {
        const payload = this.details[scan.id]?.payload ?? {};
        const results = payload.results ?? [];
        const spans = payload.spans ?? [];
        const uncovered = spans.filter(span => span.state === "uncovered");
        const config = scan.config ?? {};
        return html`<details class="band-scan-session" data-scan-id=${scan.id} @toggle=${event => this._loadDetails(event, scan.id)}>
          <summary><strong>${(config.start_hz / 1e6).toFixed(3)}–${(config.end_hz / 1e6).toFixed(3)} MHz</strong>
            <span>${scan.state}</span><span>${scan.result_count} signals</span>
          <span>${scan.covered_count}/${scan.total_spans} covered · ${scan.span_count} attempted</span>
            <span>${new Date(scan.started_at * 1000).toLocaleString()}</span></summary>
          ${this.details[scan.id] && uncovered.length ? html`<p class="band-scan-uncovered"><strong>Uncovered:</strong>
            ${uncovered.map(s => `${(s.start_hz / 1e6).toFixed(3)}–${(s.end_hz / 1e6).toFixed(3)} MHz${s.reason ? ` (${s.reason})` : ""}`).join(" · ")}</p>` : ""}
          ${this.details[scan.id] && scan.total_spans > scan.span_count ? html`<p class="band-scan-uncovered">${scan.total_spans - scan.span_count} spans were not visited.</p>` : ""}
          ${this.details[scan.id] && results.length ? html`<div class="band-scan-results">
            ${results.map(row => html`<details class="band-scan-result">
              <summary><strong>${mhz(row.frequency_hz)}</strong>
                <span>${row.observation_count ?? row.observations?.length ?? 0} samples</span>
                <span>${row.span_centers_hz?.length ?? 0} spans</span></summary>
              <button class="band-scan-open" @click=${() => this._open(row.frequency_hz)}>Open frequency review</button>
              <div class="band-scan-observations">
                ${(row.observations ?? []).map(obs => html`<div class="band-scan-observation">
                  <time>${new Date(obs.observed_at * 1000).toLocaleString()}</time>
                  <span>${obs.snr_db == null ? "SNR —" : `SNR ${obs.snr_db.toFixed(1)} dB`}</span>
                  <span>${(obs.spectrum_bandwidth_hz ?? obs.width_hz ?? obs.bw_hz) == null ? (obs.bw_class ?? "BW —") : `${Math.round(obs.spectrum_bandwidth_hz ?? obs.width_hz ?? obs.bw_hz)} Hz`}</span>
                  <span>${obs.family?.name ?? obs.confirmed?.name ?? obs.rds?.ps ?? ""}</span>
                  <span>${(obs.span_center_hz / 1e6).toFixed(3)} MHz span</span>
                </div>`)}
              </div>
            </details>`)}
          </div>` : this.details[scan.id] ? html`<p class="dim">No signals were observed during this sweep.</p>` : html`<p class="dim">Open to load saved results.</p>`}
        </details>`;
      }) : html`<p class="dim">Completed band scans will appear here.</p>`}
    </section>`;
  }
}

customElements.define("band-scan-inbox", BandScanInbox);
