import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import { profileSummary } from "../profile-summary.js";
import { containModalFocus, restoreModalFocus, trapModalTab } from "./modal-focus.js";

/** Shared Profiles list (name, summary, open/Edit, Apply, Delete) used by the
 * Library section and the receiver strip's Profiles modal. Events bubble so
 * the host's existing open-profile / apply-profile / refresh / api-error
 * handlers keep working. */
export class ProfileList extends LitElement {
  static properties = {
    profiles: { type: Array },
    activeId: { type: String },
    disabled: { type: Boolean }, // receiver can't be controlled -> Apply off
    openLabel: { type: String },
    emptyText: { type: String },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.profiles = [];
    this.activeId = null;
    this.disabled = false;
    this.openLabel = "Open";
    this.emptyText = "No profiles yet.";
  }

  _fire(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  async _delete(p) {
    if (!window.confirm(`Delete profile "${p.name}"?`)) return;
    try {
      await api.deleteProfile(p.id);
      this._fire("refresh");
    } catch (err) {
      this._fire("api-error", { message: err.message });
    }
  }

  render() {
    if (!this.profiles?.length) return html`<p class="empty">${this.emptyText}</p>`;
    return html`<ul class="profile-list" style="list-style:none;margin:0;padding:0">
      ${this.profiles.map(
        (p) => html`<li style="display:flex;gap:8px;align-items:center;padding:4px 0">
          <span style="flex:1;min-width:0">
            <strong>${p.id === this.activeId ? "✓ " : ""}${p.name}</strong>
            <span class="dim" style="margin-left:8px">${profileSummary(p)}</span>
          </span>
          <button @click=${() => this._fire("open-profile", { id: p.id })}>${this.openLabel}</button>
          <button
            ?disabled=${this.disabled}
            title=${this.disabled ? "Receiver can't be controlled right now" : `Apply ${p.name}`}
            @click=${() => this._fire("apply-profile", { id: p.id })}
          >Apply</button>
          <button style="color:var(--danger)" @click=${() => this._delete(p)}>Delete</button>
        </li>`
      )}
    </ul>`;
  }
}
customElements.define("profile-list", ProfileList);

/** Modal over the current workspace (same overlay-backdrop / role=dialog
 * pattern as the scanner setup modal; focus handling as in setup-drawer). */
export class ProfilesModal extends LitElement {
  static properties = {
    open: { type: Boolean },
    profiles: { type: Array },
    activeId: { type: String },
    disabled: { type: Boolean },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.profiles = [];
    this.activeId = null;
    this.disabled = false;
    this._returnFocus = null;
    this._focusInHandler = (ev) =>
      containModalFocus(ev, this.querySelector('[role="dialog"]'), this.querySelector(".profiles-close"));
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  _fire(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  updated(changed) {
    if (!changed.has("open")) return;
    if (this.open) {
      this._returnFocus = this.ownerDocument.activeElement;
      this.ownerDocument.addEventListener("focusin", this._focusInHandler, true);
      queueMicrotask(() => this.querySelector(".profiles-close")?.focus());
    } else {
      this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
      restoreModalFocus(this._returnFocus);
      this._returnFocus = null;
    }
  }

  disconnectedCallback() {
    this.ownerDocument.removeEventListener("focusin", this._focusInHandler, true);
    super.disconnectedCallback();
  }

  _onKeydown(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopPropagation();
      this._close();
      return;
    }
    trapModalTab(ev, this.querySelector('[role="dialog"]'));
  }

  render() {
    if (!this.open) return "";
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel"
          id="profiles-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="profiles-modal-title"
          style="margin:60px auto 0;width:min(720px,92vw);max-height:80vh;overflow:auto;align-self:flex-start"
          @click=${(e) => e.stopPropagation()}
          @keydown=${(ev) => this._onKeydown(ev)}
        >
          <div style="display:flex;align-items:center">
            <h2 id="profiles-modal-title" style="flex:1">Profiles</h2>
            <button class="profiles-close" title="close" aria-label="close profiles" @click=${() => this._close()}>&times;</button>
          </div>
          <profile-list
            .profiles=${this.profiles}
            .activeId=${this.activeId}
            .disabled=${this.disabled}
            openLabel="Edit"
            emptyText="No saved profiles yet"
          ></profile-list>
          <div style="margin-top:8px">
            <button @click=${() => this._fire("new-profile", { fromCurrent: true })}>Save current settings as profile…</button>
          </div>
        </div>
      </div>
    `;
  }
}
customElements.define("profiles-modal", ProfilesModal);
