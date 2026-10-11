import { LitElement, html } from "../vendor/lit.js";
import { tagColorVar, evidenceStateLabel, bandCategoryLabel, supplementalHintsNotice } from "../format.js";
import * as api from "../api.js";
import "./allocation-summary.js";

const DEBOUNCE_MS = 400;

/** Category short code -> its tag, matched by name (case-insensitive) against
 * the tag list held by the app -- same lookup as ident-panel.js. */
function tagFor(category, tags) {
  if (!category) return null;
  return tags.find((t) => t.name.toLowerCase() === category.toLowerCase()) ?? null;
}

/** Frequency references from GET /api/identify. The allocation-only instance
 * sits by the VFO; the receiver-context instance shows only independent
 * candidates, so a band-plan lookup is never presented twice or as detection. */
export class DetectReadout extends LitElement {
  static properties = {
    hz: { type: Number },
    mode: { type: String },
    bw: { type: Number },
    enabled: { type: Boolean },
    allocationOnly: { type: Boolean },
    editAction: { type: Boolean },
    disabled: { type: Boolean },
    tags: { type: Array },
    savedNames: { type: Array },
    // Settings.allocation_jurisdiction (US / ITU1 / ITU2 / ITU3) -- wired in
    // by app.js the same way region is passed to other components.
    jurisdiction: { type: String },
    candidates: { state: true },
    expanded: { state: true },
    _allocation: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.hz = null;
    this.mode = null;
    this.bw = null;
    this.enabled = false;
    this.allocationOnly = false;
    this.tags = [];
    this.savedNames = [];
    this.jurisdiction = "US";
    this.candidates = null; // null = nothing fetched yet (or request in flight)
    this.expanded = false;
    // undefined = not yet requested/in flight, null = request failed, object
    // = the lookup result (docs/allocations.md "Lookup result"), whatever its
    // status -- allocation-summary.js renders every status itself.
    this._allocation = undefined;
    this._timer = null;
    this._reqSeq = 0;
    // Monotonically increasing per docs/allocations.md "UI": a late
    // allocationLookup response (token !== this._allocSeq when it lands)
    // never overwrites what a newer retune already requested.
    this._allocSeq = 0;
    this._onKeydown = this._onKeydown.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    document.addEventListener("keydown", this._onKeydown);
  }

  disconnectedCallback() {
    document.removeEventListener("keydown", this._onKeydown);
    if (this._timer) clearTimeout(this._timer);
    super.disconnectedCallback();
  }

  _onKeydown(ev) {
    if (ev.key === "Escape" && this.expanded) this.expanded = false;
  }

  updated(changed) {
    if (
      changed.has("hz") ||
      changed.has("mode") ||
      changed.has("bw") ||
      changed.has("enabled") ||
      changed.has("jurisdiction")
    ) {
      this._scheduleLookup();
    }
  }

  _scheduleLookup() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    // Bumping the sequence numbers here makes any in-flight response stale,
    // so it can never overwrite what we're about to show/clear.
    this._reqSeq++;
    this._allocSeq++;
    this.candidates = null;
    this._setAllocation(undefined);
    this.expanded = false;
    if (!this.enabled || this.hz === null || this.hz === undefined) {
      this.candidates = null;
      this._setAllocation(undefined);
      this.expanded = false;
      return;
    }
    this._timer = setTimeout(() => {
      this._lookup();
      this._lookupAllocation();
    }, DEBOUNCE_MS);
  }

  async _lookup() {
    const seq = ++this._reqSeq;
    const { hz, mode, bw } = this;
    let resp;
    try {
      resp = await api.identifyLive(hz, mode, bw);
    } catch (err) {
      if (seq !== this._reqSeq) return; // a newer request/settle superseded this one
      console.error("identifyLive failed", err);
      this.candidates = [];
      this.expanded = false;
      return;
    }
    if (seq !== this._reqSeq) return;
    this.candidates = resp.candidates ?? [];
    if (!this.candidates.length) this.expanded = false;
  }

  /** docs/allocations.md "UI": each tune/identify carries its own token, so a
   * reply for a frequency the operator has already tuned away from is simply
   * dropped -- fast retuning can never show stale allocation context. */
  async _lookupAllocation() {
    const token = ++this._allocSeq;
    const { hz, jurisdiction } = this;
    let resp;
    try {
      resp = await api.allocationLookup(hz, jurisdiction);
    } catch (err) {
      if (token !== this._allocSeq) return;
      console.error("allocationLookup failed", err);
      this._setAllocation(null);
      return;
    }
    if (token !== this._allocSeq) return;
    this._setAllocation(resp);
  }

  /** Store the lookup result and report it upward (`allocation-change`), so
   * the one-line pill beside the frequency digits (rendered by the parent,
   * outside this strip) shows the same result without a second lookup. The
   * pill must stay a single <button>: nesting the summary's own buttons
   * inside it was invalid HTML and made Details also open the frequency editor. */
  _setAllocation(value) {
    this._allocation = value;
    this.dispatchEvent?.(new CustomEvent("allocation-change", {
      detail: value, bubbles: true, composed: true,
    }));
  }

  _toggle() {
    if (this.candidates?.some(candidate => candidate.provider !== "bandplan")) {
      this.expanded = !this.expanded;
    }
  }

  _onLineKeydown(ev) {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      this._toggle();
    }
  }

  _row(c, { reason, hideName = false }) {
    const tag = tagFor(c.category, this.tags);
    const color = tag ? tagColorVar(tag.color) : null;
    return html`
      <span class="dim" style="font-size:0.85em">${c.provider === "catalog" ? "catalog match · reference only" : evidenceStateLabel("estimated")}</span>
      ${hideName ? "" : html`<span>${c.name}</span>`}
      ${c.category
        ? html`<span
            class="tag-chip mono"
            style=${color ? `color:${color};border-color:${color}` : ""}
            >[${c.category}]</span
          >`
        : ""}
      ${reason ? html`<span class="dim" style="font-size:0.85em">${c.reason}</span>` : ""}
    `;
  }

  render() {
    const list = this.candidates ?? [];
    if (this.allocationOnly) {
      // One-line context strip under the frequency digits: the regulatory
      // allocation's Details / Open chart actions (allocation-summary.js in
      // actionsOnly mode -- the one-line pill next to the digits, rendered by
      // the parent from `allocation-change`, already shows the text) plus
      // ALL band-plan channel/application hints, ellipsised with the full
      // list in the title.
      const hints = list.filter(candidate => candidate.provider === "bandplan");
      // The band-plan hints are US-specific: under an ITU jurisdiction the
      // lookup says so (`supplemental.reason`) and that replaces the hints.
      const hintsNotice = supplementalHintsNotice(this._allocation);
      return html`<div class="frequency-context-strip" aria-label="Tuned band allocation">
        ${this._allocation !== undefined
          ? html`<allocation-summary .data=${this._allocation} compact .actionsOnly=${true}></allocation-summary>`
          : html`<span class="dim">Looking up allocation…</span>`}
        ${hintsNotice
          ? html`<div class="band-plan-hints">
              <span class="frequency-context-label" title="Channel / application hints from the band plan">Band plan</span>
              <small class="dim">${hintsNotice}</small>
            </div>`
          : hints.length
          ? html`<div class="band-plan-hints" title=${hints.map(h => h.name).join(" · ")}>
              <span class="frequency-context-label" title="Channel / application hints from the band plan">Band plan</span>
              ${hints.map((h, i) => html`<small>${i ? " · " : ""}${h.name}${h.category ? ` (${bandCategoryLabel(h.category)})` : ""}</small>`)}
            </div>`
          : ""}
      </div>`;
    }

    const other = list.filter(candidate => candidate.provider !== "bandplan");
    const top = other[0] ?? null;
    if (!top) return html``;
    const normalize = text => String(text ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
    const topName = normalize(top.name);
    const repeatedName = top.provider === "catalog" && topName &&
      this.savedNames.some(name => { const saved = normalize(name); return saved === topName || (topName.length >= 4 && saved.includes(topName)); });

    return html`
      <div class="detect-readout">
        <span class="label">${top.provider === "catalog" ? "Catalog" : "Other evidence"}</span>
        <div
          class="row mono"
          style="padding:2px 0;flex-wrap:wrap"
          tabindex="0"
          title=${top.name}
          @click=${() => this._toggle()}
          @keydown=${(ev) => this._onLineKeydown(ev)}
        >
          ${this._row(top, { reason: false, hideName: repeatedName })}
        </div>
        ${this.expanded
          ? html`<div class="list" style="max-height:none">
              ${other.map(
                (c) => html`<div class="row" style="flex-wrap:wrap">${this._row(c, { reason: true })}</div>`
              )}
            </div>`
          : ""}
      </div>
    `;
  }
}

customElements.define("detect-readout", DetectReadout);
