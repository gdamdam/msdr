# SDRconnect WebSocket API — research notes (2026-09-22)

Primary sources:
- Spec PDF "SDRconnect WebSocket API 1.0.3": https://sdrplay.com/wp-content/uploads/2026/08/SDRconnect_WebSocket_API.pdf
- Release notes V1.0.10: https://sdrplay.com/wp-content/uploads/2026/04/SDRconnect_Release_Notes.pdf
- Landing page: https://sdrplay.com/websocket-api/

Tags: [DOC] documented in spec/release notes, [OBS] observed in third-party code, [UNK] unknown/assumption.

## 1. Transport
- [DOC] Bidirectional WebSocket, same spec for SDRconnect GUI and SDRconnect Headless.
  TEXT frames: one JSON object per frame. BINARY frames: 2-byte LE uint16 payload-type header, then int16/uint8 data.
- [DOC] JSON envelope: `event_type`, `property`, `value` (all strings), optional `device` ("primary"|"secondary").
- [OBS] Default 127.0.0.1:5454, path `/`, plain `ws://`. Headless: `SDRconnect_headless --websocket_port=5454`.
- [OBS] Off by default in GUI; enable via Primary SP "..." menu -> Preferences -> "WebSocket Server".
- [DOC] No auth in GUI. 1.0.10 added WSS + config-file ACL, headless only. Config format [UNK].
- [DOC] Versions: 1.0.6 (Jan 2026) API introduced; 1.0.7 headless binary, device select by serial/name,
  `device_sample_rate` writable, center-freq set fixes; 1.0.8 API 1.0.2, RSPduo dual tuner; 1.0.9 API 1.0.3
  adds RO `api_version`; 1.0.10 WSS/ACL.

## 2. Readable properties [DOC] (RO = read-only)
device_center_frequency (LO, u64 Hz), device_sample_rate, device_vfo_frequency, lna_state (+ lna_state_min/max RO),
filter_bandwidth, demodulator (AM/USB/LSB/CW/SAM/NFM/WFM), demod_max_bandwidth RO, started RO, overload RO,
can_control RO, audio_volume_percent, audio_mute, audio_limiters, audio_filter, squelch_enable, squelch_threshold,
agc_enable / agc_threshold (AUDIO AGC only), wfm_stereo_enable, wfm_stereo RO, noise_reduction_enable/strength,
nfm_deemphasis_enable, spectrum_ref_level, spectrum_base, rds_ps/pi/pty/radiotext RO, rds_enable,
signal_power / signal_snr RO (dB), valid_antennas RO, active_antenna, valid_devices RO, active_device RO, api_version RO.

NOT in spec: tuning step, IF gain, hardware RF/IF AGC, notch filters, bias-T, HDR mode, decimation,
spectrum span/zoom, recording state, device model/serial as readable.

## 3. Writable
[DOC] Everything above not RO, via `set_property`. [OBS] "the API has no IF gain".

## 4. Push vs poll
[DOC] `property_changed` pushed on internal changes. `get_property` -> `get_property_response`.
Binary streams gated by `iq_stream_enable`, `audio_stream_enable`, `spectrum_enable`.
Payload types 1-3 primary (audio PCM16 stereo 48k, IQ int16, uint8 spectrum bins normalised to visible range), 4-6 secondary.

## 5. Multiple receivers
[DOC] One VFO per tuner. `device` selects primary/secondary (RSPduo only). Multiple RSPs: `selected_device`,
`selected_device_serial`, `selected_device_name` (value may carry `:<network mode>` for remote servers).
No slices / multi-VFO concept.

## 6. Hardware retune behaviour
[UNK] Center and VFO are separate properties; VFO set outside current span is undocumented.
[OBS] TandemSDR nudges center 10 kHz off VFO above 2 MSPS to avoid DC spike. No documented gain/antenna reset.

## 7. Atomicity / acks
[DOC] No batch message, no request IDs. `set_property` has no explicit ack; confirmation is via subsequent
`property_changed` or `get_property`. [OBS] clients correlate by property name; sdrminusminus uses a 400 ms settle.
[DOC] `apply_device_profile "<name>"` applies a native SDRconnect profile (closest thing to atomic multi-setting apply).

## 8. Native memories / profiles
[DOC] SDRconnect has a stored-frequency module and Profiles. Only `apply_device_profile` is exposed; no list/read
of profiles or frequencies. Import/export format [UNK].

## 9. Architecture
[DOC] WS server runs inside the SDRconnect client app (GUI or Headless). The separate "SDRconnect Server"
(remote RSP streaming) is a different component and exposes no WS API [UNK/assumed].

## 10. Limitations / gotchas
- [DOC] Fixed in 1.0.7-1.0.9: center-freq set, device select, squelch slider stuck when using API,
  macOS startup failure when Local Network permission denied with WS enabled.
- [OBS] Values sometimes arrive as bare JSON numbers despite spec saying strings.
- [OBS] Enabling `audio_stream_enable` mutes SDRconnect's own audio.
- No rate limits documented. Support: SDRplay tickets + Facebook group.

## 11. Existing clients
- Go: https://github.com/carlos-andres-osorio/sdrconnect
- Scanner CLI (profile-driven): https://github.com/fventuri/sdrconnect-scanner
- Browser GUI: https://github.com/Roger-Need/TandemSDR
- Rust backend: sdrminusminus PR #77 (https://github.com/Newspicel/sdrminusminus/pull/77)
- FT8 module: https://github.com/JvanKatwijk/sdrconnect-ft8-module
- rigctl bridge: https://www.w9ksb.com/rigctl-server-for-sdrconnect/ (unverified)
- Python: none found. PyPI `sdrconnect` is an unrelated SpyServer client.

## RSPdx-R2 specifics
[DOC] Antenna A/B/C via `active_antenna` / `valid_antennas`. HDR mode, FM/MW/DAB notches, bias-T: absent from API 1.0.3.
Only workaround: bake into a native SDRconnect profile and `apply_device_profile` [UNK whether profiles carry these].

## M0 findings (pending)

- [ ] 1. What does SDRconnect do when `device_vfo_frequency` is set outside the current span? Does
      it move the centre itself, clamp, or ignore?
- [ ] 2. Does `apply_device_profile` emit `property_changed` for every property it touches, and do
      native profiles carry notch, HDR and bias-T on the RSPdx-R2?
- [ ] 3. Per property, do values arrive as strings or numbers?
- [ ] 4. Latency of `device_sample_rate` and `device_center_frequency` changes, and whether either
      disturbs `lna_state` or `active_antenna`.
- [ ] 5. Does `set_property` on an unchanged value still echo a `property_changed`? (Affects
      confirmation logic.)
- [ ] 6. Does user tuning in the GUI produce one event per wheel step, or coalesced events? (Affects
      dwell defaults.)
- [ ] 7. Exact string values of `active_antenna` and `demodulator` as reported.

## 7. Spectrum stream — measured on the real radio (2026-09-23, SDRconnect 1.0.10, RSPdx-R2)
- [OBS] Enabled by a dedicated command, **not** a property: `{"event_type":"spectrum_enable","property":"","value":"true"}`
  (`"false"` to stop). `set_property spectrum_enable` and `get_property spectrum_enable` are silently ignored.
  The stream is per connection: it ends when the socket closes.
- [OBS] Binary frames of 514 bytes: 2-byte LE header `03 00` (payload type 3) + **512 uint8 bins**, ~23 frames/s.
- [OBS] Bin *i* maps to `center - sample_rate/2 + (i + 0.5) * sample_rate/512` (19.53 kHz/bin at 10 MS/s):
  with the centre at 98.0 MHz, all 10 strongest peaks computed this way fell on US FM channels (odd 100 kHz),
  mostly within one bin. Measured with the GUI at its default zoom; whether GUI zoom narrows the bins'
  range is untested [UNK] — scanner C must treat bin frequencies as coarse and confirm by tuning.
- [OBS] Levels are relative (floor ≈ 70–90, strong carriers 140–175); the centre bin carries the LO/DC spike.
- [OBS] `spectrum_ref_level` (-20) and `spectrum_base` (-145) are readable (dB range of the display).
