# Raw-IQ recording analysis

In Review, you can run IQ decoders on a recording, not just audio decoders. This page explains what those decoders receive, when they reset, and what the output events record.

## What IQ decoders receive

Review's single-channel decoder selector lists IQ decoders alongside the compatible audio decoders.

- IQ decoders get the original recorded complex samples, centered on the virtual receiver frequency.
- They do not get demodulated audio, and they never write to the live receiver.
- The recorded sample rate and the full required channel bandwidth must satisfy the decoder. An incompatible recording reports an error.

## When a decoder resets

The following together define a decoding context: source identity, source generation, recording run, virtual receiver channel generation, center frequency, sample rate and selected frequency.

- A new context recreates the backend and its DSP and framing state.
- A forward sample-index gap or a reported queue drop also resets the backend.
- Duplicate and older blocks are ignored.
- Asynchronous results from an old backend or context are discarded, even before the first new block arrives.
- Stop invalidates output before waiting for process or task cleanup.
- Concurrent start and stop operations are serialized.

## Event attribution

Events keep the recording source ID, generation, run index, analysis run ID, and the starting sample index of the most recent block fed to the decoder. That identifies the input block associated with the emission, not the message onset, because subprocess and windowed decoders can emit later. This attribution is kept distinct from live reception.

## Decoder-specific requirements

- **FT8** uses the capture UTC plus the block's sample offset to align its 15-second windows. A recording without capture UTC cannot start FT8 analysis. Playback wall-clock time is not substituted.
- The optional `jt9` and `dump1090` requirements still apply where those backends are used.
- Other IQ backends keep their own declared integrity policies. Replay does not promote an unvalidated decoder to a validated one.

## Validation

Deterministic lifecycle tests cover seek, loop, run and recording changes, retune, dropped samples, stale and duplicate blocks, queued output during stop, and fresh analysis identities. Protocol and off-air validation is documented separately; see [decoder-expansion.md](decoder-expansion.md).
