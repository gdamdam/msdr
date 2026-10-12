import { LitElement, html, nothing } from "../vendor/lit.js";
import { bw, decodeChipLabel, callLineText, LAYOUT_PRESETS, DEMODULATORS, BW_PRESETS, stepLabel } from "../format.js";
import { summarizeSavedMatches } from "../saved-frequency.js";
import { profileSummary } from "../profile-summary.js";
import { antennaOptions, antennaDisplay } from "../antenna.js";
import * as api from "../api.js";
import { readTheme, applyTheme, nextTheme, themeLabel } from "../theme.js";
import { dspControlStatus, dspEffectiveValue, dspRejectionNote, settlePendingDsp } from "../audio-dsp.js";
import "./led-dot.js";
import "./segment-readout.js";
import "./band-popover.js";
import "./detect-readout.js";
import { compactText, allocationStatusInfo } from "./allocation-summary.js";
import "./s-meter.js";
import "./failure-note.js";
import { renderCaptureHealth } from "./recorder-control.js";
import "./receiver-status-strip.js";
import "./profile-list.js";

const DISCONNECTED_HINT = "Enable SDRconnect ▸ Preferences ▸ WebSocket Server (port 5454)";

export class InstrumentPanel extends LitElement {
  static properties = {
    state: { type: Object },
    wsDown: { type: Boolean },
    profiles: { type: Array },
    tunedColor: { type: String },
    tags: { type: Array },
    detectFeature: { type: Boolean }, // state.features.detect -- hides the DETECT line when off
    jurisdiction: { type: String }, // settings.allocation_jurisdiction, passed to <detect-readout>
    _bandPopoverOpen: { state: true },
    _allocation: { state: true }, // latest lookup from the context strip, shown in the frequency pill
    failure: { type: Object },
    lastDecodeEvent: { type: Object }, // newest decode event, shown as the DECODE chip tooltip
    stepHz: { type: Number },
    libraryOpen: { type: Boolean },
    recorderFeature: { type: Boolean }, // state.features.recorder -- hides the recorder control when off
    currentFavorite: { type: Boolean },
    antennaLocked: { type: Boolean }, // settings.antenna_locked -- restores keep the current antenna
    modeLocked: { type: Boolean }, // settings.demodulator_locked -- restores keep the current mode
    lnaLocked: { type: Boolean }, // settings.lna_locked -- restores keep the current LNA state
    savedFrequencyMatches: { type: Array },
    recorderStatus: { type: Object },
    scannerDeck: { attribute: false },
    bandScanStatus: { attribute: false },
    bandScanSampleRates: { type: Array },
    surveySettings: { attribute: false },
    peakBusy: { type: Boolean },
    replayActive: { type: Boolean }, // strip VFO controls always address SDRconnect
    frequencyDetailsOpen: { type: Boolean }, // ⓘ reflects/toggles app.js's current-frequency section
    interruptMessage: { type: String }, // U1: confirmInterrupt's pending strip text, or null
    _layoutMenuOpen: { state: true },
    _scannerOpen: { state: true }, // channel-scanner deck disclosure under the settings strip
    _lnaRequested: { state: true }, // set-lna in flight, cleared like _antennaRequested
    _antennaRequested: { state: true }, // set-antenna in flight: the requested "Antenna X" value, until props.active_antenna echoes it back or .failure reports it didn't take
    _dspStatus: { state: true }, // per-property audio-dsp request state: {status: "pending"|"unsupported", value, reason}
    _dspDraft: { state: true }, // in-progress Levels popover edits, keyed by property, cleared once a request settles
    _dspNote: { state: true }, // one-line rejection note beside the Audio line, cleared on the next change
    _profileMenuOpen: { state: true }, // receiver-strip PROFILE dropdown
    _profilesModalOpen: { state: true }, // "Manage profiles…" modal
    _dspOpen: { state: true }, // which AGC/NR/SQL chip's level popover is open (its property key), else null
    _captureHealthOpen: { state: true }, // capture-health panel under the operations row
  };

  createRenderRoot() {
    return this; // use the page's stylesheet, no shadow DOM isolation needed here
  }

  constructor() {
    super();
    this.state = null;
    this.wsDown = false;
    this.profiles = [];
    this.tunedColor = null;
    this.tags = [];
    this.detectFeature = true;
    this.jurisdiction = "US";
    this.failure = null;
    this.stepHz = 1000;
    this.libraryOpen = false;
    this.recorderFeature = true;
    this.currentFavorite = false;
    this.antennaLocked = false;
    this.modeLocked = false;
    this.lnaLocked = false;
    this.savedFrequencyMatches = [];
    this.recorderStatus = null;
    this.scannerDeck = null;
    this.bandScanStatus = { state: "idle" };
    this.bandScanSampleRates = [];
    this.surveySettings = null;
    this.peakBusy = false;
    this.replayActive = false;
    this.interruptMessage = null;
    this._layoutMenuOpen = false;
    this._scannerOpen = false;
    this._antennaRequested = null;
    this._dspStatus = {};
    this._dspDraft = {};
    this._dspNote = null;
    this._dspOpen = null;
    this._profileMenuOpen = false;
    this._profilesModalOpen = false;
    this._onProfileDocClick = (ev) => {
      if (!ev.composedPath().some((n) => n?.classList?.contains?.("profile-menu-wrap"))) this._setProfileMenu(false);
    };
    this._captureHealthOpen = false;
    this._onDspDocClick = (ev) => {
      // Clicks inside the open chip's wrapper (chip or popover) are handled there.
      if (!ev.composedPath().some((n) => n?.classList?.contains?.("receiver-dsp-pill"))) this._setDspOpen(null);
    };
    this._onDspKey = (ev) => {
      if (ev.key !== "Escape") return;
      const key = this._dspOpen;
      this._setDspOpen(null);
      this.querySelector(`.receiver-dsp-pill[data-dsp="${key}"] .receiver-dsp-toggle`)?.focus();
    };
  }

  /** Open one chip's popover (or close all with null); document listeners
   * only live while a popover is open. */
  _setDspOpen(key) {
    this._dspOpen = key;
    document.removeEventListener("click", this._onDspDocClick);
    document.removeEventListener("keydown", this._onDspKey);
    if (key) {
      document.addEventListener("click", this._onDspDocClick);
      document.addEventListener("keydown", this._onDspKey);
    }
  }

  /** Open/close the PROFILE dropdown; the outside-click listener only lives
   * while it is open (Escape is handled on the wrapper so focus can return). */
  _setProfileMenu(open) {
    this._profileMenuOpen = open;
    document.removeEventListener("click", this._onProfileDocClick);
    if (open) document.addEventListener("click", this._onProfileDocClick);
  }

  _profileMenu(activeId, disabled) {
    const fire = (name, detail) => {
      this._setProfileMenu(false);
      this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
    };
    const profiles = this.profiles ?? [];
    const active = profiles.find((p) => p.id === activeId);
    const label = active ? active.name : "custom";
    return html`<span
      class="profile-menu-wrap"
      style="position:relative;display:inline-block;min-width:0"
      @keydown=${(ev) => {
        if (ev.key !== "Escape" || !this._profileMenuOpen) return;
        ev.preventDefault();
        ev.stopPropagation();
        this._setProfileMenu(false);
        this.querySelector("#profile-menu-button")?.focus();
      }}
    >
      <button
        id="profile-menu-button"
        class="value mono"
        style="background:none;border:0;padding:0;cursor:pointer;color:inherit;font:inherit;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        aria-haspopup="menu"
        aria-expanded=${this._profileMenuOpen ? "true" : "false"}
        title="Receiver profiles"
        @click=${() => this._setProfileMenu(!this._profileMenuOpen)}
      >${label} ▾</button>
      ${this._profileMenuOpen
        ? html`<div
            class="panel profile-menu"
            role="menu"
            style="position:absolute;top:100%;left:0;margin-top:4px;padding:6px;display:flex;flex-direction:column;gap:2px;z-index:5;width:max-content;max-width:320px"
          >
            ${profiles.length === 0
              ? html`<span class="dim" style="padding:2px 6px">No saved profiles yet</span>`
              : profiles.map(
                  (p) => html`<button
                    role="menuitemradio"
                    aria-checked=${p.id === activeId ? "true" : "false"}
                    style="text-align:left;display:block"
                    ?disabled=${disabled}
                    title=${disabled ? "Receiver can't be controlled right now" : `Apply ${p.name}`}
                    @click=${() => fire("apply-profile", { id: p.id })}
                  >
                    <span>${p.id === activeId ? "✓ " : ""}${p.name}</span>
                    <span class="dim" style="display:block;font-size:0.85em">${profileSummary(p)}</span>
                  </button>`
                )}
            <hr style="width:100%;margin:4px 0;border:0;border-top:1px solid var(--border, currentColor);opacity:.3" />
            <button role="menuitem" style="text-align:left" @click=${() => fire("new-profile", { fromCurrent: true })}>Save current settings as profile…</button>
            ${active
              ? html`<button role="menuitem" style="text-align:left" @click=${() => fire("open-profile", { id: active.id })}>Edit ${active.name}…</button>`
              : ""}
            ${profiles.length
              ? html`<button role="menuitem" style="text-align:left" @click=${() => {
                  this._setProfileMenu(false);
                  // The modal returns focus to whatever had it on open; the
                  // menu item is about to disappear, so hand it the button.
                  this.querySelector("#profile-menu-button")?.focus();
                  this._profilesModalOpen = true;
                }}>Manage profiles…</button>`
              : ""}
          </div>`
        : ""}
      <profiles-modal
        .open=${this._profilesModalOpen}
        .profiles=${profiles}
        .activeId=${activeId}
        .disabled=${disabled}
        @close=${() => (this._profilesModalOpen = false)}
        @open-profile=${() => (this._profilesModalOpen = false)}
        @new-profile=${() => (this._profilesModalOpen = false)}
      ></profiles-modal>
    </span>`;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._setProfileMenu(false);
    this._setDspOpen(null);
  }

  updated(changed) {
    if (changed.has("surveySettings") || changed.has("peakBusy")) {
      const select = this.querySelector(".receiver-quick-peak select");
      if (select) select.value = String(this.surveySettings?.peak_threshold_db ?? 10);
    }
    // Clear the pending ANT request once SDRconnect echoes it back in props
    // (confirmed), or once a failure comes in for this panel (reverts to the
    // still-confirmed value -- see _onAntennaChange/render's antennaDisplay).
    if (this._antennaRequested) {
      if (changed.has("state") && this.state?.props?.active_antenna === this._antennaRequested) {
        this._antennaRequested = null;
      } else if (changed.has("failure") && this.failure) {
        this._antennaRequested = null;
      }
    }
    if (this._lnaRequested != null) {
      if (changed.has("state") && this.state?.props?.lna_state === this._lnaRequested) {
        this._lnaRequested = null;
      } else if (changed.has("failure") && this.failure) {
        this._lnaRequested = null;
      }
    }
    // A fast SDRconnect echo (over the websocket) can beat the audio-dsp
    // POST's own response -- clear any pending entry as soon as props
    // confirm it rather than waiting on that response (see audio-dsp.js).
    if (changed.has("state")) {
      const next = settlePendingDsp(this._dspStatus, this.state?.props ?? {});
      if (next !== this._dspStatus) this._dspStatus = next;
    }
  }

  _setQuickPeakThreshold(db) {
    if (!this.surveySettings || this.peakBusy || !Number.isFinite(db)) return;
    this.dispatchEvent(new CustomEvent("survey-peak-threshold", {
      detail: { db: Math.max(0, Math.min(60, db)) },
    }));
  }






  /** app.js's "R" keyboard shortcut -- no click event to work with, mirrors
   * focusFrequencyEdit above. */
  toggleRecording() {
    this.querySelector("recorder-control")?.toggleRec();
  }

  /** MODE change -> tune-frequency {hz, demodulator}; BW change -> {hz,
   * filter_bandwidth_hz} (or a "Custom…" prompt for a kHz value not in the
   * mode's presets). Both keep the current VFO hz and let AppService.plan_
   * with_desired's diff-only planner do the rest -- no other prop moves. */
  _emitModeOrBw({ demodulator, filterBandwidthHz } = {}) {
    const hz = this.state?.props?.device_vfo_frequency;
    if (hz === null || hz === undefined) return;
    // manualMode: an explicit MODE pick goes through the mode lock.
    this.dispatchEvent(new CustomEvent("tune-frequency", {
      detail: { hz, demodulator, filterBandwidthHz, manualMode: !!demodulator },
    }));
  }

  /** ANT select's change handler -- shows the requested value with a pending
   * marker (antennaDisplay) until props.active_antenna confirms it or
   * .failure reports it didn't take (see updated()). */
  _onAntennaChange(value) {
    if (!value || value === this._antennaRequested) return;
    this._antennaRequested = value;
    this.dispatchEvent(new CustomEvent("set-antenna", { detail: { antenna: value } }));
  }

  /** LNA field's change handler: out-of-range or unchanged values snap back
   * to the confirmed state instead of sending a request. */
  _onLnaChange(input, props) {
    const value = Number(input.value);
    const low = props.lna_state_min ?? 0;
    const high = props.lna_state_max ?? 59;
    if (!Number.isInteger(value) || value < low || value > high || value === props.lna_state) {
      input.value = props.lna_state ?? "";
      return;
    }
    this._lnaRequested = value;
    this.dispatchEvent(new CustomEvent("set-lna", { detail: { lna_state: value } }));
  }

  /** Audio AGC / Noise reduction / Squelch toggles and their Levels popover
   * fields (receiver-inline-context's Audio line): shows "pending" until
   * SDRconnect's props echo the value back (or settlePendingDsp catches it
   * in updated()), clears to a one-line note on rejection, and stays
   * disabled with the reason in its title when SDRconnect reports the
   * property unsupported. Never shows the word "confirmed" -- see
   * audio-dsp.js. */
  async _setAudioDsp(property, value, label) {
    this._dspNote = null;
    this._dspStatus = { ...this._dspStatus, [property]: { status: "pending", value } };
    let result;
    try {
      result = await api.setAudioDspProperty(property, value);
    } catch (error) {
      result = { status: "rejected", reason: error.message };
    }
    const next = { ...this._dspStatus };
    if (result?.status === "unsupported") {
      next[property] = { status: "unsupported", value, reason: result.reason };
    } else {
      delete next[property];
    }
    this._dspStatus = next;
    this._dspNote = dspRejectionNote(label, result);
    const drafts = { ...this._dspDraft };
    delete drafts[property];
    this._dspDraft = drafts;
  }

  /** One AGC/NR/SQL toggle: aria-pressed on/off (filled/outline via CSS),
   * an ellipsis appended while pending, disabled (with the reason folded
   * into its title) when unsupported/unavailable or the receiver can't be
   * controlled right now. */
  _dspToggle(property, label, title, confirmedValue, disabled, levelField, hint) {
    const entry = this._dspStatus[property];
    const status = dspControlStatus(entry, confirmedValue);
    const pressed = !!dspEffectiveValue(entry, confirmedValue);
    const gated = disabled || status === "unsupported" || status === "unavailable";
    const fullTitle = status === "unsupported" || status === "unavailable"
      ? `${title} (${entry?.reason ?? "SDRconnect hasn't reported this"})`
      : title;
    const open = this._dspOpen === property;
    // The chip opens its own popover (on/off switch + that function's level);
    // aria-pressed keeps showing the on/off state, aria-expanded the popover.
    return html`<span class="receiver-dsp-pill" data-dsp=${property}>
      <button type="button"
        class="receiver-dsp-toggle"
        aria-pressed=${pressed}
        aria-expanded=${open}
        aria-haspopup="dialog"
        data-status=${status}
        title=${fullTitle}
        ?disabled=${gated}
        @click=${() => this._setDspOpen(open ? null : property)}
      >${label}${status === "pending" ? "…" : ""}</button>
      ${open ? html`<div class="receiver-dsp-popover" role="dialog" aria-label=${`${title.split(":")[0]} settings`}>
        <label class="receiver-dsp-switch">
          <input type="checkbox" role="switch" .checked=${pressed} ?disabled=${gated}
            @change=${(ev) => this._setAudioDsp(property, ev.target.checked, label)}>
          <span>${pressed ? "On" : "Off"}</span>
        </label>
        ${levelField}
        <span class="receiver-dsp-hint">${hint}</span>
      </div>` : ""}
    </span>`;
  }

  /** Threshold field: a slider over [min, max] beside a number box. SDRconnect
   * documents no range for these thresholds, so the slider span is only a
   * convenience -- the number box accepts any value, and an out-of-span value
   * just parks the slider at its end. */
  _dspNumberField(property, label, confirmedValue, disabled, short = label, [min, max] = [-100, 100]) {
    const entry = this._dspStatus[property];
    const status = dspControlStatus(entry, confirmedValue);
    const gated = disabled || status === "unsupported" || status === "unavailable";
    const value = this._dspDraft[property] ?? dspEffectiveValue(entry, confirmedValue) ?? "";
    const commit = (raw) => {
      if (raw === "") return;
      const next = Number(raw);
      if (Number.isFinite(next)) this._setAudioDsp(property, next, label);
    };
    return html`<label class="receiver-dsp-field" data-status=${status} title=${label}>
      <span>${short}</span>
      <span class="receiver-dsp-range-wrap">
        <input type="range" min=${min} max=${max} step="0.5" .value=${value === "" ? (min + max) / 2 : value}
          ?disabled=${gated} aria-label=${`${label} slider`}
          @input=${(ev) => { this._dspDraft = { ...this._dspDraft, [property]: ev.target.value }; }}
          @change=${(ev) => commit(ev.target.value)}>
        <input type="number" step="0.5" .value=${value} ?disabled=${gated} aria-label=${label}
          @change=${(ev) => commit(ev.target.value)}>
      </span>
    </label>`;
  }

  _dspSliderField(property, label, confirmedValue, disabled, short = label) {
    const entry = this._dspStatus[property];
    const status = dspControlStatus(entry, confirmedValue);
    const gated = disabled || status === "unsupported" || status === "unavailable";
    const value = this._dspDraft[property] ?? dspEffectiveValue(entry, confirmedValue) ?? 0;
    return html`<label class="receiver-dsp-field" data-status=${status} title=${label}>
      <span>${short}</span>
      <span class="receiver-dsp-range-wrap">
        <input type="range" min="0" max="100" step="1" .value=${value} ?disabled=${gated}
          aria-label=${label}
          @input=${(ev) => { this._dspDraft = { ...this._dspDraft, [property]: ev.target.value }; }}
          @change=${(ev) => {
            const next = Number.parseInt(ev.target.value, 10);
            if (Number.isFinite(next)) this._setAudioDsp(property, next, label);
          }}>
        <output>${value} %</output>
      </span>
    </label>`;
  }

  /** The Audio line: AGC/NR/SQL chips, each opening a small popover with its
   * own on/off switch and level field. Lives in .receiver-inline-context beside antenna,
   * LNA and profile -- moved from listen-workbench.js's Audio & DSP card,
   * which sat above the station list where it didn't belong (see
   * instrument-panel.js's module comment / the UX fix this accompanies). */
  _renderAudioDsp(props, disabled) {
    return html`<div class="receiver-audio-line" role="group" aria-label="Audio DSP (SDRconnect)">
      <div class="receiver-audio-toggles" role="group" aria-label="Audio DSP toggles">
        ${this._dspToggle("agc_enable", "AGC", "Audio AGC: SDRconnect evens out loudness", props.agc_enable, disabled,
          this._dspNumberField("agc_threshold", "AGC threshold", props.agc_threshold, disabled, "AGC threshold", [-100, 100]),
          "SDRconnect's own threshold units. The slider applies on release; the box takes any value.")}
        ${this._dspToggle("noise_reduction_enable", "NR", "Noise reduction: SDRconnect suppresses hiss", props.noise_reduction_enable, disabled,
          this._dspSliderField("noise_reduction_strength", "Noise reduction strength", props.noise_reduction_strength, disabled, "Noise reduction strength"),
          "SDRconnect percent; applies when you release the slider.")}
        ${this._dspToggle("squelch_enable", "SQL", "Squelch: mutes audio below the threshold", props.squelch_enable, disabled,
          this._dspNumberField("squelch_threshold", "Squelch threshold", props.squelch_threshold, disabled, "Squelch threshold", [-140, 0]),
          "SDRconnect's own threshold units. The slider applies on release; the box takes any value.")}
      </div>
      ${this._dspNote ? html`<span class="receiver-dsp-note" role="status">${this._dspNote}</span>` : ""}
    </div>`;
  }

  _onBwChange(value) {
    if (value === "custom") {
      const input = window.prompt("Filter bandwidth (kHz):");
      if (input === null) return;
      const khz = Number(input);
      if (!Number.isFinite(khz) || khz <= 0) return;
      this._emitModeOrBw({ filterBandwidthHz: Math.round(khz * 1000) });
      return;
    }
    this._emitModeOrBw({ filterBandwidthHz: Number(value) });
  }

  /** BW select's options: the mode's presets, plus the current value up
   * front when it isn't one of them (e.g. RAW, which has no presets --
   * "current" per the v0.9.0 design -- or a previously-set custom value). */
  _bwOptions(mode, currentHz) {
    const presets = BW_PRESETS[mode] ?? [];
    if (currentHz && !presets.includes(currentHz)) return [currentHz, ...presets];
    return presets;
  }

  _emitStep(direction, ev) {
    const multiplier = ev.shiftKey ? 10 : ev.altKey ? 0.1 : 1;
    this.dispatchEvent(new CustomEvent("step-frequency", { detail: { direction, multiplier } }));
  }

  /** app.js's "f" keyboard shortcut -- no click event to work with, so it
   * reaches straight into the readout (a no-op when not editable). */
  focusFrequencyEdit() {
    this.querySelector("segment-readout")?.startEdit();
  }

  _applyLayout(key) {
    this._layoutMenuOpen = false;
    this.dispatchEvent(new CustomEvent("apply-layout", { detail: { key } }));
  }

  /** LAYOUT ▾ menu (advanced area) -- a small dropdown of preset buttons.
   * Escape closes it and returns focus to the toggle button; a click on any
   * preset applies it immediately. */
  _layoutMenu() {
    if (!this._layoutMenuOpen) return "";
    return html`
      <div
        class="panel"
        style="position:absolute;top:0;right:100%;margin-right:4px;padding:6px;display:flex;flex-direction:column;gap:2px;z-index:2;width:max-content"
        @keydown=${(ev) => {
          if (ev.key !== "Escape") return;
          ev.preventDefault();
          ev.stopPropagation();
          this._layoutMenuOpen = false;
          this.querySelector("#layout-menu-button")?.focus();
        }}
      >
        ${Object.entries(LAYOUT_PRESETS).map(
          ([key, preset]) =>
            html`<button @click=${() => this._applyLayout(key)}>${preset.label.toUpperCase()}</button>`
        )}
      </div>
    `;
  }

  _savedContext(mode) {
    const summary = summarizeSavedMatches(this.savedFrequencyMatches);
    if (!summary) return "";
    const modes = summary.modes.filter(value => value !== mode);
    const caption = html`${summary.favorite ? "★ Favorite" : "Saved"} · ${summary.name}
      ${modes.length ? html`<small>(${modes.join(" / ")})</small>` : ""}`;
    const classes = `tuned-saved-name${summary.favorite ? " tuned-saved-name--favorite" : ""}`;
    return html`<div class="tuned-saved-context" aria-label="Saved frequency matches">
      ${summary.records.length === 1 ? html`<span class=${classes}>${caption}</span>`
        : html`<details class="tuned-saved-details">
          <summary class=${classes}>${caption} <small>· ${summary.records.length} saved entries</small></summary>
          <ul>${summary.records.map(match => html`<li>
            ${match.favorite ? "★ Favorite" : "Saved"} · ${match.name || "Unnamed channel"}
            ${match.demodulator ? html`<small>(${match.demodulator})</small>` : ""}
          </li>`)}</ul>
        </details>`}
    </div>`;
  }

  /** The ANT select: options from props.valid_antennas (no fallback list --
   * unlike the library edit form, a disconnected/unreported receiver just
   * disables this rather than guessing). Disabled with an explanatory title
   * while not connected, the receiver isn't started/confirmed, a replay is
   * active, there's nothing to choose from, or a set-antenna is in flight. */
  _renderAntennaSelect(props, connected, stopped, complete) {
    const options = antennaOptions(props.valid_antennas);
    const reason = !connected ? "Not connected to SDRconnect"
      : stopped ? "Receiver is stopped"
      : !complete ? "Receiver state not yet confirmed"
      : this.replayActive ? "Read-only while replay is active"
      : !options.length ? "SDRconnect hasn't reported any antennas"
      : null;
    const disabled = !!reason || !!this._antennaRequested;
    const effective = this._antennaRequested ?? props.active_antenna ?? null;
    const display = antennaDisplay(props.active_antenna, this._antennaRequested);
    return html`<select
      id="antenna-select"
      class="value mono"
      aria-label="Antenna"
      title=${reason ?? "Antenna"}
      ?disabled=${disabled}
      @change=${(ev) => this._onAntennaChange(ev.target.value)}
    >
      ${options.map(
        (opt) => html`<option value=${opt.value} title=${opt.value} ?selected=${opt.value === effective}>${
          opt.value === effective ? display : opt.letter
        }</option>`
      )}
    </select>`;
  }

  /** Lock beside ANT / MODE: while on, tuning a memory/profile or
   * Back/Forward keeps the current antenna/mode (enforced server-side in
   * AppService._restore); the ANT/MODE select itself still changes it. */
  _renderLock(locked, eventName, what, noun = what) {
    locked = !!locked;
    // `noun` is what the lock covers: the mode lock also keeps the bandwidth.
    const label = locked
      ? `${noun[0].toUpperCase()}${noun.slice(1)} locked: memories and profiles keep the current ${noun}. Click to unlock.`
      : `Lock ${noun}: memories and profiles can't switch it`;
    return html`<button class="receiver-lock" aria-pressed=${locked ? "true" : "false"}
      title=${label} aria-label=${label}
      @click=${() => this.dispatchEvent(new CustomEvent(eventName, { detail: { locked: !locked } }))}>
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6">
        <rect x="3" y="7" width="10" height="7" rx="1"></rect>
        <path d=${locked ? "M5 7V5a3 3 0 0 1 6 0v2" : "M5 7V5a3 3 0 0 1 5.8-1"}></path>
      </svg>
    </button>`;
  }

  _profileName(id) {
    if (!id) return "custom";
    const profile = this.profiles.find((p) => p.id === id);
    return profile ? profile.name : "custom";
  }

  /** LIVE · SDRconnect, or REPLAY · <name> while state.survey.replay is set --
   * clicking the latter dispatches open-review {name, path} (app.js switches
   * to Review). */
  /** DECODE chip (same lamp-cluster look as the Wi-Fi/BLE chips): the one
   * summary of a running decoder here; click jumps to the decode deck. */
  _decodeChip() {
    const label = decodeChipLabel(this.state);
    if (!label) return "";
    const ev = this.lastDecodeEvent;
    const last = ev ? (ev.kind === "call" ? callLineText(ev) : "") || ev.text || "" : "";
    const open = () => this.dispatchEvent(new CustomEvent("open-decode"));
    return html`<div class="lamp-cluster" role="button" tabindex="0"
      title=${last ? `Last decoded: ${last}` : "Open decode controls"}
      aria-label="Open decode controls"
      @click=${open}
      @keydown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
      <led-dot .color=${"var(--ok)"} .lit=${true}></led-dot>
      <span class="label">${label}</span>
    </div>`;
  }

  _sourceChip() {
    const replay = this.state?.survey?.replay ?? null;
    if (replay) {
      return html`<button
        class="chip source-chip"
        title="Replay is active. Receiver strip controls are disabled; open Review for replay controls."
        @click=${() =>
          this.dispatchEvent(
            new CustomEvent("open-review", { detail: { name: replay.name, path: replay.path } })
          )}
      >
        REPLAY · ${replay.name ?? ""}
      </button>`;
    }
    return html`<span class="chip source-chip" data-help="The displayed frequency and receiver controls are connected to the live SDRconnect VFO. A replay is identified separately here.">LIVE · SDRconnect</span>`;
  }

  /** The "§" icon button in the band-info group: opens the allocation details
   * popover; the Open chart icon lives beside it (see <detect-readout allocationOnly>). */
  _allocationPill(tuneDisabled) {
    const a = this._allocation;
    const ok = !!a && a.status === "ok";
    const text = a === undefined ? "Looking up allocation…"
      : ok ? compactText(a)
      : (allocationStatusInfo(a)?.label ?? "Allocation unavailable");
    // The band-plan hint beside it answers "what is this signal"; the pill is
    // just a "§" icon button, with the full range · jurisdiction · services line
    // in the Details popover. Non-ok statuses dim the § and carry the status in the title.
    // Details popover, not edit: frequency editing is double-click on the digits or F.
    return html`<button type="button"
      class="frequency-allocation frequency-edit-pill seg-info-button${ok ? "" : " frequency-allocation--empty"}"
      aria-label="Band allocation (regulatory table) — click for details"
      aria-haspopup="dialog"
      title=${ok ? "Band allocation (regulatory table) — click for details" : `Band allocation (regulatory table) — click for details · ${text}`}
      ?disabled=${tuneDisabled}
      @click=${(ev) => { this._bandAnchor = ev.currentTarget; this._bandPopoverOpen = true; }}
    >§</button>
    <band-popover
      label="Band allocation details"
      .anchor=${this._bandAnchor ?? null}
      .open=${!!this._bandPopoverOpen}
      .content=${a ? html`<allocation-summary .detailsOnly=${true} .data=${a}></allocation-summary>` : null}
      @close=${() => { this._bandPopoverOpen = false; }}
      @open-allocation-chart=${() => { this._bandPopoverOpen = false; }}
    ></band-popover>`;
  }

  render() {
    const s = this.state;
    const props = s?.props ?? {};
    const connected = !!s?.connected;
    const canControl = !!s?.can_control;
    const complete = !!s?.complete;
    const obs = s?.observation ?? {};
    const history = s?.history ?? { can_back: false, can_forward: false };
    // SDRconnect's receiver is stopped (Play not pressed): it reports
    // started=false and silently ignores every set_property, so tuning
    // would just time out (F7) -- gray out Back/Fwd rather than let that
    // happen.
    const stopped = connected && !(s?.started ?? true);
    // v0.9.0 direct tuning: frequency entry, MODE/BW selects and step
    // buttons all need the receiver actually controllable, same bar as a
    // restore -- SDRconnect running but can_control=false silently drops
    // every set_property, so there's no point offering them.
    // The strip is permanently bound to SDRconnect. Keep it read-only while
    // a replay is the selected source so REPLAY never appears to be tunable.
    const tuneDisabled = this.replayActive || !connected || stopped || !canControl;
    const liveSourceHint = this.replayActive ? "LIVE SDRconnect VFO · read-only while replay is active" : "Live SDRconnect VFO";

    // green when fully connected & controllable, amber when stopped or
    // connected but SDRconnect has can_control=false (read-only), red when
    // disconnected.
    const linkColor = !connected ? "var(--danger)" : stopped || !canControl ? "var(--accent)" : "var(--ok)";
    // Same four checks the diagnostics modal lists (diag-banner.js): only
    // then is the "⚠ LINK" shortcut worth showing.
    const linkProblem = this.wsDown || !connected || stopped || !canControl || s?.synced === false;
    const alertColor = s?.tuned_signal_id ? this.tunedColor ?? null : null;
    // The backend owner describes VFO control. Survey observes without
    // taking the VFO, so show it only when no other controller is active.
    const scanActive = ["running", "paused"].includes(this.bandScanStatus?.state);
    const owner = s?.owner?.kind && s.owner.kind !== "manual" ? s.owner
      : scanActive ? { kind: "band_scan", label: "BAND SCAN" }
        : s?.survey?.active && !s.survey.replay ? { kind: "survey", label: "SURVEY" }
          : s?.owner;
    // Survey and band scan lamps jump to where they're controlled (Discover's
    // live survey / the band-scan controls); manual and other owners don't.
    const ownerOpen = owner?.kind === "survey"
      ? () => this.dispatchEvent(new CustomEvent("open-discover", { detail: { view: "spectrum" }, bubbles: true }))
      : owner?.kind === "band_scan"
        ? () => this.dispatchEvent(new CustomEvent("open-band-scan"))
        : null;

    const recState = this.recorderFeature && connected ? this.recorderStatus?.state : null;
    const recLit = recState === "armed" || recState === "recording";
    const chanState = this.scannerDeck?.scan?.state ?? "idle";
    const closeOverflow = () => { const d = this.querySelector(".strip-overflow-menu"); if (d) d.open = false; };
    const toolEvent = (name) => () => { closeOverflow(); this.dispatchEvent(new CustomEvent(name)); };

    return html`
      <div class="panel" id="instrument-panel">
        <!-- Row 1: status bar. Row 2: readout strip. Row 3: settings strip
             (receiver settings left, operations right). -->
        <div class="strip-row1 top">
          <span class="nameplate">MSDR</span>
          <div
            class="lamp-cluster"
            style="cursor:pointer"
            data-help=${this.wsDown ? "MSDR is unreachable, so receiver status may be stale. Select LINK for diagnostics and retry." : !connected ? "SDRconnect is disconnected. Select LINK to see connection checks and retry." : stopped ? "SDRconnect is connected but its receiver is stopped. Press Play there; select LINK for diagnostics." : !canControl ? "SDRconnect is linked in read-only mode. Select LINK to check receiver control and sync." : "MSDR is connected to SDRconnect and can control the running receiver. Select LINK for detailed checks."}
            @click=${() => this.dispatchEvent(new CustomEvent("toggle-diagnostics"))}
          >
            <!-- wsDown outranks every other lamp state: with no socket to
                 msdr nothing on screen is current, so the lamp goes unlit
                 (grey) rather than claiming a link we can't see. -->
            <led-dot .color=${linkColor} .lit=${!this.wsDown}></led-dot>
            <span class="label">${this.wsDown ? "NO MSDR" : stopped ? "STOPPED" : "LINK"}</span>
          </div>
          <div class="lamp-cluster" data-help="Lights in the saved channel’s color when the tuned frequency matches a saved signal with a compatible mode. This is a channel match, not an RF alarm or confirmed reception.">
            <led-dot .color=${alertColor} .lit=${!!alertColor}></led-dot>
            <span class="label">ALERT</span>
          </div>
          ${owner
            ? html`
                <div class="lamp-cluster" data-help=${owner.kind === "manual" ? "Manual means you control receiver tuning directly. Survey measures the current band; Band scan retunes across a range." : owner.kind === "survey" ? "Survey is measuring the current sampled band. It observes spectrum without taking over manual VFO tuning. STOP ends the survey." : owner.kind === "band_scan" ? "Band scan is stepping the receiver through the configured range. STOP ends the range scan; Survey can remain active." : `${owner.label ?? owner.kind} currently controls receiver tuning. Use STOP beside it to return control.`}
                  role=${ownerOpen ? "button" : nothing}
                  tabindex=${ownerOpen ? "0" : nothing}
                  @click=${() => ownerOpen?.()}
                  @keydown=${(e) => { if (ownerOpen && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); ownerOpen(); } }}
                >
                  <led-dot
                    .color=${owner.kind === "manual" ? null : "var(--accent)"}
                    .lit=${owner.kind !== "manual"}
                  ></led-dot>
                  <span class="label">${owner.label ?? owner.kind ?? "manual"}</span>
                </div>
              `
            : ""}
          ${recLit
            ? html`<div class="lamp-cluster" data-help="The recorder is armed (pre-roll buffering) or recording. Controls are in the Operations group below.">
                <led-dot .color=${"var(--danger)"} .lit=${true}></led-dot>
                <span class="label">${recState === "recording" ? "REC" : "REC ARMED"}</span>
              </div>`
            : ""}
          ${linkProblem
            ? html`<button
                class="link-problem"
                title="connection diagnostics"
                @click=${() => this.dispatchEvent(new CustomEvent("toggle-diagnostics"))}
              >
                ⚠ LINK
              </button>`
            : ""}
          <span class="strip-sep" aria-hidden="true"></span>
          ${this._sourceChip()}
          <receiver-status-strip></receiver-status-strip>
          ${this._decodeChip()}
          <div class="spacer" style="flex:1"></div>
          ${owner && owner.kind !== "manual"
            ? html`<button
                class="owner-stop run-stop"
                title="Stop ${owner.label ?? owner.kind}"
                @click=${() => this.dispatchEvent(new CustomEvent("owner-stop", { detail: { kind: owner.kind } }))}
              >
                ■ STOP
              </button>`
            : ""}
          <div class="strip-primary-actions" aria-label="Receiver actions">
          <button
            id="favorite-current-button"
            class="strip-icon-button"
            title=${this.currentFavorite
              ? "Current frequency is in Favorites"
              : "Add current frequency to Favorites; saves it as a station first if needed"}
            aria-label=${this.currentFavorite ? "Current frequency in Favorites" : "Favorite current frequency"}
            ?disabled=${!connected || !complete || this.replayActive || this.currentFavorite}
            @click=${() => this.dispatchEvent(new CustomEvent("favorite-current"))}
          >${this.currentFavorite ? "★" : "☆"}</button>
          <button
            id="capture-button"
            class="strip-icon-button"
            title="Save current frequency to Library Inbox without adding it to Favorites (S)"
            aria-label="Save to library"
            ?disabled=${!connected || !complete || this.replayActive}
            @click=${() => this.dispatchEvent(new CustomEvent("capture"))}
          >＋</button>
          <button
            class="history-nav"
            aria-label="Back"
            title="Back (Ctrl-[)"
            ?disabled=${!history.can_back || stopped || this.replayActive}
            @click=${() => this.dispatchEvent(new CustomEvent("back"))}
          >
            ←
          </button>
          <button
            class="history-nav"
            aria-label="Forward"
            title="Forward (Ctrl-])"
            ?disabled=${!history.can_forward || stopped || this.replayActive}
            @click=${() => this.dispatchEvent(new CustomEvent("forward"))}
          >
            →
          </button>
          <button
            class="strip-icon-button"
            aria-label="Find"
            title="Find (Ctrl-K)"
            @click=${() => this.dispatchEvent(new CustomEvent("find"))}
          >
            ⌕
          </button>
          <button
            class="strip-icon-button"
            aria-label="Theme"
            title=${`Theme: ${themeLabel(readTheme())} — click to switch`}
            @click=${() => { applyTheme(nextTheme(readTheme())); this.requestUpdate(); }}
          >
            ◐
          </button>
          <button
            class="strip-icon-button"
            aria-label="Setup"
            title="Setup (,)"
            @click=${() => this.dispatchEvent(new CustomEvent("setup"))}
          >
            ⚙
          </button>
          <!-- Tools live behind ⋯ so the status bar stays one short row. -->
          <details class="strip-overflow-menu"
            @keydown=${(ev) => { if (ev.key === "Escape") { ev.stopPropagation(); closeOverflow(); } }}>
            <summary class="strip-icon-button" aria-label="More tools" title="More tools">⋯</summary>
            <div class="strip-tools panel" aria-label="Receiver tools">
              <button @click=${toolEvent("new-profile")}>New profile</button>
              <button title="Connection diagnostics" @click=${toolEvent("toggle-diagnostics")}>Diagnostics</button>
              <span style="position:relative">
                <button
                  id="layout-menu-button"
                  title="Layout presets"
                  aria-haspopup="true"
                  aria-expanded=${this._layoutMenuOpen}
                  @click=${() => (this._layoutMenuOpen = !this._layoutMenuOpen)}
                >
                  Layout ▾
                </button>
                ${this._layoutMenu()}
              </span>
              <button title="Browse recordings" @click=${toolEvent("open-recordings")}>Recordings</button>
            </div>
          </details>
          </div>
        </div>

        ${this.interruptMessage
          ? html`
              <div class="interrupt-strip mono">
                <span>${this.interruptMessage}</span>
                <span class="spacer"></span>
                <button @click=${() => this.dispatchEvent(new CustomEvent("interrupt-continue"))}>
                  CONTINUE
                </button>
                <button @click=${() => this.dispatchEvent(new CustomEvent("interrupt-cancel"))}>CANCEL</button>
              </div>
            `
          : ""}

        ${this.failure
          ? html`<failure-note
              .failure=${this.failure}
              @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}
            ></failure-note>`
          : ""}

        ${!connected
          ? html`<p class="dim" style="margin:10px 0 0">${DISCONNECTED_HINT}</p>`
          : html`
              <div
                class="freq-meter-row readout-strip"
                style=${this.wsDown ? "opacity:0.45" : ""}
              >
                <!-- Titles stay for screen readers; the digits, meter and
                     labelled selects already say what each group is. -->
                <h3 class="receiver-group-title visually-hidden">Receiver frequency</h3>
                <!-- Mock B: digits · step · meter on the left, band info on
                     the right. The allocation pill is rendered in .band-info
                     (not as the readout's edit control) so it sits with the
                     band plan; it still opens the readout's editor on click. -->
                <segment-readout
                  compact
                  title=${liveSourceHint}
                  .hz=${props.device_vfo_frequency ?? null}
                  .unit=${this.replayActive ? "MHz · LIVE READ-ONLY" : "MHz"}
                  .editable=${!tuneDisabled}
                  .fineTune=${true}
                  .editControl=${nothing}
                  @tune-frequency=${(ev) =>
                    this.dispatchEvent(new CustomEvent("tune-frequency", { detail: ev.detail }))}
                  @step-frequency=${(ev) =>
                    this.dispatchEvent(new CustomEvent("step-frequency", { detail: ev.detail }))}
                ></segment-readout>
                <div class="step-row step-group mono" role="group" aria-label="Step size">
                  <button
                    id="step-down"
                    title="Step down (Shift x10, Alt x0.1)"
                    ?disabled=${tuneDisabled}
                    @click=${(ev) => this._emitStep(-1, ev)}
                  >
                    ◂
                  </button>
                  <button
                    id="step-size"
                    title="Step size (click to cycle)"
                    ?disabled=${tuneDisabled}
                    @click=${() => this.dispatchEvent(new CustomEvent("cycle-step"))}
                  >
                    ${stepLabel(this.stepHz)}
                  </button>
                  <button
                    id="step-up"
                    title="Step up (Shift x10, Alt x0.1)"
                    ?disabled=${tuneDisabled}
                    @click=${(ev) => this._emitStep(1, ev)}
                  >
                    ▸
                  </button>
                </div>
                <div class="receiver-signal-level">
                  <h3 class="receiver-group-title visually-hidden">Signal level</h3>
                  <s-meter
                    class="panel-smeter"
                    compact
                    .power=${obs.signal_power_db ?? null}
                    .snr=${obs.signal_snr_db ?? null}
                  ></s-meter>
                </div>
                <div class="spacer"></div>
                <div class="band-info">
                <detect-readout class="frequency-context-strip-host"
                  .allocationOnly=${true}
                  .jurisdiction=${this.jurisdiction ?? "US"}
                  .hz=${props.device_vfo_frequency ?? null} .mode=${props.demodulator ?? null}
                  .bw=${props.filter_bandwidth ?? null} .enabled=${connected && complete}
                  @allocation-change=${(ev) => { this._allocation = ev.detail; }}
                ></detect-readout>
                <button type="button" class="seg-info-button"
                  aria-label=${this.frequencyDetailsOpen ? "Close current frequency details" : "Show current frequency details"}
                  aria-pressed=${this.frequencyDetailsOpen ? "true" : "false"}
                  title=${this.frequencyDetailsOpen ? "Close the current-frequency details below" : "Show details for the current frequency below the receiver; follows retuning"}
                  ?disabled=${props.device_vfo_frequency == null && !this.frequencyDetailsOpen}
                  @click=${() => this.dispatchEvent(new CustomEvent("frequency-details-toggle"))}>ⓘ</button>
                ${this._allocationPill(tuneDisabled)}
                </div>
              </div>
            `}

        <div class="settings-strip receiver-inline-context" aria-label="Receiver settings and operations">
          ${connected
            ? html`
                <span class="strip-group-label">Receiver</span>
                <div class="readout">
                  <label class="label" for="antenna-select">Ant</label>
                  ${this._renderAntennaSelect(props, connected, stopped, complete)}
                  ${this._renderLock(this.antennaLocked, "antenna-lock", "antenna")}
                </div>
                <div class="readout">
                  <label class="label" for="lna-input">LNA</label>
                  <input id="lna-input" class="value mono strip-lna-value" type="number" style="width:4em"
                    aria-label="LNA state" title="LNA state (device-specific, not dB)"
                    min=${props.lna_state_min ?? 0} max=${props.lna_state_max ?? 59} step="1"
                    placeholder="--"
                    .value=${String(this._lnaRequested ?? props.lna_state ?? "")}
                    ?disabled=${stopped || !complete || this.replayActive || this._lnaRequested != null || props.lna_state == null}
                    @change=${(ev) => this._onLnaChange(ev.target, props)} />
                  ${this._renderLock(this.lnaLocked, "lna-lock", "LNA")}
                </div>
                <div class="step-profile" title=${`Profile: ${this._profileName(s.active_profile_id)}`}>
                  <div class="readout">
                    <span class="label">Profile</span>
                    ${this._profileMenu(s.active_profile_id, tuneDisabled)}
                  </div>
                </div>
                <span class="strip-sep" aria-hidden="true"></span>
                <div class="receiver-demodulation">
                  <div class="readout">
                    <label class="label" for="mode-select">Mode</label>
                    <select
                      id="mode-select"
                      class="value mono"
                      ?disabled=${tuneDisabled}
                      @change=${(ev) => this._emitModeOrBw({ demodulator: ev.target.value })}
                    >
                      ${DEMODULATORS.map(
                        (m) => html`<option value=${m} ?selected=${m === props.demodulator}>${m}</option>`
                      )}
                    </select>
                  </div>
                  <div class="readout">
                    <label class="label" for="bw-select">Bandwidth</label>
                    <select
                      id="bw-select"
                      class="value mono"
                      ?disabled=${tuneDisabled}
                      @change=${(ev) => this._onBwChange(ev.target.value)}
                    >
                      ${this._bwOptions(props.demodulator, props.filter_bandwidth).map(
                        (hz) => html`<option value=${hz} ?selected=${hz === props.filter_bandwidth}>${bw(hz)}</option>`
                      )}
                      <option value="custom">Custom…</option>
                    </select>
                  </div>
                  ${this._renderLock(this.modeLocked, "mode-lock", "mode", "mode and bandwidth")}
                </div>
                <span class="strip-sep" aria-hidden="true"></span>
                ${this._renderAudioDsp(props, tuneDisabled)}
              `
            : ""}
          <div class="spacer" style="flex:1"></div>
          <div class="receiver-operations" aria-label="Recording and scanning">
            <span class="strip-group-label">Operations</span>
            ${this.recorderFeature && connected
              ? html`<div class="strip-recording" role="group" aria-label="Recorder controls">
                <recorder-control
                  .status=${this.recorderStatus}
                  .disabled=${stopped}
                  .healthOpen=${this._captureHealthOpen}
                  @capture-health-toggle=${(ev) => (this._captureHealthOpen = ev.detail.open)}
                  @recorder-arm=${(ev) =>
                    this.dispatchEvent(new CustomEvent("recorder-arm", { detail: ev.detail }))}
                  @recorder-rec=${() => this.dispatchEvent(new CustomEvent("recorder-rec"))}
                  @recorder-stop=${() => this.dispatchEvent(new CustomEvent("recorder-stop"))}
                  @recorder-reveal=${(ev) =>
                    this.dispatchEvent(new CustomEvent("recorder-reveal", { detail: ev.detail }))}
                  @recorder-analyze=${(ev) =>
                    this.dispatchEvent(new CustomEvent("recorder-analyze", { detail: ev.detail }))}
                  @open-recordings=${() => this.dispatchEvent(new CustomEvent("open-recordings"))}
                ></recorder-control></div>`
              : ""}
            ${this.scannerDeck
              ? html`<button class="strip-ops-button" aria-expanded=${this._scannerOpen}
                  title="VFO / saved-channel sweep"
                  @click=${() => (this._scannerOpen = !this._scannerOpen)}
                >Channel scan <span class="dim">· ${chanState === "idle" ? "off" : chanState}</span></button>`
              : ""}
          </div>
        </div>

        ${this.recorderFeature && connected && this._captureHealthOpen && this.recorderStatus?.capture_health
          ? renderCaptureHealth(this.recorderStatus.capture_health)
          : ""}

        ${this.scannerDeck
          ? html`<div class="channel-scanner-disclosure" ?hidden=${!this._scannerOpen}>
              <div class="panel-scanner-deck" aria-label="Receiver scanner">${this.scannerDeck}</div>
            </div>`
          : ""}

        ${connected
          ? html`<div class="receiver-context-line" aria-label="Saved channels and catalog context">
              ${this._savedContext(props.demodulator)}
              ${this.detectFeature
                ? html`<detect-readout
                    .hz=${props.device_vfo_frequency ?? null}
                    .mode=${props.demodulator ?? null}
                    .bw=${props.filter_bandwidth ?? null}
                    .enabled=${connected && complete}
                    .jurisdiction=${this.jurisdiction ?? "US"}
                    .tags=${this.tags}
                    .savedNames=${this.savedFrequencyMatches.map(match => match.name).filter(Boolean)}
                  ></detect-readout>`
                : ""}
            </div>`
          : ""}
      </div>
    `;
  }
}

customElements.define("instrument-panel", InstrumentPanel);
