# Real-world checklist for decoders

Run through this whenever a decoder is written or changed. A test that uses
the app's own generator only proves the receiver agrees with the generator;
this list is what a real front end, band and transmitter add. Idea borrowed
from OnAir's REAL_WORLD_CHECKLIST.md.

Synthetic results below are **not** off-air validation. Record each item as
tested (synthetic), tested (live), or not tested -- never leave it blank.

## Radio and front-end faults

Covered synthetically by `msdr/validation/impairments.py` and the
"Impairment profiles" table in [scorecard-synthetic.md](scorecard-synthetic.md)
(regenerate with `.venv/bin/python scripts/scorecard.py`).

- [ ] Frequency / ppm error (carrier offset and sample clock): profile `ppm_20`
- [ ] Off-centre tuning: `off_centre`
- [ ] DC spike at the centre: `dc_spike`
- [ ] I/Q swapped or imbalanced: `iq_swap`, `iq_imbalance`
- [ ] Dropped sample blocks (USB loss): `drops`
- [ ] 8-bit ADC: `8bit`
- [ ] Low SNR: `low_snr`
- [ ] NaN/inf or huge values from upstream: `nonfinite`, and
      `tests/decoder/test_fuzz_native.py` (no exception, no hang)
- [ ] Gain extremes: clipped (too hot) and near the noise floor (too cold)
- [ ] Strong adjacent channel / image

Add the decoder to `_IMPAIRMENT_DECODERS` in `msdr/validation/scorecard.py`
and to the fuzz table if it is in-process. A failing cell is a result to
record, not something to tune away.

## Propagation

- [ ] Fading and multipath (amplitude and phase wander mid-frame)
- [ ] Impulse noise and bursts of interference
- [ ] Overlapping or colliding transmissions
- [ ] Weak signal right after a strong one

## Transmitter variations

- [ ] Real transmitters drift, start late, and use odd deviation, baud error
      or timing jitter
- [ ] Optional/extended fields, unknown message types, truncated frames
- [ ] Bad CRC reported as `failed`, not silently dropped or shown as valid
- [ ] Repeats and duplicates handled; no false output on pure noise

## Program behaviour

- [ ] Retune or channel change mid-frame
- [ ] Random start point in the stream (no clean frame boundary)
- [ ] Feed in odd chunk sizes, including 1 sample and odd byte counts
- [ ] Slow CPU / backpressure: decoder falls behind without crashing
- [ ] Stop/start and restart reuse; flush on stop
- [ ] Long soak (see `scripts/soak.py`)

## Before claiming "validated"

Synthetic pass + impairment profiles + fuzz is the floor. Field validation
needs a real off-air capture with an independent reference; see
[operator-checklist.md](operator-checklist.md) and [corpus.md](corpus.md).
