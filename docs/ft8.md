# FT8 decoder

FT8 decoding in msdr, built on WSJT-X's `jt9` command-line decoder.

## Pipeline

1. The receiver supplies complex signed 16-bit IQ.
2. msdr tunes the requested USB channel and filters 0–3 kHz.
3. msdr writes mono 12 kHz PCM WAV files aligned to 15-second UTC periods.
4. `jt9` decodes each completed file asynchronously.

A successful event means WSJT-X accepted the message through its FT8 decoder checks.

## Input constraints

- The IQ rate must be an integer multiple of 12 kHz and greater than 12 kHz.
- The complete USB passband must fit inside the captured IQ span.
- Live input has no source timestamp, so the first boundary is estimated from the time IQ reaches msdr; subsequent windows follow the sample count. UTC accuracy is therefore limited to stream timing and startup latency.

## Dependency

- Install WSJT-X for the host architecture and make its `jt9` executable discoverable by msdr.
- On macOS the executable lives at `wsjtx.app/Contents/MacOS/jt9`.
- `jt9` depends on libraries and helper files shipped in the WSJT-X application bundle; keep it in that bundle rather than copying `jt9` alone.
- Check availability in the decoder readiness view before starting an FT8 session.

## Validation

### Adapter tests

They exercise jt9 row parsing, rate guards, 12 kHz/15-second WAV construction, and the async subprocess-to-event path using a small fake executable. This verifies the application boundary, not RF performance.

### WSJT-X smoke check

WSJT-X 3.0.2's `ft8sim` generated a 12 kHz FT8 WAV for `K1ABC W9XYZ EN37` at 1500 Hz, 0.0 seconds offset and -10 dB SNR. The bundled Apple Silicon `jt9` decoded it as:

```text
000001 -10  0.0 1500 ~  K1ABC W9XYZ EN37
```

- The same `jt9` decoded 13 messages from the SDRangel sample `sdrbench/samples/ft8/230105_091630.wav`.
- Both WAVs were also passed through msdr's actual async `jt9` subprocess path: the generated capture returned a structured `K1ABC W9XYZ EN37` event and the recording returned 13 structured messages.

### Channelizer smoke check

The generated waveform was resampled to 48 kHz, supplied as I with Q=0, passed through msdr's USB filter and 4:1 decimator, and decoded by the same backend as `K1ABC W9XYZ EN37` at -13 dB. This synthetic path check does not replace validation against captured IQ or verify live clock alignment from the receiver.

### Fixture and install notes

- The recorded fixture's SHA-256 is `cf8da30e44b5632515b407cf7171f55ccfc5116a83d8f69244fa4482a4472a24`.
- Native WSJT-X 3.0.2 is installed locally at `~/Applications/wsjtx.app`; msdr now discovers `jt9` there automatically, after explicit binary directories and PATH.
- The official arm64 package SHA-256 was verified before extracting the bundle. No system installer scripts or shared-memory system changes were used.

See also: [decoder-expansion.md](decoder-expansion.md), [validation/](validation/).
