import { LitElement, html } from "../vendor/lit.js";
import { mhz, epochHM, callLineText, readinessLabels, integrityBadge, sessionFactText } from "../format.js";
import "./led-dot.js";
import "./failure-note.js";
import "./run-indicator.js";
import "./decoder-prepare.js";
import "./cw-transcript.js";
import * as api from "../api.js";
import { DECODER_STATUS_LEGEND, decoderCategory, decoderDescription, decoderStatusIcons, groupDecoders } from "../decoder-info.js";

const SELECTION_KEY = "msdr.decode.selection";
const BAND_SELECTION_KEY = "msdr.decode.bandSelection";
const BAND_OPTIONS_KEY = "msdr.decode.bandOptions";

/** localStorage may throw (private browsing, quota, disabled) -- these
 * always fail soft so a storage hiccup never breaks the modal. */
function loadIds(key) {
  try {
    const arr = JSON.parse(localStorage.getItem(key));
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveIds(key, set) {
  try {
    localStorage.setItem(key, JSON.stringify(Array.from(set)));
  } catch {
    /* ignore */
  }
}

function loadBandOptions() {
  try {
    const obj = JSON.parse(localStorage.getItem(BAND_OPTIONS_KEY));
    return obj && typeof obj === "object" ? obj : {};
  } catch {
    return {};
  }
}

function saveBandOptions(maxChannels, thresholdDb) {
  try {
    localStorage.setItem(BAND_OPTIONS_KEY, JSON.stringify({ max_channels: maxChannels, threshold_db: thresholdDb }));
  } catch {
    /* ignore */
  }
}

/** Compact decode deck, next to the scanner deck in the instrument panel's
 * frequency row (Decoders P1, see docs/superpowers/specs/2026-09-25-decoders-design.md).
 * `status` mirrors DecoderManager.status() (msdr/decoder/manager.py), pushed
 * live as state.decoder / the "decoder" ws message; `lastEvent` is the newest
 * entry of app.js's decodeEvents (a DecodeEvent dict), or null. `decoders` is
 * the GET /api/decoders registry, used to build the ⚙ modal's checkbox list.
 * `status` itself is null when the decoder package isn't available server
 * side (see AppService.state_payload) -- treated the same as "off". */
export class DecodeDeck extends LitElement {
  static properties = {
    frequencyHz: { type: Number },
    _restoreToken: { state: true },
    status: { type: Object },
    lastEvent: { type: Object },
    decodeEvents: { type: Array },
    decoders: { type: Array },
    receiverProps: { type: Object },
    connected: { type: Boolean },
    started: { type: Boolean },
    canControl: { type: Boolean },
    surveyActive: { type: Boolean }, // live survey running: ⚗ test / ◎ optimize need its IQ span
    _probeBusy: { state: true },
    _probeResult: { state: true }, // last ⚗/◎ result, shown as one line under the strip
    failure: { type: Object },
    _setupOpen: { state: true },
    _setupTab: { state: true }, // "single" | "band" tab of the ⚙ modal
    _decoderQuery: { state: true },
    _singleHint: { state: true },
    _selected: { state: true },
    _bandSelected: { state: true },
    _maxChannels: { state: true },
    _thresholdDb: { state: true },
    _dropWarn: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.status = null;
    this.lastEvent = null;
    this.decodeEvents = [];
    this.decoders = [];
    this.receiverProps = null;
    this.connected = false;
    this.started = true;
    this.canControl = false;
    this.surveyActive = false;
    this._probeBusy = false;
    this._probeResult = null;
    this.failure = null;
    this._setupOpen = false;
    this._decoderQuery = "";
    this._selected = new Set();
    this._bandSelected = new Set();
    this._maxChannels = 8;
    this._thresholdDb = 10;
    this._prevDropped = null;
    this._dropWarn = false;
    this._dropSession = null;
  }

  /** Tracks status.band.iq.dropped across updates so the deck can show a
   * dropped-frames warning once it *increases* (a nonzero-but-flat count from
   * before this session started shouldn't alarm). */
  updated(changed) {
    if (this._setupOpen) {
      const dialog = this.querySelector("dialog.decode-setup-layer");
      if (dialog && !dialog.open) dialog.showModal();
    }
    if (!changed.has("status")) return;
    // Favorite recall starts decoding on the server. Remember that session
    // for Stop -> Start instead of reviving an older manual selection.
    if (this.status?.active && this.status.since !== this._selectionSession) {
      this._selectionSession = this.status.since;
      const ids = (this.status.decoders ?? []).map(d => d.id);
      if (ids.length) {
        saveIds(SELECTION_KEY, ids);
        if (!this._setupOpen) this._selected = new Set(ids);
      }
    }
    const bandSession = this.status?.band?.active ? this.status.band.since : null;
    if (bandSession !== this._dropSession) {
      this._dropSession = bandSession;
      this._prevDropped = null;
      this._dropWarn = false;
    }
    const dropped = this.status?.band?.iq?.dropped;
    if (typeof dropped !== "number") return;
    if (this._prevDropped !== null && dropped > this._prevDropped) this._dropWarn = true;
    this._prevDropped = dropped;
  }

  /** app.js's Escape priority (querySelector("decode-deck"), same pattern as
   * scanner-deck's setupOpen/closeSetup). */
  get setupOpen() {
    return this._setupOpen;
  }

  closeSetup() {
    this._setupOpen = false;
  }

  get _ready() {
    return this.connected && (this.started ?? true) && this.canControl;
  }

  get _preparationNeeded() {
    if (!this._ready || this.status?.active || this.status?.band?.active) return false;
    const ids = loadIds(SELECTION_KEY);
    if (ids.length !== 1) return false;
    const spec = this.decoders.find((d) => d.id === ids[0] && d.available);
    const props = this.receiverProps;
    if (!spec || !props) return false;
    const hz = spec.id === "adsb" ? 1_090_000_000 : this.frequencyHz;
    const rate = Number(props.device_sample_rate);
    const center = Number(props.device_center_frequency);
    const bandwidth = spec.id === "dsc" && hz >= 30_000_000
      ? 12_500 : Number(spec.default_bandwidth_hz);
    if (![hz, rate, center, bandwidth].every(Number.isFinite) || rate <= 0) return false;
    if (Math.abs(Number(props.device_vfo_frequency) - hz) > 1 ||
        rate < bandwidth * 1.1 || Math.abs(center - hz) + bandwidth / 2 > rate / 2) return true;
    if (spec.demodulators?.includes("IQ")) return false;
    return !spec.demodulators?.includes(props.demodulator) ||
      Number(props.filter_bandwidth) < bandwidth ||
      !!props.squelch_enable || !!props.noise_reduction_enable;
  }

  _lampInfo(status) {
    const band = status?.band;
    if (band?.active) {
      const n = (band.channels ?? []).length;
      const hasErr = (band.channels ?? []).some((c) => c.state === "error");
      return { label: `BAND · ${n} ch`, color: hasErr ? "var(--danger)" : "var(--ok)", lit: true };
    }
    if (!status || !status.active) {
      return { label: "OFF", color: "var(--led-unlit)", lit: false };
    }
    if (status.audio?.silent) {
      return { label: "SILENT", color: "var(--accent)", lit: true, blink: true };
    }
    if ((status.decoders ?? []).some((d) => d.state === "error")) {
      return { label: "ERR", color: "var(--danger)", lit: true };
    }
    return { label: "DECODING", color: "var(--ok)", lit: true };
  }

  _revealPreparation() {
    const target = [...this.querySelectorAll("decoder-prepare")].find(el => el.decoder === [...this._selected][0]);
    const details = target?.closest("details");
    if (details) details.open = true;
    target?.scrollIntoView({block: "center"});
  }

  _matchesDecoder(decoder) {
    const words = this._decoderQuery.trim().toLowerCase().split(/\s+/);
    const text = `${decoder.id} ${decoder.label} ${decoderCategory(decoder)} ${decoderDescription(decoder)}`.toLowerCase();
    return words.every(word => text.includes(word));
  }

  _openSetup(tab) {
    this._decoderQuery = "";
    const status = this.status ?? {};
    // Open on the tab that's running, so its STOP is one click away.
    this._setupTab = tab ?? (status.band?.active && !status.active ? "band" : "single");
    // Seed the checkbox list from whatever's currently running, else the
    // operator's last remembered choice (localStorage), else empty.
    this._selected = new Set(status.active ? (status.decoders ?? []).map((d) => d.id) : loadIds(SELECTION_KEY));
    const band = status.band ?? {};
    this._bandSelected = new Set(band.active ? band.decoders ?? [] : loadIds(BAND_SELECTION_KEY));
    if (band.active) {
      this._maxChannels = band.max_channels ?? 8;
      this._thresholdDb = band.threshold_db ?? 10;
    } else {
      const savedOpts = loadBandOptions();
      this._maxChannels = savedOpts.max_channels ?? 8;
      this._thresholdDb = savedOpts.threshold_db ?? 10;
    }
    this._setupOpen = true;
  }

  async _openPrepareForFailure() {
    const message = String(this.failure?.detail ?? "");
    const decoder = this.decoders.find(d => message.startsWith(`${d.label}:`));
    this._openSetup("single");
    if (decoder) {
      this._selected = new Set([decoder.id]);
      saveIds(SELECTION_KEY, this._selected);
    }
    await this.updateComplete;
    this._revealPreparation();
  }

  async _openPrepare() {
    this._openSetup("single");
    await this.updateComplete;
    if (this._selected.size === 1) {
      this._revealPreparation();
    }
  }

  _toggle(id) {
    const next = new Set(this._selected);
    if (next.has(id)) next.delete(id);
    else {
      const direct = key => this.decoders.find(d => d.id === key)?.demodulators?.includes("IQ");
      const removed = [...next].filter(key => !!direct(key) !== !!direct(id));
      removed.forEach(key => next.delete(key));
      next.add(id);
      this._singleHint = removed.length
        ? `Deselected ${removed.map(key => this.decoders.find(d => d.id === key)?.label ?? key).join(", ")}: these decoders need a separate session.`
        : "";
    }
    this._selected = next;
    saveIds(SELECTION_KEY, next);
  }

  async _selectSingle(d) {
    this._selected = new Set([d.id]);
    saveIds(SELECTION_KEY, this._selected);
    this._singleHint = `${d.label} selected for single-channel decoding.` +
      (d.id === "adsb" ? " Tune to 1090 MHz with at least 2.4 MS/s, then press START." : " Press START.");
    this._setupTab = "single";
    await this.updateComplete;
    const input = this.querySelector(`#decode-channel-list input[data-decoder-id="${d.id}"]`);
    input?.scrollIntoView({ block: "center" });
    input?.focus({ preventScroll: true });
  }

  _mixedSelection(ids) {
    const specs = ids.map(id => this.decoders.find(d => d.id === id)).filter(Boolean);
    const raw = specs.filter(d => d.demodulators?.includes("IQ"));
    return raw.length > 0 && raw.length < specs.length;
  }

  _start() {
    if (!this._selected.size) return;
    if (this._mixedSelection([...this._selected])) {
      this._singleHint = "Incompatible saved selection. Select one decoder to start; AIS and ADS-B need separate sessions.";
      return;
    }
    saveIds(SELECTION_KEY, this._selected);
    this.dispatchEvent(new CustomEvent("decode-start", { detail: { decoders: Array.from(this._selected) } }));
    this._setupOpen = false;
  }

  _stop() {
    this.dispatchEvent(new CustomEvent("decode-stop"));
  }

  _toggleBand(id) {
    const next = new Set(this._bandSelected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._bandSelected = next;
    saveIds(BAND_SELECTION_KEY, next);
  }

  _startBand() {
    if (!this._bandSelected.size) return;
    saveIds(BAND_SELECTION_KEY, this._bandSelected);
    saveBandOptions(this._maxChannels, this._thresholdDb);
    this.dispatchEvent(
      new CustomEvent("band-start", {
        detail: {
          decoders: Array.from(this._bandSelected),
          max_channels: this._maxChannels,
          threshold_db: this._thresholdDb,
        },
      })
    );
  }

  _stopBand() {
    this.dispatchEvent(new CustomEvent("band-stop"));
  }

  /** Deck-level START (Decoders P3): uses the remembered single-channel
   * selection directly from localStorage rather than the modal's `_selected`
   * state (which is only populated once the modal has been opened). Empty
   * selection means there's nothing to start -- open the ⚙ modal instead and
   * focus the checkbox list so the operator can pick one. */
  async _startTop() {
    const ids = loadIds(SELECTION_KEY);
    if (!ids.length || this._mixedSelection(ids)) {
      this._openSetup();
      if (ids.length) this._singleHint = "Incompatible saved selection. Select one decoder to start; AIS and ADS-B need separate sessions.";
      await this.updateComplete;
      this.querySelector("#decode-channel-list input[type=checkbox]")?.focus();
      return;
    }
    this.dispatchEvent(new CustomEvent("decode-start", { detail: { decoders: ids } }));
  }

  _stopTop() {
    this.dispatchEvent(new CustomEvent("decode-stop"));
  }

  /** Same idea as `_startTop`, for the band session's remembered decoder
   * selection and max_channels/threshold_db options. */
  async _startBandTop() {
    const ids = loadIds(BAND_SELECTION_KEY);
    if (!ids.length) {
      this._openSetup();
      await this.updateComplete;
      this.querySelector("#decode-band-list input[type=checkbox]")?.focus();
      return;
    }
    const opts = loadBandOptions();
    this.dispatchEvent(
      new CustomEvent("band-start", {
        detail: { decoders: ids, max_channels: opts.max_channels ?? 8, threshold_db: opts.threshold_db ?? 10 },
      })
    );
  }

  _stopBandTop() {
    this.dispatchEvent(new CustomEvent("band-stop"));
  }

  /** Inline reason shown on the deck's control row when START and STOP are
   * both disabled (inactive but not ready -- e.g. receiver stopped). Prefers
   * the manager's own warnings (status.warnings, see DecoderManager.status)
   * over a generic fallback. */
  _reasonText() {
    const warnings = this.status?.warnings ?? [];
    if (warnings.length) return warnings[0];
    if (!this.connected) return "not connected";
    if (!(this.started ?? true)) return "receiver stopped";
    if (!this.canControl) return "no control";
    return "";
  }

  _saveBandChannel(frequencyHz) {
    this.dispatchEvent(new CustomEvent("band-save", { detail: { frequency_hz: frequencyHz } }));
  }

  _installPresets() {
    this.dispatchEvent(new CustomEvent("install-presets"));
  }

  _openMessages() {
    this._setupOpen = false;
    this.dispatchEvent(new CustomEvent("open-decode-view"));
  }

  /** ⚗ / ◎ on the Decode line: the signal panel's TEST SIGNAL / OPTIMIZE
   * DECODE, applied to the tuned frequency. They probe the survey's IQ span,
   * so they're disabled (with the reason on hover) without a live survey. */
  _probeButton(mode, symbol, help) {
    const reason = !this.surveyActive ? "Start the survey first: this uses its IQ span."
      : !this.frequencyHz ? "No tuned frequency yet." : "";
    return html`<button class="decode-probe-button" aria-label=${help.split(":")[0]}
      title=${reason ? `${help} — ${reason}` : help}
      ?disabled=${!!reason || this._probeBusy} @click=${() => this._probe(mode)}>${symbol}</button>`;
  }

  async _probe(mode) {
    const hz = this.frequencyHz;
    if (!hz || this._probeBusy || !this.surveyActive) return;
    // The decoders checked in Decode setup; empty lets the backend pick its
    // band defaults (msdr/decoder/signal_test.py).
    const choices = [...loadIds(SELECTION_KEY)].slice(0, 8);
    this._probeBusy = true;
    this._probeResult = { mode, busy: true };
    try {
      const result = mode === "optimize"
        ? await api.surveyOptimizeDecode(hz, choices)
        : await api.surveyTestSignal(hz, choices);
      this._probeResult = { ...result, mode };
      if (mode === "optimize" && result.recommended_hz && result.recommended_decoder) {
        this.dispatchEvent(new CustomEvent("optimize-decode", {
          detail: { frequency_hz: result.recommended_hz, decoder: result.recommended_decoder },
        }));
      }
    } catch (err) {
      this._probeResult = { mode, error: err.message };
    } finally {
      this._probeBusy = false;
    }
  }

  _probeLine() {
    const r = this._probeResult;
    if (!r) return "";
    const label = r.mode === "optimize" ? "Optimize" : "Test";
    let text;
    if (r.busy) text = r.mode === "optimize" ? "trying nearby offsets…" : "testing decoders on the IQ span…";
    else if (r.error) text = `failed: ${r.error}`;
    else if (r.outcome === "no_iq") text = "no IQ received";
    else if (r.outcome === "span_changed") text = "inconclusive, receiver span changed";
    else if (r.outcome === "unavailable") text = "no compatible decoder available";
    else if (r.mode === "optimize") {
      text = r.recommended_hz
        ? `better sync at ${(r.recommended_hz / 1e6).toFixed(6)} MHz, starting ${r.recommended_decoder.toUpperCase()}`
        : "no repeatable improvement; settings unchanged";
    } else {
      const hits = (r.results ?? []).filter((x) => ["validated", "synchronized", "output"].includes(x.state));
      text = hits.length
        ? hits.map((x) => `${x.label} ${x.state === "validated" ? `✓ ${x.valid_frames}` : x.state === "synchronized" ? "sync" : "output"}`).join(" · ")
        : "no protocol sync during this short test";
    }
    // Same size as the facts line above; wraps inside the strip (the sidebar
    // strip is narrow) instead of pushing the × out of view.
    return html`<div class="scanner-deck-line decode-probe-line" aria-live="polite"
      style="flex:1 1 100%;min-width:0;align-items:flex-start;gap:6px">
      <span class="dim mono" style="font-size:0.85em;flex:1;min-width:0;white-space:normal;overflow-wrap:anywhere">${label}: ${text}</span>
      ${r.busy ? "" : html`<button class="decode-probe-dismiss" aria-label="Dismiss result" title="Dismiss"
        @click=${() => (this._probeResult = null)}>×</button>`}
    </div>`;
  }

  _lastEventLine() {
    const ev = this.lastEvent;
    if (!ev) {
      const status = this.status ?? {};
      if (status.active) {
        const ids = (status.decoders ?? []).map((d) => d.id.toUpperCase()).join(", ");
        const freq = status.frequency_hz ? mhz(status.frequency_hz) : "the current frequency";
        return html`<span class="dim mono">Listening on ${freq}${ids ? ` for ${ids}…` : "…"}</span>`;
      }
      return html`<span class="dim mono">Nothing decoded yet — START decodes the current frequency</span>`;
    }
    const text =
      ev.kind === "call"
        ? callLineText(ev)
        : ev.text || (Object.keys(ev.fields ?? {}).length ? JSON.stringify(ev.fields) : "");
    const badge = integrityBadge(ev.integrity, ev.integrity_note);
    const isReplay = typeof ev.source_id === "string" && ev.source_id.startsWith("replay:");
    const fullText = `${epochHM(ev.ts)} ${(ev.decoder ?? "").toUpperCase()} ${text}`;
    return html`<span class="dim mono decode-last-event-text" title=${fullText} aria-label=${fullText}
      >${fullText}</span
    >
      <span class="chip integrity-badge ${badge.cls}" title=${badge.title}>${badge.text}</span>
      ${isReplay ? html`<span class="chip tag-replay">replay</span>` : ""}`;
  }

  /** Per-decoder session facts (S4, §5): one line per currently running
   * decoder from the manager's own status.decoders (never the ⚙ modal's
   * installed/tested facts) -- "acquiring"/"N events (unverified)"/"N valid
   * frames"/"synchronized"/"quiet"/"unsupported input: <reason>"/"error",
   * never "available"/"ready". */
  _decoderFactsLines(decoders) {
    if (!decoders?.length) return "";
    return html`
      <div class="scanner-deck-line" style="flex-wrap:wrap;gap:8px">
        ${decoders.map(
          (d) => html`
            <span class="dim mono" style="font-size:0.85em"
              >${(d.id ?? "").toUpperCase()}: ${sessionFactText(d)}</span
            >
          `
        )}
      </div>
    `;
  }

  _setupModal() {
    if (!this._setupOpen) return "";
    const close = () => (this._setupOpen = false);
    const status = this.status ?? {};
    const active = !!status.active;
    return html`
      <dialog class="overlay-backdrop decode-setup-layer"
        aria-labelledby="decode-setup-title"
        style="margin:0;padding:0;border:0;width:100vw;height:100vh;max-width:none;max-height:none;color:inherit"
        @cancel=${(e) => { e.preventDefault(); close(); }} @click=${close}>
        <div
          class="panel"
          id="decode-setup-modal"
          aria-labelledby="decode-setup-title"
          style="margin:60px auto 0;width:min(480px,92vw);max-height:80vh;overflow:auto;align-self:flex-start"
          @click=${(e) => e.stopPropagation()}
        >
          <div style="display:flex;align-items:center">
            <h2 id="decode-setup-title" style="flex:1">Decode setup</h2>
            <button title="close" aria-label="close decode setup" @click=${close}>&times;</button>
          </div>
          ${this.failure
            ? html`<failure-note
                .failure=${this.failure}
                @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
              ></failure-note>`
            : ""}
          <div class="decode-setup-tabs" role="tablist" aria-label="Decode mode" style="display:flex;gap:6px;margin-bottom:10px">
            ${[["single", "Single-channel decode"], ["band", "Band decode"]].map(([id, label]) => html`<button role="tab"
              aria-selected=${this._setupTab === id ? "true" : "false"} @click=${() => (this._setupTab = id)}>${label}</button>`)}
          </div>
          ${this._setupTab === "band" ? this._bandSection() : this._singleSection(active)}
        </div>
      </dialog>
    `;
  }

  _singleSection(active) {
    return html`
          <h2 style="font-size:1em">Single-channel decode</h2>
          <p class="dim">Decode the tuned frequency. IQ decoders use raw receiver samples automatically.</p>
          ${this._singleHint ? html`<p role="status">${this._singleHint}</p>` : ""}
          <input type="search" aria-label="Search decoders" placeholder="Search decoders…"
            style="width:100%;box-sizing:border-box;margin-bottom:10px"
            .value=${this._decoderQuery} @input=${e => this._decoderQuery = e.target.value} />
          <div class="decoder-status-header dim mono" aria-hidden="true">
            ${[["installed", "INST"], ["tested", "TEST"], ["session", "NOW"]].map(([key, label]) => html`
              <span class="decoder-status-col">${label}<span class="help-mark" tabindex="0" role="note"
                aria-label=${DECODER_STATUS_LEGEND[key]} title=${DECODER_STATUS_LEGEND[key]}
                data-help=${DECODER_STATUS_LEGEND[key]}>?</span></span>`)}
          </div>
          <div class="list" id="decode-channel-list">
            ${groupDecoders(this.decoders).map(({ category, decoders }) => html`
              <section class="decoder-category" aria-label=${category}
                style=${decoders.some(d => this._matchesDecoder(d)) ? "" : "display:none"}>
                <h3 class="decoder-category-title">${category}</h3>
                ${decoders.map((d) => {
              // usable mirrors readiness.usable (installed, regardless of
              // validation state) -- an untested decoder is still selectable,
              // only a missing/incompatible one is disabled.
              const usable = d.readiness ? !!d.readiness.usable : !!d.available;
              const status = decoderStatusIcons(d, readinessLabels(d));
              return html`
                <div class="decoder-choice" style=${this._matchesDecoder(d) ? "" : "display:none"}>
                <label
                  class="row decoder-option"
                  title=${decoderDescription(d)}
                  style="cursor:${usable ? "pointer" : "default"};opacity:${usable ? 1 : 0.55}"
                >
                  <input
                    type="checkbox"
                    data-decoder-id=${d.id}
                    ?disabled=${!usable}
                    .checked=${this._selected.has(d.id)}
                    @change=${() => this._toggle(d.id)}
                  />
                  <span class="decoder-name">
                    <span class="label">${d.id === "lora" ? "LoRa / mesh" : d.label}</span>
                    <span class="decoder-option-category dim">${decoderCategory(d)} · ${(d.demodulators ?? []).join("/")}</span>
                  </span>
                  <span class="decoder-status mono">
                    ${["installed", "tested", "session"].map((key) => html`<span
                      class="decoder-status-col ${status[key].tone}" title=${status[key].title}
                      aria-label=${status[key].title}>${status[key].sym}</span>`)}
                  </span>
                </label>
                ${d.id === "lora" ? html`<details class="lora-profile-controls" style="margin:0 0 16px 28px">
                  <summary style="cursor:pointer">LoRa profiles · MeshCore / Meshtastic / custom</summary>
                  <decoder-prepare .decoder=${'lora'} .frequencyHz=${this.frequencyHz}
                    .restoreToken=${this._restoreToken}
                    @decoder-preset-applied=${e=>this._restoreToken=e.detail.token}
                    @decoder-preset-restored=${()=>this._restoreToken=null}></decoder-prepare>
                </details>` : ""}
                </div>
              `;
                })}
              </section>
            `)}
            ${!this.decoders.length ? html`<p class="empty">no decoders registered</p>` : !this.decoders.some(d => this._matchesDecoder(d)) ? html`<p class="empty" role="status">No matching decoders</p>` : ""}
          </div>
          <div class="control-row" style="margin-top:10px">
            <button
              class="run-start"
              ?disabled=${active || !this._ready || !this._selected.size}
              @click=${() => this._start()}
            >
              ▶ START
            </button>
            <button class="run-stop" ?disabled=${!active} @click=${() => this._stop()}>■ STOP</button>
            ${active ? html`<run-indicator .since=${this.status?.since ?? null} label="DECODING"></run-indicator>` : ""}
            <button @click=${() => this._installPresets()}>INSTALL PRESETS</button>
            <button @click=${() => this._openMessages()}>MESSAGES</button>
          </div>
          ${!this._selected.has("lora") ? html`<decoder-prepare .decoder=${this._selected.size === 1 ? [...this._selected][0] : ''}
            .frequencyHz=${this.frequencyHz} .restoreToken=${this._restoreToken}
            @decoder-preset-applied=${e=>this._restoreToken=e.detail.token}
            @decoder-preset-restored=${()=>this._restoreToken=null}></decoder-prepare>` : ""}
    `;
  }

  /** BAND DECODE section (Decoders P2): a multi-channel session (msdr/decoder/
   * band.py) that runs NFM-family decoders across a span at once. A decoder
   * qualifies when its GET /api/decoders `demodulators` includes "NFM"; the
   * rest are disabled ("single-channel only") since the channelizer only
   * feeds an NFM discriminator per channel. */
  _bandSection() {
    const status = this.status ?? {};
    const band = status.band ?? {};
    const active = !!band.active;
    const channels = band.channels ?? [];
    const candidates = (band.candidates ?? []).slice(0, 5);
    return html`
      <h2 style="font-size:1em">Band decode</h2>
      <div class="list" id="decode-band-list">
        ${groupDecoders(this.decoders).map(({category, decoders}) => html`
          <section class="decoder-category" aria-label=${category}>
            <h3 class="decoder-category-title">${category}</h3>
        ${decoders.map((d) => {
          const qualifies = (d.demodulators ?? []).includes("NFM");
          const enabled = qualifies && d.available;
          return html`
            <label
              class="row"
              style="cursor:${enabled ? "pointer" : "default"};opacity:${enabled ? 1 : 0.55}"
              title=${qualifies ? (d.available ? "" : d.install_hint) : "single-channel only"}
            >
              <input
                type="checkbox"
                ?disabled=${!enabled}
                .checked=${this._bandSelected.has(d.id)}
                @change=${() => this._toggleBand(d.id)}
              />
              <span class="label">${d.label}</span>
              <span class="spacer"></span>
              ${!qualifies ? html`<button type="button" style="font-size:0.75em"
                ?disabled=${!(d.readiness ? d.readiness.usable : d.available)}
                aria-label=${`Use ${d.label} in single-channel decode`}
                title=${d.available ? "Select in the Single-channel decode tab" : d.install_hint}
                @click=${(e) => { e.preventDefault(); e.stopPropagation(); this._selectSingle(d); }}
              >Use single channel</button>` : ""}
            </label>
          `;
        })}
          </section>`)}
      </div>
      <div class="control-row" style="margin-top:8px;gap:12px">
        <label class="dim mono" style="font-size:0.85em;display:flex;align-items:center;gap:4px"
          >MAX CHANNELS
          <input
            type="number"
            min="1"
            style="width:60px"
            .value=${String(this._maxChannels)}
            @change=${(e) => {
              this._maxChannels = Number(e.target.value) || 8;
              saveBandOptions(this._maxChannels, this._thresholdDb);
            }}
        /></label>
        <label class="dim mono" style="font-size:0.85em;display:flex;align-items:center;gap:4px"
          >THRESHOLD dB
          <input
            type="number"
            step="0.5"
            style="width:60px"
            .value=${String(this._thresholdDb)}
            @change=${(e) => {
              this._thresholdDb = Number(e.target.value) || 10;
              saveBandOptions(this._maxChannels, this._thresholdDb);
            }}
        /></label>
      </div>
      <div class="control-row" style="margin-top:8px">
        <button
          class="run-start"
          ?disabled=${active || !this._ready || !this._bandSelected.size}
          @click=${() => this._startBand()}
        >
          ▶ START BAND
        </button>
        <button class="run-stop" ?disabled=${!active} @click=${() => this._stopBand()}>■ STOP BAND</button>
      </div>
      ${channels.length
        ? html`
            <div class="list" style="margin-top:8px">
              ${channels.map(
                (c) => html`
                  <div class="row" style="cursor:default">
                    <span class="mono">${mhz(c.frequency_hz)}</span>
                    <span class="dim mono" style="font-size:0.8em">${c.source}</span>
                    <span class="mono" style="font-size:0.9em"
                      >${c.snr_db != null ? c.snr_db.toFixed(1) : "--"} dB</span
                    >
                    <span class="dim mono" style="font-size:0.85em">${c.events ?? 0} ev</span>
                    <span class="dim mono" style="font-size:0.8em"
                      >${c.last_event_at ? epochHM(c.last_event_at) : "--"}</span
                    >
                    ${c.decoder_status
                      ? html`<span class="dim mono" style="font-size:0.8em">${sessionFactText(c.decoder_status)}</span>`
                      : ""}
                    <span class="spacer"></span>
                    <button
                      ?disabled=${c.source === "memory"}
                      @click=${() => this._saveBandChannel(c.frequency_hz)}
                    >
                      SAVE
                    </button>
                  </div>
                `
              )}
            </div>
          `
        : ""}
      ${candidates.length
        ? html`
            <div class="dim mono" style="font-size:0.8em;margin-top:6px">
              candidates:
              ${candidates.map((c) => `${mhz(c.frequency_hz)} (${c.snr_db.toFixed(1)} dB)`).join(" · ")}
            </div>
          `
        : ""}
    `;
  }

  /** Second line under the lamp when a band session is active: live channel
   * count, cpu_load as a percentage, and a dropped-frames warning (sticky for
   * the session once status.band.iq.dropped has been seen to grow). */
  _bandLine(band) {
    const n = (band.channels ?? []).length;
    const cpu = band.iq?.cpu_load != null ? `${Math.round(band.iq.cpu_load * 100)}%` : "--";
    return html`
      <div class="scanner-deck-line">
        <span class="dim mono" style="font-size:0.85em"
          >${n} ch · cpu ${cpu}${this._dropWarn
            ? html` <span style="color:var(--danger)">⚠ dropped frames</span>`
            : ""}</span
        >
      </div>
    `;
  }

  render() {
    const status = this.status ?? {};
    const lamp = this._lampInfo(this.status);
    const active = !!status.active;
    const startDisabled = active || !this._ready;
    const stopDisabled = !active;
    const bandActive = !!status.band?.active;
    const channelProtocols = (active
      ? (status.decoders ?? []).map((d) => d.id)
      : loadIds(SELECTION_KEY)).map((id) => id.toUpperCase()).join(", ");
    const bandProtocols = (bandActive
      ? status.band?.decoders ?? []
      : loadIds(BAND_SELECTION_KEY)).map((id) => id.toUpperCase()).join(", ");
    const bandStartDisabled = bandActive || !this._ready;
    const bandStopDisabled = !bandActive;
    const noDecoders = !this.decoders.some((d) => d.available);
    const installHint = "Install a decoder — see ⚙";
    const reason = startDisabled && stopDisabled
      ? this._reasonText()
      : !active && noDecoders
        ? installHint
        : "";
    const bandReason = bandStartDisabled && bandStopDisabled
      ? this._reasonText()
      : !bandActive && noDecoders
        ? installHint
        : "";
    const preparationNeeded = this._preparationNeeded;
    return html`
      <div class="scanner-deck" style="position:relative">
        ${!this._setupOpen && this.failure
          ? html`<div class="decode-failure">
              <failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>
              ${/needs IQ rate|full signal bandwidth must fit in IQ span/.test(String(this.failure?.detail ?? ""))
                ? html`<button class="decode-quick-setup" @click=${() => this._openPrepareForFailure()}>Set up ${this.decoders.find(d => String(this.failure?.detail ?? "").startsWith(`${d.label}:`))?.label ?? "decoder"}</button>` : ""}
            </div>`
          : ""}
        <div class="scanner-deck-line">
          <span class="label">Decode</span>
          <button class=${`decode-prepare-button${preparationNeeded ? " needs-attention" : ""}`}
            ?disabled=${!this._ready}
            aria-label=${preparationNeeded ? "Prepare receiver: selected decoder needs different receiver settings" : "Prepare receiver"}
            title=${preparationNeeded ? "Selected decoder needs different receiver settings. Preview changes before applying." : "Preview receiver settings for a decoder"}
            @click=${()=>this._openPrepare()}>⇄</button>
          ${this._probeButton("test", "⚗",
            "Test signal: briefly run compatible decoders on the tuned frequency and report which ones sync. Uses the survey's IQ span; no retuning, nothing saved.")}
          ${this._probeButton("optimize", "◎",
            "Optimize decode: try nearby offsets (0, ±2, ±4 kHz) with compatible decoders; retune and start the best decoder only if a sync advantage repeats.")}
          <led-dot color=${lamp.color} .lit=${lamp.lit} class=${lamp.blink ? "led-blink" : ""}></led-dot>
          <span class="mono">${lamp.label}</span>
          <span class="spacer"></span>
          <button class="decode-messages-button" title="All decoded messages" aria-label="All decoded messages"
            @click=${() => this._openMessages()}>MESSAGES</button>
        </div>
        <div class="scanner-deck-controls">
                <span class="strip-group-label">CHANNEL</span>
                <button class="run-start" aria-label="Start decoding" title="Start decoding"
                  ?disabled=${startDisabled} @click=${() => this._startTop()}>▶</button>
                <button class="run-stop" aria-label="Stop decoding" title="Stop decoding"
                  ?disabled=${stopDisabled} @click=${() => this._stopTop()}>■</button>
                ${active ? html`<run-indicator .since=${status.since ?? null} label="DECODING"></run-indicator>` : ""}
                ${channelProtocols ? html`<span class="decode-identifiers dim mono" style="min-width:0;max-width:40ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title=${`Channel decoders: ${channelProtocols}`}>${channelProtocols}</span>` : ""}
                <button title="Decode setup" @click=${() => this._openSetup()}>⚙</button>
                ${reason ? html`<span class="hint">${reason}</span>` : ""}
              </div>
              <div class="scanner-deck-controls">
                <span class="strip-group-label">BAND</span>
                <button class="run-start" aria-label="Start band decoding" title="Start band decoding"
                  ?disabled=${bandStartDisabled} @click=${() => this._startBandTop()}>▶</button>
                <button class="run-stop" aria-label="Stop band decoding" title="Stop band decoding"
                  ?disabled=${bandStopDisabled} @click=${() => this._stopBandTop()}>■</button>
                ${bandActive
                  ? html`<run-indicator .since=${status.band?.since ?? null} label="BAND"></run-indicator>`
                  : ""}
                ${bandProtocols ? html`<span class="decode-identifiers dim mono" style="min-width:0;max-width:40ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title=${`Band decoders: ${bandProtocols}`}>${bandProtocols}</span>` : ""}
                ${bandReason ? html`<span class="hint">${bandReason}</span>` : ""}
              </div>
              ${status.band?.active ? this._bandLine(status.band) : ""}
              ${(status.decoders ?? []).some((d) => d.id === "cw")
                ? html`<cw-transcript .events=${this.decodeEvents} .since=${status.since ?? null}
                    .frequencyHz=${status.frequency_hz ?? null} .compact=${true}></cw-transcript>`
                : html`<div class="scanner-deck-line decode-last-event-line">${this._lastEventLine()}</div>`}
              ${this._decoderFactsLines(status.decoders)}
              ${this._probeLine()}
      </div>
      ${this._setupModal()}
    `;
  }
}

customElements.define("decode-deck", DecodeDeck);
