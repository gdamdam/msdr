import { LitElement, html } from "../vendor/lit.js";

// docs/allocations.md "Lookup result" / "Source status" is the contract this
// component renders against; the backend (msdr/allocations/*) is built in
// parallel, so every shape here is a fixture object until it lands.

/** Status-specific label shown above the lookup's verbatim `message` --
 * every non-"ok" status gets one of these, never a generic "unavailable". */
const STATUS_LABELS = {
  not_covered: "Not covered by loaded data",
  unsupported_jurisdiction: "Jurisdiction not supported",
  source_unavailable: "Allocation source unavailable",
  unallocated: "Explicitly unallocated",
};

/** {label, message} for a non-"ok" lookup result, or null when `status` is
 * "ok" (or missing/unrecognized -- callers then fall through to the normal
 * render). Falls back to the raw status string so an unrecognized value
 * still shows something rather than nothing. */
export function allocationStatusInfo(d) {
  if (!d || d.status === "ok" || d.status == null) return null;
  return { label: STATUS_LABELS[d.status] ?? d.status, message: d.message ?? "" };
}

/** "MOBILE except aeronautical mobile" -- a service's display text, preferring
 * the table's own raw cell text (keeps whatever capitalization/wording the
 * source used) over reconstructing name+qualifier. */
function serviceText(s) {
  return s?.raw || [s?.name, s?.qualifier].filter(Boolean).join(" ");
}

/** De-duplicated (by display text) services, first occurrence wins -- the
 * same service can legitimately appear in both the federal and non-federal
 * columns (docs/allocations.md "shared"), and this is a text summary, not a
 * column-by-column table. */
function dedupeServices(services) {
  const seen = new Set();
  const out = [];
  for (const s of services ?? []) {
    const text = serviceText(s);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push({ ...s, text });
  }
  return out;
}

function nationalServices(d) {
  if (d.national) return [...(d.national.federal?.services ?? []), ...(d.national.non_federal?.services ?? [])];
  return d.international?.services ?? [];
}

/** "5.250–5.275 MHz" -- a 3-decimal band-edge pair, distinct from format.js's
 * mhz() (6-decimal, single frequency) since this always describes a range. */
function mhzRange(loHz, hiHz) {
  const f = (hz) => (Number(hz) / 1_000_000).toFixed(3);
  return `${f(loHz)}–${f(hiHz)} MHz`;
}

/** All services for an "ok" lookup result, primary first, secondary marked --
 * e.g. "FIXED, MOBILE except aeronautical mobile · Radiolocation (secondary)".
 * Never truncates: this is the full list, every time. */
export function servicesLine(d) {
  const services = dedupeServices(nationalServices(d));
  const primary = services.filter((s) => s.priority !== "secondary").map((s) => s.text);
  const secondary = services.filter((s) => s.priority === "secondary").map((s) => `${s.text} (secondary)`);
  const parts = [];
  if (primary.length) parts.push(primary.join(", "));
  if (secondary.length) parts.push(secondary.join(", "));
  return parts.join(" · ") || "No services listed";
}

/** The full compact-line text (never truncated in the DOM -- see render()'s
 * use of CSS clamping plus this same string as the title attribute). */
export function compactText(d) {
  return `${mhzRange(d.lo_hz, d.hi_hz)} · ${d.jurisdiction} · ${servicesLine(d)}`;
}

/** `<allocation-summary .data=${lookupResult} ?compact=${...}>` -- renders a
 * docs/allocations.md "Lookup result" object. Light DOM (createRenderRoot
 * returns `this`, matching every other component here) so it picks up the
 * app's existing .chip/.dim/.empty/.row styling from styles.css without its
 * own stylesheet. */
export class AllocationSummary extends LitElement {
  static properties = {
    data: { type: Object },
    compact: { type: Boolean },
    // Compact strip beside a pill that already shows compactText(): render
    // only the Details / Open chart actions (and the stale chip) so the
    // allocation line isn't printed twice. The full line moves to the
    // Details button's title; the modal is unchanged.
    actionsOnly: { type: Boolean },
    // Render just the details body (no dialog) for hosting in <band-popover>.
    detailsOnly: { type: Boolean },
    _expanded: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.data = null;
    this.compact = false;
    this.actionsOnly = false;
    this.detailsOnly = false;
    this._expanded = false;
  }

  _toggle() {
    this._expanded = !this._expanded;
  }

  /** Keep the native <dialog> in step with `_expanded`: showModal() gives us
   * the backdrop, Esc handling and inert page for free. */
  updated() {
    const dialog = this.querySelector("dialog.allocation-modal");
    if (!dialog) return;
    if (this._expanded && !dialog.open) {
      this._returnFocus = document.activeElement;
      dialog.showModal();
    } else if (!this._expanded && dialog.open) {
      dialog.close();
      if (this._returnFocus?.isConnected) this._returnFocus.focus();
    }
  }

  _openChart() {
    this._expanded = false;
    // Must bubble out of the light-DOM tree to the window listener in app.js.
    this.dispatchEvent(new CustomEvent("open-allocation-chart", {
      detail: { hz: this.data?.frequency_hz }, bubbles: true, composed: true,
    }));
  }

  _priorityChip(s) {
    const secondary = s.priority === "secondary";
    return html`<span class="chip allocation-priority${secondary ? " allocation-priority--secondary" : ""}"
      title=${secondary ? "Secondary: must not interfere with, and cannot claim protection from, primary services" : "Primary service"}
    >${s.priority ?? "--"}</span>`;
  }

  _footnoteRefs(ids) {
    if (!ids?.length) return "";
    return html`<span class="allocation-refs">${ids.map((id) => html`<a href=${`#fn-${id}`} class="allocation-ref">${id}</a>`)}</span>`;
  }

  /** One national column (Federal / Non-Federal) as a card: service rows with
   * a priority chip and footnote references, then rule parts. */
  _renderServiceColumn(label, block) {
    if (!block) return "";
    const services = dedupeServices(block.services);
    return html`
      <section class="allocation-column">
        <h4>${label}</h4>
        ${services.length
          ? html`<ul class="allocation-services">
              ${services.map((s) => html`<li>${this._priorityChip(s)}<span class="allocation-service">${s.text}</span>${this._footnoteRefs(s.footnotes)}</li>`)}
            </ul>`
          : html`<p class="empty">No services listed</p>`}
        ${block.rule_parts?.length ? html`<p class="allocation-meta"><span class="dim">Rule parts</span> ${block.rule_parts.join(", ")}</p>` : ""}
        ${block.footnotes?.length ? html`<p class="allocation-meta"><span class="dim">Band footnotes</span> ${this._footnoteRefs(block.footnotes)}</p>` : ""}
      </section>
    `;
  }

  _renderInternational(d) {
    if (!d.international) return "";
    const services = dedupeServices(d.international.services);
    const region = (d.international.region ?? "ITU2").replace("ITU", "ITU Region ");
    return html`
      <section class="allocation-section">
        <h4>International context (ITU Region 2 table as reproduced in the US table)</h4>
        <p class="dim allocation-hint">${region} column, Radio Regulations 2016 edition as reproduced in the US table. National rules above take precedence.</p>
        ${services.length
          ? html`<ul class="allocation-services allocation-services--inline">
              ${services.map((s) => html`<li>${this._priorityChip(s)}<span class="allocation-service">${s.text}</span>${this._footnoteRefs(s.footnotes)}</li>`)}
            </ul>`
          : html`<p class="empty">No services listed</p>`}
        ${d.international.footnotes?.length ? html`<p class="allocation-meta"><span class="dim">Band footnotes</span> ${this._footnoteRefs(d.international.footnotes)}</p>` : ""}
      </section>
    `;
  }

  /** Footnotes as an id / text grid. Long texts start folded behind a native
   * <details> so the list stays scannable; short ones show in full. */
  _renderFootnotes(d) {
    if (!d.footnotes?.length) return "";
    const FOLD = 220;
    return html`
      <section class="allocation-section">
        <h4>Footnotes <span class="dim">(${d.footnotes.length})</span></h4>
        <dl class="allocation-footnotes">
          ${d.footnotes.map((f) => {
            const text = f.text ?? "";
            const long = text.length > FOLD;
            const flags = [f.fallback ? "NTIA text" : "", f.missing ? "text unavailable" : ""].filter(Boolean);
            return html`
              <dt id=${`fn-${f.id}`}><strong>${f.id}</strong>${flags.length ? html`<small class="dim">${flags.join(" · ")}</small>` : ""}</dt>
              <dd>${f.missing && !text
                ? html`<span class="dim">Referenced by the table but not present in the loaded sources.</span>`
                : long
                ? html`<details class="allocation-fold"><summary>${text.slice(0, FOLD).trimEnd()}…</summary><p>${text}</p></details>`
                : text}</dd>`;
          })}
        </dl>
      </section>
    `;
  }

  _renderCitations(d) {
    if (!d.citations?.length) return "";
    return html`
      <section class="allocation-section allocation-sources">
        <h4>Sources</h4>
        <ul class="allocation-citations">
          ${d.citations.map((c) => html`<li>
            ${c.url ? html`<a href=${c.url} target="_blank" rel="noopener noreferrer">${c.title}</a>` : c.title}
            ${c.edition ? html`<span class="dim"> · ${c.edition}</span>` : ""}
            ${c.effective_date ? html`<span class="dim"> · effective ${c.effective_date}</span>` : ""}
          </li>`)}
        </ul>
      </section>
    `;
  }

  _renderDisagreements(d) {
    if (!d.disagreements?.length) return "";
    const ids = d.disagreements.map((x) => x.id);
    return html`
      <section class="allocation-section allocation-disagreements">
        <details class="allocation-fold">
          <summary><strong>${d.disagreements.length}</strong> wording difference${d.disagreements.length === 1 ? "" : "s"} between sources</summary>
          <ul class="allocation-citations">
            ${d.disagreements.map((x) => html`<li><strong>${x.id}</strong> <span class="dim">${x.kind} · ${x.sources.join(" vs ")}</span></li>`)}
          </ul>
        </details>
        <span hidden>${ids.join(" ")}</span>
      </section>
    `;
  }

  /** Modal body: title, line, columns, footnotes, sources, "Open chart".
   * `close` null (details-only mode) drops the x / Close buttons -- the host
   * <band-popover> has its own. */
  _renderDetails(d, line, close) {
    const range = `${formatBandEdge(d.lo_hz)}–${formatBandEdge(d.hi_hz)}`;
    const tables = d.national ? "Federal and Non-Federal tables" : `${(d.jurisdiction ?? "").replace("ITU", "ITU Region ")} table`;
    return html`
          <header class="allocation-modal-head">
            <div>
              <h2 id="allocation-modal-title" class="mono">${range}</h2>
              <p class="dim">${d.jurisdiction ?? "US"} · ${tables}${d.sources?.stale ? html` · <span class="chip" title="Allocation data may be out of date">stale</span>` : ""}</p>
            </div>
            ${close ? html`<button type="button" title="close" aria-label="close allocation details" @click=${close}>&times;</button>` : ""}
          </header>
          <p class="allocation-modal-line mono" title=${line}>${line}</p>
          ${d.national
            ? html`<div class="allocation-columns">
                ${this._renderServiceColumn("Federal", d.national.federal)}
                ${this._renderServiceColumn("Non-Federal", d.national.non_federal)}
              </div>`
            : ""}
          ${this._renderInternational(d)}
          ${this._renderFootnotes(d)}
          ${this._renderDisagreements(d)}
          <footer class="allocation-modal-foot">
            ${this._renderCitations(d)}
            <div class="control-row">
              <button type="button" class="allocation-open-chart" @click=${() => this._openChart()}>Open chart</button>
              ${close ? html`<button type="button" @click=${close}>Close</button>` : ""}
            </div>
          </footer>
    `;
  }

  _renderModal(d, line) {
    const close = () => { this._expanded = false; };
    return html`
      <dialog class="overlay-backdrop allocation-modal" aria-labelledby="allocation-modal-title"
        style="margin:0;padding:0;border:0;width:100vw;height:100vh;max-width:none;max-height:none;color:inherit"
        @cancel=${(e) => { e.preventDefault(); close(); }} @click=${close}>
        <div class="panel allocation-modal-panel" role="document" @click=${(e) => e.stopPropagation()}>
          ${this._renderDetails(d, line, close)}
        </div>
      </dialog>
    `;
  }

  render() {
    const d = this.data;
    if (!d) return html``;
    const info = allocationStatusInfo(d);
    if (info) {
      // The pill beside the digits already carries the status label.
      if (this.actionsOnly) {
        return html`<span class="allocation-summary allocation-summary--status allocation-status-message dim"
          data-status=${d.status} title=${info.message}>${info.message}</span>`;
      }
      return html`
        <div class="allocation-summary allocation-summary--status" data-status=${d.status}>
          <div class="allocation-status-label">${info.label}</div>
          <p class="dim" style="margin:2px 0 0">${info.message}</p>
        </div>
      `;
    }
    const line = compactText(d);
    // Hosted in <band-popover> (instrument-panel pill): just the details body.
    if (this.detailsOnly) return html`<div class="allocation-summary allocation-summary--details">${this._renderDetails(d, line, null)}</div>`;
    const stale = !!d.sources?.stale;
    // Full text always stays in the DOM -- only CSS (line-clamp/wrapping)
    // limits what's visible; the same full text is on the title attribute.
    // Compact: one row (ellipsised line + inline actions) so the strip under the
    // frequency digits stays inside the instrument-panel height gate.
    const lineStyle = this.compact
      ? "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 auto"
      : "white-space:normal;overflow-wrap:anywhere";
    return html`
      <div class="allocation-summary${this.compact ? " allocation-summary--compact" : ""}">
        ${this.actionsOnly ? "" : html`<div class="allocation-compact-line mono" title=${line} style=${lineStyle}>${line}</div>`}
        ${stale ? html`<span class="chip" title="Allocation data may be out of date">stale</span>` : ""}
        <div class="control-row allocation-actions" style=${this.compact ? "margin:0;flex:0 0 auto;gap:6px" : "margin-top:4px"}>
          ${this.actionsOnly ? "" : html`<button
            type="button"
            class="allocation-toggle"
            aria-haspopup="dialog"
            aria-expanded=${this._expanded ? "true" : "false"}
            @click=${() => this._toggle()}
          >Details</button>`}
          ${this.actionsOnly
            // Icon-only in the receiver strip; the § pill beside it already opens the details.
            ? html`<button type="button" class="allocation-open-chart seg-info-button"
                title="Open the allocation chart at this frequency" aria-label="Open the allocation chart at this frequency"
                @click=${() => this._openChart()}>▦</button>`
            : html`<button type="button" class="allocation-open-chart" @click=${() => this._openChart()}>Open chart</button>`}
        </div>
        ${this._expanded && !this.actionsOnly ? this._renderModal(d, line) : ""}
      </div>
    `;
  }
}

/** "25.670 MHz" / "5264 kHz" / "60 kHz" -- band edges for the modal title. */
export function formatBandEdge(hz) {
  if (hz == null) return "?";
  if (hz >= 1e9) return `${trimZeros((hz / 1e9).toFixed(4))} GHz`;
  if (hz >= 1e6) return `${trimZeros((hz / 1e6).toFixed(4))} MHz`;
  if (hz >= 1e3) return `${trimZeros((hz / 1e3).toFixed(3))} kHz`;
  return `${hz} Hz`;
}

function trimZeros(text) {
  return text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

customElements.define("allocation-summary", AllocationSummary);
