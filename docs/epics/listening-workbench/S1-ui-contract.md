# S1 — Signal-first UI: contract (0.26.0)

Fixed interfaces for the S1 workers. Plan §2. Ids and API routes are
preserved; only user-facing labels change.

## Destinations (workspaces)

`WorkspaceId = "listen" | "discover" | "review" | "library" | "satellites"`,
tab order exactly that. Labels: LISTEN · DISCOVER · REVIEW · LIBRARY ·
SATELLITES. Number keys 1–5 switch. `review` is new. A saved
`settings.json` without a `review` (or any) workspace gets that workspace's
default layout filled in on load (never a 422, never a lost layout).

Section ids (`SECTION_IDS`, `SECTION_LABELS`) — existing ids keep their ids:

| id | label (user-facing) | workspace | notes |
|---|---|---|---|
| survey | WATCH SPECTRUM | listen, discover | rendered once in the primary slot, see below |
| scan | SCAN FREQUENCIES | discover | **new**: `<scanner-deck>` moved out of the strip |
| decode | DECODE | listen | **new**: `<decode-deck>` moved out of the strip |
| finds | FINDS | discover | |
| scans | ANALYZE RECORDING | discover | |
| beacons | BEACONS | discover | |
| nearby / recent / listen / log | unchanged | listen | |
| review | REVIEW | review | **new**: `<review-view>` |
| library | LIBRARY | library | **new**: `<library-view>` as a section; the old LIBRARY button switches to this workspace |
| inbox | INBOX | library | moved from listen |
| catalog | CATALOG | library | |
| passes / sky | unchanged | satellites | |

`WORKSPACE_SECTIONS` partitions all ids (settings.py assertion stays).
Defaults (format.js `DEFAULT_LAYOUT_COLUMNS` == settings.py `Layout()`):

- listen: columns `[["survey","listen","decode"],["nearby","recent"],["log"]]`, wide `["survey"]`
- discover: columns `[["survey"],["scan","finds"],["scans","beacons"]]`, wide `["survey","scan","finds"]`
- review: columns `[["review"],[],[]]`, wide `["review"]`
- library: columns `[["library"],["inbox"],["catalog"]]`, wide `["library"]`
- satellites: unchanged

Layout's validator backfills unknown-missing ids into the last column, so
old saved layouts migrate by themselves; a test proves a v0.25 `settings.json`
(no `review`, no `scan`/`decode`/`library` ids) loads and renders every id.

`libraryOpen` is removed from app.js. `toggle-library` → `_switchWorkspace("library")`.

## Primary spectrum slot

app.js renders `<survey-view>` **once**, in a fixed slot directly under the
tab strip, whenever `workspace ∈ {listen, discover}`; it is skipped in the
wide band / grid for those workspaces so the element instance survives a
Listen↔Discover switch (acquisition is server-side already; this keeps the
canvas, hover and selection state too). Selecting a row only selects;
Tune/Listen actions change the channel. The slot is not rendered in other
workspaces (the survey keeps running server-side).

app.js passes `.mode=${workspace}` (`"listen" | "discover"`, default
`"discover"`) to the same instance so the two tabs get different chrome
without ever re-creating it: `"listen"` renders the compact toolbar (span/
centre readout, PEAK/AVG, waterfall range controls) and hides OPTIONS,
IDENTIFY/analyzer start, the TECH toggle and the REPLAY picker, and its
table hides the technical (TECH) columns and the → DECODE column, keeping
Frequency/Candidate/Activity/Evidence/Last seen/TUNE/SAVE. `"discover"`
keeps every control exactly as before.

Default surface heights at ≥1280 px wide: spectrum 100 px, waterfall 150 px,
occupancy 24 px (survey-view `dimensions` defaults; still user-adjustable).

## Receiver strip (`<instrument-panel>`)

`Cmd/Ctrl-Z` undoes the newest confirmed VFO tune, saved SETUP patch, or
undoable Library memory edit. Tune and settings undo are limited to the current
app session (50 actions); Library memory undo uses the server's persistent
50-entry log. Text inputs keep native text-edit undo. Incoming decoder output
and recordings are received data and are never undo actions.
`Cmd/Ctrl-Shift-Z` reapplies the most recently undone action while its redo
history is still available. New edits clear redo; Library redo is held only
for the current server session.

Target 100–140 px tall at 1280×800 with the advanced area collapsed.

Row 1 (≈28 px): `MSDR` nameplate · LINK lamp (+ ⚠ LINK when there is a
problem) · receiver owner chip + `■ STOP` · **source chip** (`LIVE ·
SDRconnect` or `REPLAY · <file name>` from `state.survey.replay`; clicking it
switches to Review when replaying) · record state chip (from
`recorderStatus`: `ARMED`/`REC 00:12`/idle) · audio state (`AUDIO · SDRconnect`
static text until S3) · spacer · `★ CAPTURE` · `◂ ▸` history · `FIND` ·
`SETUP` · `MORE ▾/▴`.

Row 2 (≈60–70 px): `<segment-readout>` (compact size) + step ◂ [step] ▸ ·
Mode select · BW select · compact `<s-meter>` · `<recorder-control>` (ARM/REC/STOP).

Advanced (`MORE`, collapsed by default, remembered in localStorage
`msdr.strip.more`): Ant, LNA, Profile, `<detect-readout>`, NEW PROFILE,
`DIAG`, layout presets — everything else that used to be in the panel.
Scanner and decode decks are **no longer** children of the panel (they are
grid sections now; app.js stops passing their props).

Events dispatched are unchanged (`tune-frequency`, `step-frequency`,
`cycle-step`, `capture`, `back`, `forward`, `find`, `setup`, `new-profile`,
`toggle-diagnostics`, `owner-stop`, `interrupt-*`, `recorder-*`,
`open-recordings`, `apply-layout`, `dismiss-failure`) plus one new:
`open-review` (source chip click while replaying).

## Docked inspector (`<signal-panel>`)

≥1100 px: docked right column, 360 px, full height of the content area, no
dimming overlay, content reflows (`.app-body { display:flex }`). <1100 px:
existing sheet behaviour. Sections in order: summary, evidence, measurements,
decoder results, observations, notes. Escape closes; focus returns to the
opener.

## Review shell (`<review-view>`) — new, section id `review`

Props: `.replay` (= `state.survey?.replay ?? null`, the existing replay
status dict: path, name, center_hz, sample_rate_hz, duration_s, position_s,
playing, loop), `.recorderStatus`, `.tz`, `.failure`.
Loads `GET /api/recordings` itself (api.js `listRecordings`) when mounted and
on `refreshAt` change. Shows: recordings list (name, runs, centre/rate,
duration, capture start **only if** `recording.json`/SigMF `core:datetime`
is present, else "capture time unknown"), selected recording metadata,
replay transport (⏵/⏸, seek, loop) reusing the existing events
`survey-replay {path, loop}`, `survey-replay-control {playing, position_s,
loop}`, `survey-replay-stop`. Buttons for virtual receiver / timeline /
messages / clips / bookmarks / notes render **disabled with the text "later
release (S3/S5)"** — no fake affordances. The live receiver owner/record chips
stay visible in the strip above (unchanged).
`<recordings-view>` (U1 modal) gains a `▶ REVIEW` row action dispatching
`open-review {name, path}`; app.js switches to `review` and starts the replay
through the existing `_doSurveyReplay`.

## Task presets (`<preset-picker>`) — new modal

Data in `msdr/ui/presets.js`: `PRESETS = [{id, label, workspace, plan:
[strings], visible: {more: bool, tech: bool}, recommended: [strings]}]` for
`browse-local` (Browse local signals → listen), `monitor-saved` (Monitor
saved channels → discover), `explore-hf` (Explore HF → listen; recommended
USB/LSB/CW, HF field validation pending note), `review-recording` (Review
recording → review). The picker shows the plan **before** its Start button.
Start dispatches `preset-start {id, workspace, visible}` and nothing else:
app.js switches workspace and stores the visibility flags; it never retunes,
starts a decoder, arms recording or takes receiver ownership. Opened from the
palette (`presets…` command) and the strip's `MORE` area (`PRESETS`).

## Setup drawer additions

- `DEPENDENCIES` group: table of decoders from `.decoders` (GET
  /api/decoders) showing label · installed (readiness.installation) · tested
  (readiness.validation, "(stale)" when `validation_stale`) · install hint.
  Uses format.js `readinessLabels`.
- `LAYOUT` group gains an `EDIT LAYOUT` checkbox → dispatches `layout-edit
  {on}`; app.js stores it in localStorage `msdr.layoutEdit` and renders the
  drag grips / ⇔ WIDE toggles only while on.

## Survey table columns (`<survey-view>`)

Default columns: Frequency · Candidate/name · Activity (glyph **and** short
text: "voice", "data", "quiet", "keyed"…) · Evidence · Last seen.
Technical columns (SNR, BW, baud, modulation guess, duty…) behind a `TECH`
toggle (localStorage `msdr.survey.tech`). Evidence column text, from the
existing label dict (S2 replaces this with real claims):
`confirmed` decoder label → "decoded"; catalog/band-plan/schedule label only →
"context"; analyzer modulation/voice estimate → "estimated"; nothing → "—".
Never "confirmed"/"identified" from allocation alone; no percentages.

## Ownership

- W1 (shell): `msdr/ui/app.js`, `msdr/ui/format.js`, `msdr/ui/styles.css`,
  `msdr/ui/components/signal-panel.js`, `msdr/settings.py`,
  `msdr/api/schemas.py`, tests: `tests/api/test_app.py` (layout tests),
  `tests/ui/test_format_js.py`, settings tests, new `tests/api/test_workspaces_migration.py`.
- W2 (strip): `msdr/ui/components/instrument-panel.js`, `recorder-control.js`,
  `s-meter.js`, `segment-readout.js`, CSS in new `msdr/ui/styles-strip.css`
  (merged into styles.css by the coordinator).
- W3 (review/presets/setup/table): new `review-view.js`, `preset-picker.js`,
  `msdr/ui/presets.js`; `setup-drawer.js`, `survey-view.js`,
  `recordings-view.js`; CSS in new `msdr/ui/styles-review.css`.
- Coordinator: index.html/CSS merge, README, version, ledger, 1280×800 check.
