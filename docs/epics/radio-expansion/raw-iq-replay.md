# Raw-IQ recording analysis

Review's single-channel decoder selector includes IQ decoders as well as compatible
audio decoders. IQ decoders receive the original recorded complex samples, centered
on the virtual receiver frequency. They do not receive demodulated audio and do not
write to the live receiver. The recorded sample rate and full required channel
bandwidth must satisfy the decoder; an incompatible recording reports an error.

Source identity, source generation, recording run, virtual receiver channel generation,
center frequency, sample rate and selected frequency define a decoding context. A new
context recreates the backend and its DSP/framing state. A forward sample-index gap or
reported queue drop also resets the backend. Duplicate and older blocks are ignored.
Asynchronous results from an old backend/context are discarded, even before the first
new block arrives. Stop invalidates output before waiting for process/task cleanup.
Concurrent start/stop operations are serialized.

Events retain recording source ID, generation, run index, analysis run ID and the most
recent fed block's starting sample index. This identifies the input block associated
with emission, not the message onset: subprocess/windowed decoders can emit later. This attribution is
kept distinct from live reception.

FT8 uses capture UTC plus the block's sample offset to align its 15-second windows.
Recordings without capture UTC cannot start FT8 analysis; playback wall-clock time is
not substituted. Existing optional jt9/dump1090 requirements still apply. Other IQ
backends retain their own declared integrity policies: replay does not promote an
unvalidated decoder into a validated one.

Validation: deterministic lifecycle tests cover seek/loop/run/recording changes,
retune, dropped samples, stale/duplicate blocks, queued output during stop and fresh
analysis identities. Actual protocol/off-air validation is documented separately.
