import { LitElement, html } from "../vendor/lit.js";

// Bounded ring: at most this many seconds of audio held client-side. Bigger
// than this and a network hiccup would let latency grow unboundedly instead
// of catching up; the incoming (newest) chunk is dropped when the ring is
// already full, rather than the buffered (oldest) audio, so the ring never
// blocks or grows -- see _pushPcm.
const RING_SECONDS = 1;

/** Inline AudioWorkletProcessor, loaded from a Blob URL so audio-player.js
 * stays a single file with nothing to route through msdr's static server.
 * It owns only a FIFO queue of Float32Array chunks pushed from the main
 * thread (see _feedWorklet) and pulls them out one sample at a time each
 * render quantum, writing silence once the queue runs dry -- silence, never
 * a stall, because the audio thread cannot block on the network. It reports
 * its queued sample count back on a throttled cadence so the main thread can
 * show a buffer-ms readout without being able to read the audio thread's
 * memory directly (there's no SharedArrayBuffer here -- this app ships with
 * no cross-origin-isolation headers). */
const WORKLET_SOURCE = `
class RingProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._offset = 0;
    this._tick = 0;
    this.port.onmessage = (ev) => {
      const msg = ev.data;
      if (msg.type === "push") this._chunks.push(msg.samples);
      else if (msg.type === "flush") { this._chunks = []; this._offset = 0; }
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (out) {
      for (let i = 0; i < out.length; i++) {
        if (!this._chunks.length) { out[i] = 0; continue; }
        const chunk = this._chunks[0];
        out[i] = chunk[this._offset++];
        if (this._offset >= chunk.length) { this._chunks.shift(); this._offset = 0; }
      }
    }
    // Throttled (~every 32 quanta, ~90ms at 48k) -- posting on every call
    // would flood the message port for no benefit to a once-a-frame UI meter.
    if (++this._tick % 32 === 0) {
      let queued = this._offset ? -this._offset : 0;
      for (const c of this._chunks) queued += c.length;
      this.port.postMessage({ type: "level", queued });
    }
    return true;
  }
}
registerProcessor("ring-processor", RingProcessor);
`;

/** <audio-player>: plays the selected virtual receiver only while `.active`.
 * Review defaults to its replay-only /ws/audio endpoint. Live Scope explicitly
 * selects /ws/live-audio, whose receiver must be started separately.
 *
 * Autoplay policy: browsers refuse to start an AudioContext without a user
 * gesture, so nothing happens until the operator presses PLAY AUDIO --
 * that click is the gesture, and is also the only place the socket opens. */
export class AudioPlayer extends LitElement {
  static properties = {
    endpoint: { type: String },
    active: { type: Boolean }, // selected receiver running (server-side)
    rate: { type: Number }, // hint for the initial AudioContext sample rate
    volume: { type: Number }, // 0..1, mirrored from review-view's slider
    muted: { type: Boolean }, // mirrored from review-view's MUTE
    _playing: { state: true },
    _bufferMs: { state: true },
    _dropped: { state: true },
    _unavailable: { state: true },
    _error: { state: true },
  };

  createRenderRoot() {
    return this;
  }

  constructor() {
    super();
    this.endpoint = "/ws/audio";
    this.active = false;
    this.rate = 48000;
    this.volume = 1;
    this.muted = false;
    this._playing = false;
    this._starting = false;
    this._playGen = 0;
    this._bufferMs = 0;
    this._dropped = 0;
    this._unavailable = null;
    this._error = null;
    this._ctx = null;
    this._gain = null;
    this._node = null; // AudioWorkletNode or ScriptProcessorNode
    this._usingWorklet = false;
    this._socket = null;
    this._pcmRate = null; // rate actually carried by the WS frame headers
    this._pendingSamples = 0; // approximate ring fill (worklet path)
    this._fallbackChunks = []; // ring queue for the ScriptProcessor fallback
    this._fallbackOffset = 0;
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stop();
  }

  updated(changed) {
    if (changed.has("active") && !this.active && this._playing) this._stop();
    if (changed.has("endpoint") && this._playing) this._stop();
    if ((changed.has("volume") || changed.has("muted")) && this._gain) {
      this._gain.gain.value = this.muted ? 0 : this.volume;
    }
  }

  async _play() {
    // _starting covers the await below: a double-click must not build a second
    // context/socket before the first sets _playing.
    if (!this.active || this._playing || this._starting) return;
    this._starting = true;
    const gen = this._playGen;
    this._error = null;
    this._unavailable = null;
    try {
      // Request the context at the PCM's own rate (best guess from `.rate`
      // until the first frame header confirms it) so samples play back
      // directly with no resampling step -- a mismatched context rate would
      // otherwise play the audio at the wrong pitch/speed.
      this._ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: this.rate || 48000 });
      this._gain = this._ctx.createGain();
      this._gain.gain.value = this.muted ? 0 : this.volume;
      this._gain.connect(this._ctx.destination);
      await this._setupNode();
      // _stop() ran while we awaited (STOP click / active=false): it already
      // tore the context down, so don't open a socket for a dead session.
      if (gen !== this._playGen) return;
      this._openSocket();
      this._playing = true;
    } catch (err) {
      if (gen !== this._playGen) return; // failure is just the aborted start
      this._error = err.message;
      this._teardownAudio();
    } finally {
      this._starting = false;
    }
  }

  async _setupNode() {
    if (this._ctx.audioWorklet) {
      const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
      const url = URL.createObjectURL(blob);
      try {
        await this._ctx.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      this._usingWorklet = true;
      this._node = new AudioWorkletNode(this._ctx, "ring-processor", { outputChannelCount: [1] });
      this._node.port.onmessage = (ev) => {
        if (ev.data.type === "level") {
          this._pendingSamples = Math.max(0, ev.data.queued);
          this._bufferMs = Math.round((this._pendingSamples / (this._pcmRate || this.rate || 48000)) * 1000);
        }
      };
    } else {
      // Fallback for browsers without AudioWorklet: ScriptProcessorNode runs
      // its callback on the main thread, so the ring is just a plain array
      // read directly -- no postMessage round trip needed here.
      this._usingWorklet = false;
      this._node = this._ctx.createScriptProcessor(2048, 0, 1);
      this._node.onaudioprocess = (ev) => {
        const out = ev.outputBuffer.getChannelData(0);
        for (let i = 0; i < out.length; i++) {
          if (!this._fallbackChunks.length) {
            out[i] = 0;
            continue;
          }
          const chunk = this._fallbackChunks[0];
          out[i] = chunk[this._fallbackOffset++];
          if (this._fallbackOffset >= chunk.length) {
            this._fallbackChunks.shift();
            this._fallbackOffset = 0;
          }
        }
        let queued = this._fallbackOffset ? -this._fallbackOffset : 0;
        for (const c of this._fallbackChunks) queued += c.length;
        this._pendingSamples = Math.max(0, queued);
        this._bufferMs = Math.round((this._pendingSamples / (this._pcmRate || this.rate || 48000)) * 1000);
      };
    }
    this._node.connect(this._gain);
  }

  _openSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${proto}//${location.host}${this.endpoint}`);
    socket.binaryType = "arraybuffer";
    this._socket = socket;
    socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "start" })));
    socket.addEventListener("message", (ev) => this._onMessage(ev));
    socket.addEventListener("close", () => {
      // Tear down too: otherwise the next PLAY overwrites this._ctx and leaks
      // the old context (browsers cap live AudioContexts).
      if (this._socket === socket) this._stop();
    });
    socket.addEventListener("error", () => socket.close());
  }

  _onMessage(ev) {
    if (typeof ev.data === "string") {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "flush") {
        // A seek or generation bump: whatever's still queued is audio from
        // before the jump -- drop it rather than let it play out of order.
        this._flushRing();
      } else if (msg.type === "unavailable") {
        this._unavailable = msg.reason;
      }
      return;
    }
    this._unavailable = null;
    this._handleBinary(ev.data);
  }

  _handleBinary(buffer) {
    if (buffer.byteLength < 12) return; // short/malformed frame -- ignore
    const view = new DataView(buffer);
    const rate = view.getInt32(8, true);
    if (rate > 0) this._pcmRate = rate;
    const pcm = new Int16Array(buffer, 12);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
    this._pushPcm(samples);
  }

  /** Enforces the <=1s ring bound: if the queue is already at/over capacity,
   * the *new* chunk is dropped (counted) instead of evicting older audio --
   * evicting older audio from an AudioWorkletNode's queue would need another
   * message round trip and could reorder samples; dropping the newest is
   * simple and keeps playback strictly in order. */
  _pushPcm(samples) {
    const rate = this._pcmRate || this.rate || 48000;
    const capacity = rate * RING_SECONDS;
    if (this._pendingSamples + samples.length > capacity) {
      this._dropped += 1;
      return;
    }
    this._pendingSamples += samples.length;
    if (this._usingWorklet) {
      this._node.port.postMessage({ type: "push", samples }, [samples.buffer]);
    } else {
      this._fallbackChunks.push(samples);
    }
  }

  _flushRing() {
    this._pendingSamples = 0;
    this._bufferMs = 0;
    if (this._usingWorklet) {
      this._node.port.postMessage({ type: "flush" });
    } else {
      this._fallbackChunks = [];
      this._fallbackOffset = 0;
    }
  }

  _stop() {
    this._playGen += 1; // aborts a _play() still awaiting _setupNode()
    if (this._socket) {
      this._socket.close();
      this._socket = null;
    }
    this._teardownAudio();
    this._playing = false;
    this._bufferMs = 0;
    this._pendingSamples = 0;
  }

  _teardownAudio() {
    if (this._node) {
      try {
        this._node.disconnect();
      } catch {
        // already disconnected
      }
      this._node = null;
    }
    if (this._ctx) {
      // suspend rather than close: STOP is meant to be resumable by pressing
      // PLAY AUDIO again without rebuilding the whole graph, but _play()
      // currently always builds a fresh context -- suspend still guarantees
      // no audio keeps playing after STOP even if that changes later.
      this._ctx.suspend().catch(() => {});
      this._ctx.close().catch(() => {});
      this._ctx = null;
      this._gain = null;
    }
    this._fallbackChunks = [];
    this._fallbackOffset = 0;
  }

  render() {
    return html`
      <div class="audio-player" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        ${!this._playing
          ? html`<button @click=${() => this._play()} ?disabled=${!this.active}>▶ PLAY AUDIO</button>`
          : html`<button @click=${() => this._stop()}>⏹ STOP</button>`}
        ${this._playing
          ? html`<span class="dim mono" style="font-size:0.8em"
              >buffer ${this._bufferMs} ms${this._dropped ? html` · dropped ${this._dropped}` : ""}</span
            >`
          : ""}
        ${this._unavailable
          ? html`<span class="dim mono" style="font-size:0.8em;color:var(--danger)">audio unavailable: ${this._unavailable}</span>`
          : ""}
        ${this._error ? html`<span class="dim mono" style="font-size:0.8em;color:var(--danger)">${this._error}</span>` : ""}
      </div>
    `;
  }
}

customElements.define("audio-player", AudioPlayer);
