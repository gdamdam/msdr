// Keep the UI's speech pulse tied to the timestamp of the DSP result so a
// cached label cannot restart the hold every time the status view rerenders.
export const SPEECH_HOLD_SECONDS = 3;
export const ACTIVITY_FRESHNESS_SECONDS = 30;

const finite = value => typeof value === "number" && Number.isFinite(value);

/** Describe current RF/speech activity without treating old DSP output as live. */
export function signalActivity(label, nowSeconds, lastSpeechAt = null) {
  if (label?.active_now === false) {
    return { state: "no_signal", text: "No signal", known: true, carrierPresent: false, speechDetected: false };
  }

  const analyzedAt = label?.analyzed_at;
  const age = finite(analyzedAt) && finite(nowSeconds) ? nowSeconds - analyzedAt : Infinity;
  const analysisState = label?.analysis_state?.state;
  const explicitlyStale = analysisState != null && analysisState !== "current_result";
  if (label?.active_now !== true) {
    return { state: "unknown", text: "Activity unknown", known: false, carrierPresent: null, speechDetected: false };
  }
  if (!finite(analyzedAt) || !finite(nowSeconds) || age < 0 ||
      age > ACTIVITY_FRESHNESS_SECONDS || explicitlyStale) {
    return { state: "carrier", text: "Carrier present · speech unknown", known: true,
      carrierPresent: true, speechDetected: false };
  }

  const kind = label?.voice?.kind;
  if (kind === "silence" && finite(lastSpeechAt) && lastSpeechAt > analyzedAt) {
    return { state: "carrier", text: "Carrier present · speech unknown", known: true,
      carrierPresent: true, speechDetected: false };
  }
  const speechAt = kind === "voice" ? analyzedAt : lastSpeechAt;
  const canHoldSpeech = finite(speechAt) && speechAt <= analyzedAt &&
    nowSeconds < speechAt + SPEECH_HOLD_SECONDS;
  if (kind === "voice" || kind === "silence" && canHoldSpeech) {
    if (canHoldSpeech) {
      return { state: "speech", text: "Speech-like activity? (tentative)", known: true,
        carrierPresent: true, speechDetected: true, speechAt };
    }
  }
  if (kind === "silence") {
    return { state: "carrier_only", text: "Carrier only", known: true, carrierPresent: true, speechDetected: false };
  }
  if (kind === "voice") {
    return { state: "carrier", text: "Carrier present · speech unknown", known: true,
      carrierPresent: true, speechDetected: false };
  }
  if (kind === "data") {
    return { state: "carrier", text: "Carrier present · data detected", known: true, carrierPresent: true, speechDetected: false, dataDetected: true };
  }
  return { state: "carrier", text: "Carrier present · speech unknown", known: true,
    carrierPresent: true, speechDetected: false };
}

/** True only while a fresh, timestamp-bounded speech-like result is current. */
export function isSpeechActivity(label, nowSeconds, lastSpeechAt = null) {
  return signalActivity(label, nowSeconds, lastSpeechAt).speechDetected;
}
