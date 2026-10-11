import { LitElement, html } from "../vendor/lit.js";
import * as api from "../api.js";
import "./led-dot.js";

const DISCONNECTED_HINT = "Enable SDRconnect ▸ Preferences ▸ WebSocket Server (port 5454)";

/** Auto-shown whenever any of the four checks below isn't green -- the
 * instrument panel's LINK lamp alone doesn't say *why* it's unlit. The LINK
 * label (instrument-panel.js) opens it via `open` when
 * everything's fine, to check in on a healthy link. Retry/Test act on
 * /api/connection/retry and /api/state directly (not through app.js's
 * action/status bus) since their result is meant to be read right here next
 * to the checklist, not flashed in the footer. */
export class DiagBanner extends LitElement {
  static properties = {
    state: { type: Object },
    wsDown: { type: Boolean },
    open: { type: Boolean },
    _busy: { state: true },
    _test: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.state = null;
    this.wsDown = false;
    this.open = false;
    this._busy = false;
    this._test = null;
  }

  get _checks() {
    const s = this.state;
    const connected = !!s?.connected;
    const started = s?.started ?? true;
    const canControl = !!s?.can_control;
    const synced = s?.synced ?? true; // missing (older backend) = treat as in sync
    const nextRetry = s?.next_retry_s;
    const decoder = s?.decoder;
    // Missing entirely (feature off, or the decoder package isn't installed
    // server-side) -- omit the check rather than show a permanently-red row
    // for something the operator hasn't turned on.
    const decoderCheck = decoder
      ? (() => {
          const anyBinary = Object.values(decoder.binaries ?? {}).some((v) => v != null);
          return [
            {
              label: "DECODERS",
              ok: anyBinary,
              hint: anyBinary ? "" : "multimon-ng / minimodem not found — see DECODE ⚙ for install steps",
            },
          ];
        })()
      : [];
    return [
      {
        label: "MSDR SERVER",
        ok: !this.wsDown,
        hint: this.wsDown ? "no socket to msdr — retrying…" : "",
      },
      {
        label: "SDRCONNECT LINK",
        ok: connected,
        hint: connected ? "" : nextRetry != null ? "reconnecting… (Retry to try now)" : DISCONNECTED_HINT,
      },
      {
        label: "RECEIVER RUNNING",
        ok: connected && started,
        hint: connected && !started ? "press Play in SDRconnect" : "",
      },
      {
        label: "CONTROL & SYNC",
        ok: connected && canControl && synced,
        hint: !connected
          ? ""
          : !canControl
            ? "read-only in SDRconnect"
            : !synced
              ? "not yet in sync"
              : "",
      },
      ...decoderCheck,
    ].map((c, i) =>
      // Without the msdr socket the receiver rows are only the last known
      // state, so show them unlit ("unknown") rather than a stale green.
      i > 0 && this.wsDown ? { ...c, ok: false, unknown: true, hint: "unknown while msdr is unreachable" } : c
    );
  }

  get _allGreen() {
    return this._checks.every((c) => c.ok);
  }

  async _retry() {
    this._busy = true;
    try {
      await api.connectionRetry();
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "retry requested" } }));
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._busy = false;
    }
  }

  async _runTest() {
    this._busy = true;
    try {
      const s = await api.getState();
      this._test = { ok: true, message: `state ok — connected=${!!s.connected} synced=${s.synced ?? true}` };
    } catch (err) {
      this._test = { ok: false, message: err.message };
    } finally {
      this._busy = false;
    }
  }

  /** A modal, opened on demand (LINK lamp, or the "⚠ LINK" button the
   * instrument panel shows only while a check fails) -- never rendered inline,
   * so a healthy-looking page isn't pushed down by a panel nobody asked for. */
  render() {
    if (!this.open) return html``;
    const checks = this._checks;
    const close = () => this.dispatchEvent(new CustomEvent("close"));
    return html`
      <div class="overlay-backdrop" @click=${close}>
      <div
        class="panel"
        id="diag-banner"
        role="dialog"
        aria-modal="true"
        aria-labelledby="diag-title"
        style="margin:80px auto 0;width:min(520px,90vw);align-self:flex-start"
        @click=${(e) => e.stopPropagation()}
      >
        <div style="display:flex;align-items:center">
          <h2 id="diag-title" style="flex:1">Diagnostics</h2>
          <button title="close" aria-label="close diagnostics" @click=${close}>&times;</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px">
          ${checks.map(
            (c) => html`
              <div class="row" style="cursor:default">
                <led-dot .color=${c.ok ? "var(--ok)" : "var(--danger)"} .lit=${!c.unknown}></led-dot>
                <span class="label" style="flex:none">${c.label}</span>
                ${c.hint ? html`<span class="dim mono" style="font-size:0.85em">${c.hint}</span>` : ""}
              </div>
            `
          )}
        </div>
        <div class="control-row" style="margin-top:10px">
          <button ?disabled=${this._busy} @click=${() => this._retry()}>RETRY</button>
          <button ?disabled=${this._busy} @click=${() => this._runTest()}>TEST</button>
        </div>
        ${this._test
          ? html`<p class="${this._test.ok ? "dim" : "status-error"} mono" style="margin:8px 0 0;font-size:0.85em">
              ${this._test.message}
            </p>`
          : ""}
      </div>
      </div>
    `;
  }
}

customElements.define("diag-banner", DiagBanner);
