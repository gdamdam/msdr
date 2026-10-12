import { LitElement, html } from "../vendor/lit.js";
import { tagColorVar, withLocalWindow, evidenceStateLabel } from "../format.js";

/** Category short code -> its tag, matched by name (case-insensitive) against
 * the tag list already held by the drawer; null when nothing maps yet, in
 * which case the chip still shows but uncolored (dim ink, like an unknown
 * tag-chips lookup). */
function tagFor(category, tags) {
  if (!category) return null;
  return tags.find((t) => t.name.toLowerCase() === category.toLowerCase()) ?? null;
}

/** IDENT sub-panel inside the Memory Edit drawer: renders the candidates from
 * GET /api/signals/{id}/identify (or the loading/empty states) as clickable
 * rows. Picking a row only fills the drawer's draft -- nothing is written
 * until the user presses Save (see drawer.js _pickCandidate). */
export class IdentPanel extends LitElement {
  static properties = {
    loading: { type: Boolean },
    candidates: { type: Array }, // null = not run yet, [] = ran, no matches
    tags: { type: Array },
    tz: { type: Object }, // state.timezone -- see withLocalWindow
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.loading = false;
    this.candidates = null;
    this.tags = [];
    this.tz = null;
  }

  _pick(candidate) {
    this.dispatchEvent(new CustomEvent("pick", { detail: { candidate } }));
  }

  // Ident candidates are the analyzer's best guess against the band plan --
  // never a confirmed identity -- so the state word is always "estimated"
  // (no bars, no % anywhere, see format.js's evidenceStateLabel).
  _row(c) {
    const tag = tagFor(c.category, this.tags);
    const color = tag ? tagColorVar(tag.color) : null;
    return html`
      <div class="row" @click=${() => this._pick(c)}>
        <span class="dim" style="font-size:0.85em">${evidenceStateLabel("estimated")}</span>
        <span>${c.name}</span>
        ${c.category
          ? html`<span
              class="tag-chip mono"
              style=${color ? `color:${color};border-color:${color}` : ""}
              >[${c.category}]</span
            >`
          : ""}
        <span class="dim" style="font-size:0.85em">${withLocalWindow(c.reason, this.tz)}</span>
      </div>
    `;
  }

  render() {
    return html`
      <div class="panel">
        <h2>Ident</h2>
        ${this.candidates === null
          ? ""
          : this.candidates.length
            ? html`<div class="list" style="max-height:none">
                ${this.candidates.map((c) => this._row(c))}
              </div>`
            : html`<p class="label" style="margin:0">no match in US band plan</p>`}
      </div>
    `;
  }
}

customElements.define("ident-panel", IdentPanel);
