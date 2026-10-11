# S2 — Observations and honest evidence: contract (0.27.0)

Plan §3, §7. Additive: nothing existing is renamed or dropped. `Signal`
stays a reusable memory (not a transmitter id). New objects: Session,
Observation, Claim (from `msdr.contracts.evidence`), RecordingReference.
Frequency proximity *suggests* a link and never proves identity.

## Storage — migration `012_observations.sql` (additive)

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,          -- "live:sdrconnect" | "replay:<recording name>"
  mode TEXT NOT NULL,               -- "live" | "replay"
  origin TEXT NOT NULL DEFAULT '',  -- adapter host or recording path
  started_at REAL NOT NULL,         -- epoch s (processing clock)
  ended_at REAL,
  center_hz INTEGER, sample_rate_hz INTEGER,
  receiver_json TEXT,               -- antenna/gain/profile snapshot
  notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE observations (
  id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES sessions(id),
  signal_id TEXT,                   -- optional memory link; may stay NULL
  find_id TEXT,                     -- scanner find this came from (legacy import or live hit)
  start_ts REAL, end_ts REAL,       -- original capture time when known, else NULL
  sample_start INTEGER, sample_end INTEGER,  -- replay: sample range on the source
  reported_hz INTEGER,              -- asserted/tuned frequency
  measured_hz INTEGER,              -- centroid; kept separate, never overwrites reported
  bandwidth_hz INTEGER,
  receiver_json TEXT,               -- demodulator/filter/antenna/gain snapshot
  measurements_json TEXT NOT NULL DEFAULT '[]',   -- list[Measurement]
  capture_quality_json TEXT NOT NULL DEFAULT '[]',-- list[CaptureQuality]
  legacy INTEGER NOT NULL DEFAULT 0, -- 1 = summary imported from finds/decode_events
  provenance TEXT NOT NULL DEFAULT '', -- e.g. "survey label", "scanner hit", "migrated: finds"
  closed INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX idx_obs_freq ON observations(reported_hz);
CREATE INDEX idx_obs_signal ON observations(signal_id);
CREATE INDEX idx_obs_session ON observations(session_id);
CREATE TABLE claims (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id),
  subject TEXT NOT NULL, value TEXT NOT NULL,
  evidence_type TEXT NOT NULL, integrity TEXT NOT NULL,
  producer TEXT NOT NULL, producer_version TEXT NOT NULL DEFAULT '',
  observed_start REAL, observed_end REAL, sample_start INTEGER, sample_end INTEGER,
  support_json TEXT NOT NULL DEFAULT '[]', measurements_json TEXT NOT NULL DEFAULT '[]',
  alternatives_json TEXT NOT NULL DEFAULT '[]', limitations_json TEXT NOT NULL DEFAULT '[]',
  score REAL, next_action TEXT NOT NULL DEFAULT '',
  analysis_run_id TEXT,             -- review reruns carry a run id; never inflate live counts
  superseded_by TEXT,               -- append-only: newer claim id
  created_at REAL NOT NULL
);
CREATE INDEX idx_claims_obs ON claims(observation_id);
CREATE TABLE recording_refs (
  id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL REFERENCES observations(id),
  path TEXT NOT NULL, name TEXT NOT NULL,
  run_index INTEGER, sample_start INTEGER, sample_end INTEGER,
  available INTEGER NOT NULL DEFAULT 1,   -- 0 = media missing/deleted: explicit unavailable
  note TEXT NOT NULL DEFAULT ''
);
ALTER TABLE decode_events ADD COLUMN observation_id TEXT;
ALTER TABLE finds ADD COLUMN observation_id TEXT;
```

Timestamps are epoch seconds (REAL) like `decode_events.ts` / `finds.*_ts`.

## Python package `msdr/observations/`

- `models.py`: pydantic `Session`, `Observation`, `RecordingReference`
  (fields = columns; `measurements: list[Measurement]`,
  `capture_quality: list[CaptureQuality]`); re-export `Claim` from
  `msdr.contracts.evidence`.
- `repo.py`: `ObservationRepo(db)` — **this interface is fixed** (W2 codes
  against it while W1 implements it):

```python
class ObservationRepo:
    # sessions
    def open_session(self, session: Session) -> Session
    def close_session(self, session_id: str, ended_at: float) -> None
    def get_session(self, session_id: str) -> Session | None
    # observations
    def add(self, obs: Observation) -> Observation
    def update(self, obs: Observation) -> None            # measurements/quality/end/close
    def close(self, observation_id: str, end_ts: float | None, sample_end: int | None) -> None
    def get(self, observation_id: str) -> Observation | None
    def link_signal(self, observation_id: str, signal_id: str | None) -> None
    def list(self, *, frequency_hz: int | None = None, tolerance_hz: int = 6000,
             signal_id: str | None = None, session_id: str | None = None,
             include_legacy: bool = True, limit: int = 50) -> list[Observation]
    def open_near(self, frequency_hz: int, tolerance_hz: int, session_id: str) -> Observation | None
    # claims (append-only)
    def add_claim(self, claim: Claim) -> Claim
    def supersede(self, old_claim_id: str, new: Claim) -> Claim   # sets old.superseded_by
    def claims(self, observation_id: str, *, include_historical: bool = True) -> list[Claim]
    # recording refs
    def add_ref(self, ref: RecordingReference) -> RecordingReference
    def refs(self, observation_id: str) -> list[RecordingReference]
    def mark_unavailable(self, path: str) -> int
    # links
    def link_decode_event(self, event_id: str, observation_id: str) -> None
    def decode_event_ids(self, observation_id: str, limit: int = 50) -> list[str]
    # summary for API rows
    def summary(self, obs: Observation) -> dict   # obs + evidence_state + claim counts + refs
```

`summary()` returns `{**obs.model_dump(mode="json"), "evidence_state": evidence_state(current
claims), "identity_established": bool, "claims": [claim dicts, historical flagged],
"refs": [...], "decode_event_ids": [...]}`.

- `recorder.py`: `ObservationRecorder(repo, *, now=time.time)` — pure logic, unit-tested with dict fixtures:
  - `start_session(source_id, mode, origin, center_hz, sample_rate_hz, receiver) -> Session`;
    `end_session()`.
  - `sync_labels(labels: list[dict], *, sample_index: int | None)` called by the survey **at
    label boundaries only** (a label appears, a dimension result lands: analyzer/family/voice,
    catalog/bandplan, `confirmed`, `rds`; a label retires). Diffs against its own cache keyed by
    label id: opens an Observation (`provenance="survey label"`, reported_hz = label
    frequency, bandwidth = width_hz, measurements: snr dB method "spectrum_display_snr",
    duty % with denominator "occupancy window" + window_s), appends Claims when a dimension
    first appears or its value changes (never every frame), closes the Observation when the
    label retires (end = now / sample_end).
  - Claim mapping (subject, evidence_type, integrity):
    - bandplan → (`service`, `allocation`, `not_applicable`), value = plan name;
    - catalog entry → (`station`, `schedule`, `not_applicable`), value = entry name,
      limitations ["schedule predicts a transmitter; not reception"];
    - modulation → (`modulation`, `iq_estimate`, `unknown`), value kind, score = analyzer
      confidence (unitless), alternatives from family alternatives if present;
    - family → (`protocol`, `iq_estimate`, `unknown`), value family name, support: symbol rate
      raw + snapped, next_action "decode <suggested decoders>";
    - voice `speech`/`data` → (`modulation`, `iq_estimate`), value "speech-like audio" /
      "data-like"; `quiet` → capture_quality `quiet`, no claim;
    - activity keyed/ook → (`presence`, `iq_estimate`), value "keyed carrier" (never "CW");
    - confirmed decoder → per `msdr.decoder.capability_matrix.MATRIX[id].integrity_policy`:
      `fcs`/`crc`/`parity`/`fec`/`builtin_check` → (`protocol`, `validated_decode`, `passed`);
      `none_exposed` → (`protocol`, `decoder_output`, `unavailable`) with limitation
      "backend exposes no per-message integrity flag";
    - rds → PI: (`station`, `validated_decode`, `passed`) value `PI 0xXXXX` when groups>0;
      PS: separate (`station`, `decoder_output`, `unknown`) value PS text with limitation
      "PS text can change"; callsign lookup is NOT done here.
  - `on_decode_event(event: dict)`: links the event to the open observation near its
    frequency in the current session (tolerance 6000 Hz) or opens one
    (`provenance="decode event"`), then `repo.link_decode_event`. Does not add claims (the
    survey's `confirmed` path does, or, without a survey, adds one decoder_output/validated claim
    per decoder per observation — first event only).
  - `on_scanner_hit(hit: dict, find_id: str)`: opens+closes an observation for the hit's dwell
    (`provenance="scanner hit"`, measurement snr).
- `legacy.py`: `import_legacy(db, repo) -> dict` — runs once (settings key
  `observations.legacy_import_done`): for each find without `observation_id` create ONE
  Observation `legacy=1`, `provenance="migrated: finds (frequency aggregate, seen_count=N)"`,
  start/end = first/last ts, reported_hz, bandwidth, link find; for decode_events without
  `observation_id`, group by (decoder, frequency within 6 kHz, gap ≤ 300 s) → one Observation
  `legacy=1`, `provenance="migrated: decode_events (N events)"` with one Claim per group per the
  integrity policy above; link events. Never fabricates per-event receptions beyond the events
  themselves. Returns counts. `backup_db(path) -> Path` copies the SQLite file to
  `<name>.bak-<YYYYmmdd-HHMMSS>` before the first import (skip for `:memory:`).

## Service / API (W2)

- `AppService.observations = ObservationRepo(db)`; `AppService.obs_recorder =
  ObservationRecorder(repo)`. On startup after `db.connect()`: `backup_db` + `import_legacy`
  once (log counts).
- Sessions: survey start (live) → `start_session("live:sdrconnect", "live", host,…)`; replay
  start → `start_session("replay:<name>", "replay", path,…)`; survey stop / replay stop →
  `end_session()`. Session id carried in survey status as `session_id`.
- Survey boundary hook: `Survey` accepts an optional `on_labels_changed(labels)` callback
  (constructor kwarg or setter) invoked only from the places labels change: track appears,
  `_confirm`, analyzer result applied, catalog/bandplan applied, retire. The service passes
  `obs_recorder.sync_labels`. **No call per spectrum frame.**
- `DecodeEventRepo.add` callers in service → also `obs_recorder.on_decode_event(event)`.
- Scanner find logging (`FindRepo.add_or_merge` call site) → `obs_recorder.on_scanner_hit`.
- Routes:
  - `GET /api/observations?frequency_hz&tolerance_hz&signal_id&session_id&limit` → list of
    `summary()` rows, newest first.
  - `GET /api/observations/{id}` → summary.
  - `PATCH /api/observations/{id}` body `{signal_id: str|null}` → link/unlink memory (the
    response includes `"note": "link suggests, does not prove identity"` — literal).
  - `POST /api/observations/{id}/claims` body `{subject, value, rationale}` → operator claim
    (`evidence_type="operator"`, `integrity="not_applicable"`, producer "operator", support
    [rationale]). Optional `supersedes: claim_id`.
  - `GET /api/signal-detail` gains `observations: [summary rows near frequency (limit 10)]`
    and each existing `evidence` item gains `state` (context|estimated|validated|operator),
    `evidence_type`, `score` (unitless or null) and **loses `confidence`**. `_rds_evidence`
    splits PI (validated) from PS (decoder_output). Catalog on-air filtering keeps using the
    internal score threshold; it is not shown.
- Schemas in `msdr/api/schemas.py`: `ObservationPatch`, `OperatorClaimIn`.
- Tests: tests/api/test_observations.py (routes, restart persistence: write, close app, reopen
  same DB path, still listed), test_signal_detail.py updates (no `confidence` key; `state`).

## UI (W3)

- `api.js`: `listObservations(params)`, `getObservation(id)`, `patchObservation(id, body)`,
  `addOperatorClaim(id, body)`.
- `signal-panel.js`: EVIDENCE cards render `state` chip (context / estimated / decoded /
  validated / operator) + title + detail + limitations + integrity word + next action; **no
  percentages anywhere**. New OBSERVATIONS section (between Decoder results and Notes): rows
  `when (capture time or "time unknown") · source (live/replay name) · evidence state ·
  N claims · legacy badge`; click → expands claims (historical ones greyed with "superseded");
  actions: LINK TO THIS MEMORY / UNLINK (when a memory is shown), IDENTIFY… (operator claim
  form: subject select station/protocol/service, value, rationale — required).
- `detect-readout.js`, `ident-panel.js`: replace the confidence meter with the candidate text
  plus a state word ("estimated"); no bars, no %.
- `format.js`: remove "How sure the analyzer is…%" tooltip text; add `evidenceStateLabel()`.
- `survey-view.js`: nothing besides confirming no `%` remains outside TECH `score`.
- tests/ui/test_format_js.py: a test that greps `msdr/ui/components/*.js` for
  `Math.round(.*confidence.*100)` / `confidence\s*\*\s*100` and asserts none.

## Ownership

- W1 (storage, opus): `msdr/storage/migrations/012_observations.sql`, `msdr/observations/*`,
  `tests/observations/*`, `tests/storage/test_repos.py` (migration count test if any).
- W2 (service/API, sonnet): `msdr/api/app.py`, `msdr/api/service.py`, `msdr/api/schemas.py`,
  `msdr/api/signal_detail.py`, `msdr/survey/survey.py` (callback hook only),
  `msdr/engine/scanner.py` (hit hook only, if the find call site is there), `tests/api/*`.
- W3 (UI, sonnet): `msdr/ui/api.js`, `msdr/ui/components/signal-panel.js`, `detect-readout.js`,
  `ident-panel.js`, `msdr/ui/format.js`, `tests/ui/*`, CSS appended to `msdr/ui/styles.css`.
- Coordinator: migration review on a copy of the real DB (`~/Library/Application Support/msdr/msdr.db`
  → `$TMPDIR`), integration, README, version, ledger.
