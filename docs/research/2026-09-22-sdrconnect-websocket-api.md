# SDRconnect WebSocket API: research notes (2026-09-22)

Notes on the SDRconnect WebSocket API, taken from the vendor documentation, third-party clients and measurements on a real radio. They are a record for developers working on msdr's receiver control. Nothing here is a vendor guarantee unless tagged [DOC].

Sources:

- Spec PDF "SDRconnect WebSocket API 1.0.3": https://sdrplay.com/wp-content/uploads/2026/08/SDRconnect_WebSocket_API.pdf
- Release notes V1.0.10: https://sdrplay.com/wp-content/uploads/2026/04/SDRconnect_Release_Notes.pdf
- Landing page: https://sdrplay.com/websocket-api/

Tags used below:

- [DOC]: documented in the spec or release notes.
- [OBS]: observed in third-party code or measured directly.
- [UNK]: unknown or an assumption.

## 1. Transport

- [DOC] The API is a bidirectional WebSocket, with the same spec for SDRconnect GUI and SDRconnect Headless.
  - TEXT frames carry one JSON object each.
  - BINARY frames start with a 2-byte little-endian uint16 payload-type header, followed by int16/uint8 data.
- [DOC] The JSON envelope is `event_type`, `property`, `value` (all strings), with an optional `device` ("primary" or "secondary").
- [OBS] Default endpoint is 127.0.0.1:5454, path `/`, plain `ws://`. Headless: `SDRconnect_headless --websocket_port=5454`.
- [OBS] Off by default in the GUI. Enable it via Primary SP "..." menu → Preferences → "WebSocket Server".
- [DOC] The GUI has no authentication. Version 1.0.10 added WSS and a config-file ACL, headless only. The config format is [UNK].
- [DOC] Version history:
  - 1.0.6 (Jan 2026): API introduced.
  - 1.0.7: headless binary, device select by serial/name, `device_sample_rate` writable, center-freq set fixes.
  - 1.0.8: API 1.0.2, RSPduo dual tuner.
  - 1.0.9: API 1.0.3, adds read-only `api_version`.
  - 1.0.10: WSS/ACL.

## 2. Properties

[DOC] Read-only properties are marked RO. Everything else is writable via `set_property`. [OBS] "The API has no IF gain."

- **Frequency and sampling:** `device_center_frequency` (LO, u64 Hz), `device_sample_rate`, `device_vfo_frequency`.
- **Gain and tuning:** `lna_state` (with `lna_state_min`/`lna_state_max` RO), `filter_bandwidth`, `demodulator` (AM/USB/LSB/CW/SAM/NFM/WFM), `demod_max_bandwidth` RO.
- **Status:** `started` RO, `overload` RO, `can_control` RO.
- **Audio:** `audio_volume_percent`, `audio_mute`, `audio_limiters`, `audio_filter`.
- **Squelch, AGC, noise reduction:** `squelch_enable`, `squelch_threshold`, `agc_enable` and `agc_threshold` (audio AGC only), `noise_reduction_enable`/`noise_reduction_strength`, `nfm_deemphasis_enable`.
- **WFM and RDS:** `wfm_stereo_enable`, `wfm_stereo` RO, `rds_enable`, `rds_ps`/`rds_pi`/`rds_pty`/`rds_radiotext` RO.
- **Spectrum display:** `spectrum_ref_level`, `spectrum_base`.
- **Signal level:** `signal_power` and `signal_snr`, both RO, in dB.
- **Devices and antennas:** `valid_antennas` RO, `active_antenna`, `valid_devices` RO, `active_device` RO, `api_version` RO.

Not in the spec: tuning step, IF gain, hardware RF/IF AGC, notch filters, bias-T, HDR mode, decimation, spectrum span/zoom, recording state, and device model/serial as readable values.

## 3. Push and poll

- [DOC] `property_changed` is pushed on internal changes. `get_property` returns `get_property_response`.
- [DOC] Binary streams are gated by `iq_stream_enable`, `audio_stream_enable` and `spectrum_enable`.
- [DOC] Payload types 1–3 are for the primary device and 4–6 for the secondary: audio PCM16 stereo at 48k, IQ int16, and uint8 spectrum bins normalised to the visible range.

## 4. Multiple receivers

- [DOC] There is one VFO per tuner. `device` selects primary or secondary (RSPduo only).
- [DOC] With multiple RSPs, use `selected_device`, `selected_device_serial` and `selected_device_name`. The value may carry `:<network mode>` for remote servers.
- [DOC] There are no slices and no multi-VFO concept.

## 5. Hardware retune behaviour

- [UNK] Center and VFO are separate properties. Setting a VFO outside the current span is undocumented.
- [OBS] TandemSDR nudges the center 10 kHz off the VFO above 2 MSPS to avoid the DC spike.
- No documented gain or antenna reset on retune.

## 6. Atomicity and acknowledgements

- [DOC] There is no batch message and there are no request IDs. `set_property` has no explicit ack. Confirmation comes through a later `property_changed` or `get_property`.
- [OBS] Clients correlate by property name. sdrminusminus uses a 400 ms settle time.
- [DOC] `apply_device_profile "<name>"` applies a native SDRconnect profile. It is the closest thing to an atomic multi-setting apply.

## 7. Native memories and profiles

- [DOC] SDRconnect has a stored-frequency module and Profiles. Only `apply_device_profile` is exposed. There is no way to list or read profiles or frequencies.
- The import/export format is [UNK].

## 8. Architecture

[DOC] The WebSocket server runs inside the SDRconnect client app (GUI or Headless). The separate "SDRconnect Server" (remote RSP streaming) is a different component and is assumed to expose no WebSocket API [UNK].

## 9. Limitations and gotchas

- [DOC] Fixed in 1.0.7–1.0.9: center-freq set, device select, the squelch slider sticking when using the API, and a macOS startup failure when Local Network permission is denied with the WebSocket enabled.
- [OBS] Values sometimes arrive as bare JSON numbers although the spec says strings.
- [OBS] Enabling `audio_stream_enable` mutes SDRconnect's own audio.
- No rate limits are documented. Support is through SDRplay tickets and a Facebook group.

## 10. Existing clients

- Go: https://github.com/carlos-andres-osorio/sdrconnect
- Scanner CLI (profile-driven): https://github.com/fventuri/sdrconnect-scanner
- Browser GUI: https://github.com/Roger-Need/TandemSDR
- Rust backend: sdrminusminus PR #77 (https://github.com/Newspicel/sdrminusminus/pull/77)
- FT8 module: https://github.com/JvanKatwijk/sdrconnect-ft8-module
- rigctl bridge: https://www.w9ksb.com/rigctl-server-for-sdrconnect/ (unverified)
- Python: none found. The PyPI package `sdrconnect` is an unrelated SpyServer client.

## 11. RSPdx-R2 specifics

- [DOC] Antennas A/B/C are selected with `active_antenna` and `valid_antennas`.
- HDR mode, FM/MW/DAB notches and bias-T are absent from API 1.0.3.
- The only workaround is to bake them into a native SDRconnect profile and use `apply_device_profile` [UNK whether profiles carry these].

## 12. Spectrum stream, measured on the real radio

Measured 2026-09-23 with SDRconnect 1.0.10 and an RSPdx-R2.

- [OBS] The stream is enabled by a dedicated command, not a property: `{"event_type":"spectrum_enable","property":"","value":"true"}` (`"false"` to stop). `set_property spectrum_enable` and `get_property spectrum_enable` are silently ignored. The stream is per connection and ends when the socket closes.
- [OBS] Frames are 514-byte binary messages: a 2-byte little-endian header `03 00` (payload type 3) plus **512 uint8 bins**, at about 23 frames/s.
- [OBS] Bin *i* maps to `center - sample_rate/2 + (i + 0.5) * sample_rate/512`, which is 19.53 kHz per bin at 10 MS/s. With the center at 98.0 MHz, all 10 strongest peaks computed this way fell on US FM channels (odd 100 kHz), mostly within one bin. This was measured with the GUI at its default zoom. Whether GUI zoom narrows the bins' range is untested [UNK], so scanner code must treat bin frequencies as coarse and confirm by tuning.
- [OBS] Levels are relative: the floor is about 70–90 and strong carriers 140–175. The center bin carries the LO/DC spike.
- [OBS] `spectrum_ref_level` (-20) and `spectrum_base` (-145) are readable (the dB range of the display).

## Open questions

Still to be checked on the real radio:

- [ ] 1. What does SDRconnect do when `device_vfo_frequency` is set outside the current span? Does it move the center itself, clamp, or ignore?
- [ ] 2. Does `apply_device_profile` emit `property_changed` for every property it touches, and do native profiles carry notch, HDR and bias-T on the RSPdx-R2?
- [ ] 3. Per property, do values arrive as strings or numbers?
- [ ] 4. What is the latency of `device_sample_rate` and `device_center_frequency` changes, and does either disturb `lna_state` or `active_antenna`?
- [ ] 5. Does `set_property` on an unchanged value still echo a `property_changed`? (This affects confirmation logic.)
- [ ] 6. Does user tuning in the GUI produce one event per wheel step, or coalesced events? (This affects dwell defaults.)
- [ ] 7. What are the exact string values of `active_antenna` and `demodulator` as reported?
