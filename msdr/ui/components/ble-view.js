import { LitElement, html, svg } from "../vendor/lit.js";
import * as api from "../api.js";
import "./device-label-editor.js";
import { autoBucketS } from "./device-history-chart.js";

// docs/history.md "Shared timeline (UI)": the Wi-Fi and BLE detail panes
// also carry the label editor and a 24h history chart for the selected
// device -- same window ble-view's own detail pane now fetches.
const LABEL_HISTORY_HOURS = 24;

const LIVE_KEEP = 300; // frames kept on screen
const RUNNING = ["starting", "receiving", "stale"];
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// docs/ble.md "UI contract": every summary states this honestly instead of
// implying coverage the Bluetooth 4.x raw-HCI helper doesn't have. The
// server computes the exact sentence per backend/PHY/channel
// (msdr.ble.supervisor._coverage_for) and sends it as `status.coverage`;
// this fallback is only for before the first status response arrives.
const COVERAGE_FALLBACK = "legacy advertising, 1M PHY, channels 37-39 controller-scheduled; no extended advertising, no scan responses";

/** The honest coverage sentence for whatever backend/PHY/channel this
 * receiver is actually configured for right now (docs/ble.md "UI
 * contract": "Sniffle covers what it is configured for... never 'all'"). */
export function coverageText(status) {
  return status?.coverage ?? COVERAGE_FALLBACK;
}

/** Short header form: the hci backend's fixed sentence shortens the same
 * way it always did; a configured sniffle backend states its own PHY and
 * channel instead of a generic short form (there is no safe generic one). */
export function coverageShortText(status) {
  if (!status || status.backend !== "sniffle") {
    return "Legacy advertising only · 1M PHY · channels 37–39";
  }
  const phy = { "1m": "1M PHY", "2m": "2M PHY", coded: "Coded PHY" }[status.phy] ?? "PHY";
  const ch = status.channel ?? "37";
  const chan = ch === "hop" ? "channels 37-39 (hop)" : `channel ${ch}${BLE_ADV_MHZ[ch] ? ` (${BLE_ADV_MHZ[ch]} MHz)` : ""}`;
  return `Sniffle · ${phy} · ${chan}`;
}

/** Advertising channels sit at the edges and middle of the 2.4 GHz ISM band,
 * clear of Wi-Fi 1/6/11 -- not at 2402 + 2*index like data channels. */
const BLE_ADV_MHZ = { 37: 2402, 38: 2426, 39: 2480 };
const BLE_BAND = "2402–2480 MHz";

/** "2402 MHz" for one advertising channel, all three for hop. */
export function bleChannelMHzText(channel) {
  const ch = String(channel ?? "37");
  if (ch === "hop") return "2402 / 2426 / 2480 MHz";
  return BLE_ADV_MHZ[ch] ? `${BLE_ADV_MHZ[ch]} MHz` : "";
}

/** Wraps the trailing "channel …" part of the coverage line in the accent
 * highlight, matching the Wi-Fi header. */
function highlightChannel(text) {
  const m = text.match(/^(.*?)(channels? .*)$/);
  return m ? html`${m[1]}<span class="chan-hl">${m[2]}</span>` : text;
}

/** Why Start is unavailable here, or null. Disabled-in-settings is NOT a
 * blocker: pressing Start enables BLE first (same as wifi-view.js's
 * wifiStartBlocker). */
export function bleStartBlocker(status) {
  if (!status) return "Loading receiver status…";
  if (status.state === "dependency_missing") return "pyusb not installed — see Setup → BLE receiver";
  if (status.state === "no_device") return "No Bluetooth USB dongle detected — see Setup → BLE receiver";
  if (status.state === "claimed") return "Dongle is in use by macOS — see Setup → BLE receiver";
  if (status.state === "no_le") return "Controller has no LE support";
  if (status.state === "firmware_mismatch") return "Firmware does not answer as Sniffle — see Setup → BLE receiver";
  if (status.state === "wrong_baud") return "No response at this baud rate — see Setup → BLE receiver";
  return null;
}

/** Column header -> server sort key, and the direction a first click uses
 * (docs/ble.md API table: every column has a backing sort key). */
export const BLE_COLUMNS = [
  { label: "Device", key: "device", first: "asc" },
  { label: "Identity", key: "identity", first: "asc" },
  { label: "Company", key: "company", first: "asc" },
  { label: "Services", key: "services", first: "desc" },
  { label: "Sensor", key: "sensor", first: "asc" },
  { label: "RSSI", key: "rssi", first: "desc" },
  { label: "Reports", key: "reports", first: "desc" },
  { label: "Last seen", key: "last_seen", first: "desc" },
];

// msdr/ble/sensors.py protocol id -> short display name for the table chip
// and the details readings section.
const SENSOR_PROTOCOL_LABELS = {
  bthome: "BTHome",
  ruuvi_rawv2: "Ruuvi",
  ibeacon: "iBeacon",
  eddystone: "Eddystone",
  atc1441: "ATC1441",
  pvvx: "pvvx",
};

/** Table chip text for a row's decoded sensor beacon (docs/ble.md
 * "Sensors"), or null when the advertiser has none. */
export function sensorChipText(sensor) {
  if (!sensor) return null;
  const label = SENSOR_PROTOCOL_LABELS[sensor.protocol] ?? sensor.protocol;
  return sensor.encrypted ? `${label} (encrypted)` : label;
}

/** One reading's value, in words: booleans as yes/no, otherwise the value
 * with its unit when it has one. */
export function sensorReadingText(reading) {
  if (!reading || reading.value === null || reading.value === undefined) return "--";
  if (typeof reading.value === "boolean") return reading.value ? "yes" : "no";
  return reading.unit ? `${reading.value} ${reading.unit}` : `${reading.value}`;
}

const DEFAULT_SORT = { sort: "first_seen", order: "asc" };

/** Next {sort, order} after clicking a header: a new column starts at its
 * natural direction; clicking the active column flips it. */
export function nextSort(current, key) {
  const col = BLE_COLUMNS.find((c) => c.key === key);
  if (!col) return current;
  if (current.sort !== key) return { sort: key, order: col.first };
  return { sort: key, order: current.order === "asc" ? "desc" : "asc" };
}

/** Merge newly fetched live items into the on-screen list (newest first),
 * dropping duplicates and anything at or below the last sequence seen --
 * identical shape to wifi-view.js's mergeLiveFrames (both just key off `seq`). */
export function mergeLiveFrames(current, incoming, lastSeq, keep = LIVE_KEEP) {
  const fresh = (incoming ?? []).filter((f) => f.seq > lastSeq);
  if (!fresh.length) return { frames: current, lastSeq };
  const frames = [...fresh.reverse(), ...current].slice(0, keep);
  return { frames, lastSeq: Math.max(lastSeq, ...fresh.map((f) => f.seq)) };
}

/** One line of the live feed, in words (docs/ble.md live item shape). `phy`/
 * `channel`/`extended` are sniffle-backend-only fields, absent (null/false)
 * from hci reports. */
export function liveFrameText(f) {
  const who = f.name ? `“${f.name}”` : f.addr ?? "";
  const rssi = f.rssi != null ? `${f.rssi} dBm` : "";
  const phyChan = f.phy ? `${f.phy.toUpperCase()}${f.channel != null ? ` ch${f.channel}` : ""}` : "";
  return [
    f.evt,
    who,
    f.company,
    rssi,
    `${f.length} B`,
    phyChan,
    f.extended ? "ext" : "",
    f.remote_id ? "Remote ID" : "",
  ].filter(Boolean).join(" · ");
}

const LIMIT = 100;
const GAP_S = 10;
const FILTER_DEBOUNCE_MS = 300;

const STATE_LABELS = {
  dependency_missing: "pyusb not installed",
  no_device: "no dongle detected",
  claimed: "dongle in use by macOS",
  no_le: "no LE support",
  firmware_mismatch: "firmware does not answer as Sniffle",
  wrong_baud: "no response at this baud rate",
  stopped: "stopped",
  starting: "starting…",
  receiving: "receiving",
  stale: "receiving (no reports recently)",
  failed: "failed",
};

const IDENTITY_LABELS = {
  public: "public",
  static: "static random",
  rpa: "resolvable private (RPA)",
  nrpa: "non-resolvable private (NRPA)",
};

export function identityLabel(kind) {
  return IDENTITY_LABELS[kind] ?? kind ?? "unknown";
}

// Compact chip text for the Identity column; the long explanation above goes
// in the chip's `title` instead of widening the column.
const IDENTITY_CHIPS = { public: "Public", static: "Static", rpa: "RPA", nrpa: "NRPA" };
const ROTATING_IDENTITIES = new Set(["rpa", "nrpa"]);

export function identityChipText(kind) {
  return IDENTITY_CHIPS[kind] ?? kind ?? "Unknown";
}

// A subset of msdr/ble/adv.py's SERVICE_NAMES, kept in sync by hand -- same
// reasoning as epochHHMM below: this view has no dependency on anything
// another agent may be editing. Row summaries carry raw service_uuids only
// (service_names is a details-only field), so an unrecognised uuid falls
// back to its short form.
const SERVICE_NAMES = {
  "0x1800": "Generic Access",
  "0x180a": "Device Information",
  "0x180d": "Heart Rate",
  "0x180f": "Battery",
  "0x1812": "HID",
  "0x181a": "Environmental Sensing",
  "0xfe2c": "Google Fast Pair",
  "0xfe9f": "Google",
  "0xfeaa": "Eddystone",
  "0xfd6f": "Exposure Notification",
  "0xfffa": "ASTM Remote ID",
};

function shortUuid(u) {
  return u.length > 10 ? `${u.slice(0, 8)}…` : u;
}

/** Services cell: "<n> services · <first name or short uuid>", or null when
 * the advertiser has none (rendered as "—"). Full list goes in the cell's
 * `title`. */
export function servicesCellText(serviceUuids, names = {}) {
  const uuids = serviceUuids ?? [];
  if (!uuids.length) return null;
  // Server-side names include the downloaded SIG list (msdr/ble/sig.py).
  const first = names?.[uuids[0]] ?? SERVICE_NAMES[uuids[0]] ?? shortUuid(uuids[0]);
  return `${plural(uuids.length, "service")} · ${first}`;
}

/** Device column, primary line: a user label beats the advertised name,
 * which beats the company (same precedence as observations.py's device sort
 * key) -- `labeled` says whether the text came from a label, so the caller
 * can render it bold. */
export function deviceDisplay(row, label) {
  if (label?.name) return { text: label.name, labeled: true };
  if (row.name) return { text: row.name, labeled: false };
  const audio = row.apple?.audio;
  if (audio) return { text: audio.model ?? `Apple audio ${audio.model_code}`, labeled: false };
  const types = appleTypeNames(row.apple);
  if (row.company && types.length) return { text: `${row.company} device · ${types.join(", ")}`, labeled: false };
  if (row.company) return { text: `${row.company} device`, labeled: false };
  return { text: "Unknown device", labeled: false };
}

/** Device summary strip: "About N devices heard in the last minute" plus
 * groups ("Apple 18 (iPhone / iPad / Mac nearby 14, AirPods / Beats 3)").
 * Null when nothing was heard. */
// Regroup parts into side-by-side blocks: a Total block, then one block per
// top-level group (header = name + count, rows = its subgroups).
export function summaryBlocks(parts) {
  if (!parts) return [];
  const blocks = [{ title: "Total", count: parts.total, caption: parts.window, rows: [] }];
  for (const r of parts.rows) {
    if (r.level === 0) blocks.push({ title: r.label, count: r.count, share: r.share, rows: [] });
    else blocks[blocks.length - 1].rows.push(r);
  }
  return blocks;
}

export function deviceSummaryParts(summary) {
  if (!summary?.total) return null;
  const minutes = Math.round((summary.window_s ?? 60) / 60);
  const window = minutes <= 1 ? "the last minute" : `the last ${minutes} minutes`;
  return {
    lead: `About ${plural(summary.total, "device")} heard in ${window}`,
    total: summary.total,
    window,
    // Flat table rows: a group row, then its subgroups indented (level 1).
    // share is count/total (0-1) for the bar.
    rows: (summary.groups ?? []).flatMap((g) => [
      { label: g.label, count: g.count, level: 0, share: g.count / summary.total },
      ...(g.subgroups ?? []).map((sg) => ({ label: sg.label, count: sg.count, level: 1, share: sg.count / summary.total })),
    ]),
    groups: (summary.groups ?? []).map((g) => ({
      label: g.label,
      count: g.count,
      subgroups: g.subgroups ?? [],
      detail: (g.subgroups ?? []).map((sg) => `${sg.label} ${sg.count}`).join(", "),
    })),
  };
}

/** Apple Continuity message type names (msdr/ble/apple.py), hex code for a
 * type without a known name. */
export function appleTypeNames(apple) {
  return (apple?.types ?? []).map((t) => t.name ?? `type ${t.type}`);
}

/** AirPods/Beats battery line: "L 40% · R 40% · case --", with a ⚡ on
 * whatever is charging. Values are in 10% steps as broadcast. */
export function appleBatteryText(audio) {
  if (!audio) return null;
  const part = (label, value, charging) =>
    `${label} ${value == null ? "--" : `${value}%`}${charging ? " ⚡" : ""}`;
  return [
    part("L", audio.battery_left, audio.charging_left),
    part("R", audio.battery_right, audio.charging_right),
    part("case", audio.battery_case, audio.charging_case),
  ].join(" · ");
}

/** RSSI bar fill, 0-100, scaled -100..-30 dBm (clamped); null when there is
 * no reading. */
export function rssiBarPct(rssi, min = -100, max = -30) {
  if (rssi == null) return null;
  return Math.max(0, Math.min(100, ((rssi - min) / (max - min)) * 100));
}

/** rows with a label whose class is `ignore` dropped when hideIgnored is on
 * (docs/history.md: "their tables show the label and a 'hide ignored'
 * filter"). Client-side only -- labels live in a separate fetch from the
 * paginated advertiser list, so this never changes `total`/paging math. */
export function visibleRows(rows, labels, hideIgnored) {
  if (!hideIgnored) return rows;
  return (rows ?? []).filter((r) => labels.get(r.addr)?.class !== "ignore");
}

/** epoch seconds -> local "HH:MM", "--:--" when missing/invalid. Deliberately
 * local (not format.js's epochHM) -- kept here so this view has no
 * dependency on a file another agent may be editing in parallel, same
 * reasoning as wifi-view.js's own copy. */
export function epochHHMM(ts) {
  if (ts === null || ts === undefined) return "--:--";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "--:--";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** epoch seconds -> "37m ago" / "5h ago" / "3d ago" / "--" when missing. */
export function relativeAge(ts, atS = Date.now() / 1000) {
  if (ts === null || ts === undefined) return "--";
  const s = Math.max(0, Math.round(atS - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** [[ts, value], ...] -> SVG `points` strings, one per contiguous run --
 * copied from wifi-view.js's sparklineSegments (gapS breaks the line rather
 * than drawing a straight segment across a silent stretch). */
export function sparklineSegments(history, width = 120, height = 24, gapS = GAP_S) {
  const pts = (history ?? []).filter(([ts, v]) => ts !== null && ts !== undefined && v !== null && v !== undefined);
  if (pts.length < 2) return [];
  const ts0 = pts[0][0];
  const span = Math.max(pts[pts.length - 1][0] - ts0, 1);
  const values = pts.map(([, v]) => v);
  const vMin = Math.min(...values);
  const vSpan = Math.max(Math.max(...values) - vMin, 1e-9);
  const segments = [];
  let current = [];
  for (let i = 0; i < pts.length; i++) {
    const [ts, v] = pts[i];
    if (i > 0 && ts - pts[i - 1][0] > gapS) {
      if (current.length > 1) segments.push(current);
      current = [];
    }
    const x = ((ts - ts0) / span) * width;
    const y = height - ((v - vMin) / vSpan) * height;
    current.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  if (current.length > 1) segments.push(current);
  return segments.map((seg) => seg.join(" "));
}

/** What the table area shows instead of rows, keyed off the receiver status
 * -- null when rows should render normally. Every branch states BLE's
 * honest coverage (docs/ble.md UI contract), not just the networks-table
 * case. `setup` is true when the message should offer a link to Setup. */
export function emptyStateFor(status, rowCount) {
  if (!status) return { text: `Loading receiver status… ${COVERAGE_FALLBACK}.`, setup: false };
  const coverage = coverageText(status);
  if (status.state === "dependency_missing") {
    const dep = status.backend === "sniffle" ? "pyserial not installed" : STATE_LABELS.dependency_missing;
    return { text: `BLE receiver unavailable: ${dep}. ${coverage}.`, setup: true };
  }
  if (status.state === "no_device") {
    const what = status.backend === "sniffle" ? "No SONOFF serial port detected." : "No Bluetooth USB dongle detected.";
    return { text: `${what} ${coverage}.`, setup: true };
  }
  if (status.state === "claimed") {
    return { text: `Dongle is in use by macOS. ${coverage}.`, setup: true };
  }
  if (status.state === "no_le") {
    return { text: `Controller has no LE support. ${coverage}.`, setup: true };
  }
  if (status.state === "firmware_mismatch") {
    return {
      text: "Firmware does not answer as Sniffle — flash the matching Sniffle build for this "
        + "dongle's bridge chip (CP2102 vs CP2102N); msdr never flashes firmware.",
      setup: true,
    };
  }
  if (status.state === "wrong_baud") {
    return { text: "No response at the configured baud rate — check CP2102 vs CP2102N and try the other rate.", setup: true };
  }
  // Stop keeps the advertisers heard so far on screen; the next Start
  // replaces the table (BleSupervisor.start builds a new AdvertiserTable).
  if (status.state === "stopped" && rowCount > 0) return null;
  if (status.state === "stopped") {
    return { text: `BLE receiver is stopped. Press Start above. ${coverage}.`, setup: false };
  }
  if (status.state === "failed") {
    const base = status.last_error ? `Capture failed: ${status.last_error}.` : "Capture failed.";
    return { text: `${base} ${coverage}.`, setup: true };
  }
  if (RUNNING.includes(status.state) && rowCount === 0) {
    return { text: `Receiving — no advertisers observed yet. ${coverage}.`, setup: false };
  }
  return null;
}

/** Discover -> BLE: advertisers heard by the passive raw-HCI receiver
 * (docs/ble.md). Never an inventory, and never implies more coverage than a
 * Bluetooth 4.x controller's legacy scanning actually has. */
export class BleView extends LitElement {
  static properties = {
    settings: { type: Object },
    _status: { state: true },
    _rows: { state: true },
    _summary: { state: true }, // GET /api/ble/summary -- counts by device type
    _total: { state: true },
    _since: { state: true },
    _q: { state: true },
    _offset: { state: true },
    _selectedAddr: { state: true },
    _detail: { state: true },
    _error: { state: true },
    _loading: { state: true },
    _view: { state: true }, // "advertisers" | "live"
    _liveFrames: { state: true },
    _livePaused: { state: true },
    _sort: { state: true },
    _controlBusy: { state: true },
    _controlError: { state: true },
    _labels: { state: true },
    _hideIgnored: { state: true },
    _detailHistory: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.settings = null;
    this._status = null;
    this._rows = [];
    this._summary = null;
    this._total = 0;
    this._since = null;
    this._q = "";
    this._offset = 0;
    this._selectedAddr = null;
    this._detail = null;
    this._error = null;
    this._loading = true;
    this._reqSeq = 0;
    this._detailSeq = 0;
    this._filterTimer = null;
    this._pollTimer = null;
    this._view = "advertisers";
    this._sort = { ...DEFAULT_SORT };
    this._controlBusy = "";
    this._controlError = null;
    this._liveFrames = [];
    this._liveSeq = 0;
    this._livePaused = false;
    this._liveToken = 0;
    this._labels = new Map(); // addr -> label, from one listLabels("ble") fetch
    this._hideIgnored = true; // docs/history.md: ignore hidden by default
    this._detailHistory = null;
  }

  async _refreshLive() {
    if (this._view !== "live" || this._livePaused) return;
    const token = ++this._liveToken;
    try {
      const resp = await api.getBleLive({ after: this._liveSeq });
      if (token !== this._liveToken) return; // a newer request owns the list
      // The server restarted its ring (new capture): start over from its sequence.
      if (resp.seq < this._liveSeq) { this._liveSeq = 0; this._liveFrames = []; }
      const merged = mergeLiveFrames(this._liveFrames, resp.items, this._liveSeq);
      this._liveFrames = merged.frames;
      this._liveSeq = merged.lastSeq;
    } catch (err) {
      if (token === this._liveToken) this._error = err.message;
    }
  }

  _setView(view) {
    this._view = view;
    if (view === "live") this._refreshLive();
  }

  _renderLive() {
    const frames = this._liveFrames;
    return html`<div class="ble-live">
      <div class="control-row ble-live-controls">
        <button aria-pressed=${this._livePaused ? "true" : "false"}
          @click=${() => { this._livePaused = !this._livePaused; if (!this._livePaused) this._refreshLive(); }}>
          ${this._livePaused ? "Resume" : "Pause"}</button>
        <button @click=${() => { this._liveFrames = []; }}>Clear frames</button>
        <span class="dim">${this._livePaused ? "paused" : "updating every second"} · newest first · last ${LIVE_KEEP} kept · not saved</span>
      </div>
      ${frames.length
        ? html`<ol class="ble-live-list mono" aria-live="off">${frames.map((f) => html`<li>
            <span class="dim">${new Date(f.ts * 1000).toLocaleTimeString()}</span>
            <span class="ble-live-kind">${f.evt}</span>
            <span>${liveFrameText(f)}</span></li>`)}</ol>`
        : html`<p class="empty">${this._status?.state === "receiving" ? "Waiting for advertisers…" : "Start the BLE receiver in Setup to see reports as they arrive."}</p>`}
    </div>`;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refreshRows();
    this._refreshStatus();
    this._refreshLabels();
    this._pollTimer = setInterval(() => {
      this._refreshLive();
      this._refreshRows();
      this._refreshStatus();
    }, 1000);
  }

  disconnectedCallback() {
    clearInterval(this._pollTimer);
    clearTimeout(this._filterTimer);
    super.disconnectedCallback();
  }

  async _refreshStatus() {
    try {
      this._status = await api.getBleStatus();
    } catch (err) {
      this._error = err.message;
    }
  }

  /** One listLabels("ble") fetch shared by the table's Label column and the
   * hide-ignored filter; re-run whenever device-label-editor reports a
   * change instead of refetching per row. */
  async _refreshLabels() {
    try {
      const { items } = await api.listLabels("ble");
      this._labels = new Map((items ?? []).map((l) => [l.ident, l]));
    } catch (err) {
      this._error = err.message;
    }
  }

  _onLabelChanged() {
    this._refreshLabels();
  }

  /** Every list request carries its own sequence token -- a slow response
   * for a filter/page the operator already changed away from must never
   * overwrite what a newer request already fetched (same pattern as
   * wifi-view.js's _refreshRows). */
  async _refreshRows() {
    const seq = ++this._reqSeq;
    try {
      const params = {
        q: this._q || undefined,
        offset: this._offset,
        limit: LIMIT,
        sort: (this._sort ?? DEFAULT_SORT).sort,
        order: (this._sort ?? DEFAULT_SORT).order,
      };
      const [data, summary] = await Promise.all([
        api.listBleAdvertisers(params),
        // Never lets a summary failure (or a missing endpoint) break the table.
        Promise.resolve().then(() => api.bleSummary()).catch(() => null),
      ]);
      if (seq !== this._reqSeq) return; // a newer request already landed
      this._summary = summary;
      this._rows = data.items ?? [];
      this._total = data.total ?? 0;
      this._since = data.since ?? null;
      this._error = null;
      this._loading = false;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._error = err.message;
      this._loading = false;
    }
  }

  async _fetchDetail(addr) {
    const seq = ++this._detailSeq;
    try {
      const detail = await api.getBleAdvertiser(addr);
      if (seq !== this._detailSeq) return;
      this._detail = detail;
    } catch (err) {
      if (seq !== this._detailSeq) return;
      // Never leave the pane on "loading…": say what failed.
      this._detail = { addr, error: err.message };
    }
  }

  async _fetchDetailHistory(addr) {
    const seq = this._detailSeq;
    try {
      const data = await api.getDeviceHistory("ble", addr, {
        hours: LABEL_HISTORY_HOURS, bucket: autoBucketS(LABEL_HISTORY_HOURS * 3600),
      });
      if (seq !== this._detailSeq) return;
      this._detailHistory = data;
    } catch {
      if (seq !== this._detailSeq) return;
      this._detailHistory = null; // history is a nice-to-have in this pane; never block the advertiser detail on it
    }
  }

  _select(addr) {
    this._selectedAddr = this._selectedAddr === addr ? null : addr;
    this._detailHistory = null;
    if (this._selectedAddr) {
      this._fetchDetail(this._selectedAddr);
      this._fetchDetailHistory(this._selectedAddr);
    } else {
      this._detail = null;
    }
  }

  _setQuery(value) {
    this._q = value;
    this._offset = 0;
    clearTimeout(this._filterTimer);
    this._filterTimer = setTimeout(() => this._refreshRows(), FILTER_DEBOUNCE_MS);
  }

  _page(delta) {
    this._offset = Math.max(0, this._offset + delta * LIMIT);
    this._refreshRows();
  }

  _openSetup() {
    this.dispatchEvent(new CustomEvent("open-ble-setup", { bubbles: true, composed: true }));
  }

  async _control(label, action) {
    this._controlBusy = label;
    this._controlError = null;
    try {
      await action();
      await this._refreshStatus();
    } catch (err) {
      this._controlError = `${label} failed: ${err.message}`;
    } finally {
      this._controlBusy = "";
    }
  }

  _start() {
    return this._control("Start", async () => {
      // Pressing Start is the intent to receive: enable BLE if it isn't yet
      // (same pattern as wifi-view.js's _start).
      // Partial patch on purpose: `this.settings` can lag the server (the
      // app only refreshes it from the Setup drawer's settings-saved), and
      // spreading a stale copy would silently undo a newer PHY/channel.
      if (!this.settings?.enabled) {
        await api.patchSettings({ ble: { enabled: true } });
        this.dispatchEvent(new CustomEvent("settings-saved", { bubbles: true, composed: true }));
      }
      await api.startBle();
    });
  }

  _stop() { return this._control("Stop", () => api.stopBle()); }

  /** Save a Sniffle PHY/channel choice. The server defers the helper rebuild
   * while capture runs and applies it on the next start, so a running
   * capture is stopped and started again here to pick the change up. Only
   * the changed key is sent (see _start): a stale `this.settings` spread
   * here is how choosing "hop" used to flip the PHY back to 1M. */
  _setRadio(key, value) {
    return this._control("Radio", async () => {
      await api.patchSettings({ ble: { [key]: value } });
      this.dispatchEvent(new CustomEvent("settings-saved", { bubbles: true, composed: true }));
      if (RUNNING.includes(this._status?.state)) {
        await api.stopBle();
        await api.startBle();
      }
    });
  }

  _clearAdvertisers() {
    return this._control("Clear", async () => {
      await api.clearBleAdvertisers();
      this._selectedAddr = null; this._detail = null; this._offset = 0;
      await this._refreshRows();
    });
  }

  /** One compact controls row: Start/Stop/Clear, state words, advertiser
   * count, search, Hide ignored, paging -- everything that used to be split
   * across a receiver-controls row and a separate filters row. */
  _renderSummary() {
    const parts = deviceSummaryParts(this._summary);
    if (!parts) return "";
    return html`<section class="summary-blocks ble-summary" aria-label="Device summary"
      data-help="Approximate counts by kind of device, from advertisers heard in the last minute. Phones change address every few minutes, so one device can briefly count twice. Nothing about what a device or its owner is doing is decoded.">
      ${summaryBlocks(parts).map((b) => html`<table class="summary-block">
        <thead><tr><th>${b.title}</th><th class="num"><span class="mono">${b.count}</span>${b.caption ? html` <span class="dim">${b.caption.replace("the ", "")}</span>` : ""}</th></tr></thead>
        ${b.rows.length ? html`<tbody>${b.rows.map((r) => html`<tr>
          <td class="label">${r.label}</td>
          <td class="num"><span class="mono">${r.count}</span><span class="share-bar"><span style="width:${Math.round(r.share * 100)}%"></span></span></td>
        </tr>`)}</tbody>` : ""}
      </table>`)}
    </section>`;
  }

  _renderControls() {
    const s = this._status;
    const running = RUNNING.includes(s?.state);
    const blocker = running ? null : bleStartBlocker(s);
    const busy = !!this._controlBusy;
    return html`<div class="ctl-groups">
      <span class="ctl-label">Receiver</span>
      <div class="ble-controls control-row">
      <span class=${`state-pill${running ? " on" : blocker ? " warn" : ""}`} title=${coverageText(s)}>${s ? `${STATE_LABELS[s.state] ?? s.state}${running ? ` · ${plural(s.advertiser_count ?? this._total, "advertiser")}` : ""}` : "Loading…"}</span>
      ${running
        ? html`<button class="ble-stop" ?disabled=${busy} @click=${() => this._stop()}>${this._controlBusy === "Stop" ? "Stopping…" : "Stop"}</button>`
        : html`<button class="ble-start" ?disabled=${busy || !!blocker} title=${blocker ?? "Start passive scanning"}
            @click=${() => this._start()}>${this._controlBusy === "Start" ? "Starting…" : "Start"}</button>`}
      <button ?disabled=${busy} data-help="Empty the advertiser list; capture keeps running" @click=${() => this._clearAdvertisers()}>Clear advertisers</button>
      <span class="ctl-sep" aria-hidden="true"></span>
      ${s?.backend === "sniffle" ? html`
        <label data-help="Which Bluetooth LE radio mode Sniffle listens on. 1M = standard BLE advertising (most devices). 2M = faster BLE 5, rare for advertising. Coded = BLE 5 Long Range (some Remote ID modules, trackers). Changing it restarts a running capture.">PHY <select aria-label="BLE PHY" ?disabled=${busy}
          @change=${(e) => this._setRadio("phy", e.target.value)}>
          ${[["1m", "1M"], ["2m", "2M"], ["coded", "Coded"]].map(([v, l]) => html`<option value=${v} ?selected=${s.phy === v}>${l}</option>`)}
        </select></label>
        <label data-help="Which advertising channel Sniffle listens on: 37, 38, 39, or hop across all three. Changing it restarts a running capture.">Channel <select aria-label="BLE advertising channel" ?disabled=${busy}
          @change=${(e) => this._setRadio("channel", e.target.value)}>
          ${["37", "38", "39", "hop"].map((v) => html`<option value=${v} ?selected=${String(s.channel) === v}>${v}</option>`)}
        </select></label>
        <span class="chan-hl">${bleChannelMHzText(s.channel)}</span>`
        : html`<span class="dim">${highlightChannel(coverageShortText(s))}</span>`}
      ${blocker && !running ? html`<span class="ble-problem">${blocker}</span>` : ""}
      ${this._controlError ? html`<span class="ble-problem" role="alert">${this._controlError}</span>` : ""}
      </div>
      <span class="ctl-label">List</span>
      <div class="control-row">
      <input type="search" placeholder="search address / name / company…" .value=${this._q}
        @input=${(e) => this._setQuery(e.target.value)} />
      <label><input type="checkbox" ?checked=${this._hideIgnored}
        @change=${() => { this._hideIgnored = !this._hideIgnored; }} /> Hide ignored</label>
      <span class="pager">
      <button class="link" ?disabled=${this._offset === 0} @click=${() => this._page(-1)}>‹ Prev</button>
      <span class="dim">${this._total ? `${this._offset + 1}-${Math.min(this._offset + LIMIT, this._total)} of ${this._total}` : "0"}</span>
      <button class="link" ?disabled=${this._offset + LIMIT >= this._total} @click=${() => this._page(1)}>Next ›</button>
      </span>
      </div>
    </div>`;
  }

  render() {
    const empty = emptyStateFor(this._status, this._rows.length);
    return html`<section class="panel ble-view" aria-label="BLE advertisers">
      <h2>BLE <span class="h2-band">${BLE_BAND}</span></h2>
      <div class="ble-tabs" role="tablist" aria-label="BLE view">
        ${[["advertisers", "Advertisers"], ["live", "Live frames"]].map(([id, label]) => html`<button role="tab"
          aria-selected=${this._view === id ? "true" : "false"} @click=${() => this._setView(id)}>${label}</button>`)}
      </div>
      ${this._renderControls()}
      ${this._renderSummary()}

      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}

      <div class=${`ble-body${this._selectedAddr && this._view !== "live" ? " has-detail" : ""}`}>
      ${this._view === "live" ? this._renderLive() : empty
        ? html`<p class="empty">${empty.text}${empty.setup ? html` <button class="link" @click=${() => this._openSetup()}>Open Setup</button>` : ""}</p>`
        : html`<div class="lib-table-wrap">
            <table class="lib-table ble-table">
              <thead><tr>${BLE_COLUMNS.map((c) => {
                const active = this._sort.sort === c.key;
                const dir = active ? (this._sort.order === "asc" ? "ascending" : "descending") : "none";
                const numeric = c.key === "rssi" || c.key === "reports";
                return html`<th scope="col" class=${numeric ? "num" : ""} aria-sort=${dir}><button type="button" class="ble-sort"
                  title=${`Sort by ${c.label.toLowerCase()}`}
                  @click=${() => { this._sort = nextSort(this._sort, c.key); this._offset = 0; this._refreshRows(); }}>
                  ${c.label}<span aria-hidden="true">${active ? (this._sort.order === "asc" ? " ▲" : " ▼") : ""}</span></button></th>`;
              })}</tr></thead>
              <tbody>${visibleRows(this._rows, this._labels, this._hideIgnored).map((row) => this._row(row))}</tbody>
            </table>
          </div>`}

      ${this._selectedAddr && this._view !== "live" ? this._renderDetail() : ""}
      </div>
    </section>`;
  }

  _row(row) {
    const selected = row.addr === this._selectedAddr;
    const label = this._labels.get(row.addr);
    const device = deviceDisplay(row, label);
    const services = servicesCellText(row.service_uuids, row.service_names);
    const rssiPct = rssiBarPct(row.rssi);
    return html`<tr class=${selected ? "selected" : ""} @click=${() => this._select(row.addr)}>
      <td class="ble-device-cell">
        <div class="ble-device-primary">${device.labeled ? html`<strong>${device.text}</strong>` : device.text}
          ${label ? html`<span class="chip timeline-class-chip timeline-class-${label.class}">${label.class}</span>` : ""}</div>
        <div class="ble-device-secondary mono dim">${row.addr}</div>
      </td>
      <td><span class=${`chip ble-identity-chip${ROTATING_IDENTITIES.has(row.identity) ? " muted" : ""}`}
        title=${identityLabel(row.identity)}>${identityChipText(row.identity)}</span>
        ${row.phy ? html`<span class="chip ble-phy-chip" title="PHY / channel, Sniffle backend">${row.phy.toUpperCase()}${row.channel != null ? ` ch${row.channel}` : ""}${row.extended ? " ext" : ""}</span>` : ""}</td>
      <td>${row.company || html`<span class="dim">—</span>`}</td>
      <td title=${(row.service_uuids ?? []).join(", ")}>${services ?? html`<span class="dim">—</span>`}</td>
      <td>${row.sensor
        ? html`<span class=${`chip ble-sensor-chip${row.sensor.encrypted ? " encrypted" : ""}`}
            title=${row.sensor.encrypted ? "Encrypted sensor payload"
              : Object.entries(row.sensor.readings ?? {}).map(([n, r]) => `${n}: ${sensorReadingText(r)}`).join(", ")}
            >${sensorChipText(row.sensor)}</span>`
        : html`<span class="dim">—</span>`}</td>
      <td class="num">${row.rssi != null
        ? html`${row.rssi} dBm<span class="ble-rssi-bar"><span class="ble-rssi-bar-fill" style="width:${rssiPct}%"></span></span>`
        : html`<span class="dim">— not reported</span>`}</td>
      <td class="num">${row.reports ?? 0}</td>
      <td>${relativeAge(row.last_seen)}</td>
    </tr>`;
  }

  _renderDetail() {
    const d = this._detail;
    if (!d) return html`<aside class="ble-detail panel"><p class="dim">Loading…</p></aside>`;
    if (d.error) return html`<aside class="ble-detail panel"><p class="ble-problem" role="alert">Could not load details for ${d.addr}: ${d.error}</p></aside>`;
    const services = (d.service_uuids ?? [])
      .map((u) => (d.service_names?.[u] ? `${d.service_names[u]} (${u})` : u))
      .join(", ") || "none advertised";
    return html`<aside class="ble-detail panel" aria-label="Advertiser details">
      <header class="ble-detail-head">
        <h3 class="mono">${d.addr}</h3>
        <button class="ble-detail-close" aria-label="Close details" @click=${() => { this._selectedAddr = null; this._detail = null; }}>×</button>
      </header>
      <div class="ble-detail-chips">
        <span class="chip">${identityLabel(d.identity)}</span>
        <span class="chip">${d.connectable ? "connectable" : "non-connectable"}</span>
        ${d.remote_id ? html`<span class="chip ble-remoteid">Remote ID</span>` : ""}
      </div>

      <div class="ble-detail-grid">
        <section>
          <h4>Identity</h4>
          <dl class="ble-facts">
            <dt>Address</dt><dd class="mono">${d.addr}</dd>
            <dt>Address type</dt><dd>${d.addr_type ?? "unknown"}</dd>
            <dt>Identity kind</dt><dd>${identityLabel(d.identity)}</dd>
            ${d.name ? html`<dt>Name</dt><dd>${d.name}</dd>` : ""}
            ${d.vendor ? html`<dt>Vendor</dt><dd>${d.vendor}</dd>` : ""}
          </dl>
        </section>
        <section>
          <h4>Advertisement</h4>
          <dl class="ble-facts">
            <dt>Connectable</dt><dd>${d.connectable ? "yes" : "no"}</dd>
            <dt>Services</dt><dd>${services}</dd>
            ${d.company ? html`<dt>Company</dt><dd>${d.company}</dd>` : ""}
            ${d.tx_power != null ? html`<dt>TX power</dt><dd>${d.tx_power} dBm</dd>` : ""}
            ${d.appearance != null ? html`<dt>Appearance</dt><dd>${d.appearance_name
              ? `${d.appearance_name} (0x${d.appearance.toString(16).padStart(4, "0")})` : d.appearance}</dd>` : ""}
            ${d.phy ? html`<dt>PHY</dt><dd>${d.phy.toUpperCase()}</dd>` : ""}
            ${d.channel != null ? html`<dt>Channel</dt><dd>${d.channel}</dd>` : ""}
            ${d.phy ? html`<dt>Extended advertising</dt><dd>${d.extended ? "yes" : "no"}</dd>` : ""}
            ${d.remote_id ? html`<dt>Remote ID</dt><dd>flagged (service data 0xFFFA)</dd>` : ""}
          </dl>
        </section>
        ${d.apple ? html`<section>
          <h4>Apple Continuity</h4>
          <dl class="ble-facts">
            <dt>Messages</dt><dd>${appleTypeNames(d.apple).join(", ")}</dd>
            ${d.apple.audio ? html`<dt>Model</dt><dd>${d.apple.audio.model ?? `unknown (${d.apple.audio.model_code})`}</dd>
              <dt>Battery</dt><dd>${appleBatteryText(d.apple.audio)}</dd>` : ""}
          </dl>
          <p class="dim">Message types only; Apple doesn't publish this format, so names and battery
            come from public reverse-engineering and are best effort.</p>
        </section>` : ""}
        <section>
          <h4>Reception</h4>
          <dl class="ble-facts">
            <dt>Heard</dt><dd>first ${relativeAge(d.first_seen)}, last ${relativeAge(d.last_seen)}</dd>
            <dt>Reports</dt><dd>${plural(d.reports ?? 0, "report")}</dd>
            <dt>RSSI</dt><dd>${this._rssiSummary(d)}</dd>
          </dl>
        </section>
      </div>

      <section class="ble-label">
        <device-label-editor receiver="ble" .ident=${d.addr} .kind=${d.identity}
          .label=${this._labels.get(d.addr) ?? null} @label-changed=${() => this._onLabelChanged()}></device-label-editor>
      </section>

      <section class="ble-history">
        <h4>History (${LABEL_HISTORY_HOURS} h)</h4>
        ${this._detailHistory
          ? html`<device-history-chart .cells=${this._detailHistory.cells ?? []} .coverage=${this._detailHistory.coverage ?? []}
              .bucketS=${this._detailHistory.bucket_s ?? 60} .start=${this._detailHistory.start ?? 0}
              .end=${this._detailHistory.end ?? 0} .signalKind="dbm"></device-history-chart>`
          : html`<p class="dim">Loading…</p>`}
      </section>

      ${d.sensor ? this._renderSensor(d.sensor) : ""}
      ${d.payloads?.length ? this._renderPayloads(d.payloads) : ""}
      ${d.recent?.length ? this._renderRecent(d.recent) : ""}

      <p class="dim ble-provenance">${plural(d.reports ?? 0, "report")} from the msdr BLE helper · ${coverageText(this._status)}</p>
    </aside>`;
  }

  /** Latest RSSI with its range, in words; a sparkline only when it varies. */
  _rssiSummary(d) {
    const values = (d.rssi_history ?? []).map((p) => p?.[1]).filter((v) => Number.isFinite(v));
    if (!values.length) return "not reported";
    const lo = Math.min(...values), hi = Math.max(...values), latest = values[values.length - 1];
    const segments = hi > lo ? sparklineSegments(d.rssi_history ?? []) : [];
    return html`${latest} dBm${hi > lo ? `, range ${lo}–${hi} dBm` : ", steady"}
      ${segments.length ? html`<svg class="ble-spark" viewBox="0 0 120 24" width="120" height="24" role="img"
        aria-label="RSSI trend">${segments.map((pts) => svg`<polyline points=${pts} fill="none" stroke="var(--context-ble)" stroke-width="1.5"></polyline>`)}</svg>` : ""}`;
  }

  /** Decoded sensor beacon readings (docs/ble.md "Sensors",
   * msdr/ble/sensors.py). Encrypted payloads show only the protocol name and
   * that they are encrypted -- never a guessed reading. */
  _renderSensor(sensor) {
    const label = SENSOR_PROTOCOL_LABELS[sensor.protocol] ?? sensor.protocol;
    if (sensor.encrypted) {
      return html`<section class="ble-sensor">
        <h4>Sensor <span class="dim">(${label})</span></h4>
        <p class="dim">Encrypted payload — readings cannot be decoded without the bind key.</p>
      </section>`;
    }
    const entries = Object.entries(sensor.readings ?? {});
    return html`<section class="ble-sensor">
      <h4>Sensor <span class="dim">(${label})</span></h4>
      <dl class="ble-sensor-readings">
        ${entries.map(([name, reading]) => html`<dt>${name.replace(/_/g, " ")}</dt><dd>${sensorReadingText(reading)}</dd>`)}
      </dl>
    </section>`;
  }

  /** Distinct payloads (last 8 kept, docs/ble.md details shape). */
  _renderPayloads(payloads) {
    return html`<section class="ble-payloads">
      <h4>Distinct payloads <span class="dim">(${payloads.length} kept)</span></h4>
      <ul>${payloads.map((p) => html`<li>
        <code class="mono ble-hex">${p.hex}</code>
        <span class="dim">${plural(p.count ?? 0, "time")} · first ${relativeAge(p.first_seen)} · last ${relativeAge(p.last_seen)}</span>
      </li>`)}</ul>
    </section>`;
  }

  /** Recent reports (last 20 kept). */
  _renderRecent(recent) {
    const latest = [...recent].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)).slice(0, 20);
    return html`<section class="ble-recent">
      <h4>Recent reports <span class="dim">(last ${recent.length} kept)</span></h4>
      <table class="lib-table ble-reports">
        <thead><tr><th>Time</th><th>Event</th><th>RSSI</th><th>Bytes</th></tr></thead>
        <tbody>${latest.map((f) => html`<tr>
          <td class="mono">${new Date((f.ts ?? 0) * 1000).toLocaleTimeString()}</td>
          <td>${f.evt}</td>
          <td>${f.rssi != null ? `${f.rssi} dBm` : "--"}</td>
          <td>${f.length}</td>
        </tr>`)}</tbody>
      </table>
    </section>`;
  }
}

customElements.define("ble-view", BleView);
