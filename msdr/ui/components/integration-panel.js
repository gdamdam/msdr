import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";

const REFRESH_MS = 5000;

/** Opt-in receive/status links to separately-running digital-mode apps. */
export class IntegrationPanel extends LitElement {
  static properties = {
    settings: { type: Object },
    _draft: { state: true },
    _status: { state: true },
    _messages: { state: true },
    _busy: { state: true },
    _error: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.settings = {};
    this._draft = {};
    this._status = {};
    this._messages = [];
    this._busy = false;
    this._error = null;
    this._timer = null;
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

  willUpdate(changed) {
    if (changed.has("settings")) {
      this._draft = structuredClone(this.settings ?? {});
    }
  }

  async _refresh() {
    try {
      const [status, messages] = await Promise.all([
        api.getIntegrationStatus(),
        api.listIntegrationMessages(100),
      ]);
      this._status = status;
      this._messages = messages;
      this._error = null;
    } catch (err) {
      this._error = err.message;
    }
  }

  _set(source, key, value) {
    this._draft = {
      ...this._draft,
      [source]: { ...(this._draft[source] ?? {}), [key]: value },
    };
  }

  async _save(source) {
    this._busy = true;
    this._error = null;
    const config = this._draft[source] ?? {};
    this.dispatchEvent(new CustomEvent("save-integration", {
      bubbles: true,
      composed: true,
      detail: { source, config },
    }));
  }

  _connection(source) {
    const status = this._status[source] ?? {};
    if (!status.enabled) return "off";
    if (status.connected) return "connected";
    return status.error || "connecting";
  }

  saveComplete() {
    this._busy = false;
    this._refresh();
  }

  saveFailed(message) {
    this._busy = false;
    this._error = message;
  }

  render() {
    const js8 = this._draft.js8call ?? {};
    const pat = this._draft.pat ?? {};
    const freedata = this._draft.freedata ?? {};
    return html`<div class="setup-group integration-group">
      <p class="setup-help">These links read decoded receive data and mailbox status from applications already running on this computer. They do not transmit, compose, mark mail read, or modify the external apps.</p>
      ${this._applicationCard("JS8Call", "js8call", this._connection("js8call"), html`
        <label class="integration-toggle"><input type="checkbox" .checked=${!!js8.enabled}
          @change=${e => this._set("js8call", "enabled", e.target.checked)} /> Enable receive feed</label>
        <div class="integration-fields">
          <label>Host <input .value=${js8.host ?? "127.0.0.1"}
            @input=${e => this._set("js8call", "host", e.target.value)} /></label>
          <label>TCP port <input type="number" min="1" max="65535" .value=${js8.port ?? 2242}
            @input=${e => this._set("js8call", "port", Number(e.target.value))} /></label>
        </div>
        <p class="dim">Enable JS8Call’s Network Message API (TCP) in its Reporting settings. The default API port is 2242.</p>
        <button ?disabled=${this._busy} @click=${() => this._save("js8call")}>SAVE JS8CALL</button>
      `)}
      ${this._applicationCard("Pat / Winlink", "pat", this._connection("pat"), html`
        <label class="integration-toggle"><input type="checkbox" .checked=${!!pat.enabled}
          @change=${e => this._set("pat", "enabled", e.target.checked)} /> Enable mailbox feed</label>
        <label>Pat HTTP URL <input class="integration-url" .value=${pat.base_url ?? "http://127.0.0.1:8080"}
          @input=${e => this._set("pat", "base_url", e.target.value)} /></label>
        <p class="dim">Start Pat’s HTTP interface with <code>pat http</code>. Mailbox reads are view-only.</p>
        <button ?disabled=${this._busy} @click=${() => this._save("pat")}>SAVE PAT</button>
      `)}
      ${this._applicationCard("FreeDATA", "freedata", this._connection("freedata"), html`
        <label class="integration-toggle"><input type="checkbox" .checked=${!!freedata.enabled}
          @change=${e => this._set("freedata", "enabled", e.target.checked)} /> Enable receive feed</label>
        <label>FreeDATA server URL <input class="integration-url" .value=${freedata.base_url ?? "http://127.0.0.1:5000"}
          @input=${e => this._set("freedata", "base_url", e.target.value)} /></label>
        <p class="dim">Start FreeDATA’s server. Received text, file names and modem status appear here; open FreeDATA to use attachments or send.</p>
        <button ?disabled=${this._busy} @click=${() => this._save("freedata")}>SAVE FREEDATA</button>
      `)}
      ${this._error ? html`<p class="integration-error">${this._error}</p>` : ""}
      <h3>Received messages</h3>
      ${this._messages.length ? html`<ol class="integration-messages">
        ${this._messages.map(message => html`<li>
          <div class="integration-message-head">
            <strong>${message.from || "Unknown sender"}</strong>
            <span>${message.protocol} · ${message.received_at ?? "time unavailable"}</span>
          </div>
          <div class="dim">
            ${message.to ? html`To ${Array.isArray(message.to) ? message.to.join(", ") : message.to} · ` : ""}
            ${message.frequency_hz != null ? html`${(message.frequency_hz / 1e6).toFixed(6)} MHz · ` : ""}
            ${message.snr_db != null ? html`SNR ${message.snr_db} dB · ` : ""}
            ${message.grid ? html`Grid ${message.grid}` : ""}
          </div>
          ${message.subject ? html`<strong>${message.subject}</strong>` : ""}
          <p>${message.text || "Message received; open the source app to read its contents."}</p>
          ${message.attachment_count ? html`<div class="dim">${message.attachment_count} file${message.attachment_count === 1 ? "" : "s"}${message.attachment_names?.length ? html`: ${message.attachment_names.join(", ")}` : ""} · open the source app to access files</div>` : ""}
          ${message.source === "pat" ? html`<a href=${message.pat_message_url} target="_blank" rel="noopener">Open in Pat</a>` : ""}
          <small>${message.provenance}</small>
        </li>`)}
      </ol>` : html`<p class="dim">No received messages yet. JS8Call events appear as they arrive; Pat and FreeDATA inboxes appear after their first successful check.</p>`}
    </div>`;
  }

  _applicationCard(name, source, connection, contents) {
    const status = this._status[source] ?? {};
    return html`<article class="integration-card">
      <div class="integration-card-head"><h3>${name}</h3>
        <span class="integration-state ${status.connected ? "is-connected" : ""}">${connection}</span></div>
      ${source === "pat" && status.connected
        ? html`<p class="dim">Inbox: ${status.inbox_count ?? 0} · unread: ${status.unread_count ?? 0}</p>`
        : ""}
      ${source === "freedata" && status.connected
        ? html`<p class="dim">Inbox: ${status.inbox_count ?? 0} · unread: ${status.unread_count ?? 0} · modem: ${status.modem_running ? "running" : "stopped"} · radio: ${status.radio_connected ? "connected" : "offline"}${status.radio_frequency_hz ? html` · ${(status.radio_frequency_hz / 1e6).toFixed(6)} MHz` : ""}</p>`
        : ""}
      ${contents}
    </article>`;
  }
}

customElements.define("integration-panel", IntegrationPanel);
