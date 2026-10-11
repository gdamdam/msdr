import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import "./led-dot.js";

// development notes Phase 1 "Station status strip": a compact, always-visible summary
// of the Wi-Fi and BLE receivers beside the existing SDRconnect strip. Self-
// contained like ble-setup-section.js/wifi-setup-section.js: polls its own
// status rather than depending on app.js state, so it adds no wiring to any
// file another agent may be editing concurrently. Reuses the strip's
// existing `.lamp-cluster`/`<led-dot>` styling (instrument-panel.js's LINK/
// ALERT lamps) instead of adding any CSS of its own, so it adds no height to
// the strip (tests/e2e/test_layout_gate.py's 100-220 px gate).
const REFRESH_MS = 1000;
// With both receivers disabled the strip shows nothing, so it only checks
// now and then whether one was enabled in Setup instead of every second.
const IDLE_REFRESH_MS = 15000;

/** Next poll delay: fast only while a receiver is enabled (its lamp shows
 * live state); slow otherwise. */
export function refreshDelay(wifi, ble) {
  return wifi?.enabled || ble?.enabled ? REFRESH_MS : IDLE_REFRESH_MS;
}
const STALE_COLOR_STATES = new Set(["starting", "stale"]);
const PROBLEM_STATES = new Set([
  "dependency_missing",
  "no_device",
  "claimed",
  "no_le",
  "firmware_mismatch",
  "wrong_baud",
  "failed",
]);

/** `{color, lit}` for the led-dot, from a receiver status's `state`. */
export function lampFor(state) {
  if (state === "receiving") return { color: "var(--ok)", lit: true };
  if (PROBLEM_STATES.has(state)) return { color: "var(--danger)", lit: true };
  if (STALE_COLOR_STATES.has(state)) return { color: "var(--accent)", lit: true };
  return { color: null, lit: false }; // stopped, or no status yet
}

/** Seconds-since-last-packet, in words; null (never seen one) renders as
 * "no packets yet" by the caller. */
export function sinceLabel(lastAtS, nowS = Date.now() / 1000) {
  if (lastAtS == null) return null;
  const dt = Math.max(0, nowS - lastAtS);
  if (dt < 1) return "<1s ago";
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  return `${Math.round(dt / 3600)}h ago`;
}

/** Compact "chan/PHY" label: Wi-Fi has a channel number (the tuned one, or
 * the configured one while stopped -- same as the BLE chip, which names its
 * channel whether or not it is running), BLE (sniffle) has PHY+channel, BLE
 * (hci) has neither (controller-scheduled, docs/ble.md). */
export function channelLabel(kind, status) {
  if (kind === "wifi") {
    const ch = status.channel ?? status.configured_channel;
    return ch != null ? `ch${ch}` : "";
  }
  if (status.backend === "sniffle") {
    const phy = (status.phy ?? "1m").toUpperCase();
    return status.channel === "hop" ? `${phy} hop` : `${phy} ch${status.channel ?? "37"}`;
  }
  return "";
}

/** Station status strip (development notes Phase 1): one compact lamp per *enabled*
 * receiver (Wi-Fi, BLE) -- a disabled receiver is hidden entirely, same as
 * the task's "hide receivers that are disabled". Each lamp's tooltip
 * (`data-help`, same attribute the LINK/ALERT lamps use) carries the state
 * word, channel/PHY and packet rate; the visible label stays to one or two
 * words so this never wraps the strip to a second line. */
export class ReceiverStatusStrip extends LitElement {
  static properties = {
    _wifi: { state: true },
    _ble: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this._wifi = null;
    this._ble = null;
    this._timer = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._onVisibility = () => { if (!document.hidden) this._refresh(); };
    document.addEventListener("visibilitychange", this._onVisibility);
    this._refresh();
  }

  disconnectedCallback() {
    clearTimeout(this._timer);
    this._timer = null;
    document.removeEventListener("visibilitychange", this._onVisibility);
    super.disconnectedCallback();
  }

  async _refresh() {
    clearTimeout(this._timer);
    this._timer = null;
    // A hidden tab doesn't poll; visibilitychange restarts the loop.
    if (document.hidden || !this.isConnected) return;
    const [wifi, ble] = await Promise.allSettled([api.getWifiStatus(), api.getBleStatus()]);
    this._wifi = wifi.status === "fulfilled" ? wifi.value : null;
    this._ble = ble.status === "fulfilled" ? ble.value : null;
    if (this.isConnected && this._timer === null) {
      this._timer = setTimeout(() => this._refresh(), refreshDelay(this._wifi, this._ble));
    }
  }

  _lamp(kind, status) {
    if (!status?.enabled) return "";
    const lamp = lampFor(status.state);
    const rate = kind === "wifi" ? status.frame_rate : status.report_rate;
    const lastAt = kind === "wifi" ? status.last_frame_at : status.last_report_at;
    const chan = channelLabel(kind, status);
    const help = `${kind === "wifi" ? "Wi-Fi" : "BLE"}: ${status.state}`
      + (chan ? `, ${chan}` : "")
      + `, ${(rate ?? 0).toFixed(1)}/s, last packet ${sinceLabel(lastAt) ?? "never"}`;
    const open = () => this.dispatchEvent(new CustomEvent("open-discover", { detail: { view: kind }, bubbles: true }));
    return html`<div class="lamp-cluster" role="button" tabindex="0" data-help=${help}
      aria-label="Open ${kind === "wifi" ? "Wi-Fi" : "BLE"} in Discover"
      @click=${open}
      @keydown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <led-dot .color=${lamp.color} .lit=${lamp.lit}></led-dot>
      <span class="label">${kind === "wifi" ? "WIFI" : "BLE"}${chan ? ` ${chan}` : ""}</span>
    </div>`;
  }

  render() {
    const wifi = this._lamp("wifi", this._wifi);
    const ble = this._lamp("ble", this._ble);
    if (!wifi && !ble) return "";
    return html`${wifi}${ble}`;
  }
}

customElements.define("receiver-status-strip", ReceiverStatusStrip);
