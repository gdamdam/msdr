# Receiver audio and DSP

Reference for the two listening paths and their audio controls. For users tuning Listen audio.

| Path | What it does |
|---|---|
| **SDRconnect Audio & DSP** | Changes the running receiver through its WebSocket properties. |
| **Native Audio & DSP** | Processes IQ inside msdr during recording Review and live Signal workbench inspection. |

The native controls never alter SDRconnect, saved IQ, or decoder taps.

## SDRconnect

- Listen shows confirmed audio AGC, noise-reduction and squelch values.
- The backend sends each change through the existing restore/confirmation path and reports pending, rejected or unsupported commands. Sliders commit on release.
- The [SDRconnect WebSocket specification](https://sdrplay.com/wpyog_document/sdrconnect-websocket-api-specification/) documents noise-reduction strength as a percentage; it does not state a numeric range or unit for AGC and squelch thresholds, so msdr does not claim one.
- Their numeric inputs are subject to SDRconnect's confirmation.

## Native receiver

| Control | Behavior and default |
|---|---|
| AGC | Default retains the former AM slow and SSB/CW fast response. Off uses manual gain; fast and slow are presets. |
| Bandwidth/passband | Mode-specific bandwidth presets retune the virtual channel. USB/LSB low and high edges are applied to the sideband filter; a high edge wider than the tuned channel is rejected until bandwidth is widened. Replay span checks include extractor margin. |
| CW pitch | 700 Hz by default; adjustable 300–1500 Hz. |
| Notch | One bypassable audio biquad, 100–5000 Hz center and 10–500 Hz width, crossfaded on changes. Off by default. |
| WFM de-emphasis | 75 µs by default, or 50 µs/off. NFM has voice (existing filter/de-emphasis) and flat listening choices. |
| Noise reduction | Off by default. A half-overlap spectral gate uses a robust per-frame noise estimate, a 0.5 minimum amplitude gain, temporal smoothing and a strength of 0–1. Its fixed delay is 512 samples (10.7 ms at 48 kHz). It is conservative, not equivalent to SDR# NINR. |
| Impulse blanker | Off by default. After demodulation, isolated audio spikes are replaced by interpolation. It fails open if more than 1% of a block would be blanked. It does not act on IQ or IF. |

Signal chain:

- Effects are after demodulation and before PCM output.
- The existing NFM flat tap, RDS multiplex tap and clean audio decoder tap branch before listening effects; when listening demodulator settings differ, the clean tap uses a separate default demodulator.
- The audio output remains 48 kHz mono signed 16-bit with one output sample per input audio sample; NR adds a reported 10.7 ms listening delay.
- PCM conversion clips to the signed range.
- Retunes, seeks, sample-rate changes and stream gaps rebuild the stateful DSP.

## Validation and limits

- Deterministic fixtures verify filter attenuation, AGC response/bypass, FM de-emphasis, noise reduction without loss of a clean tone, impulse reduction without damage to a clean tone, block continuity and decoder-tap isolation.
- On a 2026-09-28 local 2 MS/s recording, 1.016 s of NFM audio with notch, NR and blanker enabled stayed finite; the pre-PCM peak was 0.482. This establishes processing stability, not listening quality.
- Speech intelligibility and off-air reception quality have not been evaluated by listening.

On this Apple Silicon host, processing 10 s of 48 kHz random audio in 2048-sample blocks took:

| Configuration | Time |
|---|---|
| Bypassed | 0.002 s |
| Notch | 0.054 s |
| NR | 0.059 s |
| Blanker | 0.008 s |
| All three | 0.116 s |

These are single-run measurements, not guaranteed throughput on other machines.

See also: [integrations](integrations.md), [decoder expansion](decoder-expansion.md).
