import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

// Discover -> Remote ID (docs/remote-id.md "Live"): a compact live UAS table
// plus a details pane, polling GET /api/remoteid/live the same way
// wifi-view.js/ble-view.js poll their own status -- this view has no
// dependency on the websocket "remoteid" message, so it works even on a
// slow/backgrounded tab that only gets REST responses.
const POLL_MS = 1000;

const UA_TYPES = {
  0: "None/unknown", 1: "Aeroplane", 2: "Multirotor", 3: "Gyroplane",
  4: "Hybrid lift", 5: "Ornithopter", 6: "Glider", 7: "Kite",
  8: "Free balloon", 9: "Captive balloon", 10: "Airship", 11: "Free fall/parachute",
  12: "Rocket", 13: "Tethered powered aircraft", 14: "Ground obstacle", 15: "Other",
};

export function uaTypeLabel(uaType) {
  return UA_TYPES[uaType] ?? (uaType == null ? "Unknown" : `Type ${uaType}`);
}

const age = (ts) => Number.isFinite(ts) ? `${Math.max(0, Math.floor(Date.now() / 1000 - ts))} s ago` : "unknown";

function fmt(v, unit = "", digits = 0) {
  return Number.isFinite(v) ? `${v.toFixed(digits)} ${unit}`.trim() : "Not received";
}

function positionSummary(pos, label) {
  if (!pos) return `${label}: not reported`;
  return `${label}: ${pos.latitude_deg.toFixed(5)}, ${pos.longitude_deg.toFixed(5)}${pos.stale ? " (stale)" : ""}`;
}

// One sort key per table column; a null key keeps the server's order.
// Missing values sort last in either direction so unknowns never crowd the top.
const SORT_VALUE = {
  uas: (g) => (g.uas_id_text || g.uas_id_hex || g.key || "").toLowerCase(),
  type: (g) => uaTypeLabel(g.ua_type),
  transport: (g) => (g.sources ?? []).join(", "),
  positions: (g) => ["drone", "operator", "takeoff"].filter((k) => g[`${k}_position`]).length,
  last_seen: (g) => (Number.isFinite(g.last_seen) ? g.last_seen : null),
  flags: (g) => (g.conflict ? 1 : 0),
};
const COLUMNS = [
  ["uas", "UAS ID"], ["type", "Type"], ["transport", "Transport"],
  ["positions", "Positions"], ["last_seen", "Last seen"], ["flags", "Flags"],
];

export function sortRows(rows, sort) {
  const value = SORT_VALUE[sort?.key];
  if (!value) return rows;
  const sign = sort.dir === "desc" ? -1 : 1;
  return rows.map((r, i) => [r, i, value(r)]).sort((a, b) => {
    if (a[2] == null || b[2] == null) return (a[2] == null) - (b[2] == null) || a[1] - b[1];
    return (a[2] < b[2] ? -sign : a[2] > b[2] ? sign : 0) || a[1] - b[1];
  }).map(([r]) => r);
}

export class RemoteIdView extends LitElement {
  static properties = {
    _items: { state: true }, _now: { state: true }, _selectedKey: { state: true },
    _detail: { state: true }, _error: { state: true }, _q: { state: true }, _sort: { state: true },
  };
  createRenderRoot() { return this; }
  constructor() {
    super();
    this._items = [];
    this._now = null;
    this._selectedKey = null;
    this._detail = null;
    this._error = null;
    this._q = "";
    this._sort = { key: null, dir: "asc" };
    this._detailSeq = 0;
  }
  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._pollTimer = setInterval(() => this._refresh(), POLL_MS);
  }
  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._pollTimer);
  }
  async _refresh() {
    try {
      const body = await api.getRemoteIdLive();
      this._items = body.items ?? [];
      this._now = body.now ?? null;
      this._error = null;
      if (this._selectedKey) this._fetchDetail(this._selectedKey);
    } catch (err) {
      this._error = err.message;
    }
  }
  async _fetchDetail(key) {
    const seq = ++this._detailSeq;
    try {
      const detail = await api.getRemoteIdUas(key);
      if (seq !== this._detailSeq) return;
      this._detail = detail;
    } catch (err) {
      if (seq !== this._detailSeq) return;
      this._detail = { key, error: err.message };
    }
  }
  _select(key) {
    this._selectedKey = this._selectedKey === key ? null : key;
    this._detail = this._selectedKey ? this._detail : null;
    if (!this._selectedKey) this._detailSeq += 1; // drop a fetch still in flight for the deselected UAS
    if (this._selectedKey) this._fetchDetail(this._selectedKey);
  }
  _toggleSort(key) {
    this._sort = this._sort.key === key
      ? { key, dir: this._sort.dir === "asc" ? "desc" : "asc" }
      : { key, dir: "asc" };
  }
  get _rows() {
    const q = this._q.trim().toLowerCase();
    if (!q) return sortRows(this._items, this._sort);
    return sortRows(this._items, this._sort).filter((g) =>
      (g.uas_id_text || "").toLowerCase().includes(q) ||
      (g.uas_id_hex || "").toLowerCase().includes(q) ||
      (g.key || "").toLowerCase().includes(q));
  }
  _renderDetail() {
    const d = this._detail;
    if (!d || !this._selectedKey) return "";
    if (d.error) return html`<aside class="panel"><p role="alert">${d.error}</p></aside>`;
    return html`<aside class="panel remoteid-detail" aria-label="UAS details">
      <header class="wifi-detail-head">
        <h3>${d.uas_id_text || d.uas_id_hex || d.key}</h3>
        <button class="wifi-detail-close" aria-label="Close details" @click=${() => this._select(this._selectedKey)}>×</button>
      </header>
      <p class="dim">${uaTypeLabel(d.ua_type)} · transport ${d.sources.join(", ") || "unknown"}
        · last seen ${age(d.last_seen)}${d.stale ? " · stale" : ""}
        ${d.conflict ? " · CONFLICT: two sources disagree on this ID" : ""}</p>
      <ul>
        <li>${positionSummary(d.drone_position, "Drone position")}</li>
        <li>${positionSummary(d.operator_position, "Operator position")}</li>
        <li>${positionSummary(d.takeoff_position, "Takeoff/reference position")}</li>
      </ul>
      ${d.drone_position ? html`<dl class="vehicle-detail-facts">
        <div><dt>Altitude (barometric)</dt><dd>${fmt(d.drone_position.altitude_baro_m, "m")}</dd></div>
        <div><dt>Altitude (geodetic)</dt><dd>${fmt(d.drone_position.altitude_geo_m, "m")}</dd></div>
        <div><dt>Speed</dt><dd>${fmt(d.drone_position.speed_horizontal_ms, "m/s", 1)}</dd></div>
        <div><dt>Heading</dt><dd>${fmt(d.drone_position.direction_deg, "°")}</dd></div>
        <div><dt>Horizontal accuracy</dt><dd>${fmt(d.drone_position.horiz_accuracy_m, "m")}</dd></div>
        <div><dt>Vertical accuracy</dt><dd>${fmt(d.drone_position.vert_accuracy_m, "m")}</dd></div>
      </dl>` : ""}
      <p class="dim">Every field above is a reported claim: receiving a broadcast does not authenticate it, and no location is inferred beyond what was explicitly sent.</p>
      <details open><summary>Recent messages (${d.recent_messages?.length ?? 0})</summary>
        <table class="geo-table"><thead><tr><th>Received</th><th>Type</th><th>Fields</th></tr></thead><tbody>
          ${(d.recent_messages ?? []).slice().reverse().map((m) => html`<tr>
            <td>${new Date(m.received_at * 1000).toLocaleTimeString()}</td>
            <td>${m.message_type}</td>
            <td class="mono">${JSON.stringify(Object.fromEntries(
              Object.entries(m).filter(([k]) => !["received_at", "message_type"].includes(k))
            ))}</td>
          </tr>`)}
        </tbody></table>
      </details>
    </aside>`;
  }
  render() {
    const rows = this._rows;
    return html`<section class="panel remoteid-view" aria-label="Remote ID">
      <h2>Remote ID</h2>
      <p class="dim">Live drone identification decoded from trusted Wi-Fi beacons and BLE advertisements on both receivers. Reported claims only — never verified or inferred positions.</p>
      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      <div class="control-row">
        <input type="search" placeholder="search UAS ID…" .value=${this._q}
          @input=${(e) => (this._q = e.target.value)} />
        <span class="dim">${rows.length} tracked</span>
      </div>
      ${rows.length
        ? html`<table class="geo-table"><thead><tr>
            ${COLUMNS.map(([key, label]) => {
              const active = this._sort.key === key;
              const dir = active ? (this._sort.dir === "asc" ? "ascending" : "descending") : "none";
              return html`<th scope="col" aria-sort=${dir}><button type="button" class="wifi-sort"
                @click=${() => this._toggleSort(key)}>${label}<span aria-hidden="true">${active ? (this._sort.dir === "asc" ? " ▲" : " ▼") : ""}</span></button></th>`;
            })}
          </tr></thead><tbody>${rows.map((g) => html`<tr class=${g.key === this._selectedKey ? "selected" : ""}>
            <td><button class="geo-select" @click=${() => this._select(g.key)}>${g.uas_id_text || g.uas_id_hex || g.key}</button></td>
            <td>${uaTypeLabel(g.ua_type)}</td>
            <td>${g.sources.join(", ") || "unknown"}</td>
            <td>${["drone", "operator", "takeoff"].filter((k) => g[`${k}_position`]).join(", ") || "none"}</td>
            <td>${age(g.last_seen)}${g.stale ? " · stale" : ""}</td>
            <td>${g.conflict ? html`<span class="ble-problem">conflict</span>` : ""}</td>
          </tr>`)}</tbody></table>`
        : html`<p class="empty">No Remote ID broadcasts received yet. Start the Wi-Fi and/or BLE receiver in Setup.</p>`}
      ${this._renderDetail()}
    </section>`;
  }
}
customElements.define("remoteid-view", RemoteIdView);
