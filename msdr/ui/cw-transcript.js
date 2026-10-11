// CW arrives as phrase-sized events. Keep the displayed history bounded and
// partition by the provenance fields carried by persisted decode events.
const MAX_EVENTS = 500;
const MAX_CHARS = 12000;
const SESSION_GAP_S = 300;

function streamKey(ev) {
  return JSON.stringify([
    ev.frequency_hz ?? null,
    ev.source_id ?? null,
    ev.generation ?? null,
    ev.analysis_run_id ?? null,
    ev.run_index ?? null,
  ]);
}

export function cwTranscripts(events, { since = null, frequencyHz = null } = {}) {
  const sorted = (events ?? [])
    .filter((ev) => ev.decoder === "cw" && typeof ev.text === "string" && ev.text.trim())
    .filter((ev) => since == null || ev.ts >= since)
    .filter((ev) => frequencyHz == null || ev.frequency_hz === frequencyHz)
    .sort((a, b) => a.ts - b.ts || String(a.id ?? "").localeCompare(String(b.id ?? "")))
    .slice(-MAX_EVENTS);
  const groups = [];
  const latestByStream = new Map();
  for (const ev of sorted) {
    const key = streamKey(ev);
    let group = latestByStream.get(key);
    if (!group || ev.ts - group.lastTs > SESSION_GAP_S) {
      group = { key: `${key}:${ev.id ?? ev.ts}`, frequencyHz: ev.frequency_hz,
        sourceId: ev.source_id, firstTs: ev.ts, lastTs: ev.ts, parts: [] };
      groups.push(group);
      latestByStream.set(key, group);
    }
    group.parts.push(ev.text.trim().replace(/\s+/g, " "));
    group.lastTs = ev.ts;
  }
  return groups.map(({ parts, ...group }) => ({
    ...group,
    text: parts.join(" ").slice(-MAX_CHARS),
  }));
}
