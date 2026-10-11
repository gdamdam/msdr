# Signal workbench epic

Implementation order: activity-triggered capture; signal inspection and Scope;
within-band watch scheduling; bounded carrier tracking; LoRa and mesh receive feeds.

## Operator workflows

- Arm once, watch chosen channels, retain pre-roll on sustained activity and close
  after a quiet hold. Record the trigger reason with the existing observation.
- Expand Scope in Review for audio waveform, IQ envelope and spectrum. Freeze,
  trigger, zoom and measure sample-relative time. Reuse the same inspection engine
  for an explicitly started live receiver. Never modify archived IQ.
- Monitor channels sharing a confirmed captured span together, select one using
  priorities and hold time, and optionally cycle span groups. Report unobserved
  frequencies and resolution limits explicitly.
- Measure an explicitly selected carrier's drift, and optionally correct the
  software channel within a bounded range. Reset on discontinuities and gate on
  signal quality; no automatic hardware tuning for drift correction.
- Receive LoRa packet evidence from SDRconnect live IQ or recordings, with
  MeshCore/Meshtastic protocol evidence from those decoded packets. Preserve
  provenance, dependencies, encryption limits and unavailable states.

## Acceptance

Real UI and API workflows; bounded queues/buffers; explicit receiver ownership;
no stale samples after stop, seek or retune; no RF transmission; no claims of
native LoRa IQ decoding for external decoder results. Synthetic and fake-IQ
regressions plus browser checks are required. Hardware interoperability remains
unverified until exercised against actual devices.
