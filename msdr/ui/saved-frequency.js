/** Return saved signal records at the current receiver frequency.
 * Matching is frequency-only (±1 Hz); mode is returned for context but does
 * not hide a saved channel when the receiver mode differs. */
export function savedFrequencyMatches(hz, signals = [], favorites = []) {
  if (typeof hz !== "number" || !Number.isFinite(hz)) return [];
  const favoriteIds = new Set(
    (favorites || []).filter((item) => item?.id != null).map((item) => String(item.id))
  );
  const seen = new Set();
  const matches = [];
  for (const signal of [...(signals || []), ...(favorites || [])]) {
    if (!signal || signal.id == null || !Number.isFinite(signal.frequency_hz)) continue;
    const id = String(signal.id);
    if (seen.has(id) || Math.abs(signal.frequency_hz - hz) > 1) continue;
    seen.add(id);
    matches.push({
      id: signal.id,
      name: signal.name ?? null,
      demodulator: signal.demodulator ?? null,
      favorite: favoriteIds.has(id),
      kind: "signal",
    });
  }
  return matches;
}

/** Closest operator-saved channel for a measured frequency. This is context,
 * never an identification or an instruction to retune. A bounded 50 ppm
 * window covers survey-center drift (33 kHz at 1090 MHz) while staying at
 * 5–8 kHz for the narrow VHF channels. The exact-match receiver badge above
 * deliberately retains its much stricter ±1 Hz rule. */
export function nearestSavedFrequency(hz, signals = [], favorites = []) {
  if (typeof hz !== "number" || !Number.isFinite(hz)) return null;
  const favoriteIds = new Set(
    (favorites || []).filter((item) => item?.id != null).map((item) => String(item.id))
  );
  const seen = new Set();
  let best = null;
  for (const signal of [...(signals || []), ...(favorites || [])]) {
    if (!signal || signal.id == null || !Number.isFinite(signal.frequency_hz)) continue;
    const id = String(signal.id);
    if (seen.has(id)) continue;
    seen.add(id);
    const distanceHz = Math.abs(signal.frequency_hz - hz);
    const radiusHz = Math.max(5_000, Math.min(50_000, Math.round(signal.frequency_hz * 50e-6)));
    if (distanceHz > radiusHz || (best && distanceHz >= best.distanceHz)) continue;
    best = {
      id: signal.id,
      name: signal.name,
      frequencyHz: signal.frequency_hz,
      distanceHz,
      favorite: favoriteIds.has(id),
    };
  }
  return best;
}

/** Compact display of exact-frequency matches; library records remain intact. */
export function summarizeSavedMatches(matches = []) {
  if (!matches.length) return null;
  const label = match => String(match.name ?? "")
    .replace(/\s*[·•-]?\s*\d+(?:\.\d+)?\s*MHz\s*$/i, "").trim();
  const named = match => Boolean(label(match)) && !/^unknown(?:\s|$)/i.test(label(match));
  const preferred = matches.find(m => m.favorite && named(m))
    ?? matches.find(named) ?? matches.find(m => m.favorite) ?? matches[0];
  return {
    name: named(preferred) ? label(preferred) : "Saved channel",
    favorite: matches.some(m => m.favorite),
    modes: [...new Set(matches.map(m => m.demodulator).filter(Boolean))],
    records: matches,
  };
}
