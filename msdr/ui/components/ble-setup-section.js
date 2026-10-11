import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

const STATUS_REFRESH_MS = 1000;

const STATE_LABELS = {
  dependency_missing: "dependency not installed",
  no_device: "no device detected",
  claimed: "dongle is in use by macOS",
  no_le: "controller has no LE support",
  firmware_mismatch: "firmware does not answer as Sniffle",
  wrong_baud: "no response at this baud rate",
  stopped: "stopped",
  starting: "starting…",
  receiving: "receiving",
  stale: "receiving (no reports recently)",
  failed: "failed",
};

/** Plain-language help under the status line for states that need the
 * person to act outside msdr (docs/ble.md "UI contract"). msdr never
 * flashes firmware or changes a baud rate automatically. */
export function stateExplanation(status) {
  if (!status) return null;
  if (status.state === "dependency_missing") {
    return status.backend === "sniffle"
      ? "pyserial not installed -- run pip install 'msdr[ble]'"
      : "pyusb not installed -- run pip install 'msdr[ble]'";
  }
  if (status.state === "firmware_mismatch") {
    return "Firmware does not answer as Sniffle -- flash the matching Sniffle build for this "
      + "dongle's bridge chip (CP2102 vs CP2102N); msdr never flashes firmware.";
  }
  if (status.state === "wrong_baud") {
    return "No response at the configured baud rate -- check CP2102 vs CP2102N and try the "
      + "other rate (auto tries both).";
  }
  if (status.state === "no_device") {
    return status.backend === "sniffle"
      ? "No CP210x serial port detected -- plug in the SONOFF dongle and rescan."
      : "No Bluetooth-class USB dongle detected.";
  }
  return null;
}

/** Why the START control is disabled right now, or null when it's fine to
 * press -- same reasoning as wifi-setup-section.js's startDisabledReason. */
export function startDisabledReason(status, enabled) {
  if (!enabled) return "Turn on “Enable BLE receiver” first";
  if (!status) return null;
  if (status.state === "dependency_missing") return stateExplanation(status);
  if (status.state === "no_device") return stateExplanation(status);
  if (status.state === "claimed") return "Dongle is in use by macOS";
  if (status.state === "no_le") return "Controller has no LE support";
  if (status.state === "firmware_mismatch") return "Firmware does not answer as Sniffle";
  if (status.state === "wrong_baud") return "No response at this baud rate";
  if (["starting", "receiving", "stale"].includes(status.state)) return "Already running";
  return null;
}

/** Self-contained like wifi-setup-section.js: polls its own status/devices
 * and emits `save-ble` for setup-drawer.js to PATCH, so this file never
 * touches the drawer's big draft/patch machinery. */
export class BleSetupSection extends LitElement {
  static properties = {
    settings: { type: Object },
    _draft: { state: true },
    _status: { state: true },
    _devices: { state: true },
    _devicesError: { state: true },
    _busy: { state: true },
    _error: { state: true },
    _names: { state: true },
    _namesBusy: { state: true },
    _namesError: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.settings = {};
    this._draft = {};
    this._status = null;
    this._devices = [];
    this._devicesError = null;
    this._busy = false;
    this._error = null;
    this._timer = null;
    this._names = null;
    this._namesBusy = false;
    this._namesError = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refreshStatus();
    this._refreshDevices();
    this._refreshNames();
    this._timer = setInterval(() => this._refreshStatus(), STATUS_REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    super.disconnectedCallback();
  }

  async _refreshNames() {
    try {
      this._names = await api.bleNames();
    } catch (err) {
      this._namesError = err.message;
    }
  }

  async _downloadNames() {
    this._namesBusy = true;
    this._namesError = null;
    try {
      this._names = await api.refreshBleNames();
    } catch (err) {
      this._namesError = err.message;
    } finally {
      this._namesBusy = false;
    }
  }

  _renderNames() {
    const n = this._names;
    const when = n?.fetched_at ? new Date(n.fetched_at * 1000).toLocaleDateString() : null;
    return html`<div class="ble-sig-names">
      <p>Bluetooth names: ${n?.downloaded
        ? html`${n.companies.toLocaleString()} companies, ${n.services.toLocaleString()} services,
            ${n.appearance.toLocaleString()} device types · downloaded ${when}`
        : html`<span class="dim">built-in short list only</span>`}
        <button ?disabled=${this._namesBusy} @click=${() => this._downloadNames()}>
          ${this._namesBusy ? "Downloading…" : n?.downloaded ? "Update" : "Download"}</button></p>
      <p class="dim">Downloads the Bluetooth SIG's company, service and device-type lists into
        your msdr data folder, for your own use under the SIG's terms; they are not shipped with msdr.</p>
      ${this._namesError ? html`<p role="alert">${this._namesError}</p>` : ""}
    </div>`;
  }

  willUpdate(changed) {
    if (changed.has("settings")) {
      this._draft = {
        enabled: !!this.settings?.enabled,
        backend: this.settings?.backend ?? "hci",
        usbId: this.settings?.usb_id ?? "",
        serialPort: this.settings?.serial_port ?? "",
        baud: this.settings?.baud ? String(this.settings.baud) : "",
        phy: this.settings?.phy ?? "1m",
        channel: this.settings?.channel ?? "37",
      };
    }
  }

  async _refreshStatus() {
    try {
      this._status = await api.getBleStatus();
      // Clear only a failure from this poll, not e.g. a settings-save error.
      if (this._pollFailed) { this._error = null; this._pollFailed = false; }
    } catch (err) {
      this._error = err.message;
      this._pollFailed = true;
    }
  }

  async _refreshDevices() {
    try {
      const resp = await api.getBleDevices();
      this._devices = resp?.devices ?? [];
      this._devicesError = resp?.error ?? null;
    } catch {
      // devices listing needs the helper installed; leave _devices = []
      // rather than surfacing this as a hard error alongside status.
      this._devices = [];
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
      backend: this._draft.backend,
      usb_id: this._draft.usbId || null,
      serial_port: this._draft.serialPort || null,
      baud: this._draft.baud ? Number.parseInt(this._draft.baud, 10) : null,
      phy: this._draft.phy,
      channel: this._draft.channel,
    };
    this.dispatchEvent(new CustomEvent("save-ble", { bubbles: true, composed: true, detail: { config } }));
  }

  saveComplete() {
    this._busy = false;
  }

  saveFailed(message) {
    this._busy = false;
    this._error = `Could not save BLE settings: ${message}`;
    this._pollFailed = false;
  }

  render() {
    const d = this._draft;
    const s = this._status;
    const running = s && ["starting", "receiving", "stale"].includes(s.state);
    const sniffle = d.backend === "sniffle";
    const explanation = stateExplanation(s);
    return html`<div class="setup-group ble-setup">
      <p class="setup-help">Listens passively to Bluetooth Low Energy advertising -- independent of SDRconnect and the rest of this receiver. msdr never scans actively, sends scan requests, connects, pairs, bonds, initiates or transmits, and never touches Bluetooth Classic.</p>

      <label class="integration-toggle"><input type="checkbox" .checked=${!!d.enabled}
        @change=${(e) => { this._set("enabled", e.target.checked); this._save(); }} /> Enable BLE receiver</label>

      <label>Backend
        <select .value=${d.backend} @change=${(e) => { this._set("backend", e.target.value); this._save(); }}>
          <option value="hci">USB HCI dongle (Bluetooth 4.x)</option>
          <option value="sniffle">SONOFF ZBDongle-P (Sniffle firmware)</option>
        </select>
      </label>

      ${sniffle
        ? html`
          <label>Serial port <input style="width:100%" .value=${d.serialPort}
            placeholder="/dev/cu.usbserial-... (empty = auto-detect CP210x)"
            @input=${(e) => this._set("serialPort", e.target.value)} /></label>

          <label>Baud
            <select .value=${d.baud} @change=${(e) => { this._set("baud", e.target.value); this._save(); }}>
              <option value="">auto (try 2000000, then 921600)</option>
              <option value="2000000">2000000 (CP2102N)</option>
              <option value="921600">921600 (CP2102, non-N)</option>
            </select>
          </label>

          <label>PHY
            <select .value=${d.phy} @change=${(e) => { this._set("phy", e.target.value); this._save(); }}>
              <option value="1m">1M</option>
              <option value="2m">2M</option>
              <option value="coded">Coded (long range)</option>
            </select>
          </label>

          <label>Channel
            <select .value=${d.channel} @change=${(e) => { this._set("channel", e.target.value); this._save(); }}>
              <option value="37">37</option>
              <option value="38">38</option>
              <option value="39">39</option>
              <option value="hop">hop 37/38/39</option>
            </select>
          </label>
          <p class="dim">A genuine SONOFF ZBDongle-P (CC2652P) with matching Sniffle firmware; msdr never flashes firmware. CP2102N bridges use 2000000 baud, CP2102 (non-N, 2022 shortage units) need 921600.</p>
        `
        : html`
          <label>USB id <input style="width:100%" .value=${d.usbId}
            placeholder="19ff:0239 (empty = auto-detect)"
            @input=${(e) => this._set("usbId", e.target.value)} /></label>
          <p class="dim">Optional <code>vid:pid</code> to pick a specific dongle when more than one is attached; empty auto-detects the first known Bluetooth-class device.</p>
        `}

      <p>Detected ${sniffle ? "serial ports" : "devices"}:</p>
      ${this._devices.length
        ? html`<ul class="ble-devices">${this._devices.map((dv) => sniffle
            ? html`<li class="mono">${dv.port}${dv.vid ? ` ${dv.vid}:${dv.pid}` : ""} ${dv.product ?? "unknown"}${dv.known ? "" : html` <span class="dim">(not CP210x)</span>`}${dv.serial ? `, serial ${dv.serial}` : ""}</li>`
            : html`<li class="mono">
            ${dv.vid}:${dv.pid} ${dv.product ?? "unknown"}${dv.known ? "" : html` <span class="dim">(unrecognized class)</span>`}
            -- bus ${dv.bus} addr ${dv.address}${dv.serial ? `, serial ${dv.serial}` : ""}</li>`)}</ul>`
        : html`<p class="dim">${this._devicesError ?? "none detected"}</p>`}
      <button class="link" @click=${() => this._refreshDevices()}>rescan</button>

      <div class="control-row">
        <button ?disabled=${this._busy} @click=${() => this._save()}>SAVE BLE</button>
        <span class="dim">Start and stop capture in Discover → BLE.</span>
      </div>

      <p>Status: <strong>${s ? (STATE_LABELS[s.state] ?? s.state) : "loading…"}</strong>
        ${s ? html`· ${s.advertiser_count ?? 0} advertisers` : ""}</p>
      ${explanation ? html`<p class="dim">${explanation}</p>` : ""}
      ${s?.last_error ? html`<p role="alert">${s.last_error}</p>` : ""}
      ${running && s?.controller
        ? html`<p class="dim">${sniffle
            ? html`Sniffle ${s.controller.firmware_version ?? "?"} · ${s.controller.baud ?? "?"} baud · ${(s.controller.phy ?? "1m").toUpperCase()} PHY · channel ${s.controller.channel ?? "37"}${s.controller.extended ? " · extended advertising" : ""}`
            : html`${s.controller.product ?? "controller"}${s.controller.manufacturer_name ? ` (${s.controller.manufacturer_name})` : ""}${s.controller.hci_version != null ? ` · HCI ${s.controller.hci_version}` : ""} · passive scan, no scan requests`}</p>`
        : html`<p class="dim">Controller facts are reported once capture starts.</p>`}
      ${running
        ? html`<p class="dim">Reception check: ${(s.reports ?? 0) > 0
            ? html`${s.reports} report${s.reports === 1 ? "" : "s"} received, ${s.report_rate ?? 0}/s.`
            : "no reports received yet."}</p>`
        : ""}
      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      ${this._renderNames()}
    </div>`;
  }
}

customElements.define("ble-setup-section", BleSetupSection);
