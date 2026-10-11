const DELAY_MS = 900;

const WORKSPACES = {
  listen: "Listen to saved and nearby channels, browse recent activity, and decode the current frequency.",
  discover: "Survey live spectrum, inspect detected signals, and review discoveries.",
  map: "See decoded positions and trajectories for aircraft, vessels, and other located signals.",
  band_scans: "Review saved band-scan coverage, detections, and observation history.",
  review: "Inspect recordings, replay sessions, decoded messages, and observations.",
  library: "Manage saved channels, profiles, lists, recordings, and reports.",
  satellites: "Check passes, sky position, Doppler tracking, and beacon monitoring.",
  catalog: "Search reference frequencies and schedules; a match does not confirm reception.",
  allocations: "See what a frequency is legally allocated for, by jurisdiction -- regulatory context, not evidence of reception.",
};

const LISTEN = {
  recent: "Channels you recently visited or saved.",
  favorites: "Channels you marked as favorites for quick access.",
  nearby: "Saved channels near the receiver’s current frequency.",
  suggestions: "Possible channels and catalog references near the current frequency.",
  log: "A dated record of receiver activity and notes.",
};

const HELP = [
  ["#favorite-current-button", "Mark the tuned frequency as a favorite. It is saved first if needed."],
  [".channel-scanner-disclosure > summary", "Open the optional VFO and saved-channel scanner controls."],
  [".panel-scanner-deck", "Sweep a channel list or VFO range and pause when a signal meets your threshold."],
  [".receiver-operations .strip-recording", "Arm and record receiver IQ, then open recordings for review."],
  [".receiver-operations .strip-tools", "Receiver setup, diagnostics, and layout."],
  [".receiver-context-line", "Saved-channel matches and independent catalog context for this tuned frequency."],
  [".frequency-edit-pill", "Band allocation (regulatory table) for the tuned frequency. Select it for details. To type a frequency, double-click the digits or press F."],
  [".receiver-signal-level", "Current power and signal-to-noise estimate from the receiver."],
  [".receiver-demodulation", "Choose the demodulation mode and filter width for the tuned channel."],
  [".step-row", "Tune one frequency step at a time. Shift multiplies the step; Alt makes it finer."],
  [".frequency-details", "The live receiver frequency and direct tuning controls."],
  [".receiver-inline-context", "Receiver antenna, gain state, mode, and bandwidth."],
  [".strip-primary-actions", "Favorite, save, navigate tuning history, search, or open Setup."],
  ["#instrument-panel", "Live receiver controls and status from SDRconnect."],

  [".discover-views button[aria-pressed='true']", "This Discover view is selected. Switch views without losing saved scan reports."],
  [".discover-views button", "Switch between live survey, discovery inbox, and saved signal records."],
  [".listen-list-controls input", "Filter this Listen list by channel name or frequency."],
  [".listen-list-controls select", "Change how this Listen list is ordered."],
  [".listen-resize-handle", "Drag to make the Listen list taller or shorter. Arrow keys also work."],
  [".listen-workbench-content", "Browse channels in the selected Listen view; actions sit beside each channel."],
  [".listen-workbench-left", "Quick access to recent, favorite, nearby, suggested, and logged channels."],
  [".listen-decode-controls .decode-prepare-button", "Preview receiver changes needed by the selected decoder."],
  [".listen-decode-controls .decode-messages-button", "Open the complete decoded-message history."],
  [".listen-decode-controls .scanner-deck-controls", "Start or stop decoding on this channel or across the tuned band."],
  [".listen-decode-controls", "Decode signals on the current channel or band; results appear in Review."],

  [".survey-band-scan > summary", "Scan a frequency range one sampled span at a time and save a coverage report."],
  [".survey-band-scan-controls label", "Band-scan range, width, listening duration, and overlap settings."],
  [".survey-band-scan-controls .run-start", "Begin the range scan with these settings."],
  [".survey-band-scan", "Band scanning visits each span, measures activity, and retains a report."],
  [".survey-sampling-control", "Sample rate sets the width of spectrum received in each live frame."],
  [".survey-band-step", "Move the visible span around its center while keeping overlap between views."],
  [".survey-toolbar-scroll", "Spectrum display controls for sensitivity, levels, averaging, and zoom."],
  [".survey-topbar", "Start, stop, clear, and configure the live spectrum survey."],
  [".survey-signal-shortcuts > summary", "Open the list of currently visible spectrum markers."],
  [".survey-results", "Measured and proposed signals. Select a row to inspect its evidence."],
  ["survey-view .survey-visual", "Live spectrum and waterfall. Canvas gestures have their own interaction hints."],

  [".band-report-toolbar", "Switch between detected frequencies, the scan timeline, and coverage."],
  [".band-report-list-controls", "Select a scan session and refresh or export its report."],
  [".band-scan-report", "Saved band scans show measured frequencies, observations, and uncovered spans."],
  [".discover-geography .geo-toolbar", "Filter map layers, search positions, clear or refresh history, and fit the map."],
  [".discover-geography", "Decoded positions are shown on the map or in a data table."],
  [".signal-panel-header", "Selected frequency, evidence, and Tune, Decode, and Save actions."],
  [".signal-panel", "Inspect evidence and history for one frequency without leaving the current workspace."],
  [".setup-section > summary", "Expand this group to edit related receiver and application settings."],
  [".setup-drawer", "Application settings; saved changes affect the live receiver or future sessions."],
  [".library-view", "Saved channels, profiles, lists, recordings, and reports."],
  [".review-view", "Review recordings, decode results, observations, and replay sessions."],
  [".catalog-view", "Reference catalog matches help find frequencies; they are not received-signal proof."],
  [".sky-view", "Satellite sky position and pass information for your location."],
  [".passes-view", "Upcoming satellite passes and timing for your location."],
];

const HOVER_OWNED = "canvas, .leaflet-container, .survey-hover-card, .vehicle-hover-card, .survey-signal-shortcuts .survey-badge, .ui-help-tooltip";

function helpFor(target, root) {
  if (!(target instanceof Element) || !root.contains(target)) return null;
  if (target.closest(HOVER_OWNED)) return null;
  const existing = target.closest("[title]");
  if (existing && root.contains(existing) && existing.getAttribute("title")?.trim()) return null;
  const explicit = target.closest("[data-help]");
  if (explicit && root.contains(explicit)) {
    return { element: explicit, text: explicit.getAttribute("data-help") };
  }
  const workspace = target.closest(".workspace-tab");
  if (workspace) return { element: workspace, text: WORKSPACES[workspace.dataset.workspace] };
  const listenTab = target.closest(".listen-workbench-tab");
  if (listenTab) return { element: listenTab, text: LISTEN[listenTab.id.replace("listen-tab-", "")] };
  for (const [selector, text] of HELP) {
    const element = target.closest(selector);
    if (element && root.contains(element)) return { element, text };
  }
  return null;
}

export function installUiHelp(root) {
  const bubble = document.createElement("div");
  bubble.id = "msdr-ui-help";
  bubble.className = "ui-help-tooltip";
  bubble.setAttribute("role", "tooltip");
  bubble.hidden = true;
  document.body.append(bubble);
  let current = null;
  let timer = null;
  let origin = null;
  let focusTarget = null;
  let priorDescription = null;

  const hide = () => {
    clearTimeout(timer);
    timer = null;
    bubble.hidden = true;
    if (focusTarget) {
      if (priorDescription == null) focusTarget.removeAttribute("aria-describedby");
      else focusTarget.setAttribute("aria-describedby", priorDescription);
    }
    focusTarget = null;
    priorDescription = null;
    current = null;
  };
  const place = (x, y) => {
    const box = bubble.getBoundingClientRect();
    bubble.style.left = `${Math.max(8, Math.min(x + 14, window.innerWidth - box.width - 8))}px`;
    bubble.style.top = `${Math.max(8, Math.min(y + 18, window.innerHeight - box.height - 8))}px`;
  };
  const schedule = (candidate, x, y, focused = null) => {
    if (!candidate?.text) { hide(); return; }
    if (current?.element === candidate.element && current.text === candidate.text) return;
    hide();
    current = candidate;
    origin = { x, y };
    timer = setTimeout(() => {
      if (!current || !current.element.isConnected) return;
      bubble.textContent = current.text;
      bubble.hidden = false;
      place(origin.x, origin.y);
      if (focused) {
        focusTarget = focused;
        priorDescription = focused.getAttribute("aria-describedby");
        focused.setAttribute("aria-describedby", [priorDescription, bubble.id].filter(Boolean).join(" "));
      }
    }, DELAY_MS);
  };
  const onOver = event => schedule(helpFor(event.target, root), event.clientX, event.clientY);
  const onMove = event => {
    const candidate = helpFor(event.target, root);
    if (candidate?.element !== current?.element || candidate?.text !== current?.text) {
      schedule(candidate, event.clientX, event.clientY);
      return;
    }
    if (!current) return;
    if (bubble.hidden && origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 12) {
      current = null;
      schedule(candidate, event.clientX, event.clientY);
    } else if (!bubble.hidden) place(event.clientX, event.clientY);
  };
  const onOut = event => {
    if (!current) return;
    const next = helpFor(event.relatedTarget, root);
    if (next?.element !== current.element) hide();
  };
  const onFocus = event => {
    const candidate = helpFor(event.target, root);
    if (!candidate) return;
    const box = event.target.getBoundingClientRect();
    schedule(candidate, box.left + box.width / 2, box.bottom, event.target);
  };
  const onKey = event => { if (event.key === "Escape") hide(); };
  root.addEventListener("pointerover", onOver);
  root.addEventListener("pointermove", onMove);
  root.addEventListener("pointerout", onOut);
  root.addEventListener("pointerdown", hide);
  root.addEventListener("focusin", onFocus);
  root.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, true);
  window.addEventListener("keydown", onKey);
  return () => {
    hide();
    root.removeEventListener("pointerover", onOver);
    root.removeEventListener("pointermove", onMove);
    root.removeEventListener("pointerout", onOut);
    root.removeEventListener("pointerdown", hide);
    root.removeEventListener("focusin", onFocus);
    root.removeEventListener("focusout", hide);
    window.removeEventListener("scroll", hide, true);
    window.removeEventListener("keydown", onKey);
    bubble.remove();
  };
}
