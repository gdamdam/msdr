import { LitElement, html } from "../vendor/lit.js";
import { mhz, timeHM } from "../format.js";
import { decoderValidation, groupDecoders } from "../decoder-info.js";
import {
  patchSignal,
  deleteSignal,
  createProfile,
  patchProfile,
  deleteProfile,
  createTag,
  patchTag,
  identifySignal,
  getSkyFor,
} from "../api.js";
import "./ident-panel.js";
import "./failure-note.js";
import { antennaOptions } from "../antenna.js";

const DEMODULATORS = ["", "AM", "USB", "LSB", "CW", "SAM", "NFM", "WFM"];
const STATUSES = ["unknown", "tentative", "identified"];
// Above this many tags the drawer hides unselected ones behind "N more".
const TAGS_COLLAPSE_AT = 6;
// Must match msdr/library/models.py SIGNAL_COLORS / TAG_COLORS (see contract.md).
const SIGNAL_COLORS = ["red", "orange", "yellow", "green", "blue", "magenta", "cyan", "white"];
const TAG_COLORS = ["red", "orange", "green", "blue", "violet", "gray"];
// Must match msdr/providers/nextpass.py SATELLITES (see contract.md).
const SATELLITES = ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"];

function blankSignalDraft() {
  return {
    name: "",
    status: "unknown",
    frequencyMhz: "",
    demodulator: "",
    filterBandwidthHz: "",
    profileId: "",
    station: "",
    location: "",
    latitude: "",
    longitude: "",
    urlsText: "",
    notes: "",
    tagIds: new Set(),
    color: null,
    satellite: null,
    decoders: new Set(),
    antenna: null,
  };
}

function signalDraftFrom(item) {
  return {
    name: item.name,
    status: item.status,
    frequencyMhz: (item.frequency_hz / 1_000_000).toFixed(6),
    demodulator: item.demodulator ?? "",
    filterBandwidthHz: item.filter_bandwidth_hz ?? "",
    profileId: item.profile_id ?? "",
    station: item.station ?? "",
    location: item.location ?? "",
    latitude: item.latitude ?? "",
    longitude: item.longitude ?? "",
    urlsText: (item.urls ?? []).join("\n"),
    notes: item.notes ?? "",
    tagIds: new Set(item.tag_ids ?? []),
    color: item.color ?? null,
    satellite: item.satellite ?? null,
    decoders: new Set(item.decoders ?? []),
    antenna: item.antenna ?? null,
  };
}

function blankProfileDraft() {
  return {
    name: "",
    description: "",
    nativeProfileName: "",
    antenna: "",
    lnaState: "",
    sampleRateHz: "",
    squelchEnabled: "",
    squelchThreshold: "",
    audioAgcEnabled: "",
    audioAgcThreshold: "",
    noiseReductionEnabled: "",
    noiseReductionStrength: "",
  };
}

function profileDraftFrom(item) {
  const str = (v) => (v === null || v === undefined ? "" : String(v));
  return {
    name: item.name,
    description: item.description ?? "",
    nativeProfileName: str(item.native_profile_name),
    antenna: str(item.antenna),
    lnaState: str(item.lna_state),
    sampleRateHz: str(item.sample_rate_hz),
    squelchEnabled: item.squelch_enabled === null || item.squelch_enabled === undefined ? "" : String(item.squelch_enabled),
    squelchThreshold: str(item.squelch_threshold),
    audioAgcEnabled:
      item.audio_agc_enabled === null || item.audio_agc_enabled === undefined ? "" : String(item.audio_agc_enabled),
    audioAgcThreshold: str(item.audio_agc_threshold),
    noiseReductionEnabled:
      item.noise_reduction_enabled === null || item.noise_reduction_enabled === undefined
        ? ""
        : String(item.noise_reduction_enabled),
    noiseReductionStrength: str(item.noise_reduction_strength),
  };
}

// Profile fields the backend stores as nullable. Clearing one in the form
// has to PATCH an explicit null: compact() drops undefined, and the route's
// exclude_unset semantics then leave the old value in place, so antenna /
// native profile / squelch could never be cleared once set.
const NULLABLE_PROFILE_FIELDS = [
  "native_profile_name",
  "antenna",
  "lna_state",
  "sample_rate_hz",
  "squelch_enabled",
  "squelch_threshold",
  "audio_agc_enabled",
  "audio_agc_threshold",
  "noise_reduction_enabled",
  "noise_reduction_strength",
];

const maybeStr = (v) => (v === "" ? undefined : v);
const maybeNum = (v) => (v === "" ? undefined : Number(v));
const maybeBool = (v) => (v === "" ? undefined : v === "true");
const compact = (obj) => Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined));

export class Drawer extends LitElement {
  static properties = {
    selected: { type: Object },
    profiles: { type: Array },
    tags: { type: Array },
    passes: { type: Object },
    state: { type: Object },
    identFeature: { type: Boolean }, // state.features.ident -- hides the "∿ Ident" button when off
    trackingFeature: { type: Boolean }, // state.features.tracking -- hides the TRACK button when off
    decoders: { type: Array }, // GET /api/decoders registry, for the DECODERS checkbox list
    failure: { type: Object },
    _draft: { state: true },
    _tagsPanelOpen: { state: true },
    _tagsExpanded: { state: true },
    _identLoading: { state: true },
    _identCandidates: { state: true },
    _skyPos: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.selected = null;
    this.profiles = [];
    this.tags = [];
    this.passes = null;
    this.state = null;
    this.identFeature = true;
    this.trackingFeature = true;
    this.decoders = [];
    this.failure = null;
    this._draft = null;
    this._tagsPanelOpen = false;
    this._tagsExpanded = false;
    this._identLoading = false;
    this._identCandidates = null; // null = IDENT not run yet for this selection
    this._skyPos = null;
  }

  updated(changed) {
    if (!changed.has("selected")) return;
    this._tagsPanelOpen = false;
    this._tagsExpanded = false;
    this._identLoading = false;
    this._identCandidates = null;
    this._skyPos = null;
    if (!this.selected) {
      this._draft = null;
    } else if (this.selected.kind === "signal") {
      this._draft = signalDraftFrom(this.selected.item);
      // Row "∿" actions and the "I" keyboard shortcut select the signal and
      // ask for IDENT to run immediately, flagged via selected.identify.
      if (this.selected.identify) this._runIdent();
      if (this._draft.satellite) this._loadSkyPos(this._draft.satellite);
    } else {
      this._draft = this.selected.item
        ? profileDraftFrom(this.selected.item)
        : this.selected.prefill
          ? profileDraftFrom({ name: "", ...this.selected.prefill })
          : blankProfileDraft();
    }
  }

  /** Current elevation for the TRACK gate below -- degrades to "unknown"
   * (disabled) rather than throwing when /api/sky isn't there yet. */
  async _loadSkyPos(satellite) {
    try {
      const res = await getSkyFor(satellite, 1);
      this._skyPos = res.position ?? null;
    } catch {
      this._skyPos = null;
    }
  }

  /** U1: forwards to app.js's _doTrackToggle (via a "track-toggle" event)
   * rather than calling startTrack/stopTrack here directly, so starting a
   * track goes through the shared "will this interrupt the current owner"
   * gate (confirmInterrupt) in one place. */
  _toggleTrack(active) {
    this.dispatchEvent(new CustomEvent("track-toggle", { detail: { active, id: this.selected.item.id } }));
  }

  _close() {
    this.dispatchEvent(new CustomEvent("close"));
  }

  _error(message) {
    this.dispatchEvent(new CustomEvent("api-error", { detail: { message } }));
  }

  _set(key, value) {
    this._draft = { ...this._draft, [key]: value };
  }

  /** Collapsed tag list: selected tags always show; the rest hide behind "N more"
   * once the library grows past a handful, so the drawer stays short. */
  _visibleTags(d) {
    if (this._tagsExpanded || this.tags.length <= TAGS_COLLAPSE_AT) return this.tags;
    return this.tags.filter((t) => d.tagIds.has(t.id));
  }

  _hiddenTagCount(d) {
    if (this.tags.length <= TAGS_COLLAPSE_AT) return 0;
    return this.tags.length - this._visibleTags(d).length;
  }

  _toggleTag(id) {
    const next = new Set(this._draft.tagIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._set("tagIds", next);
  }

  _toggleDecoder(id) {
    const next = new Set(this._draft.decoders);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this._set("decoders", next);
  }

  async _newTag() {
    const name = window.prompt("New tag name:");
    if (!name) return;
    try {
      const tag = await createTag(name.trim());
      this.tags = [...this.tags, tag];
      this._toggleTag(tag.id);
      this.dispatchEvent(new CustomEvent("refresh"));
    } catch (err) {
      this._error(err.message);
    }
  }

  async _renameTag(tag, name) {
    const trimmed = name.trim();
    if (!trimmed || trimmed === tag.name) return;
    try {
      const updated = await patchTag(tag.id, { name: trimmed });
      this.tags = this.tags.map((t) => (t.id === tag.id ? updated : t));
      this.dispatchEvent(new CustomEvent("refresh"));
    } catch (err) {
      this._error(err.message);
    }
  }

  async _recolorTag(tag, color) {
    try {
      const updated = await patchTag(tag.id, { color });
      this.tags = this.tags.map((t) => (t.id === tag.id ? updated : t));
      this.dispatchEvent(new CustomEvent("refresh"));
    } catch (err) {
      this._error(err.message);
    }
  }

  async _saveSignal() {
    const d = this._draft;
    const payload = {
      name: d.name,
      status: d.status,
      frequency_hz: Math.round(parseFloat(d.frequencyMhz) * 1_000_000),
      demodulator: d.demodulator || null,
      filter_bandwidth_hz: d.filterBandwidthHz === "" ? null : parseInt(d.filterBandwidthHz, 10),
      profile_id: d.profileId || null,
      station: d.station,
      location: d.location,
      latitude: d.latitude === "" ? null : Number(d.latitude),
      longitude: d.longitude === "" ? null : Number(d.longitude),
      urls: d.urlsText.split("\n").map((s) => s.trim()).filter(Boolean),
      notes: d.notes,
      tag_ids: Array.from(d.tagIds),
      color: d.color ?? null,
      satellite: d.satellite ?? null,
      decoders: Array.from(d.decoders),
      antenna: d.antenna ?? null,
    };
    try {
      await patchSignal(this.selected.item.id, payload);
      this.dispatchEvent(new CustomEvent("refresh"));
      this._close();
    } catch (err) {
      this._error(err.message);
    }
  }

  /** Runs GET /api/signals/{id}/identify. Backend may not exist yet (404) --
   * that surfaces through the normal api-error -> footer status line path
   * rather than breaking the drawer. */
  async _runIdent() {
    this._identLoading = true;
    this._identCandidates = null;
    this.dispatchEvent(new CustomEvent("action", { detail: { message: "scanning…" } }));
    try {
      const res = await identifySignal(this.selected.item.id);
      this._identCandidates = res.candidates;
    } catch (err) {
      this._error(err.message);
    } finally {
      this._identLoading = false;
    }
  }

  /** Fills the draft from a chosen candidate -- name, tentative status, and
   * the category tag if one already exists by that name. Nothing is saved
   * until Save. Frequency is deliberately left alone: candidate.frequency_hz,
   * when set, is just the matched channel's centre near the signal's own
   * tuned frequency, so there's nothing useful to overwrite it with. */
  _pickCandidate(candidate) {
    const tag = this.tags.find(
      (t) => t.name.toLowerCase() === (candidate.category ?? "").toLowerCase()
    );
    const tagIds = new Set(this._draft.tagIds);
    if (tag) tagIds.add(tag.id);
    this._draft = { ...this._draft, name: candidate.name, status: "tentative", tagIds };
    this.dispatchEvent(
      new CustomEvent("action", {
        detail: { message: `IDENT: ${candidate.name} applied to draft — SAVE to keep` },
      })
    );
  }

  async _deleteSignal() {
    if (!window.confirm(`Delete "${this.selected.item.name}"?`)) return;
    try {
      await deleteSignal(this.selected.item.id);
      this.dispatchEvent(new CustomEvent("refresh"));
      this._close();
    } catch (err) {
      this._error(err.message);
    }
  }

  /** @param existing the profile being edited, or null when creating one. */
  _profilePayload(existing = null) {
    const d = this._draft;
    const payload = compact({
      name: maybeStr(d.name),
      description: maybeStr(d.description),
      native_profile_name: maybeStr(d.nativeProfileName),
      antenna: maybeStr(d.antenna),
      lna_state: maybeNum(d.lnaState),
      sample_rate_hz: maybeNum(d.sampleRateHz),
      squelch_enabled: maybeBool(d.squelchEnabled),
      squelch_threshold: maybeNum(d.squelchThreshold),
      audio_agc_enabled: maybeBool(d.audioAgcEnabled),
      audio_agc_threshold: maybeNum(d.audioAgcThreshold),
      noise_reduction_enabled: maybeBool(d.noiseReductionEnabled),
      noise_reduction_strength: maybeNum(d.noiseReductionStrength),
    });
    if (!existing) return payload;
    // description is a plain string on the model, so clearing it sends "".
    if (payload.description === undefined && (existing.description ?? "") !== "") {
      payload.description = "";
    }
    for (const key of NULLABLE_PROFILE_FIELDS) {
      if (payload[key] === undefined && existing[key] !== null && existing[key] !== undefined) {
        payload[key] = null;
      }
    }
    return payload;
  }

  async _saveProfile() {
    const existing = this.selected.item;
    if (!existing && !this._draft.name.trim()) {
      this._error("Name is required");
      return;
    }
    try {
      if (existing) {
        await patchProfile(existing.id, this._profilePayload(existing));
      } else {
        await createProfile({ name: this._draft.name, ...this._profilePayload() });
      }
      this.dispatchEvent(new CustomEvent("refresh"));
      this._close();
    } catch (err) {
      this._error(err.message);
    }
  }

  async _deleteProfile() {
    if (!window.confirm(`Delete profile "${this.selected.item.name}"?`)) return;
    try {
      await deleteProfile(this.selected.item.id);
      this.dispatchEvent(new CustomEvent("refresh"));
      this._close();
    } catch (err) {
      this._error(err.message);
    }
  }

  render() {
    if (!this.selected || !this._draft) return html``;
    return html`
      <div class="overlay-backdrop" @click=${() => this._close()}>
        <div
          class="panel"
          style="margin-left:auto;width:min(420px,90vw);height:100%;border-radius:0;overflow-y:auto;display:flex;flex-direction:column;gap:10px"
          @click=${(ev) => ev.stopPropagation()}
        >
          ${this.failure
            ? html`<failure-note .failure=${this.failure} @dismiss=${() => this.dispatchEvent(new CustomEvent("dismiss-failure"))}></failure-note>`
            : ""}
          ${this.selected.kind === "signal" ? this._signalForm() : this._profileForm()}
        </div>
      </div>
    `;
  }

  _field(label, node) {
    return html`<label style="display:flex;flex-direction:column;gap:4px;font-size:0.85em"
      ><span class="dim">${label}</span>${node}</label
    >`;
  }

  /** Like _field but a plain div: a <label> forwards clicks to its first button,
   * so groups that contain buttons (tag chips + "+ tag") must not be labels. */
  _group(label, node) {
    return html`<div style="display:flex;flex-direction:column;gap:4px;font-size:0.85em">
      <span class="dim">${label}</span>${node}
    </div>`;
  }

  /** Line under the sat select when the passes cache (app.js) already has a
   * pass for it -- degrades to nothing when passes haven't loaded or don't
   * cover this satellite. Passes come rank-ordered, not chronological, so
   * this picks the soonest future rise for the satellite (same rule as
   * passes-view's header countdown), not just the first list match. */
  _nextPassLine(satellite) {
    const now = Date.now();
    let best = null;
    let bestRise = Infinity;
    for (const p of this.passes?.passes ?? []) {
      if (p.satellite !== satellite) continue;
      const rise = Date.parse(p.rise);
      if (Number.isNaN(rise) || rise <= now) continue;
      if (rise < bestRise) {
        best = p;
        bestRise = rise;
      }
    }
    if (!best) return "";
    const elev = best.max_elevation_deg != null ? `${Math.round(best.max_elevation_deg)}°` : "--";
    return html`<p class="dim mono" style="font-size:0.85em;margin:0">
      NEXT PASS  ${timeHM(best.rise)} ▸ ${timeHM(best.set)}  ${elev}
    </p>`;
  }

  /** TRACK toggle for the open signal -- enabled only when connected, started,
   * and its bound satellite is currently above the horizon (or already the
   * active track, so Stop always stays reachable). */
  _trackButton() {
    const tracking = this.state?.tracking ?? null;
    const active = !!(tracking?.active && tracking.signal_id === this.selected.item.id);
    const above = this._skyPos?.elevation_deg != null && this._skyPos.elevation_deg >= 0;
    const enabled = active || (!!this.state?.connected && !!this.state?.started && above);
    return html`<button
      title="Track (Doppler-follow)"
      ?disabled=${!enabled}
      style=${active ? "color:var(--accent);border-color:var(--accent)" : ""}
      @click=${() => this._toggleTrack(active)}
    >
      [ ⦿ TRACK ]
    </button>`;
  }

  /** The signal edit form's Antenna options: the app's latest known
   * valid_antennas when connected, else a fixed A/B/C (SDRconnect's own
   * letters) so the field still works while disconnected or before state
   * has loaded. */
  _antennaOptions() {
    const known = antennaOptions(this.state?.props?.valid_antennas);
    return known.length ? known : antennaOptions("Antenna A,Antenna B,Antenna C");
  }

  _signalForm() {
    const d = this._draft;
    return html`
      <h2 style="margin:0">Memory Edit</h2>
      ${this._identLoading || this._identCandidates !== null
        ? html`<ident-panel
            .loading=${this._identLoading}
            .candidates=${this._identCandidates}
            .tags=${this.tags}
            .tz=${this.state?.timezone ?? null}
            @pick=${(ev) => this._pickCandidate(ev.detail.candidate)}
          ></ident-panel>`
        : ""}
      ${this._field(
        "name",
        html`<input name="name" .value=${d.name} @input=${(e) => this._set("name", e.target.value)} />`
      )}
      ${this._field(
        "status",
        html`<select name="status" @change=${(e) => this._set("status", e.target.value)}>
          ${STATUSES.map(
            (s) => html`<option value=${s} ?selected=${s === d.status}>${s}</option>`
          )}
        </select>`
      )}
      ${this._field(
        "frequency (MHz)",
        html`<input
          name="frequency-mhz"
          class="mono"
          .value=${d.frequencyMhz}
          @input=${(e) => this._set("frequencyMhz", e.target.value)}
        />`
      )}
      ${this._field(
        "demodulator",
        html`<select name="demodulator" @change=${(e) => this._set("demodulator", e.target.value)}>
          ${DEMODULATORS.map(
            (m) => html`<option value=${m} ?selected=${m === d.demodulator}>${m || "--"}</option>`
          )}
        </select>`
      )}
      ${this._field(
        "bandwidth (Hz)",
        html`<input
          name="bandwidth-hz"
          type="number"
          .value=${d.filterBandwidthHz}
          @input=${(e) => this._set("filterBandwidthHz", e.target.value)}
        />`
      )}
      ${this._field(
        "antenna",
        html`<select name="antenna" aria-label="Antenna"
          @change=${(e) => this._set("antenna", e.target.value || null)}>
          <option value="" ?selected=${!d.antenna}>Keep current (don't change)</option>
          ${this._antennaOptions().map(
            (opt) => html`<option value=${opt.value} ?selected=${opt.value === d.antenna}>${opt.letter}</option>`
          )}
        </select>`
      )}
      ${this._field(
        "profile",
        html`<select name="profile-id" @change=${(e) => this._set("profileId", e.target.value)}>
          <option value="" ?selected=${!d.profileId}>--</option>
          ${this.profiles.map(
            (p) => html`<option value=${p.id} ?selected=${p.id === d.profileId}>${p.name}</option>`
          )}
        </select>`
      )}
      ${this._field(
        "sat",
        html`<select
          name="satellite"
          @change=${(e) => {
            const sat = e.target.value || null;
            this._set("satellite", sat);
            this._skyPos = null;
            if (sat) this._loadSkyPos(sat);
          }}
        >
          <option value="" ?selected=${!d.satellite}>—</option>
          ${SATELLITES.map(
            (sat) => html`<option value=${sat} ?selected=${sat === d.satellite}>${sat}</option>`
          )}
        </select>`
      )}
      ${d.satellite ? this._nextPassLine(d.satellite) : ""}
      ${this.decoders.length
        ? this._group(
            "decoders",
            html`${groupDecoders(this.decoders).map(({ category, decoders }) => html`
              <section class="decoder-category" aria-label=${category}>
                <h3 class="decoder-category-title">${category}</h3>
                <div style="display:flex;flex-wrap:wrap;gap:10px">
                  ${decoders.map((dec) => {
                    const validation = decoderValidation(dec);
                    return html`<label
                      style="display:flex;align-items:center;gap:4px;cursor:pointer;opacity:${dec.available ? 1 : 0.55}"
                      title=${[dec.available ? "" : dec.install_hint, validation.title].filter(Boolean).join(" — ")}
                    >
                      <input
                        type="checkbox"
                        .checked=${d.decoders.has(dec.id)}
                        @change=${() => this._toggleDecoder(dec.id)}
                      />
                      ${dec.label}
                      <span class="dim" style="font-size:0.8em">${validation.text}</span>
                    </label>`;
                  })}
                </div>
              </section>
            `)}`
          )
        : ""}
      ${this._field(
        "station",
        html`<input name="station" .value=${d.station} @input=${(e) => this._set("station", e.target.value)} />`
      )}
      ${this._field(
        "location",
        html`<input name="location" .value=${d.location} @input=${(e) => this._set("location", e.target.value)} />`
      )}
      ${this._field(
        "latitude",
        html`<input name="latitude" type="number" min="-90" max="90" step="any" .value=${d.latitude} @input=${(e) => this._set("latitude", e.target.value)} />`
      )}
      ${this._field(
        "longitude",
        html`<input name="longitude" type="number" min="-180" max="180" step="any" .value=${d.longitude} @input=${(e) => this._set("longitude", e.target.value)} />`
      )}
      ${this._field(
        "urls (one per line)",
        html`<textarea
          name="urls"
          rows="3"
          .value=${d.urlsText}
          @input=${(e) => this._set("urlsText", e.target.value)}
        ></textarea>`
      )}
      ${this._field(
        "notes",
        html`<textarea
          name="notes"
          rows="3"
          .value=${d.notes}
          @input=${(e) => this._set("notes", e.target.value)}
        ></textarea>`
      )}
      ${this._field(
        "color",
        html`<div class="swatch-row">
          ${SIGNAL_COLORS.map(
            (c) => html`
              <span
                class="swatch ${d.color === c ? "selected" : ""}"
                style="--sw:var(--led-${c})"
                title=${c}
                @click=${() => this._set("color", c)}
              ></span>
            `
          )}
          <span
            class="swatch none ${!d.color ? "selected" : ""}"
            title="none"
            @click=${() => this._set("color", null)}
            >&times;</span
          >
        </div>`
      )}
      ${this._group(
        "tags",
        html`<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
          ${this._visibleTags(d).map(
            (t) => html`
              <span
                class="tag-chip mono ${d.tagIds.has(t.id) ? "selected" : ""}"
                style="cursor:pointer;border-color:${d.tagIds.has(t.id) ? "var(--accent)" : "transparent"};${d.tagIds.has(
                  t.id
                )
                  ? "color:var(--accent)"
                  : ""}"
                @click=${() => this._toggleTag(t.id)}
                >[${t.name}]</span
              >
            `
          )}
          ${this._hiddenTagCount(d) > 0 || this._tagsExpanded
            ? html`<button
                style="font-size:0.8em;padding:2px 8px"
                @click=${() => (this._tagsExpanded = !this._tagsExpanded)}
              >
                ${this._tagsExpanded ? "less ▴" : `${this._hiddenTagCount(d)} more ▾`}
              </button>`
            : ""}
          <button style="font-size:0.8em;padding:2px 8px" @click=${() => this._newTag()}>+ tag</button>
          <button
            style="font-size:0.8em;padding:2px 8px"
            @click=${() => (this._tagsPanelOpen = !this._tagsPanelOpen)}
          >
            ${this._tagsPanelOpen ? "tags ▴" : "tags ▾"}
          </button>
        </div>`
      )}
      ${this._tagsPanelOpen ? this._tagsPanel() : ""}

      <div style="display:flex;gap:8px;margin-top:8px">
        <button
          style="color:var(--accent);border-color:var(--accent)"
          @click=${() =>
            this.dispatchEvent(
              new CustomEvent("tune-signal", { detail: { id: this.selected.item.id } })
            )}
        >
          Tune
        </button>
        ${this.identFeature
          ? html`<button title="Identify (I)" @click=${() => this._runIdent()}>∿ Ident</button>`
          : ""}
        ${d.satellite && this.trackingFeature ? this._trackButton() : ""}
        <span class="spacer"></span>
        <button @click=${() => this._deleteSignal()} style="color:var(--danger)">Delete</button>
        <button @click=${() => this._saveSignal()}>Save</button>
      </div>
      <p class="dim" style="font-size:0.8em;margin:0">${mhz(this.selected.item.frequency_hz)} originally</p>
    `;
  }

  /** MEMORY EDIT > TAGS sub-panel: edits tag name/color themselves (not which
   * tags this signal carries -- that's the chip row above). Each edit calls
   * PATCH /api/tags/{id} directly. */
  _tagsPanel() {
    return html`
      <div class="panel" style="margin-top:4px">
        <h2 style="border-bottom:none;padding-bottom:0">Tags</h2>
        <div style="display:flex;flex-direction:column;gap:8px">
          ${this.tags.map(
            (t) => html`
              <div style="display:flex;align-items:center;gap:8px">
                <input
                  class="mono"
                  style="flex:1"
                  .value=${t.name}
                  @change=${(e) => this._renameTag(t, e.target.value)}
                />
                <div class="swatch-row">
                  ${TAG_COLORS.map(
                    (c) => html`
                      <span
                        class="swatch ${t.color === c ? "selected" : ""}"
                        style="--sw:var(--tag-${c})"
                        title=${c}
                        @click=${() => this._recolorTag(t, c)}
                      ></span>
                    `
                  )}
                  <span
                    class="swatch none ${!t.color ? "selected" : ""}"
                    title="none"
                    @click=${() => this._recolorTag(t, null)}
                    >&times;</span
                  >
                </div>
              </div>
            `
          )}
        </div>
      </div>
    `;
  }

  _boolSelect(key, value) {
    return html`<select name=${key} @change=${(e) => this._set(key, e.target.value)}>
      <option value="" ?selected=${value === ""}>leave as is</option>
      <option value="true" ?selected=${value === "true"}>on</option>
      <option value="false" ?selected=${value === "false"}>off</option>
    </select>`;
  }

  _profileForm() {
    const d = this._draft;
    const isNew = !this.selected.item;
    return html`
      <h2 style="margin:0">${isNew ? "New profile" : "Profile"}</h2>
      ${this._field(
        "name",
        html`<input name="name" .value=${d.name} @input=${(e) => this._set("name", e.target.value)} />`
      )}
      ${this._field(
        "description",
        html`<input
          name="description"
          .value=${d.description}
          @input=${(e) => this._set("description", e.target.value)}
        />`
      )}
      ${this._field(
        "native profile name",
        html`<input
          name="native-profile-name"
          .value=${d.nativeProfileName}
          @input=${(e) => this._set("nativeProfileName", e.target.value)}
        />`
      )}
      ${this._field(
        "antenna",
        html`<input name="antenna" .value=${d.antenna} @input=${(e) => this._set("antenna", e.target.value)} />`
      )}
      ${this._field(
        "lna state",
        html`<input
          name="lna-state"
          type="number"
          .value=${d.lnaState}
          @input=${(e) => this._set("lnaState", e.target.value)}
        />`
      )}
      ${this._field(
        "sample rate (Hz)",
        html`<input
          name="sample-rate-hz"
          type="number"
          .value=${d.sampleRateHz}
          @input=${(e) => this._set("sampleRateHz", e.target.value)}
        />`
      )}
      ${this._field("squelch enabled", this._boolSelect("squelchEnabled", d.squelchEnabled))}
      ${this._field(
        "squelch threshold",
        html`<input
          name="squelch-threshold"
          type="number"
          .value=${d.squelchThreshold}
          @input=${(e) => this._set("squelchThreshold", e.target.value)}
        />`
      )}
      ${this._field("audio AGC enabled", this._boolSelect("audioAgcEnabled", d.audioAgcEnabled))}
      ${this._field(
        "audio AGC threshold",
        html`<input
          name="audio-agc-threshold"
          type="number"
          .value=${d.audioAgcThreshold}
          @input=${(e) => this._set("audioAgcThreshold", e.target.value)}
        />`
      )}
      ${this._field(
        "noise reduction enabled",
        this._boolSelect("noiseReductionEnabled", d.noiseReductionEnabled)
      )}
      ${this._field(
        "noise reduction strength",
        html`<input
          name="noise-reduction-strength"
          type="number"
          .value=${d.noiseReductionStrength}
          @input=${(e) => this._set("noiseReductionStrength", e.target.value)}
        />`
      )}

      <div style="display:flex;gap:8px;margin-top:8px">
        ${!isNew
          ? html`<button
              style="color:var(--accent);border-color:var(--accent)"
              @click=${() =>
                this.dispatchEvent(
                  new CustomEvent("apply-profile", { detail: { id: this.selected.item.id } })
                )}
            >
              Apply
            </button>`
          : ""}
        <span class="spacer"></span>
        ${!isNew
          ? html`<button @click=${() => this._deleteProfile()} style="color:var(--danger)">
              Delete
            </button>`
          : ""}
        <button @click=${() => this._saveProfile()}>Save</button>
      </div>
    `;
  }
}

customElements.define("msdr-drawer", Drawer);
