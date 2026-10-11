// Thin REST/WS client for the msdr API (see msdr/api/app.py for the routes).

/** GET/POST/PATCH/DELETE JSON. Throws Error(detail) for non-2xx responses. */
export async function fetchJson(path, opts = {}) {
  const init = { ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      if (data && data.detail) {
        // FastAPI's 422 detail is a list of {msg, loc, ...} objects, not a
        // string -- join their messages so the status line shows text
        // instead of "[object Object]".
        detail = Array.isArray(data.detail)
          ? data.detail.map((e) => e.msg ?? String(e)).join("; ")
          : data.detail;
      }
    } catch {
      // body wasn't JSON; keep statusText
    }
    throw new Error(detail || `request failed: ${resp.status}`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function qs(params) {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (!entries.length) return "";
  const search = new URLSearchParams();
  for (const [key, value] of entries) {
    if (Array.isArray(value)) value.forEach((item) => search.append(key, item));
    else search.append(key, value);
  }
  return "?" + search.toString();
}

/** Open /ws, calling onMessage(msg) for every decoded message; reconnects
 * forever with backoff from 1s up to 10s. onDown() fires whenever the socket
 * drops, so the UI can stop presenting stale readings as live. onOpen(isReconnect),
 * if given, fires on every successful open, true from the second one on --
 * callers use that to reload anything a gap while disconnected could have
 * missed (the server has no queue to flag/resync while nobody's subscribed).
 * Returns a function that closes the socket and stops reconnecting. */
export function connectWs(onMessage, onDown, onOpen) {
  let closed = false;
  let socket = null;
  let delay = 1000;
  let everConnected = false;

  function connect() {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${proto}//${location.host}/ws`);
    socket.addEventListener("open", () => {
      delay = 1000;
      onOpen?.(everConnected);
      everConnected = true;
    });
    socket.addEventListener("message", (ev) => {
      try {
        onMessage(JSON.parse(ev.data));
      } catch {
        // ignore malformed frames
      }
    });
    socket.addEventListener("close", () => {
      if (!closed) onDown?.();
      scheduleReconnect();
    });
    socket.addEventListener("error", () => socket.close());
  }

  function scheduleReconnect() {
    if (closed) return;
    setTimeout(connect, delay);
    delay = Math.min(delay * 2, 10_000);
  }

  connect();
  return () => {
    closed = true;
    if (socket) socket.close();
  };
}

// Frequency allocation reference (v0.53.0, see docs/allocations.md):
// normalized NTIA Table of Frequency Allocations + 47 CFR S2.106 footnotes,
// backing <allocation-summary> (signal/survey/band-scan panels) and the
// <allocation-chart> view (components/allocation-chart.js). `jurisdiction`
// is one of "US"/"ITU1"/"ITU2"/"ITU3"; `column` for allocationBands is one of
// "itu_r1"/"itu_r2"/"itu_r3"/"us_federal"/"us_non_federal", omitted for all.
export const allocationLookup = (hz, jurisdiction) =>
  fetchJson(`/api/allocations/lookup${qs({ hz, jurisdiction })}`);
export const allocationBands = (loHz, hiHz, jurisdiction, column) =>
  fetchJson(`/api/allocations/bands${qs({ lo_hz: loHz, hi_hz: hiHz, jurisdiction, column })}`);
export const allocationSources = () => fetchJson("/api/allocations/sources");
export const refreshAllocationSource = (id) =>
  fetchJson("/api/allocations/refresh", { method: "POST", body: { source: id } });

export const getState = () => fetchJson("/api/state");
export const connectionRetry = () => fetchJson("/api/connection/retry", { method: "POST" });
export const capture = () => fetchJson("/api/capture", { method: "POST" });
export const palette = (q) => fetchJson(`/api/palette${qs({ q })}`);
export const tuneSignal = (id) => fetchJson(`/api/tune/signal/${id}`, { method: "POST" });
export const tuneFrequency = (hz, demodulator, filterBandwidthHz, manualMode = undefined) =>
  fetchJson("/api/tune/frequency", {
    method: "POST",
    body: { hz, demodulator, filter_bandwidth_hz: filterBandwidthHz, manual_mode: manualMode },
  });
export const applyProfile = (id) => fetchJson(`/api/profiles/${id}/apply`, { method: "POST" });
// Receiver antenna switch (instrument-panel.js's ANT select): a RestoreReport
// like the tune routes, not an echo -- props.active_antenna only updates once
// SDRconnect confirms it.
export const setAntenna = (antenna) =>
  fetchJson("/api/receiver/antenna", { method: "POST", body: { antenna } });
export const back = () => fetchJson("/api/history/back", { method: "POST" });
export const forward = () => fetchJson("/api/history/forward", { method: "POST" });
export const listSignals = (params = {}) => fetchJson(`/api/signals${qs(params)}`);
export const inbox = () => fetchJson("/api/signals/inbox");
export const identifySignal = (id) => fetchJson(`/api/signals/${id}/identify`);
export const identifyLive = (hz, mode, bw) =>
  fetchJson(`/api/identify${qs({ hz, mode, bw })}`);
export const recent = () => fetchJson("/api/signals/recent");
export const patchSignal = (id, patch) =>
  fetchJson(`/api/signals/${id}`, { method: "PATCH", body: patch });
export const createSignal = (body) => fetchJson("/api/signals", { method: "POST", body });
export const deleteSignal = (id) => fetchJson(`/api/signals/${id}`, { method: "DELETE" });
export const listProfiles = () => fetchJson("/api/profiles");
export const createProfile = (body) => fetchJson("/api/profiles", { method: "POST", body });
export const patchProfile = (id, patch) =>
  fetchJson(`/api/profiles/${id}`, { method: "PATCH", body: patch });
export const deleteProfile = (id) => fetchJson(`/api/profiles/${id}`, { method: "DELETE" });
export const listTags = () => fetchJson("/api/tags");
export const createTag = (name) => fetchJson("/api/tags", { method: "POST", body: { name } });
export const patchTag = (id, patch) => fetchJson(`/api/tags/${id}`, { method: "PATCH", body: patch });
export const journalDay = (day) => fetchJson(`/api/journal${qs({ day })}`);
export const journalDays = () => fetchJson("/api/journal/days");
export const exportLibrary = () => fetchJson("/api/export");
export const getSettings = () => fetchJson("/api/settings");
export const patchSettings = (patch) => fetchJson("/api/settings", { method: "PATCH", body: patch });
export const getIntegrationStatus = () => fetchJson("/api/integrations");
export const listIntegrationMessages = (limit = 100) =>
  fetchJson(`/api/integrations/messages?limit=${encodeURIComponent(limit)}`);
// Sorted IANA zone names for setup-drawer.js's Timezone field datalist.
export const getTimezones = () => fetchJson("/api/timezones");
// No default here: omitting `days` lets the server resolve it from
// settings.passes_days instead of silently overriding it with 2.
export const getPasses = (days) => fetchJson(`/api/passes${qs({ days })}`);
export const refreshPasses = () => fetchJson("/api/passes/refresh", { method: "POST" });
// A plain download link, not fetched as JSON -- see passes-view.js's "↓ ICS" button.
export const passesIcsUrl = () => "/api/passes.ics";
export const listScans = () => fetchJson("/api/scans");
export const getScan = (id) => fetchJson(`/api/scans/${encodeURIComponent(id)}`);
export const scanReportUrl = (id, name) => `/api/scans/${encodeURIComponent(id)}/report/${name}`;
export const scanClipUrl = (id, name) =>
  `/api/scans/${encodeURIComponent(id)}/clips/${encodeURIComponent(name)}`;
export const getSky = () => fetchJson("/api/sky");
export const getSkyFor = (label, hours, start, end) =>
  fetchJson(`/api/sky/${encodeURIComponent(label)}${qs({ hours, start, end })}`);
export const refreshTle = () => fetchJson("/api/sky/refresh", { method: "POST" });
export const startTrack = (signalId) => fetchJson(`/api/track/${signalId}`, { method: "POST" });
export const stopTrack = () => fetchJson("/api/track", { method: "DELETE" });
// Reference-catalog filters (docs/reference-catalogs.md "API and UI"):
// `network`, `station_id`, `ref_type` ("schedule"/"station"/"navaid"/
// "radar_site"), `sort` ("relevance" default/"frequency"/"distance") -- qs()
// above omits any of these (or the existing params) left undefined, so a
// caller only ever sends the filters it actually set.
export const searchCatalog = (params = {}) => fetchJson(`/api/catalog/search${qs(params)}`);
export const catalogNear = (hz, tol) => fetchJson(`/api/catalog/near${qs({ hz, tol })}`);
export const catalogSources = () => fetchJson("/api/catalog/sources");
export const refreshCatalog = (source) =>
  fetchJson("/api/catalog/refresh", { method: "POST", body: { source } });
export const importCatalog = (path, format) =>
  fetchJson("/api/catalog/import", { method: "POST", body: { path, format } });
export const tuneCatalog = (id) => fetchJson(`/api/tune/catalog/${id}`, { method: "POST" });
export const saveCatalogEntry = (id) => fetchJson(`/api/catalog/${id}/save`, { method: "POST" });
export const listen = (preset, limit) => fetchJson(`/api/listen${qs({ preset, limit })}`);
export const runScan = (body) => fetchJson("/api/scans/run", { method: "POST", body });
export const scanJobs = () => fetchJson("/api/scans/jobs");

// v0.10.0 "Library": paged/filtered signal browsing (library-view.js), bulk
// tag/status/color/delete ops, and the single-slot undo they leave behind.
export const library = (params = {}) => fetchJson(`/api/library${qs(params)}`);
export const libraryBulk = (ids, op, value) =>
  fetchJson("/api/library/bulk", { method: "POST", body: { ids, op, value } });
export const libraryUndoPeek = () => fetchJson("/api/library/undo");
export const libraryUndo = () => fetchJson("/api/library/undo", { method: "POST" });
export const libraryRedo = (id) => fetchJson("/api/library/redo", { method: "POST", body: { id } });

// v0.11.0 "Lists": named signal lists (built-in "favorites" always exists),
// list-scoped item order, saved searches, and duplicate detection/merge.
export const listLists = () => fetchJson("/api/lists");
export const createList = (name) => fetchJson("/api/lists", { method: "POST", body: { name } });
export const patchList = (id, patch) =>
  fetchJson(`/api/lists/${id}`, { method: "PATCH", body: patch });
export const deleteList = (id) => fetchJson(`/api/lists/${id}`, { method: "DELETE" });
export const listListItems = (id) => fetchJson(`/api/lists/${id}/items`);
export const addListItems = (id, ids) =>
  fetchJson(`/api/lists/${id}/items`, { method: "POST", body: { ids } });
export const removeListItems = (id, ids) =>
  fetchJson(`/api/lists/${id}/items/remove`, { method: "POST", body: { ids } });
export const orderListItems = (id, ids) =>
  fetchJson(`/api/lists/${id}/order`, { method: "PUT", body: { ids } });

export const listSavedSearches = () => fetchJson("/api/saved-searches");
export const createSavedSearch = (name, params) =>
  fetchJson("/api/saved-searches", { method: "POST", body: { name, params } });
export const deleteSavedSearch = (id) => fetchJson(`/api/saved-searches/${id}`, { method: "DELETE" });

export const libraryDuplicates = (toleranceHz) =>
  fetchJson(`/api/library/duplicates${qs({ tolerance_hz: toleranceHz })}`);
export const mergeLibrary = (keepId, mergeIds) =>
  fetchJson("/api/library/merge", { method: "POST", body: { keep_id: keepId, merge_ids: mergeIds } });

// v0.12.0 "Scanner": sweeps a list or frequency range, holding on channels
// whose SNR crosses a threshold (see components/scanner-view.js). Prefixed
// "scanner" (not "scan") to keep these distinct from the older iqscan
// offline-file endpoints above (listScans/getScan/runScan/scanJobs).
export const scannerStart = (body) => fetchJson("/api/scan/start", { method: "POST", body });
export const scannerPause = () => fetchJson("/api/scan/pause", { method: "POST" });
export const scannerResume = () => fetchJson("/api/scan/resume", { method: "POST" });
export const scannerStop = () => fetchJson("/api/scan/stop", { method: "POST" });
export const scannerHold = () => fetchJson("/api/scan/hold", { method: "POST" });
export const scannerSkip = (persist) => fetchJson("/api/scan/skip", { method: "POST", body: { persist } });
export const scannerStatus = () => fetchJson("/api/scan");
export const patchListItemSkip = (listId, signalId, skip) =>
  fetchJson(`/api/lists/${listId}/items/${signalId}`, { method: "PATCH", body: { skip } });

// Pre-roll IQ recorder: ARM keeps a rolling on-disk buffer from its own
// SDRconnect stream; REC (start) promotes it into a recording, STOP (stop)
// finalizes it into SigMF (see components/recorder-control.js).
export const getRecorder = () => fetchJson("/api/recorder");
export const recorderArm = (on) => fetchJson("/api/recorder/arm", { method: "POST", body: { on } });
export const recorderStart = () => fetchJson("/api/recorder/start", { method: "POST" });
export const recorderStop = () => fetchJson("/api/recorder/stop", { method: "POST" });

// Decoders (P1): multimon-ng/minimodem-backed digital decode of SDRconnect's
// demodulated audio, driven from a memory's `decoders` list or manually on
// the current VFO (see components/decode-deck.js and decode-view.js).
export const getDecoders = () => fetchJson("/api/decoders");
export const getDecoderStatus = () => fetchJson("/api/decoder");
export const startDecoder = (decoders, frequencyHz = null) =>
  fetchJson("/api/decoder/start", {
    method: "POST",
    body: frequencyHz != null ? { decoders, frequency_hz: frequencyHz } : { decoders },
  });
export const stopDecoder = () => fetchJson("/api/decoder/stop", { method: "POST" });
export const listDecodeEvents = ({ limit, decoder, q, since } = {}) =>
  fetchJson(`/api/decoder/events${qs({ limit, decoder, q, since })}`);
export const clearDecodeEvents = () => fetchJson("/api/decoder/events", { method: "DELETE" });
export const installDecoderPresets = () =>
  fetchJson("/api/decoder/presets/install", { method: "POST" });

// Decoders P2 "BAND DECODE": a polyphase-channelizer session that runs several
// NFM-family decoders at once across a span (msdr/decoder/band.py); status
// rides along in the same GET /api/decoder payload, under status.band.
export const startBand = (body) => fetchJson("/api/decoder/band/start", { method: "POST", body });
export const stopBand = () => fetchJson("/api/decoder/band/stop", { method: "POST" });
export const saveBandChannel = (frequency_hz, name) =>
  fetchJson("/api/decoder/band/save", { method: "POST", body: { frequency_hz, name } });

// Survey (S1): span picker + spectrum/waterfall/occupancy over the spectrum
// stream (see components/survey-view.js and the "spectrum"/"survey" ws
// messages handled in app.js's _onWsMessage).
export const getSurvey = () => fetchJson("/api/survey");
export const getSurveyBandScan = () => fetchJson("/api/survey/band-scan");
export const getSurveyBandScanDetail = (id) => fetchJson(`/api/survey/band-scan/${encodeURIComponent(id)}`);
export const startSurveyBandScan = (body) => fetchJson("/api/survey/band-scan/start", { method: "POST", body });
export const pauseSurveyBandScan = () => fetchJson("/api/survey/band-scan/pause", { method: "POST" });
export const resumeSurveyBandScan = () => fetchJson("/api/survey/band-scan/resume", { method: "POST" });
export const stopSurveyBandScan = () => fetchJson("/api/survey/band-scan/stop", { method: "POST" });
export const startSurvey = () => fetchJson("/api/survey/start", { method: "POST" });
export const stopSurvey = () => fetchJson("/api/survey/stop", { method: "POST" });
export const clearSurvey = () => fetchJson("/api/survey/clear", { method: "POST" });
export const setSurveySpan = (body) => fetchJson("/api/survey/span", { method: "POST", body });
// S2: dimension filter (activity/bandwidth/bandplan/catalog; needs_iq ones
// rejected by the server until S3) -- see survey-view.js's dimension toggles.
export const setSurveyDimensions = (dimensions) =>
  fetchJson("/api/survey/dimensions", { method: "POST", body: { dimensions } });
// S5: decode handoff (pins a band-decoder channel on a label's frequency),
// confirmed identity, and save-as-memory -- see components/survey-view.js's
// row actions (→ DECODE / ■ / SAVE) and app.js's _doSurveyDecode/Undecode/Save.
export const surveyDecode = (frequency_hz, decoders) =>
  fetchJson("/api/survey/decode", { method: "POST", body: { frequency_hz, decoders: decoders ?? null } });
export const surveyTestSignal = (frequency_hz, decoders = []) =>
  fetchJson("/api/survey/test-signal", { method: "POST", body: { frequency_hz, decoders } });
export const surveyOptimizeDecode = (frequency_hz, decoders = []) =>
  fetchJson("/api/survey/optimize-decode", { method: "POST", body: { frequency_hz, decoders } });
export const surveyUndecode = (frequency_hz) =>
  fetchJson("/api/survey/undecode", { method: "POST", body: { frequency_hz } });
export const surveySave = (frequency_hz, name) =>
  fetchJson("/api/survey/save", { method: "POST", body: { frequency_hz, name: name ?? null } });

// Replay: surveys a recorded SigMF file instead of the live SDRconnect
// stream -- status.replay carries {path, name, center_hz, sample_rate_hz,
// duration_s, position_s, playing, loop} while active, and status.span then
// reflects the file's span (see components/survey-view.js's REPLAY picker/
// transport and app.js's _doSurveyReplay/_doSurveyReplayControl/Stop).
export const startSurveyReplay = (path, loop) =>
  fetchJson("/api/survey/replay", { method: "POST", body: { path, loop } });
export const controlSurveyReplay = (body) =>
  fetchJson("/api/survey/replay/control", { method: "POST", body });
export const stopSurveyReplay = () => fetchJson("/api/survey/replay/stop", { method: "POST" });

// S10 "BEACONS": NCDXF/NDB beacon monitor -- cycles the receiver over beacon
// frequencies, logging SNR per scheduled slot and confirming call signs via
// the cw decoder (see components/beacons-view.js and the "beacons" ws
// message handled in app.js's _onWsMessage).
export const getBeacons = () => fetchJson("/api/beacons");
export const startBeacons = (body) => fetchJson("/api/beacons/start", { method: "POST", body });
export const stopBeacons = () => fetchJson("/api/beacons/stop", { method: "POST" });
export const listBeaconHits = (params = {}) => fetchJson(`/api/beacons/hits${qs(params)}`);
export const clearBeaconHits = () => fetchJson("/api/beacons/hits", { method: "DELETE" });

// Receiver owner (U1): which controller (scanner/tracking/beacons/manual) is
// currently driving the receiver -- see components/instrument-panel.js's
// owner chip and app.js's confirmInterrupt.
export const ownerStop = () => fetchJson("/api/owner/stop", { method: "POST" });
export const ownerConflicts = (action) => fetchJson(`/api/owner/conflicts${qs({ action })}`);

// Recordings list (U1): browse/manage finished pre-roll recorder captures --
// see components/recordings-view.js.
export const listRecordings = () => fetchJson("/api/recordings");
export const deleteRecording = (name) =>
  fetchJson(`/api/recordings/${encodeURIComponent(name)}`, { method: "DELETE" });
export const revealRecording = (name) =>
  fetchJson(`/api/recordings/${encodeURIComponent(name)}/reveal`, { method: "POST" });
export const analyzeRecording = (name) =>
  fetchJson(`/api/recordings/${encodeURIComponent(name)}/analyze`, { method: "POST" });

// Scanner finds log: every scanner hit recorded (grouped by frequency across
// runs) with whatever classification was available at hold end, reviewed
// later in the FINDS view (tune, promote to memory, ignore) -- see
// components/finds-view.js and the "find" ws message handled in app.js's
// _onWsMessage.
export const listFinds = (params = {}) => fetchJson(`/api/finds${qs(params)}`);
export const patchFind = (id, body) => fetchJson(`/api/finds/${id}`, { method: "PATCH", body });
export const deleteFind = (id) => fetchJson(`/api/finds/${id}`, { method: "DELETE" });
export const promoteFind = (id, name) =>
  fetchJson(`/api/finds/${id}/promote`, { method: "POST", body: { name: name ?? null } });
export const tuneFind = (id) => fetchJson(`/api/finds/${id}/tune`, { method: "POST" });

// U3 signal-detail panel (components/signal-panel.js): one aggregate view of
// a frequency (memory/find/survey-label/evidence/messages), reached from
// every "select a signal" surface via app.js's openSignal()/"signal-open".
export const getSignalDetail = (params = {}) => fetchJson(`/api/signal-detail${qs(params)}`);

// S3 "Virtual receiver" (review-view.js/audio-player.js): tunes/listens to a
// replay's own IQ, never the live SDRconnect adapter -- the zero-live-writes
// exit gate. GET /api/review carries {replay, receiver, bookmarks} (all
// null/[] when no replay is running); audio itself rides WS /ws/audio, opened
// directly by audio-player.js (binary PCM frames, not JSON, so it doesn't go
// through fetchJson/connectWs above).
export const getReview = () => fetchJson("/api/review");
export const reviewTune = (frequencyHz, demodulator, bandwidthHz) =>
  fetchJson("/api/review/tune", {
    method: "POST",
    body: { frequency_hz: frequencyHz, demodulator, bandwidth_hz: bandwidthHz },
  });
export const reviewAudio = (body) => fetchJson("/api/review/audio", { method: "POST", body });
export const setAudioDspProperty = (property, value) =>
  fetchJson("/api/receiver/audio-dsp", { method: "POST", body: { property, value } });
export const configureNativeAudio = (mode, body) =>
  fetchJson(`/api/inspection/${encodeURIComponent(mode)}/audio-dsp`, { method: "POST", body });
export const reviewSeek = (positionS) =>
  fetchJson("/api/review/seek", { method: "POST", body: { position_s: positionS } });
export const reviewLoop = (loop) => fetchJson("/api/review/loop", { method: "POST", body: { loop } });
export const reviewBookmarkCreate = (note) =>
  fetchJson("/api/review/bookmarks", { method: "POST", body: { note } });
export const listReviewBookmarks = (path) => fetchJson(`/api/review/bookmarks${qs({ path })}`);
export const reviewBookmarkSeek = (id) =>
  fetchJson(`/api/review/bookmarks/${encodeURIComponent(id)}/seek`, { method: "POST" });

// S2 "Observations": honest evidence records backing signal-detail (session-
// scoped captures with append-only Claims -- see
// docs/epics/listening-workbench/S2-observations-contract.md). Frequency
// proximity to a memory only *suggests* a link -- PATCH's response carries a
// "note" saying so, rendered by signal-panel.js after a link/unlink.
export const listObservations = (params = {}) => fetchJson(`/api/observations${qs(params)}`);
export const getObservation = (id) => fetchJson(`/api/observations/${encodeURIComponent(id)}`);
export const patchObservation = (id, body) =>
  fetchJson(`/api/observations/${encodeURIComponent(id)}`, { method: "PATCH", body });
export const addOperatorClaim = (id, body) =>
  fetchJson(`/api/observations/${encodeURIComponent(id)}/claims`, { method: "POST", body });
// S5: PROMOTE TO MEMORY (creates/links a Signal from the observation -- the
// response's `note` says "link suggests, does not prove identity", same
// caveat as the memory-link PATCH above) and EXPORT (full JSON incl.
// historical claims/decode events, downloaded client-side as a blob -- see
// signal-panel.js's _promoteObservation/_exportObservation).
export const promoteObservation = (id, name) =>
  fetchJson(`/api/observations/${encodeURIComponent(id)}/promote`, { method: "POST", body: { name: name ?? null } });
export const exportObservation = (id) => fetchJson(`/api/observations/${encodeURIComponent(id)}/export`);

// S4 "Reliable decode": review-view's own DECODE section, a ReplayDecodeSession
// bound to the running replay (single-channel or band, see msdr/decoder/
// replay_decode.py) -- distinct from the live decoder's startDecoder/startBand
// above; status rides along in GET /api/review's `decode` field.
export const reviewDecodeStart = (body) => fetchJson("/api/review/decode", { method: "POST", body });
export const reviewDecodeStop = () => fetchJson("/api/review/decode/stop", { method: "POST" });

// S5 "Clips": a bounded excerpt of the current replay's active run, copied to
// its own SigMF pair under <recorder_root>/clips (msdr/api/service.py) --
// see review-view.js's MARK IN/MARK OUT/CREATE CLIP. start_s/end_s are the
// same file-elapsed-seconds axis reviewSeek takes (continuous across runs,
// gaps excluded); a range crossing a run boundary or exceeding 600s is a 409,
// an unwritable clips dir a 503 -- both carry a `detail` string surfaced
// verbatim by fetchJson's Error.
export const createReviewClip = (startS, endS, note) =>
  fetchJson("/api/review/clips", { method: "POST", body: { start_s: startS, end_s: endS, note } });
export const listReviewClips = (path) => fetchJson(`/api/review/clips${qs({ path })}`);

// S5 "Validation status": corpus/decoder validation state, the synthetic
// scorecard summary, and the fixed outstanding-gates list (HF field
// validation, private VHF captures, etc.) -- see setup-drawer.js's
// VALIDATION group. Never implies "all validated"; gates stay listed
// regardless of how clean the rest of the payload looks.
export const getValidation = () => fetchJson("/api/validation");


export const prepareDecoderReceiver = body => fetchJson('/api/decoder/prepare', { method: 'POST', body });
export const restoreDecoderReceiver = token => fetchJson('/api/decoder/restore', { method: 'POST', body: { token } });
export const reapplyDecoderReceiver = token => fetchJson('/api/decoder/reapply', { method: 'POST', body: { token } });

// Passive Wi-Fi reception (docs/wifi.md): a separate helper process feeds an
// AWUS036ACH adapter's monitor-mode frames through msdr/wifi/; msdr itself
// never transmits (helper.host_tx_dma_disabled/mac_tx_enabled in the status payload confirm this at
// runtime). Status payload / `wifi` ws message: {state, enabled, channel,
// frequency_mhz, frames, frame_rate, drops, last_frame_at, last_error, helper,
// device, recording}.
export const getWifiStatus = () => fetchJson("/api/wifi/status");
export const getWifiDevices = () => fetchJson("/api/wifi/devices");
export const startWifi = (channel) => fetchJson("/api/wifi/start", { method: "POST", body: { channel } });
export const stopWifi = () => fetchJson("/api/wifi/stop", { method: "POST" });
export const clearWifiNetworks = () => fetchJson("/api/wifi/networks/clear", { method: "POST" });
// `params` carries whichever of {q, channel, offset, limit, source} the
// caller actually set (qs() above drops the rest) -- `source` selects a
// saved session ("session:<id>") instead of the live receiver.
export const listWifiNetworks = (params = {}) => fetchJson(`/api/wifi/networks${qs(params)}`);
// Live frame feed: frames with seq > after, oldest first (bounded server ring).
export const getWifiLive = (params = {}) => fetchJson(`/api/wifi/live${qs(params)}`);
// `source` is "live" (default) or "session:<id>" for a saved/imported session.
export const getWifiNetwork = (bssid, source) =>
  fetchJson(`/api/wifi/networks/${encodeURIComponent(bssid)}${qs({ source })}`);
export const getWifiFrame = (bssid, n, source) =>
  fetchJson(`/api/wifi/networks/${encodeURIComponent(bssid)}/frames/${n}${qs({ source })}`);
export const startWifiSession = () => fetchJson("/api/wifi/sessions/start", { method: "POST" });
export const stopWifiSession = () => fetchJson("/api/wifi/sessions/stop", { method: "POST" });
export const listWifiSessions = () => fetchJson("/api/wifi/sessions");
export const wifiSessionExportUrl = (id) => `/api/wifi/sessions/${encodeURIComponent(id)}/export.pcap`;
// multipart upload (not JSON), so this bypasses fetchJson -- same reasoning
// as backup-control.js's /api/backup/restore.
export async function importWifiPcap(file) {
  // Raw pcap bytes as the request body: msdr has no multipart dependency.
  const name = encodeURIComponent(file?.name ?? "import.pcap");
  const resp = await fetch(`/api/wifi/import?name=${name}`, {
    method: "POST", body: file, headers: { "Content-Type": "application/vnd.tcpdump.pcap" },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      if (data?.detail) detail = Array.isArray(data.detail) ? data.detail.map((e) => e.msg ?? String(e)).join("; ") : data.detail;
    } catch {
      // body wasn't JSON; keep statusText
    }
    throw new Error(detail || `request failed: ${resp.status}`);
  }
  return resp.status === 204 ? null : resp.json();
}

// Wi-Fi channel survey (docs/history.md "Wi-Fi channel survey"): the ACH
// helper has no channel hopping of its own, so this restarts capture per
// channel -- a separate lifecycle from startWifi/stopWifi above, polled at
// GET /api/wifi/survey while its tab is open or a survey is running.
export const startWifiSurvey = (channels, dwell_s, rounds) =>
  fetchJson("/api/wifi/survey/start", { method: "POST", body: { channels, dwell_s, rounds } });
// IEEE MAC vendor names, downloaded on demand (docs/wifi.md "Vendor names").
export const wifiSummary = (windowS = 60) => fetchJson(`/api/wifi/summary?window_s=${windowS}`);
export const wifiVendors = () => fetchJson("/api/wifi/vendors");
export const refreshWifiVendors = () => fetchJson("/api/wifi/vendors/refresh", { method: "POST" });
// Bluetooth SIG names, downloaded on demand (docs/ble.md "Bluetooth SIG names").
export const bleSummary = (windowS = 60) => fetchJson(`/api/ble/summary?window_s=${windowS}`);
export const bleNames = () => fetchJson("/api/ble/names");
export const refreshBleNames = () => fetchJson("/api/ble/names/refresh", { method: "POST" });
export const stopWifiSurvey = () => fetchJson("/api/wifi/survey/stop", { method: "POST" });
export const getWifiSurvey = () => fetchJson("/api/wifi/survey");

// Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): packets received
// per listened second, by channel and time bucket -- a read of the same
// history database the Wi-Fi/BLE timeline uses, never the receiver itself
// (viewing this never starts or stops reception).
export const getWifiActivity = (params = {}) => fetchJson(`/api/wifi/activity${qs(params)}`);

// Passive BLE reception (docs/ble.md): a raw-HCI helper subprocess drives a
// second, macOS-unclaimed Bluetooth 4.x USB dongle in passive-scan mode --
// msdr never scans actively, sends scan requests, connects, pairs or bonds.
// Status payload: {state, enabled, device, advertiser_count, ...}.
export const getBleStatus = () => fetchJson("/api/ble/status");
export const getBleDevices = () => fetchJson("/api/ble/devices");
export const startBle = () => fetchJson("/api/ble/start", { method: "POST", body: {} });
export const stopBle = () => fetchJson("/api/ble/stop", { method: "POST" });
export const clearBleAdvertisers = () => fetchJson("/api/ble/advertisers/clear", { method: "POST" });
// `params` carries whichever of {q, sort, order, offset, limit} the caller
// actually set (qs() above drops the rest).
export const listBleAdvertisers = (params = {}) => fetchJson(`/api/ble/advertisers${qs(params)}`);
// Live report feed: items with seq > after, oldest first (bounded server ring).
export const getBleLive = (params = {}) => fetchJson(`/api/ble/live${qs(params)}`);
export const getBleAdvertiser = (addr) => fetchJson(`/api/ble/advertisers/${encodeURIComponent(addr)}`);

// Live Remote ID (docs/remote-id.md "Live"): one Tracker fed from both
// receivers' trusted frames -- current UAS list and per-UAS detail, plus a
// coalescable "remoteid" ws message published on change (see connectWs below).
export const getRemoteIdLive = () => fetchJson("/api/remoteid/live");
export const getRemoteIdUas = (key) => fetchJson(`/api/remoteid/uas/${encodeURIComponent(key)}`);
// Offline decode of a recorded/imported Wi-Fi session (docs/remote-id.md),
// reused by Review's "Remote ID" action on a saved session.
export const getWifiSessionRemoteId = (id) =>
  fetchJson(`/api/wifi/sessions/${encodeURIComponent(id)}/remoteid`);

// Device history, shared timeline and labels (docs/history.md "API"):
// per-minute buckets recorded while a Wi-Fi/BLE receiver runs, plus user
// labels in the main database. `receivers`/`hide` are comma lists per the
// contract, not repeated query params, so they're joined here rather than
// left to qs()'s array handling (which repeats the key).
const csv = (v) => (Array.isArray(v) ? v.join(",") : v);

export const getHistoryStatus = () => fetchJson("/api/history/status");
export const clearHistory = () => fetchJson("/api/history/clear", { method: "POST" });
export const getTimeline = (params = {}) => fetchJson(`/api/history/timeline${qs({
  ...params, receivers: csv(params.receivers), hide: csv(params.hide),
})}`);
export const getDeviceHistory = (receiver, ident, params = {}) => fetchJson(
  `/api/history/device/${encodeURIComponent(receiver)}/${encodeURIComponent(ident)}${qs(params)}`
);

// Capture comparison (docs/history.md "Capture comparison"): two listening
// windows, e.g. "device off" vs "device on" or an antenna/placement A/B test.
export const getHistoryCompare = (params = {}) => fetchJson(`/api/history/compare${qs({
  ...params, receivers: csv(params.receivers),
})}`);

export const listLabels = (receiver) => fetchJson(`/api/labels${qs({ receiver })}`);
export const putLabel = (receiver, ident, label) => fetchJson(
  `/api/labels/${encodeURIComponent(receiver)}/${encodeURIComponent(ident)}`,
  { method: "PUT", body: label }
);
export const deleteLabel = (receiver, ident) => fetchJson(
  `/api/labels/${encodeURIComponent(receiver)}/${encodeURIComponent(ident)}`,
  { method: "DELETE" }
);

// Watch rules (docs/rules.md): beacon presence/absence, BLE sensor
// thresholds and Remote ID drone geofences, evaluated on the server's own
// 1-2 s tick -- this view only ever reads the resulting rules/events.
export const listRules = () => fetchJson("/api/rules");
export const createRule = (rule) => fetchJson("/api/rules", { method: "POST", body: rule });
export const patchRule = (id, patch) =>
  fetchJson(`/api/rules/${encodeURIComponent(id)}`, { method: "PATCH", body: patch });
export const deleteRule = (id) =>
  fetchJson(`/api/rules/${encodeURIComponent(id)}`, { method: "DELETE" });
export const listRuleEvents = (params = {}) => fetchJson(`/api/rules/events${qs(params)}`);
export const testRule = (id) =>
  fetchJson(`/api/rules/${encodeURIComponent(id)}/test`, { method: "POST" });
