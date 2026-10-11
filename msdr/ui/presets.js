// Task presets (S1 contract, "Task presets"): pure data for <preset-picker>.
// Starting one only switches the workspace and remembers a couple of
// visibility flags (app.js's _onPresetStart) -- it never retunes, starts a
// decoder, arms recording or takes receiver ownership. Each preset's `plan`
// is shown to the operator *before* the Start button so they know exactly
// what's about to happen.
export const PRESETS = [
  {
    id: "browse-local",
    label: "Browse local signals",
    workspace: "listen",
    plan: [
      "Switches to LISTEN and watches the spectrum around your local frequencies for activity.",
      "Keeps the current source. If a replay is active, it stays active; open the REPLAY source chip to stop it in Review before returning to live reception.",
      "Technical survey columns stay collapsed, out of the way.",
    ],
    visible: { more: false, tech: false },
    recommended: [],
  },
  {
    id: "monitor-saved",
    label: "Monitor saved channels",
    workspace: "discover",
    plan: [
      "Switches to DISCOVER so FINDS, SCAN FREQUENCIES and ANALYZE RECORDING sit next to the spectrum.",
      "Meant for keeping an eye on channels you've already saved, not for hunting new ones.",
      "Does not start a scan, arm the recorder, or change any saved channel by itself.",
    ],
    visible: { more: false, tech: false },
    recommended: [],
    // S5, UI-only: hints app.js's _onPresetStart to preselect the scan
    // deck's LIST source (scanner-config.js's saved params) when at least
    // one signal list exists -- never starts a scan itself.
    scanListHint: true,
  },
  {
    id: "explore-hf",
    label: "Explore HF",
    workspace: "listen",
    plan: [
      "Switches to LISTEN; mode, antenna, and profile context are visible in the receiver header (technical survey columns live in DISCOVER).",
      "HF field validation pending — synthetic/reference tested only: treat any catalog or band-plan match as a starting point, not a confirmed identification.",
      "Recommended starting modes: USB, LSB, CW -- you still pick the mode and tune manually.",
    ],
    visible: { more: true, tech: false },
    recommended: ["USB", "LSB", "CW"],
  },
  {
    id: "review-recording",
    label: "Review recording",
    workspace: "review",
    plan: [
      "Switches to REVIEW so you can browse finished pre-roll recordings and play one back.",
      "Starting playback there never arms the recorder or retunes the live receiver -- it only replays a file.",
      "Pick a recording in the REVIEW view, or use a recordings-list row's ▶ REVIEW action.",
    ],
    visible: { more: false, tech: false },
    recommended: [],
  },
];
