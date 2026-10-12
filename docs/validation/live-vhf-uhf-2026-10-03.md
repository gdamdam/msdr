# Live VHF/UHF decoder notes, 2026-10-03

Observations from a US West Coast receiver session. A heard burst is evidence of radio activity, not proof that its contents or protocol were decoded. Earlier results (2026-09-28) are kept outside the repository.

## Summary

| Decoder | Frequency | Outcome |
|---|---:|---|
| AIS | 161.975 and 162.025 MHz | Operator-confirmed live decoding resumed; earlier zero-frame capture unexplained |
| ADS-B / Mode S | 1090 MHz | Live decoding works; compared with `dump1090-fa` |
| POCSAG / FLEX | 152.840 MHz | POCSAG live output confirmed after repair; FLEX untested |
| APRS / AX.25 1200 | 144.390 MHz | Live output confirmed |
| CW | 432.304620 MHz | Live audio and decoder output confirmed after exact tuning |
| DMR Capacity Plus | 152.878017 MHz | Control, call metadata and voice audio confirmed |
| DMR, UHF sample | 451.0875, 451.2000, 452.4625 MHz | Off-air DMR synchronization confirmed |
| NXDN96 | 451.099600 MHz | Synchronization and encrypted call metadata observed |
| NXDN candidate | About 452.7000 MHz | Mode unconfirmed |

## AIS: 161.975 and 162.025 MHz

**Session.** AIS ran on antenna A, 6 MS/s, LNA 0. An earlier 30-second direct-IQ comparison and the app's channelizer both received samples but produced zero valid AIS frames. Candidate preambles failed framing or CRC. Later in the session the operator reported that AIS works again. The channel, frame count and receiver settings for that later observation were not recorded.

**Interpretation.** Operator-confirmed live decoding resumed. The earlier zero-frame capture remains unexplained, and this note does not establish whether reception conditions or software state changed. The same site received 58 and 49 CRC-valid messages on these channels in the 2026-09-28 test.

## ADS-B / Mode S: 1090 MHz

**Session.** Live antenna-B IQ was compared with `dump1090-fa` on the same capture. At LNA state 0, native msdr decoded 980 messages in about 20 seconds. dump1090 decoded 931 without bit fixing and 1031 with fixing.

**Interpretation.** Live decoding works in the tested configuration. Counts vary with traffic, and the comparison is not a general parity claim.

## POCSAG / FLEX: 152.840 MHz

**Session.**

- On antenna A, 6 MS/s, LNA 0, the live app showed zero pages. A simultaneous 150-second IQ capture independently decoded two POCSAG1200 pages with the same address and sequential message numbers. The capture had 3,000 frames, no client queue drops and no receive gap above 0.2 seconds.
- The reference recording came from the same SDRconnect IQ stream as the app, centered at 152.850 MHz and narrowed to 152.840 MHz at 160 kS/s. The operator's private message text is intentionally omitted.
- After the fixed-channel repair and a server restart, the app reported a live POCSAG event at the same tuning, and the operator later confirmed more pages decode.

**Interpretation.** POCSAG live output is confirmed after the repair. The fixed-channel path now uses exact-frequency extraction. Message content still needs independent comparison, because multimon-ng exposes no integrity flag for POCSAG. FLEX remains untested.

## APRS / AX.25 1200: 144.390 MHz

**Session.** The operator confirms APRS decoding in this session. A 2026-09-28 off-air packet decoded by Direwolf passed the AX.25 FCS; see the earlier live log.

**Interpretation.** Live output confirmed. Exact payload comparison against an independent decoder is pending.

## CW: 432.304620 MHz

**Session.** A keyed carrier measured 2.276 kHz above the initial 432.302344 MHz VFO. SAM/6 kHz included it, while CW/1 kHz excluded it. After tuning CW/1 kHz to the measured carrier, the operator heard it clearly. Receiver SNR was about 21 dB, and the audio stream had a tone near 594 Hz. The app emitted CW text, and the operator confirmed the decoder.

**Interpretation.** Live audio and decoder output confirmed after exact tuning. Morse has no integrity check, so independent transcription comparison is pending.

## DMR Capacity Plus: 152.878017 MHz

**Session.** The app decoded color code 02, site 1, rest LSN changes, and talkgroup 70 call events from three radio IDs on both time slots. dsd-fme saved non-silent WAV clips, and the operator listened and confirmed voice. Some WAV clips are shorter than their associated call durations.

**Interpretation.** Live DMR control, call metadata and voice audio confirmed. Complete-call audio quality, per-message integrity and independent content comparison remain unverified. The decoded fields do not identify the system operator.

## DMR, UHF sample: 451.0875, 451.2000, 452.4625 MHz

**Session.** An eight-second wideband IQ recording was channelized at each center and decoded with dsd-fme. It repeatedly synchronized as DMR, with color codes 09, 02 and 03 respectively.

**Interpretation.** Off-air DMR synchronization confirmed at these centers. No talkgroup or voice claim is made from this sample. Survey markers at 6 MS/s are about 11.7 kHz apart, so their displayed peak positions alone are not precise enough for narrowband tuning.

## NXDN96: 451.099600 MHz

**Session.**

- Off-air IQ replay produced NXDN96 RTCH2 data/voice indications and an explicit `Sync: NXDN96` line.
- At 18:35, dsd-fme's separate event log recorded NXDN target/source `00000777`, RAN 07 and encryption algorithm 1. The app did not ingest that log at the time. The event-log ingestion bug is repaired.
- Later wideband NXDN rows with talkgroup `/TG` were malformed status text and are now rejected.
- NXDN48 at this frequency yielded no frames in the checked recordings.

**Interpretation.** Protocol synchronization and encrypted call metadata observed. Clear voice remains unverified.

## NXDN candidate: about 452.7000 MHz

**Session.** Forced NXDN48 and NXDN96 replay modes each produced some synchronization lines, but also CRC errors and conflicting rate indications in the short sample.

**Interpretation.** Unconfirmed mode. A cleaner capture and valid payload or call evidence are needed before identifying this signal.
