import { LitElement, html } from "../vendor/lit.js";
import { ledColorVar } from "../format.js";

/** Resolve `color` to a CSS color: a signal color name ("red".."white"),
 * a raw var()/hex passed straight through (used for the LINK/ALERT lamps,
 * whose states aren't part of the 8-color signal palette), or null (unlit). */
function resolve(color) {
  if (!color) return null;
  const named = ledColorVar(color);
  if (named) return named;
  if (color.startsWith("var(") || color.startsWith("#")) return color;
  return null;
}

/** A single panel indicator LED. Lit in `color` with a subtle glow -- the one
 * glow allowed anywhere on this panel -- or unlit (dim, flat) when there's no
 * color to show. */
export class LedDot extends LitElement {
  static properties = { color: { type: String }, lit: { type: Boolean } };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.color = null;
    this.lit = true;
  }

  render() {
    const cssColor = this.lit ? resolve(this.color) : null;
    const style = cssColor
      ? `background:${cssColor};box-shadow:0 0 4px 1px ${cssColor}`
      : "";
    return html`<span class="led ${cssColor ? "" : "led-unlit"}" style=${style}></span>`;
  }
}

customElements.define("led-dot", LedDot);
