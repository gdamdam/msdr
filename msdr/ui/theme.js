// Theme selection: tokens live in styles.css as :root[data-theme="<id>"]
// blocks; "default" is the absence of the attribute. Switched from the ◐
// button in the instrument strip, which cycles through THEMES in order.
export const THEMES = [
  { id: "default", label: "Default (instrument)" },
  { id: "midnight", label: "Midnight" },
  { id: "blueprint", label: "Blueprint" },
  { id: "avionics", label: "Avionics" },
  { id: "monochrome", label: "Monochrome" },
];

const KEY = "msdr.theme";

export function readTheme() {
  try {
    const id = localStorage.getItem(KEY);
    return THEMES.some((t) => t.id === id) ? id : "default";
  } catch {
    // Storage can throw (private window, blocked site data); fall back.
    return "default";
  }
}

export function applyTheme(id) {
  const root = document.documentElement;
  if (id === "default") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", id);
  try {
    localStorage.setItem(KEY, id);
  } catch {
    // Persistence is best-effort; the theme still applies for this session.
  }
}

export function themeLabel(id) {
  return (THEMES.find((t) => t.id === id) ?? THEMES[0]).label;
}

/** The theme after `id` in THEMES order, wrapping; unknown ids restart. */
export function nextTheme(id) {
  const i = THEMES.findIndex((t) => t.id === id);
  return THEMES[(i + 1) % THEMES.length].id;
}
