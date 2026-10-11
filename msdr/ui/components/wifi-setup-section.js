import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

const STATUS_REFRESH_MS = 1000;

// 2.4 GHz channels 1-13 plus the 20 MHz-wide 5 GHz channels the helper can
// tune (docs/wifi.md: "fixed 20 MHz channel; no hopping").
const CHANNELS_24 = Array.from({ length: 13 }, (_, i) => i + 1);
const CHANNELS_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165];

const STATE_LABELS = {
  unavailable: "helper not installed",
  dependency_missing: "a helper dependency is missing",
  no_device: "no compatible adapter detected",
  stopped: "stopped",
  starting: "starting…",
  receiving: "receiving",
  stale: "receiving (no frames recently)",
  failed: "failed",
};

/** Why the START control is disabled right now, or null when it's fine to
 * press -- shown as the button's title so the operator doesn't have to guess
 * (helper missing / disabled in settings / no device, per docs/wifi.md UI
 * contract). */
export function startDisabledReason(status, enabled) {
  if (!enabled) return "Turn on “Enable Wi-Fi receiver” first";
  if (!status) return null;
  if (status.state === "unavailable") return "Helper not installed -- see the setup command below";
  if (status.state === "dependency_missing") return "A helper dependency is missing -- see the setup command below";
  if (status.state === "no_device") return "No AWUS036ACH-class USB adapter detected";
  if (["starting", "receiving", "stale"].includes(status.state)) return "Already running";
  return null;
}

/** Self-contained like integration-panel.js: polls its own status/devices
 * and emits `save-wifi` for setup-drawer.js to PATCH (same whole-object
 * pattern as decoder/survey/beacons there), so this file never touches the
 * drawer's big draft/patch machinery. */
export class WifiSetupSection extends LitElement {
  static properties = {
    settings: { type: Object },
    _draft: { state: true },
    _status: { state: true },
    _devices: { state: true },
    _busy: { state: true },
    _error: { state: true },
    _vendors: { state: true },
    _vendorsBusy: { state: true },
    _vendorsError: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.settings = {};
    this._draft = {};
    this._status = null;
    this._devices = [];
    this._busy = false;
    this._error = null;
    this._timer = null;
    this._vendors = null;
    this._vendorsBusy = false;
    this._vendorsError = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refreshStatus();
    this._refreshDevices();
    this._refreshVendors();
    this._timer = setInterval(() => this._refreshStatus(), STATUS_REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    super.disconnectedCallback();
  }

  async _refreshVendors() {
    try {
      this._vendors = await api.wifiVendors();
    } catch (err) {
      this._vendorsError = err.message;
    }
  }

  async _downloadVendors() {
    this._vendorsBusy = true;
    this._vendorsError = null;
    try {
      this._vendors = await api.refreshWifiVendors();
    } catch (err) {
      this._vendorsError = err.message;
    } finally {
      this._vendorsBusy = false;
    }
  }

  _renderVendors() {
    const v = this._vendors;
    const when = v?.fetched_at ? new Date(v.fetched_at * 1000).toLocaleDateString() : null;
    return html`<div class="wifi-vendor-names">
      <p>Vendor names: ${v?.downloaded
        ? html`${v.prefixes.toLocaleString()} MAC prefixes · downloaded ${when}`
        : html`<span class="dim">not downloaded</span>`}
        <button ?disabled=${this._vendorsBusy} @click=${() => this._downloadVendors()}>
          ${this._vendorsBusy ? "Downloading…" : v?.downloaded ? "Update" : "Download"}</button></p>
      <p class="dim">Downloads the IEEE MAC address registry (about 5 MB) into your msdr data
        folder to name the maker of each BSSID, and of BLE devices with a public address.
        Not shipped with msdr.</p>
      ${this._vendorsError ? html`<p role="alert">${this._vendorsError}</p>` : ""}
    </div>`;
  }

  willUpdate(changed) {
    if (changed.has("settings")) {
      this._draft = {
        enabled: !!this.settings?.enabled,
        helperPath: this.settings?.helper_path ?? "",
        channel: this.settings?.channel ?? 6,
      };
    }
  }

  async _refreshStatus() {
    try {
      this._status = await api.getWifiStatus();
    } catch (err) {
      this._error = err.message;
    }
  }

  async _refreshDevices() {
    try {
      this._devices = await api.getWifiDevices();
    } catch {
      // devices listing needs the helper installed; leave _devices = []
      // rather than surfacing this as a hard error alongside status.
    }
  }

  _set(key, value) {
    this._draft = { ...this._draft, [key]: value };
  }

  _save() {
    this._busy = true;
    const config = {
      ...this.settings,
      enabled: this._draft.enabled,
      helper_path: this._draft.helperPath || null,
      channel: Number(this._draft.channel),
    };
    this.dispatchEvent(new CustomEvent("save-wifi", { bubbles: true, composed: true, detail: { config } }));
  }

  saveComplete() {
    this._busy = false;
    // START with unsaved changes saves first, then continues here.
    if (this._startAfterSave) {
      this._startAfterSave = false;
      this._start();
    }
  }

  /** Draft differs from the saved settings (enable, helper path or channel). */
  _dirty() {
    const s = this.settings ?? {};
    return !!this._draft.enabled !== !!s.enabled
      || (this._draft.helperPath || null) !== (s.helper_path ?? null)
      || Number(this._draft.channel) !== Number(s.channel ?? 6);
  }

  saveFailed(message) {
    this._busy = false;
    this._startAfterSave = false;
    this._error = `Could not save Wi-Fi settings: ${message}`;
  }

  async _start() {
    this._error = null;
    if (this._dirty()) {
      // The server only starts a receiver that is enabled in saved settings.
      this._startAfterSave = true;
      this._save();
      return;
    }
    try {
      await api.startWifi(Number(this._draft.channel));
      await this._refreshStatus();
    } catch (err) {
      this._error = `Start failed: ${err.message}`;
    }
  }

  async _stop() {
    this._error = null;
    try {
      await api.stopWifi();
      await this._refreshStatus();
    } catch (err) {
      this._error = err.message;
    }
  }

  render() {
    const d = this._draft;
    const s = this._status;
    const running = s && ["starting", "receiving", "stale"].includes(s.state);
    const startReason = startDisabledReason(s, d.enabled);
    const device = s?.device ?? this._devices.find((dv) => dv.supported) ?? null;
    return html`<div class="setup-group wifi-setup">
      <p class="setup-help">Reads passively from a separate USB Wi-Fi adapter and its own helper process -- independent of SDRconnect and the rest of this receiver. msdr never transmits, injects, deauthenticates or associates.</p>

      <label class="integration-toggle"><input type="checkbox" .checked=${!!d.enabled}
        @change=${(e) => { this._set("enabled", e.target.checked); this._save(); }} /> Enable Wi-Fi receiver</label>

      <label>Helper path <input style="width:100%" .value=${d.helperPath}
        placeholder="~/dev/radio/rtl8812au-helper/.venv/bin/msdr-wifi-capture"
        @input=${(e) => this._set("helperPath", e.target.value)} /></label>
      <p class="dim">Install the helper separately (see "Helper setup" in docs/wifi.md), then point this at its <code>msdr-wifi-capture</code> binary.</p>

      <label>Channel <select @change=${(e) => this._set("channel", e.target.value)}>
        <optgroup label="2.4 GHz">
          ${CHANNELS_24.map((c) => html`<option value=${c} ?selected=${Number(d.channel) === c}>${c}</option>`)}
        </optgroup>
        <optgroup label="5 GHz (20 MHz)">
          ${CHANNELS_5.map((c) => html`<option value=${c} ?selected=${Number(d.channel) === c}>${c}</option>`)}
        </optgroup>
      </select></label>

      <p>Detected device: ${device
        ? html`<span class="mono">${device.product ?? "802.11n NIC"} (bus ${device.bus}, addr ${device.address}${device.serial ? `, serial ${device.serial}` : ""})</span>`
        : html`<span class="dim">none detected</span>`}
        <button class="link" @click=${() => this._refreshDevices()}>rescan</button></p>

      <div class="control-row">
        <button ?disabled=${this._busy} @click=${() => this._save()}>SAVE WI-FI</button>
        <span class="dim">Start and stop capture in Discover → Wi-Fi.</span>
      </div>

      <p>Status: <strong>${s ? (STATE_LABELS[s.state] ?? s.state) : "loading…"}</strong>
        ${s ? html`· ${s.frames ?? 0} frames · ${s.frame_rate != null ? `${s.frame_rate.toFixed(1)} fps` : "-- fps"} · ${s.drops ?? 0} drops` : ""}</p>
      ${s?.channel != null ? html`<p class="dim">Channel ${s.channel}${s.frequency_mhz != null ? ` (${s.frequency_mhz} MHz)` : ""}
        ${s.last_frame_at != null ? `· last frame ${Math.max(0, Math.round(Date.now() / 1000 - s.last_frame_at))}s ago` : "· no frames yet"}</p>` : ""}
      ${s?.last_error ? html`<p role="alert">${s.last_error}</p>` : ""}
      ${s?.helper?.upstream_commit ? html`<p class="dim mono">helper ${s.helper.upstream_commit}</p>
        ${s.helper.host_tx_dma_disabled === true
          ? html`<p class="dim">Host transmit path: disabled</p>`
          : s.helper.host_tx_dma_disabled === false
            ? html`<p role="alert">Host transmit path: enabled</p>`
            : ""}
        ${s.helper.mac_tx_enabled === true
          ? html`<p class="dim">Radio MAC transmit: on — required for reception; it may answer frames addressed to this adapter (it never advertises its address)</p>`
          : ""}`
        : html`<p class="dim">Transmit state is reported once capture starts.</p>`}
      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      ${this._renderVendors()}
    </div>`;
  }
}

customElements.define("wifi-setup-section", WifiSetupSection);
