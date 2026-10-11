# S4 — Reliable decode and inference: contract (0.29.0)

Plan §3–§5, §8 (AIS is validation-led). Exit gate: single/band fixture parity;
corpus scorecard with negative controls; no fabricated integrity or identity;
every required supported path tested; AIS either validated on synthetic
frames with an explicit "field validation pending" state or abstained.

## 1. Event integrity and provenance (W2)

`DecodeEvent` (msdr/decoder/backends.py) gains optional fields with defaults:

```python
integrity: Literal["passed", "failed", "unknown", "unavailable", "not_applicable"] = "unknown"
integrity_note: str = ""          # e.g. "AX.25 FCS verified by direwolf", "multimon exposes no flag"
source_id: str | None = None      # "live:sdrconnect" | "replay:<name>"
generation: int | None = None     # source generation the input belonged to
sample_index: int | None = None   # run-local sample index of the input block (replay)
analysis_run_id: str | None = None  # set for replay/re-analysis so live counts are not inflated
```

Parsers set `integrity` from the capability matrix policy
(`msdr.decoder.capability_matrix.MATRIX[id].integrity_policy`), never guessed:
`fcs`/`crc` → "passed" (a line exists only because the check passed), `parity`
→ "passed"/"failed" from the decoder's own parity result (dcf77/msf expose it
in their event fields — read timecode.py), `builtin_check` → "passed" (wwv
marker/range check) , `fec` → navtex: "passed" when `errors == 0` for the
message else "unknown" with the count in the note, `none_exposed` →
"unavailable", images (wefax) → "not_applicable". `DecodeEventRepo` stores the
new columns (migration `013_decode_integrity.sql`: ALTER TABLE decode_events ADD
COLUMN integrity TEXT, integrity_note TEXT, source_id TEXT, generation INTEGER,
sample_index INTEGER, analysis_run_id TEXT) and returns them.

Manager/band status: `status()["decoders"][i]` gains `events`, `valid_frames`
(integrity == passed), `last_event_ts`, `synchronized: bool|None` (dsd-fme
"Sync:" lines only; None where the backend reports nothing), and a
`session_state` computed by `msdr.decoder.readiness.session_for` **per decoder**
(no more manager-wide `events_total` proxy): running + no events → acquiring;
events but valid_frames == 0 and policy none_exposed → unverified_output;
valid_frames > 0 → valid_frames; synchronized → synchronized; input rate
mismatch/unsupported demodulator → unsupported_input; error → error; quiet
(silence check) → quiet. `describe(session=...)` keeps working with the richer
dict.

## 2. Replay decode paths (W2)

`msdr/decoder/replay_decode.py`: `ReplayDecodeSession(receiver: VirtualReceiver,
source: IQSource, *, binaries, settings, on_event, on_status, calls_dir, fax_dir)`

- `start_single(decoder_ids)`: for each id: NFM-family (`"NFM" in spec.demodulators`)
  → feed from `receiver.flat_tap()` (float32 discriminator at 48 k) resampled to
  `spec.input_rate` (reuse `msdr/decoder/pcm.py` Resampler/Normalizer as band.py
  does) ; SSB/AM/CW decoders → feed from the receiver's *pre-volume, pre-squelch*
  demod audio (add a `clean_tap()` on VirtualReceiver returning the demod output
  before the listening gain/squelch; W1 owns virtual_receiver.py in S3 — in S4 W2
  may add this one method). Requires the receiver's demodulator to be
  compatible with the spec (`spec.demodulators`); otherwise the decoder is
  reported `unsupported_input` with the reason, not started.
- `start_band(decoder_ids, max_channels, threshold_db)`: `BandDecoder(
  iq_stream_factory=source.iq_stream, center_hz/sample_rate_hz from
  source.descriptor(), source_tag="replay-band", ...)`; on `SourceEvent("run")`
  retune the BandDecoder to the new span (existing `retune`); on any
  generation bump: drop queued frames, restart channel backends
  (PythonBackend: recreate the decoder; ProcessBackend: restart the process)
  so no stale symbols cross a seek.
- Every event gets `source_id`, `generation`, `sample_index` (from the frame/
  AudioBlock that produced the input) and `analysis_run_id` (uuid7 per
  start). Events flow through the same `on_event` → service publish path as
  live, but the service tags them so the S2 recorder treats them as review
  analysis (claims carry `analysis_run_id`; no live encounter counts change).
- `status()` mirrors the manager's shape (`mode: "single"|"band"`, decoders
  with the §1 facts, channels for band, `dropped_frames`, `restarts`).

Parity test (tests/decoder/test_parity.py): synthesise an AX.25 AFSK-1200
burst train (Bell 202 tones, HDLC flags, bit-stuffing, CRC-16/X.25 — write
the generator as a shared test helper `tests/helpers/ax25.py`, reusing the
PoC's deframing code path style from docs/decisions/poc/gmsk_ais_poc.py for
the CRC) FM-modulated onto IQ at 2 MS/s at +100 kHz offset in a small
synthetic SigMF recording; decode it once through `start_single(["aprs"])`
on a VirtualReceiver tuned to the channel and once through
`start_band(["aprs"])`; assert the same set of decoded frames (from/to/text)
and `integrity == "passed"` on both. `pytest.mark.skipif` when neither
direwolf nor multimon-ng resolves (record the skip reason; where
both exist so the test runs). Negative control: the same test on noise-only
IQ yields zero events on both paths.

## 3. AIS: bounded, validation-led (W1)

- `msdr/decoder/pydecoders/ais.py`: `AisDecoder(PyDecoder)`, `input_rate = 48000`,
  input = flat NFM discriminator audio (float32) at 48 k = 5 samples/symbol.
  Port of the PoC's `disc` path with attribution to SDRangel
  `plugins/channelrx/demodais/aisdemodsink.cpp` @ 6627a5cf (GPL-3.0-or-later,
  Jon Beniston M7RCE, Edouard Griffiths F4EXB) in the module header:
  training-sequence preamble correlation, training-only DC estimate, symbol
  timing, NRZI, HDLC flag/de-stuff/abort, CRC-16/X.25. Payload: 6-bit
  unpacking; message type, repeat, MMSI; for types 1/2/3/18: nav status (1-3),
  SOG, lon/lat (with the 181/91 "not available" sentinels), COG, true heading;
  type 5/24: only MMSI + type (no name parsing in S4). Each frame → one
  `DecodeEvent(kind="text", decoder="ais", integrity="passed",
  integrity_note="CRC-16/X.25 verified", fields={type, mmsi, ...})`. Frames
  failing CRC are counted (`status`-visible via a `stats()` dict: frames_ok,
  crc_fail, preambles) and NOT emitted as events.
- Registry: `DecoderSpec(id="ais", label="AIS (GMSK 9600)", kind="text",
  demodulators=("NFM",), default_bandwidth_hz=25_000, input_rate=48_000,
  backend="python", binaries=(), args=("msdr.decoder.pydecoders.ais:AisDecoder",),
  iq_only=True, phase=1)`. Capability matrix entry: input_path "iq",
  band_path True, integrity_policy "crc", validation `fixture_tested` (list the
  new tests), notes: "synthetic GMSK bursts only; field validation pending —
  161.975/162.025 MHz capture not accessible (tests/corpus/manifest.json
  ais-161975-162025)". Update `test_capability_matrix.py` parity (18 ids now)
  and the S0 baseline/corpus docs' "17 decoders" wording where it states a
  count (`docs/validation/corpus.md`, README line about DECODE if it lists
  decoders — add AIS there).
- Analyzer/families (msdr/survey/families.py, analyzer.py, features.py):
  - keep raw and snapped rates: analyzer fields gain `symbol_rate_raw_hz`,
    `symbol_rate_snapped_hz`, `symbol_rate_tolerance` (0.03); `symbol_rate_hz`
    stays as today (snapped else raw) for compatibility.
  - `family_for` returns `alternatives: list[str]` and `next_action: str`
    alongside `name`/`suggested_decoders`. New rows: fm_fsk/gmsk-like at 9600
    → name "GMSK/FSK 9600 (AIS or packet)", suggested ["ais", "packet9600"],
    alternatives ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s ×2)"],
    next_action "decode ais or packet9600; AIS identity requires a CRC-valid
    frame with an MMSI". The existing 9600 row is replaced by this one.
    `classify_modulation` must not return "psk_like" for a constant-envelope
    signal whose instantaneous-frequency histogram is bimodal at ±2400 Hz
    (GMSK 9600, h=0.5): add a bimodality check in features.py (a test with a
    synthetic GMSK burst must classify as fm_fsk with rate ≈ 9600 and the
    family above; a synthetic BPSK/QPSK burst must still be psk_like). Do
    not use frequency/allocation anywhere in these decisions.
  - Voice wording: family "NFM voice (analog)" only when modulation is fm-like
    AND voice score says speech; otherwise the label says "speech-like audio"
    (`family.name = "speech-like audio (demodulation untested)"` with
    next_action "listen / tested path: NFM discriminator") — update
    `_identity_notes` accordingly. Keyed carriers keep "keyed carrier" (S2
    recorder already maps ook to presence); CW family only when the on/off
    rate is < 100 Bd **and** dot/dash duration statistics fit (add a small
    check on run-length bimodality), else name "keyed carrier (unknown code)".
- Tests: tests/decoder/test_ais.py (synthetic bursts at 10/12/14/16/20 dB Eb/N0
  from the PoC generator moved into `tests/helpers/gmsk.py`; frame success
  ≥ 0.85 at 14 dB with 200 bursts, 0 CRC-false-passes on 10 s noise; payload
  fields for a known type-1 message decode exactly), tests/survey/test_families.py
  and test_features.py updates (GMSK → fm_fsk 9600 family, BPSK → psk_like,
  raw/snapped/tolerance fields), tests/decoder/test_registry.py 18 entries.

## 4. Service, evidence, scorecard (W3)

- Service/API: `POST /api/review/decode {decoders, mode: "single"|"band",
  max_channels?, threshold_db?}` → starts a `ReplayDecodeSession` bound to the
  running replay (409 without one); `POST /api/review/decode/stop`; `GET
  /api/review` gains `decode: session.status()|None`. Events are published
  like live decode events (`decoder_event` WS message) but with `source_id`
  starting with `replay:` and `analysis_run_id` set; the S2 recorder's
  `on_decode_event` passes `analysis_run_id` into claims and links the event to
  the observation for that replay session (no `heard_count`/find increments).
- `GET /api/signal-detail` and the S2 recorder use the analyzer's new fields:
  the candidate evidence item carries `support` (raw rate, snapped rate,
  tolerance, modulation kind), `alternatives`, `next_action`; decoded items
  carry `integrity` from the event, and `state` follows it (passed →
  validated; unavailable/unknown → estimated). Messages in signal-detail carry
  `integrity`/`integrity_note`.
- `GET /api/decoders`: `readiness.session` now reflects the per-decoder facts
  from §1 (events, valid_frames, synchronized).
- Scorecard: `msdr/validation/scorecard.py` runs a fixed synthetic corpus
  (each case is generated in code; nothing committed as IQ): NFM voice-like
  (speech-shaped noise), AFSK-1200 AX.25, keyed carrier (OOK 20 Bd), AM tone,
  USB/LSB tone, CW (proper timing), WFM + RDS (pilot and pilotless), GMSK 9600
  AIS burst; negative controls: white noise, single steady tone, clipped IQ
  (hard-limited), adjacent strong channel, dropped blocks. For each case it
  records, per detector (analyzer modulation/family/voice/keyed): correct /
  incorrect / abstained / unavailable, and per decoder (aprs via direwolf or
  multimon if present, ais, rds, cw via multimon if present): valid / invalid /
  unknown-integrity frame counts, expected-payload match, missed events, false
  output on negatives. Output: a dict + Markdown. `scripts/scorecard.py`
  writes `docs/validation/scorecard-synthetic.md` (commit the generated file
  once; the header says "synthetic fixtures only; not a claim of field
  accuracy"). `tests/corpus/test_scorecard.py` asserts the deterministic
  expectations (exact outcomes: negatives produce no decoder output and no
  protocol/station claim; AIS burst decodes; keyed carrier is not CW; noise is
  not voice) and that the Markdown renders.
- Corpus manifest: the AIS case gets `status: "synthetic_validated_field_pending"`
  with the tests named; the manifest schema test accepts the new status.

## 5. UI (wave 2, sonnet) — after §1–§4 land

- Decode deck + decode-view + signal-panel messages: integrity badge per
  message (✓ "FCS"/"CRC" when passed, ✗ failed, "no check" when unavailable,
  "—" not applicable) with the note as tooltip; per-decoder session facts live
  in the deck (acquiring / N events (unverified) / N valid frames / synchronized /
  quiet / unsupported input / error).
- Candidate card (signal-panel EVIDENCE for analyzer items): "Candidate: …
  Support: … Alternatives: … Next: …" using the new fields; never a percentage.
- Review: DECODE section (single: decoders compatible with the current
  virtual demodulator; band: NFM-family) with start/stop and the session
  facts; MESSAGES becomes real for the replay (decode events with
  `source_id == replay:<name>`), each with its sample index → JUMP seeks.
- Remove "later release (S4)" placeholders that are now real.

## Ownership (wave 1)

- W1 (opus): `msdr/decoder/pydecoders/ais.py`, `msdr/decoder/registry.py` (AIS
  entry), `msdr/decoder/capability_matrix.py` (AIS entry), `msdr/survey/families.py`,
  `msdr/survey/features.py`, `msdr/survey/analyzer.py` (fields + wording),
  `tests/helpers/__init__.py`, `tests/helpers/gmsk.py`, `tests/decoder/test_ais.py`,
  `tests/decoder/test_registry.py`, `tests/decoder/test_capability_matrix.py`,
  `tests/survey/test_families.py`, `tests/survey/test_features.py`,
  `tests/survey/test_analyzer.py`, `docs/validation/corpus.md` (count wording).
- W2 (opus): `msdr/decoder/backends.py`, `msdr/decoder/manager.py`,
  `msdr/decoder/band.py`, `msdr/decoder/readiness.py`, `msdr/decoder/replay_decode.py`
  (new), `msdr/decoder/pydecoders/{navtex,timecode,wefax}.py` (integrity only),
  `msdr/source/virtual_receiver.py` (`clean_tap()` only), `msdr/storage/repos.py`
  (DecodeEventRepo columns), `msdr/storage/migrations/013_decode_integrity.sql`,
  `tests/helpers/ax25.py`, `tests/decoder/test_parity.py`, `tests/decoder/test_backends.py`,
  `tests/decoder/test_manager.py`, `tests/decoder/test_readiness.py`, `tests/storage/test_repos.py`.
- W3 (sonnet): `msdr/api/app.py`, `msdr/api/service.py`, `msdr/api/schemas.py`,
  `msdr/api/signal_detail.py`, `msdr/observations/recorder.py` (analysis_run_id +
  integrity + candidate support), `msdr/validation/__init__.py`, `msdr/validation/scorecard.py`,
  `scripts/scorecard.py`, `docs/validation/scorecard-synthetic.md`,
  `tests/corpus/manifest.json` + `tests/corpus/test_manifest.py` (status),
  `tests/corpus/test_scorecard.py`, `tests/api/test_review.py` (decode routes),
  `tests/api/test_signal_detail.py`, `tests/observations/test_recorder.py`.
  W3 waits for W1/W2 modules where it depends on them (AIS decoder, new event
  fields, families output) and codes against the names above meanwhile.
- Coordinator: review AIS DSP + parity evidence, integration, wave-2 UI, README,
  version, ledger, corpus manifest sanity.
