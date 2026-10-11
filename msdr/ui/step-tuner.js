/** Serializes held step controls into bounded receiver tune requests.
 * A burst always starts immediately. While that request is in flight, steps
 * accumulate into one latest target; subsequent requests are spaced by
 * intervalMs and never overlap. */
export class StepTuner {
  constructor(intervalMs = 150) {
    this.intervalMs = intervalMs;
    this.targetHz = null;
    this.sentHz = null;
    this.inFlight = null;
    this.sendTimer = null;
    this.lastStartedAt = 0;
    this.generation = 0;
    this.tune = null;
    this.confirmedAt = null;
  }

  get isSettled() {
    return this.targetHz != null && this.targetHz === this.sentHz && this.inFlight == null;
  }

  step(baseHz, deltaHz, tune) {
    if (!Number.isFinite(baseHz) || !Number.isFinite(deltaHz)) return;
    this.tune = tune;
    if (this.targetHz == null) this.targetHz = baseHz;
    this.targetHz = Math.max(0, this.targetHz + deltaHz);
    this._schedule(tune);
  }

  /** Drop queued work. The returned promise settles after an already-sent
   * request, allowing a manual tune to follow it without overlapping. */
  cancel() {
    this.generation += 1;
    clearTimeout(this.sendTimer);
    this.sendTimer = null;
    this.targetHz = null;
    this.sentHz = null;
    this.confirmedAt = null;
    return this.inFlight ?? Promise.resolve();
  }

  _schedule(tune) {
    if (this.inFlight || this.targetHz === this.sentHz) return;
    clearTimeout(this.sendTimer);
    const wait = Math.max(0, this.lastStartedAt + this.intervalMs - Date.now());
    if (wait === 0) this._send(tune);
    else this.sendTimer = setTimeout(() => {
      this.sendTimer = null;
      this._send(tune);
    }, wait);
  }

  _send(tune) {
    if (this.inFlight || this.targetHz == null || this.targetHz === this.sentHz) return;
    const generation = this.generation;
    const hz = this.targetHz;
    this.sentHz = hz;
    this.lastStartedAt = Date.now();
    const request = Promise.resolve().then(() => tune(hz));
    this.inFlight = request;
    const done = (result) => {
      if (this.inFlight === request) this.inFlight = null;
      if (result === false && generation === this.generation) {
        this.cancel();
        return;
      }
      if (result === true && generation === this.generation) this.confirmedAt = Date.now();
      if (this.targetHz !== this.sentHz) this._schedule(this.tune);
    };
    request.then(done, () => done(false));
  }
}
