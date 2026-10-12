# Validation corpus

This page is the human-readable companion to `tests/corpus/manifest.json`, which `tests/corpus/test_manifest.py` validates. Each row is a case to check, not a confirmed result. An operator-reported frequency or label is a claim, not ground truth, until a decode or an independent reference backs it up.

| id | class | reference | status | question |
|---|---|---|---|---|
| `noaa-wx-162400-425-494` | NFM voice | operator report | pending intake | Is speech present, and is tuning accurate? |
| `aprs-144390` | packet | operator report | pending intake | Tone/clock match, valid FCS or parsed payload? |
| `keyed-carrier-161098` | unknown keyed carrier | operator report | pending intake | Is on/off activity present, and what, if anything, is it? |
| `repeater-147060` | NFM voice | operator report | pending intake | Is the frequency mapping accurate (versus the old 147.074 label), and is the active/quiet state correct? |
| `land-mobile-bursty-160805-165278-162553` | NFM bursty | operator report | pending intake | Does evidence accumulate correctly across dwells, with identity left open? |
| `ais-161975-162025` | AIS | operator report | synthetic validated, field pending | Is the GMSK/9600 hypothesis validated, without identifying AIS by frequency alone? |
| `fm-broadcast-96_5-104_5-operator-claims` | FM broadcast | operator report | pending intake | Do off-air checks support the claimed station identities? |

## Cases

### NOAA weather radio (`noaa-wx-162400-425-494`)

Status: pending intake.

The frequencies are 162.400 / 162.425 / 162.494 MHz. They were previously cataloged under the single label "162.424". The open questions:

- Is speech actually present (NFM)?
- Is msdr tuning the right one of the three frequencies for each transmitter? The old single label conflates them.

### APRS (`aprs-144390`)

Status: pending intake.

144.390 MHz is the North America APRS calling frequency, 1200 baud Bell 202 AFSK. There are three claims to distinguish, from weakest to strongest:

1. A tone/clock match consistent with 1200 Bd AFSK.
2. A frame that passes AX.25's 16-bit FCS.
3. A fully parsed APRS payload.

A live capture should say which of the three msdr actually reaches, not just assert "APRS".

### Unidentified keyed carrier (`keyed-carrier-161098`)

Status: pending intake.

161.098 MHz shows on/off keyed activity. It is not assumed to be CW. The corpus asks only whether keyed activity is present and, if so, what can be said about it, rather than defaulting every keyed carrier to a CW identification.

### 2 m repeater (`repeater-147060`)

Status: pending intake.

A frequency-mapping regression is suspected. An older catalog entry labeled this repeater 147.074, while current reporting says 147.060. Both the frequency-mapping bug and the active/quiet voice state need independent confirmation.

### Bursty land-mobile traffic (`land-mobile-bursty-160805-165278-162553`)

Status: pending intake.

160.805 / 165.278 / 162.553 MHz show bursty land-mobile-style traffic across multiple dwells, with gaps. The question is about msdr's own accumulation behavior: does it combine evidence across dwells correctly and report "insufficient data" rather than a premature guess? It is not about naming the operator on these frequencies, and identity is left open.

### AIS (`ais-161975-162025`)

Status: synthetic validated, field pending.

161.975 / 162.025 MHz are the AIS channels by band-plan allocation. Previously the survey analyzer reported `psk_like` with no baud on them. Allocation is a hint, not a decode, and AIS must never be identified from frequency or allocation alone. The bar to clear is a validated GMSK/9600-baud hypothesis with `valid_frames > 0`.

What is in place:

- The analyzer classifies a synthetic GMSK 9600 burst as `fm_fsk` at 9600 Bd, using ifreq bimodality and a frequency zero-crossing grid (`tests/survey/test_features.py`, `tests/survey/test_analyzer.py`). The family is "GMSK/FSK 9600 (AIS or packet)", which is a hypothesis, not an identity.
- The registry's `ais` decoder (`msdr/decoder/pydecoders/ais.py`) decodes synthetic bursts with CRC-16/X.25 checked (`tests/decoder/test_ais.py`). AIS identity needs a CRC-valid frame with an MMSI.

No 161.975 / 162.025 MHz capture is in the corpus, so nothing here is measured off-air. Live AIS decoding on these channels was observed separately; see [live-vhf-uhf-2026-10-03.md](live-vhf-uhf-2026-10-03.md).

### FM broadcast operator claims (`fm-broadcast-96_5-104_5-operator-claims`)

Status: pending intake.

Nine US West Coast stations across 96.5–104.5 MHz are claimed: KOIT, KLLC, KCRC, KSOL, KBRG, KRBQ, KBLX, KCPD and KNBR. They were reported mono/pilotless. These are operator claims to verify, not ground truth, and none of the nine has a fixture in this repo. An RDS decode would show only that something valid decoded, not the claimed callsign, because RDS has no station-name field.
