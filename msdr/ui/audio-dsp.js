// Pure helpers for the receiver panel's Audio line (AGC / Noise reduction /
// Squelch toggles, plus the Levels popover's threshold/strength fields).
// Moved out of listen-workbench.js's "Audio & DSP" card, which sat in the
// LISTEN station panel and printed a raw "confirmed" status word -- this
// quieter version lives with the receiver's other live settings (antenna,
// LNA, profile) and mirrors antenna.js's pending-vs-confirmed pattern: a
// request shows a transient "pending" state until SDRconnect's props echo
// it back, or the request is rejected/unsupported. The word "confirmed" is
// never shown; normal operation is just the toggle's pressed/unpressed
// state.

/** True once a pending request's value is reflected in SDRconnect's
 * confirmed props. Numbers use a 0.51 tolerance (SDRconnect echoes rounded
 * percentages/thresholds); every other type (booleans) needs an exact
 * match. */
export function dspValueMatches(pendingValue, confirmedValue) {
  if (typeof pendingValue === "number") {
    return Math.abs(Number(confirmedValue) - pendingValue) < 0.51;
  }
  return confirmedValue === pendingValue;
}

/** Drops any "pending" entries whose value now matches the latest receiver
 * props -- call from updated() when props change, so a fast SDRconnect
 * echo (over the websocket) clears the pending ellipsis without waiting on
 * the audio-dsp POST's own response. Returns the same map (by reference)
 * when nothing resolved, so callers can skip a re-render. */
export function settlePendingDsp(statusMap, props) {
  let next = null;
  for (const [property, entry] of Object.entries(statusMap)) {
    if (entry.status !== "pending" || props[property] === undefined) continue;
    if (!dspValueMatches(entry.value, props[property])) continue;
    if (!next) next = { ...statusMap };
    delete next[property];
  }
  return next ?? statusMap;
}

/** One control's rendered status: "pending" (ellipsis, mid-request),
 * "unsupported" (SDRconnect rejected the property itself -- stays disabled
 * with the reason in its title), "unavailable" (SDRconnect hasn't reported
 * a value for it -- also disabled), or "ready" (normal on/off operation
 * from the confirmed prop). Never "confirmed" or "rejected" as a standing
 * state -- a reject clears back to "ready" and is surfaced as a one-time
 * note instead (see dspRejectionNote). */
export function dspControlStatus(entry, confirmedValue) {
  if (entry?.status === "pending") return "pending";
  if (entry?.status === "unsupported") return "unsupported";
  return confirmedValue === undefined || confirmedValue === null ? "unavailable" : "ready";
}

/** The control's effective (pressed/displayed) value: the in-flight
 * request's value while pending, otherwise the confirmed prop. */
export function dspEffectiveValue(entry, confirmedValue) {
  return entry?.status === "pending" ? entry.value : confirmedValue;
}

/** Short note for the Audio line when a request comes back neither
 * confirmed nor unsupported (i.e. rejected, or the request itself failed) --
 * or null when there's nothing to say. The caller clears it on the next
 * change. */
export function dspRejectionNote(label, result) {
  const status = result?.status;
  if (!result || status === "confirmed" || status === "unsupported") return null;
  return result.reason ? `${label}: ${result.reason}` : `${label} wasn't accepted.`;
}
