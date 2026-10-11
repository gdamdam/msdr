import { LitElement, html } from "../vendor/lit.js";
import { mapRows, remoteIdRows, trackSegments } from "../map-data.js";
import * as api from "../api.js";
import { vehicleExternalLinks } from "../map-vehicle-links.js";

const LAYERS = { aircraft: "Aircraft", maritime: "Maritime", amateur: "APRS", sondes: "Radiosondes", stations: "Stations / HF", remoteid: "Drones (Remote ID)" };
const COLORS = { aircraft: "#69b6ff", maritime: "#365bd2", amateur: "#efbd58", sondes: "#e397ed", stations: "#ef925e" };
// One color per Remote ID position kind (docs/remote-id.md "Map"): markers
// also differ in shape (diamond/circle/square, see .remoteid-marker in
// styles.css) so the kinds are tellable apart without relying on hue alone.
const REMOTEID_COLORS = { drone: "#ff5577", operator: "#5fd3bc", takeoff: "#ffd166" };
// Explicit-gap threshold for Remote ID trails: tighter than the 1800s default
// (aircraft/AIS) because a position is itself considered stale after 60s
// (docs/remote-id.md "Live") -- a trail must never bridge a stale gap or a
// receiver reconnect with a straight line.
const REMOTEID_TRAIL_GAP_S = 60;
// Both silhouettes point to the top of the map at 0°. Their artwork is fixed,
// unlike platform emoji, so a true course can rotate the nose exactly.
const DIRECTION_ICONS = {
  aircraft: '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 2 L19 13 L29 18 L29 21 L19 18 L18 26 L22 29 L22 31 L16 28 L10 31 L10 29 L14 26 L13 18 L3 21 L3 18 L13 13 Z"/></svg>',
  maritime: '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 2 L25 12 L24 25 Q16 30 8 25 L7 12 Z"/><path class="direction-deck" d="M16 8 L16 24"/></svg>',
};
const trackBearing = (from, to) => {
  const rad = Math.PI / 180;
  const a = from.lat * rad, b = to.lat * rad, delta = (to.lon - from.lon) * rad;
  return (Math.atan2(Math.sin(delta) * Math.cos(b),
    Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(delta)) / rad + 360) % 360;
};
const positioned = r => Number.isFinite(r.lat) && Number.isFinite(r.lon);
const age = ts => Number.isFinite(ts) ? `${Math.max(0, Math.floor((Date.now() / 1000 - ts) / 60))} min ago` : "time unknown";

const rowAge = row => row.positionTimeKnown === false ? `report ${age(row.ts)}; position age unknown` : age(row.ts);
// Map and Table are separate workspace views; retain their investigation
// state while Lit unmounts one view and mounts the other.
let selectedRowId = null;
let selectedRowSnapshot = null;
const MAP_CLEAR_KEY = "msdr.map.clear";
function readMapClear() {
  try {
    const saved = JSON.parse(localStorage.getItem(MAP_CLEAR_KEY) ?? "null");
    return { at: Number.isFinite(saved?.at) ? saved.at : 0,
      ids: Array.isArray(saved?.ids) ? saved.ids.filter(id => typeof id === "string") : [] };
  } catch { return { at: 0, ids: [] }; }
}
const savedMapClear = readMapClear();
let clearedMapAt = savedMapClear.at;
let hiddenMapReferenceIds = new Set(savedMapClear.ids);
let hiddenMapIds = new Set(savedMapClear.ids);
function persistMapClear() {
  try { localStorage.setItem(MAP_CLEAR_KEY, JSON.stringify({ at: clearedMapAt,
    ids: [...hiddenMapReferenceIds] })); } catch { /* Storage may be unavailable. */ }
}

/** Positions are evidence attached to messages, never inferred from frequency. */
export class DiscoverMap extends LitElement {
  static properties = {
    events: { type: Array }, signals: { type: Array }, location: { type: Object },
    view: { type: String }, canTune: { type: Boolean },
    _catalog: { state: true }, _error: { state: true }, _layer: { state: true },
    _query: { state: true }, _selected: { state: true }, _tiles: { state: true },
    _tick: { state: true }, _hiddenIds: { state: true }, _selectedSnapshot: { state: true },
    _trackEvents: { state: true }, _remoteId: { state: true },
  };
  createRenderRoot() { return this; }
  constructor() {
    super();
    this.events = []; this.signals = []; this.location = null; this.view = "map";
    this.canTune = false; this._catalog = []; this._error = ""; this._layer = "";
    this._query = ""; this._selected = selectedRowId; this._tiles = true; this._tick = 0;
    this._hiddenIds = hiddenMapIds;
    this._selectedSnapshot = selectedRowSnapshot;
    this._trackEvents = [];
    this._remoteId = []; // GET /api/remoteid/live items, polled below (docs/remote-id.md "Map")
    this._hoverRowKey = null;
    this._hoverOrigin = null;
    this._hoverHideTimer = null;
    this._hoverShowTimer = null;
    this._hoverPositionFrame = 0;
    this._onPendingVehiclePointerMove = event => {
      if (!event.target.closest?.(".vehicle-map-label, .reception-direction-marker")) {
        clearTimeout(this._hoverShowTimer);
        this._hoverShowTimer = null;
        document.removeEventListener("pointermove", this._onPendingVehiclePointerMove);
      }
    };
    this._onVehiclePointerMove = event => {
      if (!this._hoverPreview) return;
      if (event.target.closest?.(".vehicle-map-label, .reception-direction-marker, .vehicle-hover-card")) {
        clearTimeout(this._hoverHideTimer);
        this._hoverHideTimer = null;
      } else this._scheduleVehicleHoverHide();
    };
  }
  connectedCallback() {
    super.connectedCallback(); this._loadCatalog(); this._loadTrackEvents();
    this._timer = setInterval(() => this._tick++, 30000);
    this._loadRemoteId();
    this._remoteIdTimer = setInterval(() => this._loadRemoteId(), 2000);
  }
  disconnectedCallback() {
    super.disconnectedCallback(); clearInterval(this._timer); clearInterval(this._remoteIdTimer);
    this._hideVehicleHover();
    this._mapResizeObserver?.disconnect();
    cancelAnimationFrame(this._mapResizeFrame);
    this._map?.remove(); this._map = null;
  }
  async _loadRemoteId() {
    try {
      const result = await api.getRemoteIdLive();
      if (!this.isConnected) return;
      this._remoteId = result.items ?? [];
    } catch { /* leave the previous list showing; the rest of the map stays usable */ }
  }
  // Groups cleared by "Clear map" (ts-based, same reasoning as _mapEvents
  // below) -- a group with a newer report than the clear reappears on its own.
  get _mapRemoteIdGroups() {
    return this._remoteId.filter(g => !(Number.isFinite(g.last_seen) && g.last_seen <= clearedMapAt));
  }
  async _loadCatalog() {
    try {
      const result = await api.searchCatalog({ has_coordinates: true, hi_hz: 30000000, limit: 500 });
      if (!this.isConnected) return;
      this._catalog = result.entries ?? [];
      this._error = result.count > 500 ? "Showing the first 500 located HF catalog entries." : "";
    } catch { this._error = "HF catalog unavailable. Decoded positions and memories remain available."; }
  }
  async _loadTrackEvents() {
    // Decoder events are already persisted. Load the retained ADS-B and AIS
    // history so a path survives leaving Map and is not limited to the live
    // message strip's latest 500 events.
    const settled = await Promise.allSettled([
      api.listDecodeEvents({ limit: 5000, decoder: "adsb" }),
      api.listDecodeEvents({ limit: 5000, decoder: "ais" }),
    ]);
    if (!this.isConnected) return;
    this._trackEvents = settled.flatMap(item => item.status === "fulfilled" && Array.isArray(item.value) ? item.value : []);
  }
  get _mapEvents() {
    const seen = new Set();
    return [...this.events, ...this._trackEvents].filter(event => {
      if (!event || seen.has(event.id) ||
          (Number.isFinite(event.ts) && event.ts <= clearedMapAt)) return false;
      if (event.id != null) seen.add(event.id);
      return true;
    });
  }
  _selectedRow(rows) {
    const trackId = this._selectedSnapshot?.trackId;
    if (trackId) return rows.filter(row => row.trackId === trackId && positioned(row))
      .sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))[0] ?? this._selectedSnapshot;
    return rows.find(row => row.id === this._selected) ?? this._selectedSnapshot;
  }
  _cardSide(trackId) {
    const points = mapRows(this._mapEvents, [], []).filter(row => row.trackId === trackId && positioned(row));
    const segment = trackSegments(points).at(-1);
    if (!segment) return 1;
    // Keep the route on the opposite side of the anchored card.
    return segment[0].lon > segment.at(-1).lon ? -1 : 1;
  }
  _fitSelectedTrack(trackId) {
    if (!this._map || !trackId) return false;
    const points = mapRows(this._mapEvents, [], []).filter(row => row.trackId === trackId && positioned(row));
    const segment = trackSegments(points).at(-1);
    if (!segment) return false;
    const sidePadding = Math.min(390, this._map.getSize().x * 0.42);
    const cardLeft = this._cardSide(trackId) < 0;
    this._map.fitBounds(segment.map(row => [row.lat, row.lon]), {
      paddingTopLeft: [cardLeft ? sidePadding : 45, 50],
      paddingBottomRight: [cardLeft ? 45 : sidePadding, 50],
      maxZoom: 11, animate: false,
    });
    return true;
  }
  get rows() {
    const q = this._query.trim().toLowerCase();
    const base = mapRows(this.view === "map" ? this._mapEvents : this.events, this.signals, this._catalog);
    // Remote ID is a live map-only layer (docs/remote-id.md "Map"): it has no
    // decoder event/signal/catalog shape, so it never appears in the Table view.
    const remoteid = this.view === "map" ? remoteIdRows(this._mapRemoteIdGroups) : [];
    return [...base, ...remoteid].filter(r =>
      (this.view !== "map" || !this._hiddenIds.has(r.id)) &&
      (!this._layer || r.layer === this._layer) && (!q || `${r.label} ${r.text}`.toLowerCase().includes(q)));
  }
  _clearMap() {
    const rows = mapRows(this._mapEvents, this.signals, this._catalog);
    clearedMapAt = Date.now() / 1000;
    this._hiddenIds = new Set([...this._hiddenIds, ...rows.map(row => row.id)]);
    hiddenMapIds = this._hiddenIds;
    // Decoder fixes are filtered by reception time; only timeless references
    // and events without timestamps need individual IDs across reloads.
    hiddenMapReferenceIds = new Set([...hiddenMapReferenceIds,
      ...rows.filter(row => !Number.isFinite(row.receivedAt)).map(row => row.id)]);
    persistMapClear();
    this._selected = selectedRowId = null;
    this._selectedSnapshot = selectedRowSnapshot = null;
  }
  async _refreshMap() {
    // Refresh fetches current sources; Clear map is a persisted visibility
    // boundary. Keep that boundary when retained decoder history is reloaded,
    // otherwise a routine refresh resurrects every old position and station.
    this._initialFit = false;
    this.dispatchEvent(new CustomEvent("refresh-map", { bubbles: true, composed: true }));
    await Promise.all([this._loadCatalog(), this._loadTrackEvents()]);
  }
  _range(row) {
    const loc = this.location;
    if (!positioned(row) || !Number.isFinite(loc?.lat) || !Number.isFinite(loc?.lon)) return "";
    const rad = Math.PI / 180, a = loc.lat * rad, b = row.lat * rad;
    const dLat = b - a, dLon = (row.lon - loc.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a) * Math.cos(b) * Math.sin(dLon / 2) ** 2;
    const km = 12742 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
    const bearing = (Math.atan2(Math.sin(dLon) * Math.cos(b),
      Math.cos(a) * Math.sin(b) - Math.sin(a) * Math.cos(b) * Math.cos(dLon)) / rad + 360) % 360;
    return `${km.toFixed(1)} km · ${Math.round(bearing) % 360}° true from receiver`;
  }
  _select(row) {
    this._hideVehicleHover();
    this._selectedSnapshot = selectedRowSnapshot = row;
    this._selected = selectedRowId = row.id;
    if (positioned(row) && !(row.trackId && ["aircraft", "maritime"].includes(row.layer) && this._fitSelectedTrack(row.trackId))) {
      this._map?.panTo([row.lat, row.lon]);
    }
    if (this.view !== "map" || !["aircraft", "maritime"].includes(row.layer)) {
      this.updateComplete.then(() => {
        if (this._selected === row.id) this.querySelector(".geo-detail")?.scrollIntoView({ block: "start", behavior: "auto" });
      });
    }
  }
  _vehicleFacts(row, full = false) {
    const missing = "Not received";
    const text = value => value == null || value === "" ? missing : String(value);
    const numeric = (value, suffix, digits = 0) => Number.isFinite(value) ? `${value.toFixed(digits)} ${suffix}` : missing;
    const facts = row.layer === "aircraft"
      ? [["ICAO address", text(row.icao)], ["Altitude", numeric(row.altitude, "m")]]
      : [["MMSI", text(row.mmsi)]];
    facts.push(["Speed", numeric(row.speedKn, "kn", 1)], ["Course", numeric(row.courseDeg, "°", 1)],
      ["Received", age(row.receivedAt)], ["Frequency", Number.isFinite(row.frequency_hz) ? `${(row.frequency_hz / 1e6).toFixed(6)} MHz` : missing],
      ["Decoder", text(row.event?.decoder)]);
    if (full) {
      facts.unshift(["Callsign", text(row.callsign)]);
      if (row.layer === "aircraft") facts.splice(1, 0, ["Registration", text(row.registration)]);
      facts.push(["Position age", rowAge(row)], ["Source", text(row.event?.source_id)],
        ["SNR", numeric(row.decodedFields?.snr_db, "dB", 1)],
        ["RF level", numeric(row.decodedFields?.rssi_dbm, "dBm", 1)],
        ["Integrity", text(row.event?.integrity_note || row.event?.integrity)]);
    }
    return facts;
  }
  _hideVehicleHover() {
    cancelAnimationFrame(this._hoverPositionFrame);
    this._hoverPositionFrame = 0;
    clearTimeout(this._hoverHideTimer);
    this._hoverHideTimer = null;
    clearTimeout(this._hoverShowTimer);
    this._hoverShowTimer = null;
    document.removeEventListener("pointermove", this._onPendingVehiclePointerMove);
    this._hoverPreview?.getElement()?.remove();
    this._hoverPreview?.remove();
    this._hoverPreview = null;
    this._hoverRowKey = null;
    this._hoverOrigin = null;
    document.removeEventListener("pointermove", this._onVehiclePointerMove);
  }
  _scheduleVehicleHoverHide() {
    if (this._hoverHideTimer) return;
    this._hoverHideTimer = setTimeout(() => this._hideVehicleHover(), 2500);
  }
  _scheduleVehicleHover(row, origin = null) {
    if (this._hoverPreview && this._hoverRowKey === (row.trackId ?? row.id)) {
      clearTimeout(this._hoverHideTimer);
      this._hoverHideTimer = null;
      return;
    }
    clearTimeout(this._hoverShowTimer);
    this._hoverOrigin = origin;
    document.addEventListener("pointermove", this._onPendingVehiclePointerMove);
    this._hoverShowTimer = setTimeout(() => {
      this._hoverShowTimer = null;
      document.removeEventListener("pointermove", this._onPendingVehiclePointerMove);
      this._showVehicleHover(row);
    }, 500);
  }
  _positionVehicleHover() {
    const card = this._hoverPreview?.getElement();
    const canvas = this.querySelector(".discover-map-canvas");
    if (!card || !canvas) return;
    const bounds = canvas.getBoundingClientRect();
    const inset = 8;
    card.style.width = `${Math.min(320, Math.max(0, bounds.width - 2 * inset))}px`;
    card.style.marginLeft = "0px";
    card.style.marginTop = "0px";
    const rect = card.getBoundingClientRect();
    const point = this._map.latLngToContainerPoint(this._hoverPreview.getLatLng());
    const x = bounds.left + point.x, y = bounds.top + point.y;
    const origin = this._hoverOrigin?.isConnected ? this._hoverOrigin.getBoundingClientRect()
      : { left: x - 8, right: x + 8, top: y - 8, bottom: y + 8 };
    const centered = (origin.left + origin.right - rect.width) / 2;
    const middle = (origin.top + origin.bottom - rect.height) / 2;
    const candidates = [
      [centered, origin.top - rect.height - 12],
      [centered, origin.bottom + 12],
      [origin.left - rect.width - 12, middle],
      [origin.right + 12, middle],
    ];
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
    const fitted = candidates.map(([left, top], index) => {
      left = clamp(left, bounds.left + inset, bounds.right - inset - rect.width);
      top = clamp(top, bounds.top + inset, bounds.bottom - inset - rect.height);
      const overlap = Math.max(0, Math.min(left + rect.width, origin.right) - Math.max(left, origin.left))
        * Math.max(0, Math.min(top + rect.height, origin.bottom) - Math.max(top, origin.top));
      return { left, top, overlap, index };
    }).sort((a, b) => a.overlap - b.overlap || a.index - b.index)[0];
    card.style.marginLeft = `${fitted.left - rect.left}px`;
    card.style.marginTop = `${fitted.top - rect.top}px`;
  }
  _scheduleHoverPosition() {
    if (!this._hoverPreview || this._hoverPositionFrame) return;
    this._hoverPositionFrame = requestAnimationFrame(() => {
      this._hoverPositionFrame = 0;
      this._positionVehicleHover();
    });
  }
  _showVehicleHover(row) {
    clearTimeout(this._hoverHideTimer);
    this._hoverHideTimer = null;
    if (!this._map) return;
    const key = row.trackId ?? row.id;
    if (this._hoverPreview && this._hoverRowKey === key) {
      this._hoverPreview.setLatLng([row.lat, row.lon]);
      this._positionVehicleHover();
      return;
    }
    const content = document.createElement("div");
    content.setAttribute("role", "tooltip");
    const title = document.createElement("strong");
    title.textContent = String(row.label);
    const facts = document.createElement("dl");
    for (const [name, value] of this._vehicleFacts(row)) {
      const term = document.createElement("dt"), description = document.createElement("dd");
      term.textContent = name; description.textContent = value;
      facts.append(term, description);
    }
    const help = document.createElement("small");
    help.textContent = "Click icon or label for received details";
    content.append(title, facts, help);
    const external = vehicleExternalLinks(row).find(link => link.name === (row.layer === "aircraft" ? "FlightAware" : "VesselFinder"));
    if (external) {
      const link = document.createElement("a");
      link.className = "vehicle-hover-link";
      link.textContent = `${external.name} ↗`;
      link.href = external.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.addEventListener("click", event => event.stopPropagation());
      content.append(link);
    }
    content.addEventListener("mouseenter", () => {
      clearTimeout(this._hoverHideTimer);
      this._hoverHideTimer = null;
    });
    content.addEventListener("mouseleave", () => this._scheduleVehicleHoverHide());
    const origin = this._hoverOrigin;
    this._hideVehicleHover();
    this._hoverOrigin = origin;
    document.addEventListener("pointermove", this._onVehiclePointerMove);
    this._hoverRowKey = key;
    this._hoverPreview = globalThis.L.tooltip({ direction: "top", offset: [0, -20],
      className: "vehicle-hover-card", opacity: 1, interactive: true })
      .setLatLng([row.lat, row.lon]).setContent(content).addTo(this._map);
    this._positionVehicleHover();
  }
  _tune(row) {
    if (!this.canTune) return;
    const type = row.signal ? "tune-signal" : "tune-catalog";
    this.dispatchEvent(new CustomEvent(type, { bubbles: true, composed: true,
      detail: { id: row.signal?.id ?? row.catalog?.id ?? row.id, label: row.label } }));
  }
  _resizeMap(width, height) {
    const frame = this.querySelector(".discover-map-frame");
    if (!frame) return;
    const available = frame.parentElement.clientWidth;
    frame.style.width = `${Math.min(available, Math.max(240, width))}px`;
    frame.style.height = `${Math.max(240, Math.min(1200, height))}px`;
  }
  _startMapResize(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    const rect = this.querySelector(".discover-map-frame").getBoundingClientRect();
    this._mapDrag = { x: event.clientX, y: event.clientY, width: rect.width, height: rect.height };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  _moveMapResize(event) {
    const start = this._mapDrag;
    if (start) this._resizeMap(start.width + event.clientX - start.x, start.height + event.clientY - start.y);
  }
  _resetMapSize() {
    const frame = this.querySelector(".discover-map-frame");
    frame.style.removeProperty("width");
    frame.style.removeProperty("height");
  }
  _keyMapResize(event) {
    if (event.key === "Home") { event.preventDefault(); this._resetMapSize(); return; }
    const delta = { ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] }[event.key];
    if (!delta) return;
    event.preventDefault();
    const rect = this.querySelector(".discover-map-frame").getBoundingClientRect();
    this._resizeMap(rect.width + delta[0], rect.height + delta[1]);
  }
  updated() {
    const canvas = this.querySelector(".discover-map-canvas");
    if (!canvas || !globalThis.L) return;
    const L = globalThis.L;
    if (!this._map) {
      const loc = this.location;
      const valid = Number.isFinite(loc?.lat) && Number.isFinite(loc?.lon) && Math.abs(loc.lat) <= 90 && Math.abs(loc.lon) <= 180;
      this._map = L.map(canvas, { scrollWheelZoom: true }).setView(valid ? [loc.lat, loc.lon] : [25, 0], valid ? 6 : 2);
      this._map.on("move zoom resize", () => this._scheduleHoverPosition());
      this._mapResizeObserver = new ResizeObserver(() => {
        cancelAnimationFrame(this._mapResizeFrame);
        this._mapResizeFrame = requestAnimationFrame(() => {
          this._map?.invalidateSize({ pan: false });
          this._scheduleHoverPosition();
        });
      });
      this._mapResizeObserver.observe(canvas);
      this._markers = L.layerGroup().addTo(this._map);
      this._base = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      });
      this._base.on("tileerror", () => { if (!this._tileFailed) { this._tileFailed = true; this.requestUpdate(); } });
    }
    if (this.view === "map") this._map.invalidateSize();
    if (this._tiles && this.view === "map") this._base.addTo(this._map);
    else this._base.remove();
    if (this.view !== "map") this._hideVehicleHover();
    // Replace labels immediately; fading old interactive labels leaves duplicate targets.
    this._markers.eachLayer(layer => layer.getTooltip?.()?.getElement()?.remove());
    this._markers.clearLayers();
    const rows = this.rows;
    if (!this._initialFit && this.view === "map" && rows.some(positioned)) {
      this._initialFit = true;
      this._fit();
    }
    // Only the newest position per source/entity gets a marker. Older fixes form tracks.
    const latest = new Map();
    for (const row of rows.filter(positioned)) {
      const key = row.trackId ?? row.id;
      if (!latest.has(key) || (row.ts ?? 0) > (latest.get(key).ts ?? 0)) latest.set(key, row);
    }
    if (this._hoverPreview) {
      const hovered = [...latest.values()].find(row => (row.trackId ?? row.id) === this._hoverRowKey);
      if (hovered) {
        this._hoverPreview.setLatLng([hovered.lat, hovered.lon]);
        this._scheduleHoverPosition();
      } else this._scheduleVehicleHoverHide();
    }
    const segments = [
      ...trackSegments(rows.filter(r => r.layer !== "remoteid")),
      ...trackSegments(rows.filter(r => r.layer === "remoteid"), { maxGapSeconds: REMOTEID_TRAIL_GAP_S }),
    ];
    const recentTrackCourse = new Map();
    for (const segment of segments) {
      const last = segment.at(-1), previous = segment.at(-2);
      if (last && previous && (last.lat !== previous.lat || last.lon !== previous.lon)) {
        recentTrackCourse.set(last.id, trackBearing(previous, last));
      }
    }
    for (const row of latest.values()) {
      if (row.layer === "remoteid") {
        const symbol = document.createElement("span");
        symbol.className = `remoteid-marker ${row.posKind}${row.stale ? " stale" : ""}`;
        const icon = L.divIcon({ html: symbol, iconSize: [16, 16], iconAnchor: [8, 8],
          className: `remoteid-marker-wrap${row.id === this._selected ? " selected" : ""}` });
        const marker = L.marker([row.lat, row.lon], { icon,
          title: `${row.posKind} position: ${row.label}${row.stale ? " (stale)" : ""}`,
          alt: `${row.posKind} position: ${row.label}` });
        const fmt = (v, unit = "") => Number.isFinite(v) ? `${v.toFixed(unit === "°" ? 0 : 1)}${unit}` : "not received";
        const lines = [`<strong>${row.uasIdText || row.uasIdHex || "Unknown UAS"}</strong> · ${row.posKind}`];
        if (row.posKind === "drone") {
          lines.push(`Altitude: barometric ${fmt(row.altitudeBaroM, " m")} · geodetic ${fmt(row.altitudeGeoM, " m")}`);
          lines.push(`Speed ${fmt(row.speedHorizontalMs, " m/s")} · Heading ${fmt(row.headingDeg, "°")}`);
          lines.push(`Accuracy: ±${fmt(row.horizAccuracyM, " m")} horiz / ±${fmt(row.vertAccuracyM, " m")} vert`);
        } else {
          lines.push(`Altitude: ${fmt(row.altitudeGeoM, " m")}`);
        }
        lines.push(`Transport: ${row.transports.join(", ") || "unknown"}`);
        lines.push(`Last report: ${rowAge(row)}${row.stale ? " · stale" : ""}`);
        lines.push(`<small>Reported claim, not a verified location</small>`);
        marker.bindTooltip(lines.join("<br>"), { direction: "top", offset: [0, -8], opacity: 1,
          className: "remoteid-hover-card" })
          .on("click", () => this._select(row)).addTo(this._markers);
        continue;
      }
      const old = row.event && (row.positionTimeKnown === false || !Number.isFinite(row.ts) || Date.now() / 1000 - row.ts > 900);
      const fixAge = Number.isFinite(row.ts) ? Math.max(0, Date.now() / 1000 - row.ts) : Infinity;
      const fresh = !!row.event && row.positionTimeKnown !== false && fixAge < 120;
      const direction = row.courseDeg ?? recentTrackCourse.get(row.id);
      const directional = row.layer === "aircraft" || row.layer === "maritime";
      const selectedTrack = !!row.trackId && row.trackId === this._selectedSnapshot?.trackId;
      let marker;
      if (directional) {
        const symbol = document.createElement("span");
        if (direction == null) {
          // Keep the vehicle type visible without giving its artwork a map bearing.
          symbol.className = "type-symbol";
          const vehicle = document.createElement("span");
          vehicle.textContent = row.layer === "aircraft" ? "✈️" : "🛥️";
          const unknown = document.createElement("span");
          unknown.className = "direction-unknown-badge";
          unknown.textContent = "?";
          symbol.append(vehicle, unknown);
        } else {
          symbol.className = "direction-symbol";
          symbol.innerHTML = DIRECTION_ICONS[row.layer];
          symbol.style.transform = `rotate(${direction}deg)`;
        }
        const icon = L.divIcon({ html: symbol, iconSize: [32, 32], iconAnchor: [16, 16],
          className: `reception-direction-marker ${row.layer}${direction == null ? " direction-unknown" : ""}${selectedTrack || row.id === this._selected ? " selected" : ""}${fresh ? " fresh" : ""}` });
        const directionLabel = direction == null ? "direction unknown"
          : `${row.courseDeg != null ? "reported course" : "course from track"} ${Math.round(direction)}°`;
        marker = L.marker([row.lat, row.lon], { icon, opacity: old ? 0.55 : 1,
          title: `${row.layer === "aircraft" ? "Aircraft" : "Vessel"}: ${row.label} · ${directionLabel}${old ? " (stale)" : ""}`,
          alt: `${row.layer === "aircraft" ? "Aircraft" : "Vessel"}: ${row.label} · ${directionLabel}` });
      } else {
        marker = L.circleMarker([row.lat, row.lon], { radius: row.id === this._selected ? 9 : 6,
          color: COLORS[row.layer], fillOpacity: old ? 0.45 : 0.85, opacity: old ? 0.7 : 1,
          className: `reception-point-marker${fresh ? " fresh" : ""}` });
      }
      if (fresh) marker.on("add", () => {
        const el = marker.getElement();
        if (el) el.style.setProperty("--fresh-age", `-${fixAge}s`);
      });
      const label = document.createElement(directional ? "button" : "span");
      label.textContent = directional ? `${row.label}${old ? " · stale" : ""}`
        : `${row.label} · ${row.evidence} · ${row.event ? rowAge(row) : "reference location"}`;
      if (directional) {
        label.type = "button";
        label.className = "vehicle-label-button";
        label.setAttribute("aria-label", `Details for ${row.label}`);
        label.addEventListener("mouseenter", event => this._scheduleVehicleHover(row, event.currentTarget));
        label.addEventListener("mouseleave", () => this._scheduleVehicleHoverHide());
        label.addEventListener("focus", () => this._showVehicleHover(row));
        label.addEventListener("blur", () => this._scheduleVehicleHoverHide());
        label.addEventListener("pointerdown", event => {
          if (event.button !== 0) return;
          event.stopPropagation();
          this._select(row);
        });
        label.addEventListener("click", event => {
          event.stopPropagation();
          if (event.detail === 0) this._select(row);
        });
        marker.on("mouseover", () => this._scheduleVehicleHover(row, marker.getElement()));
        marker.on("mouseout", () => this._scheduleVehicleHoverHide());
      }
      const tooltipOptions = directional ? {
        permanent: true, interactive: true, direction: "right", offset: [14, 0], opacity: 1,
        className: `vehicle-map-label ${row.layer}-map-label`,
      } : {};
      marker.bindTooltip(label, tooltipOptions).on("click", () => this._select(row)).addTo(this._markers);
    }
    const selectedTrackId = this._selectedSnapshot?.trackId;
    for (const segment of segments) {
      const selected = !!selectedTrackId && segment[0].trackId === selectedTrackId;
      const color = segment[0].layer === "remoteid"
        ? (REMOTEID_COLORS[segment[0].posKind] ?? "#9aa7bb") : (COLORS[segment[0].layer] ?? "#8eaee6");
      L.polyline(segment.map(r => [r.lat, r.lon]), {
        color, weight: selected ? 5 : 2.5, opacity: selected ? 0.98 : 0.45,
        lineCap: "round", lineJoin: "round", className: selected ? "selected-vehicle-track" : "reception-track",
      }).addTo(this._markers);
      if (selected) {
        L.circleMarker([segment[0].lat, segment[0].lon], { radius: 4, color,
          weight: 2, fillColor: "#101820", fillOpacity: 1 }).bindTooltip("First received fix in this segment").addTo(this._markers);
      }
    }
    // A thin line links one UAS's simultaneously-reported drone/operator/
    // takeoff positions -- never a trail (trackSegments above already draws
    // each kind's own trail), just "these belong to the same aircraft right now".
    const remoteIdLatestByGroup = new Map();
    for (const row of latest.values()) {
      if (row.layer !== "remoteid") continue;
      if (!remoteIdLatestByGroup.has(row.groupKey)) remoteIdLatestByGroup.set(row.groupKey, {});
      remoteIdLatestByGroup.get(row.groupKey)[row.posKind] = row;
    }
    for (const kinds of remoteIdLatestByGroup.values()) {
      const points = ["drone", "operator", "takeoff"].map(k => kinds[k]).filter(Boolean);
      if (points.length > 1) {
        L.polyline(points.map(r => [r.lat, r.lon]), {
          color: "#9aa7bb", weight: 1.5, opacity: 0.8, dashArray: "4 4", className: "remoteid-link-line",
        }).addTo(this._markers);
      }
    }
    const loc = this.location;
    if (Number.isFinite(loc?.lat) && Number.isFinite(loc?.lon) && Math.abs(loc.lat) <= 90 && Math.abs(loc.lon) <= 180) {
      L.circleMarker([loc.lat, loc.lon], { radius: 7, color: "#fff", fillOpacity: 0 }).bindTooltip("Receiver location").addTo(this._markers);
    }
  }
  _fit() {
    const points = this.rows.filter(positioned).map(r => [r.lat, r.lon]);
    if (points.length) this._map?.fitBounds(points, { padding: [25, 25], maxZoom: 12 });
  }
  _renderRemoteIdDetails(selected) {
    const fmt = (v, unit = "", digits = 0) => Number.isFinite(v) ? `${v.toFixed(digits)} ${unit}`.trim() : "Not received";
    return html`<aside class="geo-detail" aria-label="Selected Remote ID evidence">
      <div class="geo-detail-heading"><strong>${selected.uasIdText || selected.uasIdHex || "Unknown UAS"}</strong>
        <button aria-label="Close map details" @click=${() => {
          this._selected = selectedRowId = null;
          this._selectedSnapshot = selectedRowSnapshot = null;
        }}>×</button></div>
      <p class="dim">Reported ${selected.posKind} position · a Remote ID claim, never verified or inferred.</p>
      <dl class="vehicle-detail-facts">
        <div><dt>UAS ID</dt><dd>${selected.uasIdText || selected.uasIdHex || "Unknown"}</dd></div>
        <div><dt>UA type</dt><dd>${selected.uaType ?? "Unknown"}</dd></div>
        <div><dt>Transport</dt><dd>${selected.transports.join(", ") || "Unknown"}</dd></div>
        <div><dt>Conflict</dt><dd>${selected.conflict ? "Yes — two sources disagree on this ID" : "No"}</dd></div>
        ${selected.posKind === "drone" ? html`
          <div><dt>Altitude (barometric)</dt><dd>${fmt(selected.altitudeBaroM, "m")}</dd></div>
          <div><dt>Altitude (geodetic)</dt><dd>${fmt(selected.altitudeGeoM, "m")}</dd></div>
          <div><dt>Height above takeoff</dt><dd>${fmt(selected.heightM, "m")}</dd></div>
          <div><dt>Speed</dt><dd>${fmt(selected.speedHorizontalMs, "m/s", 1)}</dd></div>
          <div><dt>Heading</dt><dd>${fmt(selected.headingDeg, "°")}</dd></div>
          <div><dt>Horizontal accuracy</dt><dd>${fmt(selected.horizAccuracyM, "m")}</dd></div>
          <div><dt>Vertical accuracy</dt><dd>${fmt(selected.vertAccuracyM, "m")}</dd></div>
        ` : html`<div><dt>Altitude (geodetic)</dt><dd>${fmt(selected.altitudeGeoM, "m")}</dd></div>`}
        <div><dt>Last report</dt><dd>${rowAge(selected)}${selected.stale ? " · stale" : ""}</dd></div>
      </dl>
      <p>${selected.lat.toFixed(5)}, ${selected.lon.toFixed(5)}</p>
      <p class="dim">${this._range(selected)}</p>
    </aside>`;
  }
  _renderDetails(selected) {
    if (selected.layer === "remoteid") return this._renderRemoteIdDetails(selected);
    const vehicle = selected.layer === "aircraft" || selected.layer === "maritime";
    const links = vehicleExternalLinks(selected);
    const history = selected.trackId ? mapRows(this.view === "map" ? this._mapEvents : this.events, [], [])
      .filter(row => row.trackId === selected.trackId)
      .sort((a, b) => (b.receivedAt ?? 0) - (a.receivedAt ?? 0)) : [];
    return html`<aside class="geo-detail" aria-label="Selected map evidence">
      <div class="geo-detail-heading"><strong>${selected.label}</strong>
        <button aria-label="Close map details" @click=${() => {
          this._selected = selectedRowId = null;
          this._selectedSnapshot = selectedRowSnapshot = null;
        }}>×</button></div>
      <p class="dim">Selected reception · ${selected.evidence}</p>
      ${vehicle ? html`<div class="vehicle-track-summary">Observed trajectory · ${history.filter(positioned).length} position fixes from this receiver${history.filter(positioned).length < 2 ? "; another fix is needed to draw a path" : ""}.
        ${history.filter(positioned).length > 1 ? html`<button @click=${() => this._fitSelectedTrack(selected.trackId)}>Fit trajectory</button>` : ""}
      </div>` : ""}
      ${vehicle ? html`<dl class="vehicle-detail-facts">${this._vehicleFacts(selected, true).map(([name, value]) =>
        html`<div><dt>${name}</dt><dd>${value}</dd></div>`)}</dl>` : ""}
      ${vehicle ? html`<section class="vehicle-lookups" aria-label="External vehicle details">
        <strong>More information</strong>
        ${links.length ? html`<div class="vehicle-lookup-buttons">${links.map(link => html`<div>
          <a class="vehicle-lookup-link" href=${link.url} target="_blank" rel="noopener noreferrer">${link.name} ↗</a>
          <small>${link.note}</small>
          </div>`)}</div>
          <p class="dim">Opens an external website. Match the identifier and date; provider data is separate from your reception.</p>`
          : html`<p class="dim">No lookup identifier received yet.</p>`}
      </section>` : ""}
      <p>${selected.text}</p><p class="dim">${this._range(selected)}</p>
      <p>${positioned(selected) ? `${selected.lat.toFixed(5)}, ${selected.lon.toFixed(5)}` : "No usable position"}
        ${!vehicle && selected.altitude != null ? ` · altitude ${selected.altitude} m` : ""}
        ${!vehicle && selected.frequency_hz ? ` · ${(selected.frequency_hz / 1e6).toFixed(6)} MHz` : ""}
        · ${selected.event ? rowAge(selected) : "reference location, not proof of reception"}</p>
      ${selected.signal || selected.catalog ? html`<button ?disabled=${!this.canTune} @click=${() => this._tune(selected)}>Tune</button>` : ""}
      ${vehicle && selected.trackId ? html`<details class="vehicle-track-history"><summary>Received track history · ${history.length} reports available</summary>
        <p class="dim">Most recent 10 reports from this decoder and source; no external tracking data.</p>
        <ol>${history.slice(0, 10).map(row => html`<li><button @click=${() => this._select(row)}>
          ${Number.isFinite(row.receivedAt) ? new Date(row.receivedAt * 1000).toLocaleString() : "Time unknown"}
          · ${positioned(row) ? `${row.lat.toFixed(4)}, ${row.lon.toFixed(4)}` : "No usable position"}
        </button></li>`)}</ol></details>` : ""}
      ${selected.event ? html`<details><summary>Original decoded fields</summary><pre>${JSON.stringify(selected.event, null, 2)}</pre></details>` : ""}
    </aside>`;
  }
  render() {
    const rows = this.rows, mapped = rows.filter(positioned);
    const selected = this._selectedRow(rows);
    return html`<section class="panel discover-geography" aria-label="Reception map and table">
      <div class="geo-toolbar">
        <label>Layer <select aria-label="Map layer" @change=${e => this._layer = e.target.value}>
          <option value="">All layers</option>${Object.entries(LAYERS).map(([id, label]) => html`<option value=${id}>${label}</option>`)}
        </select></label>
        <input aria-label="Search map and table" placeholder="Search station or message" @input=${e => this._query = e.target.value}>
        ${this.view === "table" ? html`<button @click=${() => this._loadCatalog()}>Refresh catalog</button>` : ""}
        ${this.view === "map" ? html`<button title="Hide current map points; new live positions can still appear" @click=${() => this._clearMap()}>Clear map</button>
          <button title="Reload decoded messages, saved stations, and catalog positions" @click=${() => this._refreshMap()}>Refresh map</button>
          <button @click=${() => this._fit()}>Fit positions</button>
          <label><input type="checkbox" .checked=${this._tiles} @change=${e => this._tiles = e.target.checked}>Online basemap</label>` : ""}
      </div>
      <p class="dim geo-summary">${mapped.length} positioned / ${rows.length} rows
        <span class="help-mark" tabindex="0" role="note" aria-label="What this shows"
          title="Retained ADS-B/AIS decoder history plus saved stations and located HF catalog entries. Faded markers are over 15 minutes old or have unknown position age."
          data-help="Retained ADS-B/AIS decoder history plus saved stations and located HF catalog entries. Faded markers are over 15 minutes old or have unknown position age.">?</span></p>
      ${this._error ? html`<p role="status">${this._error}</p>` : ""}
      <div class="discover-map-frame" ?hidden=${this.view !== "map"}>
        <div class="discover-map-canvas" aria-label="Reception positions"></div>
        ${this.view === "map" && selected && positioned(selected) && ["aircraft", "maritime"].includes(selected.layer)
          ? html`<div class=${`vehicle-map-card ${this._cardSide(selected.trackId) < 0 ? "on-left" : "on-right"}`}
              aria-label="Vehicle details card">${this._renderDetails(selected)}</div>` : ""}
        <button class="map-resize-handle" aria-label="Resize map"
          title="Drag to resize map; arrow keys adjust size; double-click or Home resets"
          @pointerdown=${event => this._startMapResize(event)}
          @pointermove=${event => this._moveMapResize(event)}
          @pointerup=${() => { this._mapDrag = null; }}
          @pointercancel=${() => { this._mapDrag = null; }}
          @lostpointercapture=${() => { this._mapDrag = null; }}
          @keydown=${event => this._keyMapResize(event)}
          @dblclick=${() => this._resetMapSize()}>◢</button>
      </div>
      ${this.view === "map" && this._tiles ? html`<p class="dim">Basemap loads from OpenStreetMap. ${this._tileFailed ? "Some tiles are unavailable; positions and table still work." : ""}</p>` : ""}
      ${!mapped.length && this.view === "map" ? html`<p>No positioned messages yet. Add coordinates to a saved station, import located catalog entries, or receive position-bearing messages. Messages without a position remain in Table.</p>` : ""}
      ${this.view === "table" ? html`<div class="geo-table-scroll"><table class="geo-table"><thead><tr>
        <th>Station / message</th><th>Layer</th><th>Position</th><th>Evidence / age</th><th>Action</th>
      </tr></thead><tbody>${rows.map(row => html`<tr class=${row.id === this._selected ? "selected" : ""}>
        <td><button class="geo-select" @click=${() => this._select(row)}>${row.label}</button><div class="dim">${row.text}</div></td>
        <td>${LAYERS[row.layer] ?? row.layer}</td><td>${positioned(row) ? `${row.lat.toFixed(4)}, ${row.lon.toFixed(4)}` : "No usable position"}<div class="dim">${this._range(row)}</div></td>
        <td>${row.evidence}<div class="dim">${row.event ? rowAge(row) : "reference location"}</div></td>
        <td>${row.signal || row.catalog ? html`<button ?disabled=${!this.canTune} @click=${() => this._tune(row)}>Tune</button>` : ""}</td>
      </tr>`)}</tbody></table>${!rows.length ? html`<p>No matching messages or stations.</p>` : ""}</div>` : ""}
      ${selected && (this.view !== "map" || !["aircraft", "maritime"].includes(selected.layer) || !positioned(selected)) ? this._renderDetails(selected) : ""}
    </section>`;
  }
}
customElements.define("discover-map", DiscoverMap);
