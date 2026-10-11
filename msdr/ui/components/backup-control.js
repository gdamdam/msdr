import { LitElement, html } from "../vendor/lit.js";

export class BackupControl extends LitElement {
  static properties = { media: {state: true}, busy: {state: true}, message: {state: true} };
  constructor() { super(); this.media = false; this.busy = false; this.message = ""; }
  createRenderRoot() { return this; }
  async _restore(event) {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file || !window.confirm("Replace the current library and all evidence history with this backup? Export a backup first to keep your current data.")) return;
    this.busy = true;
    this.message = "Restoring and verifying…";
    try {
      const response = await fetch("/api/backup/restore", {method: "POST", body: file,
        headers: {"Content-Type": "application/zip"}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Restore failed");
      this.message = result.verified ? "Restore verified. Reload to refresh all views." : "Restore completed; verification unavailable.";
    } catch (e) { this.message = e.message; }
    finally { this.busy = false; }
  }
  render() {
    return html`<details class="setup-section" id="setup-backup"><summary>Backup and restore</summary>
      <p>Full database backup includes observations, claims, decoder history, finds and band scans. Receiver settings are separate.</p>
      <label><input type="checkbox" .checked=${this.media} @change=${e => this.media = e.target.checked}> Include referenced media</label>
      <p><a href=${`/api/backup?include_media=${this.media}`} download="msdr-backup.zip">Download full backup</a></p>
      <label>Restore a backup <input aria-label="Restore backup" type="file" accept=".zip,application/zip"
        ?disabled=${this.busy} @change=${e => this._restore(e)}></label>
      <p class="dim">Stop capture and scanning before restore. Restore replaces the database and verifies its contents.</p>
      <p role="status">${this.message}</p>
      ${this.message.startsWith("Restore verified") ? html`<button @click=${() => location.reload()}>Reload views</button>` : ""}
    </details>`;
  }
}
customElements.define("backup-control", BackupControl);
