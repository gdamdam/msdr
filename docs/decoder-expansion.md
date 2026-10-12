# Decoder scope and validation status

This page lists the raw-IQ decoders msdr runs, what each one outputs, and how far each has been validated. It is for anyone deciding how much to trust a decoder.

These decoders run inside msdr or through their own protocol-specific command-line tool. None starts or requires an SDRangel process. A decoder showing as ready means only that its implementation or executable is available. RF correctness still needs labeled IQ and off-air validation.

## Raw-IQ decoders

| Decoder | Implementation | Output and limits |
| --- | --- | --- |
| ADS-B / Mode S | Native numpy receiver (default); FlightAware `dump1090-fa` is an alternative backend | Aircraft reports and coordinates. The native receiver accepts only CRC-24-valid frames; `dump1090` BaseStation output does not expose per-message CRC status. Needs at least 2.4 MS/s and the signal within the captured IQ span. See [adsb-native.md](adsb-native.md). |
| RS41 radiosonde | In-process Python decoder | Acquires the 200-bit alternating preamble across integer sample phases and applies RS(255,231) correction to both interleave lanes (up to 12 symbols per lane). Emits a frame only when the payload sub-block CRCs pass. Tested with synthetic IQ and recording replay; off-air validation is pending. |
| FT8 | WSJT-X `jt9` subprocess | Decodes UTC-aligned 15-second USB windows and reports accepted messages. `jt9` is found on PATH or in its macOS application bundle. Live UTC alignment is estimated from IQ receipt time. Needs an IQ rate that is an integer multiple of 12 kHz. |
| DSC | In-process Python decoder | Framing, one-bit-tolerant phasing, time diversity, XOR ECC and bounded common call semantics (MMSI/category, distress position/time, safety position request/acknowledgement). Tested with HF and VHF synthetic IQ; off-air validation is pending. |
| End-of-Train | In-process Python decoder | Emits only packets with valid CRC-18. Received and decoded live off-air on 2026-10-11; independent reference comparison is pending. |
| VOR / ILS | In-process measurement estimators | Tentative radial/DDM measurements only. They do not identify a station and are not safe for navigation. |

D-Star, Yaesu System Fusion, dPMR and M17 use the standalone `dsd-fme` executable. The existing NFM decoders are independent of these raw-IQ paths. FreeDV and DAB/DAB+ are not shipped.

## How raw-IQ decoders behave

- They support live single-channel reception and recording-backed single-channel analysis in Review. They do not run in the NFM band channelizer.
- Replay resets decoder state on seeks, loops, run changes, virtual retunes and sample gaps. See [raw-IQ replay](raw-iq-replay.md) for the rate, span and FT8 capture-time requirements.
- Survey suggestions are candidates only. A matching allocation or analyzer result does not assert a decoded identity.
- ADS-B and RS41 coordinates appear on the Discover map only when actual position fields are decoded.

## Validation status

Availability and parsing are separate from RF validation.

- **FT8:** checked with native WSJT-X 3.0.2, a recorded audio sample (13 messages) and a synthetic IQ path.
- **DSC:** synthetic IQ tests for both physical profiles.
- **End-of-Train:** received and decoded live off-air (CRC-18-valid packets) on 2026-10-11.
- **VOR:** validated off-air on 2026-10-11. Radials from OAK VOR at a known location agree with the computed bearing within about ±2°, after fixing a sign error this check exposed.
- **ILS:** not confirmed off-air.
- **Other newly added paths** and live field performance still need validation.

See [FT8](ft8.md) and [DSC](dsc.md) for the exact evidence and limits.

When judging results:

- Output with no exposed integrity check must not be presented as a CRC-verified message. VOR and ILS are measurements with `unavailable` integrity.
- Track misses and false positives alongside successes before treating a decoder as dependable.

Implementation references: [FlightAware input sample clock](https://github.com/flightaware/dump1090/blob/master/dump1090.c) (2.4 MS/s) and [WSJT-X standalone decoder](https://github.com/WSJTX/wsjtx/blob/master/lib/jt9.f90).

See also: [FT8](ft8.md), [DSC](dsc.md), [integrations](integrations.md), [receiver audio and DSP](receiver-dsp.md).
