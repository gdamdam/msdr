import { LitElement, html, svg } from "../vendor/lit.js";
import * as api from "../api.js";
import "./device-label-editor.js";
import { autoBucketS } from "./device-history-chart.js";
import "./wifi-activity-chart.js";
import { ACTIVITY_RANGE_OPTIONS, ACTIVITY_HELP, hasSignalMode, wifiChannelMHz } from "./wifi-activity-chart.js";

// docs/history.md "Shared timeline (UI)": the Wi-Fi and BLE detail panes
// also carry the label editor and a 24h history chart for the selected
// device -- same window ble-view's own detail pane fetches.
const LABEL_HISTORY_HOURS = 24;

const LIVE_KEEP = 300; // frames kept on screen
const CHANNELS_24 = Array.from({ length: 13 }, (_, i) => i + 1);
// 5 GHz split at the DFS range (52-144, radar-detection channels): tuning the
// AWUS036ACH to its first DFS channel has dropped the card off the USB bus,
// and home routers rarely sit there, so the default presets leave it out.
const CHANNELS_5_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165];
const CHANNELS_5_DFS = [52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144];
const CHANNELS_5 = [...CHANNELS_5_NON_DFS.slice(0, 4), ...CHANNELS_5_DFS, ...CHANNELS_5_NON_DFS.slice(4)];
const RUNNING = ["starting", "receiving", "stale"];
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Why Start is unavailable here, or null. Disabled-in-settings is NOT a
 * blocker: pressing Start enables Wi-Fi first. */
export function wifiStartBlocker(status) {
  if (!status) return "Loading receiver status…";
  if (status.state === "unavailable") return "Capture helper not installed — set it up in Setup → Wi-Fi receiver";
  if (status.state === "dependency_missing") return "A helper dependency is missing — see Setup → Wi-Fi receiver";
  if (status.state === "no_device") return "No compatible Wi-Fi adapter detected — plug in the AWUS036ACH";
  return null;
}

/** Column header -> server sort key, and the direction a first click uses
 * (newest / strongest / busiest first for time, signal and packets). */
export const WIFI_COLUMNS = [
  { label: "SSID", key: "ssid", first: "asc" },
  { label: "BSSID", key: "bssid", first: "asc" },
  { label: "Label", key: null },
  { label: "Channel", key: "channel", first: "asc" },
  { label: "Security", key: "security", first: "asc" },
  { label: "Signal", key: "signal", first: "desc" },
  { label: "First seen", key: "first_seen", first: "desc" },
  { label: "Last seen", key: "last_seen", first: "desc" },
  { label: "Packets", key: "packets", first: "desc" },
];

const DEFAULT_SORT = { sort: "first_seen", order: "asc" };

/** Next {sort, order} after clicking a header: a new column starts at its
 * natural direction; clicking the active column flips it. */
export function nextSort(current, key) {
  const col = WIFI_COLUMNS.find((c) => c.key === key);
  if (!col) return current;
  if (current.sort !== key) return { sort: key, order: col.first };
  return { sort: key, order: current.order === "asc" ? "desc" : "asc" };
}

// --- Channel survey (docs/history.md "Wi-Fi channel survey") ---------------
// The ACH helper has no channel hopping of its own, so a survey restarts
// capture per channel server-side; this view only drives it and renders
// GET /api/wifi/survey's results.

/** Preset channel lists (docs/history.md's UI contract: "presets: 2.4 GHz
 * 1-13; 2.4 GHz 1/6/11; 5 GHz 20 MHz channels; all"). */
export const SURVEY_PRESETS = [
  { id: "24_all", label: "2.4 GHz (1-13)", channels: CHANNELS_24 },
  { id: "24_1_6_11", label: "2.4 GHz (1/6/11)", channels: [1, 6, 11] },
  { id: "5ghz", label: "5 GHz (non-DFS: 36-48, 149-165)", channels: CHANNELS_5_NON_DFS },
  { id: "5ghz_dfs", label: "5 GHz incl. DFS (36-165)", channels: CHANNELS_5 },
  { id: "all", label: "All channels (no DFS)", channels: [...CHANNELS_24, ...CHANNELS_5_NON_DFS] },
  { id: "all_dfs", label: "All channels incl. DFS", channels: [...CHANNELS_24, ...CHANNELS_5] },
];

/** Preset id -> its channel list, or [] for an unknown/empty id (custom). */
export function presetChannels(id) {
  return SURVEY_PRESETS.find((p) => p.id === id)?.channels ?? [];
}

/** Free-text "1,6,11" (whatever a preset filled in, or a hand-edited list)
 * -> a deduplicated array of channel numbers, dropping anything that isn't
 * one (blank entries, stray commas, non-numeric text). */
export function parseSurveyChannels(text) {
  const seen = new Set();
  for (const part of (text ?? "").split(",")) {
    const n = Number(part.trim());
    if (part.trim() && Number.isFinite(n)) seen.add(n);
  }
  return [...seen];
}

/** Vendor of a BSSID (msdr/oui.py), or that it's locally administered
 * (a router's derived guest/mesh BSSID or a random MAC, never in the
 * registry). Nothing when names aren't downloaded. */
export function vendorText(row) {
  if (row?.vendor) return row.vendor;
  if (row?.local_admin) return "locally administered";
  return null;
}

function vendorLine(row) {
  const text = vendorText(row);
  return text ? html`<div class="wifi-vendor dim">${text}</div>` : "";
}

/** Network summary tiles from GET /api/wifi/summary, or null when nothing
 * was heard. */
// Regroup rows into side-by-side blocks: Totals first, then one per header.
export function summaryBlocks(parts) {
  if (!parts) return [];
  const blocks = [];
  for (const r of parts.rows) {
    if (r.kind === "head") blocks.push({ title: r.label, rows: [] });
    else {
      if (!blocks.length) blocks.push({ title: "Totals", rows: [] });
      blocks[blocks.length - 1].rows.push(r);
    }
  }
  return blocks;
}

export function networkSummaryParts(summary) {
  if (!summary?.total) return null;
  const minutes = Math.round((summary.window_s ?? 60) / 60);
  const window = minutes <= 1 ? "the last minute" : `the last ${minutes} minutes`;
  const sub = (list) => (list ?? []).map((x) => ({ label: x.label, count: x.count }));
  const groups = [
    { label: "probable access points", count: summary.access_points, subgroups: [] },
    { label: "by band", count: summary.total, subgroups: sub(summary.bands) },
    { label: "by security", count: summary.total, subgroups: sub(summary.security) },
  ];
  if (summary.makers?.length) groups.push({ label: "by maker", count: summary.total, subgroups: sub(summary.makers) });
  if (summary.hidden) groups.push({ label: "hidden", count: summary.hidden, subgroups: [] });
  // Table rows: kind "total" = headline figure, "head" = section header,
  // "item" = label/count with share bar; warn flags Open/WEP security.
  const item = (x) => ({ kind: "item", label: x.label, count: x.count, share: x.count / summary.total,
    warn: /^(open|wep)\b/i.test(x.label) });
  const rows = [
    { kind: "total", label: `Networks heard (${window.replace("the ", "")})`, count: summary.total },
    { kind: "total", label: "Probable access points", count: summary.access_points },
  ];
  if (summary.hidden) rows.push({ kind: "total", label: "Hidden", count: summary.hidden });
  const section = (label, list) => {
    if (list?.length) rows.push({ kind: "head", label }, ...list.map(item));
  };
  section("Band", summary.bands);
  section("Security", summary.security);
  section("Maker", summary.makers);
  return { lead: `${plural(summary.total, "network")} heard in ${window}`, groups, rows };
}

const NON_OVERLAPPING_24 = [1, 6, 11];
const OVERLAP_SPAN = 4; // 2.4 GHz: 20 MHz wide on 5 MHz centres -> ±4 channels overlap

/** Frames/s of everything whose frequencies overlap each channel's window:
 * on 2.4 GHz the channel itself plus ±4 neighbours; on 5 GHz (20 MHz
 * channels don't overlap) the channel's own rate. Returns Map channel ->
 * load, over channels that were listened to. A channel's own decoded count
 * misses half-aligned frames from neighbours, so this is the number that
 * says how busy its air actually is. */
export function overlapLoad(results) {
  const heard = (results ?? []).filter((r) => r.listened_s > 0);
  const loads = new Map();
  for (const r of heard) {
    const load = r.channel <= 14
      ? heard.filter((o) => o.channel <= 14 && Math.abs(o.channel - r.channel) <= OVERLAP_SPAN)
        .reduce((n, o) => n + surveyFrameRate(o), 0)
      : surveyFrameRate(r);
    loads.set(r.channel, load);
  }
  return loads;
}

function noisiestNeighbour(r, heard) {
  const near = heard.filter((o) => o.channel !== r.channel && o.channel <= 14
    && Math.abs(o.channel - r.channel) <= OVERLAP_SPAN);
  return near.length ? near.reduce((a, b) => (surveyFrameRate(b) > surveyFrameRate(a) ? b : a)) : null;
}

/** Survey verdict tiles from the per-channel results (channels with a
 * completed or in-progress dwell only), or null before any. "Least
 * congested 2.4 GHz" is chosen among 1/6/11 by overlap load, never by a
 * channel's own decoded count (an in-between channel looks quiet only
 * because its neighbours' frames don't decode there). */
export function surveyVerdict(results) {
  const heard = (results ?? []).filter((r) => r.listened_s > 0);
  if (!heard.length) return null;
  const rate = (r) => surveyFrameRate(r);
  const loads = overlapLoad(heard);
  const busiest = heard.reduce((a, b) => (rate(b) > rate(a) ? b : a));
  const most = heard.reduce((a, b) => (b.networks > a.networks ? b : a));
  const tiles = [
    { label: "busiest channel", value: busiest.channel, detail: `${rate(busiest).toFixed(0)} frames/s · ${plural(busiest.networks, "network")}` },
    { label: "most networks", value: most.channel, detail: `${plural(most.networks, "network")} · ${rate(most).toFixed(0)} frames/s` },
  ];
  const quietest = (list, key) => list.reduce((a, b) =>
    (key(b) < key(a) || (key(b) === key(a) && b.networks < a.networks)) ? b : a);
  const band24 = heard.filter((r) => r.channel <= 14);
  const candidates = band24.filter((r) => NON_OVERLAPPING_24.includes(r.channel));
  if (candidates.length >= 2) {
    const q = quietest(candidates, (r) => loads.get(r.channel));
    tiles.push({ label: "least congested 2.4 GHz", value: q.channel,
      detail: `${loads.get(q.channel).toFixed(0)} frames/s incl. overlap · ${plural(q.networks, "network")} · among 1/6/11` });
  } else if (band24.length >= 2) {
    const q = quietest(band24, (r) => loads.get(r.channel));
    tiles.push({ label: "least congested 2.4 GHz", value: q.channel,
      detail: `${loads.get(q.channel).toFixed(0)} frames/s incl. overlap · ${plural(q.networks, "network")}` });
  }
  const band5 = heard.filter((r) => r.channel > 14);
  if (band5.length >= 2) {
    const q = quietest(band5, rate);
    tiles.push({ label: "least congested 5 GHz", value: q.channel,
      detail: `${rate(q).toFixed(0)} frames/s · ${plural(q.networks, "network")}` });
  }
  const overlap = heard.reduce((n, r) =>
    n + (r.top ?? []).filter((t) => t.ds_channel != null && t.ds_channel !== r.channel).length, 0);
  if (overlap) tiles.push({ label: "overlap", value: overlap, detail: "networks heard off their own channel" });
  return tiles;
}

/** "Why these picks": findings computed from the results as
 * {title, lines[]} sections -- every sentence comes from the numbers of
 * this survey, so it is never wrong for the survey it describes. */
export function surveyFindings(results) {
  const heard = (results ?? []).filter((r) => r.listened_s > 0);
  if (!heard.length) return [];
  const rate = (r) => surveyFrameRate(r);
  const f0 = (n) => n.toFixed(0);
  const loads = overlapLoad(heard);
  const out = [];

  const band24 = heard.filter((r) => r.channel <= 14).sort((a, b) => a.channel - b.channel);
  if (band24.length >= 2) {
    const lines = [];
    const candidates = band24.filter((r) => NON_OVERLAPPING_24.includes(r.channel));
    if (candidates.length >= 2) {
      lines.push("Only channels 1, 6 and 11 don't overlap each other; each 20 MHz signal covers the two channels either side, so a router in between collides with both neighbours instead of sharing one channel.");
      for (const r of candidates) {
        const nb = noisiestNeighbour(r, band24);
        lines.push(`Channel ${r.channel}: ${f0(rate(r))} frames/s on it, ${f0(loads.get(r.channel))} frames/s including overlap (channels ${Math.max(1, r.channel - OVERLAP_SPAN)}-${Math.min(14, r.channel + OVERLAP_SPAN)}), ${plural(r.networks, "network")} heard`
          + (nb ? `; noisiest neighbour channel ${nb.channel} at ${f0(rate(nb))} frames/s` : "") + ".");
      }
      const pick = candidates.reduce((a, b) => (loads.get(b.channel) < loads.get(a.channel) ? b : a));
      lines.push(`Pick: channel ${pick.channel}, the lowest overlap load.`);
    }
    // Quiet-looking traps: a low own count next to busy neighbours.
    for (const r of band24) {
      if (NON_OVERLAPPING_24.includes(r.channel)) continue;
      const own = rate(r); const load = loads.get(r.channel);
      if (own < 10 && load >= 5 * Math.max(own, 1)) {
        const nbs = band24.filter((o) => o.channel !== r.channel && Math.abs(o.channel - r.channel) <= OVERLAP_SPAN && rate(o) >= 20).map((o) => o.channel);
        lines.push(`Channel ${r.channel} looks quiet (${own.toFixed(1)} frames/s) but ${f0(load)} frames/s from channel${nbs.length === 1 ? "" : "s"} ${nbs.join(", ")} share its frequencies: the card can't decode their half-aligned frames, so they aren't counted on ${r.channel}, but a router there would collide with them.`);
      }
    }
    if (lines.length) out.push({ title: "2.4 GHz router channel", lines });
  }

  const band5 = heard.filter((r) => r.channel > 14).sort((a, b) => a.channel - b.channel);
  if (band5.length >= 2) {
    const lines = [];
    // An 80 MHz stream shows the same heavy rate on four 20 MHz slices with few networks.
    const blocks = [[36, 40, 44, 48], [52, 56, 60, 64], [100, 104, 108, 112], [116, 120, 124, 128], [132, 136, 140, 144], [149, 153, 157, 161]];
    for (const block of blocks) {
      const rows = block.map((c) => band5.find((r) => r.channel === c)).filter(Boolean);
      if (rows.length < 3) continue;
      const rates = rows.map(rate);
      const min = Math.min(...rates), max = Math.max(...rates);
      if (min >= 500 && min >= max * 0.5) {
        const primary = rows.reduce((a, b) => (b.networks > a.networks ? b : a));
        lines.push(`Channels ${rows[0].channel}-${rows[rows.length - 1].channel} all carry ${f0(min)}-${f0(max)} frames/s with only ${Math.max(...rows.map((r) => r.networks))} network${Math.max(...rows.map((r) => r.networks)) === 1 ? "" : "s"}: that is data traffic on one 80 MHz-wide channel, not beacons, with its primary on ${primary.channel}${surveyStrongest(primary) ? ` (${surveyStrongest(primary).ssid || "hidden network"})` : ""}.`);
      }
    }
    const q = band5.reduce((a, b) => (rate(b) < rate(a) || (rate(b) === rate(a) && b.networks < a.networks)) ? b : a);
    lines.push(`Pick: channel ${q.channel} (${f0(rate(q))} frames/s, ${plural(q.networks, "network")}), the quietest 5 GHz channel surveyed. 5 GHz 20 MHz channels don't overlap, but a wide-channel neighbour still covers several of them.`);
    out.push({ title: "5 GHz router channel", lines });
  }

  // One loud radio heard across many channels is spill, not many networks.
  const bySsid = new Map();
  for (const r of heard) {
    const t = surveyStrongest(r);
    if (!t) continue;
    const key = t.ssid || `hidden network ${t.bssid}`;
    const e = bySsid.get(key) ?? { channels: [], best: null, bestCh: null };
    e.channels.push(r.channel);
    if (e.best === null || (t.signal ?? -1e9) > e.best) { e.best = t.signal; e.bestCh = r.channel; }
    bySsid.set(key, e);
  }
  const spill = [...bySsid.entries()].filter(([, e]) => e.channels.length >= 4).sort((a, b) => b[1].channels.length - a[1].channels.length);
  const hidden = heard.filter((r) => surveyStrongest(r) && !surveyStrongest(r).ssid).map((r) => r.channel);
  if (spill.length || hidden.length) {
    const lines = [];
    for (const [name, e] of spill.slice(0, 3)) {
      lines.push(`${name} is the strongest network on ${e.channels.length} channels (${e.channels.join(", ")}; best raw ${e.best ?? "?"} on ${e.bestCh}): one strong radio spilling into neighbouring channels, not ${e.channels.length} networks.`);
    }
    if (hidden.length) lines.push(`A hidden network (no name in its beacons) is the strongest on channel${hidden.length === 1 ? "" : "s"} ${hidden.join(", ")}.`);
    out.push({ title: "Networks", lines });
  }

  out.push({ title: "Limits", lines: [
    "One adapter, one channel at a time, about ten seconds each: a quiet sample can miss a bursty network.",
    "Frames per second stands in for air time; one large data frame occupies more air than a beacon.",
    "Signal values in parentheses are the adapter's raw codes, comparable with each other but not dBm.",
  ] });
  return out;
}

/** Plain text of surveyFindings() for the Copy button. */
export function findingsText(findings, status) {
  const head = status?.started_at ? `msdr Wi-Fi channel survey · ${new Date(status.started_at * 1000).toLocaleString()}` : "msdr Wi-Fi channel survey";
  return [head, ...findings.flatMap((f) => ["", f.title, ...f.lines.map((l) => `- ${l}`)])].join("\n");
}

/** Animated marker: a pulsing dot while a channel is heard, a spinner while
 * the helper restarts on the next one (styles.css .wifi-survey-indicator). */
function surveyIndicator(phase) {
  return phase === "listening" || phase === "switching"
    ? html`<span class="wifi-survey-indicator ${phase}" aria-hidden="true"></span>` : "";
}

export const SURVEY_HONESTY_TEXT =
  "Each channel is heard only during its dwell. Not a spectrum measurement.";

/** 2.4 GHz channels are 1-14; everything else surveyed here is 5 GHz. */
export function surveyBand(channel) {
  return channel <= 14 ? "2.4 GHz" : "5 GHz";
}

/** Frames per second actually listened on this channel (0 when nothing was
 * listened yet -- never NaN/Infinity, which would break the frames/s bar's
 * width). */
export function surveyFrameRate(result) {
  return result.listened_s > 0 ? result.frames / result.listened_s : 0;
}

/** The strongest network heard on this channel's visits, or null -- `top`
 * is already strongest-first (server-sorted by signal), so this is just its
 * first entry. */
export function surveyStrongest(result) {
  return result.top?.[0] ?? null;
}

export const SURVEY_COLUMNS = [
  { label: "Channel", key: "channel", first: "asc",
    help: "Wi-Fi channel number the adapter was tuned to for this row." },
  { label: "Status", key: "status", first: "asc",
    help: "pending: not visited yet · switching: the helper is restarting on it · listening: counting now · done: visited · failed: the helper could not start on it (hover the row for why)." },
  { label: "Band", key: "band", first: "asc",
    help: "2.4 GHz (channels 1-14) or 5 GHz. Only 1, 6 and 11 don't overlap on 2.4 GHz; 5 GHz 20 MHz channels don't overlap each other." },
  { label: "Listened", key: "listened_s", first: "desc",
    help: "Seconds the adapter was actually receiving on this channel (helper restart time excluded)." },
  { label: "Frames", key: "frames", first: "desc",
    help: "Frames received while tuned here: beacons, probe responses and data from any device whose signal lines up with this channel." },
  { label: "Frames/s", key: "rate", first: "desc",
    help: "Frames per listened second on this exact channel. Understates a 2.4 GHz channel's real traffic: neighbouring channels' frames overlap its frequencies but don't decode here, so they aren't counted." },
  { label: "Load (±4)", key: "load", first: "desc",
    help: "How busy this channel's air is. Frames/s of the channel plus every 2.4 GHz channel within ±4, whose 20 MHz signals overlap it. Use this, not Frames/s, to judge a 2.4 GHz channel; on 5 GHz it equals Frames/s." },
  { label: "Networks", key: "networks", first: "desc",
    help: "Distinct access points (BSSIDs) whose frames arrived while tuned here. A strong neighbour can be heard on several channels, so one radio may count on each." },
  { label: "Strongest", key: "strongest", first: "desc",
    help: "The loudest network heard on this channel and its signal: the adapter's raw code, comparable between rows but not dBm. Click the row for the full list." },
];

// Status sort: what's happening now first, then finished, then still to do.
const SURVEY_STATUS_ORDER = { listening: 0, switching: 1, failed: 2, done: 3, pending: 4 };

const SURVEY_SORT_KEYS = {
  channel: (r) => r.channel,
  status: (r) => SURVEY_STATUS_ORDER[r.status] ?? 4,
  band: (r) => surveyBand(r.channel),
  listened_s: (r) => r.listened_s,
  frames: (r) => r.frames,
  rate: (r) => surveyFrameRate(r),
  load: (r) => r.load ?? null,
  networks: (r) => r.networks,
  strongest: (r) => surveyStrongest(r)?.signal ?? null,
};

/** Next {sort, order} for the survey results table -- same rule as
 * nextSort() above, over SURVEY_COLUMNS instead of WIFI_COLUMNS. */
export function nextSurveySort(current, key) {
  const col = SURVEY_COLUMNS.find((c) => c.key === key);
  if (!col) return current;
  if (current.sort !== key) return { sort: key, order: col.first };
  return { sort: key, order: current.order === "asc" ? "desc" : "asc" };
}

/** Per-channel survey results, sorted for display -- null/missing values
 * (e.g. "strongest" on a channel with no networks yet) sort last in both
 * directions, channel number breaks ties so rows never jitter. */
export function sortSurveyResults(results, sort) {
  const key = SURVEY_SORT_KEYS[sort?.sort] ?? SURVEY_SORT_KEYS.channel;
  const order = sort?.order ?? "asc";
  const rows = [...(results ?? [])].sort((a, b) => a.channel - b.channel);
  const present = rows.filter((r) => key(r) !== null && key(r) !== undefined);
  const missing = rows.filter((r) => key(r) === null || key(r) === undefined);
  present.sort((a, b) => {
    const [ka, kb] = [key(a), key(b)];
    const cmp = ka < kb ? -1 : ka > kb ? 1 : 0;
    return order === "desc" ? -cmp : cmp;
  });
  return [...present, ...missing];
}

/** The progress line (docs/history.md: "round 1 of 1 · channel 44 · 6 s
 * left"). `nowS` is injectable so this stays deterministic in tests. */
export function surveyProgressText(status, nowS = Date.now() / 1000) {
  if (!status || status.state === "idle") return "";
  if (status.state === "stopping") return "Stopping… restoring the previous capture";
  if (status.state === "running") {
    const parts = [
      status.rounds ? `round ${status.round} of ${status.rounds}` : `round ${status.round}`,
    ];
    if (status.phase === "switching") {
      parts.push(`switching to channel ${status.current_channel}…`);
    } else if (status.current_channel != null) {
      const left = status.channel_ends_at != null
        ? ` · ${Math.max(0, Math.round(status.channel_ends_at - nowS))} s left` : "";
      parts.push(`listening on channel ${status.current_channel}${left}`);
    }
    if (status.started_at != null) {
      const elapsed = formatDuration(nowS - status.started_at);
      parts.push(status.estimated_total_s != null
        ? `${elapsed} of ~${formatDuration(status.estimated_total_s)}` : `${elapsed} elapsed`);
    }
    return parts.join(" · ");
  }
  if (status.state === "done") {
    const failed = status.failed_channels ?? 0;
    return failed ? `Survey complete; ${failed} channel${failed === 1 ? "" : "s"} could not be started (hover the row for why).` : "Survey complete.";
  }
  if (status.state === "stopped") return "Survey stopped.";
  if (status.state === "failed") return status.error ? `Survey failed: ${status.error}` : "Survey failed.";
  return "";
}

/** m:ss for the survey's elapsed/estimate. */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Before Start: roughly how long a survey takes -- dwell plus one helper
 * restart per channel per round (the server refines the restart time once
 * it has measured one). Null when rounds is 0 (until stopped). */
export function surveyEstimateText(channelCount, dwellS, rounds, switchS = 5) {
  if (!channelCount || !rounds) return null;
  const total = channelCount * rounds * (dwellS + switchS);
  return `about ${formatDuration(total)} for ${channelCount} channel${channelCount === 1 ? "" : "s"}`
    + (rounds > 1 ? ` × ${rounds} rounds` : "");
}

/** rows with a label whose class is `ignore` dropped when hideIgnored is on
 * (docs/history.md: "their tables show the label and a 'hide ignored'
 * filter"). Client-side only -- labels live in a separate fetch from the
 * paginated network list, so this never changes `total`/paging math. */
export function visibleRows(rows, labels, hideIgnored) {
  if (!hideIgnored) return rows;
  return (rows ?? []).filter((r) => labels.get(r.bssid)?.class !== "ignore");
}

/** Merge newly fetched live frames into the on-screen list (newest first),
 * dropping duplicates and anything at or below the last sequence seen. */
export function mergeLiveFrames(current, incoming, lastSeq, keep = LIVE_KEEP) {
  const fresh = (incoming ?? []).filter((f) => f.seq > lastSeq);
  if (!fresh.length) return { frames: current, lastSeq };
  const frames = [...fresh.reverse(), ...current].slice(0, keep);
  return { frames, lastSeq: Math.max(lastSeq, ...fresh.map((f) => f.seq)) };
}

/** One line of the live feed, in words. */
export function liveFrameText(f) {
  const who = f.ssid ? `“${f.ssid}”` : f.bssid ?? f.source ?? "";
  const channel = f.channel != null ? `ch ${f.channel}` : "";
  const signal = f.signal ? (f.signal.kind === "dbm" ? `${f.signal.value} dBm` : `raw ${f.signal.value}`) : "";
  return [f.name, who, channel, signal, `${f.length} B`, f.bad_fcs ? "bad checksum" : ""]
    .filter(Boolean).join(" · ");
}

const LIMIT = 100;
const GAP_S = 10;
const FILTER_DEBOUNCE_MS = 300;

const STATE_LABELS = {
  unavailable: "helper not installed",
  dependency_missing: "a helper dependency is missing",
  no_device: "no compatible adapter detected",
  stopped: "stopped",
  starting: "starting…",
  receiving: "receiving",
  stale: "receiving (no frames recently)",
  failed: "failed",
};

const SUBTYPE_LABELS = { 8: "beacon", 5: "probe response" };

/** epoch seconds -> local "HH:MM", "--:--" when missing/invalid. Deliberately
 * local (not format.js's epochHM, which is identical) -- kept here so this
 * view has no dependency on a file another agent is editing in parallel
 * (format.js, see docs/wifi.md's SECTION_IDS note). */
export function epochHHMM(ts) {
  if (ts === null || ts === undefined) return "--:--";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "--:--";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** epoch seconds -> "37m ago" / "5h ago" / "3d ago" / "--" when missing,
 * relative to `atS` (injectable, defaults to now) so it stays deterministic
 * in tests. */
export function relativeAge(ts, atS = Date.now() / 1000) {
  if (ts === null || ts === undefined) return "--";
  const s = Math.max(0, Math.round(atS - ts));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Network row's SSID cell text -- "hidden network" (italic, dim) whenever
 * ssid_hidden or the ssid came back empty, never a blank cell. */
export function ssidLabel(row) {
  if (row.ssid_hidden || !row.ssid) {
    const seen = row.revealed_ssids ?? [];
    return { text: seen.length ? `hidden (seen as ${seen.join(", ")})` : "hidden network", hidden: true };
  }
  return { text: row.ssid, hidden: false };
}

/** Grouping key for "same access point (probable)": routers usually derive
 * extra BSSIDs from their base MAC by changing the last digits and/or
 * setting the locally-administered bit (0x02 of the first byte), so ignore
 * that bit and the last hex digit. A heuristic -- shown as "probable". */
export function apGroupKey(bssid) {
  const octets = (bssid ?? "").toLowerCase().split(":");
  if (octets.length !== 6) return bssid;
  const first = (parseInt(octets[0], 16) & ~0x02).toString(16).padStart(2, "0");
  return [first, ...octets.slice(1, 5), octets[5][0]].join(":");
}

/** Network name a row belongs to: its SSID, or a hidden network's revealed
 * name, else null (hidden, name never seen). */
function networkName(row) {
  if (!row.ssid_hidden && row.ssid) return row.ssid;
  return (row.revealed_ssids ?? [])[0] ?? null;
}

/** Rows grouped for display: mode "name" (network name) or "ap" (probable
 * access point). Groups keep the rows' existing order; returns null for no
 * grouping. */
export function groupNetworks(rows, mode) {
  if (mode !== "name" && mode !== "ap") return null;
  const groups = new Map();
  for (const row of rows ?? []) {
    const key = mode === "name" ? (networkName(row) ?? "\u0000hidden") : apGroupKey(row.bssid);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return [...groups.entries()].map(([key, members]) => {
    const channels = [...new Set(members.map((r) => r.channel).filter((c) => c != null))].sort((a, b) => a - b);
    const names = [...new Set(members.map(networkName).filter(Boolean))];
    const vendor = members.map((r) => r.vendor).find(Boolean) ?? null;
    const title = mode === "name"
      ? (key === "\u0000hidden" ? "Hidden, name not seen" : key)
      : `${vendor ?? "Unknown maker"} · ${names.length ? names.join(", ") : "hidden networks"}`;
    const detail = `${members.length} BSSID${members.length === 1 ? "" : "s"}`
      + (channels.length ? ` · channel${channels.length === 1 ? "" : "s"} ${channels.join(", ")}` : "")
      + (mode === "ap" && members.length > 1 ? " · probably one access point" : "");
    return { key, title, detail, rows: members };
  });
}

/** Channel cell: the number plus a "configured" marker when the DS IE wasn't
 * available (channel_source: "configured") and a mismatch marker when a
 * frame's own DS channel disagreed with the receiver's tuned channel --
 * see docs/wifi.md's radiotap-channel limitation. */
export function channelMarkers(row) {
  const marks = [];
  if (row.channel_source === "configured") {
    marks.push({ text: "configured", title: "DS Parameter Set channel unavailable; the receiver's configured channel is shown instead" });
  }
  if (row.channel_mismatch) {
    marks.push({ text: "mismatch", title: "This network's DS Parameter Set channel differs from the receiver's configured channel" });
  }
  return marks;
}

/** Signal cell -- raw PHY codes are uncalibrated and relative (docs/wifi.md
 * "Limitations"), so they're never shown as if they were dBm. Returns null
 * when nothing was reported (caller renders "-- not reported"). */
/** The value the table shows and sorts by: the server's median of recent
 * frames (one odd frame can't dominate), else the latest. */
export function typicalSignal(signal) {
  return signal?.median ?? signal?.latest ?? null;
}

/** Networks heard fewer times than this get no signal bar and are dimmed:
 * a handful of stray frames is not a reliable strength. */
export const RARE_PACKETS = 10;

export function isRare(row) {
  return (row?.beacons ?? 0) + (row?.probe_responses ?? 0) < RARE_PACKETS;
}

export function signalCellText(signal) {
  const value = typicalSignal(signal);
  if (value === null) return null;
  const basis = signal.samples > 1 ? ` (median of the last ${Math.min(signal.samples, 30)} frames)` : "";
  if (signal.kind === "dbm") return { text: `${value} dBm`, title: basis ? `Signal${basis}` : null };
  return { text: `raw ${value}`, title: `Uncalibrated raw PHY status code${basis} — relative, not a calibrated signal strength` };
}

/** Signal bar fill, 0-100, same look as the BLE RSSI bar. dBm uses the BLE
 * scale (-100..-30 dBm). A raw code has no known scale, so it's placed
 * between the weakest and strongest raw codes in `rawRange` ([min, max] of
 * the rows on screen) -- relative only, never a strength claim. */
export function signalBarPct(signal, rawRange = null) {
  const value = typicalSignal(signal);
  if (value === null) return null;
  const clamp = (v) => Math.max(0, Math.min(100, v));
  if (signal.kind === "dbm") return clamp(((value + 100) / 70) * 100);
  if (!rawRange) return null;
  const [lo, hi] = rawRange;
  return hi > lo ? clamp(((value - lo) / (hi - lo)) * 100) : 100;
}

/** [min, max] typical raw code across rows heard often enough (rare rows
 * don't set the scale), or null when there are none. */
export function rawSignalRange(rows) {
  const raw = (rows ?? []).filter((r) => !isRare(r)).map((r) => r.signal)
    .filter((s) => s?.kind === "raw" && typicalSignal(s) !== null).map(typicalSignal);
  return raw.length ? [Math.min(...raw), Math.max(...raw)] : null;
}

/** [[ts, value], ...] -> an array of SVG `points` strings, one per
 * contiguous run; a gap of more than `gapS` seconds between consecutive
 * samples starts a new segment (rendered as a visible break, never a
 * straight line across the gap). Empty when fewer than 2 usable points. */
export function sparklineSegments(history, width = 120, height = 24, gapS = GAP_S) {
  const pts = (history ?? []).filter(([ts, v]) => ts !== null && ts !== undefined && v !== null && v !== undefined);
  if (pts.length < 2) return [];
  const ts0 = pts[0][0];
  const span = Math.max(pts[pts.length - 1][0] - ts0, 1);
  const values = pts.map(([, v]) => v);
  const vMin = Math.min(...values);
  const vSpan = Math.max(Math.max(...values) - vMin, 1e-9);
  const segments = [];
  let current = [];
  for (let i = 0; i < pts.length; i++) {
    const [ts, v] = pts[i];
    if (i > 0 && ts - pts[i - 1][0] > gapS) {
      if (current.length > 1) segments.push(current);
      current = [];
    }
    const x = ((ts - ts0) / span) * width;
    const y = height - ((v - vMin) / vSpan) * height;
    current.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  if (current.length > 1) segments.push(current);
  return segments.map((seg) => seg.join(" "));
}

/** What the table area shows instead of rows, keyed off the receiver status
 * (docs/wifi.md UI contract: "helper not installed", "stopped", "receiving
 * but no networks yet", "failed with last_error") -- null when rows should
 * render normally. `setup` is true when the message should offer a link to
 * Setup's Wi-Fi section. */
export function emptyStateFor(status, rowCount, viewingSession) {
  if (viewingSession) {
    if (rowCount > 0) return null;
    return { text: "No networks recorded in this session.", setup: false };
  }
  if (!status) return { text: "Loading receiver status…", setup: false };
  if (status.state === "unavailable" || status.state === "dependency_missing") {
    return { text: `Wi-Fi receiver unavailable: ${STATE_LABELS[status.state]}.`, setup: true };
  }
  if (status.state === "no_device") {
    return { text: "No compatible Wi-Fi adapter detected.", setup: true };
  }
  // Stop keeps the networks heard so far on screen; the next Start replaces
  // the table (WifiSupervisor.start builds a new NetworkTable).
  if (status.state === "stopped" && rowCount > 0) return null;
  if (status.state === "stopped") {
    return { text: "Wi-Fi receiver is stopped. Choose a channel and press Start above.", setup: false };
  }
  if (status.state === "failed") {
    return { text: status.last_error ? `Capture failed: ${status.last_error}` : "Capture failed.", setup: true };
  }
  if (["starting", "receiving", "stale"].includes(status.state) && rowCount === 0) {
    return { text: "Receiving — no networks observed yet.", setup: false };
  }
  return null;
}

/** Discover -> Wi-Fi: networks heard on the monitored channel (live receiver,
 * or a saved session when `source` is set to "session:<id>" -- see
 * review-view.js's "Inspect"). Never an inventory: docs/wifi.md's header
 * wording and empty states both say so explicitly. */
export class WifiView extends LitElement {
  static properties = {
    source: { type: String },
    settings: { type: Object },
    _status: { state: true },
    _rows: { state: true },
    _total: { state: true },
    _configuredChannel: { state: true },
    _since: { state: true },
    _q: { state: true },
    _channelFilter: { state: true },
    _offset: { state: true },
    _selectedBssid: { state: true },
    _detail: { state: true },
    _frameHex: { state: true },
    _error: { state: true },
    _loading: { state: true },
    _view: { state: true }, // "networks" | "live"
    _liveFrames: { state: true },
    _livePaused: { state: true },
    _liveKind: { state: true },
    _sort: { state: true },
    _channel: { state: true },
    _controlBusy: { state: true },
    _controlError: { state: true },
    _surveyStatus: { state: true },
    _surveyPreset: { state: true },
    _surveyChannelsText: { state: true },
    _surveyDwell: { state: true },
    _surveyRounds: { state: true },
    _surveySort: { state: true },
    _surveySelectedChannel: { state: true },
    _surveyBusy: { state: true },
    _surveyError: { state: true },
    _labels: { state: true },
    _hideIgnored: { state: true },
    _summary: { state: true }, // GET /api/wifi/summary -- networks by band/security/maker
    _findingsCopied: { state: true },
    _group: { state: true }, // "" | "name" | "ap" -- groupNetworks()
    _detailHistory: { state: true },
    _activityData: { state: true },
    _activityRange: { state: true },
    _activityMode: { state: true },
    _activityError: { state: true },
    _activityLoading: { state: true },
  };

  createRenderRoot() { return this; }

  constructor() {
    super();
    this.source = null;
    this.settings = null;
    this._status = null;
    this._rows = [];
    this._total = 0;
    this._configuredChannel = null;
    this._since = null;
    this._q = "";
    this._channelFilter = "";
    this._offset = 0;
    this._selectedBssid = null;
    this._detail = null;
    this._frameHex = {};
    this._error = null;
    this._loading = true;
    this._reqSeq = 0;
    this._detailSeq = 0;
    this._filterTimer = null;
    this._pollTimer = null;
    this._view = "networks";
    this._sort = { ...DEFAULT_SORT };
    this._channel = null; // chosen channel; falls back to status/settings
    this._controlBusy = "";
    this._controlError = null;
    this._liveFrames = [];
    this._liveSeq = 0;
    this._livePaused = false;
    this._liveKind = "all";
    this._liveToken = 0;
    this._surveyStatus = null;
    this._surveyPreset = "24_1_6_11";
    this._surveyChannelsText = presetChannels("24_1_6_11").join(",");
    this._surveyDwell = 10;
    this._surveyRounds = 1;
    this._surveySort = { sort: "channel", order: "asc" };
    this._surveySelectedChannel = null;
    this._surveyBusy = false;
    this._surveyError = null;
    this._labels = new Map(); // bssid -> label, from one listLabels("wifi") fetch
    this._hideIgnored = true; // docs/history.md: ignore hidden by default
    this._summary = null;
    this._group = "";
    this._detailHistory = null;
    this._activityData = null;
    this._activityRange = 60; // minutes; ACTIVITY_RANGE_OPTIONS default "1 h"
    this._activityMode = "packets";
    this._activityError = null;
    this._activityLoading = true;
    this._activityTimer = null;
  }

  async _refreshLive() {
    if (this._view !== "live" || this._livePaused || this._viewingSession()) return;
    const token = ++this._liveToken;
    try {
      const resp = await api.getWifiLive({ after: this._liveSeq, kind: this._liveKind });
      if (token !== this._liveToken) return; // a newer request owns the list
      // The server restarted its ring (new capture): start over from its sequence.
      if (resp.seq < this._liveSeq) { this._liveSeq = 0; this._liveFrames = []; }
      const merged = mergeLiveFrames(this._liveFrames, resp.items, this._liveSeq);
      this._liveFrames = merged.frames;
      this._liveSeq = merged.lastSeq;
    } catch (err) {
      if (token === this._liveToken) this._error = err.message;
    }
  }

  _setView(view) {
    this._view = view;
    if (view === "live") this._refreshLive();
    if (view === "survey") this._refreshSurveyStatus();
    if (view === "activity") this._refreshActivity();
  }

  _renderLive() {
    const frames = this._liveFrames;
    return html`<div class="wifi-live">
      <div class="control-row wifi-live-controls">
        <button aria-pressed=${this._livePaused ? "true" : "false"}
          @click=${() => { this._livePaused = !this._livePaused; if (!this._livePaused) this._refreshLive(); }}>
          ${this._livePaused ? "Resume" : "Pause"}</button>
        <button @click=${() => { this._liveFrames = []; }}>Clear frames</button>
        <label>Show <select @change=${(e) => { this._liveKind = e.target.value; this._liveSeq = 0; this._liveFrames = []; this._refreshLive(); }}>
          <option value="all" ?selected=${this._liveKind === "all"}>all frames</option>
          <option value="management" ?selected=${this._liveKind === "management"}>management (beacons, probes…)</option>
          <option value="control" ?selected=${this._liveKind === "control"}>control (ACK, RTS, CTS…)</option>
          <option value="data" ?selected=${this._liveKind === "data"}>data</option>
        </select></label>
        <span class="dim">${this._livePaused ? "paused" : "updating every second"} · newest first · last ${LIVE_KEEP} kept · not saved</span>
      </div>
      ${frames.length
        ? html`<ol class="wifi-live-list mono" aria-live="off">${frames.map((f) => html`<li class=${f.bad_fcs ? "bad" : ""}>
            <span class="dim">${new Date(f.ts * 1000).toLocaleTimeString()}</span>
            <span class="wifi-live-kind">${f.kind}</span>
            <span>${liveFrameText(f)}</span></li>`)}</ol>`
        : html`<p class="empty">${this._status?.state === "receiving" ? "Waiting for frames…" : "Start the Wi-Fi receiver in Setup to see frames as they arrive."}</p>`}
    </div>`;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refreshRows();
    this._refreshLabels();
    if (!this._viewingSession()) {
      this._refreshStatus();
      if (this._view === "survey") this._refreshSurveyStatus();
    }
    this._pollTimer = setInterval(() => {
      this._refreshLive();
      this._refreshRows();
      if (!this._viewingSession()) {
        this._refreshStatus();
        // Poll while the tab is open, or a survey is running even if the
        // operator switched tabs -- it must not look stuck at "running".
        if (this._view === "survey" || ["running", "stopping"].includes(this._surveyStatus?.state)) {
          this._refreshSurveyStatus();
        }
      }
    }, 1000);
    // Activity (docs/wifi.md "Wi-Fi activity"): a read of the history
    // database, refreshed only while its own tab is visible -- never
    // starts/stops reception, so it keeps running (or not) independent of
    // the receiver's own 1 s poll above.
    this._activityTimer = setInterval(() => {
      if (this._view === "activity" && !this._viewingSession()) this._refreshActivity();
    }, 5000);
  }

  disconnectedCallback() {
    clearInterval(this._pollTimer);
    clearInterval(this._activityTimer);
    clearTimeout(this._filterTimer);
    super.disconnectedCallback();
  }

  updated(changed) {
    if (changed.has("source")) {
      this._offset = 0;
      this._refreshRows();
    }
  }

  _viewingSession() {
    return typeof this.source === "string" && this.source.startsWith("session:");
  }

  async _refreshStatus() {
    try {
      this._status = await api.getWifiStatus();
    } catch (err) {
      this._error = err.message;
    }
  }

  /** One listLabels("wifi") fetch shared by the table's Label column and the
   * hide-ignored filter; re-run whenever device-label-editor reports a
   * change instead of refetching per row. */
  async _refreshLabels() {
    try {
      const { items } = await api.listLabels("wifi");
      this._labels = new Map((items ?? []).map((l) => [l.ident, l]));
    } catch (err) {
      this._error = err.message;
    }
  }

  _onLabelChanged() {
    this._refreshLabels();
  }

  /** Every list request carries its own sequence token -- a slow response
   * for a filter/page the operator already changed away from must never
   * overwrite what a newer request already fetched (same pattern as
   * detect-readout.js's allocation lookups). */
  async _refreshRows() {
    const seq = ++this._reqSeq;
    try {
      const params = {
        q: this._q || undefined,
        channel: this._channelFilter || undefined,
        offset: this._offset,
        limit: LIMIT,
        source: this.source || undefined,
        sort: (this._sort ?? DEFAULT_SORT).sort,
        order: (this._sort ?? DEFAULT_SORT).order,
      };
      const [data, summary] = await Promise.all([
        api.listWifiNetworks(params),
        // Live table only; never lets a summary failure break the table.
        this._viewingSession() ? null : Promise.resolve().then(() => api.wifiSummary()).catch(() => null),
      ]);
      this._summary = summary;
      if (seq !== this._reqSeq) return; // a newer request already landed
      this._rows = data.items ?? [];
      this._total = data.total ?? 0;
      this._configuredChannel = data.configured_channel ?? null;
      this._since = data.since ?? null;
      this._error = null;
      this._loading = false;
    } catch (err) {
      if (seq !== this._reqSeq) return;
      this._error = err.message;
      this._loading = false;
    }
  }

  async _fetchDetail(bssid) {
    const seq = ++this._detailSeq;
    try {
      const detail = await api.getWifiNetwork(bssid, this.source || undefined);
      if (seq !== this._detailSeq) return;
      this._detail = detail;
    } catch (err) {
      if (seq !== this._detailSeq) return;
      // Never leave the pane on "loading…": say what failed.
      this._detail = { bssid, error: err.message };
    }
  }

  async _fetchDetailHistory(bssid) {
    const seq = this._detailSeq;
    try {
      const data = await api.getDeviceHistory("wifi", bssid, {
        hours: LABEL_HISTORY_HOURS, bucket: autoBucketS(LABEL_HISTORY_HOURS * 3600),
      });
      if (seq !== this._detailSeq) return;
      this._detailHistory = data;
    } catch {
      if (seq !== this._detailSeq) return;
      this._detailHistory = null; // history is a nice-to-have in this pane; never block the network detail on it
    }
  }

  _select(bssid) {
    this._selectedBssid = this._selectedBssid === bssid ? null : bssid;
    this._frameHex = {};
    this._detailHistory = null;
    if (this._selectedBssid) {
      this._fetchDetail(this._selectedBssid);
      // History is live-only (docs/history.md): a saved session has no
      // ongoing bucket recording to query.
      if (!this._viewingSession()) this._fetchDetailHistory(this._selectedBssid);
    } else {
      this._detail = null;
    }
  }

  _setQuery(value) {
    this._q = value;
    this._offset = 0;
    clearTimeout(this._filterTimer);
    this._filterTimer = setTimeout(() => this._refreshRows(), FILTER_DEBOUNCE_MS);
  }

  _setChannelFilter(value) {
    this._channelFilter = value;
    this._offset = 0;
    this._refreshRows();
  }

  _page(delta) {
    this._offset = Math.max(0, this._offset + delta * LIMIT);
    this._refreshRows();
  }

  async _showFrame(n) {
    if (!this._selectedBssid) return;
    try {
      const { hex } = await api.getWifiFrame(this._selectedBssid, n, this.source || undefined);
      this._frameHex = { ...this._frameHex, [n]: hex };
    } catch (err) {
      this._error = err.message;
    }
  }

  _openSetup() {
    this.dispatchEvent(new CustomEvent("open-wifi-setup", { bubbles: true, composed: true }));
  }

  async _toggleRecording() {
    this._error = null;
    try {
      if (this._status?.recording) await api.stopWifiSession();
      else await api.startWifiSession();
      await this._refreshStatus();
    } catch (err) {
      this._error = err.message;
    }
  }

  _currentChannel() {
    return Number(this._channel ?? this._status?.channel ?? this.settings?.channel ?? 6);
  }

  async _control(label, action) {
    this._controlBusy = label;
    this._controlError = null;
    try {
      await action();
      await this._refreshStatus();
    } catch (err) {
      this._controlError = `${label} failed: ${err.message}`;
    } finally {
      this._controlBusy = "";
    }
  }

  _start() {
    const channel = this._currentChannel();
    return this._control("Start", async () => {
      // Pressing Start is the intent to receive: enable Wi-Fi if it isn't yet.
      if (!this.settings?.enabled) {
        await api.patchSettings({ wifi: { ...(this.settings ?? {}), enabled: true, channel } });
        this.dispatchEvent(new CustomEvent("settings-saved", { bubbles: true, composed: true }));
      }
      await api.startWifi(channel);
    });
  }

  _stop() { return this._control("Stop", () => api.stopWifi()); }

  _changeChannel(value) {
    this._channel = Number(value);
    // Changing channel while receiving restarts capture on the new channel.
    if (RUNNING.includes(this._status?.state)) {
      return this._control("Change channel", async () => { await api.stopWifi(); await api.startWifi(this._channel); });
    }
  }

  _clearNetworks() {
    return this._control("Clear", async () => {
      await api.clearWifiNetworks();
      this._selectedBssid = null; this._detail = null; this._offset = 0;
      await this._refreshRows();
    });
  }

  _renderReceiverControls() {
    const s = this._status;
    const running = RUNNING.includes(s?.state);
    const blocker = running ? null : wifiStartBlocker(s);
    const channel = this._currentChannel();
    const busy = !!this._controlBusy;
    const mhz = wifiChannelMHz(channel);
    return html`<span class="ctl-label">Receiver</span>
      <div class="wifi-controls control-row">
      <span class=${`state-pill${running ? " on" : blocker ? " warn" : ""}`}>${s ? `${STATE_LABELS[s.state] ?? s.state}${running ? ` · ${(s.frame_rate ?? 0).toFixed(0)} frames/s` : ""}` : "Loading…"}</span>
      ${running
        ? html`<button class="wifi-stop" ?disabled=${busy} @click=${() => this._stop()}>${this._controlBusy === "Stop" ? "Stopping…" : "Stop"}</button>`
        : html`<button class="wifi-start" ?disabled=${busy || !!blocker} title=${blocker ?? "Start passive capture on this channel"}
            @click=${() => this._start()}>${this._controlBusy === "Start" ? "Starting…" : "Start"}</button>`}
      <button ?disabled=${busy} data-help="Empty the network list; capture keeps running" @click=${() => this._clearNetworks()}>Clear networks</button>
      <span class="ctl-sep" aria-hidden="true"></span>
      <label>Channel <select aria-label="Wi-Fi channel" ?disabled=${busy}
        @change=${(e) => this._changeChannel(e.target.value)}>
        <optgroup label="2.4 GHz">${CHANNELS_24.map((c) => html`<option value=${c} ?selected=${c === channel}>${c}</option>`)}</optgroup>
        <optgroup label="5 GHz (20 MHz)">${CHANNELS_5.map((c) => html`<option value=${c} ?selected=${c === channel}>${c}</option>`)}</optgroup>
      </select></label>
      ${mhz ? html`<span class="chan-hl">${mhz} MHz</span>` : ""}
      <span class="dim">since ${epochHHMM(this._since)}</span>
      ${blocker && !running ? html`<span class="wifi-problem">${blocker}</span>` : ""}
      ${this._controlError ? html`<span class="wifi-problem" role="alert">${this._controlError}</span>` : ""}
    </div>`;
  }

  _renderRecordControl() {
    const rec = this._status?.recording;
    const maxMb = this.settings?.record_max_mb;
    const maxMin = this.settings?.record_max_minutes;
    return html`<span class="ctl-label">Record</span>
      <div class="wifi-record control-row">
      <button @click=${() => this._toggleRecording()}>${rec ? "STOP recording" : "Record packets"}</button>
      ${rec
        ? html`<span class="dim">session ${rec.session_id} · ${((rec.bytes ?? 0) / 1e6).toFixed(1)} MB · ${rec.frames ?? 0} frames</span>`
        : (maxMb != null || maxMin != null)
          ? html`<span class="dim">max ${maxMb ?? "--"} MB / ${maxMin ?? "--"} min</span>`
          : ""}
    </div>`;
  }

  // --- activity heatmap (docs/wifi.md "Wi-Fi activity") -------------------
  // A read of the history database (same one the Networks/Label history
  // pane reads), never the receiver -- opening or refreshing this tab must
  // never start or stop capture.

  async _refreshActivity() {
    this._activityLoading = this._activityData === null;
    try {
      const data = await api.getWifiActivity({ minutes: this._activityRange });
      this._activityData = data;
      if (!hasSignalMode(data.rows) && this._activityMode === "signal") {
        this._activityMode = "packets";
      }
      this._activityError = null;
    } catch (err) {
      this._activityError = err.message;
    } finally {
      this._activityLoading = false;
    }
  }

  _setActivityRange(minutes) {
    this._activityRange = minutes;
    this._refreshActivity();
  }

  _setActivityMode(mode) {
    this._activityMode = mode;
  }

  _renderActivity() {
    const data = this._activityData;
    const signalAvailable = hasSignalMode(data?.rows);
    return html`<div class="wifi-activity">
      <div class="wifi-activity-controls control-row">
        <label>Range <select @change=${(e) => this._setActivityRange(Number(e.target.value))}>
          ${ACTIVITY_RANGE_OPTIONS.map((r) => html`<option value=${r.minutes} ?selected=${r.minutes === this._activityRange}>${r.label}</option>`)}
        </select></label>
        ${signalAvailable ? html`<label>Colour
          <select @change=${(e) => this._setActivityMode(e.target.value)}>
            <option value="packets" ?selected=${this._activityMode === "packets"}>packets / listened second</option>
            <option value="signal" ?selected=${this._activityMode === "signal"}>uncalibrated signal code (not dBm)</option>
          </select></label>` : ""}
        <span class="dim">updates every 5 s while this tab is open</span>
        <span class="help-mark" tabindex="0" role="note" aria-label="What this shows" title=${ACTIVITY_HELP} data-help=${ACTIVITY_HELP}>?</span>
      </div>
      ${this._activityError ? html`<p class="wifi-problem" role="alert">${this._activityError}</p>` : ""}
      ${this._activityLoading && !data
        ? html`<p class="dim">Loading…</p>`
        : html`<wifi-activity-chart .data=${data} .mode=${this._activityMode}></wifi-activity-chart>`}
    </div>`;
  }

  // --- channel survey (docs/history.md "Wi-Fi channel survey") -----------

  async _refreshSurveyStatus() {
    try {
      this._surveyStatus = await api.getWifiSurvey();
    } catch (err) {
      this._surveyError = err.message;
    }
  }

  _setSurveyPreset(id) {
    this._surveyPreset = id;
    if (id) this._surveyChannelsText = presetChannels(id).join(",");
  }

  _setSurveyChannelsText(value) {
    this._surveyChannelsText = value;
    this._surveyPreset = ""; // hand-edited -- no longer matches a preset
  }

  async _startSurvey() {
    this._surveyBusy = true;
    this._surveyError = null;
    try {
      const channels = parseSurveyChannels(this._surveyChannelsText);
      await api.startWifiSurvey(channels, this._surveyDwell, this._surveyRounds);
      await this._refreshSurveyStatus();
    } catch (err) {
      this._surveyError = err.message;
    } finally {
      this._surveyBusy = false;
    }
  }

  async _stopSurvey() {
    this._surveyBusy = true;
    try {
      await api.stopWifiSurvey();
      await this._refreshSurveyStatus();
    } catch (err) {
      this._surveyError = err.message;
    } finally {
      this._surveyBusy = false;
    }
  }

  _renderSurvey() {
    const status = this._surveyStatus;
    const stopping = status?.state === "stopping";
    const running = status?.state === "running" || stopping;
    const loads = overlapLoad(status?.results ?? []);
    const results = (status?.results ?? []).map((r) => ({ ...r, load: loads.get(r.channel) ?? null }));
    const sorted = sortSurveyResults(results, this._surveySort);
    const maxRate = Math.max(1e-9, ...sorted.map((r) => surveyFrameRate(r)));
    // With no row picked, follow the channel being heard so its networks
    // appear as they arrive.
    const selected = sorted.find((r) => r.channel === this._surveySelectedChannel)
      ?? (running ? sorted.find((r) => r.status === "listening") : undefined);
    let estimate = null;
    try {
      estimate = surveyEstimateText(
        parseSurveyChannels(this._surveyChannelsText).length, this._surveyDwell, this._surveyRounds);
    } catch {
      estimate = null; // channel text not parseable yet
    }
    return html`<div class="wifi-survey">
      <div class="wifi-survey-controls control-row">
        <label>Preset <select ?disabled=${running} .value=${this._surveyPreset}
          @change=${(e) => this._setSurveyPreset(e.target.value)}>
          <option value="">custom…</option>
          ${SURVEY_PRESETS.map((p) => html`<option value=${p.id} ?selected=${p.id === this._surveyPreset}>${p.label}</option>`)}
        </select></label>
        <label>Channels <input type="text" ?disabled=${running} placeholder="1,6,11"
          .value=${this._surveyChannelsText} @input=${(e) => this._setSurveyChannelsText(e.target.value)} /></label>
        <label>Dwell (s) <input type="number" min="5" max="120" ?disabled=${running}
          .value=${this._surveyDwell} @input=${(e) => { this._surveyDwell = Number(e.target.value); }} /></label>
        <label>Rounds <input type="number" min="0" max="100" ?disabled=${running} title="0 = until stopped"
          .value=${this._surveyRounds} @input=${(e) => { this._surveyRounds = Number(e.target.value); }} /></label>
        ${running
          ? html`<button class="wifi-stop" ?disabled=${this._surveyBusy || stopping} @click=${() => this._stopSurvey()}>${this._surveyBusy || stopping ? "Stopping…" : "Stop"}</button>`
          : html`<button class="wifi-start" ?disabled=${this._surveyBusy} @click=${() => this._startSurvey()}>${this._surveyBusy ? "Starting…" : "Start"}</button>`}
        ${!running && estimate ? html`<span class="dim">${estimate}</span>` : ""}
      </div>

      ${this._surveyError ? html`<p class="wifi-problem" role="alert">${this._surveyError}</p>` : ""}
      ${status && status.state !== "idle" ? html`<p class="wifi-survey-progress" aria-live="polite">
        ${status.state === "running" ? surveyIndicator(status.phase) : ""}${surveyProgressText(status)}</p>` : ""}
      ${this._renderSurveyVerdict(results)}

      ${results.length
        ? html`<div class="lib-table-wrap"><table class="lib-table wifi-survey-table">
            <thead><tr>${SURVEY_COLUMNS.map((c) => {
              const active = this._surveySort.sort === c.key;
              const dir = active ? (this._surveySort.order === "asc" ? "ascending" : "descending") : "none";
              return html`<th scope="col" aria-sort=${dir}><button type="button" class="wifi-sort"
                data-help=${`${c.help} Click to sort.`}
                @click=${() => { this._surveySort = nextSurveySort(this._surveySort, c.key); }}>
                ${c.label}<span aria-hidden="true">${active ? (this._surveySort.order === "asc" ? " ▲" : " ▼") : ""}</span></button></th>`;
            })}</tr></thead>
            <tbody>${sorted.map((r) => this._surveyRow(r, maxRate))}</tbody>
          </table></div>`
        : html`<p class="empty">No channels surveyed yet — choose channels and press Start.</p>`}

      ${selected ? this._renderSurveyNetworks(selected) : ""}

      <p class="dim wifi-survey-honesty">${SURVEY_HONESTY_TEXT}</p>
    </div>`;
  }

  _surveyRow(r, maxRate) {
    const rate = surveyFrameRate(r);
    const strongest = surveyStrongest(r);
    const selected = r.channel === this._surveySelectedChannel;
    return html`<tr class=${`${selected ? "selected" : ""} ${r.status === "listening" ? "wifi-survey-live" : ""}`}
      @click=${() => { this._surveySelectedChannel = selected ? null : r.channel; }}>
      <td class="mono">${r.channel}</td>
      <td class="wifi-survey-status" title=${r.status === "failed" ? (r.error ?? "") : ""}>${r.status === "listening" || r.status === "switching"
        ? html`${surveyIndicator(r.status)}${r.status}`
        : r.status === "failed"
          ? html`<span class="wifi-problem">failed</span>`
          : html`<span class=${r.status === "done" ? "" : "dim"}>${r.status ?? "--"}</span>`}</td>
      <td>${surveyBand(r.channel)}</td>
      <td>${r.listened_s.toFixed(1)}s</td>
      <td>${r.frames}</td>
      <td><span class="wifi-rate-bar"><span class="wifi-rate-bar-fill" style=${`width:${Math.min(100, (rate / maxRate) * 100)}%`}></span></span>
        <span class="dim">${rate.toFixed(1)}/s</span></td>
      <td data-help=${r.channel <= 14 ? "Frames/s of this channel plus the ±4 channels whose frequencies overlap it: how busy its air is, which the channel's own count understates" : "5 GHz 20 MHz channels don't overlap: same as frames/s"}>${r.load == null ? html`<span class="dim">--</span>` : `${r.load.toFixed(0)}/s`}</td>
      <td>${r.networks}</td>
      <td>${strongest
        ? html`<span class=${strongest.ssid ? "" : "dim"}>${strongest.ssid || "hidden network"}</span>${strongest.signal != null ? html` <span class="dim">(${strongest.signal})</span>` : ""}`
        : html`<span class="dim">--</span>`}</td>
    </tr>`;
  }

  _renderSurveyNetworks(result) {
    const top = result.top ?? [];
    return html`<section class="wifi-survey-networks">
      <h4>Channel ${result.channel} · ${plural(result.networks, "network")}</h4>
      ${top.length
        ? html`<table class="lib-table wifi-survey-networks-table">
            <thead><tr><th>SSID</th><th>BSSID</th><th>DS channel</th><th>Signal</th></tr></thead>
            <tbody>${top.map((n) => html`<tr>
              <td class=${n.ssid ? "" : "dim"}>${n.ssid || "hidden network"}</td>
              <td class="mono">${n.bssid}${vendorLine(n)}</td>
              <td>${n.ds_channel ?? "--"}${n.ds_channel != null && n.ds_channel !== result.channel ? html` <span class="chip wifi-marker" title="This beacon's DS Parameter Set channel differs from the surveyed channel">overlap</span>` : ""}</td>
              <td>${n.signal ?? "--"}</td>
            </tr>`)}</tbody>
          </table>`
        : html`<p class="dim">No networks heard on this channel yet.</p>`}
    </section>`;
  }

  render() {
    const empty = emptyStateFor(this._status, this._rows.length, this._viewingSession());
    const stateText = this._viewingSession()
      ? "viewing saved session"
      : this._status
        ? (STATE_LABELS[this._status.state] ?? this._status.state)
        : "Loading…";
    const channel = this._configuredChannel ?? this._status?.channel;
    return html`<section class="panel wifi-view" aria-label="Wi-Fi networks">
      <h2>Wi-Fi <span class="h2-band">2412–2484 MHz · 5180–5825 MHz</span></h2>
      ${this._viewingSession() ? "" : html`<div class="wifi-tabs" role="tablist" aria-label="Wi-Fi view">
        ${[["networks", "Networks"], ["live", "Live frames"], ["survey", "Channel survey"], ["activity", "Activity"]].map(([id, label]) => html`<button role="tab"
          aria-selected=${this._view === id ? "true" : "false"} @click=${() => this._setView(id)}>${label}</button>`)}
      </div>`}
      ${!(this._viewingSession() || this._view === "survey") ? "" : html`<p class="wifi-header">Networks observed on <span class="chan-hl">channel ${channel ?? "?"}${wifiChannelMHz(channel) ? ` (${wifiChannelMHz(channel)} MHz)` : ""}</span> since ${epochHHMM(this._since)} · ${stateText}</p>`}
      <div class="ctl-groups">
      ${this._viewingSession() || this._view === "survey" || this._view === "activity" ? "" : this._renderReceiverControls()}
      ${this._viewingSession() || this._view === "survey" || this._view === "activity" ? "" : this._renderRecordControl()}

      ${(this._view === "survey" || this._view === "activity") && !this._viewingSession() ? "" : html`<span class="ctl-label">List</span>
        <div class="wifi-filters control-row">
        <input type="search" placeholder="search SSID / BSSID…" .value=${this._q}
          @input=${(e) => this._setQuery(e.target.value)} />
        <label><input type="checkbox" ?checked=${this._hideIgnored}
          @change=${() => { this._hideIgnored = !this._hideIgnored; }} /> Hide ignored</label>
        <select aria-label="Group networks" title="Group the networks on this page"
          @change=${(e) => { this._group = e.target.value; }}>
          <option value="" ?selected=${!this._group}>no grouping</option>
          <option value="name" ?selected=${this._group === "name"}>group by network name</option>
          <option value="ap" ?selected=${this._group === "ap"} data-help="BSSIDs that differ only in the last digit or the locally-administered bit">group by access point (probable)</option>
        </select>
        <select @change=${(e) => this._setChannelFilter(e.target.value)}>
          <option value="">all channels</option>
          ${[...new Set(this._rows.map((r) => r.channel).filter((c) => c != null))].sort((a, b) => a - b)
            .map((c) => html`<option value=${c} ?selected=${String(c) === this._channelFilter}>${c}</option>`)}
        </select>
        <span class="pager">
        <button class="link" ?disabled=${this._offset === 0} @click=${() => this._page(-1)}>‹ Prev</button>
        <span class="dim">${this._total ? `${this._offset + 1}-${Math.min(this._offset + LIMIT, this._total)} of ${this._total}` : "0"}</span>
        <button class="link" ?disabled=${this._offset + LIMIT >= this._total} @click=${() => this._page(1)}>Next ›</button>
        </span>
      </div>`}
      </div>

      ${this._error ? html`<p role="alert">${this._error}</p>` : ""}
      ${this._view === "networks" && !this._viewingSession() ? this._renderSummary() : ""}

      <div class=${`wifi-body${this._selectedBssid && this._view !== "live" && this._view !== "survey" && this._view !== "activity" ? " has-detail" : ""}`}>
      ${this._view === "live" && !this._viewingSession() ? this._renderLive()
        : this._view === "survey" && !this._viewingSession() ? this._renderSurvey()
        : this._view === "activity" && !this._viewingSession() ? this._renderActivity()
        : empty
        ? html`<p class="empty">${empty.text}${empty.setup ? html` <button class="link" @click=${() => this._openSetup()}>Open Setup</button>` : ""}</p>`
        : html`<div class="lib-table-wrap">
            <table class="lib-table wifi-table">
              <thead><tr>${WIFI_COLUMNS.map((c) => {
                if (!c.key) return html`<th scope="col">${c.label}</th>`;
                const active = this._sort.sort === c.key;
                const dir = active ? (this._sort.order === "asc" ? "ascending" : "descending") : "none";
                return html`<th scope="col" aria-sort=${dir}><button type="button" class="wifi-sort"
                  title=${`Sort by ${c.label.toLowerCase()}`}
                  @click=${() => { this._sort = nextSort(this._sort, c.key); this._offset = 0; this._refreshRows(); }}>
                  ${c.label}<span aria-hidden="true">${active ? (this._sort.order === "asc" ? " ▲" : " ▼") : ""}</span></button></th>`;
              })}</tr></thead>
              <tbody>${this._tableBody(visibleRows(this._rows, this._labels, this._hideIgnored))}</tbody>
            </table>
          </div>`}

      ${this._selectedBssid && this._view !== "live" && this._view !== "survey" ? this._renderDetail() : ""}
      </div>
    </section>`;
  }

  _renderSummary() {
    const parts = networkSummaryParts(this._summary);
    if (!parts) return "";
    return html`<section class="summary-blocks wifi-summary" aria-label="Network summary"
      data-help="Networks (access points) heard on this channel in the last minute. Probable access points groups BSSIDs that differ only in the last digit or the locally-administered bit, the way routers derive guest and mesh networks. Makers need the vendor names download (Setup → Wi-Fi). msdr never tracks client devices.">
      ${summaryBlocks(parts).map((b) => html`<table class="summary-block">
        <thead><tr><th colspan="2">${b.title}</th></tr></thead>
        <tbody>${b.rows.map((r) => html`<tr class="${r.kind}${r.warn ? " warn" : ""}">
          <td class="label">${r.label}</td>
          <td class="num"><span class="mono">${r.count}</span>${r.kind === "item"
            ? html`<span class="share-bar"><span style="width:${Math.round(r.share * 100)}%"></span></span>` : ""}</td>
        </tr>`)}</tbody>
      </table>`)}
    </section>`;
  }

  async _copyFindings(text) {
    try {
      await navigator.clipboard.writeText(text);
      this._findingsCopied = true;
      setTimeout(() => { this._findingsCopied = false; }, 1500);
    } catch {
      this._findingsCopied = false;
      window.prompt("Copy the survey findings:", text); // clipboard blocked: let the user copy by hand
    }
  }

  _renderSurveyVerdict(results) {
    const v = surveyVerdict(results);
    if (!v) return "";
    const findings = surveyFindings(results);
    return html`<section class="summary-tiles wifi-survey-verdict" aria-label="Survey verdict"
      data-help="From the channels surveyed so far. Busiest = most frames per listened second. Least congested = fewest frames per second, then fewest networks, among channels listened to in that band: a starting point for choosing your own router's channel, not a measurement of interference. Overlap = networks heard away from their own channel.">
      <ul class="summary-groups">
        ${v.map((t) => html`<li class="summary-group">
          <span class="summary-count mono">${t.value}</span>
          <span class="summary-label">${t.label}</span>
          ${t.detail ? html`<ul class="summary-sub"><li>${t.detail}</li></ul>` : ""}
        </li>`)}
      </ul>
      ${findings.length ? html`<details class="wifi-survey-why">
        <summary>Why these picks</summary>
        ${findings.map((f) => html`<h4>${f.title}</h4><ul>${f.lines.map((l) => html`<li>${l}</li>`)}</ul>`)}
        <button @click=${() => this._copyFindings(findingsText(findings, this._surveyStatus))}>${this._findingsCopied ? "Copied" : "Copy findings"}</button>
      </details>` : ""}
    </section>`;
  }

  _tableBody(rows) {
    const rawRange = rawSignalRange(rows);
    const groups = groupNetworks(rows, this._group);
    if (!groups) return rows.map((row) => this._row(row, rawRange));
    return groups.map((g) => html`<tr class="wifi-group-row"><td colspan=${WIFI_COLUMNS.length}>
        <strong>${g.title}</strong> <span class="dim">${g.detail}</span></td></tr>
      ${g.rows.map((row) => this._row(row, rawRange))}`);
  }

  _row(row, rawRange = null) {
    const ssid = ssidLabel(row);
    const marks = channelMarkers(row);
    const signal = signalCellText(row.signal);
    const rare = isRare(row);
    const barPct = rare ? null : signalBarPct(row.signal, rawRange);
    const heard = (row.beacons ?? 0) + (row.probe_responses ?? 0);
    const barTitle = rare
      ? `Heard only ${heard} time${heard === 1 ? "" : "s"} — too few frames for a reliable signal`
      : row.signal?.kind === "raw" ? "Bar is relative to the other networks listed (uncalibrated raw code, not dBm)" : "";
    const selected = row.bssid === this._selectedBssid;
    const label = this._labels.get(row.bssid);
    return html`<tr class=${`${selected ? "selected" : ""} ${rare ? "wifi-rare" : ""}`} @click=${() => this._select(row.bssid)}>
      <td class=${ssid.hidden ? "dim" : ""}>${ssid.hidden ? html`<em>${ssid.text}</em>` : ssid.text}</td>
      <td class="mono">${row.bssid}${vendorLine(row)}</td>
      <td>${label
        ? html`<strong>${label.name}</strong> <span class="chip timeline-class-chip timeline-class-${label.class}">${label.class}</span>`
        : html`<span class="dim">--</span>`}</td>
      <td>${row.channel ?? "--"} ${marks.map((m) => html`<span class="chip wifi-marker" title=${m.title}>${m.text}</span>`)}</td>
      <td>${row.security ?? "--"}</td>
      <td class="wifi-signal" title=${[signal?.title, barTitle].filter(Boolean).join(" · ")}>${signal
        ? html`${signal.text}${barPct != null
          ? html`<span class="wifi-signal-bar"><span class="wifi-signal-bar-fill ${row.signal.kind}" style="width:${barPct}%"></span></span>`
          : ""}`
        : html`<span class="dim">-- not reported</span>`}</td>
      <td>${relativeAge(row.first_seen)}</td>
      <td>${relativeAge(row.last_seen)}</td>
      <td>${(row.beacons ?? 0) + (row.probe_responses ?? 0)}</td>
    </tr>`;
  }

  _renderDetail() {
    const d = this._detail;
    if (!d) return html`<aside class="wifi-detail panel"><p class="dim">Loading…</p></aside>`;
    if (d.error) return html`<aside class="wifi-detail panel"><p class="wifi-problem" role="alert">Could not load details for ${d.bssid}: ${d.error}</p></aside>`;
    const ssid = ssidLabel(d);
    const rsn = d.rsn ?? null;
    const packets = (d.beacons ?? 0) + (d.probe_responses ?? 0);
    const history = (Array.isArray(d.ssid_history) ? d.ssid_history : [])
      .map((h) => (typeof h === "string" ? h : h?.ssid)).filter(Boolean);
    const standards = [["ht", "Wi-Fi 4 (HT)"], ["vht", "Wi-Fi 5 (VHT)"], ["he", "Wi-Fi 6 (HE)"]]
      .filter(([k]) => d[k]).map(([, label]) => label);
    return html`<aside class="wifi-detail panel" aria-label="Network details">
      <header class="wifi-detail-head">
        <h3 class=${ssid.hidden ? "dim" : ""}>${ssid.text}</h3>
        <span class="mono dim">${d.bssid}</span>${vendorLine(d)}
        <button class="wifi-detail-close" aria-label="Close details" @click=${() => { this._selectedBssid = null; this._detail = null; }}>×</button>
      </header>
      <div class="wifi-detail-chips">
        <span class="chip">${d.security ?? "security unknown"}</span>
        ${d.channel != null ? html`<span class="chip">channel ${d.channel}${d.channel_mismatch ? " (from beacon)" : ""}</span>` : ""}
        <span class="chip">last seen ${relativeAge(d.last_seen)}</span>
      </div>

      <div class="wifi-detail-grid">
        <section>
          <h4>Security</h4>
          <dl class="wifi-facts">
            <dt>Protection</dt><dd>${d.security ?? "unknown"}</dd>
            ${rsn ? html`
              <dt>Ciphers</dt><dd>${[].concat(rsn.pairwise_ciphers ?? []).join(", ") || "--"}${rsn.group_cipher ? ` (group ${rsn.group_cipher})` : ""}</dd>
              <dt>Authentication</dt><dd>${[].concat(rsn.akms ?? []).join(", ") || "--"}</dd>
              <dt>Protected management frames</dt><dd>${rsn.mfpr ? "required" : rsn.mfpc ? "supported" : "no"}</dd>` : ""}
            ${d.wpa1 ? html`<dt>Legacy WPA</dt><dd>${this._kv(d.wpa1)}</dd>` : ""}
          </dl>
        </section>
        <section>
          <h4>Radio</h4>
          <dl class="wifi-facts">
            <dt>Standards</dt><dd>${standards.length ? standards.join(", ") : "802.11a/b/g only"}</dd>
            ${d.country ? html`<dt>Country</dt><dd>${d.country}</dd>` : ""}
            ${d.beacon_interval != null ? html`<dt>Beacon interval</dt><dd>${d.beacon_interval} TU (≈${Math.round(d.beacon_interval * 1.024)} ms)</dd>` : ""}
            ${d.capabilities ? html`<dt>Type</dt><dd>${d.capabilities.cap_ibss ? "ad-hoc" : "access point"}${d.capabilities.cap_privacy ? ", encrypted" : ""}</dd>` : ""}
          </dl>
        </section>
        <section>
          <h4>Reception</h4>
          <dl class="wifi-facts">
            <dt>Heard</dt><dd>first ${relativeAge(d.first_seen)}, last ${relativeAge(d.last_seen)}</dd>
            <dt>Frames</dt><dd>${plural(d.beacons ?? 0, "beacon")}, ${plural(d.probe_responses ?? 0, "probe response")}${d.bad_fcs_seen ? `, ${d.bad_fcs_seen} with bad checksum` : ""}</dd>
            ${history.length > 1 ? html`<dt>Names seen</dt><dd>${history.join(", ")}</dd>` : ""}
            <dt>Signal</dt><dd>${this._signalSummary(d)}</dd>
          </dl>
        </section>
      </div>

      <section class="wifi-label">
        <device-label-editor receiver="wifi" .ident=${d.bssid} kind="bssid"
          .label=${this._labels.get(d.bssid) ?? null} @label-changed=${() => this._onLabelChanged()}></device-label-editor>
      </section>

      ${this._viewingSession() ? "" : html`<section class="wifi-history">
        <h4>History (${LABEL_HISTORY_HOURS} h)</h4>
        ${this._detailHistory
          ? html`<device-history-chart .cells=${this._detailHistory.cells ?? []} .coverage=${this._detailHistory.coverage ?? []}
              .bucketS=${this._detailHistory.bucket_s ?? 60} .start=${this._detailHistory.start ?? 0}
              .end=${this._detailHistory.end ?? 0} .signalKind=${this._detailHistory.device?.signal_kind ?? "raw"}></device-history-chart>`
          : html`<p class="dim">Loading…</p>`}
      </section>`}

      ${d.recent?.length ? this._renderRecent(d.recent) : ""}

      ${d.ies?.length ? html`<details class="wifi-ies"><summary>Information elements (${d.ies.length})</summary>
        <table class="lib-table wifi-ie-table">
          <thead><tr><th>Element</th><th>ID</th><th>Bytes</th><th>Data</th></tr></thead>
          <tbody>${d.ies.map((ie) => html`<tr>
            <td>${ie.name ?? "unknown"}</td>
            <td class="mono">${ie.id}${ie.ext_id != null ? `/${ie.ext_id}` : ""}</td>
            <td>${ie.length}</td>
            <td><details><summary>hex</summary><code class="mono wifi-hex">${ie.hex}</code></details></td>
          </tr>`)}</tbody>
        </table>
      </details>` : ""}

      <p class="dim wifi-provenance">${plural(packets, "frame")} from the msdr Wi-Fi helper${this._status?.channel != null ? ` on channel ${this._status.channel}` : ""}${this._status?.helper?.upstream_commit ? ` · helper ${this._status.helper.upstream_commit.slice(0, 7)}` : ""}</p>
    </aside>`;
  }

  /** Latest value with its range, in words; a sparkline only when it varies. */
  _signalSummary(d) {
    const values = (d.signal_history ?? []).map((p) => p?.[1]).filter((v) => Number.isFinite(v));
    if (!values.length) return "not reported by this adapter";
    const unit = d.signal?.kind === "dbm" ? " dBm" : "";
    const label = d.signal?.kind === "dbm" ? "" : " (raw code, relative, uncalibrated)";
    const lo = Math.min(...values), hi = Math.max(...values), latest = values[values.length - 1];
    const segments = hi > lo ? sparklineSegments(d.signal_history ?? []) : [];
    return html`${latest}${unit}${hi > lo ? `, range ${lo}–${hi}${unit}` : ", steady"}<span class="dim">${label}</span>
      ${segments.length ? html`<svg class="wifi-spark" viewBox="0 0 120 24" width="120" height="24" role="img"
        aria-label="Signal trend">${segments.map((pts) => svg`<polyline points=${pts} fill="none" stroke="var(--context-wifi)" stroke-width="1.5"></polyline>`)}</svg>` : ""}`;
  }

  /** Recent frames: one line per frame type with a count, the latest few on demand. */
  _renderRecent(recent) {
    const byType = new Map();
    for (const f of recent) {
      const name = SUBTYPE_LABELS[f.subtype] ?? `subtype ${f.subtype}`;
      const entry = byType.get(name) ?? { name, count: 0, bad: 0, last: 0 };
      entry.count += 1; entry.bad += f.bad_fcs ? 1 : 0; entry.last = Math.max(entry.last, f.ts ?? 0);
      byType.set(name, entry);
    }
    const latest = [...recent].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)).slice(0, 8);
    return html`<section class="wifi-recent">
      <h4>Recent broadcasts <span class="dim">(last ${recent.length} kept)</span></h4>
      <p class="wifi-recent-summary">${[...byType.values()].map((t) => html`<span class="chip"><strong>${t.count}</strong> × ${t.name}${t.bad ? html` · <span class="wifi-problem">${t.bad} bad checksum</span>` : ""}</span>`)}</p>
      <details><summary>Latest ${plural(latest.length, "frame")}</summary>
        <table class="lib-table wifi-frames">
          <thead><tr><th>Time</th><th>Type</th><th>Bytes</th><th>Checksum</th><th></th></tr></thead>
          <tbody>${latest.map((f) => html`<tr>
            <td class="mono">${new Date((f.ts ?? 0) * 1000).toLocaleTimeString()}</td>
            <td>${SUBTYPE_LABELS[f.subtype] ?? `subtype ${f.subtype}`}</td>
            <td>${f.length}</td>
            <td>${f.bad_fcs ? html`<span class="wifi-problem">bad</span>` : "ok"}</td>
            <td><button class="link" @click=${() => this._showFrame(f.n)}>${this._frameHex[f.n] ? "Raw shown" : "Show raw"}</button></td>
          </tr>${this._frameHex[f.n] ? html`<tr><td colspan="5"><code class="mono wifi-hex">${this._frameHex[f.n]}</code></td></tr>` : ""}`)}</tbody>
        </table>
      </details>
    </section>`;
  }

  _kvChips(obj) {
    if (Array.isArray(obj)) return obj.map((v) => html`<span class="chip">${v}</span>`);
    return Object.entries(obj).filter(([, v]) => v).map(([k]) => html`<span class="chip">${k}</span>`);
  }

  _kv(obj) {
    if (obj == null) return "";
    if (typeof obj !== "object") return String(obj);
    return Object.entries(obj).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join("/") : v}`).join(" · ");
  }
}

customElements.define("wifi-view", WifiView);
