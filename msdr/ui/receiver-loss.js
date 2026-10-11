// Pure "receiver lost" detection for the alert bar under the workspace tabs
// (components/receiver-lost-bar.js, wired in app.js). No DOM/Lit imports so
// tests/ui/test_receiver_loss.py can run it in node.

export const KINDS = ["sdrconnect", "wifi", "ble"];

const IN_USE = new Set(["receiving", "stale", "starting"]);
const PROBLEM = {
  wifi: new Set(["no_device", "failed", "unavailable", "dependency_missing"]),
  // BLE has the same supervisor shape (wanted, retry_attempt) plus
  // dongle-specific terminal states.
  ble: new Set(["no_device", "failed", "dependency_missing", "claimed", "no_le", "firmware_mismatch", "wrong_baud"]),
};
// Why: a card pulled mid-capture shows up as a failing retry long before the
// supervisor exhausts MAX_RETRIES (~20 s) and reports "failed".
const USB_GONE = /no such device|no usb device|disconnected/i;

export const TITLES = {
  sdrconnect: "SDRconnect connection lost",
  wifi: "Wi-Fi adapter disconnected",
  ble: "BLE dongle disconnected",
};

function isLostStatus(kind, s) {
  if (!s || !s.wanted) return false;
  if (PROBLEM[kind].has(s.state)) return true;
  return (s.retry_attempt ?? 0) > 0 && USB_GONE.test(s.last_error ?? "");
}

/** The alert for a receiver that was in use in `prev` and is gone in `next`,
 * or null. Transition-only: a first status (prev null) or one already lost
 * never alerts, so loading the app with a broken receiver stays quiet. For
 * "sdrconnect", prev/next are msdr state objects ({connected}); `next.wsDown`
 * marks the msdr websocket itself dropping. */
export function detectLoss(kind, prev, next, now = Date.now()) {
  if (!next) return null;
  if (kind === "sdrconnect") {
    if (!prev?.connected || (next.connected && !next.wsDown)) return null;
    return {
      kind, title: TITLES.sdrconnect, at: now,
      detail: (next.wsDown ? "The link to msdr dropped, so the SDRconnect status is unknown. " : "")
        + "Tuning, the live spectrum and decoders are interrupted. See LINK for diagnostics.",
    };
  }
  if (!PROBLEM[kind]) return null;
  const wasInUse = !!prev && !!prev.wanted && IN_USE.has(prev.state) && !isLostStatus(kind, prev);
  if (!wasInUse || !isLostStatus(kind, next)) return null;
  const channel = next.channel ?? prev.channel ?? null;
  const stopped = kind === "wifi"
    ? (channel != null ? `Capture on channel ${channel} stopped.` : "Capture stopped.")
    : "Scanning stopped.";
  return {
    kind, title: TITLES[kind], at: now, channel,
    // Trim the helper's trailing period so the sentences join cleanly.
    detail: `${(next.last_error || `receiver state: ${next.state}`).replace(/[.\s]+$/, "")}. ${stopped}`,
  };
}

/** True once the receiver is back, which clears its alert. */
export function isRecovered(kind, status) {
  if (!status) return false;
  if (kind === "sdrconnect") return !!status.connected && !status.wsDown;
  if (!status.wanted) return false;
  return status.state === "receiving" || (status.state === "starting" && !status.last_error);
}

/** Extra sentence for a Wi-Fi loss while a channel survey was running (GET
 * /api/wifi/survey), or "" if none was. */
export function surveyNote(survey) {
  if (!survey || (survey.state !== "running" && survey.state !== "stopping")) return "";
  return survey.current_channel != null
    ? `The channel survey stopped at channel ${survey.current_channel}.`
    : "The channel survey stopped.";
}

/** Notify only when the user opted in, permission is granted and msdr isn't
 * the visible tab: an alert already on screen needs no second nudge. */
export function shouldNotify({ pref, permission, hidden }) {
  return !!pref && permission === "granted" && !!hidden;
}
