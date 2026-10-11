import { LitElement, html, svg } from "../vendor/lit.js";

// docs/history.md "API": the timeline/device endpoints choose a bucket from
// this fixed set so a range never grows past ~360 columns.
export const BUCKET_OPTIONS = [60, 300, 900, 3600];

/** Smallest bucket (seconds) from BUCKET_OPTIONS that keeps a `rangeS`-wide
 * window at or under `maxColumns` columns -- used by callers (ble-view.js,
 * timeline-view.js) picking the `bucket` query param for a chosen range,
 * same contract the server's own timeline auto-bucket follows. */
export function autoBucketS(rangeS, maxColumns = 360) {
  for (const b of BUCKET_OPTIONS) {
    if (rangeS / b <= maxColumns) return b;
  }
  return BUCKET_OPTIONS[BUCKET_OPTIONS.length - 1];
}

/** Number of columns a [start, end) window divides into at `bucketS`.
 * At least 1 so a degenerate (empty) range still has a drawable column. */
export function bucketCount(start, end, bucketS) {
  return Math.max(1, Math.round((end - start) / bucketS));
}

/** rows (cells or coverage, both `[t, ...]`) -> Map(t -> row), for O(1)
 * lookup of "is there data for this bucket" while walking columns. */
export function indexByTime(rows) {
  const m = new Map();
  for (const r of rows ?? []) m.set(r[0], r);
  return m;
}

/** Fractional x position (0..1) of bucket index `i` of `count` columns. */
export function xForIndex(i, count) {
  return i / count;
}

/** [min, max] domain for the signal band+line, padded so a flat series
 * (min === max) still draws a visible line instead of collapsing to zero
 * height. `signal_kind` doesn't change the domain, only the axis label. */
export function signalDomain(cells) {
  const vals = [];
  for (const c of cells ?? []) {
    if (c[2] != null) vals.push(c[2]);
    if (c[3] != null) vals.push(c[3]);
    if (c[4] != null) vals.push(c[4]);
  }
  if (!vals.length) return { min: -1, max: 1 };
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }
  return { min, max };
}

/** value -> y pixel within [padTop, height - padBottom], domain highest at top. */
export function yForValue(v, domain, height, padTop = 4, padBottom = 4) {
  const span = Math.max(domain.max - domain.min, 1e-9);
  const usable = Math.max(height - padTop - padBottom, 1);
  return padTop + (1 - (v - domain.min) / span) * usable;
}

/** Max reports across cells, for scaling the reports strip (>=1 so an
 * all-zero/empty series doesn't divide by zero). */
export function maxReports(cells) {
  let max = 1;
  for (const c of cells ?? []) if (c[1] != null && c[1] > max) max = c[1];
  return max;
}

/** Column indices [0, count) that have no cell at all -- a bucket the
 * receiver may have been listening through but heard nothing from this
 * device. Distinct from a *coverage* gap (see missingCoverage below):
 * this is "device silent", not "not listening". */
export function cellColumns(cells, start, bucketS, count) {
  const byT = indexByTime(cells);
  const cols = [];
  for (let i = 0; i < count; i++) {
    const t = start + i * bucketS;
    cols.push({ i, t, cell: byT.get(t) ?? null });
  }
  return cols;
}

/** Contiguous runs of populated columns (cellColumns() output), so the
 * min/max band and avg line break across a run of empty buckets instead of
 * drawing a straight line across a silence -- same reasoning as
 * ble-view.js/wifi-view.js's sparklineSegments, but keyed on bucket
 * adjacency rather than an elapsed-seconds threshold. */
export function cellRuns(columns) {
  const runs = [];
  let current = [];
  for (const col of columns) {
    if (col.cell) current.push(col);
    else if (current.length) { runs.push(current); current = []; }
  }
  if (current.length) runs.push(current);
  return runs;
}

/** Column indices [0, count) where `coverage` has no entry for that bucket
 * -- the receiver was not listening at all that minute, per docs/history.md
 * ("coverage ... only list buckets with data"). These are hatched in the
 * chart so "not listening" is never mistaken for "device absent". */
export function missingCoverage(coverage, start, bucketS, count) {
  const byT = indexByTime(coverage);
  const gaps = [];
  for (let i = 0; i < count; i++) {
    const t = start + i * bucketS;
    if (!byT.has(t)) gaps.push(i);
  }
  return gaps;
}

/** "HH:MM", or "Mon D HH:MM" once the range spans more than a day and the
 * date itself becomes useful context. */
export function formatTickLabel(t, spanS) {
  const d = new Date(t * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (spanS > 36 * 3600) {
    const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return `${day} ${hh}:${mm}`;
  }
  return `${hh}:${mm}`;
}

/** Evenly spaced tick marks (at most `maxTicks`) across [start, end), each
 * with its fractional x position (0..1) and a formatted label. */
export function timeTicks(start, end, bucketS, maxTicks = 6) {
  const count = bucketCount(start, end, bucketS);
  const span = Math.max(end - start, bucketS);
  const step = Math.max(1, Math.ceil(count / maxTicks));
  const ticks = [];
  for (let i = 0; i < count; i += step) {
    const t = start + i * bucketS;
    ticks.push({ t, frac: xForIndex(i, count), label: formatTickLabel(t, span) });
  }
  return ticks;
}

let hatchSeq = 0;

const W = 720, H = 170;
const COVERAGE_Y = 0, COVERAGE_H = 10;
const BAND_Y = 16, BAND_H = 92;
const STRIP_Y = 114, STRIP_H = 28;
const AXIS_Y = 156;

/** device-history-chart: one device's signal (min/max band + avg line) and
 * report volume over time, with the receiver's own listening gaps hatched
 * in behind both so a quiet stretch never looks like the device vanished
 * (docs/history.md "Shared timeline (UI)"). Pure scale/tick/gap helpers
 * above are exported for unit tests; this class is just their SVG wiring. */
export class DeviceHistoryChart extends LitElement {
  static properties = {
    cells: { type: Array },
    coverage: { type: Array },
    bucketS: { type: Number },
    start: { type: Number },
    end: { type: Number },
    signalKind: { type: String },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.cells = [];
    this.coverage = [];
    this.bucketS = 60;
    this.start = 0;
    this.end = 0;
    this.signalKind = "dbm";
    this._hatchId = `dhc-hatch-${++hatchSeq}`;
  }

  render() {
    const count = bucketCount(this.start, this.end, this.bucketS);
    const columns = cellColumns(this.cells, this.start, this.bucketS, count);
    const runs = cellRuns(columns);
    const domain = signalDomain(this.cells);
    const maxR = maxReports(this.cells);
    const gaps = missingCoverage(this.coverage, this.start, this.bucketS, count);
    const ticks = timeTicks(this.start, this.end, this.bucketS);
    const colW = W / count;
    const unit = this.signalKind === "dbm" ? "dBm" : "raw (uncalibrated)";

    if (!this.cells?.length) {
      return html`<p class="dim history-chart-empty">No reports in this window.</p>`;
    }

    // signalDomain skips null min/max/avg, so a null cell must not be plotted
    // (yForValue(null) would land it at 0): split the run there instead.
    const splitOnNull = (run, fields) => {
      const parts = [[]];
      for (const c of run) {
        if (fields.some((f) => c.cell[f] == null)) { if (parts.at(-1).length) parts.push([]); }
        else parts.at(-1).push(c);
      }
      return parts.filter((p) => p.length);
    };
    const bandPath = (run) => {
      const top = run.map((c) => `${(xForIndex(c.i, count) * W).toFixed(1)},${yForValue(c.cell[4], domain, BAND_H, 2, 2) + BAND_Y}`);
      const bottom = [...run].reverse()
        .map((c) => `${(xForIndex(c.i, count) * W).toFixed(1)},${yForValue(c.cell[3], domain, BAND_H, 2, 2) + BAND_Y}`);
      return `M${[...top, ...bottom].join(" L")} Z`;
    };
    const avgLine = (run) => run
      .map((c) => `${(xForIndex(c.i, count) * W).toFixed(1)},${yForValue(c.cell[2], domain, BAND_H, 2, 2) + BAND_Y}`)
      .join(" ");

    return html`<div class="history-chart">
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
        aria-label="Reports and signal over time, ${unit}">
        <defs>
          <pattern id=${this._hatchId} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="var(--panel-well)"></rect>
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--line)" stroke-width="2"></line>
          </pattern>
        </defs>

        ${gaps.map((i) => svg`<rect x=${(xForIndex(i, count) * W).toFixed(1)} y="0"
          width=${Math.max(colW, 1).toFixed(1)} height=${H - (AXIS_Y - STRIP_Y - STRIP_H)}
          fill=${`url(#${this._hatchId})`}></rect>`)}

        <line x1="0" y1=${COVERAGE_Y + COVERAGE_H} x2=${W} y2=${COVERAGE_Y + COVERAGE_H} stroke="var(--line)"></line>

        ${runs.map((run) => svg`${splitOnNull(run, [3, 4]).map((part) => svg`<path d=${bandPath(part)} fill="var(--context-ble)" opacity="0.18" stroke="none"></path>`)}
          ${splitOnNull(run, [2]).map((part) => svg`<polyline points=${avgLine(part)} fill="none" stroke="var(--context-ble)" stroke-width="1.5"></polyline>`)}`)}

        ${columns.filter((c) => c.cell).map((c) => svg`<rect
          x=${(xForIndex(c.i, count) * W).toFixed(1)} y=${(STRIP_Y + STRIP_H - (c.cell[1] / maxR) * STRIP_H).toFixed(1)}
          width=${Math.max(colW - 0.5, 1).toFixed(1)} height=${Math.max((c.cell[1] / maxR) * STRIP_H, 1).toFixed(1)}
          fill="var(--context-ble)" opacity=${(0.3 + 0.7 * (c.cell[1] / maxR)).toFixed(2)}></rect>`)}

        ${ticks.map((t) => svg`<line x1=${(t.frac * W).toFixed(1)} y1=${BAND_Y} x2=${(t.frac * W).toFixed(1)} y2=${STRIP_Y + STRIP_H}
            stroke="var(--line)" stroke-dasharray="1 3"></line>
          <text x=${(t.frac * W).toFixed(1)} y=${AXIS_Y} class="history-chart-tick" font-size="10">${t.label}</text>`)}
      </svg>
      <p class="dim history-chart-legend">min–max band and avg line (${unit}) · reports per bucket below · hatched = receiver not listening</p>
    </div>`;
  }
}

customElements.define("device-history-chart", DeviceHistoryChart);
