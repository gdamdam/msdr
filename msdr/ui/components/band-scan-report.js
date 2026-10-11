import { LitElement, html, svg } from "../vendor/lit.js";
import * as api from "../api.js";
import { buildBandScanReport, bandScanFrequenciesCsv, activityText, signalKind } from "../band-scan-report-data.js";

const PAGE_SIZE = 25;
const number = value => typeof value === "number" && Number.isFinite(value);
const date = value => number(value) ? new Date(value * 1000).toLocaleString() : "—";
const dec = (value, digits = 1) => number(value) ? value.toFixed(digits) : "—";
const freq = value => number(value) ? (value / 1e6).toFixed(6) : "—";
const shortFreq = value => number(value) ? (value / 1e6).toFixed(3) : "—";
const candidateText = obs => [...obs].reverse().map(row => row.confirmed?.name ?? row.rds?.ps ?? row.recognized?.name ?? row.family?.name).find(Boolean) ?? "Unidentified signal";
const visitedSpan = row => typeof row.visited === "boolean" ? row.visited : row.visited_at != null || row.state !== "unvisited";
const extent = values => {
  let min = Infinity, max = -Infinity;
  for (const value of values) if (number(value)) { if (value < min) min = value; if (value > max) max = value; }
  return [min === Infinity ? -120 : min, max === -Infinity ? -20 : max];
};

/** A durable, inspectable report for each Survey band scan session. */
export class BandScanReport extends LitElement {
  static properties = {
    selectedId: { type: String },
    scans: { state: true },
    detail: { state: true },
    report: { state: true },
    error: { state: true },
    loading: { state: true },
    activeId: { state: true },
    view: { state: true },
    page: { state: true },
    query: { state: true },
    filter: { state: true },
    sort: { state: true },
    comparison: { state: true },
    compareId: { state: true },
    observationPage: { state: true },
    observationTotal: { state: true },
    observationsLoading: { state: true },
    spectrum: { state: true },
    spectrumLoading: { state: true },
    selectedFrequency: { state: true },
    minPeakSNR: { state: true },
    hoveredSpectrumFinding: { state: true },
  };

  createRenderRoot() { return this; }
  constructor() {
    super();
    this.selectedId = null;
    this.scans = [];
    this.detail = null;
    this.report = null;
    this.error = "";
    this.loading = true;
    this.activeId = null;
    this.view = "frequencies";
    this.page = 0;
    this.query = "";
    this.filter = "all";
    this.sort = "frequency";
    this.comparison = null;
    this.compareId = "";
    this.observationPage = null;
    this.observationTotal = 0;
    this.observationsLoading = false;
    this.spectrum = null;
    this.spectrumLoading = false;
    this.selectedFrequency = null;
    this.minPeakSNR = null;
    this.hoveredSpectrumFinding = null;
    this._summaryController = null;
    this._summaryInflight = null;
    this._summaryUpdatedAt = null;
    this._observationController = null;
    this._observationRequest = 0;
    this._spectrumController = null;
    this._spectrumRequest = 0;
    this._spectrumWindow = null;
    this._summaryRetryBlocked = false;
    this._refreshRequest = 0;
    this._deletedIds = new Set();
  }

  connectedCallback() {
    super.connectedCallback();
    this.refresh();
    this._timer = setInterval(() => this.refresh(), 3000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._summaryController?.abort();
    this._observationController?.abort();
    this._spectrumController?.abort();
    this._summaryInflight = null;
    super.disconnectedCallback();
  }

  updated(changed) {
    if (["view", "page", "query", "filter", "sort", "minPeakSNR"].some(key => changed.has(key))) {
      this.hoveredSpectrumFinding = null;
    }
    if (changed.has("selectedId") && this.selectedId && this.selectedId !== this.activeId) {
      this._select(this.selectedId);
    }
    if (this.activeId && this.detail && (changed.has("view") || changed.has("page") || changed.has("selectedFrequency") || changed.has("minPeakSNR"))) {
      if (this.view === "timeline") this._loadObservations();
      if (this.view === "spectrum") this._loadSpectrum();
    }
    if (this.view === "spectrum") this._drawSpectrogram();
  }

  _drawSpectrogram() {
    const canvas = this.querySelector(".band-report-spectrogram canvas");
    const slices = this.spectrum?.slices ?? [];
    if (!canvas || !slices.length) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const width = canvas.width, height = canvas.height;
    let lo = Infinity, hi = -Infinity;
    for (const slice of slices) for (const value of slice.values_db ?? []) if (number(value)) {
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
    if (lo === Infinity) lo = -120;
    if (hi === -Infinity) hi = -20;
    const [xmin, xmax] = this._spectrumWindow ?? [this.spectrum.start_hz, this.spectrum.end_hz];
    ctx.clearRect(0, 0, width, height);
    const ordered = [...slices].sort((a, b) => a.at - b.at);
    const times = ordered.map(slice => slice.at).filter(number);
    const duration = Math.max(1, times.at(-1) - times[0]);
    ordered.forEach((slice, row) => {
      const vals = slice.values_db ?? [];
      const py = (slice.at - times[0]) / duration * height;
      const cellH = 2;
      const centerIndex = number(slice.center_hz) && slice.bin_width_hz
        ? (slice.center_hz - slice.first_bin_hz) / slice.bin_width_hz : null;
      vals.forEach((value, col) => {
        if (!number(value)) return;
        const hz = slice.first_bin_hz + col * slice.bin_width_hz;
        const endHz = hz + slice.bin_width_hz;
        if (endHz < xmin || hz > xmax) return;
        const strength = Math.max(0, Math.min(1, (value - lo) / Math.max(1, hi - lo)));
        const exclusion = slice.center_exclusion_bins;
        const excluded = typeof exclusion === "number" && centerIndex != null && Math.abs(col - centerIndex) <= exclusion;
        ctx.fillStyle = excluded ? "#35383e" : `hsl(${240 - strength * 220} 78% ${24 + strength * 48}%)`;
        const px = (hz - xmin) / (xmax - xmin) * width;
        const cellW = slice.bin_width_hz / (xmax - xmin) * width;
        ctx.fillRect(px, py, cellW + .5, cellH + .5);
      });
    });
  }

  async refresh() {
    const request = ++this._refreshRequest;
    try {
      const status = await api.getSurveyBandScan();
      if (!this.isConnected || request !== this._refreshRequest) return;
      this.scans = (status.history ?? []).filter(scan => !this._deletedIds.has(scan.id));
      const wanted = this.activeId || this.selectedId || this.scans[0]?.id;
      if (wanted && wanted !== this.activeId) this._select(wanted);
      else if (wanted && !this.detail && !this._summaryInflight && !this._summaryRetryBlocked) this._load(wanted);
      else if (wanted) {
        const summary = this.scans.find(scan => scan.id === wanted);
        if (summary && summary.updated_at !== this._summaryUpdatedAt && !this._summaryInflight) this._load(wanted);
      } else this.loading = false;
    } catch (error) {
      if (this.isConnected) { this.error = error.message; this.loading = false; }
    }
  }

  _select(id) {
    this._summaryController?.abort();
    this._observationController?.abort();
    this._spectrumController?.abort();
    this._summaryInflight = null;
    this.activeId = id;
    this.error = "";
    this._summaryRetryBlocked = false;
    this.comparison = null;
    this.compareId = "";
    this.page = 0;
    this.view = "frequencies";
    this.detail = null;
    this.report = null;
    this.observationPage = null;
    this.observationTotal = 0;
    this.observationsLoading = false;
    this.spectrum = null;
    this.spectrumLoading = false;
    this.selectedFrequency = null;
    this._spectrumWindow = null;
    this.hoveredSpectrumFinding = null;
    this._load(id);
  }

  async _load(id) {
    if (this._summaryInflight?.id === id) return this._summaryInflight.promise;
    const controller = new AbortController();
    this._summaryController = controller;
    this.loading = true;
    const promise = (async () => {
      try {
        const detail = await api.fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}/summary`, { signal: controller.signal });
        if (this.isConnected && !controller.signal.aborted && id === this.activeId) {
          this.report = buildBandScanReport(detail);
          this.detail = detail;
          this._summaryUpdatedAt = detail.updated_at;
          this.error = "";
        }
      } catch (error) {
        if (this.isConnected && !controller.signal.aborted && id === this.activeId) {
          this._summaryRetryBlocked = true;
          this.error = `Could not load report: ${error.message}`;
        }
      } finally {
        if (this._summaryInflight?.controller === controller) this._summaryInflight = null;
        if (this.isConnected && !controller.signal.aborted && id === this.activeId) this.loading = false;
      }
    })();
    this._summaryInflight = { id, promise, controller };
    return promise;
  }

  _loadObservations() {
    const id = this.activeId;
    if (!id) return;
    this._observationController?.abort();
    const controller = new AbortController();
    this._observationController = controller;
    const request = ++this._observationRequest;
    this.observationPage = null;
    this.observationsLoading = true;
    const params = new URLSearchParams({ offset: String(this.page * PAGE_SIZE), limit: String(PAGE_SIZE) });
    if (number(this.selectedFrequency)) params.set("frequency_hz", String(this.selectedFrequency));
    if (number(this.minPeakSNR)) params.set("min_peak_snr_db", String(this.minPeakSNR));
    api.fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}/observations?${params}`, { signal: controller.signal })
      .then(result => {
        if (this.isConnected && request === this._observationRequest && id === this.activeId) {
          this.observationPage = result.observations ?? [];
          this.observationTotal = result.total ?? 0;
          this.observationsLoading = false;
          if (this.error.startsWith("Could not load observations:")) this.error = "";
        }
      }).catch(error => {
        if (!controller.signal.aborted && id === this.activeId) {
          this.observationsLoading = false;
          this.error = `Could not load observations: ${error.message}`;
        }
      });
  }

  _loadSpectrum() {
    if (this.spectrum || this.spectrumLoading || !this.activeId) return;
    const id = this.activeId;
    const controller = new AbortController();
    this._spectrumController = controller;
    const request = ++this._spectrumRequest;
    this.spectrumLoading = true;
    api.fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}/spectrum`, { signal: controller.signal })
      .then(result => {
        if (this.isConnected && request === this._spectrumRequest && id === this.activeId) this.spectrum = result;
      }).catch(error => {
        if (!controller.signal.aborted && id === this.activeId) this.spectrum = { available: false, reason: `Could not load spectrum: ${error.message}` };
      }).finally(() => {
        if (request === this._spectrumRequest && id === this.activeId) this.spectrumLoading = false;
      });
  }

  _retrySpectrum() {
    this.spectrum = null;
    this._loadSpectrum();
  }

  async _compare(id) {
    this.compareId = id;
    this.comparison = null;
    if (!id) return;
    const active = this.activeId;
    try {
      const data = await api.fetchJson(`/api/survey/band-scan/compare?before=${encodeURIComponent(id)}&after=${encodeURIComponent(active)}`);
      if (active === this.activeId && id === this.compareId) this.comparison = data;
    } catch (error) { this.error = error.message; }
  }

  _comparison(config) {
    const candidates = this.scans.filter(s => s.id !== this.activeId &&
      s.config?.start_hz === config.start_hz && s.config?.end_hz === config.end_hz &&
      !["running", "paused"].includes(s.state));
    if (!candidates.length) return "";
    const c = this.comparison;
    return html`<details><summary>Compare scanning sessions</summary>
      <label>Baseline session <select aria-label="Comparison baseline" .value=${this.compareId}
        @change=${e => this._compare(e.target.value)}>
        <option value="">Choose a session in this band</option>
        ${candidates.map(s => html`<option value=${s.id}>${date(s.started_at)} · ${s.state}</option>`)}
      </select></label>
      ${c ? html`<p>${c.new.length} new · ${c.disappeared.length} disappeared · ${c.changed.length} changed · ${c.unchanged_count} unchanged · ${c.unverified.length} outside shared coverage</p>
        <p class="dim">Matched within ${c.tolerance_hz} Hz. Changed means identification differs or peak SNR shifts at least ${c.snr_change_threshold_db} dB. A missed transmission does not prove a station went off air.</p>
        <table class="lib-table"><thead><tr><th>Change</th><th>MHz</th><th>SNR change</th></tr></thead>
        <tbody>${[...c.new.map(r => ({kind: "New", row: r})),
          ...c.disappeared.map(r => ({kind: "Disappeared", row: r})),
          ...c.changed.map(r => ({kind: "Changed", row: r.after, delta: r.snr_delta_db})),
          ...c.unverified.map(r => ({kind: "Unverified coverage", row: r}))].map(r => html`<tr>
          <td>${r.kind}</td><td><button @click=${() => this._open(r.row.frequency_hz)}>${freq(r.row.frequency_hz)}</button></td>
          <td>${r.delta == null ? "—" : `${dec(r.delta)} dB`}</td></tr>`)}</tbody></table>` : ""}
    </details>`;
  }

  _download(text, type, suffix) {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `msdr-band-scan-${this.activeId}.${suffix}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  _exportJson() {
    if (!this.activeId) return;
    const link = document.createElement("a");
    link.href = `/api/survey/band-scan/${encodeURIComponent(this.activeId)}/export`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  _abortReportReads() {
    this._summaryController?.abort();
    this._observationController?.abort();
    this._spectrumController?.abort();
    this._summaryInflight = null;
    this._observationRequest++;
    this._spectrumRequest++;
    this.loading = false;
    this.observationsLoading = false;
    this.spectrumLoading = false;
  }

  _clearDeletedReport(id = null) {
    // A status poll may have started before the DELETE and return stale history afterward.
    this._refreshRequest++;
    this._abortReportReads();
    this.activeId = null;
    this.selectedId = null;
    this.detail = null;
    this.report = null;
    this.observationPage = null;
    this.observationTotal = 0;
    this.spectrum = null;
    this.selectedFrequency = null;
    this._spectrumWindow = null;
    this.hoveredSpectrumFinding = null;
    if (id) {
      this._deletedIds.add(id);
      this.scans = this.scans.filter(item => item.id !== id);
    } else {
      for (const scan of this.scans) this._deletedIds.add(scan.id);
      this.scans = [];
    }
  }

  async _deleteSelected() {
    const id = this.activeId;
    const scan = this.scans.find(item => item.id === id);
    if (!id || ["running", "paused"].includes(scan?.state)) return;
    if (!window.confirm(`Delete band-scan report ${id}? This cannot be undone.`)) return;
    try {
      await api.fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}`, { method: "DELETE" });
      this.error = "";
      this._clearDeletedReport(id);
      if (this.scans.length) this._select(this.scans[0].id);
    } catch (error) { this.error = `Could not delete report: ${error.message}`; }
  }

  async _deleteAll() {
    if (this.scans.some(scan => ["running", "paused"].includes(scan.state))) return;
    if (!this.scans.length || !window.confirm("Delete all saved band-scan reports, including reports not shown in this recent list? This cannot be undone.")) return;
    try {
      await api.fetchJson("/api/survey/band-scan", { method: "DELETE" });
      this.error = "";
      this._clearDeletedReport();
      this.scans = [];
    } catch (error) { this.error = `Could not delete reports: ${error.message}`; }
  }

  _passesPeakThreshold(row) {
    if (!number(this.minPeakSNR)) return true;
    const peak = row.peak_snr_db ?? row.snr_max_db ?? (row.observations ?? [])
      .reduce((max, observation) => number(observation.snr_db) ? Math.max(max, observation.snr_db) : max, -Infinity);
    return number(peak) && peak >= this.minPeakSNR;
  }

  _retrySummary() {
    if (!this.activeId) return;
    this._summaryRetryBlocked = false;
    this.error = "";
    this._load(this.activeId);
  }

  _open(hz) {
    this.dispatchEvent(new CustomEvent("signal-open", {
      detail: { frequency_hz: hz }, bubbles: true, composed: true,
    }));
  }

  _inspectFinding(hz) {
    this.selectedFrequency = hz;
    this.page = 0;
    this._open(hz);
  }

  _showSpectrumFinding(row, sourceBin) {
    const observations = row.observations ?? [];
    const at = observations.reduce((latest, item) => number(item.observed_at)
      ? Math.max(latest, item.observed_at) : latest, -Infinity);
    this.hoveredSpectrumFinding = {
      frequency_hz: row.frequency_hz,
      name: candidateText(observations),
      observation_count: row.observation_count ?? observations.length,
      observed_at: number(at) ? at : null,
      peak_snr_db: row.peak_snr_db ?? row.snr_max_db,
      sourceBin,
    };
  }

  _hideSpectrumFinding(event) {
    if (event.type === "pointerleave" && event.currentTarget.contains(event.relatedTarget)) return;
    if (event.type === "blur" && event.currentTarget.matches(":hover")) return;
    this.hoveredSpectrumFinding = null;
  }

  _dismissSpectrumFinding(event) {
    if (event.key !== "Escape" || !this.hoveredSpectrumFinding) return;
    event.preventDefault();
    this.hoveredSpectrumFinding = null;
  }

  _findingDetailsCard() {
    const finding = this.hoveredSpectrumFinding;
    if (!finding) return "";
    return html`<aside id="band-report-finding-details" class="band-report-finding-details" role="tooltip">
      <strong>${finding.name}</strong>
      <span>${freq(finding.frequency_hz)} MHz · ${finding.observation_count} observations</span>
      <span>Last observed ${date(finding.observed_at)}</span>
      ${number(finding.peak_snr_db) ? html`<span>Peak SNR ${dec(finding.peak_snr_db)} dB</span>` : ""}
      ${finding.sourceBin ? html`<span>Source bin ${freq(finding.sourceBin.frequency_hz)} MHz · typical ${dec(finding.sourceBin.typical_db)} dB · max ${dec(finding.sourceBin.max_db)} dB</span>
        <span>Bin captured ${date(finding.sourceBin.first_at)}–${date(finding.sourceBin.last_at)}</span>` : html`<span>No measured spectrum bin retained</span>`}
    </aside>`;
  }

  _shiftSpectrum(direction) {
    const spectrum = this.spectrum;
    if (!spectrum?.available) return;
    const [min, max] = this._spectrumWindow ?? [spectrum.start_hz, spectrum.end_hz];
    const width = max - min, lo = Math.max(spectrum.start_hz, Math.min(spectrum.end_hz - width, min + width * .2 * direction));
    this._spectrumWindow = [lo, lo + width];
    this.hoveredSpectrumFinding = null;
    this.requestUpdate();
  }

  _zoomSpectrum(direction) {
    const spectrum = this.spectrum;
    if (!spectrum?.available) return;
    const [min, max] = this._spectrumWindow ?? [spectrum.start_hz, spectrum.end_hz];
    const width = Math.min(spectrum.end_hz - spectrum.start_hz, Math.max(spectrum.bin_width_hz * 8, (max - min) * (direction > 0 ? .7 : 1.4)));
    const lo = Math.max(spectrum.start_hz, Math.min(spectrum.end_hz - width, (min + max - width) / 2));
    this._spectrumWindow = [lo, lo + width];
    this.hoveredSpectrumFinding = null;
    this.requestUpdate();
  }

  _frequencyFindings() {
    return html`<h3>Detected findings</h3><div class="band-report-findings">${(this.detail?.payload?.results ?? []).filter(row => this._passesPeakThreshold(row)).map(row =>
      html`<button @click=${() => this._inspectFinding(row.frequency_hz)}>${freq(row.frequency_hz)} MHz · ${candidateText(row.observations ?? [])} · ${row.observation_count ?? row.observations?.length ?? 0} observations</button>`)}</div>`;
  }

  _findingsStrip() {
    const rows = (this.detail?.payload?.results ?? []).filter(row => this._passesPeakThreshold(row));
    const config = this.detail?.config ?? {};
    const lo = number(config.start_hz) ? config.start_hz : Math.min(...rows.map(row => row.frequency_hz));
    const hi = number(config.end_hz) ? config.end_hz : Math.max(...rows.map(row => row.frequency_hz));
    // No findings and no numeric config range: min()/max() of nothing give
    // +-Infinity, so there is nothing to position.
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return "";
    const span = Math.max(1, hi - lo);
    return html`<h3>Detected findings across the scanned range</h3>
      <div class=${`band-report-finding-strip${this.hoveredSpectrumFinding ? " has-details" : ""}`} role="group" aria-label="Finding frequencies along scan range; no measured spectrum data"
        @pointerleave=${event => this._hideSpectrumFinding(event)} @keydown=${event => this._dismissSpectrumFinding(event)}>
        <div class="band-report-finding-axis"></div>
        ${[0,1,2,3,4].map(i => html`<span class="band-report-finding-tick" style=${`left:${4.9+i*22.95}%`}>${freq(lo+span*i/4)} MHz</span>`)}
        ${rows.map(row => {
          const left = 4.9 + 91.8 * (row.frequency_hz - lo) / span;
          const obs = row.observations ?? [];
          const at = obs.reduce((latest, item) => number(item.observed_at) ? Math.max(latest, item.observed_at) : latest, -Infinity);
          const title = `${freq(row.frequency_hz)} MHz · ${candidateText(obs)} · ${number(at) ? date(at) : "time unknown"} · ${row.observation_count ?? obs.length} observations · finding only`;
          return html`<button class="band-report-finding-marker" style=${`left:${left}%`} title=${title} aria-label="Inspect ${title}"
            aria-describedby=${this.hoveredSpectrumFinding?.frequency_hz === row.frequency_hz ? "band-report-finding-details" : ""}
            @pointerenter=${() => this._showSpectrumFinding(row, null)}
            @focus=${() => this._showSpectrumFinding(row, null)}
            @blur=${event => this._hideSpectrumFinding(event)}
            @click=${() => this._inspectFinding(row.frequency_hz)}>●</button>`;
        })}
        ${this._findingDetailsCard()}
      </div>${this._frequencyFindings()}`;
  }

  _spectrumView(spectrum) {
    if (this.spectrumLoading) return html`<p class="dim">Loading measured spectrum…</p>`;
    if (!spectrum?.available || !spectrum.bins?.length) return html`<div class="band-report-spectrum-empty"><strong>No measured spectrum is available for this session.</strong>
      <p>${spectrum?.reason ?? "This legacy scan saved detected findings but did not retain calibrated spectrum bins."} The strip below locates measured findings only. It contains no inferred noise floor or spectrum curve.</p>
      ${String(spectrum?.reason ?? "").startsWith("Could not load spectrum:") ? html`<button @click=${() => this._retrySpectrum()}>Retry spectrum</button>` : ""}</div>${this._findingsStrip()}`;
    const [xmin, xmax] = this._spectrumWindow ?? [spectrum.start_hz, spectrum.end_hz];
    const bins = spectrum.bins.filter(bin => bin.frequency_hz >= xmin && bin.frequency_hz <= xmax);
    const [dataMin, dataMax] = extent(bins.flatMap(bin => [bin.typical_db, bin.max_db]));
    const ymin = Math.floor(Math.min(dataMin, -120) / 10) * 10, ymax = Math.ceil(Math.max(dataMax, -20) / 10) * 10;
    const x = hz => 48 + (hz - xmin) / Math.max(1, xmax - xmin) * 900;
    const y = db => 286 - (db - ymin) / Math.max(1, ymax - ymin) * 250;
    const holes = (spectrum.holes ?? []).filter(h => h.end_hz >= xmin && h.start_hz <= xmax);
    const line = key => {
      let path = "", previous = null;
      for (const bin of bins) {
        const value = bin[key];
        if (!number(value) || bin.sample_count === 0) { previous = null; continue; }
        const crossesHole = previous && holes.some(hole => hole.start_hz <= bin.frequency_hz && hole.end_hz >= previous.frequency_hz);
        const contiguous = previous && !crossesHole && number(spectrum.bin_width_hz) && bin.frequency_hz - previous.frequency_hz <= spectrum.bin_width_hz * 1.6;
        path += `${contiguous ? "L" : "M"}${x(bin.frequency_hz)},${y(value)} `;
        previous = bin;
      }
      return path;
    };
    const slices = [...(spectrum.slices ?? [])].sort((a, b) => a.at - b.at);
    const markers = (this.detail?.payload?.results ?? []).filter(row => row.frequency_hz >= xmin && row.frequency_hz <= xmax && this._passesPeakThreshold(row));
    return html`<div class="band-report-spectrum-controls"><button @click=${() => this._zoomSpectrum(1)}>Zoom in</button><button @click=${() => this._zoomSpectrum(-1)}>Zoom out</button>
      <button @click=${() => this._shiftSpectrum(-1)}>← Pan</button><button @click=${() => this._shiftSpectrum(1)}>Pan →</button>
      <button @click=${() => { this._spectrumWindow = null; this.hoveredSpectrumFinding = null; this.requestUpdate(); }}>Reset view</button><span>${freq(xmin)}–${freq(xmax)} MHz · ${spectrum.bin_width_hz} Hz bins</span></div>
      <p class="dim">Survey display spectrum level (relative dB): typical and maximum hold. Gaps and center exclusions are marked; spectrogram cells retain capture time.</p>
      <div class="band-report-spectrum-chart" @pointerleave=${event => this._hideSpectrumFinding(event)}
        @keydown=${event => this._dismissSpectrumFinding(event)}><svg viewBox="0 0 980 330" role="img" aria-label="Measured spectrum with typical and maximum hold">
        <text x="6" y="18">Relative dB</text>
        ${[0,1,2,3,4].map(i => { const yy=36+i*62.5; return svg`<line x1="48" x2="948" y1=${yy} y2=${yy} stroke="#444"/><text x="3" y=${yy+4}>${dec(ymax-i*(ymax-ymin)/4,0)}</text>`; })}
        <defs><pattern id="band-report-gap" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><line x1="0" y1="0" x2="0" y2="6" stroke="#9aa3ad" stroke-width="2"/></pattern></defs>
        ${holes.map(h => svg`<rect x=${x(Math.max(xmin,h.start_hz))} y="36" width=${Math.max(2,x(Math.min(xmax,h.end_hz))-x(Math.max(xmin,h.start_hz)))} height="250" fill="url(#band-report-gap)" opacity=".65"><title>${h.reason ?? "Gap"}</title></rect>`)}
        <path d=${line("typical_db")} fill="none" stroke="#65c9ff" stroke-width="1.7"/><path d=${line("max_db")} fill="none" stroke="#ffc46b" stroke-width="1.3"/>
      </svg>${markers.map(row => {
          const obs = row.observations ?? [];
          const at = obs.reduce((latest, item) => number(item.observed_at) ? Math.max(latest, item.observed_at) : latest, -Infinity);
          const clue = candidateText(obs);
          const sourceBin = spectrum.bins.reduce((best, bin) => !best || Math.abs(bin.frequency_hz - row.frequency_hz) < Math.abs(best.frequency_hz - row.frequency_hz) ? bin : best, null);
          const sourceText = sourceBin ? `source bin ${freq(sourceBin.frequency_hz)} MHz · typical ${dec(sourceBin.typical_db)} dB · max ${dec(sourceBin.max_db)} dB · captured ${date(sourceBin.first_at)}–${date(sourceBin.last_at)}` : "no sampled source bin";
          const title = `${freq(row.frequency_hz)} MHz · ${clue} · ${number(at) ? date(at) : "time unknown"} · ${row.observation_count ?? obs.length} observations · ${sourceText}`;
          const left = 4.9 + 91.8 * (row.frequency_hz - xmin) / Math.max(1, xmax - xmin);
          return html`<button class="band-report-spectrum-marker" style=${`left:${left}%`} title=${title} aria-label="Inspect ${title}"
            aria-describedby=${this.hoveredSpectrumFinding?.frequency_hz === row.frequency_hz ? "band-report-finding-details" : ""}
            @pointerenter=${() => this._showSpectrumFinding(row, sourceBin)}
            @focus=${() => this._showSpectrumFinding(row, sourceBin)}
            @blur=${event => this._hideSpectrumFinding(event)}
            @click=${() => this._inspectFinding(row.frequency_hz)}>●</button>`;
        })}
        ${this._findingDetailsCard()}</div>
      <div class="band-report-spectrum-axis">${[0,1,2,3,4].map(i => html`<span>${freq(xmin+(xmax-xmin)*i/4)} MHz</span>`)}</div>
      <div class="band-report-spectrum-legend"><span>Typical</span><span>Maximum hold</span><span>Gap or excluded capture</span></div>
      <h3>Time-frequency spectrogram · sampled captured frames</h3>${slices.length ? html`<div class="band-report-spectrogram"><canvas width="900" height="220" role="img" aria-label="Time-frequency spectrogram of sampled captured frames"></canvas>
        <div class="band-report-spec-labels"><span>Time ${date(slices[0].at)} → ${date(slices.at(-1).at)}</span>
          <span>${freq(xmin)}–${freq(xmax)} MHz</span></div></div>
        <p class="dim">${date(slices[0].at)} to ${date(slices.at(-1).at)} · ${spectrum.time_history_sampled ? `history sampled at about ${spectrum.slice_interval_s ?? "adaptive"} s intervals` : "captured frame history"}. Each thin row marks a captured frame; gaps include unsampled time, uncaptured frequencies and center exclusions.</p>` : html`<p class="dim">No time-resolved spectrum slices were retained.</p>`}${this._frequencyFindings()}`;
  }

  _pager(total) {
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (pages === 1) return "";
    return html`<div class="band-report-pager mono">
      <span>${total} rows · page ${this.page + 1} of ${pages}</span>
      <button ?disabled=${this.page <= 0} @click=${() => this.page--}>← Previous</button>
      <button ?disabled=${this.page >= pages - 1} @click=${() => this.page++}>Next →</button>
    </div>`;
  }

  _frequencies(rows) {
    const q = this.query.trim().toLowerCase();
    const thresholdFiltered = rows.filter(row => this._passesPeakThreshold(row));
    const filtered = thresholdFiltered.filter(row => {
      if (this.filter === "identified" && row.candidate_source === "measured") return false;
      if (this.filter === "voice" && row.activity !== "Speech-like?") return false;
      if (this.filter === "catalog" && !row.catalog_matches.length) return false;
      return !q || `${freq(row.frequency_hz)} ${row.candidate} ${row.activity} ${row.catalog_matches.join(" ")} ${row.band_context ?? ""}`.toLowerCase().includes(q);
    }).sort((a, b) => this.sort === "snr" ? (b.peak_snr_db ?? -Infinity) - (a.peak_snr_db ?? -Infinity)
      : this.sort === "recent" ? (b.last_seen ?? 0) - (a.last_seen ?? 0)
        : a.frequency_hz - b.frequency_hz);
    const shown = filtered.slice(this.page * PAGE_SIZE, (this.page + 1) * PAGE_SIZE);
    return html`<div class="band-report-list-controls">
      <label>Find<input aria-label="Find report frequency or name" type="search"
        placeholder="Frequency or name" .value=${this.query} @input=${e => { this.query = e.target.value; this.page = 0; }}></label>
      <label>Show<select aria-label="Filter report frequencies" .value=${this.filter}
        @change=${e => { this.filter = e.target.value; this.page = 0; }}>
        <option value="all">All measured</option><option value="identified">With signal clue</option>
        <option value="voice">Speech-like</option><option value="catalog">Catalog context</option>
      </select></label>
      <label>Sort<select aria-label="Sort report frequencies" .value=${this.sort}
        @change=${e => { this.sort = e.target.value; this.page = 0; }}>
        <option value="frequency">Frequency</option><option value="snr">Strongest SNR</option>
        <option value="recent">Last observed</option>
      </select></label>
      <span class="band-report-count">${filtered.length} frequencies shown · ${rows.length} total</span>
    </div>
    <div class="band-report-kind-legend" aria-label="Signal evidence colors">
      <span class="band-report-kind band-report-kind-decoded">Decoded</span>
      <span class="band-report-kind band-report-kind-voice">Speech-like?</span>
      <span class="band-report-kind band-report-kind-data">Data-like</span>
      <span class="band-report-kind band-report-kind-clue">Analysis clue</span>
      <small>Colors reflect measured evidence; catalog matches remain reference only.</small>
    </div>
    <div class="band-report-table-wrap"><table class="band-report-frequency-table">
      <thead><tr><th>Frequency</th><th>Signal clue & frequency context</th><th>Measurement</th><th>Observed</th><th></th></tr></thead>
      <tbody>${shown.length ? shown.map(row => html`<tr class=${`band-report-row-${row.signal_kind}`} >
        <td class="band-report-frequency"><strong>${freq(row.frequency_hz)}</strong><small>MHz</small></td>
        <td class="band-report-interpretation">
          ${row.signal_kind !== "measured" ? html`<span class=${`band-report-kind band-report-kind-${row.signal_kind}`}>${{
            decoded: "Decoded", voice: "Speech-like?", data: "Data-like", clue: "Analysis clue",
          }[row.signal_kind]}</span>` : ""}
          <strong class=${row.candidate_source === "measured" ? "unidentified" : ""}>${row.candidate_source === "measured" ? "Unidentified signal" : row.candidate}</strong>
          <small>${row.candidate_source === "measured" ? "Measured only" : `Signal clue · ${row.candidate_source}`}</small>
          ${row.catalog_matches.length ? html`<div class="band-report-catalog" title="Frequency reference, not identification">Catalog context · ${row.catalog_matches.slice(0, 2).join(" · ")}${row.catalog_matches.length > 2 ? ` +${row.catalog_matches.length - 2}` : ""}</div>` : ""}
          ${row.band_context ? html`<div class="band-report-allocation" title="Allocation reference only, not a signal identification">Band allocation · ${row.band_context} <small>reference only</small></div>` : ""}
        </td>
        <td class="band-report-measurement"><span class="band-report-activity">${row.activity}</span>
          <strong>${dec(row.peak_snr_db)} <small>dB SNR</small></strong>
          <small>${number(row.bandwidth_hz) ? `${Math.round(row.bandwidth_hz)} Hz bandwidth` : "Bandwidth unknown"}</small>
        </td>
        <td class="band-report-observed"><strong>${row.count}</strong> <small>observations · ${row.span_count} spans</small>
          <small>Last ${date(row.last_seen)}</small>
          <details><summary>Evidence & first seen</summary><small>First ${date(row.first_seen)}</small><small>${row.evidence.join(" · ")}</small></details>
        </td>
        <td><button @click=${() => this._open(row.frequency_hz)}>Inspect</button></td>
      </tr>`) : html`<tr><td colspan="5">No frequencies match this view.</td></tr>`}</tbody>
    </table></div>${this._pager(filtered.length)}`;
  }

  _timeline(rows) {
    const shown = this.observationPage;
    const total = this.observationTotal;
    if (this.observationsLoading) return html`<p class="dim">Loading observations…</p>`;
    if (!shown) return html`<p role="alert">Could not load observations. <button @click=${() => this._loadObservations()}>Retry</button></p>`;
    return html`<div class="band-report-table-wrap"><table class="lib-table band-report-table mono">
      <thead><tr><th>Observed</th><th>Samples</th><th>MHz</th><th>Span center</th><th>SNR</th><th>BW</th><th>Activity</th><th>Evidence</th></tr></thead>
      <tbody>${shown.length ? shown.map(row => html`<tr>
        <td>${date(row.observed_at)}<small>First ${date(row.first_seen ?? row.observed_at)} · last ${date(row.last_seen ?? row.observed_at)}</small></td>
        <td>${row.sample_count ?? 1}</td><td>${freq(row.frequency_hz)}</td><td>${freq(row.span_center_hz)}</td>
        <td>${number(row.snr_min_db) || number(row.snr_max_db) || number(row.snr_avg_db)
          ? html`${dec(row.snr_min_db ?? row.snr_db)}–${dec(row.snr_max_db ?? row.snr_db)} dB${number(row.snr_avg_db) ? html`<small>Mean ${dec(row.snr_avg_db)} dB</small>` : ""}`
          : html`${dec(row.snr_db)} dB`}</td>
        <td>${number(row.spectrum_bandwidth_hz ?? row.width_hz ?? row.bw_hz) ? `${Math.round(row.spectrum_bandwidth_hz ?? row.width_hz ?? row.bw_hz)} Hz` : "—"}</td>
        <td><span class=${`band-report-kind band-report-kind-${signalKind([row])}`}>${activityText(row.activity, row.voice)}</span></td><td>${row.confirmed?.name ?? row.rds?.ps ?? row.family?.name ?? "Measured"}</td>
      </tr>`) : html`<tr><td colspan="8">No observations recorded.</td></tr>`}</tbody>
    </table></div>${this.selectedFrequency != null ? html`<button @click=${() => { this.selectedFrequency = null; this.page = 0; }}>Show all frequencies</button>` : ""}${this._pager(total)}`;
  }

  _coverage(rows) {
    const shown = rows.slice(this.page * PAGE_SIZE, (this.page + 1) * PAGE_SIZE);
    return html`<div class="band-report-table-wrap"><table class="lib-table band-report-table mono">
      <thead><tr><th>Range MHz</th><th>Center MHz</th><th>Status</th><th>Visited</th><th>Usable capture</th><th>Usable intervals MHz</th><th>DSP passes</th><th>Fresh frames</th><th>Capture time</th><th>Reason</th></tr></thead>
      <tbody>${shown.length ? shown.map(row => html`<tr>
        <td>${freq(row.start_hz)}–${freq(row.end_hz)}</td><td>${freq(row.center_hz)}</td>
        <td><span class=${`band-report-state band-report-state-${row.state}`}>${row.state}</span></td>
        <td>${visitedSpan(row) ? "Yes" : "No"}</td>
        <td>${typeof row.spectrum_captured === "boolean" ? (row.spectrum_captured ? "Yes" : "No") : "Not recorded"}</td>
        <td>${Array.isArray(row.usable_intervals_hz) ? (row.usable_intervals_hz.length
          ? row.usable_intervals_hz.map(([low, high]) => `${freq(low)}–${freq(high)}`).join(" · ") : "No usable spectrum bins") : "Not recorded"}</td>
        <td>${number(row.dsp_result_count) ? (row.dsp_result_count ? `${row.dsp_result_count} passes completed` : "No completed DSP pass") : row.dsp_completed ? "At least 1 pass completed" : row.dsp_completed === false ? "No completed DSP pass" : "Not recorded"}${number(row.dsp_pending) && row.dsp_pending > 0 ? html`<small>${row.dsp_pending} pending</small>` : ""}</td>
        <td>${row.fresh_frames ?? "—"}</td><td>${row.capture_started_at != null || row.capture_ended_at != null ? `${date(row.capture_started_at)} – ${date(row.capture_ended_at)}` : "—"}</td>
        <td>${row.reason ?? row.state ?? "—"}</td>
      </tr>`) : html`<tr><td colspan="10">No spans scheduled.</td></tr>`}</tbody>
    </table></div>${this._pager(rows.length)}`;
  }

  render() {
    const detail = this.detail?.id === this.activeId ? this.detail : null;
    const report = detail ? this.report : null;
    const config = detail?.config ?? {};
    const payload = detail?.payload ?? {};
    const degraded = payload.capture_degraded || payload.failure_reason || payload.interruption_reason;
    const degradationReason = [payload.failure_reason, payload.interruption_reason,
      typeof payload.capture_degraded === "string" ? payload.capture_degraded : null].filter(Boolean).join(" · ");
    const visibleFrequencies = report?.frequencies.filter(row => this._passesPeakThreshold(row)) ?? [];
    const visibleObservations = visibleFrequencies.reduce((sum, row) => sum + row.count, 0);
    const activeScan = this.scans.find(scan => scan.id === this.activeId);
    const hasActiveScans = this.scans.some(scan => ["running", "paused"].includes(scan.state));
    const dspPassSpans = report?.coverage.filter(row => number(row.dsp_result_count) ? row.dsp_result_count > 0
      : row.dsp_completed === true).length ?? 0;
    const uncovered = report?.coverage.filter(row => row.state === "uncovered").length ?? 0;
    const visited = report?.coverage.filter(visitedSpan).length ?? 0;
    return html`<section class="panel band-scan-report" aria-label="Band scan reports">
      <div class="band-report-header"><div><span class="band-report-eyebrow">Survey archive</span><h2>Band scan report</h2>
        <p>Measured signals across the scanned range. Catalog entries are reference context, not identifications.</p></div>
        <div class="band-report-header-actions"><button @click=${() => this.refresh()}>Refresh</button>
          <button ?disabled=${!this.activeId || ["running", "paused"].includes(activeScan?.state)} @click=${() => this._deleteSelected()}>Delete report</button>
          <button ?disabled=${!this.scans.length || hasActiveScans} @click=${() => this._deleteAll()}>Delete all reports</button></div></div>
      ${this.error ? html`<p role="alert" class="band-report-error">${this.error}${this.error.startsWith("Could not load report:") ? html` <button @click=${() => this._retrySummary()}>Retry</button>` : ""}</p>` : ""}
      ${this.scans.length ? html`<label class="band-report-picker"><span>Session</span>
        <select .value=${this.activeId ?? ""} @change=${event => this._select(event.target.value)}>
          ${this.scans.map(scan => html`<option value=${scan.id} ?selected=${scan.id === this.activeId}>
            ${freq(scan.config?.start_hz)}–${freq(scan.config?.end_hz)} · ${scan.state} · ${date(scan.started_at)}
          </option>`)}
        </select></label>` : html`<p>No band scans saved yet. Start one in Discover → Live survey → Band scan.</p>`}
      ${this.loading && !detail ? html`<p class="dim">Loading report…</p>` : ""}
      ${detail && report ? html`
        <div class="band-report-session"><div><span class="band-report-eyebrow">Frequency range</span>
          <strong>${shortFreq(config.start_hz)}–${shortFreq(config.end_hz)} <small>MHz</small></strong>
          <span class="band-report-status">${detail.state}</span></div>
          <p>${number(config.span_hz) ? `${(config.span_hz / 1e6).toFixed(1)} MHz spans` : "Spans unknown"}
            <span>·</span> ${config.dwell_s ?? "—"} s dwell <span>·</span>
            ${number(config.overlap) ? Math.round(config.overlap * 100) : "—"}% overlap</p></div>
        ${degraded ? html`<div class="band-report-capture-warning" role="alert"><strong>Capture degraded; this report may be incomplete.</strong>
          ${degradationReason ? html`<span>${degradationReason}</span>` : "Some scan data or capture coverage was not saved."}</div>` : ""}
        ${this._comparison(config)}
        <div class="band-report-summary">
          <div><strong>${visibleFrequencies.length}</strong><small>frequencies shown</small></div>
          <div><strong>${visibleObservations}</strong><small>observations shown</small></div>
          <div><strong>${visited}</strong><small>visited · ${report.coverage.length} planned</small></div>
          <div><strong>${dspPassSpans}</strong><small>spans with a DSP pass · ${uncovered} uncovered</small></div>
        </div>
        <p class="band-report-note">Signals are recorded only during each dwell. A brief transmission between visits may be missed.</p>
        <div class="band-report-toolbar"><div role="tablist" aria-label="Report views">
          ${[["frequencies", "Frequencies"], ["timeline", "Timeline"], ["coverage", "Coverage"], ["spectrum", "Spectrum"]].map(([id, label]) => html`<button role="tab" aria-selected=${this.view === id}
            @click=${() => { this.view = id; this.page = 0; }}>${label}</button>`)}
        </div><div class="band-report-exports">
          <label>Minimum peak SNR (dB)<input aria-label="Minimum peak SNR (dB)" type="number" step="0.5" placeholder="All"
            .value=${this.minPeakSNR == null ? "" : String(this.minPeakSNR)}
            @input=${event => { const value = event.target.value; this.minPeakSNR = value === "" ? null : Number(value); this.page = 0; }}></label>
          <button @click=${() => this._download(bandScanFrequenciesCsv(report.frequencies.filter(row => this._passesPeakThreshold(row))), "text/csv;charset=utf-8", "csv")}>Export frequencies CSV</button>
          <button @click=${() => this._exportJson()}>Export full JSON</button>
        </div></div>
        ${this.view === "frequencies" ? this._frequencies(report.frequencies)
          : this.view === "timeline" ? this._timeline(report.timeline)
            : this.view === "coverage" ? this._coverage(report.coverage) : this._spectrumView(this.spectrum)}
      ` : ""}
    </section>`;
  }
}
customElements.define("band-scan-report", BandScanReport);
