# S3 — Source-independent playback and audio: contract (0.28.0)

Plan §6 (+ reuse decisions in `docs/decisions/2026-09-27-reuse-before-rebuild.md`:
retain `msdr/survey/replay.py`, `rds.py` WfmChannel/RdsDemod and the channelizer;
implement AM/SSB/CW independently in numpy; NFM keeps flat decoder taps separate
from listening DSP).

Exit gate: zero live-radio writes during replay tuning/listening/decoding;
deterministic source clock + generation tests; audible synthetic fixtures for
NFM/AM/USB/LSB/CW/WFM + RDS; no stale audio/results after seek.

## 1. Source contract — `msdr/source/` (W2)

```python
class IQSource(Protocol):                       # msdr/source/base.py
    source_id: str                              # "live:sdrconnect" | "replay:<name>"
    def descriptor(self) -> SourceDescriptor    # msdr.contracts.source; sample_index = clock
    def iq_stream(self) -> IQStreamLike         # start/stop/enable/frames() of IQFrame
    def acquire_spectrum(self) -> asyncio.Queue
    def release_spectrum(self, q) -> None
    def add_listener(self, cb: Callable[[SourceEvent], None]) -> None
    def remove_listener(self, cb) -> None

@dataclass(frozen=True)
class SourceEvent:                               # emitted synchronously on the loop thread
    kind: Literal["generation", "run", "end", "position"]
    generation: int
    sample_index: int                           # position in the *current run*
    run_index: int                              # 0 for live
    descriptor: SourceDescriptor                # after the change
    discontinuity: Discontinuity | None
```

`IQFrame` gains two optional fields (default 0/None so every existing producer
keeps working): `generation: int = 0`, `sample_index: int | None = None`
(index of the frame's first sample in the run). ReplayIQStream sets both;
live IQStream leaves them default. Consumers that care (virtual receiver,
S4 decoders) drop frames whose generation != their own and re-init.

### Replay: `ReplaySource` (extend `msdr/survey/replay.py`, keep the class)

- `RecordingTimeline` (new, same module or `msdr/source/timeline.py`): built from a
  recording directory's `recording.json` (`runs[]`: data, meta, sample_rate,
  center_hz, samples, start_wall) when present, else from the sorted
  `*.sigmf-meta` files (one run each; `core:datetime` → start_wall if present),
  else one run for a WAV (start_wall None). Each run: `index, path, sample_rate_hz,
  center_hz, samples, start_wall|None, gap_before_samples|None` where the gap is
  the count from the previous run's `gap` annotations / recording.json when
  known, else None (unknown, never invented). `elapsed_s(run, local_sample)` =
  sum(prev runs' samples/their own rate) + local/rate (gaps of unknown length
  contribute 0 and are reported as `gap_unknown=True` on the run).
  `locate(elapsed_s) -> (run_index, local_sample)`; `total_elapsed_s`.
- `IQFile` no longer skips retuned segments: it exposes `timeline` and reads
  within one run at a time (`read(run_index, start, count)`); the legacy
  single-span `read(start, count)` keeps working for single-run files.
- `ReplaySource`:
  - `generation` (int), `run_index`, `sample_index` (run-local clock), `descriptor()`.
  - `seek(position_s)` → `timeline.locate`; if the run changes: update
    `center_hz`/`sample_rate_hz` and emit `SourceEvent("run")`; every seek emits
    `SourceEvent("generation")` with `Discontinuity(kind="seek"|"retune")`.
    Loop wrap → `generation` bump with `Discontinuity("loop")`. Run boundary
    reached while playing → bump with `Discontinuity("retune")` (or `"gap"` when a
    gap precedes the run, `gap_samples` from the timeline or None).
  - **No silent skipping.** Remove the `_MAX_LAG_S` catch-up jump: when behind,
    keep emitting blocks back-to-back; `status()["behind_s"]` reports the lag.
    Pause keeps the clock still; `position_s` = timeline elapsed.
  - `status()` adds: `generation`, `run_index`, `runs: [timeline runs as dicts]`,
    `capture_start: start_wall|None` of the current run, `behind_s`, `source_id`.
  - Every emitted IQFrame carries `generation` and `sample_index`.
- Live: `LiveSource` (msdr/source/live.py) is a thin adapter over the existing
  adapter+`IQStream`+`SpectrumHub` exposing the same protocol (generation bumps
  on centre/rate change events from the adapter; sample_index unknown → 0).
  It does **not** change how the live path works today; S3 only needs it for
  routing and status.

Tests (tests/source/): timeline mapping with a 3-run fixture (rate change,
centre change, one known gap, one unknown gap); seek across runs updates
descriptor and bumps generation; loop bump; frames carry monotonically
increasing sample_index within a generation; a frame produced before a seek
(old generation) is identifiable; no sample skipped under an injected slow
consumer (count samples delivered == samples in file for a non-loop replay).

## 2. Virtual receiver + demodulators — `msdr/source/dsp.py`, `msdr/source/virtual_receiver.py` (W1)

All numpy, streaming (state carried across calls), int16 interleaved IQ in,
`AudioBlock(pcm: bytes s16 mono, rate: int, generation: int, sample_index: int)` out.

- `ChannelExtractor(fs_in, offset_hz, out_rate)`: frequency shift (NCO with phase
  carried across blocks) + FIR low-pass + integer decimation (choose a stage
  plan for fs_in in {≤250 kS/s, 2, 8, 10 MS/s} → out_rate 48 000 (voice modes)
  or 250 000 (WFM). Reuse the two-stage idea of `rds.WfmChannel` for WFM
  (`WfmChannel` itself stays the MPX producer).
- Demodulators (each a class with `process(iq: complex64 block) -> float32 audio at 48 k`,
  `reset()`):
  - `NfmDemod(bw_hz)`: quadrature discriminator → **flat tap** (`.flat`, for
    decoders, unfiltered beyond the channel filter) and a **listening** path:
    de-emphasis 750 µs, 300–3000 Hz audio band-pass, squelch (noise-based,
    threshold param, opens/closes with hysteresis), volume. Two outputs from one
    discriminator; the listening DSP never feeds decoders.
  - `AmDemod(bw_hz)`: envelope (|z|) → DC block (one-pole, ~0.5 Hz) → AGC (slow
    attack/decay) → 5 kHz low-pass.
  - `SsbDemod(side: "USB"|"LSB", bw_hz)`: complex band-pass by shifting the
    channel so the wanted sideband sits at 0–bw (USB: +bw/2 offset; LSB mirror via
    conjugate), real part → AGC. Pitch is exact (test: a carrier at +1 kHz USB
    gives a 1 kHz tone; LSB at −1 kHz gives 1 kHz; USB with −1 kHz gives ~nothing).
  - `CwDemod(bw_hz=500, pitch_hz=700)`: SSB with the carrier placed at `pitch_hz`
    (a keyed carrier on the channel centre produces a 700 Hz tone while keyed).
  - `WfmDemod()`: `rds.WfmChannel`-style MPX at ~250 k → 15 kHz low-pass → decimate
    to 48 k → de-emphasis 75 µs (US) → mono audio. `.mpx` output feeds
    `rds.RdsDemod`/`RdsParser` (reuse; do not modify rds.py). Stereo: not in S3
    (mono/pilotless must not regress: test with pilot_level=0).
- `VirtualReceiver(source: IQSource, *, audio_rate=48000, queue_max=32)`:
  - `tune(frequency_hz, demodulator, bandwidth_hz)` → validates with
    `ChannelDescriptor.fits(descriptor, margin_hz=filter margin)`; raises
    `OutOfSpanError(reason)` otherwise (the API turns it into a 409 with the
    reason text). Rebuilds the extractor/demod, resets state, bumps its own
    `channel_generation`.
  - Consumes `source.iq_stream()`; drops frames with `generation != current`,
    on a `SourceEvent("generation"|"run")` flushes its audio queue and resets
    DSP; if the tuned channel no longer fits the new run's span → state
    `unavailable` with reason, audio silent (no error spam).
  - `audio()` async iterator of `AudioBlock`s (bounded queue; oldest dropped and
    counted in `status()["dropped_blocks"]`).
  - `flat_tap()` → an async iterator of `(bytes s16 at demod rate, generation,
    sample_index)` for decoders (NFM only in S3; S4 wires it).
  - `rds()` → latest RDS parser result dict or None (WFM only).
  - `status()`: channel, demodulator, bandwidth, state
    (`idle|running|unavailable`), reason, squelch_open, rds summary,
    dropped_blocks, generation, audio_rate, cpu_load (EMA of process time /
    block time).
  - `squelch(threshold_db)`, `volume(v)`, `mute(bool)` affect the listening path only.

Tests (tests/source/test_dsp.py, test_virtual_receiver.py) with synthetic IQ
generated in-test (no committed IQ): NFM 1 kHz tone at 2.5 kHz deviation →
1 kHz audio (SNR > 30 dB) on both taps; NFM squelch closes on noise-only; AM
30 % modulated 1 kHz → 1 kHz; USB/LSB pitch tests above; CW keyed → 700 Hz
bursts follow keying; WFM 1 kHz mono + RDS (reuse tests/survey/test_rds.py's
synthesiser by importing its helper, or copy the minimal generator) → PI/PS
decoded through `VirtualReceiver.rds()` with pilot and without pilot; seek
(generation bump) → no AudioBlock of the old generation is ever yielded
after the flush and the first new block's sample_index matches the seek
target; out-of-span tune raises OutOfSpanError with a reason mentioning the
span; processing cost: 10 s of 2 MS/s NFM in < 2 s wall (record the number).

## 3. Service, API, audio transport (W3)

- `AppService`: `self.virtual_rx: VirtualReceiver | None` created when a replay
  starts (source = the survey's `ReplaySource`), stopped on replay stop. **The
  virtual receiver never calls the adapter.** Commands carry `source_id`; the
  review routes act on the replay source only and return 409 `"no replay
  running"` otherwise.
- Routes:
  - `GET /api/review` → `{replay: ReplaySource.status(), receiver: virtual_rx.status()|None,
    bookmarks: [...]}`.
  - `POST /api/review/tune {frequency_hz, demodulator, bandwidth_hz}` → receiver
    status; 409 with the OutOfSpanError reason when outside the span.
  - `POST /api/review/audio {squelch_db?, volume?, mute?}`.
  - `POST /api/review/seek {position_s}`; `POST /api/review/loop {loop: bool}`;
    play/pause stays on the existing `POST /api/survey/replay/control`.
  - Bookmarks: `POST /api/review/bookmarks {note}` → creates an Observation
    (`provenance="bookmark"`, session = current replay session, `sample_start=sample_end=`
    current run-local sample, `start_ts` = run start_wall + elapsed only when
    start_wall is known, else None) plus a `RecordingReference(path, name,
    run_index, sample_start)`; `GET /api/review/bookmarks?path=` lists them
    (joins recording_refs by path); `POST /api/review/bookmarks/{id}/seek` seeks
    the running replay to that ref (same recording only; 409 otherwise).
  - `WS /ws/audio`: after a client sends `{"type":"start"}` the server streams
    binary frames: 12-byte header `<IIi` (generation, sample_index_lo32, rate)
    followed by s16 mono PCM; on generation change it sends a text frame
    `{"type":"flush","generation":N}` first. One consumer per socket; the queue
    is the receiver's bounded queue (drops counted). Never streams live audio.
- `FakeSDRconnect`: add `set_count() -> int` (total set_property/apply_profile
  seen) so tests can assert zero writes.
- Tests (tests/api/test_review.py): start replay of a synthetic single-run SigMF
  written to tmp (helper exists in tests/api/test_recordings.py), tune/listen
  (read ≥3 audio frames from `/ws/audio`), seek, loop, bookmark create/list/seek,
  then assert `fake.set_count() == 0` — **the zero-live-writes gate**; 409 out of
  span; audio flush message after seek and no frame with the old generation
  afterwards; bookmark Observation persisted (GET /api/observations lists it).

## 4. UI (wave 2, after §3 lands) — `review-view.js`, `app.js`, new `audio-player.js`

- Review shows the primary spectrum slot while a replay is running (app.js:
  `PRIMARY_SPECTRUM_WORKSPACES` + review when `state.survey.replay`).
- Virtual receiver controls bounded to the span (frequency entry, mode
  NFM/AM/USB/LSB/CW/WFM, bandwidth, squelch, volume, mute); clicking the
  spectrum in Review tunes the *virtual* receiver, never the radio; the strip's
  source chip already says REPLAY.
- Transport: play/pause, seek slider over the timeline (run boundaries and gaps
  drawn; unknown gaps hatched), loop toggle, file-relative elapsed and, when
  known, original capture time; "behind" indicator.
- `audio-player.js`: AudioWorklet (fallback ScriptProcessor) fed from
  `/ws/audio`; explicit PLAY AUDIO button (autoplay policy), bounded ring
  (≤ 1 s), flush on `{"type":"flush"}`, mute/volume; shows buffer level and
  drops. Never runs while no replay is active.
- Bookmarks list with ADD BOOKMARK (note) and jump; RDS readout (PI/PS/RT,
  block error rate as a measured % with its window) for WFM.

## Ownership (wave 1)

- W1 DSP (opus): `msdr/source/dsp.py`, `msdr/source/virtual_receiver.py`,
  `msdr/source/__init__.py` (exports only), `tests/source/test_dsp.py`,
  `tests/source/test_virtual_receiver.py`. Codes against `IQSource`/`SourceEvent`
  from §1 by name (W2 writes base.py; until it lands, use a local test double).
- W2 source/timeline (opus): `msdr/source/base.py`, `msdr/source/live.py`,
  `msdr/source/timeline.py`, `msdr/survey/replay.py`, `msdr/recorder/iq_stream.py`
  (IQFrame fields only), `tests/source/test_timeline.py`,
  `tests/source/test_replay_source.py`, `tests/survey/test_replay.py` (update).
- W3 service/API (sonnet): `msdr/api/app.py`, `msdr/api/service.py`,
  `msdr/api/schemas.py`, `msdr/radio/fake_server.py` (set_count only),
  `tests/api/test_review.py`. Waits for W1/W2 modules to exist before running.
- Coordinator: review DSP/timing diffs, integration, wave-2 UI brief, README,
  version, ledger.
