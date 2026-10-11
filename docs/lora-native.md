# Native LoRa receiver (no GNU Radio)

Pure-numpy LoRa receiver (LoRa, Meshtastic, MeshCore) for the `lora` decoder; it replaces the GNU Radio + gr-lora_sdr requirement. gr-lora_sdr stays an optional cross-check backend when `.venv-lora` exists.

Status: 0.62.0. Gate passed 2026-10-03:

- 20/20 reference vectors bit-exact with valid CRC.
- AWGN SF7 100 % to −7 dB; SF11 LongFast 100 % to −15 dB.
- Real-time factor 0.10 at 2.4 MS/s.

## Provenance

- Demodulator structure ported from SDRangel's ChirpChat demodulator,
  `plugins/channelrx/demodchirpchat/` at commit
  `030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later,
  Copyright (C) 2019-2020 Edouard Griffiths, F4EXB; its LoRa decoder notes
  "Inspired by: https://github.com/myriadrf/LoRa-SDR"). Files consulted:
  `chirpchatdemodsink.cpp/.h` (dechirp, FFT, preamble/sync detection,
  synchronisation), `chirpchatdemoddecoderlora.cpp/.h` (Gray mapping,
  diagonal deinterleaving, Hamming FEC, whitening, header, CRC),
  `chirpchatdemoddecoder.cpp`, `chirpchatdemodsettings.*`.
- SDRangel's own readme states its LoRa mode is only known to work at SF11
  and SF12 with low-data-rate optimisation (DE=2), tested against an RN2483.
  Meshtastic and MeshCore mostly use other settings, so the bit conventions
  and frame synchronisation are ported from gr-lora_sdr (Tapparel et al.,
  EPFL, GPL-3.0) at commit `fbbf6ea6684d00363096929c7ef015cabcfaeb6d`, the
  build that generated the reference vectors; the files and stages are
  listed in `lora_phy.py`'s module docstring.
- Licence: msdr is GPL-3.0-only, which may incorporate GPL-3.0-or-later code.
  New files carry `SPDX-License-Identifier: GPL-3.0-or-later` (same as the
  existing AIS/DSC ports) and name the upstream files and commit in their
  module docstring. THIRD_PARTY_NOTICES.md and the README credits list
  SDRangel ChirpChat and gr-lora_sdr.

## Modules

- `msdr/decoder/pydecoders/lora_phy.py` (pure numpy, no asyncio):
  - `LoRaParams(sf, bw, sync_word, ldro: "auto"|"on"|"off", implicit_header=False,
    implicit_length=None, implicit_cr=None, implicit_crc=None)`.
    `ldro="auto"` applies the LoRa rule: on when the symbol time
    2**sf / bw exceeds 16 ms.
  - Coding helpers, each unit-tested on its own: `gray_decode`,
    `deinterleave(block, sf_app, cr)`, `hamming_decode(codeword, cr)`
    (4/5 and 4/6 detect, 4/7 and 4/8 correct single errors),
    `dewhiten(bytes)` (LoRa whitening sequence), `header_decode` (length,
    CR, CRC flag, header checksum), `payload_crc16`.
  - `LoRaReceiver(params)` with `feed(samples: complex64 at exactly bw
    samples/s) -> list[LoRaPacket]`; streaming: packets spanning feed calls
    are completed on later calls; internal buffers are bounded.
  - `LoRaPacket(payload: bytes, crc_valid: bool | None (None = no CRC),
    header: dict | None, cr: int, sf: int, cfo_hz: float, snr_db: float | None,
    sample_index: int)`.
- `msdr/decoder/pydecoders/lora.py`: `LoRaBackend` keeps its constructor,
  `start/feed/stop/events` behaviour, event fields and mesh parsing exactly,
  but runs the native receiver: frequency shift + low-pass resample from
  the IQ rate to exactly `bw` (numpy only; scipy is not a declared
  dependency), then `LoRaReceiver`. `backend_name = "msdr-native"`;
  `integrity_note` says "LoRa payload CRC-16 verified (msdr native
  receiver)". The GNU Radio path moves to `GrLoRaBackend` in the same file
  and is selected only when `settings` asks for it and it is available.
- Readiness (`msdr/decoder/readiness.py`): `lora` is `builtin`; the
  gr-lora_sdr cross-check is reported separately as optional.

## Validation gate (must pass before release)

1. Reference vectors `tests/decoder/data/lora/` (gr-lora_sdr TX, see its
   README/manifest): every case the reference itself decodes must decode
   with the native receiver to the exact payload, CRC valid, correct header
   fields. Cases marked `reference_decode: false` are reported, not hidden.
2. Robustness on those vectors (in the test, deterministic seeds): 2x and
   non-integer oversampling through the backend's resampler, carrier offset
   of ±(bw/8) and a fractional sample timing offset, leading/trailing noise,
   two packets back to back, a packet split across many small `feed` calls.
3. SNR sweep (AWGN, fixed seed) at SF7 and SF11 (Meshtastic LongFast):
   packet success rate per SNR recorded in the test output and summarised in
   the README evidence table; no claim of parity with gr-lora_sdr unless
   measured with both.
4. Throughput: real-time factor measured for 2.4 MS/s input at SF7 and SF12
   on the development machine; the backend must keep up (rtf < 0.5) or the README says so.

## MeshCore US narrow preset

Parameters: 910.525 MHz, SF7, 62.5 kHz bandwidth, 4/5 coding rate, MeshCore payload parsing (sync word 0x12).

- Bandwidth is independent of SDRconnect's listening/audio filter; this receiver consumes raw IQ.
- The 62.5 kHz setting is supported by both native and optional GNU Radio backend validation.

### Verification

- Live, 2026-10-03: SDRconnect IQ at 6 MS/s, antenna A, LNA state 0 — seven CRC-valid MeshCore packets in 35 seconds, zero IQ queue drops. This demonstrates reception at that location and time, not a guaranteed packet rate.
- Regression tests decode a reference SF7 waveform reinterpreted at 62.5 kHz through 250 kS/s and 6 MS/s input paths, with a 10 kHz channel offset and chunk boundaries.

### Profiles in the UI

- In the decoder's Prepare receiver panel, LoRa profiles separate Raw LoRa/custom (current parameters), MeshCore US, and Meshtastic US LongFast (906.875 MHz, SF11, 250 kHz, 4/5, sync word 0x2B).
- Preview shows receiver and decoder changes together; applying a preset retains both for Restore.
- Named US profiles fix the frequency; other regions and modem configurations use custom settings.

### Saved signals

- Saved signals may carry `decoder_preset` (`meshcore_us` or `meshtastic_us_longfast`).
- Tuning a saved signal reapplies those decoder parameters before starting, so later global settings changes do not silently change that favorite's PHY configuration.
- Receiver antenna, gain, and sample rate are stored in its linked receiver profile.

See also: [decoder-expansion.md](decoder-expansion.md), [validation/](validation/).
