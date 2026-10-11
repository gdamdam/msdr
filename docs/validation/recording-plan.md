# Field recording plan — what still needs to be captured

Companion to `tests/corpus/manifest.json` / `docs/validation/corpus.md`. This
lists the off-air recordings that would close the outstanding validation
gates and how to make each one so it is usable as evidence.

Everything here is a *recording to make and check*, not a result. An operator
label (station name, "this is AIS") stays a claim until a decode or an
independent reference backs it up.

## 1. Recording conventions (apply to every capture below)

- **Use msdr's recorder (ARM → REC → STOP)** wherever possible. It writes
  SigMF `ci16_le` plus `recording.json` with `start_wall` per run, which is the
  only trustworthy capture-time source the replay path accepts. SDRconnect
  wavs are usable but their capture time is "unknown" by contract (never mtime).
- **Rate:** 2 MS/s for the VHF cases (8 MB/s → 10 min ≈ 4.8 GB). 8–10 MS/s
  only for the FM broadcast band (40 MB/s at 10 MS/s → 60 s ≈ 2.4 GB).
- **Keep targets off the exact centre** (DC spike) and inside the middle ~80 %
  of the span (filter roll-off). Recommended centres below already do this.
- **Gain:** set so the strongest signal in span does not clip. Note LNA state
  and gain; one deliberately clipped take is wanted as a negative control (§5).
- **Two sessions per case, on different days**, so one can be development and
  the other held-out (plan §5: split by session, never by adjacent windows).
- **Intake note per file** (plan §5, required before a case is promoted):
  path, sha256, format, rate, centre, capture time and its source, signal
  location (offset Hz + time range), quality issues (clipping, dropouts,
  off-channel), antenna/LNA/gain, independent reference, expected outcome.
  Record the measured centroid and the asserted channel frequency separately;
  do not "correct" a reported frequency.
- **Never put raw IQ in Git.** Files stay outside the repository;
  the manifest carries path + hash only.

## 2. Required VHF recordings

Three 2 MS/s captures cover every VHF case in the manifest.

### R1 — 160.7–162.7 MHz block (centre 161.700 MHz, 2 MS/s, 10–15 min, twice)

Covers, in one file: AIS 161.975/162.025, NOAA 162.400/162.425/162.494,
keyed carrier 161.098, land mobile 160.805 and 162.553.

| Case | What must be in the recording | Independent reference / expected |
|---|---|---|
| `noaa-wx-162400-425-494` | continuous NFM speech on at least one of the three; note which one is actually strong locally | the NWS transmitter list for the receiving location; the question is "is 162.424 a mislabel of 162.425 or 162.400?" — record what the carrier centroid measures |
| `ais-161975-162025` | ≥ 20 GMSK bursts on either channel (10 min is usually enough near busy shipping lanes) | run a second AIS decoder on the same file (AIS-catcher, gnuais, or SDRangel demodais) and compare MMSIs/positions; a CRC-valid MMSI that also appears in the reference decode is the only acceptable identity evidence. Also settles the SDRangel payload bit-order question |
| `keyed-carrier-161098` | several on/off cycles; note whether any audio/tones are present | none expected; outcome is "keyed carrier, modulation unknown" unless evidence appears |
| `land-mobile-bursty-160805-165278-162553` (two of three) | bursts separated by silence; ideally a stretch with *no* activity to test insufficient-data behaviour | none; identity stays open |

162.553 sits ~0.85 MHz from centre, near the usable edge. If it looks
attenuated, take a second file centred 162.200 MHz.

### R2 — 2 m block (centre 146.900 MHz, 2 MS/s, until a QSO is heard, twice)

Covers 147.060 repeater output and 144.390 is *not* in span — see R3.

| Case | What must be in the recording | Independent reference / expected |
|---|---|---|
| `repeater-147060` | at least one voice transmission and a quiet stretch | measure the carrier centroid: the question is whether the old 147.074 label was a mapping bug (carrier really at 147.060) or a different repeater. Repeater directory entry for the receiving area is the reference |

### R3 — APRS (centre 144.300 MHz, 2 MS/s, 5–10 min, twice)

144.390 sits 90 kHz above centre, clear of the DC spike.

| Case | What must be in the recording | Independent reference / expected |
|---|---|---|
| `aprs-144390` | ≥ 10 packets, mixed strong/weak | decode the same file with direwolf standalone; callsigns must match, and some should be visible on aprs.fi for that time window. Expected ladder: tone/clock hypothesis → valid AX.25 FCS → parsed APRS payload; record which rung each packet reaches |

### R4 — 165.278 MHz (centre 165.000 MHz, 2 MS/s, 10 min)

Only the third land-mobile frequency needs this; bursty, may be quiet for
long stretches. A quiet take is still useful (insufficient-data behaviour).

## 3. Required FM broadcast recording

### R5 — FM band (centre 100.500 MHz, 10 MS/s, 60–90 s, twice)

Span 95.5–105.5 MHz covers all nine claimed stations (96.5 … 104.5).

| Case | What must be in the recording | Independent reference / expected |
|---|---|---|
| `fm-broadcast-96_5-104_5-operator-claims` | ≥ 60 s so PS/PI repeat several times on every RDS carrier | US RDS PI codes are derived algorithmically from the callsign, so PI is an *independent* check of each claimed callsign (PS is operator-settable and is not). For each: PI decoded → callsign implied → matches claim / contradicts / no RDS |
| KNBR 104.5 "pilotless" | same file | check for absence of the 19 kHz pilot and confirm the pilotless RDS path (`tests/survey/test_rds.py` synthetic regression) works on real MPX; add as a separate field case |
| stereo WFM (ledger gate) | same file — pick any station with a pilot | first stereo fixture for the not-yet-implemented stereo path |

Watch for clipping: the FM band is the most likely place to overload the
front end. One clipped take is wanted anyway (§5), but label it.

## 4. Negative controls and robustness fixtures (plan §5, mandatory)

Short captures, one each, msdr recorder:

| Fixture | How | Tests |
|---|---|---|
| Noise floor | terminated antenna input or an empty allocation, 2 MS/s, 30 s | 0 decoder events, no protocol/station claim, "noise is not voice" |
| Clipped IQ | FM band or NOAA with gain deliberately too high | clipping/overload reported as capture quality, not as data |
| Adjacent strong channel | NOAA block with a weak target next to a strong one (R1 usually has this) | off-channel / masking behaviour |
| Rate or centre change mid-recording | ARM, REC, retune once, STOP → two runs in `recording.json` | multi-run timeline, run boundary, gap-of-unknown-length |
| Analog voice, not a protocol | any NFM voice (R1/R2 already provide) | "speech-like audio", never a decoder claim |
| Pre-roll event | trigger REC on a burst so pre-roll is populated | S5 acceptance: capture a pre-roll event, note it, restart, return to the interval |

## 5. HF (deferred gate — needs an HF antenna)

Not recordable until an HF antenna is connected. When it is, these are the
cheapest field cases for the HF decoders in the capability matrix (`cw`,
`rtty`, `wefax`, `navtex`, `wwv`; `dcf77`/`msf` are European and cannot be
received on the US West Coast — they stay fixture-only permanently and should be
listed that way in the validation panel).

| Decoder | Suggested source (receiving on the US West Coast) | Capture |
|---|---|---|
| `wwv` (AM) | WWV 5.000 / 10.000 / 15.000 MHz, WWVH on the same frequencies | AM, 2–3 min spanning a minute boundary |
| `wefax` (USB) | NMC Point Reyes radiofax, 4346 / 8682 / 12786 / 17151.2 kHz carrier (tune USB 1.9 kHz below); verify the current schedule | one full chart (~10 min) |
| `navtex` (USB, 518 kHz) | NMC Point Reyes 518 kHz, scheduled slots; verify the current schedule | one full bulletin |
| `cw` | 40 m / 20 m CW segments (7.000–7.060, 14.000–14.060 MHz) | 5 min of a clean QSO |
| `rtty` (USB) | W1AW digital bulletins (45.45 Bd) on 7.095 / 14.095 MHz per the ARRL schedule; verify | one bulletin |
| AM / SSB listening (S3 demodulators) | any SW broadcaster in the evening (9–10 MHz), 40 m SSB nets | 2 min each |

Independent references: WWV/WWVH time itself (decoded minute must match
`start_wall`), NMC chart headers, W1AW bulletin text as published.

## 6. Suggested order

1. Intake what exists: hash the DMR crop, run the dsd-fme smoke test, inspect
   the 15 MHz files (WWV or noise?). No antenna work needed.
2. R1 (one file closes or advances five cases, including AIS off-air).
3. R5 (FM band; also the first stereo fixture).
4. R3, R2, R4.
5. Negative controls (§5) — most fall out of R1/R5 takes.
6. Repeat R1/R3/R5 on a second day for the held-out split.
7. HF (§6) when an antenna exists.

After each recording: add/update the manifest case (`files[]` with sha256,
`status` → `located`), then write the expected outcome only after checking
the independent reference. Freeze quantitative thresholds only after this
intake is complete (plan §5).
