import { LitElement, html } from '../vendor/lit.js';
import * as api from '../api.js';

const names = {device_vfo_frequency:'Frequency', device_center_frequency:'IQ center',
  device_sample_rate:'IQ sample rate', demodulator:'Listening mode', filter_bandwidth:'Audio filter',
  squelch_enable:'Squelch', noise_reduction_enable:'Noise reduction', lna_state:'LNA state',
  lora_spreading_factor:'LoRa spreading factor', lora_bandwidth_hz:'LoRa bandwidth',
  lora_coding_rate:'LoRa coding rate', lora_protocol:'LoRa protocol'};
const display = (key, value) => key.includes('frequency') ? `${value / 1e6} MHz` :
  key === 'device_sample_rate' ? `${value / 1e6} MS/s` : key === 'filter_bandwidth' ? `${value / 1000} kHz` :
  key === 'lora_bandwidth_hz' ? `${value / 1000} kHz` :
  key === 'lora_coding_rate' ? `4/${4 + value}` :
  typeof value === 'boolean' ? (value ? 'On' : 'Off') : value;

export class DecoderPrepare extends LitElement {
  static properties = {
    decoder: {type: String}, frequencyHz: {type: Number}, restoreToken: {type: String},
    _frequency: {state: true}, _gain: {state: true}, _profile: {state: true}, _preview: {state: true},
    _configured: {state: true}, _message: {state: true}, _busy: {state: true},
  };
  createRenderRoot() { return this; }
  constructor() { super(); this._gain = ''; this._profile = ''; this._message = ''; this._busy = false; }
  willUpdate(changed) {
    if (changed.has('decoder')) {
      const hz = this.decoder === 'adsb' ? 1090000000 : this.frequencyHz;
      this._frequency = Number.isFinite(hz) ? (hz / 1e6).toString() : '';
      this._profile = '';
      this._message = '';
      this._preview = null;
    }
  }
  updated(changed) {
    if (changed.has('decoder') && this.decoder === 'lora') {
      this._profileEdited = false;
      this._loadConfigured();
    }
  }
  async _loadConfigured() {
    try {
      const {decoder: d} = await api.getSettings();
      if (this.decoder !== 'lora') return;
      this._configured = `${d.lora_protocol === 'meshcore' ? 'MeshCore' : d.lora_protocol === 'meshtastic' ? 'Meshtastic' : 'Raw LoRa'} · SF${d.lora_spreading_factor} / ${d.lora_bandwidth_hz / 1000} kHz / 4/${4 + d.lora_coding_rate}`;
      if (!this._profileEdited) {
        this._profile = d.lora_protocol === 'meshcore' && d.lora_spreading_factor === 7 && d.lora_bandwidth_hz === 62500 && d.lora_coding_rate === 1 && this.frequencyHz === 910525000 ? 'meshcore_us'
          : d.lora_protocol === 'meshtastic' && d.lora_spreading_factor === 11 && d.lora_bandwidth_hz === 250000 && d.lora_coding_rate === 1 && this.frequencyHz === 906875000 ? 'meshtastic_us_longfast' : '';
      }
    } catch (e) { this._message = `Cannot read current LoRa settings: ${e.message}`; }
  }
  async _run(apply = false) {
    this._busy = true; this._message = '';
    try {
      const hz = Number(this._frequency) * 1e6;
      if (!Number.isFinite(hz) || hz <= 0) throw new Error('Enter a frequency in MHz');
      const gain = this._gain === '' ? null : Number(this._gain);
      if (gain !== null && (!Number.isInteger(gain) || gain < 0 || gain > 59)) throw new Error('Enter a valid integer LNA state');
      const result = await api.prepareDecoderReceiver({ decoder: this.decoder,
        frequency_hz: Math.round(hz), lna_state: gain,
        profile: this._profile || null,
        token: apply ? this._preview?.token : null });
      this._preview = result;
      if (result.applied) {
        if (this.decoder === 'lora') this._loadConfigured();
        this.restoreToken = result.token;
        this.dispatchEvent(new CustomEvent('decoder-preset-applied', {bubbles:true, composed:true, detail:{token:result.token}}));
        this._message = result.report.status === 'confirmed'
          ? 'Receiver ready. Press START to decode; reception is not guaranteed.'
          : `Preparation ${result.report.status}: ${result.report.detail || 'Check receiver settings; Restore remains available.'}`;
      }
    } catch(e) { this._message = e.message; this._preview = null; }
    finally { this._busy = false; }
  }
  async _restore() {
    this._busy = true;
    try {
      const result = await api.restoreDecoderReceiver(this.restoreToken);
      this._message = `Receiver restore: ${result.status}`;
      if (result.status === 'confirmed') {
        this.dispatchEvent(new CustomEvent('decoder-preset-restored', {bubbles:true, composed:true, detail:{token:this.restoreToken}}));
        this.restoreToken = null; this._preview = null;
        if (this.decoder === 'lora') { this._profileEdited = false; this._loadConfigured(); }
      }
    } catch(e) { this._message = e.message; }
    finally { this._busy = false; }
  }
  render() {
    const p = this._preview;
    return html`<section aria-label="Prepare decoder receiver" style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
      <strong>${this.decoder === 'lora' ? 'LoRa / mesh profile' : 'Prepare receiver'}</strong>
      <p class="dim">${this.decoder === 'lora' ? 'MeshCore and Meshtastic use different radio settings and packet formats. Choose a profile, then preview and apply it before starting.' : 'Choose one decoder. Preview tuning before applying. Antenna stays unchanged.'}</p>
      ${this.decoder === 'lora' && this._configured ? html`<p class="dim" aria-label="Configured LoRa settings">Configured: ${this._configured}</p>` : ''}
      ${this.decoder === 'lora' ? html`<label>LoRa profile<select aria-label="LoRa receiver profile"
        .value=${this._profile} @change=${e=>{this._profileEdited=true;this._profile=e.target.value;this._frequency=this._profile==='meshcore_us'?'910.525':this._profile==='meshtastic_us_longfast'?'906.875':Number.isFinite(this.frequencyHz)?(this.frequencyHz/1e6).toString():'';this._preview=null;this._message='';}}>
        <option value="">Raw LoRa / custom · keep configured PHY</option>
        <option value="meshcore_us">MeshCore US · SF7 / 62.5 kHz / 4/5</option>
        <option value="meshtastic_us_longfast">Meshtastic US LongFast · SF11 / 250 kHz / 4/5</option>
      </select></label>` : ''}
      <label>Frequency (MHz) <input aria-label="Decoder preset frequency MHz" type="number" step="any" ?readOnly=${!!this._profile} .value=${this._frequency ?? ''}
        @input=${e=>{this._frequency=e.target.value;this._preview=null;this._message='';}}></label>
      <label>Optional LNA state <input aria-label="Decoder preset LNA state" type="number" min="0" max="59" placeholder="keep current" .value=${this._gain}
        @input=${e=>{this._gain=e.target.value;this._preview=null;this._message='';}}></label>
      <p class="dim">Gain states depend on the receiver, not dB. Leave blank to preserve the current gain.</p>
      <button ?disabled=${this._busy || !this.decoder} @click=${()=>this._run()}>Preview receiver preset</button>
      ${this.restoreToken ? html`<button ?disabled=${this._busy} @click=${()=>this._restore()}>Restore previous receiver settings</button>` : ''}
      ${p ? html`<p>${p.label}</p><ul>${p.changes.map(c=>html`<li>${names[c.property] ?? c.property}: ${display(c.property,c.before)} → ${display(c.property,c.after)}</li>`)}</ul>
        ${p.decoder_settings_changes?.length ? html`<ul aria-label="Decoder profile changes">${p.decoder_settings_changes.map(c=>html`<li>${names[c.property] ?? c.property}: ${display(c.property,c.before)} → ${display(c.property,c.after)}</li>`)}</ul>` : ''}
        ${p.notes.map(n=>html`<p class="dim">${n}</p>`)}
        ${p.conflicts.length ? html`<p role="status">Stop first: ${p.conflicts.join(', ')}. Then refresh the preview.</p>` : ''}
        <button ?disabled=${this._busy || p.applied || !!p.conflicts.length} @click=${()=>this._run(true)}>Apply receiver preset</button>` : ''}
      ${this._message ? html`<p role="status">${this._message}</p>` : ''}
    </section>`;
  }
}
customElements.define('decoder-prepare', DecoderPrepare);
