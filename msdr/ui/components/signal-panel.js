import { LitElement, html } from "../vendor/lit.js";
import {
  mhz,
  bandCategoryLabel,
  bw,
  localTime,
  withLocalWindow,
  epochHM,
  callLineText,
  evidenceLabel,
  evidenceStateLabel,
  integrityBadge,
  supplementalHintsNotice,
} from "../format.js";
import * as api from "../api.js";
import { nearestSavedFrequency } from "../saved-frequency.js";
import "./failure-note.js";
import "./allocation-summary.js";

/** Same tolerance the backend uses for "a decode event is about this
 * frequency" (msdr/api/signal_detail.py's _DECODE_MESSAGE_TOLERANCE_HZ) --
 * a fresh "decoder_event" within this band of the open target refetches the
 * panel instead of waiting for the operator to close/reopen it. */
const LIVE_REFRESH_TOLERANCE_HZ = 6000;
const EVIDENCE_GROUPS = {
  dsp: { label: "DSP · measured signal", color: "#45d5e0" },
  ml: { label: "ML · experimental predictions", color: "#bb9aff" },
  decoder: { label: "Protocol decoders", color: "#64ddbb" },
  memory: { label: "Saved memory · operator record", color: "#92afd5" },
  context: { label: "Frequency context · not identification", color: "#efc16b" },
  other: { label: "Other evidence", color: "#aab6c4" },
};
const ANALYSIS_STATE_LABELS = {
  not_analyzed: "Not analyzed",
  waiting: "Waiting",
  current_result: "Current result",
  historical_result: "Historical result",
};

/** S1 docked inspector: at this width and up the panel is a docked right
 * column inside app.js's .app-body (no backdrop, content reflows beside it);
 * below it, the original overlay sheet. Must match the media query on
 * .signal-panel.docked's parent rules in styles.css. */
const DOCK_QUERY = "(min-width: 1100px)";

/** U3 "one signal detail panel": a docked right column at >=1100px (S1),
 * otherwise a right-side slide-over (bottom sheet under 900px, see
 * styles.css's .signal-panel) opened from every "select a signal"
 * surface across the app via app.js's openSignal()/the bubbling "signal-open"
 * event -- survey label chip/row, find row, memory rows (nearby/inbox/recent/
 * library), decode-view rows, scanner hits, catalog rows. Backed by GET
 * /api/signal-detail (msdr/api/signal_detail.py), which aggregates memory,
 * scanner find, nearest survey label and an ordered evidence list for one
 * `frequency_hz`/`signal_id`/`find_id`. Never assumes any of those exist --
 * a bare frequency with nothing known about it still renders (just a SAVE
 * action and no evidence).
 *
 * Actions are dispatched as plain events for app.js to carry out (same
 * pattern as every other panel): `tune-signal`/`tune-frequency` reuse app.js's
 * existing tuning methods, `survey-decode`/`decode-start` reuse its existing
 * decoder handlers verbatim (survey-view.js and instrument-panel already wire
 * those same event names). SAVE/UPDATE/IGNORE are simple enough (and vary by
 * which of memory/find/survey_label is present) that this component makes
 * those API calls itself, self-fetch-and-act style like finds-view.js/
 * catalog-view.js, reporting through the shared `action`/`api-error` events. */
/** One line answering "what is this?" from the grouped evidence: the best
 * identification in order decoder output → DSP measurement → saved memory,
 * with its source and state in words. Never invents a result. */
function signalVerdict(groups) {
  const real = (e) => e?.title && !/^No (DSP classification|classifier result)/.test(e.title);
  const decoded = (groups.decoder ?? []).find(real);
  if (decoded) {
    const verified = decoded.integrity === "passed" || !decoded.integrity && decoded.evidence_type === "validated_decode" && decoded.state === "validated";
    return { title: decoded.title, source: "decoder", at: decoded.at ?? null,
      state: verified ? "validated decode" : decoded.integrity === "failed" ? "failed integrity check" : "decoder output · unchecked" };
  }
  const dsp = (groups.dsp ?? []).find(real);
  if (dsp) {
    return { title: dsp.title, source: "dsp", at: dsp.at ?? null,
      state: dsp._analysisState === "historical_result" ? "earlier DSP estimate" : "DSP estimate" };
  }
  const memory = (groups.memory ?? []).find(real);
  if (memory) return { title: memory.title, source: "memory", at: memory.at ?? null, state: "saved by operator · not measured" };
  return { title: "No identification yet", source: null, at: null, state: null };
}

export class SignalPanel extends LitElement {
  static properties = {
    open: { type: Boolean },
    wide: { type: Boolean, reflect: true }, // full-width "current frequency" section under the receiver (app.js ⓘ); follows the VFO
    target: { type: Object }, // {frequency_hz, signal_id, find_id} | null
    savedSignals: { type: Array },
    favorites: { type: Array },
    tz: { type: Object }, // state.timezone -- see withLocalWindow/localTime
    surveyActive: { type: Boolean }, // gates DECODE's survey-pin vs single-channel start
    replayActive: { type: Boolean }, // source selected by the app; replay decode isn't supported here
    latestDecodeEvent: { type: Object }, // app.js's decodeEvents[0] -- live refresh trigger
    jurisdiction: { type: String }, // settings.allocation_jurisdiction -- refetch so allocation evidence isn't stale
    _detail: { state: true },
    _loading: { state: true },
    _error: { state: true },
    _notesDraft: { state: true },
    _busy: { state: true },
    _testBusy: { state: true },
    _testResult: { state: true },
    _testError: { state: true },
    _docked: { state: true },
    _tab: { state: true },
    _notesOpen: { state: true },
    // S2 observations section (docs/epics/listening-workbench/S2-observations-contract.md):
    // per-row UI kept as plain component state, not part of _detail, so
    // refetching detail (after a link/claim) doesn't collapse everything.
    _expandedObs: { state: true }, // Set<observation id>
    _identifyObsId: { state: true }, // observation id showing the IDENTIFY… form, or null
    _identifyDraft: { state: true }, // {subject, value, rationale}
    _obsBusyId: { state: true }, // observation id currently in flight, or null
    _linkNote: { state: true }, // {obsId, text} -- API's "link suggests, does not prove identity"
    // S5 (§3): per-observation NOTES editor draft, keyed by observation id --
    // plain state (not part of _detail) so a refetch after SAVE doesn't wipe
    // an in-progress edit on a *different* row.
    _obsNotesDraft: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.open = false;
    this.target = null;
    this.savedSignals = [];
    this.favorites = [];
    this.tz = null;
    this.surveyActive = false;
    this.replayActive = false;
    this.latestDecodeEvent = null;
    this.jurisdiction = "US";
    this._detail = null;
    this._loading = false;
    this._error = null;
    this._notesDraft = "";
    this._busy = false;
    this._testBusy = false;
    this._testResult = null;
    this._testError = null;
    this._expandedObs = new Set();
    this._identifyObsId = null;
    this._identifyDraft = { subject: "station", value: "", rationale: "" };
    this._obsBusyId = null;
    this._linkNote = null;
    this._obsNotesDraft = {};
    // matchMedia is absent in some test/headless environments -- treat as sheet.
    this._dockMql = typeof window !== "undefined" && window.matchMedia ? window.matchMedia(DOCK_QUERY) : null;
    this._docked = !!this._dockMql?.matches;
    this._tab = "evidence"; this._notesOpen = false;
    this._onDockChange = (ev) => (this._docked = ev.matches);
    // Element that had focus when the panel opened -- focus goes back there
    // on close (if it's still in the DOM) so keyboard users don't land on <body>.
    this._opener = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._dockMql?.addEventListener?.("change", this._onDockChange);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._dockMql?.removeEventListener?.("change", this._onDockChange);
  }

  willUpdate(changed) {
    // Captured before render so the opener is still document.activeElement.
    if (changed.has("open") && this.open && !changed.get("open")) {
      this._opener = document.activeElement;
    }
  }

  updated(changed) {
    if (changed.has("open") && changed.get("open") !== undefined) {
      if (this.open) {
        // preventScroll: a docked column must not scroll the strip out of view.
        this.querySelector(".signal-panel")?.focus({ preventScroll: true });
      } else {
        const opener = this._opener;
        this._opener = null;
        if (opener?.isConnected && typeof opener.focus === "function") opener.focus();
      }
    }
    if (changed.has("target")) {
      this._testResult = null;
      this._testError = null;
      if (this.target) {
        // Opening a different signal -- collapse/forget any observation row
        // state left over from the previous one (obs ids won't line up).
        this._expandedObs = new Set();
        this._identifyObsId = null;
        this._linkNote = null;
        this._obsNotesDraft = {};
        this._load();
      } else {
        this._detail = null;
        this._error = null;
      }
    }
    if (changed.has("latestDecodeEvent") && this.open && this._detail && this.latestDecodeEvent) {
      const freq = this.latestDecodeEvent.frequency_hz;
      if (freq != null && Math.abs(freq - this._detail.frequency_hz) <= LIVE_REFRESH_TOLERANCE_HZ) {
        this._load();
      }
    }
    // The server's allocation evidence (part of /api/signal-detail) is keyed
    // on settings.allocation_jurisdiction at request time -- a jurisdiction
    // switch while this panel is open must refetch, or it keeps showing the
    // previous jurisdiction's allocation until the target happens to change.
    if (changed.has("jurisdiction") && changed.get("jurisdiction") !== undefined && this.target) {
      this._load();
    }
  }

  async _load() {
    const t = this.target;
    if (!t) return;
    // Following the VFO issues overlapping fetches, and a live decode event
    // or jurisdiction switch refetches the *same* target; only the newest
    // request may land, so a sequence number (not just target identity)
    // decides whether a reply is stale.
    const seq = (this._loadSeq = (this._loadSeq ?? 0) + 1);
    const stale = () => this.target !== t || this._loadSeq !== seq;
    this._loading = true;
    this._error = null;
    try {
      const detail = await api.getSignalDetail({
        frequency_hz: t.frequency_hz,
        signal_id: t.signal_id,
        find_id: t.find_id,
      });
      if (stale()) return;
      this._detail = detail;
      this._notesDraft = detail.memory?.notes ?? "";
      // The Live inspection panel beside the wide section seeds its mode and
      // width from this suggestion (app.js).
      if (this.wide) this.dispatchEvent?.(new CustomEvent("signal-detail-loaded", { detail: {
        frequency_hz: detail.frequency_hz, demodulator: detail.demodulator ?? null, bandwidth_hz: detail.bandwidth_hz ?? null } }));
    } catch (err) {
      if (stale()) return;
      this._error = err.message;
      this._detail = null;
    } finally {
      if (!stale()) this._loading = false;
    }
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  // --- actions --------------------------------------------------------------

  _tune() {
    const d = this._detail;
    if (!d) return;
    if (this.replayActive) {
      this.dispatchEvent(new CustomEvent("replay-tune", { detail: { frequency_hz: d.frequency_hz } }));
      return;
    }
    if (d.memory) {
      this.dispatchEvent(new CustomEvent("tune-signal", { detail: { id: d.memory.id } }));
    } else {
      this.dispatchEvent(
        new CustomEvent("tune-frequency", {
          detail: { hz: d.frequency_hz, demodulator: d.demodulator, filter_bandwidth_hz: d.bandwidth_hz },
        })
      );
    }
  }

  /** Suggested decoders when available, else [] -- band-decode's family
   * fallback (server side) only applies through the survey-pin path. */
  _decodeDecoders() {
    return this._detail?.suggested_decoders ?? [];
  }

  /** Single-channel start has no server-side default to fall back on (unlike
   * survey's label-driven pin), so it needs an explicit decoder list. */
  _canDecode() {
    return !this.replayActive && (this.surveyActive || this._decodeDecoders().length > 0);
  }

  _decode() {
    const d = this._detail;
    if (!d) return;
    const decoders = this._decodeDecoders();
    if (this.surveyActive) {
      this.dispatchEvent(
        new CustomEvent("survey-decode", {
          detail: { frequency_hz: d.frequency_hz, decoders: decoders.length ? decoders : null },
        })
      );
    } else {
      // The backend decodes the VFO: say which frequency this panel is about,
      // so a memory/find selected while tuned elsewhere is tuned first.
      this.dispatchEvent(
        new CustomEvent("decode-start", { detail: { decoders, frequency_hz: d.frequency_hz } })
      );
    }
  }

  async _testSignal() {
    if (!this._detail || this._testBusy || !this.surveyActive || this.replayActive) return;
    const frequency = this._detail.frequency_hz;
    const target = this.target;
    const choices = this._signalTestChoices();
    this._testBusy = true;
    this._testResult = null;
    this._testError = null;
    try {
      const result = await api.surveyTestSignal(frequency, choices);
      if (this.target === target) this._testResult = result;
    } catch (err) {
      if (this.target === target) this._testError = err.message;
    } finally {
      this._testBusy = false;
    }
  }

  _signalTestChoices() {
    const suggested = this._detail.suggested_decoders ?? [];
    const candidates = (this._detail.survey_label?.decoder_candidates ?? []).map(c => c.id);
    return [...new Set([...suggested, ...candidates])].slice(0, 8);
  }

  async _optimizeDecode() {
    if (!this._detail || this._testBusy || !this.surveyActive || this.replayActive) return;
    const frequency = this._detail.frequency_hz;
    const target = this.target;
    this._testBusy = true;
    this._testResult = null;
    this._testError = null;
    try {
      const result = await api.surveyOptimizeDecode(frequency, this._signalTestChoices());
      if (this.target !== target) return;
      this._testResult = { ...result, mode: "optimize" };
      if (result.recommended_hz && result.recommended_decoder) {
        this.dispatchEvent(new CustomEvent("optimize-decode", {
          detail: { frequency_hz: result.recommended_hz, decoder: result.recommended_decoder },
        }));
      }
    } catch (err) {
      if (this.target === target) this._testError = err.message;
    } finally {
      this._testBusy = false;
    }
  }

  _renderTestResult() {
    const result = this._testResult;
    if (!result && !this._testError && !this._testBusy) return "";
    return html`<div class="signal-test-result" aria-live="polite">
      ${this._testBusy ? "Testing decoder sync on the current IQ span…" : ""}
      ${this._testError ? html`<span>Signal test failed: ${this._testError}</span>` : ""}
      ${result?.mode === "optimize" ? html`
        <strong>${result.outcome === "improved" ? "Better decoder sync found" :
          result.outcome === "span_changed" ? "Inconclusive: receiver span changed" :
          result.outcome === "no_iq" ? "No IQ received" :
          result.outcome === "unavailable" ? "No compatible decoder available" :
          "No repeatable improvement found"}</strong>
        ${result.recommended_hz ? html`<div>Tuning to ${(result.recommended_hz / 1e6).toFixed(6)} MHz and starting ${result.recommended_decoder.toUpperCase()}.</div>` :
          html`<div class="dim">The receiver and live decoder stay at their original settings.</div>`}
        <div class="dim">Tried offsets 0, −2, +2, −4, +4 kHz. A short burst alone does not change tuning.</div>
        ${result.trials?.map(t => html`<div class="mono" style="font-size:0.8em">
          ${t.offset_hz >= 0 ? "+" : ""}${(t.offset_hz / 1000).toFixed(0)} kHz ·
          ${t.results.filter(r => ["validated", "synchronized", "output"].includes(r.state))
            .map(r => `${r.id.toUpperCase()} ${r.state}`).join(", ") || (t.frames ? "no sync" : "no IQ")}
        </div>`)}
      ` : result ? html`
        <strong>${result.outcome === "matched" ? "Protocol evidence found" :
          result.outcome === "no_iq" ? "No IQ received" :
          result.outcome === "span_changed" ? "Inconclusive: receiver span changed" :
          result.outcome === "unavailable" ? "No compatible decoder available" :
          result.outcome === "error" ? "Decoders could not start" :
          "Inconclusive: no protocol sync during this short test"}</strong>
        <div class="dim">Tested at ${(result.frequency_hz / 1e6).toFixed(6)} MHz · ${result.frames} IQ frames · no receiver retune</div>
        ${result.results.map(r => html`<div>${r.label}: ${r.state === "validated" ? `${r.valid_frames} checked frame(s)` :
          r.state === "synchronized" ? "protocol sync" :
          r.state === "output" ? `${r.events} output event(s), unverified` :
          r.state === "error" ? `error: ${r.error ?? "decoder did not start"}` : "no sync observed"}</div>`)}
        ${result.unavailable?.length ? html`<div class="dim">Unavailable: ${result.unavailable.join(", ")}</div>` : ""}
      ` : ""}
    </div>`;
  }

  _openMessages() {
    const d = this._detail;
    if (!d) return;
    this.dispatchEvent(new CustomEvent("open-messages", { detail: { frequency_hz: d.frequency_hz } }));
  }

  _isFavorite(d) {
    return !!d?.memory && (this.favorites ?? []).some((s) => s.id === d.memory.id);
  }

  /** FAV: make sure the frequency is a saved signal (SAVE's own path), add it
   * to Favorites if it isn't there, then open it in the editor drawer --
   * notes-only UPDATE here isn't enough to name or retune a favorite. */
  async _favoriteAndEdit() {
    if (!this._detail?.memory) await this._saveOrUpdate();
    const memory = this._detail?.memory;
    if (!memory) return; // SAVE failed and already reported, or nothing to save
    if (!this._isFavorite(this._detail)) {
      this._busy = true;
      try {
        await api.addListItems("favorites", [memory.id]);
        this.dispatchEvent(new CustomEvent("favorites-changed"));
        this.dispatchEvent(new CustomEvent("action", { detail: { message: `favorited ${memory.name}` } }));
      } catch (err) {
        this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
        return;
      } finally {
        this._busy = false;
      }
    }
    this.dispatchEvent(new CustomEvent("select-signal", { detail: { kind: "signal", item: memory } }));
  }

  /** SAVE (no memory yet) or UPDATE (memory exists -- just its notes; full
   * edits stay the drawer's job). Priority when creating: a bound find (links
   * back via finds_promote so its status/signal_id stay consistent) -> an
   * active survey label (server-derived identity_summary, same as survey-view's
   * own SAVE) -> a bare frequency. */
  async _saveOrUpdate() {
    const d = this._detail;
    if (!d) return;
    const undoAt = Date.now();
    this._busy = true;
    try {
      if (d.memory) {
        await api.patchSignal(d.memory.id, { notes: this._notesDraft });
        this.dispatchEvent(new CustomEvent("action", { detail: { message: `updated ${d.memory.name}` } }));
      } else if (d.find) {
        const signal = await api.promoteFind(d.find.id, null);
        this.dispatchEvent(
          new CustomEvent("action", { detail: { message: `promoted ${signal.name ?? "find"} to memory` } })
        );
      } else if (d.survey_label) {
        const signal = await api.surveySave(d.frequency_hz, null);
        this.dispatchEvent(
          new CustomEvent("action", { detail: { message: `saved ${signal.name ?? "channel"} as memory` } })
        );
      } else {
        const mhzVal = (d.frequency_hz / 1_000_000).toFixed(6);
        await api.createSignal({
          name: `UNKNOWN ${mhzVal} MHz`,
          frequency_hz: d.frequency_hz,
          status: "unknown",
          demodulator: d.demodulator ?? undefined,
          filter_bandwidth_hz: d.bandwidth_hz ?? undefined,
        });
        this.dispatchEvent(new CustomEvent("action", { detail: { message: `saved UNKNOWN ${mhzVal} MHz` } }));
      }
      this.dispatchEvent(new CustomEvent("library-undoable", {
        detail: { at: undoAt }, bubbles: true, composed: true,
      }));
      this.dispatchEvent(new CustomEvent("refresh"));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._busy = false;
    }
  }

  async _ignoreFind() {
    const d = this._detail;
    if (!d?.find) return;
    this._busy = true;
    try {
      await api.patchFind(d.find.id, { status: "ignored" });
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "find ignored" } }));
      this.dispatchEvent(new CustomEvent("refresh"));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._busy = false;
    }
  }

  // --- observations (S2) ------------------------------------------------------

  _toggleObsExpanded(id) {
    const next = new Set(this._expandedObs);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._expandedObs = next;
  }

  async _linkObservation(obs) {
    const d = this._detail;
    if (!d?.memory) return;
    this._obsBusyId = obs.id;
    try {
      const resp = await api.patchObservation(obs.id, { signal_id: d.memory.id });
      this._linkNote = { obsId: obs.id, text: resp?.note ?? null };
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  async _unlinkObservation(obs) {
    this._obsBusyId = obs.id;
    try {
      const resp = await api.patchObservation(obs.id, { signal_id: null });
      this._linkNote = { obsId: obs.id, text: resp?.note ?? null };
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  _openIdentify(obs) {
    this._identifyObsId = obs.id;
    this._identifyDraft = { subject: "station", value: "", rationale: "" };
  }

  _cancelIdentify() {
    this._identifyObsId = null;
  }

  async _submitIdentify(obs) {
    const { subject, value, rationale } = this._identifyDraft;
    if (!subject || !value.trim() || !rationale.trim()) return;
    this._obsBusyId = obs.id;
    try {
      await api.addOperatorClaim(obs.id, { subject, value: value.trim(), rationale: rationale.trim() });
      this._identifyObsId = null;
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "claim added" } }));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  _setObsNotesDraft(id, text) {
    this._obsNotesDraft = { ...this._obsNotesDraft, [id]: text };
  }

  /** SAVE on an observation row's NOTES editor -- PATCH notes, then refetch
   * so the row reflects whatever the server actually stored (never trusted
   * as an optimistic update, same pattern as the link/unlink actions above). */
  async _saveObsNotes(obs) {
    const text = this._obsNotesDraft[obs.id] ?? obs.notes ?? "";
    this._obsBusyId = obs.id;
    try {
      await api.patchObservation(obs.id, { notes: text });
      this.dispatchEvent(new CustomEvent("action", { detail: { message: "notes saved" } }));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  /** PROMOTE TO MEMORY: POST /promote, then open the new memory via the
   * bubbling "signal-open" event -- app.js's existing window listener
   * (openSignal/_onSignalOpen) is generic over {frequency_hz, signal_id,
   * find_id}, so no new handling is needed there. */
  async _promoteObservation(obs) {
    this._obsBusyId = obs.id;
    try {
      const resp = await api.promoteObservation(obs.id, null);
      const signal = resp?.signal;
      this.dispatchEvent(
        new CustomEvent("action", {
          detail: { message: resp?.note ?? `promoted to ${signal?.name ?? "memory"}` },
        })
      );
      if (signal?.id) {
        this.dispatchEvent(
          new CustomEvent("signal-open", { bubbles: true, composed: true, detail: { signal_id: signal.id } })
        );
      }
      this.dispatchEvent(new CustomEvent("refresh"));
      await this._load();
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  /** EXPORT: GET the full JSON (claims incl. historical, refs, decode events,
   * session) and hand it to the browser as a download -- a blob link rather
   * than a plain <a href> so it works the same whether the route serves an
   * attachment or a bare JSON body. */
  async _exportObservation(obs) {
    this._obsBusyId = obs.id;
    try {
      const data = await api.exportObservation(obs.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `observation-${obs.id}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      this.dispatchEvent(new CustomEvent("api-error", { detail: { message: err.message } }));
    } finally {
      this._obsBusyId = null;
    }
  }

  // --- render -----------------------------------------------------------------

  _evidenceSource(e) {
    if (EVIDENCE_GROUPS[e.source_group]) return e.source_group;
    if (e.kind === "analyzer") return e.source === "analyzer" || !e.source && !e.title?.startsWith("ML ") ? "dsp" : "ml";
    return { decoded: "decoder", memory: "memory", bandplan: "context", scheduled: "context" }[e.kind] ?? "other";
  }

  _evidenceRow(e) {
    const source = this._evidenceSource(e);
    const decoded = e.kind === "decoded";
    const verified = decoded && (e.integrity === "passed" || !e.integrity && e.evidence_type === "validated_decode" && e.state === "validated");
    const label = e._label ?? (decoded ? (verified ? "✓ Validated decode" : e.integrity === "failed" ? "⚠ Failed integrity check" : "Decoder output · unchecked")
      : source === "dsp" ? "DSP analysis" : source === "ml" ? (e.producer_role === "detector" ? "ML detector" : "ML classifier")
      : evidenceLabel(e.kind));
    const detailText = e.kind === "scheduled" ? withLocalWindow(e.detail ?? "", this.tz) : e.detail;
    const support = Array.isArray(e.support) ? e.support : [];
    const alternatives = Array.isArray(e.alternatives) ? e.alternatives : [];
    const limitations = Array.isArray(e.limitations) ? e.limitations : e.limitations ? [e.limitations] : [];
    return html`
      <article class="evidence-row inspector-evidence" data-evidence-source=${source}
        data-analysis-state=${e._analysisState ?? ""} style=${`--evidence-source-color:${EVIDENCE_GROUPS[source].color}`}>
        <div class="inspector-source-label">${label}${e._analysisState ? ` · ${ANALYSIS_STATE_LABELS[e._analysisState]}` : ""}${e.at != null ? html` <span class="dim inspector-result-time">· ${localTime(e.at, this.tz)}</span>` : ""}</div>
        ${e._previous ? html`<div class="dim inspector-result-meta">Previous estimate · awaiting refresh</div>` : ""}
        <strong>${e.title ?? "Unclassified"}</strong>
        ${decoded && detailText ? html`<div class="mono inspector-output">${detailText}</div>` : ""}
        ${e.next_action ? html`<div class="dim" style="font-size:0.8em">Next: ${e.next_action}</div>` : ""}
        ${detailText || support.length || alternatives.length || limitations.length ? html`
          <details class="inspector-secondary"><summary>Evidence details</summary>
            ${!decoded && detailText ? html`<p>${detailText}</p>` : ""}
            ${support.length ? html`<p>Support: ${support.join(", ")}</p>` : ""}
            ${alternatives.length ? html`<p>Alternatives: ${alternatives.join(", ")}</p>` : ""}
            ${limitations.length ? html`<p>${limitations.join(" · ")}</p>` : ""}
            ${decoded ? html`<p>${verified ? "Integrity check passed." : "Output is not independently verified."}</p>` : ""}
          </details>` : ""}
      </article>`;
  }

  /** Evidence items per source group, plus the previous DSP classification
   * when it is not already the current item. */
  _evidenceGroupItems(d) {
    const groups = Object.fromEntries(Object.keys(EVIDENCE_GROUPS).map(key => [key, []]));
    // "allocation" evidence is fully represented by the header's
    // <allocation-summary>, and "bandplan" by the "Channel / application
    // hints" block below it -- neither is repeated in this generic list.
    for (const item of d.evidence ?? []) {
      if (item.kind === "allocation" || item.kind === "bandplan") continue;
      groups[this._evidenceSource(item)].push(item);
    }
    const survey = d.survey_label;
    const analysis = survey?.analysis_state ?? {};
    const dspState = ANALYSIS_STATE_LABELS[analysis.state] ? analysis.state
      : survey?.analyzed_at || survey?.last_classification ? "historical_result" : "not_analyzed";
    if (survey || groups.dsp.length) {
      const first = groups.dsp[0] ?? {kind: "analyzer", source_group: "dsp",
        title: "No DSP classification yet", at: survey?.analyzed_at};
      groups.dsp[0] = {...first, _analysisState: dspState,
        _previous: dspState === "waiting" && groups.dsp.length > 0};
    }
    const ml = d.ml_analysis_state;
    if (ml) {
      const state = ANALYSIS_STATE_LABELS[ml.state] ? ml.state : "not_analyzed";
      const result = ml.result;
      const index = groups.ml.findIndex(e => e.producer_role === "classifier" ||
        !e.producer_role && !e.title?.includes("proposal"));
      const original = index >= 0 ? groups.ml[index] : {};
      const item = {...original, kind: "analyzer", source_group: "ml", producer_role: "classifier",
        _analysisState: state,
        title: result ? `${result.classification ?? "UNKNOWN"} · ${result.status ?? "unvalidated"}`
          : state === "waiting" ? "No classifier result yet"
            : ml.enabled === false ? "Classifier off" : "No classifier result",
        at: result?.analyzed_at ?? original.at,
        detail: result ? `Raw score ${Number.isFinite(result.score) ? result.score.toFixed(3) : "—"} · uncalibrated, not a probability. ${result.reason ?? ""}`
          : ml.error || original.detail,
      };
      if (index >= 0) groups.ml[index] = item; else groups.ml.unshift(item);
    }
    // RF ML is dormant (0.70.0): a "Classifier off" card with no result is
    // noise, so the group is left out until something real arrives.
    if (ml && ml.enabled === false && !ml.result && groups.ml.every(e => e.producer_role === "classifier")) groups.ml = [];
    const previous = d.last_classification;
    const duplicate = previous && groups.dsp.some(e => e.title === previous.name);
    const hasHistory = previous && !duplicate;
    if (hasHistory && !groups.dsp.length) groups.dsp.push({kind: "analyzer", source_group: "dsp",
      title: previous.name, at: previous.at, _analysisState: "historical_result"});
    return { groups, previous, hasHistory };
  }

  _evidenceGroups(d) {
    const { groups, previous, hasHistory } = this._evidenceGroupItems(d);
    return Object.entries(groups).filter(([,items]) => items.length).map(([source, items]) => html`
      <section class="inspector-evidence-group" data-evidence-group=${source}
        style=${`--evidence-source-color:${EVIDENCE_GROUPS[source].color}`}>
        <h3>${EVIDENCE_GROUPS[source].label}</h3>
        ${items.map(item => this._evidenceRow(item))}
        ${source === "dsp" && hasHistory && items[0]?.title !== previous.name ? html`
          <details class="inspector-analysis-history inspector-secondary">
            <summary>Previous DSP classification · ${localTime(previous.at, this.tz)}</summary>
            ${this._evidenceRow({kind:"analyzer", source_group:"dsp", title:previous.name,
              at:previous.at, _analysisState:"historical_result"})}
          </details>` : ""}
      </section>`);
  }

  _messageRow(m) {
    const text = m.kind === "call" ? callLineText(m) : m.text ?? "";
    const badge = integrityBadge(m.integrity, m.integrity_note);
    const isReplay = typeof m.source_id === "string" && m.source_id.startsWith("replay:");
    return html`
      <div class="row" style="cursor:default">
        <span class="dim mono">${epochHM(m.ts)}</span>
        <span class="mono">${(m.decoder ?? "").toUpperCase()}</span>
        <span class="chip integrity-badge ${badge.cls}" title=${badge.title}>${badge.text}</span>
        ${isReplay ? html`<span class="chip tag-replay">replay</span>` : ""}
        <span style="flex:1;min-width:0">${text}</span>
      </div>
    `;
  }

  _claimRow(c) {
    const superseded = !!c.superseded_by;
    return html`
      <div class="row" style="cursor:default;flex-wrap:wrap;${superseded ? "opacity:0.5" : ""}">
        <span class="mono dim" style="font-size:0.8em">${c.subject}</span>
        <span style="flex:1;min-width:0">${c.value}</span>
        ${c.integrity && c.integrity !== "not_applicable"
          ? html`<span class="dim" style="font-size:0.8em">${c.integrity}</span>`
          : ""}
        ${superseded ? html`<span class="dim" style="font-size:0.8em">superseded</span>` : ""}
      </div>
    `;
  }

  _identifyForm(obs) {
    const draft = this._identifyDraft;
    const busy = this._obsBusyId === obs.id;
    return html`
      <div class="control-row" style="margin-top:6px;flex-wrap:wrap" @click=${(ev) => ev.stopPropagation()}>
        <select
          .value=${draft.subject}
          @change=${(e) => (this._identifyDraft = { ...draft, subject: e.target.value })}
        >
          <option value="station">station</option>
          <option value="protocol">protocol</option>
          <option value="service">service</option>
        </select>
        <input
          type="text"
          placeholder="value"
          required
          .value=${draft.value}
          @input=${(e) => (this._identifyDraft = { ...draft, value: e.target.value })}
        />
        <input
          type="text"
          placeholder="rationale"
          required
          .value=${draft.rationale}
          @input=${(e) => (this._identifyDraft = { ...draft, rationale: e.target.value })}
        />
        <button
          ?disabled=${busy || !draft.value.trim() || !draft.rationale.trim()}
          @click=${() => this._submitIdentify(obs)}
        >
          SUBMIT
        </button>
        <button ?disabled=${busy} @click=${() => this._cancelIdentify()}>CANCEL</button>
      </div>
    `;
  }

  _observationRow(obs, d) {
    const expanded = this._expandedObs.has(obs.id);
    const when = obs.start_ts != null ? localTime(obs.start_ts, this.tz) : "capture time unknown";
    const busy = this._obsBusyId === obs.id;
    const canLink = !!d.memory && obs.signal_id == null;
    const canUnlink = !!d.memory && obs.signal_id != null && obs.signal_id === d.memory.id;
    const showingIdentify = this._identifyObsId === obs.id;
    const claims = obs.claims ?? [];
    const note = this._linkNote?.obsId === obs.id ? this._linkNote.text : null;
    const missingRefs = (obs.refs ?? []).filter((r) => r.available === false);
    return html`
      <div class="evidence-row">
        <div
          class="row"
          style="cursor:pointer"
          tabindex="0"
          @click=${() => this._toggleObsExpanded(obs.id)}
          @keydown=${(ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              this._toggleObsExpanded(obs.id);
            }
          }}
        >
          <span class="mono dim" style="font-size:0.8em">${when}</span>
          <span class="dim" style="font-size:0.85em;flex:1;min-width:0">${obs.provenance || "--"}</span>
          <span class="chip">${evidenceStateLabel(obs.evidence_state)}</span>
          <span class="dim mono" style="font-size:0.8em">${claims.length} claims</span>
          ${obs.legacy ? html`<span class="chip">legacy summary</span>` : ""}
        </div>
        ${expanded
          ? html`
              <div class="list" style="max-height:none;margin-top:4px">
                ${claims.length
                  ? claims.map((c) => this._claimRow(c))
                  : html`<p class="empty">no claims yet</p>`}
              </div>
              ${missingRefs.map(
                (r) => html`<p class="status-error mono" style="font-size:0.8em;margin:2px 0 0">
                  recording missing: ${r.path}
                </p>`
              )}
              <div class="control-row" style="margin-top:4px">
                ${canLink
                  ? html`<button
                      ?disabled=${busy}
                      @click=${(ev) => {
                        ev.stopPropagation();
                        this._linkObservation(obs);
                      }}
                    >
                      LINK TO THIS MEMORY
                    </button>`
                  : ""}
                ${canUnlink
                  ? html`<button
                      ?disabled=${busy}
                      @click=${(ev) => {
                        ev.stopPropagation();
                        this._unlinkObservation(obs);
                      }}
                    >
                      UNLINK
                    </button>`
                  : ""}
                <button
                  ?disabled=${busy}
                  @click=${(ev) => {
                    ev.stopPropagation();
                    this._openIdentify(obs);
                  }}
                >
                  IDENTIFY…
                </button>
                <button
                  ?disabled=${busy}
                  title="Create a Signal (memory) from this observation"
                  @click=${(ev) => {
                    ev.stopPropagation();
                    this._promoteObservation(obs);
                  }}
                >
                  PROMOTE TO MEMORY
                </button>
                <button
                  ?disabled=${busy}
                  title="Download the full observation record as JSON"
                  @click=${(ev) => {
                    ev.stopPropagation();
                    this._exportObservation(obs);
                  }}
                >
                  EXPORT
                </button>
              </div>
              ${note ? html`<p class="dim" style="font-size:0.8em;margin:2px 0 0">${note}</p>` : ""}
              ${showingIdentify ? this._identifyForm(obs) : ""}
              <div style="margin-top:6px" @click=${(ev) => ev.stopPropagation()}>
                <span class="dim" style="font-size:0.75em">notes</span>
                <textarea
                  rows="2"
                  placeholder="notes"
                  .value=${this._obsNotesDraft[obs.id] ?? obs.notes ?? ""}
                  @input=${(e) => this._setObsNotesDraft(obs.id, e.target.value)}
                ></textarea>
                <div class="control-row" style="margin-top:2px">
                  <button ?disabled=${busy} @click=${() => this._saveObsNotes(obs)}>SAVE</button>
                </div>
              </div>
            `
          : ""}
      </div>
    `;
  }

  _observationsSection(d) {
    const observations = d.observations;
    return html`
      <section>
        <h2>Observations</h2>
        ${Array.isArray(observations) && observations.length
          ? html`<div style="display:flex;flex-direction:column;gap:6px">
              ${observations.map((o) => this._observationRow(o, d))}
            </div>`
          : html`<p class="empty">no observations yet</p>`}
      </section>
    `;
  }

  /** Quick actions shared by both layouts. Wide mode (docked beside the
   * receiver) drops TUNE: the frequency is already the receiver's. */
  _actions(d, { tune }) {
    return html`<div class="control-row signal-panel-quick-actions" aria-label="Signal actions">
      ${tune ? html`<button @click=${() => this._tune()}>TUNE</button>` : ""}
      <button ?disabled=${!this._canDecode()}
        title=${this.replayActive
          ? "Live decoding is unavailable while reviewing a recording"
          : this._decodeDecoders().length
            ? this._decodeDecoders().join(", ")
            : "no suggested decoders"}
        @click=${() => this._decode()}>DECODE</button>
      ${this.surveyActive && !this.replayActive ? html`<button
        ?disabled=${this._testBusy}
        title="Briefly test up to three compatible narrowband decoders on the current IQ span; no retuning or saved messages"
        @click=${() => this._testSignal()}>${this._testBusy ? "TESTING…" : "TEST SIGNAL"}</button>` : ""}
      ${this.surveyActive && !this.replayActive ? html`<button
        ?disabled=${this._testBusy}
        title="Try nearby frequency offsets with compatible narrowband decoders; retune and start the best decoder only if a unique sync advantage repeats"
        @click=${() => this._optimizeDecode()}>OPTIMIZE DECODE</button>` : ""}
      <button ?disabled=${this._busy} @click=${() => this._saveOrUpdate()}>${d.memory ? "UPDATE" : "SAVE"}</button>
      <button ?disabled=${this._busy} aria-pressed=${this._isFavorite(d) ? "true" : "false"}
        title=${this._isFavorite(d) ? "Edit this favorite" : "Add to Favorites and edit"}
        @click=${() => this._favoriteAndEdit()}>${this._isFavorite(d) ? "★ FAV" : "☆ FAV"}</button>
      ${d.find && d.find.status !== "ignored"
        ? html`<button ?disabled=${this._busy} @click=${() => this._ignoreFind()}>IGNORE</button>` : ""}
    </div>`;
  }

  _notes(d) {
    if (!d.memory) return html`<p class="dim" style="font-size:0.85em;margin:0">SAVE to add notes</p>`;
    return html`<textarea rows="3" .value=${this._notesDraft}
      @input=${(e) => (this._notesDraft = e.target.value)}></textarea>`;
  }

  /** Docked "Current frequency" inspector: answer first (verdict + actions),
   * then one of Evidence / History, then Notes. Frequency context (allocation,
   * hints, suggested tuning) is not repeated here -- the receiver strip above
   * already shows it. */
  _wideBody(d) {
    const { groups } = this._evidenceGroupItems(d);
    const verdict = signalVerdict(groups);
    const saved = nearestSavedFrequency(d.frequency_hz, this.savedSignals, this.favorites);
    const evidenceCount = Object.values(groups).reduce((n, items) => n + items.length, 0);
    const historyCount = (d.messages?.length ?? 0) + (d.observations?.length ?? 0);
    const tab = this._tab === "history" && historyCount ? "history" : "evidence";
    const tabButton = (id, label, count) => html`<button role="tab" aria-selected=${tab === id ? "true" : "false"}
      ?disabled=${id === "history" && !historyCount}
      @click=${() => { this._tab = id; }}>${label}${count ? html` <span class="dim">${count}</span>` : ""}</button>`;
    return html`
      <div class="signal-panel-header">
        <div class="signal-panel-identity">
          ${this._wideTitle(d)}
          <div class="inspector-verdict" aria-label="Identification">
            <strong>${verdict.title}</strong>
            ${verdict.state ? html`<span class="dim inspector-verdict-state">${verdict.state}${verdict.at != null ? ` · ${localTime(verdict.at, this.tz)}` : ""}</span>` : ""}
          </div>
        </div>
        <button title="Close current frequency details (Esc)" aria-label="Close current frequency details"
          @click=${() => this._close()}>&times;</button>
      </div>
      <div class="freq-details-columns">
      <div class="freq-details-col">
      ${this._actions(d, { tune: false })}
      ${this._renderTestResult()}
      ${saved && (saved.distanceHz > 1 || !d.memory) ? html`<div class="nearby-saved-pill" aria-label="Nearby saved channel"
        title="Frequency proximity to an operator-saved channel; not a signal identification">
        ${saved.distanceHz > 1 ? "Nearby saved" : "Saved channel"}${saved.favorite ? " ★" : ""} · ${saved.name} · ${mhz(saved.frequencyHz)}${saved.distanceHz > 1 ? ` · ${bw(saved.distanceHz)} away` : ""}
      </div>` : ""}
      </div>
      <div class="freq-details-col">
      <div class="inspector-tabs" role="tablist" aria-label="Inspector sections">
        ${tabButton("evidence", "Evidence", evidenceCount)}
        ${tabButton("history", "History", historyCount)}
      </div>
      ${tab === "evidence" ? html`<section class="inspector-primary inspector-tabpanel" role="tabpanel">
        ${evidenceCount ? this._evidenceGroups(d) : html`<p class="empty">No measured identification yet.</p>`}
        ${d.memory?.id ? html`<a class="inspector-bundle" href=${`/api/signals/${encodeURIComponent(d.memory.id)}/evidence-bundle`}
          download="msdr-evidence.zip">Download evidence bundle with available IQ clip</a>` : ""}
      </section>` : html`<section class="inspector-tabpanel" role="tabpanel">
        ${d.messages.length ? html`<details class="inspector-secondary" open><summary>Decoder history · ${d.messages.length} messages</summary>
          <div class="list" style="max-height:260px">${d.messages.map(m => this._messageRow(m))}</div>
          <button @click=${() => this._openMessages()}>Open messages</button>
        </details>` : ""}
        ${d.observations?.length ? html`<details class="inspector-secondary"><summary>Observation history · ${d.observations.length}</summary>
          ${this._observationsSection(d)}
        </details>` : ""}
      </section>`}
      </div>
      <section class="inspector-notes freq-details-col">
        ${d.memory && !this._notesDraft && !this._notesOpen
          ? html`<button class="inspector-add-note" @click=${() => { this._notesOpen = true; }}>+ Add note</button>`
          : html`<h2>Notes</h2>${this._notes(d)}`}
      </section>
      </div>
    `;
  }

  _body(d) {
    if (this.wide) return this._wideBody(d);
    const groups = this._evidenceGroups(d);
    // msdr/api/signal_detail.py's evidence[].kind "allocation": the regulatory
    // lookup result (docs/allocations.md "Lookup result"), one item at most.
    const allocationEvidence = d.evidence?.find(e => e.kind === "allocation");
    // "bandplan": channel/application hints (NOAA WX2, DME, ...) -- every
    // item, not just the first.
    const bandplanHints = d.evidence?.filter(e => e.kind === "bandplan") ?? [];
    // US-specific hints: under an ITU jurisdiction the lookup's
    // `supplemental.reason` replaces them rather than leaving a gap.
    const hintsNotice = supplementalHintsNotice(allocationEvidence?.detail);
    const saved = nearestSavedFrequency(d.frequency_hz, this.savedSignals, this.favorites);
    return html`
      <div class="signal-panel-header">
        <div class="signal-panel-identity">
          <div class="mono" style="font-size:1.3em">${mhz(d.frequency_hz)}</div>
          ${this._actions(d, { tune: true })}
          ${this._renderTestResult()}
          ${saved && (saved.distanceHz > 1 || !d.memory) ? html`<div class="nearby-saved-pill" aria-label="Nearby saved channel"
            title="Frequency proximity to an operator-saved channel; not a signal identification">
            ${saved.distanceHz > 1 ? "Nearby saved" : "Saved channel"}${saved.favorite ? " ★" : ""} · ${saved.name} · ${mhz(saved.frequencyHz)}${saved.distanceHz > 1 ? ` · ${bw(saved.distanceHz)} away` : ""}
          </div>` : ""}
        </div>
        <div class="signal-panel-context">
          <div class="inspector-header-allocation" aria-label="Signal band allocation">
            <allocation-summary .data=${allocationEvidence?.detail ?? null}></allocation-summary>
          </div>
          ${hintsNotice ? html`<div class="band-plan-hints" aria-label="Channel / application hints">
            <span class="frequency-context-label" title="Channel / application hints from the band plan">Band plan</span>
            <small class="dim">${hintsNotice}</small>
          </div>` : bandplanHints.length ? html`<div class="band-plan-hints" aria-label="Channel / application hints">
            <span class="frequency-context-label" title="Channel / application hints from the band plan">Band plan</span>
            ${bandplanHints.map(h => html`<small>${h.category ? `${bandCategoryLabel(h.category)} · ` : ""}${h.title}</small>`)}
          </div>` : ""}
          ${d.survey_label?.rds?.ps
            ? html`<div class="mono" style="font-size:0.85em">
                RDS ${d.survey_label.rds.ps}${d.survey_label.rds.pty_name ? ` · ${d.survey_label.rds.pty_name}` : ""}
              </div>`
            : ""}
          ${d.demodulator || d.bandwidth_hz ? html`<div class="suggested-tuning-pill mono" aria-label="Suggested tuning">
            Suggested tuning · ${d.demodulator ?? ""}${d.bandwidth_hz ? ` · ${bw(d.bandwidth_hz)}` : ""}
          </div>` : ""}
        </div>
        <button title="close" aria-label="close signal detail" @click=${() => this._close()}>&times;</button>
      </div>

      <section class="inspector-primary">
        <h2>Signal evidence</h2>
        ${d.memory?.id ? html`<a href=${`/api/signals/${encodeURIComponent(d.memory.id)}/evidence-bundle`}
          download="msdr-evidence.zip">Download evidence bundle with available IQ clip</a>` : ""}
        ${groups.length ? groups : html`<p class="empty">No measured identification yet.</p>`}
      </section>
      ${d.messages.length ? html`<details class="inspector-secondary"><summary>Decoder history · ${d.messages.length} messages</summary>
        <div class="list" style="max-height:220px">${d.messages.map(m => this._messageRow(m))}</div>
        <button @click=${() => this._openMessages()}>Open messages</button>
      </details>` : ""}
      ${d.observations?.length ? html`<details class="inspector-secondary"><summary>Observation history · ${d.observations.length}</summary>
        ${this._observationsSection(d)}
      </details>` : ""}

      <section>
        <h2>Notes</h2>
        ${this._notes(d)}
      </section>

    `;
  }

  _content() {
    return html`
      ${this._error
        ? html`<failure-note
            .failure=${{ subject: "signal detail", status: "error", detail: this._error }}
            @dismiss=${() => (this._error = null)}
          ></failure-note>`
        : ""}
      ${this._loading && !this._detail ? html`<p class="empty">loading…</p>` : ""}
      ${this._detail ? this._body(this._detail) : ""}
    `;
  }

  _onKeydown(ev) {
    if (ev.key !== "Escape") return;
    // Mark it handled: app.js's window Escape handler skips defaultPrevented
    // events, otherwise the same keypress would also close the next thing
    // in its chain (a selected row/editor) after this panel is gone.
    ev.preventDefault();
    ev.stopPropagation();
    this._close();
  }

  /** Wide mode's title row: what this section is and that it tracks the
   * receiver. The old content stays while a retune refetches (see _load),
   * so "updating…" is the only cue during the swap. */
  _wideTitle(d) {
    return html`
      <div class="freq-details-title">
        <span class="freq-details-name">Current frequency${d?.frequency_hz ? html` · <span class="mono freq-details-hz">${mhz(d.frequency_hz)}</span>` : ""}</span>
        <span class="freq-details-follow" title="Updates each time the receiver retunes">● following radio</span>
        ${this._loading ? html`<span class="dim freq-details-updating">updating…</span>` : ""}
      </div>`;
  }

  render() {
    if (!this.open) return html``;
    // Wide (the receiver strip's ⓘ): a full-width, non-modal panel directly
    // below the receiver (app.js). No backdrop; Esc still closes. Without a
    // target the receiver has no VFO (disconnected), which is said rather
    // than left as stale details.
    if (this.wide) {
      return html`
        <aside
          class="panel signal-panel wide"
          aria-label="current frequency details"
          tabindex="-1"
          @keydown=${(ev) => this._onKeydown(ev)}
        >${this.target ? this._content() : html`
          <div class="signal-panel-header">
            <div class="signal-panel-identity">
              ${this._wideTitle()}
              <p class="empty">Receiver disconnected — no current frequency. The details return when it reconnects.</p>
            </div>
            <button title="Close current frequency details (Esc)" aria-label="Close current frequency details"
              @click=${() => this._close()}>&times;</button>
          </div>`}</aside>
      `;
    }
    // Docked: a non-modal side column -- no backdrop, the rest of the app
    // stays usable beside it (S1 contract, "Docked inspector").
    if (this._docked) {
      return html`
        <aside
          class="panel signal-panel docked"
          aria-label="signal detail"
          tabindex="-1"
          @keydown=${(ev) => this._onKeydown(ev)}
        >${this._content()}</aside>
      `;
    }
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel signal-panel"
          role="dialog"
          aria-modal="true"
          aria-label="signal detail"
          tabindex="-1"
          @click=${(ev) => ev.stopPropagation()}
          @keydown=${(ev) => this._onKeydown(ev)}
        >${this._content()}</div>
      </div>
    `;
  }
}

customElements.define("signal-panel", SignalPanel);
