# Native LoRa receiver

msdr's built-in LoRa receiver handles LoRa, Meshtastic and MeshCore for the `lora` decoder. It is pure numpy and needs no GNU Radio. gr-lora_sdr stays available as an optional cross-check backend when `.venv-lora` exists. This page covers the supported presets, the code layout and the validation evidence.

## Validation summary

The validation gate passed on 2026-10-03:

- 20 of 20 reference vectors decode bit-exact with valid CRC.
- AWGN: SF7 100 % to −7 dB; SF11 LongFast 100 % to −15 dB.
- Real-time factor is 0.10 at 2.4 MS/s.

## LoRaWAN headers

CRC-valid raw LoRa frames that parse as LoRaWAN 1.0 (sync word 0x34, or a structurally valid MHDR) get `lorawan_*` fields: message type, DevAddr and network operator, FCtrl flags, FCnt, MAC command names, FPort, payload length, MIC as hex, and join-request EUIs. Payloads are never decrypted and the MIC is never verified. See `pydecoders/lorawan.py`.

## MeshCore US narrow preset

Parameters: 910.525 MHz, SF7, 62.5 kHz bandwidth, 4/5 coding rate, MeshCore payload parsing (sync word 0x12).

- Bandwidth is independent of SDRconnect's listening/audio filter, because this receiver consumes raw IQ.
- The 62.5 kHz setting is covered by validation of both the native and the optional GNU Radio backend.

Verification:

- **Live, 2026-10-03:** SDRconnect IQ at 6 MS/s, antenna A, LNA state 0. Seven CRC-valid MeshCore packets arrived in 35 seconds, with zero IQ queue drops. This shows reception at that location and time, not a guaranteed packet rate.
- **Regression tests:** a reference SF7 waveform reinterpreted at 62.5 kHz is decoded through 250 kS/s and 6 MS/s input paths, with a 10 kHz channel offset and chunk boundaries.

## Profiles in the UI

In the decoder's Prepare receiver panel, LoRa profiles are:

- Raw LoRa / custom (current parameters).
- MeshCore US.
- Meshtastic US LongFast: 906.875 MHz, SF11, 250 kHz, 4/5, sync word 0x2B.

Preview shows receiver and decoder changes together. Applying a preset keeps both, so Restore can undo them. The named US profiles fix the frequency; other regions and modem configurations use custom settings.

## Saved signals

- A saved signal may carry `decoder_preset` (`meshcore_us` or `meshtastic_us_longfast`).
- Tuning a saved signal reapplies those decoder parameters before starting. A later change to the global settings therefore does not silently change that favorite's PHY configuration.
- Receiver antenna, gain and sample rate are stored in its linked receiver profile.

## Modules

### `msdr/decoder/pydecoders/lora_phy.py`

Pure numpy, no asyncio.

- `LoRaParams(sf, bw, sync_word, ldro: "auto"|"on"|"off", implicit_header=False, implicit_length=None, implicit_cr=None, implicit_crc=None)`. `ldro="auto"` applies the LoRa rule: on when the symbol time `2**sf / bw` exceeds 16 ms.
- Coding helpers, each unit-tested on its own:
  - `gray_decode`
  - `deinterleave(block, sf_app, cr)`
  - `hamming_decode(codeword, cr)`: 4/5 and 4/6 detect errors; 4/7 and 4/8 correct single errors
  - `dewhiten(bytes)`: the LoRa whitening sequence
  - `header_decode`: length, CR, CRC flag, header checksum
  - `payload_crc16`
- `LoRaReceiver(params)` with `feed(samples: complex64 at exactly bw samples/s) -> list[LoRaPacket]`. It streams: packets spanning `feed` calls are completed on later calls, and internal buffers are bounded.
- `LoRaPacket(payload: bytes, crc_valid: bool | None (None = no CRC), header: dict | None, cr: int, sf: int, cfo_hz: float, snr_db: float | None, sample_index: int)`.

### `msdr/decoder/pydecoders/lora.py`

`LoRaBackend` keeps its constructor, `start/feed/stop/events` behaviour, event fields and mesh parsing, but runs the native receiver. It shifts frequency and low-pass resamples from the IQ rate to exactly `bw` (numpy only; scipy is not a declared dependency), then hands off to `LoRaReceiver`.

- `backend_name = "msdr-native"`.
- `integrity_note` reads "LoRa payload CRC-16 verified (msdr native receiver)".
- The GNU Radio path moves to `GrLoRaBackend` in the same file. It is selected only when `settings` asks for it and it is available.

### Readiness

In `msdr/decoder/readiness.py`, `lora` is `builtin`. The gr-lora_sdr cross-check is reported separately as optional.

## Validation gate

1. **Reference vectors** in `tests/decoder/data/lora/` (gr-lora_sdr TX; see its README/manifest). Every case the reference itself decodes must decode with the native receiver to the exact payload, CRC valid, with correct header fields. Cases marked `reference_decode: false` are reported, not hidden.
2. **Robustness** on those vectors, with deterministic seeds:
   - 2x and non-integer oversampling through the backend's resampler.
   - Carrier offset of ±(bw/8) and a fractional sample timing offset.
   - Leading and trailing noise.
   - Two packets back to back.
   - A packet split across many small `feed` calls.
3. **SNR sweep** (AWGN, fixed seed) at SF7 and SF11 (Meshtastic LongFast). Packet success rate per SNR is recorded in the test output and summarised in the README evidence table. No parity with gr-lora_sdr is claimed unless measured with both.
4. **Throughput:** real-time factor measured for 2.4 MS/s input at SF7 and SF12 on the development machine. The backend must keep up (rtf < 0.5), or the README says so.

## Provenance and licence

- **Demodulator structure** is ported from SDRangel's ChirpChat demodulator, `plugins/channelrx/demodchirpchat/` at commit `030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later, Copyright (C) 2019-2020 Edouard Griffiths, F4EXB; its LoRa decoder notes "Inspired by: https://github.com/myriadrf/LoRa-SDR"). Files consulted:
  - `chirpchatdemodsink.cpp/.h`: dechirp, FFT, preamble/sync detection, synchronisation.
  - `chirpchatdemoddecoderlora.cpp/.h`: Gray mapping, diagonal deinterleaving, Hamming FEC, whitening, header, CRC.
  - `chirpchatdemoddecoder.cpp`, `chirpchatdemodsettings.*`.
- **Bit conventions and frame synchronisation** come from gr-lora_sdr (Tapparel et al., EPFL, GPL-3.0) at commit `fbbf6ea6684d00363096929c7ef015cabcfaeb6d`, the build that generated the reference vectors. SDRangel's own readme says its LoRa mode is only known to work at SF11 and SF12 with low-data-rate optimisation (DE=2), tested against an RN2483, while Meshtastic and MeshCore mostly use other settings. The files and stages are listed in `lora_phy.py`'s module docstring.
- **Licence.** msdr is GPL-3.0-only, which may incorporate GPL-3.0-or-later code. New files carry `SPDX-License-Identifier: GPL-3.0-or-later` (as for the existing AIS/DSC ports) and name the upstream files and commit in their module docstring. THIRD_PARTY_NOTICES.md and the README credits list SDRangel ChirpChat and gr-lora_sdr.

See also: [decoder-expansion.md](decoder-expansion.md), [validation/](validation/).
