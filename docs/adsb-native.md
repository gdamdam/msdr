# Native ADS-B / Mode S receiver

msdr's built-in ADS-B / Mode S receiver is the default backend for the `adsb` decoder. It is pure numpy and needs no FlightAware `dump1090-fa`. This page covers what it decodes, how it is built and how far it has been validated.

`dump1090-fa` stays available as an alternative backend (`standalone_adsb.py`, `backend="standalone_adsb"` in `registry.py`). There is currently no live runtime toggle between the two, the same as for LoRa's `GrLoRaBackend` cross-check.

## What it decodes

- **Decoded:** DF17/18 (ADS-B extended squitter: identification/category, airborne position with global CPR, airborne velocity) and DF11 (all-call reply, ICAO address only).
- **Not decoded:** other Mode S downlink formats (TCAS RA, Comm-A/B, altitude/identity replies DF4/5/20/21, surface position), Gillham-coded (Q=0) legacy altitude, and surface movement.

A frame is accepted only on an exact CRC-24 match. A corrupted frame is rejected, never corrected.

## Validation summary

The validation gate passed on 2026-10-03:

- 4 of 4 published reference frames decode bit-exact with valid CRC-24 (identification, airborne position pair, velocity).
- A single-bit corruption is rejected, never corrected.
- Real-time factor is 0.016 at 2 MS/s.
- SNR sweep, message success rate at 6/10/14/18/22/26 dB:

| Format | 6 | 10 | 14 | 18 | 22 | 26 dB |
|---|---|---|---|---|---|---|
| DF17 | 0 % | 64 % | 100 % | 100 % | 100 % | 100 % |
| DF11 | 0 % | 81 % | 100 % | 100 % | 100 % | 100 % |

The SNR and throughput results are synthetic. They are a regression bar for this receiver, not a claim of parity with `dump1090-fa` or of field performance, and no off-air ADS-B capture has been validated against it. Live results are in [validation/live-vhf-uhf-2026-10-03.md](validation/live-vhf-uhf-2026-10-03.md).

## Modules

### `msdr/decoder/pydecoders/adsb_phy.py`

Pure numpy, no asyncio. It contains:

- `crc24_remainder`.
- Vectorized preamble and PPM demodulation (`AdsbReceiver.feed`).
- Per-message-type field decoding: `decode_identification`, `decode_airborne_position`, `decode_airborne_velocity`.
- Global CPR resolution: `cpr_nl`, `cpr_resolve_global`, `CprTracker`.

`AdsbReceiver(correlation_threshold=2.0, samples_per_chip=1)` exposes `feed(complex64 IQ, now=...) -> list[AdsbMessage]`. It streams with a bounded internal tail buffer.

### `msdr/decoder/pydecoders/adsb.py`

`NativeADSBBackend` keeps `StandaloneADSBBackend`'s constructor shape, `start`/`feed`/`stop`/`events` behaviour and `DecodeEvent` field schema. The SBS-style field names are the same ones `standalone_adsb.py`'s `parse_sbs_line` emits: `icao`, `callsign`, `altitude_ft`, `latitude`/`longitude`, `ground_speed_kt`, `track_deg`, `vertical_rate_fpm`, `transmission_type`. It runs `AdsbReceiver` instead of piping IQ to a `dump1090-fa` subprocess. `backend_name = "msdr-native"`.

Sample-rate handling:

- At exactly 2 MS/s input, samples go straight to `AdsbReceiver` (1 sample per chip, SDRangel's native case).
- At any other input rate (for example msdr's 2.4 MS/s default), a windowed-sinc resampler converts to **4 MS/s (2 samples per chip)**, not 2 MS/s. It has the same structure as `standalone_adsb.py`'s `_StreamingIQResampler`.
- Decimating straight to 1 sample per chip fixes the output grid's phase, which can land squarely on a chip transition. This measured about 15 % raw bit error rate, with every frame's CRC failing. Oversampling to 2 samples per chip and summing both (`AdsbReceiver(samples_per_chip=2)`, the same `samplesPerChip` that SDRangel's correlator and slicer sum over) recovers it. The cost is about twice the native receiver's CPU, still well inside the real-time budget.

### Other files

- `msdr/decoder/standalone_adsb.py` is unchanged and remains the alternative `dump1090-fa` backend.
- In `msdr/decoder/readiness.py`, `adsb` is `builtin`. Its `install_hint` points at the `dump1090-fa` alternative.

## Validation details

### 1. Bit-level truth

Published Mode S / ADS-B frames from "The 1090 MHz Riddle" (MIT-licensed) decode correctly through the full preamble and PPM demodulation pipeline, not just the field decoder, with CRC-24 validating them. These checks are independent of this port's own code.

| Frame | Kind | Result |
|---|---|---|
| `8D4840D6202CC371C32CE0576098` | identification | callsign `KLM1023` |
| `8D40621D58C382D690C8AC2863A7` (even) and `8D40621D58C386435CC412692AD6` (odd) | airborne position pair | global CPR resolves to lat `52.25720214843750`, lon `3.91937255859375` (matches the published worked example's `Lat_EVEN`); altitude 38,000 ft on both |
| `8D485020994409940838175B284F` | velocity | ground speed 159.2 kt, track 182.88 deg, vertical rate -832 ft/min |

A single flipped bit in any of these changes the CRC-24 and is rejected (`tests/decoder/test_adsb_native.py::test_crc24_rejects_single_bit_corruption`), including through the full demodulation pipeline on synthesized IQ. This matches SDRangel's policy (see Provenance).

### 2. Synthetic IQ

`tests/decoder/test_adsb_native.py` uses rectangular-pulse PPM bursts of the frames above with AWGN and randomized lead/trail timing.

- **2 MS/s, 100 trials per point.** Results are in the table above. The DF11 frames use a synthetic ICAO/CA, with the CRC computed by the same code under test.
- **Why SNR points differ from AIS and LoRa.** Unlike AIS/LoRa's matched-filter receivers, this slicer compares 1 sample per chip with no averaging. Per-bit error therefore falls off slowly with SNR, and one wrong bit anywhere in 56/112 bits fails the exact-match CRC (about 0.35 % per-bit error at 10 dB by this test's SNR definition). These SNR points are not directly comparable to AIS's or LoRa's.
- **2.4 MS/s through the resampler.** Via `NativeADSBBackend`, the velocity frame decodes correctly end to end, including `DecodeEvent` field population.
- **Overlapping bursts.** Two different frames a few samples apart both decode (`test_overlapping_close_bursts_both_decode`).
- **DF18.** It shares the DF17 code path (both land in the "extended squitter" branch). It is exercised by the CRC and field-decode unit tests but not by a dedicated synthetic-IQ sweep.

### 3. Throughput

The real-time factor is 0.016 at 2 MS/s on the authoring Mac (50 synthetic bursts per second against a noise floor, 1 s of IQ, `test_realtime_factor`). The gate was 0.5.

## Later reception fixes

- Chip-energy sums use a float64 accumulator, so strong bursts do not erase later weak bursts through float32 rounding.
- Resampling limits the low-pass cutoff to the lower input/output Nyquist limit, including the 2.4-to-4 MS/s interpolation path.
- Successful DF11 replies suppress candidates only for their actual 56-bit duration, not the 112-bit extended-squitter duration.

Regression tests cover:

- Signed-16-bit strong/weak bursts at 4/6/8 MS/s.
- 100 randomized timing/carrier-phase bursts at 2.4 MS/s, both whole and chunked.
- Short replies followed by short/long frames at 2/6 MS/s.

These are synthetic checks, and no new live comparison with `dump1090` was made. The supported message formats and the lack of a runtime backend toggle are unchanged.

## Provenance and licence

- **Ported from SDRangel.** Preamble chip correlation, PPM bit-slicing and the CRC-24/ICAO address-recovery scheme come from SDRangel's ADS-B demodulator, `plugins/channelrx/demodadsb/adsbdemodsinkworker.cpp` and `adsb.h` at commit `030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later, Copyright (C) 2020 Jon Beniston, M7RCE). Files consulted:
  - `adsbdemodsinkworker.cpp/.h`: preamble correlation, PPM slicing, CRC check, ICAO recovery.
  - `adsbdemodsink.cpp/.h`: confirms the correlator and slicer run on power (`I^2+Q^2`), not amplitude or dB.
  - `adsb.h`: preamble, chip and bit constants.
  - `adsbdemod.cpp`: message handling, read for scope only. Its GUI, map and feed code is not ported.
- **Error correction.** SDRangel's ADS-B demod does not implement single-bit CRC-24 correction. DF17/18 extended squitter frames are accepted only on an exact CRC match (`adsbdemodsinkworker.cpp` around line 434: `if (parity == m_crc.get())`). Other Mode S downlink formats recover the ICAO address by XORing the parity field with the computed CRC remainder (around line 530: `icao = (parity ^ crc) & 0xffffff`). That is address recovery, not error correction, and it is also exact-match only. `msdr/decoder/pydecoders/adsb_phy.py` ports the same policy. dump1090's optional brute-force bit-flip search is not ported.
- **Message formats.** The CRC-24 generator polynomial (`0xFFF409`) and the per-message-type field decoding (identification 6-bit charset, airborne position CPR, airborne velocity) are not SDRangel-specific. They follow the public ICAO Annex 10 Volume IV Mode S specification, as documented in "The 1090 MHz Riddle" by Junzi Sun (mode-s.org, MIT-licensed) and the same author's pyModeS. The implementation was checked bit-exactly against that book's worked examples.
- **Licence.** msdr is GPL-3.0-only, which may incorporate GPL-3.0-or-later code. New files carry `SPDX-License-Identifier: GPL-3.0-or-later` (as for the existing AIS/DSC/LoRa ports) and name the upstream files and commit in their module docstring. THIRD_PARTY_NOTICES.md and the README credits list SDRangel's ADS-B demod.

See also: [lora-native.md](lora-native.md), [decoder-expansion.md](decoder-expansion.md).
