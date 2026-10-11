# Validation corpus

Human-readable companion to `tests/corpus/manifest.json` (machine-validated by
`tests/corpus/test_manifest.py`). Each row below is a *case to check*, not a confirmed result --
an operator-reported frequency/label is a claim, not ground truth, until a decode or an
independent reference backs it up.

| id | class | reference | status | question |
|---|---|---|---|---|
| `noaa-wx-162400-425-494` | NFM voice | operator report | pending intake | Speech present, tuning accurate? |
| `aprs-144390` | packet | operator report | pending intake | Tone/clock vs valid FCS vs parsed payload? |
| `keyed-carrier-161098` | unknown keyed carrier | operator report | pending intake | Is on/off activity present -- what, if anything, is it? |
| `repeater-147060` | NFM voice | operator report | pending intake | Frequency mapping accurate (vs old 147.074 label), active/quiet correct? |
| `land-mobile-bursty-160805-165278-162553` | NFM bursty | operator report | pending intake | Accumulation across dwells correct, identity left open? |
| `ais-161975-162025` | AIS | operator report | synthetic validated, field pending | GMSK/9600 hypothesis validated, not identified by frequency alone? |
| `fm-broadcast-96_5-104_5-operator-claims` | FM broadcast | operator report | pending intake | Do off-air checks support the claimed station identities? |

## Notes on each case

### NOAA weather radio (`noaa-wx-162400-425-494`) -- pending intake

162.400 / 162.425 / 162.494 MHz, previously cataloged under a single label "162.424". The open
questions: is speech actually present (NFM), and is msdr tuning the right one of the three
frequencies for each transmitter (the old single label conflates them).

### APRS (`aprs-144390`) -- pending intake

144.390 MHz, the North America APRS calling frequency, 1200 baud Bell 202 AFSK. Three
increasingly strong claims to distinguish, weakest to strongest: a tone/clock match consistent
with 1200 Bd AFSK; a frame that passes AX.25's 16-bit FCS; a fully parsed APRS payload. A live
capture should say which of the three msdr actually reaches, not just assert "APRS".

### Unidentified keyed carrier (`keyed-carrier-161098`) -- pending intake

161.098 MHz shows on/off keyed activity. Explicitly *not* assumed to be CW -- the corpus asks
only whether keyed activity is present and, if so, what can be said about it, rather than
defaulting every keyed carrier to a CW identification.

### 2 m repeater (`repeater-147060`) -- pending intake

Suspected frequency-mapping regression: an older catalog entry labeled this repeater 147.074,
current reporting says 147.060. Needs both the frequency-mapping bug checked and the
active/quiet voice state confirmed independently.

### Bursty land-mobile traffic (`land-mobile-bursty-160805-165278-162553`) -- pending intake

160.805 / 165.278 / 162.553 MHz show bursty land-mobile-style traffic across multiple dwells,
with gaps. The question is about msdr's own accumulation behavior (does it correctly combine
evidence across dwells and report "insufficient data" rather than a premature guess), not about
naming the operator on these frequencies -- identity is explicitly left open.

### AIS (`ais-161975-162025`) -- synthetic validated, field pending

161.975 / 162.025 MHz are the AIS channels by band-plan allocation. Before S4 the survey
analyzer reported `psk_like`/no baud on them -- **allocation is a hint, not a decode**, and this
corpus explicitly calls out that AIS must never be identified from frequency/allocation alone.
The bar to clear: a validated GMSK/9600-baud hypothesis with `valid_frames > 0`.

S4 (0.29.0) status: **synthetic-validated, field validation pending.** The analyzer now
classifies a synthetic GMSK 9600 burst as `fm_fsk` at 9600 Bd (ifreq bimodality + frequency
zero-crossing grid; `tests/survey/test_features.py`, `tests/survey/test_analyzer.py`), with the
family "GMSK/FSK 9600 (AIS or packet)" -- a hypothesis, not an identity. The registry's 18th
decoder, `ais` (`msdr/decoder/pydecoders/ais.py`), decodes synthetic bursts with CRC-16/X.25
checked (`tests/decoder/test_ais.py`); AIS identity needs a CRC-valid frame with an MMSI. No
161.975/162.025 MHz capture is accessible yet, so nothing here is measured off-air.

### FM broadcast operator claims (`fm-broadcast-96_5-104_5-operator-claims`) -- pending intake

Nine claimed US West Coast stations across 96.5-104.5 MHz (KOIT, KLLC, KCRC, KSOL,
KBRG, KRBQ, KBLX, KCPD, KNBR), reported mono/pilotless. These are **operator claims to verify,
not ground truth**. None of the nine has a fixture in this repo; an RDS decode would corroborate only *that
something valid decoded*, not the claimed callsign (RDS has no station-name field).
