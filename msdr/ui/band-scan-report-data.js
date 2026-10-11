/** A scan report keeps measured observations separate from reference-only names. */
const valueName = value => typeof value === "string" ? value : value?.name ?? value?.label ?? null;
const finite = value => typeof value === "number" && Number.isFinite(value);
const catalogNames = value => (Array.isArray(value) ? value : value ? [value] : [])
  .map(valueName).filter(Boolean);
/** docs/allocations.md "Backend integration" compact summary
 * (`{lo_hz, hi_hz, jurisdiction, status, services:[{name, priority, table}]}`)
 * -> all services, primary first, secondary marked; null when there's
 * nothing to show (no allocation on this observation). */
function allocationText(allocation) {
  const services = allocation?.services ?? [];
  const primary = services.filter(s => s.priority !== "secondary").map(s => s.name);
  const secondary = services.filter(s => s.priority === "secondary").map(s => `${s.name} (secondary)`);
  const parts = [];
  if (primary.length) parts.push(primary.join(", "));
  if (secondary.length) parts.push(secondary.join(", "));
  return parts.join(" · ") || null;
}
export function activityText(activity, voice) {
  if (voice?.kind === "voice") return "Speech-like?";
  if (voice?.kind === "data") return "Data-like";
  if (typeof activity === "string") return activity;
  if (typeof activity?.kind === "string") return activity.kind.replaceAll("_", " ");
  return "Unclassified";
}

export function signalKind(observations) {
  // Only measured/analyzed evidence colors a row. A catalog or band-plan match
  // alone does not identify a signal, and audio heuristics stay tentative.
  if (observations.some(obs => obs.confirmed || obs.rds?.ps)) return "decoded";
  if (observations.some(obs => obs.voice?.kind === "voice")) return "voice";
  if (observations.some(obs => obs.voice?.kind === "data")) return "data";
  if (observations.some(obs => obs.family || obs.recognized)) return "clue";
  return "measured";
}

function candidate(observations) {
  for (const obs of [...observations].reverse()) {
    for (const [value, source] of [
      [obs.confirmed, "confirmed"], [obs.rds?.ps, "RDS"],
      [obs.recognized, "recognized"], [obs.family, "analyzer"],
    ]) {
      const name = valueName(value);
      if (name) return { name, source };
    }
  }
  return { name: "Unidentified", source: "measured" };
}

export function buildBandScanReport(detail) {
  const payload = detail?.payload ?? {};
  const config = detail?.config ?? payload.config ?? {};
  const frequencies = (payload.results ?? []).map(result => {
    const observations = result.observations ?? [];
    const snr = observations.filter(obs => finite(obs.snr_db));
    const strongest = snr.reduce((best, obs) => !best || obs.snr_db > best.snr_db ? obs : best, null);
    const times = observations.reduce((range, obs) => {
      if (finite(obs.observed_at)) {
        range.first = Math.min(range.first, obs.observed_at);
        range.last = Math.max(range.last, obs.observed_at);
      }
      return range;
    }, { first: Infinity, last: -Infinity });
    const observed = candidate(observations);
    const sources = new Set(["Measured"]);
    if (observed.source !== "measured") sources.add(observed.source);
    const catalog_matches = [...new Set(observations.flatMap(obs => catalogNames(obs.catalog)))];
    // obs.allocation (regulatory, every service) wins over the older
    // obs.bandplan (channel/application hint) -- reports saved before
    // allocations existed only ever carry the latter.
    const band_context = [...observations].reverse().map(obs => allocationText(obs.allocation)).find(Boolean)
      ?? [...observations].reverse().map(obs => valueName(obs.bandplan)).find(Boolean) ?? null;
    if (catalog_matches.length) sources.add("Catalog reference");
    if (band_context) sources.add("Band allocation");
    if (observations.some(obs => obs.decoding || obs.decoder || obs.decoders)) sources.add("Decoder");
    return {
      frequency_hz: result.frequency_hz,
      candidate: observed.name,
      candidate_source: observed.source,
      signal_kind: signalKind(observations),
      count: result.observation_count ?? observations.length,
      span_count: result.span_count ?? result.span_centers_hz?.length ?? 0,
      peak_snr_db: result.snr_max_db ?? strongest?.snr_db ?? null,
      bandwidth_hz: strongest?.spectrum_bandwidth_hz ?? strongest?.width_hz ?? strongest?.bw_hz ?? null,
      activity: activityText(strongest?.activity ?? observations.at(-1)?.activity,
        strongest?.voice ?? observations.at(-1)?.voice),
      catalog_matches,
      band_context,
      first_seen: result.first_seen ?? (finite(times.first) ? times.first : null),
      last_seen: result.last_seen ?? (finite(times.last) ? times.last : null),
      evidence: [...sources],
    };
  }).sort((a, b) => a.frequency_hz - b.frequency_hz);
  const timeline = (payload.results ?? []).flatMap(result => (result.observations ?? []).map(obs => ({
    ...obs, frequency_hz: obs.frequency_hz ?? result.frequency_hz,
  }))).sort((a, b) => (b.observed_at ?? 0) - (a.observed_at ?? 0));
  const spans = payload.spans ?? [];
  const centers = config.centers_hz ?? spans.map(span => span.center_hz);
  const coverage = centers.map((center, index) => {
    const span = spans[index];
    return span ?? {
      center_hz: center,
      start_hz: Math.max(config.start_hz ?? 0, center - (config.span_hz ?? 0) / 2),
      end_hz: Math.min(config.end_hz ?? Infinity, center + (config.span_hz ?? 0) / 2),
      state: "unvisited", reason: "Scan ended before this span",
    };
  });
  return { frequencies, timeline, coverage, config, spectrum: detail?.spectrum ?? null };
}

function csvCell(value) {
  let text = value == null ? "" : String(value);
  if (typeof value !== "number" && /^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

export function bandScanFrequenciesCsv(frequencies) {
  const rows = [["frequency_hz", "candidate", "candidate_source", "observations", "spans", "peak_snr_db", "bandwidth_hz", "activity", "first_seen_utc", "last_seen_utc", "catalog_matches", "band_allocation", "evidence"]];
  for (const row of frequencies) rows.push([
    row.frequency_hz, row.candidate, row.candidate_source, row.count, row.span_count,
    row.peak_snr_db, row.bandwidth_hz, row.activity,
    finite(row.first_seen) ? new Date(row.first_seen * 1000).toISOString() : "",
    finite(row.last_seen) ? new Date(row.last_seen * 1000).toISOString() : "",
    row.catalog_matches.join("; "), row.band_context, row.evidence.join("; "),
  ]);
  return `${rows.map(row => row.map(csvCell).join(",")).join("\r\n")}\r\n`;
}
