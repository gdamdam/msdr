import { LitElement, html } from "../vendor/lit.js";
import { tagColorVar } from "../format.js";

/** Renders a signal's category tags as bracketed chips, e.g. "[sat]", tinted
 * in each tag's color. Looks tags up by id from `tagsById` (built once in
 * app.js from GET /api/tags) since rows only carry tag ids. */
export class TagChips extends LitElement {
  static properties = { tagIds: { type: Array }, tagsById: { type: Object } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.tagIds = [];
    this.tagsById = {};
  }

  render() {
    const ids = this.tagIds ?? [];
    if (!ids.length) return html``;
    return html`${ids.map((id) => {
      const tag = this.tagsById?.[id];
      if (!tag) return "";
      const color = tagColorVar(tag.color);
      return html`<span
        class="tag-chip mono"
        style=${color ? `color:${color};border-color:${color}` : ""}
        >[${tag.name}]</span
      >`;
    })}`;
  }
}

customElements.define("tag-chips", TagChips);
