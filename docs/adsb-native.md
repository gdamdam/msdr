# Native ADS-B / Mode S receiver (no dump1090-fa)

Pure-numpy ADS-B / Mode S receiver, the default backend for the `adsb` decoder; it replaces the FlightAware `dump1090-fa` requirement. For maintainers and anyone checking how far the decoder has been validated.

Status: 0.62.0 (same release as docs/lora-native.md). Gate passed 2026-10-03:

- 4/4 published reference frames bit-exact with valid CRC-24 (identification, airborne position pair, velocity).
- Single-bit corruption rejected, never corrected.
- Real-time factor 0.016 at 2 MS/s.
- SNR sweep, message success at 6/10/14/18/22/26 dB:

| Format | 6 | 10 | 14 | 18 | 22 | 26 dB |
|---|---|---|---|---|---|---|
| DF17 | 0 % | 64 % | 100 % | 100 % | 100 % | 100 % |
| DF11 | 0 % | 81 % | 100 % | 100 % | 100 % | 100 % |

`dump1090-fa` stays available as an alternative backend (`standalone_adsb.py`), not currently wired to a live runtime toggle (same precedent as LoRa's `GrLoRaBackend` cross-check).

## Reception fixes in 0.67.1

- Chip-energy sums use a float64 accumulator so strong bursts do not erase
  later weak bursts through float32 rounding.
- Resampling limits the low-pass cutoff to the lower input/output Nyquist
  limit, including the 2.4-to-4 MS/s interpolation path.
- Successful DF11 replies suppress candidates only for their actual 56-bit
  duration, rather than the 112-bit extended-squitter duration.

Regression tests exercise:

- signed-16-bit strong/weak bursts at 4/6/8 MS/s;
- 100 randomized timing/carrier-phase bursts at 2.4 MS/s, both whole and chunked;
- short replies followed by short/long frames at 2/6 MS/s.

These are synthetic checks; no new live comparison with dump1090 is claimed. The supported message formats and lack of a runtime backend toggle are unchanged.

## Provenance

- Preamble chip correlation, PPM bit-slicing and the CRC-24/ICAO
  address-recovery scheme are ported from SDRangel's ADS-B demodulator,
  `plugins/channelrx/demodadsb/adsbdemodsinkworker.cpp` and `adsb.h` at
  commit `030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later,
  Copyright (C) 2020 Jon Beniston, M7RCE). Files consulted:
  `adsbdemodsinkworker.cpp/.h` (preamble correlation, PPM slicing, CRC
  check, ICAO recovery), `adsbdemodsink.cpp/.h` (confirms the correlator
  and slicer run on power, `I^2+Q^2`, not amplitude or dB), `adsb.h`
  (preamble/chip/bit constants), `adsbdemod.cpp` (message handling, read
  for context on scope only -- its GUI/map/feed code is not ported).
- **Error correction**: SDRangel's ADS-B demod does **not** implement
  single-bit CRC-24 correction. DF17/18 extended squitter frames are
  accepted only on an exact CRC match
  (`adsbdemodsinkworker.cpp` ~line 434: `if (parity == m_crc.get())`); other
  Mode S downlink formats recover the ICAO address by XORing the parity
  field with the computed CRC remainder (~line 530:
  `icao = (parity ^ crc) & 0xffffff`), which is address recovery, not error
  correction, and is likewise exact-match only. `msdr/decoder/pydecoders/
  adsb_phy.py` ports the same policy: a corrupted frame is rejected, never
  silently corrected (dump1090's optional brute-force bit-flip search is
  not ported).
- The CRC-24 generator polynomial (`0xFFF409`) and the per-message-type
  field decode (identification 6-bit charset, airborne position CPR,
  airborne velocity) are not SDRangel-specific -- they follow the public
  ICAO Annex 10 Volume IV Mode S specification, as documented in
  "The 1090 MHz Riddle" by Junzi Sun (mode-s.org, MIT-licensed) and the
  same author's pyModeS. The implementation was checked bit-exactly
  against that book's own worked examples (see Validation gate below).
- Licence: msdr is GPL-3.0-only, which may incorporate GPL-3.0-or-later
  code. New files carry `SPDX-License-Identifier: GPL-3.0-or-later` (same
  as the existing AIS/DSC/LoRa ports) and name the upstream files and
  commit in their module docstring. THIRD_PARTY_NOTICES.md and the README
  credits list SDRangel's ADS-B demod.

## Scope

- Decoded: DF17/18 (ADS-B extended squitter: identification/category, airborne position with global CPR, airborne velocity) and DF11 (all-call reply: ICAO address only).
- Not decoded: other Mode S downlink formats (TCAS RA, Comm-A/B, Mode S altitude/identity replies DF4/5/20/21, surface position), Gillham-coded (Q=0) legacy altitude, and surface movement.
- This matches the task's stated minimum ("DF17/18 and DF11 at least").

## Modules

- `msdr/decoder/pydecoders/adsb_phy.py` (pure numpy, no asyncio):
  `crc24_remainder`, vectorized preamble/PPM demod (`AdsbReceiver.feed`),
  per-message-type field decode (`decode_identification`,
  `decode_airborne_position`, `decode_airborne_velocity`), global CPR
  resolution (`cpr_nl`, `cpr_resolve_global`, `CprTracker`).
  `AdsbReceiver(correlation_threshold=2.0, samples_per_chip=1)`:
  `feed(complex64 IQ, now=...) -> list[AdsbMessage]`, streaming with a
  bounded internal tail buffer.
- `msdr/decoder/pydecoders/adsb.py`: `NativeADSBBackend` keeps
  `StandaloneADSBBackend`'s constructor shape, `start`/`feed`/`stop`/
  `events` behaviour and `DecodeEvent` field schema (same SBS-style field
  names `standalone_adsb.py`'s `parse_sbs_line` emits: `icao`, `callsign`,
  `altitude_ft`, `latitude`/`longitude`, `ground_speed_kt`, `track_deg`,
  `vertical_rate_fpm`, `transmission_type`), but runs `AdsbReceiver`
  instead of piping IQ to a `dump1090-fa` subprocess.
  `backend_name = "msdr-native"`. At exactly 2 MS/s input, samples go
  straight to `AdsbReceiver` (1 sample/chip, SDRangel's native case). At
  any other input rate (e.g. msdr's 2.4 MS/s default), a windowed-sinc
  resampler (same structure as `standalone_adsb.py`'s
  `_StreamingIQResampler`) converts to **4 MS/s (2 samples/chip)**, not
  2 MS/s: decimating straight to 1 sample/chip fixes the output grid's
  phase, which can land squarely on a chip transition (measured: ~15 %
  raw bit error rate, every frame's CRC fails); oversampling to 2
  samples/chip and summing both (`AdsbReceiver(samples_per_chip=2)`, the
  same `samplesPerChip` SDRangel's correlator/slicer already sums over)
  recovers it, at the cost of ~2x the native receiver's CPU -- still well
  inside the real-time budget (see gate 3).
- `msdr/decoder/standalone_adsb.py` is unchanged and remains the
  alternative `dump1090-fa` backend (`backend="standalone_adsb"` in
  `registry.py`).
- Readiness (`msdr/decoder/readiness.py`): `adsb` is `builtin`, with
  `install_hint` pointing at the `dump1090-fa` alternative (no live
  runtime toggle between the two is implemented).

## Validation gate (must pass before release)

1. **Bit-level truth**, independent of this port's own code: published
   Mode S/ADS-B frames from "The 1090 MHz Riddle" (MIT-licensed) decode
   correctly through the *full* preamble+PPM demod pipeline (not just the
   message-field decoder), with CRC-24 validating them:
   - `8D4840D6202CC371C32CE0576098` (identification) -> callsign
     `KLM1023`.
   - `8D40621D58C382D690C8AC2863A7` (even) /
     `8D40621D58C386435CC412692AD6` (odd) (airborne position pair) ->
     global CPR resolves to lat `52.25720214843750`, lon
     `3.91937255859375` (matches the published worked example's
     `Lat_EVEN`), altitude 38,000 ft on both.
   - `8D485020994409940838175B284F` (velocity) -> ground speed 159.2 kt,
     track 182.88 deg, vertical rate -832 ft/min.
   - A single flipped bit in any of the above changes the CRC-24 and is
     rejected (`tests/decoder/test_adsb_native.py::
     test_crc24_rejects_single_bit_corruption`), including through the
     full demod pipeline on synthesized IQ -- no silent correction,
     matching SDRangel's policy (see Provenance).
2. **Synthetic IQ** (`tests/decoder/test_adsb_native.py`), rectangular-pulse
   PPM bursts of the frames above with AWGN and randomized lead/trail
   timing:
   - 2 MS/s, 100 trials/point: DF17 message success 0 %, 64 %, 100 %,
     100 %, 100 %, 100 % at 6/10/14/18/22/26 dB SNR; DF11 (synthetic
     ICAO/CA, CRC computed by the same code under test) 0 %, 81 %, 100 %,
     100 %, 100 %, 100 %. Unlike AIS/LoRa's matched-filter receivers, this
     slicer compares 1 sample/chip with no averaging, so per-bit error
     falls off slowly with SNR and a single wrong bit anywhere in 56/112
     bits already fails the exact-match CRC (measured ~0.35 % per-bit
     error at 10 dB by this test's SNR definition) -- these SNR points are
     not directly comparable to AIS's or LoRa's.
   - 2.4 MS/s via `NativeADSBBackend`'s resampler (2 samples/chip, see
     Modules): the velocity frame decodes correctly end to end, including
     through `DecodeEvent` field population.
   - Overlapping/close bursts: two different frames a few samples apart
     both decode (`test_overlapping_close_bursts_both_decode`).
   - Covers DF17 and DF11 as required; DF18 shares the DF17 code path
     (both land in the "extended squitter" branch) and is exercised by the
     CRC/field-decode unit tests but not a dedicated synthetic-IQ sweep.
3. **Throughput**: real-time factor 0.016 at 2 MS/s on the authoring Mac
   (50 synthetic bursts/second against a noise floor, 1 s of IQ,
   `test_realtime_factor`) -- comfortably under the 0.5 gate.

SYNTHETIC ONLY for (2)/(3): these are a regression bar for this receiver,
not a claim of parity with `dump1090-fa` or of field performance -- no
off-air ADS-B capture has been validated against this receiver.

See also: [lora-native.md](lora-native.md), [decoder-expansion.md](decoder-expansion.md).
