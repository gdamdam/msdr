import { LitElement, html } from "../vendor/lit.js";
import { configureNativeAudio } from "../api.js";

/** Native receiver listening controls. These settings affect MSDR's IQ replay or
 * workbench receiver only; the live SDRconnect controls have a separate API. */
export class NativeAudioDsp extends LitElement {
  static properties = {
    mode: { type: String },
    settings: { attribute: false },
    demodulator: { type: String },
    _error: { state: true },
  };

  constructor() {
    super();
    this.mode = "review";
    this.settings = {};
    this.demodulator = "NFM";
    this._error = "";
  }

  async _change(key, value) {
    try {
      const result = await configureNativeAudio(this.mode, { [key]: value });
      this.settings = result.settings;
      this._error = "";
      this.dispatchEvent(new CustomEvent("native-audio-updated", { bubbles: true, composed: true, detail: result }));
    } catch (err) {
      this._error = err.message;
    }
  }

  render() {
    const s = this.settings ?? {};
    const ssb = ["USB", "LSB"].includes(this.demodulator);
    const cw = this.demodulator === "CW";
    const wfm = this.demodulator === "WFM";
    const nfm = this.demodulator === "NFM";
    const agcAvailable = ["AM", "USB", "LSB", "CW"].includes(this.demodulator);
    return html`
      <details class="review-native-audio">
        <summary class="survey-group-label">NATIVE AUDIO & DSP</summary>
        <div class="control-row" style="flex-wrap:wrap;gap:8px;margin-top:8px">
          ${agcAvailable ? html`<label class="dim mono">AGC
            <select @change=${(e) => this._change("agc_mode", e.target.value)}>
              ${["default", "off", "fast", "slow"].map((v) => html`<option value=${v} ?selected=${(s.agc_mode ?? "default") === v}>${v}</option>`)}
            </select>
          </label>` : ""}
          ${agcAvailable && s.agc_mode === "off" ? html`<label class="dim mono">manual gain
            <input type="number" min="0" max="20" step="0.1" style="width:5em" .value=${String(s.manual_gain ?? 1)}
              @change=${(e) => this._change("manual_gain", Number(e.target.value))}>
          </label>` : ""}
          <label class="dim mono"><input type="checkbox" .checked=${!!s.notch_enabled}
            @change=${(e) => this._change("notch_enabled", e.target.checked)}> notch</label>
          <label class="dim mono">notch Hz <input type="number" min="100" max="5000" step="50" style="width:6em"
            .value=${String(s.notch_frequency_hz ?? 1000)}
            @change=${(e) => this._change("notch_frequency_hz", Number(e.target.value))}></label>
          <label class="dim mono"><input type="checkbox" .checked=${!!s.nr_enabled}
            @change=${(e) => this._change("nr_enabled", e.target.checked)}> NR</label>
          <label class="dim mono">strength <input type="range" min="0" max="1" step="0.05"
            .value=${String(s.nr_strength ?? 0.5)}
            @change=${(e) => this._change("nr_strength", Number(e.target.value))}></label>
        </div>
        <details style="margin-top:6px">
          <summary class="dim mono">Advanced native settings</summary>
          <div class="control-row" style="flex-wrap:wrap;gap:8px;margin-top:8px">
            <label class="dim mono">notch width Hz <input type="number" min="10" max="500" step="10" style="width:5em"
              .value=${String(s.notch_width_hz ?? 100)}
              @change=${(e) => this._change("notch_width_hz", Number(e.target.value))}></label>
            ${ssb ? html`
              <label class="dim mono">SSB low Hz <input type="number" min="0" max="3000" step="50" style="width:5em"
                .value=${String(s.ssb_low_hz ?? 0)}
                @change=${(e) => this._change("ssb_low_hz", Number(e.target.value))}></label>
              <label class="dim mono">SSB high Hz <input type="number" min="300" max="10000" step="50" style="width:5em" placeholder="auto"
                .value=${s.ssb_high_hz == null ? "" : String(s.ssb_high_hz)}
                @change=${(e) => this._change("ssb_high_hz", e.target.value === "" ? null : Number(e.target.value))}></label>` : ""}
            ${cw ? html`<label class="dim mono">CW pitch Hz <input type="number" min="300" max="1500" step="25" style="width:5em"
              .value=${String(s.cw_pitch_hz ?? 700)}
              @change=${(e) => this._change("cw_pitch_hz", Number(e.target.value))}></label>` : ""}
            ${wfm ? html`<label class="dim mono">de-emphasis
              <select .value=${String(s.wfm_deemphasis_us ?? 75)} @change=${(e) => this._change("wfm_deemphasis_us", Number(e.target.value))}>
                <option value="0">off</option><option value="50">50 µs</option><option value="75">75 µs</option>
              </select></label>` : ""}
            ${nfm ? html`<label class="dim mono">NFM audio
              <select .value=${s.nfm_audio_mode ?? "voice"} @change=${(e) => this._change("nfm_audio_mode", e.target.value)}>
                <option value="voice">voice</option><option value="flat">flat</option>
              </select></label>` : ""}
            <label class="dim mono"><input type="checkbox" .checked=${!!s.blanker_enabled}
              @change=${(e) => this._change("blanker_enabled", e.target.checked)}> impulse blanker</label>
            <label class="dim mono">blanker threshold <input type="range" min="4" max="20" step="0.5" style="width:8em"
              .value=${String(s.blanker_threshold ?? 8)}
              @change=${(e) => this._change("blanker_threshold", Number(e.target.value))}></label>
          </div>
        </details>
        ${this._error ? html`<p class="status-error mono">${this._error}</p>` : ""}
      </details>
    `;
  }
}

customElements.define("native-audio-dsp", NativeAudioDsp);
