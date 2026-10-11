// Scanner setup params: persisted across sessions (localStorage) and shared
// between scanner-view.js (the full setup form, embedded in scanner-deck's
// ⚙ modal) and scanner-deck.js (the instrument panel's compact deck, whose
// START button re-uses the last-saved params without reopening the form) --
// see msdr/ui build spec v0.14.2. Keeping load/save/buildConfig here (not
// duplicated in both components) means the deck's "is there anything usable
// saved?" check can never drift from what the form itself would submit.
import { parseFrequencyInput, BW_PRESETS } from "./format.js";

export const STORAGE_KEY = "msdr.scanner.params";
export const MAX_CHANNELS = 10_000;

const SPECTRUM_DEFAULT_STEP_HZ = 12_500;
const SPECTRUM_DEFAULT_THRESHOLD = 20;

export const DEFAULT_PARAMS = {
  source: "list",
  listId: "",
  rangePreset: "",
  startMhz: "",
  stopMhz: "",
  stepHz: 12_500,
  stepCustomKhz: "",
  spectrumStartMhz: "",
  spectrumStopMhz: "",
  spectrumStepHz: SPECTRUM_DEFAULT_STEP_HZ,
  spectrumThresholdDb: SPECTRUM_DEFAULT_THRESHOLD,
  mode: "NFM",
  bwHz: BW_PRESETS.NFM[0],
  dwellMs: 500,
  thresholdDb: 10,
  hangS: 2,
  stopOnHit: false,
  captureHits: false,
  // Scanner finds log (2026-09-25 plan): log_finds/keep_scanning/skip_ignored
  // mirror ScanConfig's own defaults (msdr/engine/scanner.py) so an unopened
  // form and a fresh ScanConfig agree; finds_dwell_s only matters once
  // keep_scanning is on.
  logFinds: false,
  keepScanning: false,
  findsDwellS: 3,
  skipIgnored: true,
};

export function loadParams() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed ? parsed : {};
  } catch {
    return {};
  }
}

export function saveParams(params) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(params));
  } catch {
    // private mode / storage disabled -- selection just won't persist
  }
}

function effectiveStepHz(p) {
  if (p.stepHz === "custom") {
    const khz = Number(p.stepCustomKhz);
    return Number.isFinite(khz) && khz > 0 ? Math.round(khz * 1000) : null;
  }
  const hz = Number(p.stepHz);
  return Number.isFinite(hz) && hz > 0 ? hz : null;
}

/** A DEFAULT_PARAMS-shaped params object (e.g. from loadParams()) -> a
 * POST /api/scan/start body, or null while the fields don't describe a
 * usable source -- the one place "is this saved config good enough to
 * start" is decided, so scanner-view's own START and scanner-deck's START
 * (which starts from the last-saved params without reopening the form)
 * never disagree. */
export function buildConfig(p) {
  let source;
  if (p.source === "list") {
    if (!p.listId) return null;
    source = { type: "list", list_id: p.listId };
  } else if (p.source === "range") {
    const start_hz = parseFrequencyInput(p.startMhz);
    const stop_hz = parseFrequencyInput(p.stopMhz);
    const step_hz = effectiveStepHz(p);
    if (!start_hz || !stop_hz || !step_hz || stop_hz <= start_hz) return null;
    const count = Math.floor((stop_hz - start_hz) / step_hz) + 1;
    if (count > MAX_CHANNELS) return null;
    source = {
      type: "range",
      start_hz,
      stop_hz,
      step_hz,
      demodulator: p.mode,
      filter_bandwidth_hz: p.bwHz,
    };
  } else if (p.source === "spectrum") {
    const start_hz = parseFrequencyInput(p.spectrumStartMhz) || undefined;
    const stop_hz = parseFrequencyInput(p.spectrumStopMhz) || undefined;
    const step_hz = Number(p.spectrumStepHz) || SPECTRUM_DEFAULT_STEP_HZ;
    const level_threshold = Number(p.spectrumThresholdDb);
    source = {
      type: "spectrum",
      start_hz,
      stop_hz,
      step_hz,
      level_threshold: Number.isFinite(level_threshold) ? level_threshold : SPECTRUM_DEFAULT_THRESHOLD,
      demodulator: p.mode,
      filter_bandwidth_hz: p.bwHz,
    };
  } else {
    return null;
  }
  const dwell_ms = Number(p.dwellMs);
  const threshold_snr_db = Number(p.thresholdDb);
  const hang_s = Number(p.hangS);
  const finds_dwell_s = Number(p.findsDwellS);
  return {
    source,
    dwell_ms: Number.isFinite(dwell_ms) && dwell_ms > 0 ? dwell_ms : undefined,
    threshold_snr_db: Number.isFinite(threshold_snr_db) ? threshold_snr_db : undefined,
    hang_s: Number.isFinite(hang_s) && hang_s >= 0 ? hang_s : undefined,
    stop_on_hit: !!p.stopOnHit,
    capture_hits: p.source === "range" ? !!p.captureHits : undefined,
    log_finds: !!p.logFinds,
    keep_scanning: !!p.keepScanning,
    finds_dwell_s: Number.isFinite(finds_dwell_s) && finds_dwell_s >= 0 ? finds_dwell_s : undefined,
    skip_ignored: !!p.skipIgnored,
  };
}
