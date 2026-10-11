import { LitElement, html, svg } from "../vendor/lit.js";

// docs/wifi.md "Wi-Fi activity": a heatmap of packets this adapter actually
// received, never a measurement of the RF spectrum. Pure helpers below
// (bucket choice, cell normalisation, gap/hatch detection, colour scale) are
// exported for unit tests; the class at the bottom is just their rendering.

export const ACTIVITY_SENTENCE =
  "Shows Wi-Fi packets this adapter received -- not measured RF spectrum, not continuous spectrum coverage, not channel utilisation, and never detection of non-Wi-Fi interference.";

export const ACTIVITY_COVERAGE_LEGEND = "hatched = not listening on this channel";

/** The (?) help beside the Activity controls: the caveat sentence plus how
 * to read the grid. */
export const ACTIVITY_HELP =
  `${ACTIVITY_SENTENCE} Columns are Wi-Fi channels (centre frequency under the number), rows are time slices, newest at the top. Packets count on the channel the adapter was tuned to, so a column only has colour while the adapter sat on that channel; hatching means it was elsewhere.`;

/** Range selector presets (docs/wifi.md: "15 min, 1 h, 6 h, 24 h"). */
export const ACTIVITY_RANGE_OPTIONS = [
  { id: "15m", label: "15 min", minutes: 15 },
  { id: "1h", label: "1 h", minutes: 60 },
  { id: "6h", label: "6 h", minutes: 360 },
  { id: "24h", label: "24 h", minutes: 1440 },
];

// Same bucket set as msdr.history.store.ACTIVITY_BUCKET_CHOICES -- this
// helper mirrors the server's own auto-bucket choice (same contract as
// device-history-chart.js's autoBucketS for the minute-granularity timeline)
// so a client that wants to predict a response's bucket_s can.
export const ACTIVITY_BUCKET_OPTIONS = [1, 5, 15, 60, 300];

export function activityAutoBucketS(rangeS, maxColumns = 360) {
  for (const b of ACTIVITY_BUCKET_OPTIONS) {
    if (rangeS / b <= maxColumns) return b;
  }
  return ACTIVITY_BUCKET_OPTIONS[ACTIVITY_BUCKET_OPTIONS.length - 1];
}

/** Centre frequency in MHz of a 20 MHz Wi-Fi channel (14 is the Japan-only
 * odd one out at 2484), or null when unknown. Lives here rather than in
 * wifi-view.js because that file imports this one. */
export function wifiChannelMHz(value) {
  const ch = Number(value);
  if (!Number.isInteger(ch) || ch < 1) return null;
  if (ch === 14) return 2484;
  return ch < 14 ? 2407 + 5 * ch : 5000 + 5 * ch;
}

/** "Channel 6 (2437 MHz)" for headers and tooltips; just the number when
 * the frequency is unknown. */
export function channelLabel(channel) {
  const mhz = wifiChannelMHz(channel);
  return mhz ? `Channel ${channel} (${mhz} MHz)` : `Channel ${channel}`;
}

/** 2.4 GHz channels (1-14) are listed before 5 GHz, each band's own channels
 * ascending; a one-column gap separates the two bands so they never read as
 * a single contiguous spectrum (docs/wifi.md: never imply continuous
 * spectrum coverage). Only channels actually present in the response appear
 * -- an unsurveyed channel has no column at all, not an empty one. */
export function channelColumns(channels) {
  const band24 = (channels ?? []).filter((c) => c <= 14).sort((a, b) => a - b);
  const band5 = (channels ?? []).filter((c) => c > 14).sort((a, b) => a - b);
  const cols = [];
  let i = 0;
  for (const c of band24) cols.push({ channel: c, band: "2.4 GHz", col: i++ });
  if (band24.length && band5.length) i += 1; // gap column
  for (const c of band5) cols.push({ channel: c, band: "5 GHz", col: i++ });
  return cols;
}

/** Total column span (including the band gap), >=1 so an empty response
 * still has a drawable width. */
export function columnSpan(columns) {
  return Math.max(1, ...columns.map((c) => c.col + 1), 1);
}

/** Ascending bucket start times covering [start, end) at bucketS -- at least
 * one so a degenerate range still has a drawable row. */
export function timeBuckets(start, end, bucketS) {
  const count = Math.max(1, Math.round((end - start) / bucketS));
  return Array.from({ length: count }, (_, i) => start + i * bucketS);
}

/** rows (`[t, channel, packets, listened_s, sig_mean]`) -> Map("t|channel" ->
 * row) for O(1) lookup while walking the grid. */
export function indexActivity(rows) {
  const m = new Map();
  for (const r of rows ?? []) m.set(`${r[0]}|${r[1]}`, r);
  return m;
}

/** The full render grid: one entry per (time bucket, channel column), newest
 * time first (row 0 = most recent). `cell` is null when this (bucket,
 * channel) was never listened to -- drawn hatched, distinct from a listened
 * cell with zero packets (drawn at zero intensity) -- and is never
 * synthesised from neighbouring cells (no interpolation across unobserved
 * channels/times). */
export function buildActivityGrid(data) {
  const columns = channelColumns(data?.channels ?? []);
  const bucketS = data?.bucket_s ?? 60;
  const times = timeBuckets(data?.start ?? 0, data?.end ?? 0, bucketS).reverse();
  const index = indexActivity(data?.rows ?? []);
  return times.map((t) => ({
    t,
    cells: columns.map((c) => ({ ...c, t, cell: index.get(`${t}|${c.channel}`) ?? null })),
  }));
}

/** Packets per listened second for one row -- 0 when nothing was listened
 * (should not occur: the API only returns listened_s > 0 rows), never
 * NaN/Infinity. */
export function packetRate(row) {
  const listened = row?.[3] ?? 0;
  return listened > 0 ? row[2] / listened : 0;
}

/** Mean raw signal code for one row, or null when this adapter never
 * reported one for these packets. */
export function signalMean(row) {
  return row?.[4] ?? null;
}

/** Whether the "uncalibrated signal code" mode has anything to show --
 * only offered when at least one returned cell actually carries a signal
 * mean (docs/wifi.md: only if the per-packet signal data supports it). */
export function hasSignalMode(rows) {
  return (rows ?? []).some((r) => r[4] !== null && r[4] !== undefined);
}

/** [min, max] of signalMean across rows, padded so a flat series still has
 * a usable (non-zero-span) domain. null when no row carries a signal. */
export function signalDomain(rows) {
  const vals = (rows ?? []).map(signalMean).filter((v) => v !== null && v !== undefined);
  if (!vals.length) return null;
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }
  return { min, max };
}

//: Listened-but-silent cells are still drawn (never hatched), at this floor
// opacity, so "zero packets" reads as "quiet", not "unknown" or "missing".
const ZERO_OPACITY = 0.07;

/** 0..1 colour intensity for a listened cell -- `mode` "packets" scales by
 * packetRate() against `scale.maxRate`; "signal" scales signalMean() across
 * `scale.domain` ({min, max}). Returns `ZERO_OPACITY` when the cell carries
 * no usable value in this mode (e.g. no packets, or no signal reported). */
export function cellOpacity(row, mode, scale) {
  if (mode === "signal") {
    const v = signalMean(row);
    if (v === null || v === undefined || !scale?.domain) return ZERO_OPACITY;
    const { min, max } = scale.domain;
    const frac = (v - min) / Math.max(max - min, 1e-9);
    return ZERO_OPACITY + (1 - ZERO_OPACITY) * Math.min(1, Math.max(0, frac));
  }
  const rate = packetRate(row);
  if (!(rate > 0) || !(scale?.maxRate > 0)) return ZERO_OPACITY;
  return ZERO_OPACITY + (1 - ZERO_OPACITY) * Math.min(1, rate / scale.maxRate);
}

/** Hover/tooltip text for one cell (docs/wifi.md: "channel, time range,
 * packets, observed duration and packets/s"). */
export function cellTooltip(cell, bucketS) {
  const { channel, t } = cell;
  const end = t + bucketS;
  const fmt = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: bucketS < 60 ? "2-digit" : undefined });
  if (!cell.cell) return `${channelLabel(channel)} · ${fmt(t)}–${fmt(end)} · not listening`;
  const [, , packets, listened, sig] = cell.cell;
  const rate = packetRate(cell.cell);
  const parts = [
    channelLabel(channel),
    `${fmt(t)}–${fmt(end)}`,
    `${packets} packet${packets === 1 ? "" : "s"}`,
    `${listened.toFixed(1)} s observed`,
    `${rate.toFixed(1)} packets/s`,
  ];
  if (sig !== null && sig !== undefined) parts.push(`signal code ${sig.toFixed(1)} (uncalibrated)`);
  return parts.join(" · ");
}

/** Accessible text summary of the whole visible range, for the chart's
 * aria-label / role=img fallback text. */
export function accessibleSummary(data) {
  if (!data || !data.channels?.length) return "Wi-Fi activity: no channels observed in this range.";
  const minutes = Math.round(((data.end ?? 0) - (data.start ?? 0)) / 60);
  const channels = data.channels.join(", ");
  const maxRate = data.max_rate ?? 0;
  return `Wi-Fi activity over the last ${minutes} minutes on channel${data.channels.length === 1 ? "" : "s"} ${channels}; busiest cell ${maxRate.toFixed(1)} packets per second.`;
}

// Drawn in real pixels (no viewBox scaling): a range can have up to 360
// rows, and scaling the whole drawing to fit them shrank the columns to a
// sliver. Columns keep a readable width; rows share a fixed plot height.
const COL_W = 60; // wide enough for a "2437 MHz" label under the channel number
const LABEL_W = 62;
const HEADER_H = 48;
const PLOT_H = 420;
const MAX_ROW_H = 14;
const TIME_LABEL_EVERY_PX = 40;

/** Row height in px so `rows` fit PLOT_H, capped so short ranges don't get
 * giant cells. Fractional heights are fine in SVG. */
export function rowHeight(rows) {
  return Math.min(MAX_ROW_H, PLOT_H / Math.max(1, rows));
}

/** Indices of rows that get a time label: about one per TIME_LABEL_EVERY_PX. */
export function timeLabelRows(rows, rowH) {
  const step = Math.max(1, Math.ceil(TIME_LABEL_EVERY_PX / rowH));
  const out = [];
  for (let i = 0; i < rows; i += step) out.push(i);
  return out;
}

const NICE_STEPS_S = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

/** Like timeLabelRows, but on round local times (xx:05, xx:10, ... whole
 * hours) so labels read as a clock, not as "19:32:15". Picks the smallest
 * nice step that still keeps labels >= TIME_LABEL_EVERY_PX apart; falls back
 * to the evenly spaced version when the range is too short to hold two. */
export function niceTimeLabelRows(times, bucketS, rowH) {
  const minS = Math.max(1, Math.ceil(TIME_LABEL_EVERY_PX / rowH)) * bucketS;
  const step = NICE_STEPS_S.find((s) => s >= minS) ?? NICE_STEPS_S[NICE_STEPS_S.length - 1];
  const out = [];
  times.forEach((t, i) => {
    const local = t - new Date(t * 1000).getTimezoneOffset() * 60;
    if (local % step === 0) out.push(i);
  });
  return out.length >= 2 ? out : timeLabelRows(times.length, rowH);
}

let patternSeq = 0;

/** wifi-activity-chart: columns = channels (grouped 2.4/5 GHz with a gap),
 * rows = time buckets (newest at top). `mode` is "packets" or "signal". */
export class WifiActivityChart extends LitElement {
  static properties = {
    data: { type: Object },
    mode: { type: String },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.data = null;
    this.mode = "packets";
    this._patternId = `wac-hatch-${++patternSeq}`;
    this._hoverCell = null;
  }

  render() {
    const data = this.data;
    if (!data || !data.channels?.length) {
      return html`<p class="dim wifi-activity-empty">No Wi-Fi activity recorded in this range yet.</p>`;
    }
    const columns = channelColumns(data.channels);
    const span = columnSpan(columns);
    const grid = buildActivityGrid(data);
    const scale = this.mode === "signal"
      ? { domain: signalDomain(data.rows) }
      : { maxRate: data.max_rate || 1e-9 };
    const rowH = rowHeight(grid.length);
    const cellGap = rowH >= 4 ? 1 : 0;
    const width = LABEL_W + span * COL_W;
    const height = HEADER_H + grid.length * rowH;
    const labelRows = new Set(niceTimeLabelRows(grid.map((r) => r.t), data.bucket_s, rowH));
    const bands = ["2.4 GHz", "5 GHz"].map((band) => {
      const cols = columns.filter((c) => c.band === band);
      return cols.length ? { band, x: LABEL_W + cols[0].col * COL_W, w: cols.length * COL_W } : null;
    }).filter(Boolean);
    const timeFmt = (t) => new Date(t * 1000).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: data.bucket_s < 60 ? "2-digit" : undefined, hour12: false,
    });
    const hover = this._hoverCell;

    return html`<div class="wifi-activity-chart">
      <svg viewBox="0 0 ${width} ${height}" width=${width} height=${height}
        role="img" aria-label=${accessibleSummary(data)}>
        <defs>
          <pattern id=${this._patternId} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" fill="var(--panel-well)"></rect>
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--line)" stroke-width="1" opacity="0.6"></line>
          </pattern>
        </defs>
        ${bands.map((b) => svg`<text x=${b.x + b.w / 2} y="11" class="wifi-activity-band"
          font-size="10" text-anchor="middle">${b.band}</text>`)}
        ${columns.map((c) => svg`<text x=${LABEL_W + c.col * COL_W + COL_W / 2} y="27"
          class="wifi-activity-chhdr" font-size="11" text-anchor="middle">ch ${c.channel}</text>
          ${wifiChannelMHz(c.channel) ? svg`<text x=${LABEL_W + c.col * COL_W + COL_W / 2} y="40"
            class="wifi-activity-mhz" font-size="9" text-anchor="middle">${wifiChannelMHz(c.channel)} MHz</text>` : ""}`)}
        ${grid.map((row, ri) => {
          const y = HEADER_H + ri * rowH;
          return svg`
          ${labelRows.has(ri) ? svg`<text x=${LABEL_W - 8} y=${y + 3.5}
            class="wifi-activity-tlabel" font-size="10" text-anchor="end">${timeFmt(row.t)}</text>
            <line x1=${LABEL_W - 4} y1=${y} x2=${width} y2=${y} class="wifi-activity-rule"></line>` : ""}
          ${row.cells.map((cell) => {
            const x = LABEL_W + cell.col * COL_W;
            const cls = hover === cell ? "wifi-activity-cell hover" : "wifi-activity-cell";
            const enter = () => { this._hoverCell = cell; this.requestUpdate(); };
            const leave = () => { this._hoverCell = null; this.requestUpdate(); };
            if (!cell.cell) {
              return svg`<rect x=${x} y=${y} width=${COL_W - 2} height=${rowH - cellGap}
                fill=${`url(#${this._patternId})`} class=${cls}
                @mouseenter=${enter} @mouseleave=${leave}><title>${cellTooltip(cell, data.bucket_s)}</title></rect>`;
            }
            const opacity = cellOpacity(cell.cell, this.mode, scale);
            return svg`<rect x=${x} y=${y} width=${COL_W - 2} height=${rowH - cellGap}
              fill="var(--accent)" opacity=${opacity.toFixed(2)} class=${cls}
              @mouseenter=${enter} @mouseleave=${leave}><title>${cellTooltip(cell, data.bucket_s)}</title></rect>`;
          })}`;
        })}
      </svg>
      <div class="wifi-activity-footer">
        ${hover ? html`<p class="wifi-activity-tooltip" role="status">${cellTooltip(hover, data.bucket_s)}</p>` : ""}
        <div class="wifi-activity-key">
          <span class="dim">${this.mode === "signal" ? "mean uncalibrated signal code (not dBm)" : "packets per listened second"}</span>
          <span class="mono">${this.mode === "signal" ? (scale.domain?.min ?? 0).toFixed(0) : "0"}</span>
          <span class="wifi-activity-ramp" aria-hidden="true"></span>
          <span class="mono">${this.mode === "signal" ? (scale.domain?.max ?? 0).toFixed(0) : `${(data.max_rate ?? 0).toFixed(1)}/s`}</span>
          <span class="wifi-activity-hatch-key" aria-hidden="true"></span>
          <span class="dim">${ACTIVITY_COVERAGE_LEGEND}</span>
        </div>
        <p class="dim wifi-activity-bucket">1 row = ${data.bucket_s >= 60 ? `${data.bucket_s / 60} min` : `${data.bucket_s} s`} · newest at top</p>
      </div>
    </div>`;
  }
}

customElements.define("wifi-activity-chart", WifiActivityChart);
