# FT8 decoder

msdr decodes FT8 using WSJT-X's `jt9` command-line decoder. This page covers how the pipeline works, what it needs and how it was checked.

## Pipeline

1. The receiver supplies complex signed 16-bit IQ.
2. msdr tunes the requested USB channel and filters 0–3 kHz.
3. msdr writes mono 12 kHz PCM WAV files aligned to 15-second UTC periods.
4. `jt9` decodes each completed file asynchronously.

A successful event means WSJT-X accepted the message through its FT8 decoder checks.

## Requirements

### Input

- The IQ rate must be an integer multiple of 12 kHz and greater than 12 kHz.
- The complete USB passband must fit inside the captured IQ span.
- Live input has no source timestamp. The first boundary is estimated from the time IQ reaches msdr, and later windows follow the sample count. UTC accuracy is therefore limited by stream timing and startup latency.

### `jt9`

- Install WSJT-X for the host architecture and make its `jt9` executable discoverable by msdr. msdr checks explicit binary directories first, then PATH, then `~/Applications/wsjtx.app`.
- On macOS the executable lives at `wsjtx.app/Contents/MacOS/jt9`.
- `jt9` depends on libraries and helper files shipped in the WSJT-X application bundle. Keep it in that bundle rather than copying `jt9` alone.
- Check the decoder readiness view before starting an FT8 session.

## Validation

### Adapter tests

These exercise `jt9` row parsing, rate guards, 12 kHz / 15-second WAV construction and the async subprocess-to-event path, using a small fake executable. They verify the application boundary, not RF performance.

### WSJT-X smoke check

WSJT-X 3.0.2's `ft8sim` generated a 12 kHz FT8 WAV for `K1ABC W9XYZ EN37` at 1500 Hz, 0.0 seconds offset and -10 dB SNR. The bundled Apple Silicon `jt9` decoded it as:

```text
000001 -10  0.0 1500 ~  K1ABC W9XYZ EN37
```

- The same `jt9` decoded 13 messages from the SDRangel sample `sdrbench/samples/ft8/230105_091630.wav`.
- Both WAVs were also passed through msdr's own async `jt9` subprocess path. The generated capture returned a structured `K1ABC W9XYZ EN37` event, and the recording returned 13 structured messages.

### Channelizer smoke check

The generated waveform was resampled to 48 kHz and supplied as I with Q=0. It then went through msdr's USB filter and 4:1 decimator, and the same backend decoded `K1ABC W9XYZ EN37` at -13 dB. This synthetic path check does not replace validation against captured IQ, and it does not verify live clock alignment from the receiver.

### Fixture and install notes

- The recorded fixture's SHA-256 is `cf8da30e44b5632515b407cf7171f55ccfc5116a83d8f69244fa4482a4472a24`.
- Native WSJT-X 3.0.2 was installed locally at `~/Applications/wsjtx.app`, which msdr discovers automatically.
- The official arm64 package SHA-256 was verified before the bundle was extracted. No system installer scripts or shared-memory system changes were used.

See also: [decoder-expansion.md](decoder-expansion.md), [validation/](validation/).
