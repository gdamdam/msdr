# Digital Selective Calling

msdr includes a decoder for maritime Digital Selective Calling (DSC, used for distress and selective calls) on MF/HF and VHF channel 70. It takes signed interleaved int16 complex IQ centered on the tuned signal.

## Modulation profiles

| Band | Modulation | Notes |
|---|---|---|
| MF/HF | 100-baud binary FSK, tones at ±85 Hz | |
| VHF channel 70 | 1200-baud FSK, 1300/2100-Hz audio tones | An FM phase discriminator runs before tone detection. This profile is selected for tuned frequencies of 156.4–156.7 MHz. |

## Acquisition

Phasing acquisition tolerates one corrupted bit in the 30-bit phasing pattern. The message must still pass its DSC XOR ECC before any semantic field is reported.

## Events

Events keep the received symbol list, the symbol error count and a separate `ecc_ok` flag. XOR ECC is checked before semantic fields are exposed. Invalid or unavailable coordinates are omitted.

| Layout | Fields |
|---|---|
| Distress alert | `sender_mmsi`, `distress_nature`, `latitude`, `longitude`, `utc_time` (each may be present) |
| Selective, group or all-ships call (routine, safety, urgency) | `recipient_mmsi`, `sender_mmsi`, `category`, `telecommand`, `telecommand2`; destination when present |
| Safety position request and acknowledgement | Request or response state. Accepted responses include validated coordinates and UTC time. |

Unsupported layouts, including automatic calls, stay as raw symbols and produce no inferred positions or identities.

## Sources and validation

- Position request and acknowledgement layouts follow Table A1-4.7 and Table A1-7 in [ITU-R M.493-16](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.493-16-202312-I%21%21PDF-E.pdf).
- Semantic symbol layouts and values follow SDRangel's DSC implementation by Jon Beniston, M7RCE (2023), under GPL-3.0-or-later. See [`dsc_message.py`](../msdr/decoder/pydecoders/dsc_message.py) and the [GNU GPL v3](https://www.gnu.org/licenses/gpl-3.0.html).
- Tests cover the HF and VHF synthetic IQ paths. There is no off-air reception validation.

See also: [decoder-expansion.md](decoder-expansion.md), [validation/](validation/).
