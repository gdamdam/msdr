// Display formatting helpers, mirroring msdr/library/frequency.py so the UI
// and the API agree on how a frequency looks.

/** e.g. 137900000 -> "137.900.000"; "--" when hz is missing. */
export function readout(hz) {
  if (hz === null || hz === undefined) return "--";
  const digits = String(Math.trunc(hz));
  const groups = [];
  let rest = digits;
  while (rest.length) {
    groups.unshift(rest.slice(-3));
    rest = rest.slice(0, -3);
  }
  return groups.join(".");
}

/** e.g. 137900000 -> "137.900000 MHz". */
export function mhz(hz) {
  if (hz === null || hz === undefined) return "--";
  return `${(hz / 1_000_000).toFixed(6)} MHz`;
}

// Explicit unit suffix always wins; otherwise mirrors msdr/library/frequency.py's
// bare-number handling but with its own thresholds (v0.9.0 direct-tuning
// design): <1000 -> MHz, 1000..999999 -> kHz, >=1e6 -> Hz. Kept here (not
// duplicated in palette.js, which still uses the server-side parser for
// search) so instrument-panel's frequency entry has one source of truth.
const _FREQ_SUFFIX_MULT = { mhz: 1e6, khz: 1e3, hz: 1, m: 1e6, k: 1e3 };
const _FREQ_PATTERN = /^(\d+\.?\d*)(mhz|khz|hz|m|k)?$/i;

/** Parses a user-typed frequency (instrument-panel's frequency entry) to
 * whole Hz, or null if unparseable/non-positive. defaultUnit overrides bare
 * number heuristics for explicitly labeled fields; an entered suffix still wins. E.g. "14.100" / "14100" ->
 * 14_100_000; "7.2M" / "7.2 MHz" -> 7_200_000; "145800k" / "145800 kHz" ->
 * 145_800_000; "9000000 Hz" -> 9_000_000. */
export function parseFrequencyInput(text, defaultUnit = null) {
  if (typeof text !== "string") return null;
  const cleaned = text.replace(/[\s,_]/g, "");
  if (!cleaned) return null;
  const match = _FREQ_PATTERN.exec(cleaned);
  if (!match) return null;
  const num = Number(match[1]);
  if (!Number.isFinite(num)) return null;
  const suffix = match[2];
  let multiplier;
  if (suffix) {
    multiplier = _FREQ_SUFFIX_MULT[suffix.toLowerCase()];
  } else if (defaultUnit) {
    multiplier = _FREQ_SUFFIX_MULT[defaultUnit.toLowerCase()];
    if (!multiplier) return null;
  } else if (num < 1000) {
    multiplier = 1e6;
  } else if (num < 1_000_000) {
    multiplier = 1e3;
  } else {
    multiplier = 1;
  }
  const hz = Math.round(num * multiplier);
  return hz > 0 ? hz : null;
}

// SDRconnect demodulators offered by instrument-panel's MODE select, and the
// filter-bandwidth presets offered by its BW select, per mode (RAW has none
// -- "current" per the v0.9.0 design -- so only its actual value plus
// "Custom…" show up, via _bwOptions in instrument-panel.js).
export const DEMODULATORS = ["AM", "SAM", "NFM", "WFM", "USB", "LSB", "CW", "DSB", "RAW"];

export const BW_PRESETS = {
  AM: [6000, 9000, 12000],
  SAM: [6000, 9000, 12000],
  USB: [2400, 2700, 3000],
  LSB: [2400, 2700, 3000],
  DSB: [2400, 2700, 3000],
  CW: [250, 500, 1000],
  NFM: [12500, 25000],
  WFM: [150000, 200000],
  RAW: [],
};

// Step-tuning defaults per mode, and the explicit cycle instrument-panel's
// step-size button walks through when clicked (overrides the mode default
// until the mode changes -- see app.js's _cycleStep/_stepOverrideHz).
const _STEP_DEFAULTS = {
  AM: 5000,
  SAM: 5000,
  USB: 1000,
  LSB: 1000,
  DSB: 1000,
  CW: 1000,
  NFM: 12500,
  WFM: 100000,
  RAW: 1000,
};

export const STEP_CYCLE_HZ = [10, 100, 1000, 5000, 12500, 25000, 100000];

/** Step size in Hz for `mode` (defaults to 1000 for an unknown/missing mode). */
export function defaultStepHz(mode) {
  return _STEP_DEFAULTS[mode] ?? 1000;
}

/** Step size in Hz -> e.g. "10 Hz" / "12.5 kHz", for the step-size button. */
export function stepLabel(hz) {
  if (hz === null || hz === undefined) return "--";
  return hz < 1000 ? `${hz} Hz` : `${(hz / 1000).toFixed(1)} kHz`;
}

/** Unix epoch seconds -> local "HH:MM"; companion to timeHM for the decoder
 * event stream, whose `ts` (msdr/decoder/backends.py DecodeEvent.ts) is a
 * float epoch from the stored row, not an ISO string like every other
 * timestamp in this file. */
export function epochHM(ts) {
  if (ts === null || ts === undefined) return "--:--";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "--:--";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** Instrument-strip chip label while a channel or band decoder runs
 * ("DECODE · CW"); null when none is active so the chip is hidden.
 * Shapes follow decode-deck: state.decoder.decoders = [{id}], .band.decoders = [id]. */
export function decodeChipLabel(state) {
  const d = state?.decoder;
  const ids = d?.active ? (d.decoders ?? []).map((x) => x.id) : d?.band?.active ? (d.band.decoders ?? []) : null;
  if (!ids) return null;
  return `DECODE · ${ids.map((id) => String(id).toUpperCase()).join(", ") || "BAND"}`;
}

/** Compact one-line summary for a `kind: "call"` decode event (DMR/P25/NXDN
 * via dsd-fme, see docs/superpowers/plans/2026-09-25-survey-s6-digital-voice.md
 * Stream A's DecodeEvent.fields contract) -- shared by decode-view's message
 * rows and decode-deck's last-event line so both render calls identically.
 * `fields.color_code`/`nac`/`ran` are mutually exclusive per protocol;
 * duration_s is only present once the call has ended. */
export function callLineText(ev) {
  const f = ev.fields ?? {};
  const parts = [];
  if (f.tgt != null) parts.push(`TG ${f.tgt}`);
  if (f.src != null) parts.push(`from ${f.src}`);
  if (f.protocol) parts.push(f.protocol);
  if (f.color_code != null) parts.push(`CC ${f.color_code}`);
  else if (f.nac != null) parts.push(`NAC ${f.nac}`);
  else if (f.ran != null) parts.push(`RAN ${f.ran}`);
  // The backend sends the raw float (now - started_at); round to match its
  // own summary text's `{duration_s:.1f}s`.
  if (f.duration_s != null) parts.push(`⏱ ${Number(f.duration_s).toFixed(1)}s`);
  const text = parts.join(" · ");
  return f.encrypted ? `🔒 ${text}` : text;
}

/** CSS color var for a time-signal offset in seconds (decode-view's `time`
 * rows, `fields.offset_s` -- see docs/superpowers/plans/2026-09-25-survey-s7-s10-hf.md
 * Stream C): |offset| < 0.1 s green, < 1 s amber (this palette's `--accent`),
 * else red. Null when offset_s is missing/NaN, so callers fall back to the
 * default text color. */
export function offsetColorVar(offsetS) {
  if (offsetS === null || offsetS === undefined || Number.isNaN(offsetS)) return null;
  const abs = Math.abs(offsetS);
  if (abs < 0.1) return "var(--ok)";
  if (abs < 1) return "var(--accent)";
  return "var(--danger)";
}

/** Signed offset in seconds -> e.g. "+0.12 s" (time-signal `fields.offset_s`,
 * distinct from khzDelta's kHz formatting). */
export function offsetText(offsetS) {
  if (offsetS === null || offsetS === undefined || Number.isNaN(offsetS)) return "--";
  const sign = offsetS >= 0 ? "+" : "";
  return `${sign}${offsetS.toFixed(2)} s`;
}

/** ISO-8601 instant -> local "HH:MM". */
export function timeHM(iso) {
  if (!iso) return "--:--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** Bandwidth in Hz -> e.g. "12.5 kHz". */
export function bw(hz) {
  if (hz === null || hz === undefined) return "--";
  return `${(hz / 1000).toFixed(1)} kHz`;
}

/** Signed offset in Hz -> e.g. "+2.5 kHz". */
export function khzDelta(hz) {
  if (hz === null || hz === undefined) return "--";
  const khz = hz / 1000;
  const sign = khz >= 0 ? "+" : "";
  return `${sign}${khz.toFixed(1)} kHz`;
}

/** Sample rate in Hz -> e.g. "10 MS/s" or "2.4 MS/s". */
export function sampleRate(hz) {
  if (hz === null || hz === undefined) return "--";
  const msps = hz / 1_000_000;
  const decimals = Number.isInteger(msps) ? 0 : 1;
  return `${msps.toFixed(decimals)} MS/s`;
}

/** Contrast reading in dB, always signed -> e.g. "+12.4 dB". */
export function contrastDb(db) {
  if (db === null || db === undefined) return "--";
  const sign = db >= 0 ? "+" : "";
  return `${sign}${db.toFixed(1)} dB`;
}

/** ISO-8601 instant -> local "YYYY-MM-DD HH:MM" (scan timestamps span days,
 * unlike journal entries which are already grouped by day). */
export function dateTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--";
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${mo}-${day} ${hh}:${mm}`;
}

/** ISO-8601 instant -> "just now" / "37m ago" / "5h ago" within the last day,
 * else the plain date (YYYY-MM-DD) -- library-view's LAST HEARD column, where
 * a bare date is more useful than a relative age once it's a day+ old.
 * `at` is injectable (defaults to now) so this stays deterministic in tests. */
export function relativeOrDate(iso, at = new Date()) {
  if (!iso) return "--";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "--";
  const mins = Math.floor((at.getTime() - t) / 60_000);
  if (mins < 0 || mins >= 24 * 60) return dateTime(iso).slice(0, 10);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

const COMPASS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
];

/** Azimuth degrees -> 16-point compass label, e.g. 157 -> "SSE". */
export function compassPoint(deg) {
  if (deg === null || deg === undefined) return "--";
  const idx = Math.round(((deg % 360) + 360) % 360 / 22.5) % 16;
  return COMPASS[idx];
}

/** Today's date in the device's UTC day, "YYYY-MM-DD" (matches the API's journal day key). */
export function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

// --- timezone (state.timezone: {name, auto, utc_offset_minutes}, see
// msdr/settings.py Settings.timezone / AppService.state_payload) ------------

// A name Intl.DateTimeFormat can't resolve as a real IANA zone -- the server
// synthesizes one of these when the OS-detected zone has no IANA name (see
// AppService's auto-detect fallback); localTime falls back to hand-applying
// utc_offset_minutes for these instead of throwing.
const _SYNTHETIC_TZ_RE = /^UTC[+-]\d{2}:\d{2}$/;

/** Signed UTC offset in minutes -> "UTC+02:00" / "UTC-05:00" / "UTC+00:00". */
export function utcOffsetLabel(minutes) {
  if (typeof minutes !== "number" || Number.isNaN(minutes)) return "UTC";
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  return `UTC${sign}${hh}:${mm}`;
}

/** Epoch seconds (number, matching epochHM) or an ISO-8601 string -> local
 * "HH:MM" in `tz` ({name, utc_offset_minutes}, see state.timezone). Uses
 * Intl.DateTimeFormat with timeZone: tz.name when it's a real IANA zone;
 * falls back to hand-applying utc_offset_minutes when `name` is missing, a
 * synthetic "UTC±HH:MM" label, or Intl rejects it as unknown. */
export function localTime(epochOrIso, tz) {
  const ms = typeof epochOrIso === "number" ? epochOrIso * 1000 : Date.parse(epochOrIso);
  if (Number.isNaN(ms)) return "--:--";
  const name = tz?.name;
  if (name && !_SYNTHETIC_TZ_RE.test(name)) {
    try {
      const fmt = new Intl.DateTimeFormat("en-GB", {
        timeZone: name,
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
      return fmt.format(new Date(ms));
    } catch {
      // unknown/invalid IANA name -- fall through to the offset fallback
    }
  }
  const offsetMin = tz?.utc_offset_minutes;
  if (typeof offsetMin !== "number") return "--:--";
  const d = new Date(ms + offsetMin * 60_000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** "HHMM-HHMM" UTC window (catalogWindow's own format) -> the equivalent
 * local time range, e.g. "1820-1920" -> "20:20–21:20". Only the time-of-day
 * matters (today's UTC date is just a reference point for the conversion),
 * so this is safe to call for a window whose window wraps midnight. Returns
 * null when `utcWindow` isn't in that shape. */
export function utcWindowToLocal(utcWindow, tz) {
  const m = typeof utcWindow === "string" ? /^(\d{2})(\d{2})-(\d{2})(\d{2})$/.exec(utcWindow) : null;
  if (!m) return null;
  const now = new Date();
  const epochS = (hh, mm) =>
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(hh), Number(mm)) / 1000;
  const start = localTime(epochS(m[1], m[2]), tz);
  const stop = localTime(epochS(m[3], m[4]), tz);
  return `${start}–${stop}`;
}

const _UTC_WINDOW_TEXT_RE = /(\d{2})(\d{2})-(\d{2})(\d{2}) UTC/;

/** Rewrites a server-rendered "HHMM-HHMM UTC" window inside free text (LISTEN
 * item subtitles, IDENT candidate reasons -- see msdr/api/service.py's
 * `window` string) into "HH:MM–HH:MM UTC · HH:MM–HH:MM local". Colons the UTC
 * range even with no `tz`; appends the local range only when `tz`'s offset is
 * nonzero. Text without the pattern (or without a window at all) is returned
 * unchanged. */
export function withLocalWindow(text, tz) {
  if (typeof text !== "string") return text;
  const m = _UTC_WINDOW_TEXT_RE.exec(text);
  if (!m) return text;
  const utcWindow = `${m[1]}${m[2]}-${m[3]}${m[4]}`;
  const utcLabel = `${m[1]}:${m[2]}–${m[3]}:${m[4]} UTC`;
  const offset = tz?.utc_offset_minutes;
  let replacement = utcLabel;
  if (offset) {
    const local = utcWindowToLocal(utcWindow, tz);
    if (local) replacement = `${utcLabel} · ${local} local`;
  }
  return text.slice(0, m.index) + replacement + text.slice(m.index + m[0].length);
}

/** Catalog schedule window for display: "HH:MM–HH:MM UTC" plus, when `tz`'s
 * offset is nonzero, " · HH:MM–HH:MM local" -- shared by catalog-view.js and
 * palette.js so search results and the full CATALOG view render identically.
 * NULL start/stop (msdr/catalog/models.py: always-on) still shows "24h". */
export function catalogWindowLabel(entry, tz) {
  if (!entry?.start_utc || !entry?.stop_utc) return "24h";
  const utcLabel = `${entry.start_utc.slice(0, 2)}:${entry.start_utc.slice(2)}–${entry.stop_utc.slice(0, 2)}:${entry.stop_utc.slice(2)} UTC`;
  const offset = tz?.utc_offset_minutes;
  if (!offset) return utcLabel;
  const local = utcWindowToLocal(`${entry.start_utc}-${entry.stop_utc}`, tz);
  return local ? `${utcLabel} · ${local} local` : utcLabel;
}

// Scanner/tag color palettes -- must match msdr/library/models.py exactly
// (see .superpowers/sdd/colors/contract.md). Kept here, not guessed per
// component, so an unknown or missing color name degrades to "unlit" everywhere.
const SIGNAL_COLORS = new Set([
  "red",
  "orange",
  "yellow",
  "green",
  "blue",
  "magenta",
  "cyan",
  "white",
]);
const TAG_COLORS = new Set(["red", "orange", "green", "blue", "violet", "gray"]);

/** Signal color name -> its CSS var, or null (unlit) when missing/unknown. */
export function ledColorVar(color) {
  return SIGNAL_COLORS.has(color) ? `var(--led-${color})` : null;
}

/** Tag color name -> its CSS var, or null (dim ink) when missing/unknown. */
export function tagColorVar(color) {
  return TAG_COLORS.has(color) ? `var(--tag-${color})` : null;
}

// state.features flag defaults -- an older backend that predates the
// features gate omits the field entirely, which means "everything on"
// (see msdr/ui/app.js and setup-drawer.js).
export const DEFAULT_FEATURES = {
  detect: true,
  ident: true,
  nearby: true,
  journal_passive: true,
  passes: true,
  scans: true,
  sky: true,
  tracking: true,
  catalog: true,
  telemetry: true,
  scanner: true,
  recorder: true,
  decoder: true,
  survey: true,
  beacons: true,
  finds: true,
};

/** Merges partial/missing feature flags over the all-on default. */
export function resolveFeatures(features) {
  return { ...DEFAULT_FEATURES, ...(features ?? {}) };
}

/** Catalog entry schedule window label, e.g. "2300-0400", or "24h" for a
 * NULL window (msdr/catalog/models.py: no start/stop = always). */
export function catalogWindow(entry) {
  if (!entry?.start_utc || !entry?.stop_utc) return "24h";
  return `${entry.start_utc}-${entry.stop_utc}`;
}

/** Is `entry` on air at `at` (default now)? Mirrors msdr/catalog/models.py's
 * on_air(): one-off `date`, else UTC weekday days mask ('MTWTFSS', '.' = off), NULL window =
 * always, and a window with start > stop wraps midnight. */
export function catalogOnAir(entry, at = new Date()) {
  const toMinutes = (hhmm) => {
    if (!hhmm || hhmm.length !== 4) return null;
    const h = Number(hhmm.slice(0, 2));
    const m = Number(hhmm.slice(2));
    return Number.isNaN(h) || Number.isNaN(m) ? null : h * 60 + m;
  };
  const start = toMinutes(entry?.start_utc);
  const stop = toMinutes(entry?.stop_utc);
  const now = at.getUTCHours() * 60 + at.getUTCMinutes();
  if (entry?.date) {
    // One-off event: only on its UTC date (a midnight-wrapping window runs
    // on into the next day), never recurring like `days`.
    const today = at.toISOString().slice(0, 10);
    const next = new Date(Date.parse(`${entry.date}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);
    if (start !== null && stop !== null && start > stop) {
      if (today === entry.date) return now >= start;
      if (today === next) return now < stop;
      return false;
    }
    if (today !== entry.date) return false;
  } else if (entry?.days && entry.days.length === 7) {
    const weekday = (at.getUTCDay() + 6) % 7; // Mon=0..Sun=6, matches Python's Monday=0
    if (entry.days[weekday] === ".") return false;
  }
  if (start === null || stop === null) return true;
  if (start === stop) return true;
  if (start < stop) return start <= now && now < stop;
  return now >= start || now < stop; // wraps midnight
}

/** Notes line for a signal saved from a catalog row (the CATALOG ★ action). */
export function catalogNoteLine(entry) {
  return `${entry.source} ${catalogWindow(entry)} ${entry.language ?? ""} → ${entry.target ?? ""}`.trim();
}

// state.layout column contents -- must match msdr/settings.py's Layout
// default and known section ids exactly (see msdr/ui/app.js and
// setup-drawer.js's LAYOUT group).
// v0.14.2: "scanner" retired as a grid section -- the scanner moved into a
// compact deck in the instrument panel (see scanner-deck.js). It stays a
// known-but-dropped id in msdr/settings.py's Layout validator so an existing
// settings.json saved before this still loads.
// S1 (0.26.0): "scan"/"decode" (the decks moved out of the instrument
// panel), "review" and "library" appended -- same order as msdr/settings.py
// so both sides backfill a missing id into the same place.
export const SECTION_IDS = ["survey", "nearby", "inbox", "recent", "listen", "log", "passes", "sky", "scans", "catalog", "beacons", "finds", "scan", "decode", "review", "library", "allocations"];

export const SECTION_LABELS = {
  // S1: user-facing labels say what the section does; ids are unchanged.
  survey: "WATCH SPECTRUM",
  nearby: "NEARBY",
  inbox: "INBOX",
  recent: "RECENT",
  listen: "LISTEN",
  log: "LOG",
  passes: "PASSES",
  sky: "SKY",
  scans: "ANALYZE RECORDING",
  catalog: "CATALOG",
  beacons: "BEACONS",
  finds: "FINDS",
  scan: "SCAN FREQUENCIES",
  decode: "DECODE",
  review: "REVIEW",
  library: "LIBRARY",
  allocations: "ALLOCATIONS",
};

// LISTEN's default (S1) -- not every id; resolveLayout backfills the rest
// into the last column exactly like msdr/settings.py's Layout validator.
export const DEFAULT_LAYOUT_COLUMNS = [
  ["survey", "listen", "decode"],
  ["nearby", "recent"],
  ["log"],
];

// Sections rendered full-width in a band above the 3-column grid (see
// app.js's _renderWideBand/_visibleIdsForColumn and .wide-band in
// styles.css) -- ids here still keep their slot in layout.columns
// (msdr/settings.py's Layout), they're just skipped when rendering columns.
export const DEFAULT_LAYOUT_WIDE = ["survey"];

/** Named layout presets offered by instrument-panel.js's LAYOUT ▾ menu,
 * setup-drawer.js's LAYOUT group, and the command palette ("layout: …"
 * actions, see msdr/library/search.py ACTIONS) -- applying one is just
 * `_setLayout(columns)`, the same immediate-PATCH path as a drag or the
 * SETUP arrows. */
export const LAYOUT_PRESETS = {
  listening: {
    label: "Listening",
    columns: [
      ["listen", "nearby"],
      ["catalog", "recent"],
      ["inbox", "log", "passes", "sky", "scans"],
    ],
  },
  library: {
    label: "Library",
    columns: [
      ["inbox", "recent"],
      ["catalog", "log"],
      ["nearby", "listen", "passes", "sky", "scans"],
    ],
  },
  satellites: {
    label: "Satellites",
    columns: [
      ["passes", "sky"],
      ["nearby", "recent", "inbox"],
      ["listen", "log", "scans", "catalog"],
    ],
  },
  default: {
    label: "Default",
    columns: DEFAULT_LAYOUT_COLUMNS,
  },
};

/** Is section `id` visible given resolved `features`? nearby/passes/sky/
 * scans/catalog gate on their matching flag; every other section (inbox,
 * recent, listen, log) has no feature flag and is always shown. Shared by
 * app.js's column rendering and setup-drawer.js's LAYOUT group so the two
 * never disagree on what's hidden. */
export function sectionVisible(id, features) {
  switch (id) {
    case "survey":
      return !!features?.survey;
    case "nearby":
      return !!features?.nearby;
    case "passes":
      return !!features?.passes;
    case "sky":
      return !!features?.sky;
    case "scans":
      return !!features?.scans;
    case "catalog":
      return !!features?.catalog;
    case "beacons":
      return !!features?.beacons;
    case "finds":
      return !!features?.finds;
    // S1: the decks gate on the same flags instrument-panel used for them.
    case "scan":
      return !!features?.scanner;
    case "decode":
      return !!features?.decoder;
    default:
      return true;
  }
}

/** Merges a possibly-missing/malformed layout over the default, and appends
 * any known section id missing from every column to the last one (mirrors
 * msdr.settings.Layout's own backfill) -- a client-side fallback for a
 * settings fetch that failed, or predates this field. `wide` is resolved the
 * same defensive way (unknown ids dropped, missing/malformed falls back to
 * DEFAULT_LAYOUT_WIDE) but, unlike columns, isn't a partition of SECTION_IDS
 * -- it's just the subset of section ids rendered full-width. */
export function resolveLayout(layout) {
  const columns = layout?.columns;
  const resolvedColumns = !Array.isArray(columns) || columns.length !== 3
    ? DEFAULT_LAYOUT_COLUMNS.map((c) => [...c])
    : (() => {
        const next = columns.map((c) => (Array.isArray(c) ? [...c] : []));
        const seen = new Set(next.flat());
        const missing = SECTION_IDS.filter((id) => !seen.has(id));
        if (missing.length) next[2] = [...next[2], ...missing];
        return next;
      })();
  const wideRaw = layout?.wide;
  const wide = Array.isArray(wideRaw)
    ? wideRaw.filter((id) => SECTION_IDS.includes(id))
    : [...DEFAULT_LAYOUT_WIDE];
  return { columns: resolvedColumns, wide };
}

// U2 workspaces (docs/superpowers/specs/2026-09-26-ui-rework-design.md, "Additions from the
// second review note") -- the switch under the instrument panel, mirroring
// msdr/settings.py's WorkspaceId/WORKSPACE_SECTIONS/_default_workspaces exactly.
// Array order is the tab order and the 1-8 key order.
export const WORKSPACE_IDS = ["listen", "discover", "map", "band_scans", "review", "library", "satellites", "catalog", "allocations"];

export const WORKSPACE_LABELS = {
  listen: "LISTEN",
  discover: "DISCOVER",
  map: "MAP",
  band_scans: "BAND SCANS",
  review: "REVIEW",
  library: "LIBRARY",
  satellites: "SATELLITES",
  catalog: "CATALOG",
  allocations: "ALLOCATIONS",
};

/** Hover help for each workspace tab (data-help, msdr/ui/ui-help.js). */
export const WORKSPACE_HELP = {
  listen: "Tune and listen: frequency, mode, antenna, audio and DSP. Press S to save what you hear to the Library.",
  discover: "Find what's on the air: the SDR live survey, unidentified saves and decodes, plus the Wi-Fi, BLE and Remote ID receivers.",
  map: "Everything with a position on a map: decoded aircraft and other located decodes, saved signals with a location, and Remote ID drones.",
  band_scans: "Sweep a frequency range span by span, review each scan's report and compare scans of the same range.",
  review: "Replay a recording: tune the virtual receiver inside it and run decoders or the Scope on it.",
  library: "Your memories, profiles, notes, tags, lists and favourites; import or export library JSON.",
  satellites: "Satellite passes for your location: pick satellites and days, then tune or track a pass.",
  catalog: "Search broadcast and station schedules (EiBi, HFCC, Aoki and more): who's on a frequency, who's on air now, and tune it.",
  allocations: "Which services a frequency band is allocated to, with footnotes and the full table for your jurisdiction.",
};

/** Hover help for Discover's sub-tabs (app.js). */
export const DISCOVER_VIEW_HELP = {
  spectrum: "Signals found in the current SDR span: new ones are added, ones that go silent are marked quiet.",
  inbox: "Scanner detections and unidentified saves to review: name them and keep the useful ones in your Library.",
  table: "Saved signals and decoder output as a table: the Map's data, without the map.",
  wifi: "Passive Wi-Fi: networks heard on a channel, their details, history and raw frames, and the channel survey.",
  ble: "Passive Bluetooth LE: advertisers heard nearby with name, maker, services, sensors and signal.",
  remoteid: "Drone Remote ID decoded from Wi-Fi (Beacon, NAN) and Bluetooth: drone and operator positions as broadcast.",
  timeline: "Wi-Fi networks and BLE advertisers over time; hatched areas are where msdr wasn't listening.",
  beacons: "NCDXF / NDB beacon monitor: who is on air right now from the schedule, and which beacons were heard in the last 24 hours (propagation).",
  rules: "Alerts on Wi-Fi, BLE and drone data: a beacon appearing or going missing, a sensor crossing a threshold, a drone entering an area.",
};

// Which SECTION_IDS each workspace actually shows/lets the LAYOUT group
// touch. A workspace's *stored* Layout (state.settings.workspaces[id]) also
// carries every other section id -- backfilled into its last column by the
// backend's Layout validator, same rule resolveLayout mirrors above, so the
// stored value stays a full valid Layout -- but that backfilled tail is never
// rendered or offered for reordering; it just rides along, inert, until that
// section's actual workspace is active. "listen" is whatever the other destinations
// don't claim, so nothing is ever unreachable.
// "survey" sits in discover only to keep this an exact partition; app.js
// renders it once in the primary slot for both listen and discover.
const _NON_LISTEN_WORKSPACE_SECTIONS = {
  discover: ["survey", "scan", "finds", "inbox", "scans", "beacons"],
  map: [],
  band_scans: [],
  review: ["review"],
  // Unidentified saves live beside scanner finds in Discover → Inbox.
  library: ["library"],
  satellites: ["passes", "sky"],
  catalog: ["catalog"],
  allocations: ["allocations"],
};
export const WORKSPACE_SECTIONS = {
  listen: SECTION_IDS.filter(
    (id) => !Object.values(_NON_LISTEN_WORKSPACE_SECTIONS).flat().includes(id)
  ),
  ..._NON_LISTEN_WORKSPACE_SECTIONS,
};

// "listen"'s default is DEFAULT_LAYOUT_COLUMNS/WIDE (see msdr/settings.py's
// _default_workspaces) -- the others get an arrangement of just their own
// WORKSPACE_SECTIONS ids.
export const DEFAULT_WORKSPACE_COLUMNS = {
  listen: DEFAULT_LAYOUT_COLUMNS,
  discover: [["survey"], ["scan", "finds", "inbox"], ["scans", "beacons"]],
  map: [[], [], []],
  band_scans: [[], [], []],
  review: [["review"], [], []],
  library: [["library"], [], []],
  satellites: [["passes"], ["sky"], []],
  catalog: [["catalog"], [], []],
  allocations: [["allocations"], [], []],
};
export const DEFAULT_WORKSPACE_WIDE = {
  listen: DEFAULT_LAYOUT_WIDE,
  discover: ["survey", "scan", "finds"],
  map: [],
  band_scans: [],
  review: ["review"],
  library: ["library"],
  satellites: [],
  catalog: ["catalog"],
  allocations: ["allocations"],
};

/** Same defensive resolution as resolveLayout, scoped to one workspace of
 * state.settings.workspaces -- falls back to that workspace's own default
 * (not the global one) when missing/malformed. */
export function resolveWorkspaceLayout(workspaces, workspace) {
  const fallbackColumns = DEFAULT_WORKSPACE_COLUMNS[workspace] ?? DEFAULT_LAYOUT_COLUMNS;
  const fallbackWide = DEFAULT_WORKSPACE_WIDE[workspace] ?? DEFAULT_LAYOUT_WIDE;
  const layout = workspaces?.[workspace];
  const columns = layout?.columns;
  const resolvedColumns = !Array.isArray(columns) || columns.length !== 3
    ? fallbackColumns.map((c) => [...c])
    : (() => {
        const next = columns.map((c) => (Array.isArray(c) ? [...c] : []));
        const seen = new Set(next.flat());
        const missing = SECTION_IDS.filter((id) => !seen.has(id));
        if (missing.length) next[2] = [...next[2], ...missing];
        return next;
      })();
  const wideRaw = layout?.wide;
  const wide = Array.isArray(wideRaw)
    ? wideRaw.filter((id) => SECTION_IDS.includes(id))
    : [...fallbackWide];
  return { columns: resolvedColumns, wide };
}

// Readable labels for raw backend keys shown in SURVEY (survey-view.js):
// dimension checkbox ids (msdr/survey/survey.py AVAILABLE_DIMENSIONS) and
// bw_class values (msdr/survey/classify.py bandwidth_class). "activity" kind
// values (steady/bursty/periodic/intermittent/new) keep their own word, just
// capitalised -- there's no separate mapping table for those.
const DIMENSION_LABELS = {
  activity: "Activity",
  bandwidth: "Bandwidth class",
  bandplan: "Band plan",
  catalog: "Catalog",
  modulation: "Modulation",
  rds: "RDS (FM names)",
  symbol_rate: "Symbol rate",
  voice: "Voice / data",
};

const BW_CLASS_LABELS = {
  carrier: "Carrier",
  narrow: "Narrow",
  nfm: "NFM",
  am: "AM",
  wfm: "WFM",
  wide_digital: "Wide digital",
  unresolved: "Unresolved",
};

/** Readable label for a raw backend key -- `kind` is "dimension" (survey
 * dimension checkbox ids), "bw_class" (BW CLASS column values), or "activity"
 * (ACTIVITY column kind, just capitalised). Unknown/missing values fall back
 * to the raw value so a value the mapping doesn't know about still shows
 * something rather than disappearing. */
export function labelFor(kind, value) {
  if (value === null || value === undefined || value === "") return "--";
  if (kind === "dimension") return DIMENSION_LABELS[value] ?? value;
  if (kind === "bw_class") return BW_CLASS_LABELS[value] ?? value;
  if (kind === "activity") return value.charAt(0).toUpperCase() + value.slice(1);
  return value;
}

// U3 signal-detail panel (components/signal-panel.js) evidence kinds --
// msdr/api/signal_detail.py's evidence[].kind, in the same decoded -> memory
// -> scheduled -> bandplan -> analyzer order the backend returns them.
// Shared by signal-panel.js's Evidence list and survey-view.js's reuse of the
// same classes on its BAND PLAN/CATALOG/FAMILY cells (see styles.css's .ev-*).
const EVIDENCE_GLYPHS = {
  decoded: "✓",
  memory: "★",
  scheduled: "⏱",
  bandplan: "▦",
  allocation: "§",
  analyzer: "≈",
};

const EVIDENCE_LABELS = {
  decoded: "Decoded",
  memory: "Memory",
  scheduled: "Scheduled",
  bandplan: "Channel / application hint",
  allocation: "Allocation",
  analyzer: "Analyzer",
};

/** Prefix glyph for an evidence kind, or "" when unknown. */
export function evidenceGlyph(kind) {
  return EVIDENCE_GLYPHS[kind] ?? "";
}

/** Readable label for an evidence kind, falling back to the raw value. */
export function evidenceLabel(kind) {
  return EVIDENCE_LABELS[kind] ?? kind ?? "";
}

/** styles.css class for an evidence kind's border/color treatment, or "" for
 * an unknown kind (renders with no special styling rather than breaking). */
export function evidenceClass(kind) {
  return EVIDENCE_GLYPHS[kind] ? `ev-${kind}` : "";
}

// S2 observations/evidence "state" (msdr/observations -- see
// docs/epics/listening-workbench/S2-observations-contract.md): replaces
// confidence percentages/meters everywhere in the UI with honest, unitless
// words. Never renders a number -- a `score` (unitless heuristic) is shown
// only in a tooltip, never as the label itself.
const EVIDENCE_STATE_LABELS = {
  context: "context",
  estimated: "estimated",
  validated: "validated",
  operator: "identified by operator",
};

/** Readable word for an evidence/observation `state`
 * (context/estimated/validated/operator), falling back to the raw value (or
 * "" when missing) so an unrecognized state still shows something rather
 * than a blank chip. */
export function evidenceStateLabel(state) {
  if (!state) return "";
  return EVIDENCE_STATE_LABELS[state] ?? state;
}

// SURVEY table/hover-card tooltips (survey-view.js): short plain-English
// explanations for values that aren't self-evident at a glance (fm_fsk,
// psk_like, an evidence kind, ...). labelFor/evidenceLabel still supply the
// short on-screen text -- explain() supplies the "what does that mean"
// sentence survey-view.js appends to a cell's title ("value — explanation").
const MODULATION_EXPLANATIONS = {
  fm_fsk: "Constant envelope, wandering frequency: FM voice or FSK data (APRS, POCSAG, packet).",
  psk_like: "Phase shifts carry the data -- typical of many digital modes.",
  ook: "On-off keying: the carrier is simply present or absent -- CW, remotes, some beacons.",
  am: "Amplitude carries the signal (not frequency or phase).",
  carrier: "Unmodulated tone: a beacon, an idle transmitter, or a spur.",
  unknown: "The analyzer couldn't confidently classify this signal's modulation.",
  wideband: "Broadcast-width signal -- too wide to run through the narrowband analyzer.",
};

const BW_CLASS_EXPLANATIONS = {
  carrier: "An unmodulated or near-unmodulated tone.",
  narrow: "Under ~4 kHz wide.",
  am: "AM broadcast/aviation-width channel.",
  nfm: "~4-20 kHz wide: two-way radio, pagers, APRS.",
  wfm: "FM broadcast-width channel.",
  wide_digital: "Flat-topped wide channel: DAB, HD Radio, LTE and similar.",
  unresolved: "Spectrum bins are wider than this signal at this span -- zoom in (lower MS/s) or let the analyzer measure it.",
};

const ACTIVITY_EXPLANATIONS = {
  steady: "On continuously for the whole observation window.",
  bursty: "On in short, irregular bursts.",
  periodic: "Recurring at a roughly regular interval (see the period shown).",
  intermittent: "On occasionally, with no clear pattern or period.",
  new: "First seen recently -- not yet classified as steady/bursty/periodic.",
};

const VOICE_EXPLANATIONS = {
  voice: "Tentative speech-like envelope, not confirmed analog voice. Bursty digital signals can look similar. Listen and verify with a recording or decoder.",
  data: 'Not analog speech -- includes digital voice (e.g. DMR), which reads as "data" until it\'s decoded.',
  silence: "No modulation detected during the analysis window.",
};

// FAMILY/BAND PLAN/CATALOG columns each wrap their value in an evidenceClass
// span -- this explains what kind of match that is, reusing the same ids.
const EVIDENCE_EXPLANATIONS = {
  analyzer: "The IQ analyzer's best-guess classification from modulation/symbol-rate matching -- not a confirmed identity.",
  // "bandplan" is a channel/application hint (a named channel or a usage
  // note like DME or oceanographic radar), not a regulatory allocation --
  // see "allocation" below for that.
  bandplan: "A named channel or application hint for this frequency -- not a regulatory allocation and not an identification of the actual transmitter.",
  allocation: "A regulatory frequency allocation (who/what this band is allocated to by rule), not an identification of the actual transmitter.",
  scheduled: "Matches a scheduled catalog entry (broadcast schedule, satellite pass, etc.) expected here.",
  decoded: "Confirmed by successfully decoding this signal's content.",
  memory: "Matches a frequency already saved as a memory.",
};

const FIXED_EXPLANATIONS = {
  bw_source_iq: "Measured by the analyzer from IQ (more precise than the spectrum-bin estimate).",
  symbol_rate:
    "Symbols per second estimated from the signal's clock. Common rates: 1200 (APRS/POCSAG), 2400/4800 (DMR/P25), 9600 (packet 9k6).",
  duty: "Percent of the observation window this signal was active.",
  snr: "Decibels above the span's noise floor -- higher is a stronger, cleaner signal.",
  last_seen_now: "Currently active -- being received right now.",
  last_seen: "Seconds since this signal was last detected active in this span.",
};

/** Short (1-2 sentence) plain-English tooltip for a SURVEY value that isn't
 * self-evident, or "" when there's nothing to add (so callers can always
 * concatenate safely). `kind` picks the table above; `value` looks up a
 * specific entry where the explanation depends on it (modulation/bw_class/
 * activity/voice/evidence kinds, and bw_source's "iq"/"spectrum"), and is
 * ignored for the fixed, value-independent ones (symbol_rate/confidence/
 * duty/snr), except last_seen where value is "now" or anything else. */
export function explain(kind, value) {
  switch (kind) {
    case "modulation":
      return MODULATION_EXPLANATIONS[value] ?? "";
    case "bw_class":
      return BW_CLASS_EXPLANATIONS[value] ?? "";
    case "bw_source":
      return value === "iq" ? FIXED_EXPLANATIONS.bw_source_iq : "";
    case "activity":
      return ACTIVITY_EXPLANATIONS[value] ?? "";
    case "voice":
      return VOICE_EXPLANATIONS[value] ?? "";
    case "evidence":
      return EVIDENCE_EXPLANATIONS[value] ?? "";
    case "symbol_rate":
      return FIXED_EXPLANATIONS.symbol_rate;
    case "duty":
      return FIXED_EXPLANATIONS.duty;
    case "snr":
      return FIXED_EXPLANATIONS.snr;
    case "last_seen":
      return value === "now" ? FIXED_EXPLANATIONS.last_seen_now : FIXED_EXPLANATIONS.last_seen;
    default:
      return "";
  }
}

// decode-deck's ⚙ modal decoder list (GET /api/decoders `readiness`, see
// msdr/decoder/... readiness contract): three facts kept visually and
// semantically separate -- installed, tested, and session -- rather than
// collapsed into one "available" badge, since a decoder can be installed but
// never validated, or validated but currently producing nothing.
const _INSTALL_STATE_LABELS = {
  present: "installed",
  builtin: "built-in",
  incompatible: "incompatible",
  missing: "missing",
};

const _VALIDATION_STATE_LABELS = {
  untested: "untested",
  fixture_tested: "fixture-tested",
  reference_iq_tested: "reference IQ-tested",
  live_output_observed: "live output",
  field_observed: "field-observed",
  regression_detected: "REGRESSION",
};

const _SESSION_STATE_LABELS = {
  idle: "idle",
  acquiring: "acquiring",
  synchronized: "synchronized",
  valid_frames: "valid frames",
  unverified_output: "unverified output",
  quiet: "quiet",
  unsupported_input: "unsupported input",
  error: "error",
};

/** Three short {text, title} facts -- installed/tested/session -- for one
 * GET /api/decoders entry `d`. Reads `d.readiness` when present; otherwise
 * falls back to today's pre-readiness shape (`d.available`/`d.install_hint`)
 * for the installed fact and shows "--" for tested/session, since there's no
 * data to report them from. Never returns "available"/"ready" for any fact --
 * installed-but-untested is reported as exactly that, not folded into one
 * verdict. */
export function readinessLabels(d) {
  const r = d?.readiness;
  if (!r) {
    const present = !!d?.available;
    return {
      installed: { text: present ? "installed" : "missing", title: present ? "" : d?.install_hint ?? "" },
      tested: { text: "--", title: "" },
      session: { text: "--", title: "" },
    };
  }
  const inst = r.installation ?? {};
  const installedBase = _INSTALL_STATE_LABELS[inst.state] ?? inst.state ?? "missing";
  const installed = {
    text: inst.state === "present" && inst.version ? `${installedBase} v${inst.version}` : installedBase,
    title: inst.state === "present" || inst.state === "builtin" ? "" : inst.install_hint ?? d?.install_hint ?? "",
  };

  const val = r.validation ?? {};
  const testedBase = _VALIDATION_STATE_LABELS[val.state] ?? val.state ?? "untested";
  const testedTitleParts = [];
  if (val.validated_on) testedTitleParts.push(`validated ${val.validated_on}`);
  if (val.tests?.length) testedTitleParts.push(`tests: ${val.tests.join(", ")}`);
  if (val.fixtures?.length) testedTitleParts.push(`fixtures: ${val.fixtures.join(", ")}`);
  if (val.notes) testedTitleParts.push(val.notes);
  const tested = {
    text: r.validation_stale ? `${testedBase} (stale)` : testedBase,
    title: testedTitleParts.join(" — "),
  };

  const sess = r.session ?? {};
  const sessionExtras = [];
  if (sess.events > 0) sessionExtras.push(`${sess.events} ev`);
  if (sess.valid_frames > 0) sessionExtras.push(`valid frames ${sess.valid_frames}`);
  const sessionBase = _SESSION_STATE_LABELS[sess.state] ?? sess.state ?? "idle";
  const session = {
    text: sessionExtras.length ? `${sessionBase} (${sessionExtras.join(", ")})` : sessionBase,
    title: sess.detail ?? "",
  };

  return { installed, tested, session };
}

// S4 decode integrity/session facts (docs/epics/listening-workbench/
// S4-decode-contract.md §5): a decode event's `integrity` is never guessed --
// it comes straight off the capability-matrix policy that produced the
// event (msdr.decoder.capability_matrix.MATRIX[id].integrity_policy). The
// badge text says exactly what was checked, never "available"/"ready".
const _CHECK_NAMES = ["FCS", "CRC", "PARITY", "CHECKSUM"];

/** First recognized check name (FCS/CRC/parity/checksum) mentioned in a
 * decode event's `integrity_note`, or null when the note doesn't name one
 * (e.g. "multimon exposes no flag") -- see backends.py's per-parser notes. */
function _checkNameFromNote(note) {
  if (!note) return null;
  for (const word of note.split(/[^A-Za-z0-9]+/)) {
    const upper = word.toUpperCase();
    if (_CHECK_NAMES.includes(upper)) return upper;
  }
  return null;
}

/** {text, title} badge for a decode event's `integrity` -- shown on every
 * message row (decode-deck's last event, decode-view rows, signal-panel's
 * DECODER RESULTS/messages). `note` is the event's `integrity_note`, used
 * both as the tooltip and (when it names one) to pick the check in the
 * "passed" badge's text -- "✓ FCS"/"✓ CRC" rather than a generic "checked"
 * whenever the parser said which check passed. unknown/null (including an
 * old row from before this field existed) reads "unverified", never
 * "available"/"ready" -- an unverified event is not a correctness claim. */
export function integrityBadge(integrity, note) {
  const title = note ?? "";
  switch (integrity) {
    case "passed": {
      const check = _checkNameFromNote(note);
      return { text: check ? `✓ ${check}` : "✓ checked", title, cls: "integrity-passed" };
    }
    case "failed":
      return { text: "✗ failed", title, cls: "integrity-failed" };
    case "unavailable":
      return { text: "no check", title, cls: "" };
    case "not_applicable":
      return { text: "—", title, cls: "" };
    default:
      // "unknown", null/undefined, or anything unrecognized.
      return { text: "unverified", title, cls: "" };
  }
}

const _SESSION_FACT_LABELS = {
  idle: "idle",
  acquiring: "acquiring",
  synchronized: "synchronized",
  quiet: "quiet",
  error: "error",
};

/** One-line session fact for a running decoder (decode-deck, per selected
 * decoder) -- `facts` is one entry of manager/band status's
 * `decoders`/`decoder_status` (msdr/decoder/readiness.py's `session_for`):
 * `session_state` plus `events`/`valid_frames`/`unsupported`. Deliberately
 * never says "available" or "ready" -- those aren't correctness claims,
 * only "acquiring"/"N events (unverified)"/"N valid frames"/"synchronized"/
 * "quiet"/"unsupported input: <reason>"/"error" are. */
export function sessionFactText(facts) {
  const f = facts ?? {};
  switch (f.session_state) {
    case "unverified_output":
      return `${f.events ?? 0} events (unverified)`;
    case "valid_frames":
      return `${f.valid_frames ?? 0} valid frames`;
    case "unsupported_input":
      return `unsupported input: ${f.unsupported ?? "unsupported"}`;
    default:
      return _SESSION_FACT_LABELS[f.session_state] ?? f.session_state ?? "idle";
  }
}

/** Signal bound to `satellite`, preferring the most recently heard when
 * several memories share the same satellite binding; null if none. */
export function signalForSatellite(signals, satellite) {
  if (!satellite) return null;
  const bound = (signals ?? []).filter((s) => s.satellite === satellite);
  if (!bound.length) return null;
  return bound.reduce((best, s) => {
    if (!best) return s;
    const a = Date.parse(s.last_heard_at ?? "") || 0;
    const b = Date.parse(best.last_heard_at ?? "") || 0;
    return a > b ? s : best;
  }, null);
}

/** Why the "Channel / application hints" block is empty, from an allocation
 * lookup's `supplemental` block (msdr/api/services/allocations.py
 * allocation_context: the band-plan hints are US-specific, so ITU
 * jurisdictions get `{available: false, reason}`); null when hints apply. */
export function supplementalHintsNotice(allocation) {
  const supplemental = allocation?.supplemental;
  if (!supplemental || supplemental.available !== false) return null;
  return supplemental.reason || "Channel and application hints are unavailable";
}

export function bandCategoryLabel(category) {
  return { air: "Aviation", ham: "Amateur radio", marine: "Maritime", sat: "Satellite",
    mil: "Military", bc: "Broadcast", tv: "Television", digital: "Digital", exp: "Experimental" }[category] ?? category ?? "";
}
