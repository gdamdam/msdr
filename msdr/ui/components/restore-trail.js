import { LitElement, html } from "../vendor/lit.js";
import "./led-dot.js";

// Step status -> LED color (or null for "not lit yet"/pending).
const STEP_COLOR = {
  pending: null,
  confirmed: "var(--ok)",
  unverified: "var(--warn, #f1b951)",
  failed: "var(--danger)",
  skipped: "var(--ink)",
};

/** The final status line for a finished restore (brief examples: "confirmed",
 * "partial: lna_state", "interrupted by external change", "failed at device_center_frequency"). */
function summarize(report, steps) {
  if (!report) return "";
  if (report.status === "confirmed") return "confirmed";
  if (report.status === "interrupted") return "interrupted by external change";
  const failedNames = steps
    .filter((o) => o.status === "failed" || o.status === "unverified")
    .map((o) => o.property ?? o.name);
  if (report.status === "failed") return `failed at ${failedNames[0] ?? "?"}`;
  return `partial: ${failedNames.join(", ")}`;
}

export class RestoreTrail extends LitElement {
  static properties = { restore: { type: Object } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.restore = { steps: [], report: null };
  }

  render() {
    const steps = this.restore?.steps ?? [];
    const report = this.restore?.report ?? null;
    if (!steps.length) return html``;
    // Non-confirmed stays pinned + highlighted until dismissed or the next
    // restore starts (see app.js's _onRestoreDone/_onRestoreStep); confirmed
    // still auto-clears on its own timer.
    const pinned = !!report && report.status !== "confirmed";

    // A debugging aid, collapsed at the bottom of the page: a routine
    // confirmed tune shouldn't push content around. A failed or partial
    // restore opens it so the problem is still seen.
    return html`
      <details
        class="panel"
        id="restore-trail"
        ?open=${pinned}
        style="margin-top:12px${pinned ? ";border-color:var(--danger)" : ""}"
      >
        <summary style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <span class="dim mono" style="flex:1;font-size:0.85em">Debug · tune sequence</span>
          ${report
            ? html`<button
                title="dismiss"
                style="font-size:0.75em;padding:1px 6px"
                @click=${() => this.dispatchEvent(new CustomEvent("dismiss"))}
              >
                ×
              </button>`
            : ""}
        </summary>
        ${steps.length
          ? html`<div style="display:flex;flex-direction:column;gap:4px">
              ${steps.map(
                (o) => html`
                  <div class="mono" style="font-size:0.9em;display:flex;align-items:center;gap:8px">
                    <led-dot .color=${STEP_COLOR[o.status]} .lit=${!!STEP_COLOR[o.status]}></led-dot>
                    <span>${o.property ?? o.name}</span>
                    ${o.value !== undefined ? html`<span class="dim">-> ${o.value}</span>` : ""}
                    ${o.detail ? html`<span class="dim">(${o.detail})</span>` : ""}
                  </div>
                `
              )}
            </div>`
          : ""}
        ${report
          ? html`<p class="dim" style="margin:8px 0 0">${summarize(report, steps)}</p>`
          : ""}
        <!-- Set when the final desired-vs-confirmed check found a property
             that never made it, which can happen with no failed step of its
             own (a native profile moved it back). -->
        ${report?.detail
          ? html`<p class="dim mono" style="margin:2px 0 0;font-size:0.85em">${report.detail}</p>`
          : ""}
      </details>
    `;
  }
}

customElements.define("restore-trail", RestoreTrail);
