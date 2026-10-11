import { LitElement, html } from "../vendor/lit.js";
import { mhz, sampleRate, integrityBadge, callLineText, sessionFactText } from "../format.js";
import { decoderValidation, groupDecoders } from "../decoder-info.js";
import {
  listRecordings,
  getReview,
  reviewTune,
  reviewAudio,
  reviewSeek,
  reviewLoop,
  reviewBookmarkCreate,
  reviewBookmarkSeek,
  reviewDecodeStart,
  reviewDecodeStop,
  listDecodeEvents,
  getDecoders,
  listObservations,
  patchObservation,
  createReviewClip,
  listReviewClips,
} from "../api.js";
import { recordingHealth, formatUnknown } from "../recording-health.js";
import "./failure-note.js";
import "./audio-player.js";
import "./native-audio-dsp.js";
import "./scope-panel.js";
import "./wifi-sessions-panel.js";

function mmss(seconds) {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fileSize(bytes) {
  if (bytes == null) return "--";
  const gb = bytes / 1e9;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

/** "capture time unknown" unless the row actually carries a real start
 * instant (recording.json's start_wall, or -- when that's missing -- a
 * SigMF core:datetime; see msdr/api/service.py's _recording_row). Never
 * shows a directory mtime as if it were the capture start. Also used for
 * the transport's "original capture time of the current position" (S3),
 * given `capture_start + elapsed-within-run` instead of a row's own field. */
function captureStartText(createdEpochS, tz) {
  if (createdEpochS == null) return "capture time unknown";
  const d = new Date(createdEpochS * 1000);
  if (Number.isNaN(d.getTime())) return "capture time unknown";
  const name = tz?.name;
  if (name) {
    try {
      const fmt = new Intl.DateTimeFormat("en-CA", {
        timeZone: name,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
      const parts = fmt.formatToParts(d);
      const get = (t) => parts.find((p) => p.type === t)?.value ?? "";
      return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
    } catch {
      // unknown/invalid IANA name -- fall through to the browser-local one
    }
  }
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${mo}-${day} ${hh}:${mm}`;
}

// S3 virtual receiver: fixed per-mode step size for the ◂ ▸ frequency
// buttons and the bandwidth choices offered per demodulator -- both from
// S3-replay-contract.md §4, not from format.js's defaultStepHz/STEP_CYCLE_HZ
// (those serve the *live* radio's instrument panel and use different values,
// e.g. CW there steps by 1000 Hz; here CW is 100 Hz).
const MODES = ["NFM", "AM", "USB", "LSB", "CW", "WFM"];
const STEP_HZ_BY_MODE = { NFM: 12500, AM: 5000, USB: 1000, LSB: 1000, CW: 100, WFM: 100000 };
const BANDWIDTH_OPTIONS_BY_MODE = {
  NFM: [8000, 12500, 25000],
  AM: [5000, 10000],
  USB: [1800, 2700, 3600, 6000],
  LSB: [1800, 2700, 3600, 6000],
  CW: [250, 500, 1000],
  WFM: [200000],
};

// Decorative width (in the same "seconds" unit as run durations) given to a
// gap of unknown length on the timeline bar -- its real duration is, by
// definition, not known, so this is just enough to read as a visible break
// rather than a claim about how long the gap actually was.
const UNKNOWN_GAP_WEIGHT_S = 2;

/** REVIEW shell (S1 contract, section id "review"; virtual receiver/timeline/
 * bookmarks are S3): browses finished pre-roll recordings (GET
 * /api/recordings, same pattern as survey-view.js's REPLAY picker), drives
 * the existing replay transport for play/pause (survey-replay /
 * survey-replay-control{playing} / survey-replay-stop, handled by app.js's
 * _doSurveyReplay*), and -- once a replay is running -- polls GET /api/review
 * for the virtual receiver/timeline/bookmarks state and drives its own
 * review/* routes directly (seek, loop, tune, audio, bookmarks). The live
 * receiver owner/record chips stay in the strip above, unchanged -- this
 * view and the virtual receiver it drives never touch the live adapter. */
export class ReviewView extends LitElement {
  static properties = {
    replay: { type: Object }, // state.survey?.replay ?? null (app.js's own poll/ws state)
    recorderStatus: { type: Object },
    tz: { type: Object },
    failure: { type: Object },
    refreshAt: { type: Number }, // bump to reload the recordings list
    // S4 (§5): app.js's live-capped decodeEvents array, same shape as
    // decode-view.js's -- app.js doesn't pass this down yet, so it defaults
    // empty and MESSAGES self-fetches via GET /api/decoder/events instead
    // (see _pollReview/_decodeMessages).
    decodeEvents: { type: Array },
    _recordings: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _selectedPath: { state: true },
    _loop: { state: true },
    _review: { state: true }, // last GET /api/review poll: {replay, receiver, bookmarks, decode}
    _reviewError: { state: true },
    _freqDraft: { state: true }, // frequency text field while being edited
    _squelchDraft: { state: true },
    _bookmarkDraft: { state: true },
    // S4 DECODE section (review-view's own ReplayDecodeSession, distinct
    // from decode-deck's live decoder controls).
    _decoders: { state: true }, // GET /api/decoders registry, self-fetched (see connectedCallback)
    _decodeMode: { state: true }, // "single" | "band"
    _decodeSelected: { state: true }, // Set<decoder id>
    _decodeMaxChannels: { state: true },
    _decodeThresholdDb: { state: true },
    _decodeEventsFallback: { state: true }, // GET /api/decoder/events, when decodeEvents prop is empty
    // S5 (§3): NOTES -- the recording's own Observation (provenance
    // "recording", ref path == the running replay's path), self-fetched
    // (app.js doesn't pass observations down to this view) via listObservations.
    _recordingObs: { state: true },
    _recordingObsPath: { state: true }, // path _recordingObs was loaded for, so a recording switch reloads it
    _recordingObsError: { state: true },
    _notesDraft: { state: true }, // textarea draft, or null to fall back to _recordingObs.notes
    _notesBusy: { state: true },
    _noteCreateDraft: { state: true }, // "create note" input, shown when no recording observation exists yet
    // S5 (§3): CLIPS -- MARK IN/OUT capture the current replay position
    // (file-elapsed seconds, same axis as reviewSeek) plus, when known, the
    // real capture time at that instant.
    _clipMarkIn: { state: true }, // {positionS, captureText} | null
    _clipMarkOut: { state: true },
    _clipNoteDraft: { state: true },
    _clipBusy: { state: true },
    _clipError: { state: true },
    _clips: { state: true },
    _clipsPath: { state: true }, // path _clips was loaded for
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.replay = null;
    this.recorderStatus = null;
    this.tz = null;
    this.failure = null;
    this.refreshAt = null;
    this.decodeEvents = [];
    this._recordings = [];
    this._loading = false;
    this._error = null;
    this._selectedPath = null;
    this._loop = true;
    this._review = null;
    this._reviewError = null;
    this._freqDraft = null;
    this._squelchDraft = null;
    this._bookmarkDraft = "";
    this._lastRefreshAt = null;
    this._pollTimer = null;
    this._visibilityHandler = () => this._syncPolling();
    this._decoders = [];
    this._decodeMode = "single";
    this._decodeSelected = new Set();
    this._decodeMaxChannels = 8;
    this._decodeThresholdDb = 10;
    this._decodeEventsFallback = [];
    this._recordingObs = null;
    this._recordingObsPath = null;
    this._recordingObsError = null;
    this._notesDraft = null;
    this._notesBusy = false;
    this._noteCreateDraft = "";
    this._clipMarkIn = null;
    this._clipMarkOut = null;
    this._clipNoteDraft = "";
    this._clipBusy = false;
    this._clipError = null;
    this._clips = [];
    this._clipsPath = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._lastRefreshAt = this.refreshAt;
    this._load();
    this._loadDecoders();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    this._syncPolling();
  }

  /** Self-fetched (app.js doesn't pass this view a decoder registry today) --
   * same GET /api/decoders decode-deck.js uses, just for the DECODE
   * section's compatibility filter. A failure here just leaves the
   * checkbox list empty; it isn't worth a failure-note over. */
  async _loadDecoders() {
    try {
      this._decoders = await getDecoders();
    } catch {
      /* leave _decoders empty */
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    document.removeEventListener("visibilitychange", this._visibilityHandler);
    this._stopPolling();
  }

  /** NOTES (§3): the recording's own Observation -- provenance "recording"
   * whose refs[].path matches the running replay's file. Self-fetched (no
   * per-frequency/per-path GET exists) from the same 50 most recent
   * observations signal-panel.js's own lists pull from. */
  async _loadRecordingObs(path) {
    this._recordingObsPath = path;
    this._recordingObsError = null;
    this._notesDraft = null;
    try {
      const observations = await listObservations({ limit: 50 });
      // provenance can carry an appended failure reason (e.g. "recording;
      // radio stopped") -- match the prefix, not equality.
      this._recordingObs =
        (observations ?? []).find(
          (o) => (o.provenance ?? "").startsWith("recording") && (o.refs ?? []).some((r) => r.path === path)
        ) ?? null;
    } catch (err) {
      this._recordingObsError = err.message;
      this._recordingObs = null;
    }
  }

  async _loadClips(path) {
    this._clipsPath = path;
    try {
      this._clips = await listReviewClips(path);
      this._clipError = null;
    } catch (err) {
      this._clipError = err.message;
    }
  }

  updated(changed) {
    if (changed.has("refreshAt") && this.refreshAt !== this._lastRefreshAt) {
      this._lastRefreshAt = this.refreshAt;
      this._load();
    }
    // A file currently loaded for replay is always selectable, even before
    // (or after) it shows up in the recordings list -- e.g. right after
    // recordings-view's ▶ REVIEW switched here.
    if (this.replay?.path && this._selectedPath == null) this._selectedPath = this.replay.path;
    if (changed.has("replay")) this._syncPolling();
    // S5 (§3): NOTES/CLIPS both need the actually-running replay's own path
    // (not just _selectedPath, which can point at a recording nothing is
    // playing) -- reload whenever that path changes, and drop stale state
    // once the replay stops or moves to a different file.
    const activePath = this._activeReplayPath();
    if (activePath) {
      if (activePath !== this._recordingObsPath) this._loadRecordingObs(activePath);
      if (activePath !== this._clipsPath) this._loadClips(activePath);
    } else {
      if (this._recordingObsPath != null) {
        this._recordingObsPath = null;
        this._recordingObs = null;
        this._notesDraft = null;
      }
      if (this._clipsPath != null) {
        this._clipsPath = null;
        this._clips = [];
      }
    }
  }

  _activeReplayPath() {
    const path = this.replay?.path;
    if (!path || this._review?.replay?.path !== path) return null;
    return path;
  }

  /** Real capture (wall-clock) time at `rr`'s *current* position, or null
   * when the file doesn't carry a capture_start -- used by the transport's
   * own readout and by MARK IN/OUT (§3), which capture this alongside the
   * file-relative position at the moment of the click. */
  _currentCaptureText(rr) {
    if (!(rr?.capture_start != null && rr?.runs?.[rr.run_index])) return null;
    return captureStartText(
      rr.capture_start + (rr.sample_index ?? 0) / (rr.runs[rr.run_index].sample_rate_hz || 1),
      this.tz
    );
  }

  // -- GET /api/review polling: every 500ms while mounted, visible, and a
  // replay is actually running (this.replay, driven by app.js's own
  // state/ws) -- stopped the moment any of those stop being true, so a
  // backgrounded tab or a workspace switch away from REVIEW (which unmounts
  // this element -- see app.js's WORKSPACE_SECTIONS) never keeps polling. --
  _syncPolling() {
    const shouldPoll = !!this.replay && !document.hidden && this.isConnected;
    if (shouldPoll && !this._pollTimer) {
      this._pollReview();
      this._pollTimer = setInterval(() => this._pollReview(), 500);
    } else if (!shouldPoll && this._pollTimer) {
      this._stopPolling();
    }
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  async _pollReview() {
    try {
      this._review = await getReview();
      this._reviewError = null;
      // Mirror the server's own DECODE session (a page reload, or the
      // section opening after another tab started it, must still show what's
      // actually running) rather than trusting only this component's clicks.
      const decode = this._review?.decode;
      if (decode?.active) {
        this._decodeMode = decode.mode ?? this._decodeMode;
        this._decodeSelected = new Set((decode.decoders ?? []).map((d) => d.id));
      }
      if (!this._review?.replay) this._syncPolling(); // server says it stopped
    } catch (err) {
      this._reviewError = err.message;
    }
    // MESSAGES (§5): app.js doesn't pass decodeEvents down yet -- self-fetch
    // on the same tick so the section still fills in on its own.
    if (!this.decodeEvents?.length) {
      try {
        this._decodeEventsFallback = await listDecodeEvents({ limit: 200 });
      } catch {
        /* keep the last-known fallback rather than blanking MESSAGES on a blip */
      }
    }
  }

  /** Runs a review/* POST, surfaces any error as text (never silent), and
   * re-polls immediately afterward so the UI reflects the server's actual
   * new state rather than assuming the POST's own response shape. */
  async _reviewAction(fn) {
    try {
      await fn();
      this._reviewError = null;
    } catch (err) {
      this._reviewError = err.message;
    }
    await this._pollReview();
  }

  async _load() {
    this._loading = true;
    try {
      this._recordings = await listRecordings();
      this._error = null;
      if (this._selectedPath == null && this._recordings.length) {
        this._selectedPath = this._recordings[0].path;
      }
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
    }
  }

  _select(path) {
    this._selectedPath = path;
  }

  _selectedRecording() {
    const fromList = this._recordings.find((r) => r.path === this._selectedPath) ?? null;
    if (fromList) return fromList;
    // A replay started elsewhere (▶ REVIEW, the strip's source chip, the API)
    // or a recordings list that is unavailable (recordings root unreadable ->
    // 503) must not hide the receiver/timeline/bookmarks: the running
    // replay's own status is enough to describe the file being reviewed.
    const rp = this._review?.replay ?? this.replay;
    if (rp && (!this._selectedPath || this._selectedPath === rp.path)) {
      return {
        path: rp.path,
        name: rp.name,
        center_hz: rp.center_hz,
        sample_rate: rp.sample_rate_hz,
        duration_s: rp.duration_s,
        size_bytes: null,
        gaps: null,
        capture_start: rp.capture_start ?? null,
        fromReplay: true,
      };
    }
    return null;
  }

  _startReplay(path) {
    if (!path) return;
    this.dispatchEvent(new CustomEvent("survey-replay", { detail: { path, loop: this._loop } }));
  }

  _replayControl(body) {
    this.dispatchEvent(new CustomEvent("survey-replay-control", { detail: body }));
  }

  _stopReplay() {
    this.dispatchEvent(new CustomEvent("survey-replay-stop"));
  }

  _row(r) {
    const selected = r.path === this._selectedPath;
    const playing = this.replay?.path === r.path;
    const health = recordingHealth(r);
    if (health.damaged) {
      // Damaged recording (backend-flagged, or missing a field replay
      // needs): only the name is trustworthy -- never show a 0:00/zeroed
      // duration or capture time as if it were real data.
      return html`
        <tr class=${selected ? "selected" : ""} style="cursor:pointer" @click=${() => this._select(r.path)}>
          <td class="mono">${playing ? html`<span title="currently loaded for replay">▶ </span>` : ""}${r.name}</td>
          <td class="mono" colspan="4"><span class="dim">damaged: ${health.reason}</span></td>
        </tr>
      `;
    }
    return html`
      <tr class=${selected ? "selected" : ""} style="cursor:pointer" @click=${() => this._select(r.path)}>
        <td class="mono">${playing ? html`<span title="currently loaded for replay">▶ </span>` : ""}${r.name}</td>
        <td class="mono">${r.center_hz != null ? mhz(r.center_hz) : "--"}</td>
        <td class="mono">${r.sample_rate != null ? sampleRate(r.sample_rate) : "--"}</td>
        <td class="mono">${formatUnknown(r.duration_s, mmss)}</td>
        <td class="mono dim" title="File-relative playback position is not the same as this instant -- see the transport below">
          ${captureStartText(r.capture_start, this.tz)}
        </td>
      </tr>
    `;
  }

  /** Timeline segments (runs + gaps, S3-replay-contract.md §4): each run
   * proportional to its own duration (samples/rate); a known gap gets a thin
   * block proportional to gap_before_samples/rate (at the *following* run's
   * rate -- the gap sits on that run's clock); an unknown-length gap gets a
   * fixed decorative width and is hatched, never claiming a real duration. */
  _timelineSegments(replay) {
    const runs = replay?.runs ?? [];
    const segments = [];
    let elapsed = 0;
    runs.forEach((run, i) => {
      const rate = run.sample_rate_hz || 1;
      const durationS = (run.samples ?? 0) / rate;
      if (i > 0) {
        if (run.gap_unknown) {
          segments.push({ kind: "gap-unknown", weight: UNKNOWN_GAP_WEIGHT_S });
        } else if (run.gap_before_samples) {
          const gapS = run.gap_before_samples / rate;
          segments.push({ kind: "gap-known", weight: Math.max(gapS, 0.05), seconds: gapS });
        }
      }
      segments.push({ kind: "run", run, weight: Math.max(durationS, 0.05), startElapsedS: elapsed, durationS });
      elapsed += durationS;
    });
    return segments;
  }

  /** % offset of the current position marker along the same segment weights
   * _timelineSegments lays out -- uses replay.run_index/sample_index (an
   * exact run-local sample count) rather than inverting elapsed_s, since the
   * two axes (elapsed seconds vs. decorative timeline width) don't agree
   * once gap segments are in the mix. */
  _markerPercent(replay, segments) {
    const total = segments.reduce((s, seg) => s + seg.weight, 0) || 1;
    const runIndex = replay.run_index ?? 0;
    const run = replay.runs?.[runIndex];
    const rate = run?.sample_rate_hz || 1;
    const localS = (replay.sample_index ?? 0) / rate;
    let acc = 0;
    for (const seg of segments) {
      if (seg.kind === "run" && seg.run.index === runIndex) {
        acc += Math.min(localS, seg.durationS);
        break;
      }
      acc += seg.weight;
    }
    return Math.min(100, Math.max(0, (acc / total) * 100));
  }

  _seekInSegment(seg, ev) {
    if (seg.kind !== "run") return; // gaps aren't a valid seek target
    const rect = ev.currentTarget.getBoundingClientRect();
    const fraction = rect.width ? Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)) : 0;
    const positionS = seg.startElapsedS + fraction * seg.durationS;
    this._reviewAction(() => reviewSeek(positionS));
  }

  _renderTimeline(replay) {
    const segments = this._timelineSegments(replay);
    const markerPct = this._markerPercent(replay, segments);
    return html`
      <div class="review-timeline">
        ${segments.map(
          (seg) => html`
            <div
              class="timeline-seg ${seg.kind}"
              style="flex-grow:${seg.weight}"
              title=${seg.kind === "gap-unknown"
                ? "gap of unknown length"
                : seg.kind === "gap-known"
                  ? `gap of ${seg.seconds.toFixed(1)}s`
                  : `run ${seg.run.index}`}
              @click=${(ev) => this._seekInSegment(seg, ev)}
            ></div>
          `,
        )}
        <div class="timeline-marker" style="left:${markerPct}%"></div>
      </div>
    `;
  }

  /** Transport: play/pause still goes through the existing
   * survey-replay-control event (app.js's _doSurveyReplayControl -- POST
   * /api/survey/replay/control), unchanged from S1. Seek and loop now go
   * through S3's own POST /api/review/seek and /api/review/loop instead --
   * they need the richer runs-aware position this._review carries, which
   * survey/replay/control's status never had. */
  _renderTransport(recording) {
    const replay = this.replay;
    if (!replay || replay.path !== recording.path) {
      const health = recordingHealth(recording);
      return html`
        <div class="control-row" style="flex-wrap:wrap;gap:8px">
          <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            <input type="checkbox" .checked=${this._loop} @change=${(e) => (this._loop = e.target.checked)} />
            loop
          </label>
          <button
            class="run-start"
            ?disabled=${health.damaged}
            title=${health.damaged ? `cannot replay: ${health.reason}` : "Switch to REVIEW and start replaying this recording"}
            @click=${() => this._startReplay(recording.path)}
          >
            ▶ REVIEW THIS RECORDING
          </button>
          ${this.replay ? html`<span class="dim mono" style="font-size:0.8em">replacing current replay: ${this.replay.name ?? this.replay.path}</span>` : ""}
        </div>
      `;
    }
    // this._review.replay (polled) carries runs/capture_start/behind_s that
    // replay (the prop) doesn't -- fall back to the plain slider until the
    // first poll response lands.
    const rr = this._review?.replay?.path === recording.path ? this._review.replay : null;
    const duration = replay.duration_s ?? 0;
    const position = replay.position_s ?? 0;
    const behindS = rr?.behind_s ?? 0;
    const captureNow = this._currentCaptureText(rr);
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="control-row" style="flex-wrap:wrap;gap:8px">
          <button
            style="font-size:0.85em"
            title=${replay.playing ? "Pause" : "Play"}
            @click=${() => this._replayControl({ playing: !replay.playing })}
          >
            ${replay.playing ? "⏸" : "▶"}
          </button>
          <span
            class="dim mono"
            style="font-size:0.85em"
            title="Position within the file -- not the original reception time"
          >
            ${mmss(position)} / ${mmss(duration)}
          </span>
          ${captureNow ? html`<span class="dim mono" style="font-size:0.85em">capture time: ${captureNow}</span>` : ""}
          ${behindS > 0.5
            ? html`<span class="mono" style="font-size:0.85em;color:var(--danger)" title="the replay clock is running behind its source">behind ${behindS.toFixed(1)}s</span>`
            : ""}
          <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            <input
              type="checkbox"
              .checked=${!!replay.loop}
              @change=${(e) => this._reviewAction(() => reviewLoop(e.target.checked))}
            />
            loop
          </label>
          <button class="run-stop" style="font-size:0.85em" @click=${() => this._stopReplay()}>⏹ STOP REPLAY</button>
        </div>
        ${rr ? this._renderTimeline(rr) : ""}
        ${this._reviewError ? html`<p class="status-error mono" style="font-size:0.85em;margin:0">${this._reviewError}</p>` : ""}
      </div>
    `;
  }

  // -- Virtual receiver (S3) --

  _tuneNow(hz, demodulator, bandwidthHz) {
    this._reviewAction(() => reviewTune(hz, demodulator, bandwidthHz));
  }

  _onFreqKeydown(receiver, ev) {
    if (ev.key !== "Enter") return;
    const mhzValue = parseFloat(ev.target.value);
    if (!Number.isFinite(mhzValue)) return;
    this._freqDraft = null;
    this._tuneNow(Math.round(mhzValue * 1_000_000), receiver.demodulator, receiver.bandwidth_hz);
  }

  _stepFrequency(receiver, direction) {
    const stepHz = STEP_HZ_BY_MODE[receiver.demodulator] ?? 1000;
    const hz = Math.max(0, (receiver.frequency_hz ?? 0) + direction * stepHz);
    this._tuneNow(hz, receiver.demodulator, receiver.bandwidth_hz);
  }

  _changeMode(receiver, mode) {
    const bw = BANDWIDTH_OPTIONS_BY_MODE[mode]?.[0];
    this._tuneNow(receiver.frequency_hz, mode, bw);
  }

  _changeBandwidth(receiver, bwHz) {
    this._tuneNow(receiver.frequency_hz, receiver.demodulator, bwHz);
  }

  _applySquelch(off, dbValue) {
    this._squelchDraft = null;
    this._reviewAction(() => reviewAudio({ squelch_db: off ? null : dbValue }));
  }

  _renderRds(rds) {
    if (!rds) return "";
    const pct = rds.groups ? ((rds.block_error_rate ?? 0) * 100).toFixed(1) : null;
    return html`
      <div class="survey-status mono" style="margin-top:6px">
        <div>PI ${rds.pi ?? "--"} · PS ${rds.ps ?? "--"}</div>
        <div>RT ${rds.rt || "--"}</div>
        <div>
          ${pct != null
            ? html`block errors ${pct}% over ${rds.groups} groups`
            : "no RDS groups decoded yet"}
        </div>
      </div>
    `;
  }

  _renderVirtualReceiver(receiver) {
    if (!receiver) return "";
    const mode = receiver.demodulator;
    const bwOptions = BANDWIDTH_OPTIONS_BY_MODE[mode] ?? [];
    const freqValue = this._freqDraft ?? (receiver.frequency_hz != null ? (receiver.frequency_hz / 1_000_000).toFixed(6) : "");
    const squelchOff = receiver.squelch_db == null;
    return html`
      <div style="display:flex;flex-direction:column;gap:8px">
        <div class="survey-group-label">VIRTUAL RECEIVER</div>
        <div class="control-row" style="flex-wrap:wrap;gap:8px">
          <button @click=${() => this._stepFrequency(receiver, -1)} title=${`step -${STEP_HZ_BY_MODE[mode] ?? 1000} Hz`}>◂</button>
          <input
            class="mono"
            style="width:11em"
            .value=${freqValue}
            @input=${(e) => (this._freqDraft = e.target.value)}
            @keydown=${(e) => this._onFreqKeydown(receiver, e)}
            @blur=${() => (this._freqDraft = null)}
          />
          <span class="dim mono" style="font-size:0.8em">MHz (Enter to tune)</span>
          <button @click=${() => this._stepFrequency(receiver, 1)} title=${`step +${STEP_HZ_BY_MODE[mode] ?? 1000} Hz`}>▸</button>
          <select @change=${(e) => this._changeMode(receiver, e.target.value)}>
            ${MODES.map((m) => html`<option value=${m} ?selected=${m === mode}>${m}</option>`)}
          </select>
          <select @change=${(e) => this._changeBandwidth(receiver, Number(e.target.value))}>
            ${bwOptions.map(
              (bw) => html`<option value=${bw} ?selected=${bw === receiver.bandwidth_hz}>${bw >= 1000 ? `${bw / 1000}k` : bw}</option>`,
            )}
          </select>
        </div>
        <div class="control-row" style="flex-wrap:wrap;gap:8px">
          ${mode === "NFM"
            ? html`
                <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
                  squelch
                  <input
                    class="mono"
                    style="width:5em"
                    .value=${this._squelchDraft ?? (squelchOff ? "off" : String(receiver.squelch_db))}
                    @input=${(e) => (this._squelchDraft = e.target.value)}
                    @keydown=${(e) => {
                      if (e.key !== "Enter") return;
                      const raw = e.target.value.trim().toLowerCase();
                      if (raw === "off" || raw === "") this._applySquelch(true, null);
                      else {
                        const db = Number(raw);
                        if (Number.isFinite(db)) this._applySquelch(false, db);
                      }
                    }}
                  />
                  dB
                  ${receiver.squelch_open ? html`<span style="color:var(--accent)">open</span>` : ""}
                </label>
              `
            : ""}
          <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            volume
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              .value=${String(receiver.volume ?? 1)}
              @change=${(e) => this._reviewAction(() => reviewAudio({ volume: Number(e.target.value) }))}
            />
          </label>
          <button
            aria-pressed=${!!receiver.muted}
            @click=${() => this._reviewAction(() => reviewAudio({ mute: !receiver.muted }))}
          >
            ${receiver.muted ? "MUTED" : "MUTE"}
          </button>
        </div>
        <native-audio-dsp mode="review" .settings=${this._review?.native_audio} .demodulator=${mode}></native-audio-dsp>
        <div class="survey-status mono">
          state: ${receiver.state}${receiver.reason ? ` -- ${receiver.reason}` : ""}
          ${receiver.dropped_blocks ? ` · dropped ${receiver.dropped_blocks} blocks` : ""}
        </div>
        ${mode === "WFM" ? this._renderRds(receiver.rds) : ""}
        <audio-player
          .active=${receiver.state === "running"}
          .rate=${receiver.audio_rate ?? 48000}
          .volume=${receiver.muted ? 0 : (receiver.volume ?? 1)}
          .muted=${!!receiver.muted}
        ></audio-player>
      </div>
    `;
  }

  // -- DECODE (S4, §5): review-view's own ReplayDecodeSession, bound to the
  // running replay -- single-channel (decoders compatible with the virtual
  // receiver's current demodulator) or band (NFM-family, channelized across
  // the file's span), distinct from decode-deck's live-adapter controls. --

  /** Decoders selectable for the current mode: single needs a demodulator
   * match with the virtual receiver's current mode (no receiver yet ->
   * nothing is selectable); band only needs "NFM" in the spec, same rule
   * decode-deck's own band section uses. */
  _decodeCompatibleDecoders() {
    const decoders = this._decoders ?? [];
    if (this._decodeMode === "band") {
      return decoders.filter((d) => (d.demodulators ?? []).includes("NFM"));
    }
    const mode = this._review?.receiver?.demodulator;
    if (!mode) return [];
    return decoders.filter((d) => (d.demodulators ?? []).includes(mode) ||
      (d.demodulators ?? []).includes("IQ"));
  }

  _setDecodeMode(mode) {
    if (this._review?.decode?.active) return; // mode is fixed while a session is running
    this._decodeMode = mode;
    this._decodeSelected = new Set();
  }

  _toggleDecodeSelected(id) {
    const next = new Set(this._decodeSelected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._decodeSelected = next;
  }

  async _startReviewDecode() {
    if (!this._decodeSelected.size) return;
    const body = { decoders: Array.from(this._decodeSelected), mode: this._decodeMode };
    if (this._decodeMode === "band") {
      body.max_channels = this._decodeMaxChannels;
      body.threshold_db = this._decodeThresholdDb;
    }
    await this._reviewAction(() => reviewDecodeStart(body));
  }

  async _stopReviewDecode() {
    await this._reviewAction(() => reviewDecodeStop());
  }

  _renderDecodeSection() {
    const decode = this._review?.decode;
    const active = !!decode?.active;
    const compatible = this._decodeCompatibleDecoders();
    return html`
      <div style="display:flex;flex-direction:column;gap:8px">
        <div class="survey-group-label">DECODE</div>
        <div class="control-row" style="gap:12px">
          <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            <input
              type="radio"
              name="review-decode-mode"
              ?disabled=${active}
              .checked=${this._decodeMode === "single"}
              @change=${() => this._setDecodeMode("single")}
            />
            single
          </label>
          <label class="dim mono" style="display:flex;align-items:center;gap:4px;font-size:0.85em">
            <input
              type="radio"
              name="review-decode-mode"
              ?disabled=${active}
              .checked=${this._decodeMode === "band"}
              @change=${() => this._setDecodeMode("band")}
            />
            band
          </label>
        </div>
        <div class="list">
          ${groupDecoders(compatible).map(({ category, decoders }) => html`
            <section class="decoder-category" aria-label=${category}>
              <h3 class="decoder-category-title">${category}</h3>
              ${decoders.map((d) => {
                const validation = decoderValidation(d);
                return html`<label class="row" style="cursor:${active ? "default" : "pointer"}">
                  <input
                    type="checkbox"
                    ?disabled=${active}
                    .checked=${this._decodeSelected.has(d.id)}
                    @change=${() => this._toggleDecodeSelected(d.id)}
                  />
                  <span class="label">${d.label}</span>
                  <span class="dim" title=${validation.title}>${validation.text}</span>
                </label>`;
              })}
            </section>
          `)}
          ${!compatible.length
            ? html`<p class="empty">
                no decoders compatible with this ${this._decodeMode === "band" ? "band" : "receiver's mode"}
              </p>`
            : ""}
        </div>
        ${this._decodeMode === "band"
          ? html`
              <div class="control-row" style="gap:12px">
                <label class="dim mono" style="font-size:0.85em;display:flex;align-items:center;gap:4px"
                  >MAX CHANNELS
                  <input
                    type="number"
                    min="1"
                    style="width:60px"
                    ?disabled=${active}
                    .value=${String(this._decodeMaxChannels)}
                    @change=${(e) => (this._decodeMaxChannels = Number(e.target.value) || 8)}
                  />
                </label>
                <label class="dim mono" style="font-size:0.85em;display:flex;align-items:center;gap:4px"
                  >THRESHOLD dB
                  <input
                    type="number"
                    step="0.5"
                    style="width:60px"
                    ?disabled=${active}
                    .value=${String(this._decodeThresholdDb)}
                    @change=${(e) => (this._decodeThresholdDb = Number(e.target.value) || 10)}
                  />
                </label>
              </div>
            `
          : ""}
        <div class="control-row" style="gap:8px;flex-wrap:wrap">
          <button class="run-start" ?disabled=${active || !this._decodeSelected.size} @click=${() => this._startReviewDecode()}>
            ▶ START
          </button>
          <button class="run-stop" ?disabled=${!active} @click=${() => this._stopReviewDecode()}>■ STOP</button>
          ${decode?.dropped_frames ? html`<span class="dim mono" style="font-size:0.8em">dropped ${decode.dropped_frames}</span>` : ""}
          ${decode?.restarts ? html`<span class="dim mono" style="font-size:0.8em">restarts ${decode.restarts}</span>` : ""}
        </div>
        ${active
          ? html`
              <div class="list">
                ${(decode.decoders ?? []).map(
                  (d) => html`
                    <div class="row" style="cursor:default">
                      <span class="mono">${(d.id ?? "").toUpperCase()}</span>
                      <span class="dim mono" style="font-size:0.85em">${sessionFactText(d)}</span>
                    </div>
                  `
                )}
              </div>
            `
          : ""}
      </div>
    `;
  }

  // -- MESSAGES (S4, §5): decode events for the running replay (decoder
  // parsers tag them source_id "replay:<name>", regardless of whether they
  // came from this section's own DECODE or from a decode-deck session that
  // happens to still be bound to this replay). --

  get _decodeMessages() {
    const name = this._review?.replay?.name ?? this.replay?.name;
    if (!name) return [];
    const sourceId = `replay:${name}`;
    const events = this.decodeEvents?.length ? this.decodeEvents : this._decodeEventsFallback ?? [];
    return events.filter((e) => e.source_id === sourceId);
  }

  /** POST /api/review/seek to this event's original position: sample_index
   * is run-local (§1), so it's converted using the *current* run's own
   * sample_rate_hz/start_s (replay.runs[replay.run_index]) -- the same run
   * this event's decode session is bound to. */
  _jumpToEvent(ev) {
    if (ev.sample_index == null) return;
    const rr = this._review?.replay;
    const run = rr?.runs?.[rr.run_index];
    if (!run?.sample_rate_hz) return;
    const positionS = (run.start_s ?? 0) + ev.sample_index / run.sample_rate_hz;
    this._reviewAction(() => reviewSeek(positionS));
  }

  _renderMessages() {
    const events = this._decodeMessages;
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="survey-group-label">MESSAGES</div>
        ${events.length
          ? html`
              <div class="list">
                ${events.map((ev) => {
                  const badge = integrityBadge(ev.integrity, ev.integrity_note);
                  const text = ev.kind === "call" ? callLineText(ev) : ev.text ?? "";
                  return html`
                    <div class="row" style="cursor:default">
                      <span class="chip integrity-badge ${badge.cls}" title=${badge.title}>${badge.text}</span>
                      <span class="mono">${(ev.decoder ?? "").toUpperCase()}</span>
                      <span style="flex:1;min-width:0">${text}</span>
                      ${ev.sample_index != null
                        ? html`<button style="font-size:0.85em" @click=${() => this._jumpToEvent(ev)}>JUMP</button>`
                        : ""}
                    </div>
                  `;
                })}
              </div>
            `
          : html`<p class="empty">No decode messages for this replay yet</p>`}
      </div>
    `;
  }

  // -- NOTES (S5, §3) --

  /** SAVE on the recording's own observation notes. Always refetches rather
   * than trusting the PATCH response's exact shape (the response also
   * carries a link-suggestion `note` string that isn't relevant here). */
  async _saveNotes() {
    const obs = this._recordingObs;
    if (!obs) return;
    const text = this._notesDraft ?? obs.notes ?? "";
    this._notesBusy = true;
    this._recordingObsError = null;
    try {
      await patchObservation(obs.id, { notes: text });
      this._notesDraft = null;
      await this._loadRecordingObs(this._recordingObsPath);
    } catch (err) {
      this._recordingObsError = err.message;
    } finally {
      this._notesBusy = false;
    }
  }

  /** "create note" (no recording-provenance observation exists yet): seeks to
   * position 0 and makes a bookmark there via the existing bookmark route
   * (the note text becomes the bookmark ref's own note, shown in BOOKMARKS
   * too), then PATCHes the Observation's `notes` field with the same text so
   * this section has something real to edit going forward. */
  async _createRecordingNote(path) {
    const text = this._noteCreateDraft.trim();
    this._notesBusy = true;
    this._recordingObsError = null;
    try {
      await reviewSeek(0);
      let created = await reviewBookmarkCreate(text);
      if (text) created = (await patchObservation(created.id, { notes: text })) ?? created;
      this._recordingObs = created;
      this._recordingObsPath = path;
      this._noteCreateDraft = "";
      this._notesDraft = null;
      await this._pollReview();
    } catch (err) {
      this._recordingObsError = err.message;
    } finally {
      this._notesBusy = false;
    }
  }

  _renderNotes() {
    const obs = this._recordingObs;
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="survey-group-label">NOTES</div>
        ${this._recordingObsError
          ? html`<p class="status-error mono" style="font-size:0.85em;margin:0">${this._recordingObsError}</p>`
          : ""}
        ${obs
          ? html`
              <textarea
                rows="3"
                .value=${this._notesDraft ?? obs.notes ?? ""}
                @input=${(e) => (this._notesDraft = e.target.value)}
              ></textarea>
              <div class="control-row">
                <button ?disabled=${this._notesBusy} @click=${() => this._saveNotes()}>SAVE</button>
              </div>
            `
          : html`
              <p class="empty">No note yet for this recording</p>
              <div class="control-row" style="gap:6px">
                <input
                  class="mono"
                  style="flex:1;min-width:120px"
                  placeholder="note"
                  .value=${this._noteCreateDraft}
                  @input=${(e) => (this._noteCreateDraft = e.target.value)}
                />
                <button
                  ?disabled=${this._notesBusy}
                  @click=${() => this._createRecordingNote(this._recordingObsPath)}
                >
                  CREATE NOTE
                </button>
              </div>
            `}
      </div>
    `;
  }

  // -- CLIPS (S5, §3) --

  _markIn() {
    const rr = this._review?.replay;
    if (!rr) return;
    this._clipMarkIn = { positionS: rr.position_s ?? 0, captureText: this._currentCaptureText(rr) };
    this._clipError = null;
  }

  _markOut() {
    const rr = this._review?.replay;
    if (!rr) return;
    this._clipMarkOut = { positionS: rr.position_s ?? 0, captureText: this._currentCaptureText(rr) };
    this._clipError = null;
  }

  async _createClip(path) {
    const inMark = this._clipMarkIn;
    const outMark = this._clipMarkOut;
    if (!inMark || !outMark) return;
    const startS = Math.min(inMark.positionS, outMark.positionS);
    const endS = Math.max(inMark.positionS, outMark.positionS);
    this._clipBusy = true;
    this._clipError = null;
    try {
      await createReviewClip(startS, endS, this._clipNoteDraft);
      this._clipMarkIn = null;
      this._clipMarkOut = null;
      this._clipNoteDraft = "";
      await this._loadClips(path);
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "clip created" } }));
    } catch (err) {
      // 409 (crosses a run boundary / bad range) or 503 (unwritable clips
      // dir) -- fetchJson already turned the response's `detail` text into
      // err.message, shown verbatim rather than a generic "failed".
      this._clipError = err.message;
    } finally {
      this._clipBusy = false;
    }
  }

  _jumpToClip(clip) {
    if (clip.start_s == null) return;
    this._reviewAction(() => reviewSeek(clip.start_s));
  }

  _renderClips(recording) {
    const inMark = this._clipMarkIn;
    const outMark = this._clipMarkOut;
    const canCreate = !!inMark && !!outMark && Math.abs(outMark.positionS - inMark.positionS) > 0;
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="survey-group-label">CLIPS</div>
        <div class="control-row" style="flex-wrap:wrap;gap:8px">
          <button @click=${() => this._markIn()}>MARK IN</button>
          <span class="dim mono" style="font-size:0.85em"
            >${inMark ? `${mmss(inMark.positionS)}${inMark.captureText ? ` (${inMark.captureText})` : ""}` : "--"}</span
          >
          <button @click=${() => this._markOut()}>MARK OUT</button>
          <span class="dim mono" style="font-size:0.85em"
            >${outMark ? `${mmss(outMark.positionS)}${outMark.captureText ? ` (${outMark.captureText})` : ""}` : "--"}</span
          >
        </div>
        <div class="control-row" style="gap:6px">
          <input
            class="mono"
            style="flex:1;min-width:120px"
            placeholder="note (optional)"
            .value=${this._clipNoteDraft}
            @input=${(e) => (this._clipNoteDraft = e.target.value)}
          />
          <button ?disabled=${!canCreate || this._clipBusy} @click=${() => this._createClip(recording.path)}>
            CREATE CLIP
          </button>
        </div>
        ${this._clipError
          ? html`<p class="status-error mono" style="font-size:0.85em;margin:0">${this._clipError}</p>`
          : ""}
        ${this._clips.length
          ? html`
              <div class="list">
                ${this._clips.map(
                  (c) => html`
                    <div class="row" style="cursor:default">
                      <span class="mono">${mmss(c.start_s)}–${mmss(c.end_s)}</span>
                      <span style="flex:1;min-width:0">${c.note || ""}</span>
                      <span class="dim mono" style="font-size:0.75em">${c.path}</span>
                      <button style="font-size:0.85em" @click=${() => this._jumpToClip(c)}>JUMP</button>
                    </div>
                  `
                )}
              </div>
            `
          : html`<p class="empty">No clips yet</p>`}
      </div>
    `;
  }

  // -- Bookmarks (S3) --

  _renderBookmarks(bookmarks, currentPath) {
    const rows = bookmarks ?? [];
    return html`
      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="survey-group-label">BOOKMARKS</div>
        ${rows.length
          ? html`
              <div class="list">
                ${rows.map(
                  (b) => html`
                    <div class="row">
                      <span class="mono" style="min-width:9em">${b.start_ts != null ? captureStartText(b.start_ts, this.tz) : "time unknown"}</span>
                      <span class="mono dim">run ${b.run_index}</span>
                      <span style="flex:1">${b.note || ""}</span>
                      <button style="font-size:0.85em" @click=${() => this._reviewAction(() => reviewBookmarkSeek(b.id))}>JUMP</button>
                    </div>
                  `,
                )}
              </div>
            `
          : html`<p class="empty">No bookmarks yet</p>`}
        <div class="control-row" style="gap:6px">
          <input
            class="mono"
            style="flex:1;min-width:120px"
            placeholder="note (optional)"
            .value=${this._bookmarkDraft}
            @input=${(e) => (this._bookmarkDraft = e.target.value)}
          />
          <button
            @click=${() => {
              const note = this._bookmarkDraft;
              this._bookmarkDraft = "";
              this._reviewAction(() => reviewBookmarkCreate(note));
            }}
          >
            ADD BOOKMARK
          </button>
        </div>
      </div>
    `;
  }

  _renderDetail(recording) {
    if (!recording) return html`<p class="empty">Select a recording to review it</p>`;
    const health = recordingHealth(recording);
    const activeHere = this.replay?.path === recording.path && this._review?.replay?.path === recording.path;
    return html`
      <div style="display:flex;flex-direction:column;gap:10px">
        ${health.damaged
          ? html`<p class="status-error mono" style="font-size:0.85em;margin:0">damaged: ${health.reason}</p>`
          : ""}
        <div style="display:flex;flex-wrap:wrap;gap:16px">
          <div class="readout">
            <span class="label">Name</span>
            <span class="value mono">${recording.name}</span>
          </div>
          <div class="readout">
            <span class="label">Centre / rate</span>
            <span class="value mono"
              >${recording.center_hz != null ? mhz(recording.center_hz) : "--"} ·
              ${recording.sample_rate != null ? sampleRate(recording.sample_rate) : "--"}</span
            >
          </div>
          <div class="readout">
            <span class="label">Duration</span>
            <span class="value mono">${formatUnknown(recording.duration_s, mmss)}</span>
          </div>
          <div class="readout">
            <span class="label">Size</span>
            <span class="value mono">${fileSize(recording.size_bytes)}</span>
          </div>
          <div class="readout">
            <span class="label">Gaps</span>
            <span class="value mono">${formatUnknown(recording.gaps, (g) => String(g))}</span>
          </div>
          <div class="readout">
            <span class="label">Capture start</span>
            <span class="value mono">${captureStartText(recording.capture_start, this.tz)}</span>
          </div>
        </div>
        ${this._renderTransport(recording)}
        ${activeHere ? this._renderVirtualReceiver(this._review.receiver) : ""}
        <scope-panel mode="review" .active=${!!activeHere}></scope-panel>
        ${activeHere ? this._renderDecodeSection() : ""}
        ${activeHere ? this._renderMessages() : ""}
        ${activeHere ? this._renderBookmarks(this._review.bookmarks, recording.path) : ""}
        ${activeHere ? this._renderNotes() : ""}
        ${activeHere ? this._renderClips(recording) : ""}
      </div>
    `;
  }

  render() {
    const recording = this._selectedRecording();
    const recorderState = this.recorderStatus?.state;
    return html`
      <div style="display:flex;flex-direction:column;gap:10px">
        ${this.failure
          ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
          : ""}
        ${recorderState === "recording" || recorderState === "finalizing"
          ? html`<p class="dim" style="font-size:0.85em;margin:0">
              recorder is ${recorderState} -- this list refreshes once it's finalized
            </p>`
          : ""}
        ${this._error
          ? html`<failure-note
              .failure=${{ subject: "recordings", status: "error", detail: this._error }}
              @dismiss=${() => (this._error = null)}
            ></failure-note>`
          : ""}
        ${this._loading && !this._recordings.length
          ? html`<p class="empty">loading…</p>`
          : this._recordings.length
            ? html`
                <div class="lib-table-wrap">
                  <table class="lib-table mono review-table">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>MHz</th>
                        <th>Rate</th>
                        <th>Duration</th>
                        <th>Capture start</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${this._recordings.map((r) => this._row(r))}
                    </tbody>
                  </table>
                </div>
              `
            : html`<p class="empty">No recordings yet -- REC from the instrument panel to make one</p>`}
        ${this._renderDetail(recording)}
        <wifi-sessions-panel></wifi-sessions-panel>
      </div>
    `;
  }
}

customElements.define("review-view", ReviewView);
