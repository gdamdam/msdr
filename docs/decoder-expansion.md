# Standalone decoder expansion: scope and evidence

These decoders run inside msdr or through their own protocol-specific command-line tool. None starts or requires an SDRangel process. Dependency readiness means only that the implementation or executable is available; RF correctness still needs labeled IQ and off-air validation. For anyone judging how far each decoder can be trusted.

## Live and recording raw-IQ decoders

| Decoder | Implementation | Output and limits |
| --- | --- | --- |
| ADS-B / Mode S | FlightAware `dump1090` subprocess | Aircraft reports and coordinates; BaseStation output does not expose per-message CRC status. Requires ≥2.4 MS/s and the signal within the captured IQ span. |
| RS41 radiosonde | In-process Python decoder | Acquires the 200-bit alternating preamble across integer sample phases, applies RS(255,231) correction to both interleave lanes (up to 12 symbols per lane), then emits frames only when payload sub-block CRCs pass. Synthetic IQ and recording replay are tested; off-air validation remains pending. |
| FT8 | WSJT-X `jt9` subprocess | Decodes UTC-aligned 15-second USB windows and reports accepted messages. WSJT-X `jt9` is discovered on PATH or in its macOS application bundle. Live UTC alignment is estimated from IQ receipt time. The current path needs IQ rates that are an integer multiple of 12 kHz. |
| DSC | In-process Python decoder | Framing, one-bit-tolerant phasing, time diversity, XOR ECC and bounded common call semantics (MMSI/category, distress position/time, safety position request/acknowledgement). HF/VHF synthetic IQ tested; off-air validation remains pending. |
| End-of-Train | In-process Python decoder | Emits only packets with valid CRC-18. RF capture validation is pending. |
| VOR / ILS | In-process measurement estimators | Tentative radial/DDM measurements only; they do not identify a station and are not safe for navigation. |

D-Star, Yaesu System Fusion, dPMR and M17 continue to use the standalone `dsd-fme` executable. The existing NFM decoders remain independent of these raw-IQ paths. FreeDV and DAB/DAB+ are not shipped.

Raw-IQ decoders:

- Support live single-channel reception and recording-backed single-channel analysis in Review. They do not run in the NFM band channelizer.
- Replay resets backend state on seeks, loops, run changes, virtual retunes and sample gaps; see [raw-IQ replay](epics/radio-expansion/raw-iq-replay.md) for rate, span and FT8 capture-time requirements.
- Survey suggestions are candidates only: a matching allocation or analyzer result does not assert a decoded identity.
- ADS-B and RS41 coordinates can appear on the Discover map only when actual position fields are decoded.

## Validation status

Availability and parsing are distinct from RF validation.

- FT8 was checked with native WSJT-X 3.0.2, a recorded audio sample (13 messages), and a synthetic IQ path.
- DSC has synthetic IQ tests for both physical profiles.
- The other newly added paths and live field performance still need validation. See [FT8](ft8.md) and [DSC](dsc.md) for exact evidence and limits.
- Output that has no exposed integrity check must not be presented as a CRC-verified message. VOR/ILS are measurements with `unavailable` integrity.
- Track misses and false positives alongside successes before treating any decoder as dependable.

Implementation references: [FlightAware input sample clock](https://github.com/flightaware/dump1090/blob/master/dump1090.c) (2.4 MS/s) and [WSJT-X standalone decoder](https://github.com/WSJTX/wsjtx/blob/master/lib/jt9.f90).

See also: [FT8](ft8.md), [DSC](dsc.md), [integrations](integrations.md), [receiver audio and DSP](receiver-dsp.md).
