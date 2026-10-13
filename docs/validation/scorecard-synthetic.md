# Corpus scorecard (synthetic fixtures)

**Synthetic fixtures only -- not a claim of field accuracy.** Every case
below is generated in code (`msdr.validation.fixtures`); none of it is a
captured off-air recording, and a pass here says only that msdr's real
analyzer/decoder code paths behave correctly on a *known* synthetic signal.
Field validation (a real off-air capture) is tracked separately --
see `docs/validation/corpus.md` and `tests/corpus/manifest.json`.

Generated: 2026-10-13T14:58:32.459964+00:00

## Detectors

| case | detector | outcome | detail |
|---|---|---|---|
| NFM voice-like (speech-shaped noise) | modulation | correct | fm_fsk |
| NFM voice-like (speech-shaped noise) | voice | correct | voice |
| keyed carrier (OOK, non-Morse timing) | family | correct | family='keyed carrier (unknown code)' fits_cw=False |
| AM tone | modulation | correct | am |
| USB tone | modulation | abstained | unknown |
| LSB tone | modulation | abstained | unknown |
| CW (proper Morse timing) | family | correct | family='CW' fits_cw=True |
| white noise (negative control) | voice | correct | data |
| white noise (negative control) | family | correct | family=None |
| adjacent strong channel (negative control, detector-only) | family | correct | family=None |

## Decoders

| case | decoder | status | valid | invalid | unknown | payload match | false output | detail |
|---|---|---|---|---|---|---|---|---|
| AFSK-1200 AX.25 (packet/APRS) | aprs | available | 1 | 0 | 0 | True | 0 | 1 event(s) via multimon |
| CW (proper Morse timing) | cw | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| WFM + RDS (pilot) | rds | available | 1 | 0 | 0 | True | 0 | pi=0x54A8 ps='TESTFM' groups=34 |
| WFM + RDS (pilotless) | rds | available | 1 | 0 | 0 | True | 0 | pi=0x54A8 ps='TESTFM' groups=23 |
| GMSK 9600 AIS burst | ais | available | 1 | 0 | 0 | True | 0 | 1 event(s), stats={'preambles': 1, 'frames_ok': 1, 'crc_fail': 0, 'framing_fail': 0, 'no_mmsi': 0} |
| white noise (negative control) | aprs | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| white noise (negative control) | cw | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| white noise (negative control) | ais | available | 0 | 0 | 0 | None | 0 | {'preambles': 0, 'frames_ok': 0, 'crc_fail': 0, 'framing_fail': 0, 'no_mmsi': 0} |
| single steady tone (negative control) | aprs | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| single steady tone (negative control) | cw | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| single steady tone (negative control) | ais | available | 0 | 0 | 0 | None | 0 | {'preambles': 0, 'frames_ok': 0, 'crc_fail': 0, 'framing_fail': 0, 'no_mmsi': 0} |
| clipped IQ / hard-limited (negative control) | aprs | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| clipped IQ / hard-limited (negative control) | cw | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| clipped IQ / hard-limited (negative control) | ais | available | 0 | 0 | 0 | None | 0 | {'preambles': 0, 'frames_ok': 0, 'crc_fail': 0, 'framing_fail': 0, 'no_mmsi': 0} |
| adjacent strong channel (negative control, detector-only) | aprs | not_applicable | 0 | 0 | 0 | None | 0 |  |
| adjacent strong channel (negative control, detector-only) | cw | not_applicable | 0 | 0 | 0 | None | 0 |  |
| adjacent strong channel (negative control, detector-only) | ais | not_applicable | 0 | 0 | 0 | None | 0 |  |
| dropped blocks (negative control) | aprs | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| dropped blocks (negative control) | cw | available | 0 | 0 | 0 | None | 0 | 0 event(s) via multimon |
| dropped blocks (negative control) | ais | available | 0 | 0 | 0 | None | 0 | {'preambles': 46, 'frames_ok': 0, 'crc_fail': 0, 'framing_fail': 46, 'no_mmsi': 0} |

## Negative controls

| case | decoder events | protocol/station claim |
|---|---|---|
| white noise (negative control) | 0 | False |
| single steady tone (negative control) | 0 | False |
| clipped IQ / hard-limited (negative control) | 0 | False |
| adjacent strong channel (negative control, detector-only) | 0 | False |
| dropped blocks (negative control) | 0 | False |

## Impairment profiles (synthetic)

Each decoder's known synthetic burst is degraded by `msdr.validation.impairments` and decoded; pass = the expected payload is recovered with a passed integrity check. `n/a` = profile not expressible for that decoder's input. Failures are reported as found, not tuned away. Synthetic only -- not off-air validation.

| decoder | clean | ppm_20 | off_centre | dc_spike | iq_swap | iq_imbalance | drops | 8bit | low_snr | nonfinite |
|---|---|---|---|---|---|---|---|---|---|---|
| ais | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass |
| adsb | pass | pass | pass | pass | pass | pass | FAIL | pass | pass | n/a |
| rs41 | pass | pass | pass | pass | pass | pass | FAIL | pass | pass | n/a |

Failing cells:

- adsb / drops: 0 event(s), error=None
- rs41 / drops: 0 event(s)
