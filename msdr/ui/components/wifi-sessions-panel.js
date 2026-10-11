import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

const REFRESH_MS = 3000;

function fileSize(bytes) {
  if (bytes == null) return "--";
  const gb = bytes / 1e9;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

/** epoch seconds -> local "YYYY-MM-DD HH:MM", "--" when missing/invalid --
 * local copy of format.js's dateTime (which takes ISO strings) since Wi-Fi
 * session timestamps are epoch seconds, same convention as the rest of
 * docs/wifi.md's status payload. */
function epochDateTime(ts) {
  if (ts === null || ts === undefined) return "--";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "--";
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${mo}-${day} ${hh}:${mm}`;
}

/** Review's "Wi-Fi sessions" list -- recorded pcap captures (docs/wifi.md's
 * sessions.py), distinct from the IQ recordings list above it in
 * review-view.js. Self-contained (own polling/import), same pattern as
 * recordings-view.js. "Inspect" asks app.js to open the Discover Wi-Fi tab
 * scoped to this session (source=session:<id>). */
export class WifiSessionsPanel extends LitElement {
  static properties = {
    _sessions: { state: true },
    _error: { state: true },
    _importing: { state: true },
    _remoteIdFor: { state: true },
    _remoteIdData: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this._sessions = [];
    this._error = null;
    this._importing = false;
    this._timer = null;
    this._remoteIdSeq = 0;
    // Offline Remote ID decode of a saved session (docs/remote-id.md), shown
    // inline below its row -- same reproducible decode GET /api/wifi/sessions/
    // {id}/remoteid used by Discover's Wi-Fi "Inspect" source=session:<id>.
    this._remoteIdFor = null;
    this._remoteIdData = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    super.disconnectedCallback();
  }

  async _refresh() {
    try {
      this._sessions = await api.listWifiSessions();
      this._error = null;
    } catch (err) {
      this._error = err.message;
    }
  }

  _inspect(id) {
    this.dispatchEvent(new CustomEvent("wifi-inspect", { bubbles: true, composed: true, detail: { id } }));
  }

  async _toggleRemoteId(id) {
    const seq = ++this._remoteIdSeq;
    if (this._remoteIdFor === id) {
      this._remoteIdFor = null;
      this._remoteIdData = null;
      return;
    }
    this._remoteIdFor = id;
    this._remoteIdData = null;
    try {
      const data = await api.getWifiSessionRemoteId(id);
      if (seq !== this._remoteIdSeq) return;
      this._remoteIdData = data;
    } catch (err) {
      if (seq !== this._remoteIdSeq) return;
      this._remoteIdData = { error: err.message };
    }
  }

  _renderRemoteId(session) {
    if (this._remoteIdFor !== session.id) return "";
    const data = this._remoteIdData;
    if (!data) return html`<tr><td colspan="8"><p class="dim">Decoding…</p></td></tr>`;
    if (data.error) return html`<tr><td colspan="8"><p role="alert">${data.error}</p></td></tr>`;
    const groups = data.groups ?? [];
    return html`<tr><td colspan="8">
      <div class="remoteid-session-decode">
        <p class="dim">Offline decode of this capture · ${groups.length} UAS ${groups.length === 1 ? "group" : "groups"} found.
          Reported claims only -- never verified or inferred positions.</p>
        ${groups.length ? html`<table class="geo-table"><thead><tr>
          <th>UAS ID</th><th>Type</th><th>Conflict</th><th>Drone position</th><th>Last seen</th>
        </tr></thead><tbody>${groups.map((g) => html`<tr>
          <td>${g.tracks[0]?.uas_id_text || g.uas_id_hex || g.key}</td>
          <td>${g.tracks[0]?.ua_type ?? "Unknown"}</td>
          <td>${g.conflict ? html`<span class="ble-problem">conflict</span>` : "No"}</td>
          <td>${g.tracks.find((t) => t.drone_position)?.drone_position
            ? `${g.tracks.find((t) => t.drone_position).drone_position.latitude_deg.toFixed(5)}, ${g.tracks.find((t) => t.drone_position).drone_position.longitude_deg.toFixed(5)}`
            : "Not reported"}</td>
          <td>${new Date(Math.max(...g.tracks.map((t) => t.last_seen)) * 1000).toLocaleString()}</td>
        </tr>`)}</tbody></table>` : html`<p class="empty">No Remote ID broadcasts found in this capture.</p>`}
      </div>
    </td></tr>`;
  }

  async _import(event) {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    this._importing = true;
    this._error = null;
    try {
      await api.importWifiPcap(file);
      await this._refresh();
    } catch (err) {
      this._error = err.message;
    } finally {
      this._importing = false;
    }
  }

  render() {
    return html`<div class="panel wifi-sessions">
      <h2>Wi-Fi sessions</h2>
      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      <label>Import PCAP <input type="file" accept=".pcap,.pcapng" ?disabled=${this._importing}
        @change=${(e) => this._import(e)} /></label>
      ${this._sessions.length
        ? html`<div class="lib-table-wrap">
            <table class="lib-table mono">
              <thead><tr>
                <th>Started</th><th>Stopped</th><th>Channel</th><th>Frames</th>
                <th>Size</th><th>Drops</th><th>Health</th><th></th>
              </tr></thead>
              <tbody>${this._sessions.map((s) => html`<tr>
                <td>${epochDateTime(s.started_at)}</td>
                <td>${s.stopped_at != null ? epochDateTime(s.stopped_at) : html`<span class="dim">running</span>`}</td>
                <td>${s.channel ?? "--"}</td>
                <td>${s.frames ?? 0}</td>
                <td>${fileSize(s.bytes)}</td>
                <td>${s.drops ?? 0}</td>
                <td>${s.health ?? "--"}${s.imported ? html` <span class="chip">imported</span>` : ""}</td>
                <td>
                  <a class="link" href=${api.wifiSessionExportUrl(s.id)}>Export PCAP</a>
                  <button class="link" @click=${() => this._inspect(s.id)}>Inspect</button>
                  <button class="link" aria-pressed=${this._remoteIdFor === s.id}
                    @click=${() => this._toggleRemoteId(s.id)}>Remote ID</button>
                </td>
              </tr>${this._renderRemoteId(s)}`)}</tbody>
            </table>
          </div>`
        : html`<p class="empty">No Wi-Fi sessions recorded yet -- start one from the Discover Wi-Fi tab, or import a PCAP file.</p>`}
    </div>`;
  }
}

customElements.define("wifi-sessions-panel", WifiSessionsPanel);
