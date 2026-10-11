// Shared "is this recording usable" rule for recordings-view.js (the
// recordings modal) and review-view.js (the review workspace). A recording
// is damaged either because the backend flagged it outright (`row.error`,
// see msdr/api/services/recorder.py's `_recording_error_row`) or because a
// field Review actually needs to start a replay came back null even though
// no `error` was set -- never trust that enough to coerce a null into a
// 0:00/0 display or let REVIEW start against it.

/** Fields review-view's replay needs: a data path to tell the backend which
 * file to open (_startReplay/survey-replay), and a sample rate + duration to
 * size the transport and the virtual receiver against. */
function missingReplayField(row) {
  if (!row.path) return "no data path";
  if (row.sample_rate == null) return "sample rate unknown";
  if (row.duration_s == null) return "duration unknown";
  return null;
}

/** `{ damaged, reason, canReplay, canAnalyze }` for a GET /api/recordings
 * row. `reason` is the backend's own `error` string when set, otherwise
 * whichever required-for-replay field is missing; null when the row is
 * healthy. canReplay/canAnalyze both just mirror `!damaged` today -- a
 * damaged row has nothing either action could safely operate on. */
export function recordingHealth(row) {
  const reason = row.error ?? missingReplayField(row);
  const damaged = reason != null;
  return { damaged, reason, canReplay: !damaged, canAnalyze: !damaged };
}

/** Renders `value` with `formatter`, or "unknown" when the field itself is
 * null/undefined -- never coerces a damaged/missing field into whatever
 * zero-ish default `formatter` would otherwise produce (e.g. a naive
 * mmss(null) silently reading "0:00"). */
export function formatUnknown(value, formatter) {
  if (value == null) return "unknown";
  return formatter(value);
}
