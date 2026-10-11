# S5 — Complete investigation workflow: contract (0.30.0)

Plan §9 acceptance: *capture a pre-roll event, attach notes, restart and return
to its exact interval*; lifecycle cases show partial/unavailable outcomes,
never silent success; bounded queues, visible loss, responsive Stop; an
8-hour soak on the target machine before any reliability claim (cannot run
here — a soak script ships and the gate stays open); HF field validation
listed as unresolved wherever validation status is shown.

## 1. Storage / service / API (W1, opus)

- Migration `014_observation_notes.sql`: `ALTER TABLE observations ADD COLUMN notes
  TEXT NOT NULL DEFAULT ''`. `Observation.notes: str = ""`; repo `set_notes(id, text)`;
  `PATCH /api/observations/{id}` accepts `notes` (alongside `signal_id`).
- **Recording → observation**: when the recorder promotes the pre-roll into a
  recording (`AppService.recorder_record`, and the recorder's own state change
  to "recording"), open an Observation `provenance="recording"`, `reported_hz` =
  current VFO, `bandwidth_hz` = filter, receiver snapshot, `start_ts` = the
  recorder's `start_wall` **minus the pre-roll seconds actually promoted**
  (the recorder knows; if it does not expose it, use start_wall and note
  "pre-roll length not recorded" in `provenance`), plus a `RecordingReference`
  to the recording directory (`run_index=0, sample_start=0`). When the
  recording finalizes (`recorder_stop` / auto-stop / disarm), close it with
  `end_ts`, `sample_end` = total samples of the first run, and refresh the
  reference `path/name` to the finalized directory. Auto-disarm for low disk
  or an error closes it with `capture_quality += ["missing_frames"]` and a
  provenance note. Look at msdr/recorder/recorder.py `record()/stop_recording()/
  _finalize()/_auto_disarm()` and the `_notify` status callback the service
  already consumes.
- `POST /api/observations/{id}/promote {name?}` → creates a `Signal` from the
  observation (frequency = `measured_hz or reported_hz`, demodulator/bandwidth
  from `receiver`, `status="unknown"`, notes = observation notes, decoders =
  decoders of `validated_decode` claims) and links it (`link_signal`); response
  `{signal, observation, note: "link suggests, does not prove identity"}`.
  Mirror `promote_find` in service.py.
- `GET /api/observations/{id}/export` → JSON `{observation, claims (all, incl.
  historical), refs, decode_events (full rows), session, exported_at,
  msdr_version}`; `GET /api/observations/export?since_ts=&limit=` → the same
  per observation as a JSON array. No import in S5.
- **Clips**: `POST /api/review/clips {start_s, end_s, note}` (replay running;
  0 < end − start ≤ 600 s; 409 otherwise) copies the sample range of the
  current run (**one run only**: a range crossing a run boundary is 409 with
  the reason) into `<recorder_root>/clips/<recording>_<start_ms>-<end_ms>/
  run-01_<rate>SPS_<center>Hz.sigmf-data|.sigmf-meta` + `clip.json` (parent
  recording path, run_index, sample_start/sample_end, note, created_at,
  capture_start if the parent run has one). Writes go through
  `asyncio.to_thread`; a failed write (permission/no space) is a 503 with the
  OS error text. Creates Observation `provenance="clip"` with two
  RecordingReferences: the clip (available=1) and the parent (sample range).
  `GET /api/review/clips?path=` lists clips of the current recording (join
  refs by parent path). Setting `clips_dir: str | None = None` (None →
  `<recorder_root>/clips`) in Settings + README settings table.
- **Deleted recording**: `recordings_delete` calls
  `observations.mark_unavailable(path)` and the review status/observation
  summaries expose `refs[].available` (already there) — plus `GET /api/review`
  returns 404 `"recording no longer exists"` for bookmark/clip seeks whose
  ref is unavailable.
- **Lifecycle tests** (tests/api/test_workflow.py, real on-disk DB in tmp):
  1. **End-to-end acceptance**: with the recorder driven by the FakeStream
     from tests/recorder/conftest.py (build the app with a recorder whose
     stream is fake — see how tests/api/test_recorder.py does it), ARM → REC →
     STOP; assert an Observation `provenance="recording"` exists with a
     RecordingReference to the finalized directory; PATCH notes "NOAA test";
     POST a bookmark after starting a replay of that recording at
     position 0.5 s; **close the app and reopen with the same DB path**; GET
     the observation → notes intact, refs available; start the replay again,
     seek the bookmark → `replay.position_s` equals the bookmark's elapsed
     within 1 block; `fake.set_count()` unchanged by everything after ARM/REC
     (replay never wrote to the radio).
  2. Deleted recording → refs unavailable; bookmark seek → 404 with reason.
  3. Clip: create, listed, its SigMF meta/data consistent (samples ==
     (end−start)·rate), observation with two refs; range across runs → 409;
     unwritable clips_dir → 503.
  4. Promote → Signal exists, linked, note text present.
  5. Export JSON contains claims incl. superseded ones.
  6. Stop replay while a review decode session runs → both stop, no exception,
     `GET /api/review` shows `decode: null`.

## 2. Validation status, soak, robustness (W2, sonnet)

- `msdr/validation/status.py`: `validation_status(settings) -> dict` combining:
  corpus manifest cases (id, class, status, files located?), capability matrix
  validation per decoder (state, stale?, tests), scorecard summary (parse
  `docs/validation/scorecard-synthetic.md`'s generated timestamp + counts or
  regenerate summary counts from `msdr.validation.scorecard` results if cheap —
  do NOT run the decoders at request time; read the markdown), and
  `outstanding_gates`: fixed list — HF field validation (no off-air HF
  recordings), private VHF captures not accessible (manifest
  `pending_intake`/`inaccessible`), AIS field validation pending, 8-hour soak
  on target hardware not run, stereo WFM not implemented. Pure function,
  tested. The coordinator mounts it at `GET /api/validation`.
- `scripts/soak.py`: runs msdr against `FakeSDRconnect` in-process for
  `--minutes N` (default 480): survey on with analyzer dims, band decode with
  `pocsag`+`aprs` (skips missing binaries, says so), a replay loop of a
  synthetic 2-run recording with a review decode session restarted every
  60 s, periodic seek; samples every 10 s: RSS (resource.getrusage), event
  loop lag (asyncio timer drift), queue drops (`survey status`, band
  `dropped_frames`, replay `dropped_frames`/`behind_s`, virtual rx
  `dropped_blocks`), open task count; writes `soak-<timestamp>.jsonl` +
  a summary; exits non-zero if RSS grows > 30 % from minute 2 to the end or
  loop lag p99 > 250 ms. Add `tests/validation/test_soak_smoke.py` running the
  soak for 20 s with a small config (marked slow but on by default; must run
  < 40 s) and asserting a summary is produced with no exception. Document in
  README ("Validation status" section, coordinator) that the 8-hour run on the
  target Mac is **pending**.
- `tests/decoder/test_lifecycle.py`: (a) cancel decoder startup: start_decoding
  then stop before the backend reports running → no leaked process/task
  (use the fake backend fixtures in tests/decoder/conftest.py); (b)
  incompatible input: a decoder started on a receiver whose demodulator is
  not in `spec.demodulators` reports `session_state == "unsupported_input"`
  with a reason (ReplayDecodeSession); (c) source switch with callbacks
  outstanding: ReplaySource seek + stop while ReplayDecodeSession pump is
  mid-block → clean stop, restarts counted, no exception; (d) all scan
  channels ignored → scanner status shows a visible "all channels ignored"
  reason (check engine/scanner.py's existing behaviour from 0.24.0 and assert
  the status text rather than adding engine logic unless missing).

## 3. UI (W3, sonnet)

- Inspector OBSERVATIONS rows: NOTES editor (textarea, SAVE → PATCH notes),
  PROMOTE TO MEMORY (→ `/promote`, then opens the memory), EXPORT (download
  `/export` JSON via a blob link), shows `refs[].available === false` as
  "recording missing" with the path.
- Review: NOTES section becomes real for the recording's own observation
  (`provenance="recording"` whose ref path == replay path; if none, offer
  "create note" which makes a bookmark-style observation at position 0);
  CLIPS section: MARK IN / MARK OUT buttons take the current position (shown
  as file-relative time and, when known, capture time), note input, CREATE
  CLIP → POST /api/review/clips; list clips with JUMP (seek to start) and
  the clip path; errors (409/503) shown verbatim. Remove the last "later
  release" placeholders.
- SETUP → VALIDATION group (from `GET /api/validation`): table of corpus
  cases with status words, decoder validation states (stale marked), the
  scorecard date, and an **OUTSTANDING GATES** list that always shows HF field
  validation as unresolved. Never a green "all validated" summary: the header
  reads "Validation status (synthetic + reference fixtures; field gates
  listed below)".
- Scan frequencies setup modal (`scanner-view.js`): group the existing timing
  knobs under "TIMING" with labels *Dwell (measure)*, *Hang (stay after activity
  ends)*, *Finds dwell (log then move on)* and the existing keep-scanning
  toggle labelled *Monitor only (log finds, don't hold)*; one sentence of help
  text; no engine changes. Presets: "Monitor saved channels" Start also
  preselects the scan deck's list source when a list exists (UI-only; still
  no START).
- `api.js` helpers for the new routes; tests/ui: extend format tests only if a
  pure helper is added.

## Ownership

- W1: `msdr/storage/migrations/014_observation_notes.sql`, `msdr/observations/*`,
  `msdr/api/service.py`, `msdr/api/app.py`, `msdr/api/schemas.py`, `msdr/settings.py`
  (clips_dir), `tests/api/test_workflow.py`, `tests/observations/*`, `tests/api/test_review.py`
  (clips), README settings row for `clips_dir` (coordinator merges wording).
- W2: `msdr/validation/status.py`, `scripts/soak.py`, `tests/validation/__init__.py`,
  `tests/validation/test_status.py`, `tests/validation/test_soak_smoke.py`,
  `tests/decoder/test_lifecycle.py`.
- W3: `msdr/ui/components/{signal-panel,review-view,setup-drawer,scanner-view}.js`,
  `msdr/ui/api.js`, `msdr/ui/presets.js`, `msdr/ui/app.js` (pass `.validation`
  to setup-drawer, fetched from `GET /api/validation` when SETUP opens; route
  `open-memory` after promote), `msdr/ui/styles.css` (append).
- Coordinator: mount `GET /api/validation`, README (release notes, Validation
  status section, settings row), ledger, memory notes, final full suite +
  browser gate, commit 0.30.0.
