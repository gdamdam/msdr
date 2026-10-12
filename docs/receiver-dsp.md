# Receiver audio and DSP

msdr has two listening paths, each with its own audio controls. This page is for users tuning Listen audio.

| Path | What it does |
|---|---|
| **SDRconnect Audio & DSP** | Changes the running receiver through its WebSocket properties. |
| **Native Audio & DSP** | Processes IQ inside msdr, during recording Review and live Signal workbench inspection. |

The native controls never alter SDRconnect, saved IQ or decoder taps.

## SDRconnect Audio & DSP

Listen shows the confirmed audio AGC, noise-reduction and squelch values.

- Each change goes through the existing restore/confirmation path. msdr reports commands that are pending, rejected or unsupported.
- Sliders commit on release.
- Numeric inputs are subject to SDRconnect's confirmation.
- The [SDRconnect WebSocket specification](https://sdrplay.com/wpyog_document/sdrconnect-websocket-api-specification/) documents noise-reduction strength as a percentage. It gives no numeric range or unit for the AGC and squelch thresholds, so msdr does not claim one.

## Native Audio & DSP controls

| Control | Behavior and default |
|---|---|
| AGC | The default keeps the former response: slow for AM, fast for SSB/CW. Off uses manual gain. Fast and slow are presets. |
| Bandwidth / passband | Mode-specific bandwidth presets retune the virtual channel. USB/LSB low and high edges apply to the sideband filter. A high edge wider than the tuned channel is rejected until bandwidth is widened. Replay span checks include extractor margin. |
| CW pitch | 700 Hz by default, adjustable 300–1500 Hz. |
| Notch | One bypassable audio biquad, 100–5000 Hz center and 10–500 Hz width, crossfaded on changes. Off by default. |
| WFM de-emphasis | 75 µs by default, or 50 µs / off. NFM offers voice (the existing filter and de-emphasis) or flat listening. |
| Noise reduction | Off by default. A half-overlap spectral gate with a robust per-frame noise estimate, a 0.5 minimum amplitude gain, temporal smoothing and a strength of 0–1. Fixed delay is 512 samples (10.7 ms at 48 kHz). It is conservative and not equivalent to SDR# NINR. |
| Impulse blanker | Off by default. After demodulation, isolated audio spikes are replaced by interpolation. It fails open if more than 1% of a block would be blanked. It does not act on IQ or IF. |

### Signal chain

- Effects run after demodulation and before PCM output.
- The NFM flat tap, the RDS multiplex tap and the clean audio decoder tap branch off before the listening effects. When the listening demodulator settings differ, the clean tap uses a separate default demodulator.
- Output stays 48 kHz mono signed 16-bit, with one output sample per input audio sample. Noise reduction adds a reported 10.7 ms listening delay.
- PCM conversion clips to the signed range.
- Retunes, seeks, sample-rate changes and stream gaps rebuild the stateful DSP.

## Validation and limits

- Deterministic fixtures verify filter attenuation, AGC response and bypass, FM de-emphasis, noise reduction without loss of a clean tone, impulse reduction without damage to a clean tone, block continuity and decoder-tap isolation.
- On a 2026-09-28 local 2 MS/s recording, 1.016 s of NFM audio with notch, NR and blanker enabled stayed finite, with a pre-PCM peak of 0.482. This shows processing stability, not listening quality.
- Speech intelligibility and off-air reception quality have not been evaluated by listening.

Timing on an Apple Silicon host, processing 10 s of 48 kHz random audio in 2048-sample blocks (single runs, not guaranteed throughput on other machines):

| Configuration | Time |
|---|---|
| Bypassed | 0.002 s |
| Notch | 0.054 s |
| NR | 0.059 s |
| Blanker | 0.008 s |
| All three | 0.116 s |

See also: [integrations](integrations.md), [decoder expansion](decoder-expansion.md).
