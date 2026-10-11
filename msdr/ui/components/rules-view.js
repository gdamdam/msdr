import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

// Discover -> Watch rules (docs/rules.md): create/edit/enable beacon
// presence/absence, BLE sensor threshold and Remote ID drone geofence
// rules, with pickers fed from the live Wi-Fi/BLE tables, labels and the
// Remote ID list, and the fired-event log -- the same polling pattern as
// remoteid-view.js/band-scan-inbox.js rather than a websocket channel.
const POLL_MS = 2000;

export function kindLabel(kind) {
  return (
    { beacon_presence: "Beacon presence/absence", sensor_threshold: "Sensor threshold", drone_geofence: "Drone geofence" }[kind]
    ?? kind
  );
}

export function defaultConfig(kind) {
  if (kind === "beacon_presence") {
    return { receiver: "ble", ident: "", mode: "seen", missing_after_minutes: 10 };
  }
  if (kind === "sensor_threshold") {
    return { addr: "", sensor_name: "", comparison: "above", threshold: 0, hysteresis: 0 };
  }
  return { geometry: { type: "circle", center: { lat: 0, lon: 0 }, radius_m: 100 }, max_age_s: 60 };
}

export function summarizeConfig(rule) {
  const c = rule.config ?? {};
  if (rule.kind === "beacon_presence") {
    return c.mode === "missing"
      ? `${c.ident} missing on ${c.receiver} for ${c.missing_after_minutes} min`
      : `${c.ident} seen on ${c.receiver}`;
  }
  if (rule.kind === "sensor_threshold") {
    return `${c.addr} ${c.sensor_name} ${c.comparison} ${c.threshold}${c.hysteresis ? ` (±${c.hysteresis})` : ""}`;
  }
  if (rule.kind === "drone_geofence") {
    const g = c.geometry ?? {};
    return g.type === "circle"
      ? `inside ${g.radius_m} m of ${g.center?.lat?.toFixed?.(4)}, ${g.center?.lon?.toFixed?.(4)}`
      : `inside a ${g.points?.length ?? 0}-point area`;
  }
  return JSON.stringify(c);
}

/** Merge live Wi-Fi networks and BLE advertisers (plus their labels) into
 * one `{receiver, ident, label}` option list for the beacon_presence and
 * sensor_threshold pickers. */
export function beaconOptions(wifiItems, bleItems, labelsByKey) {
  const labelFor = (receiver, ident) => labelsByKey?.[`${receiver}:${ident}`]?.name;
  const wifi = (wifiItems ?? []).map((n) => ({
    receiver: "wifi", ident: n.bssid, label: labelFor("wifi", n.bssid) ?? (n.ssid || n.bssid),
  }));
  const ble = (bleItems ?? [])
    .filter((b) => b.identity === "public" || b.identity === "static")
    .map((b) => (
      { receiver: "ble", ident: b.addr, label: labelFor("ble", b.addr) ?? (b.name || b.addr) }
    ));
  return [...wifi, ...ble];
}

export function validatePolygonPoints(points) {
  if (!Array.isArray(points) || points.length < 3) return "A polygon needs at least 3 points.";
  for (const p of points) {
    if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon)) return "Every point needs a numeric lat/lon.";
  }
  return null;
}

export class RulesView extends LitElement {
  static properties = {
    _rules: { state: true }, _events: { state: true }, _error: { state: true },
    _wifi: { state: true }, _ble: { state: true }, _labels: { state: true },
    _drones: { state: true }, _editing: { state: true }, _testResult: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this._rules = [];
    this._events = [];
    this._error = null;
    this._wifi = [];
    this._ble = [];
    this._labels = {};
    this._drones = [];
    this._reqSeq = 0;
    this._refreshing = false;
    this._editing = null; // null | "new" | rule id
    this._draft = null;
    this._testResult = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => { if (!this._refreshing) this._refresh(); }, POLL_MS);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const seq = ++this._reqSeq;
    this._refreshing = true;
    try {
      const [rules, events, wifi, ble, wifiLabels, bleLabels, remoteid] = await Promise.all([
        api.listRules(), api.listRuleEvents({ limit: 100 }),
        api.listWifiNetworks({ limit: 500 }), api.listBleAdvertisers({ limit: 500 }),
        api.listLabels("wifi"), api.listLabels("ble"), api.getRemoteIdLive(),
      ]);
      if (seq !== this._reqSeq) return;
      this._rules = rules.items ?? [];
      this._events = events.items ?? [];
      this._wifi = wifi.items ?? [];
      this._ble = ble.items ?? [];
      this._labels = Object.fromEntries(
        [...(wifiLabels.items ?? []), ...(bleLabels.items ?? [])].map((l) => [`${l.receiver}:${l.ident}`, l])
      );
      this._drones = remoteid.items ?? [];
      this._error = null;
    } catch (err) {
      if (seq === this._reqSeq) this._error = err.message;
    } finally {
      if (seq === this._reqSeq) this._refreshing = false;
    }
  }

  _startNew(kind) {
    this._editing = "new";
    this._draft = { kind, name: "", enabled: true, cooldown_s: 60, config: defaultConfig(kind) };
    this._testResult = null;
  }

  _startEdit(rule) {
    this._editing = rule.id;
    this._draft = { ...rule, config: { ...rule.config } };
    this._testResult = null;
  }

  _cancelEdit() {
    this._editing = null;
    this._draft = null;
  }

  async _save() {
    try {
      const body = {
        kind: this._draft.kind, name: this._draft.name, enabled: this._draft.enabled,
        cooldown_s: Number(this._draft.cooldown_s) || 0, config: this._draft.config,
      };
      if (this._editing === "new") await api.createRule(body);
      else await api.patchRule(this._editing, body);
      this._cancelEdit();
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    }
  }

  async _toggleEnabled(rule) {
    try {
      await api.patchRule(rule.id, { enabled: !rule.enabled });
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    }
  }

  async _delete(rule) {
    try {
      await api.deleteRule(rule.id);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    }
  }

  async _test(rule) {
    try {
      this._testResult = { id: rule.id, ...(await api.testRule(rule.id)) };
    } catch (err) {
      this._error = err.message;
    }
  }

  _updateDraftConfig(patch) {
    this._draft = { ...this._draft, config: { ...this._draft.config, ...patch } };
  }

  _renderBeaconFields() {
    const c = this._draft.config;
    const options = beaconOptions(this._wifi, this._ble, this._labels)
      .filter((o) => o.receiver === c.receiver);
    return html`
      <label>Receiver
        <select .value=${c.receiver} @change=${(e) => this._updateDraftConfig({ receiver: e.target.value, ident: "" })}>
          <option value="ble">BLE</option>
          <option value="wifi">Wi-Fi</option>
        </select>
      </label>
      <label>Device
        <input list="rules-beacon-options" .value=${c.ident}
          @input=${(e) => this._updateDraftConfig({ ident: e.target.value.trim().toLowerCase() })}
          placeholder="aa:bb:cc:dd:ee:ff" />
        <datalist id="rules-beacon-options">
          ${options.map((o) => html`<option value=${o.ident}>${o.label}</option>`)}
        </datalist>
      </label>
      <label>Mode
        <select .value=${c.mode} @change=${(e) => this._updateDraftConfig({ mode: e.target.value })}>
          <option value="seen">Seen</option>
          <option value="missing">Not seen (missing)</option>
        </select>
      </label>
      ${c.mode === "missing" ? html`<label>Missing after (minutes)
        <input type="number" min="1" max="1440" .value=${c.missing_after_minutes}
          @input=${(e) => this._updateDraftConfig({ missing_after_minutes: Number(e.target.value) })} />
      </label>` : ""}
      <p class="dim">Only public/static BLE addresses can be watched -- a rotating private address is refused, same as a label.</p>
    `;
  }

  _renderSensorFields() {
    const c = this._draft.config;
    const bleOptions = beaconOptions([], this._ble, this._labels);
    return html`
      <label>BLE device
        <input list="rules-sensor-options" .value=${c.addr}
          @input=${(e) => this._updateDraftConfig({ addr: e.target.value.trim().toLowerCase() })}
          placeholder="aa:bb:cc:dd:ee:ff" />
        <datalist id="rules-sensor-options">
          ${bleOptions.map((o) => html`<option value=${o.ident}>${o.label}</option>`)}
        </datalist>
      </label>
      <label>Sensor name
        <input .value=${c.sensor_name} placeholder="temp_c"
          @input=${(e) => this._updateDraftConfig({ sensor_name: e.target.value.trim() })} />
      </label>
      <label>Comparison
        <select .value=${c.comparison} @change=${(e) => this._updateDraftConfig({ comparison: e.target.value })}>
          <option value="above">Above</option>
          <option value="below">Below</option>
        </select>
      </label>
      <label>Threshold
        <input type="number" step="any" .value=${c.threshold}
          @input=${(e) => this._updateDraftConfig({ threshold: Number(e.target.value) })} />
      </label>
      <label>Hysteresis
        <input type="number" step="any" min="0" .value=${c.hysteresis}
          @input=${(e) => this._updateDraftConfig({ hysteresis: Number(e.target.value) })} />
      </label>
      <p class="dim">A sensor name that never appears on this device simply never fires -- it is not an error.</p>
    `;
  }

  _useDronePosition() {
    const fresh = this._drones.find((d) => d.drone_position && !d.drone_position.stale);
    if (!fresh) return;
    this._updateDraftConfig({
      geometry: {
        ...this._draft.config.geometry,
        center: { lat: fresh.drone_position.latitude_deg, lon: fresh.drone_position.longitude_deg },
      },
    });
  }

  _renderGeofenceFields() {
    const c = this._draft.config;
    const g = c.geometry;
    const setGeometry = (patch) => this._updateDraftConfig({ geometry: { ...g, ...patch } });
    return html`
      <label>Shape
        <select .value=${g.type} @change=${(e) => setGeometry(
          e.target.value === "circle"
            ? { type: "circle", center: g.center ?? { lat: 0, lon: 0 }, radius_m: g.radius_m ?? 100, points: undefined }
            : { type: "polygon", points: g.points ?? [{ lat: 0, lon: 0 }, { lat: 0, lon: 0 }, { lat: 0, lon: 0 }], center: undefined, radius_m: undefined }
        )}>
          <option value="circle">Circle</option>
          <option value="polygon">Polygon</option>
        </select>
      </label>
      ${g.type === "circle" ? html`
        <label>Center latitude
          <input type="number" step="any" .value=${g.center?.lat ?? 0}
            @input=${(e) => setGeometry({ center: { ...g.center, lat: Number(e.target.value) } })} />
        </label>
        <label>Center longitude
          <input type="number" step="any" .value=${g.center?.lon ?? 0}
            @input=${(e) => setGeometry({ center: { ...g.center, lon: Number(e.target.value) } })} />
        </label>
        <label>Radius (m)
          <input type="number" min="1" .value=${g.radius_m ?? 100}
            @input=${(e) => setGeometry({ radius_m: Number(e.target.value) })} />
        </label>
        <button type="button" @click=${() => this._useDronePosition()}
          ?disabled=${!this._drones.some((d) => d.drone_position && !d.drone_position.stale)}
        >Center on a live drone's reported position</button>
      ` : html`
        <p class="dim">Polygon points (lat, lon), at least 3:</p>
        ${(g.points ?? []).map((p, i) => html`<div class="control-row">
          <input type="number" step="any" .value=${p.lat} @input=${(e) => {
            const points = [...g.points]; points[i] = { ...p, lat: Number(e.target.value) };
            setGeometry({ points });
          }} />
          <input type="number" step="any" .value=${p.lon} @input=${(e) => {
            const points = [...g.points]; points[i] = { ...p, lon: Number(e.target.value) };
            setGeometry({ points });
          }} />
          <button type="button" @click=${() => setGeometry({ points: g.points.filter((_, j) => j !== i) })}>Remove</button>
        </div>`)}
        <button type="button" @click=${() => setGeometry({ points: [...(g.points ?? []), { lat: 0, lon: 0 }] })}>Add point</button>
      `}
      <label>Max position age (s)
        <input type="number" min="1" .value=${c.max_age_s}
          @input=${(e) => this._updateDraftConfig({ max_age_s: Number(e.target.value) })} />
      </label>
      <p class="dim">Only a fresh, reported drone position is used -- never a verified or inferred location.</p>
    `;
  }

  _renderEditor() {
    if (!this._draft) return "";
    return html`<form class="panel rules-editor" @submit=${(e) => { e.preventDefault(); this._save(); }}>
      <h3>${this._editing === "new" ? "New rule" : "Edit rule"} -- ${kindLabel(this._draft.kind)}</h3>
      <label>Name
        <input required maxlength="80" .value=${this._draft.name}
          @input=${(e) => (this._draft = { ...this._draft, name: e.target.value })} />
      </label>
      ${this._draft.kind === "beacon_presence" ? this._renderBeaconFields() : ""}
      ${this._draft.kind === "sensor_threshold" ? this._renderSensorFields() : ""}
      ${this._draft.kind === "drone_geofence" ? this._renderGeofenceFields() : ""}
      <label>Cooldown (s)
        <input type="number" min="0" .value=${this._draft.cooldown_s}
          @input=${(e) => (this._draft = { ...this._draft, cooldown_s: Number(e.target.value) })} />
      </label>
      <label><input type="checkbox" .checked=${this._draft.enabled}
        @change=${(e) => (this._draft = { ...this._draft, enabled: e.target.checked })} /> Enabled</label>
      <div class="control-row">
        <button type="submit">Save</button>
        <button type="button" @click=${() => this._cancelEdit()}>Cancel</button>
      </div>
    </form>`;
  }

  render() {
    return html`<section class="panel rules-view" aria-label="Watch rules">
      <h2>Watch rules</h2>
      <p class="dim">Beacon presence/absence, BLE sensor thresholds and Remote ID drone geofences, evaluated continuously. Firing never implies a verified location -- only a reported one.</p>
      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      <div class="control-row">
        <button type="button" @click=${() => this._startNew("beacon_presence")}>+ Beacon rule</button>
        <button type="button" @click=${() => this._startNew("sensor_threshold")}>+ Sensor rule</button>
        <button type="button" @click=${() => this._startNew("drone_geofence")}>+ Geofence rule</button>
      </div>
      ${this._renderEditor()}
      ${this._rules.length ? html`<table class="geo-table"><thead><tr>
          <th>Name</th><th>Kind</th><th>Condition</th><th>Cooldown</th><th>Enabled</th><th></th>
        </tr></thead><tbody>${this._rules.map((r) => html`<tr>
          <td>${r.name}</td>
          <td>${kindLabel(r.kind)}</td>
          <td>${summarizeConfig(r)}</td>
          <td>${r.cooldown_s}s</td>
          <td><input type="checkbox" .checked=${r.enabled} @change=${() => this._toggleEnabled(r)} /></td>
          <td class="control-row">
            <button type="button" @click=${() => this._startEdit(r)}>Edit</button>
            <button type="button" @click=${() => this._test(r)}>Test</button>
            <button type="button" @click=${() => this._delete(r)}>Delete</button>
          </td>
        </tr>`)}</tbody></table>` : html`<p class="empty">No watch rules yet.</p>`}
      ${this._testResult ? html`<p class="dim">Rule ${this._testResult.rule_id} would fire:
        ${this._testResult.would_fire.length
          ? this._testResult.would_fire.map((f) => f.message).join("; ")
          : "nothing right now."}</p>` : ""}
      <h3>Recent fired events</h3>
      ${this._events.length ? html`<table class="geo-table"><thead><tr>
          <th>When</th><th>Rule</th><th>Target</th><th>Message</th>
        </tr></thead><tbody>${this._events.map((ev) => html`<tr>
          <td>${new Date(ev.fired_at * 1000).toLocaleString()}</td>
          <td>${this._rules.find((r) => r.id === ev.rule_id)?.name ?? ev.rule_id}</td>
          <td>${ev.target}</td>
          <td>${ev.message}</td>
        </tr>`)}</tbody></table>` : html`<p class="empty">No rules have fired yet.</p>`}
    </section>`;
  }
}

customElements.define("rules-view", RulesView);
