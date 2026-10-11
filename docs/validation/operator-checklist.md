# 🧪 MSDR operator validation checklist

**Target:** v0.40.0 · **Prepared:** 2026-09-28 · **Status:** validation in progress.

**Live progress:** the 2026-09-28 reception log (kept outside the repository) separates verified frames, plausible output, synchronization without a call, and channels with no observed transmission. The checklist below remains open until each row's full acceptance criteria are met.

Use this document to test application behavior and collect evidence of RF correctness. A working button, an installed decoder and an accurate decoded signal are three different results.

## Start here

1. Run **A–C** first: setup, receiver controls and basic Survey.
2. Run **F**: recording/replay.
3. Test available decoders in **G**, then maps and library workflows in **H–J**.
4. Collect the real-recording corpus in **K**.
5. Record failures with the template at the end. Do not mark an untested operation as passed.

**Suggested sessions:** a short functional smoke test first; a longer VHF/UHF listening session next; independent held-out sessions later. HF and unavailable protocols can remain explicitly untested.

**Result codes:** ✅ PASS · ◐ PARTIAL · ❌ FAIL · ⏸ BLOCKED · ➖ NOT APPLICABLE · ☐ NOT TESTED.

For each row, replace ☐ with a result and record its ID in your session notes. “No signal heard” is not evidence that a decoder failed or passed.

## Session record

| Field | Fill in |
|---|---|
| Session ID / date / timezone | |
| msdr version / commit | |
| macOS / Mac chip / memory | |
| Python environment | |
| Browser / window size | |
| SDRconnect version / receiver | |
| Antenna / receiving location | |
| Center frequency / VFO / sample rate | |
| Demodulation / filter / gain / LNA / AGC | |
| Recording output folder / free space | |
| Development or held-out test session | |
| Capture paths / reference decoder / screenshots | |

Record missing metadata as **unknown**. Preserve the original recordings; do not edit them to make predictions look correct. Keep large IQ files outside Git.

## A. Setup, startup and recovery

| ID | Operation | Expected result | Result |
|---|---|---|---|
| A01 | Start SDRconnect, select receiver, start playback, then start msdr. | Connection and receiver state become visible; spectrum updates. | ☐ |
| A02 | Start msdr while SDRconnect is closed, then open SDRconnect. | Library remains usable; msdr connects without crashing either application. | ☐ |
| A03 | With msdr running, close and reopen SDRconnect once. | Disconnect is visible; reconnect recovers. A delayed retry after server drop is expected. Save crash report if either app crashes. | ☐ |
| A04 | Connect with no SDR device selected; then select/start it. | No-device state is understandable; starting the device restores data. | ☐ |
| A05 | Open Setup and inspect decoder dependencies. | Missing tools are named. “Ready” is not represented as RF validation. | ☐ |
| A06 | Setup → Recordings → Recording output folder: enter a new absolute path or `~/...`, then Save. | Writable folder is accepted and survives restart; no application restart is needed for the setting itself. | ☐ |
| A07 | Try a relative path or an existing file as output folder. | Clear error; previous setting remains. | ☐ |
| A08 | Try changing folder while armed/recording, replaying, or capturing a validation example. | Change is refused with an explanation. Existing activity is not silently redirected. | ☐ |
| A09 | Return to an earlier output folder. | Files were not moved or deleted. Verify recordings in the selected folder can be listed again. | ☐ |
| A10 | Change an ordinary Setup setting, save, restart, and reopen Setup. | Saved value persists. Check Cmd/Ctrl-Z separately in I05. | ☐ |

## B. Frequency, audio and screen behavior

| ID | Operation | Expected result | Result |
|---|---|---|---|
| B01 | Enter `160.000` in the top frequency editor. | Tunes to **160 MHz = 160,000,000 Hz**. MHz display must not imply 160 million MHz. Confirm against SDRconnect. | ☐ |
| B02 | Try `137.9`, `137900k`, and `14.230 MHz`; then invalid text. | Valid inputs map to the intended frequency. Invalid input does not unexpectedly retune. | ☐ |
| B03 | Edit a frequency, cancel with Esc; try digit and step controls. | Cancel keeps the prior tuning. Steps match the selected increment. | ☐ |
| B04 | Tune from a spectrum click, waterfall click and Survey row. | VFO, receiver and selected-signal display agree; no systematic half-bin offset. | ☐ |
| B05 | Change center/span with keep-VFO on and off. | Behavior matches the option; distinguish VFO from capture center. | ☐ |
| B06 | Change NFM/AM/USB/WFM and bandwidth on appropriate signals. | Receiver follows; supported audio is intelligible with sensible settings. Record mismatches. | ☐ |
| B07 | Hover near each edge of spectrum/waterfall. | Tooltip stays near the pointer and remains readable within the viewport. | ☐ |
| B08 | Use a 1280×800 window, then a larger one. | Frequency, primary controls and signal view are usable; panels do not block one another or cause unwanted horizontal overflow. | ☐ |
| B09 | Open decoder details/modal, signal inspector and Setup in turn. | Foreground controls are clickable; close/Esc works; decode modal is not under another panel. | ☐ |
| B10 | Switch Nearby / Recent / Suggestions / Log. | Selected tab and contents agree; long names and empty states remain readable. | ☐ |

## C. Survey and evidence

| ID | Operation | Expected result | Result |
|---|---|---|---|
| C01 | Start, stop and restart Survey; clear its results. | Status reflects each action; no stale rows presented as a new session. | ☐ |
| C02 | Enable IQ analysis dimensions and watch a strong steady signal. | Measurements arrive; units and evidence method are distinguishable. | ☐ |
| C03 | Watch intermittent bursts across multiple dwells. | Activity accumulates sensibly; quiet periods and last-seen age are visible. | ☐ |
| C04 | Observe a known digital signal that previously appeared as voice. | Record DSP family/voice output and audio evidence. A confident incorrect voice claim is a failure; “unknown” is acceptable uncertainty. | **Regression repaired on saved IQ:** 152.879 MHz stable DMR sections now read data; independent field checks remain. See reception log. |
| C05 | Sort/filter by evidence, frequency and SNR. | Ordering/filtering is consistent; filtering does not accidentally change sorting. | ☐ |
| C06 | Compare table evidence badges with spectrum markers and tooltips. | DSP, allocation, schedule and decode evidence remain distinguishable. A catalog match does not prove reception. | ☐ |
| C07 | Inspect a signal; tune, save and open decoded output where available. | Actions refer to the selected signal and preserve observation/evidence context. | ☐ |
| C08 | Change span/rate during analysis; compare SDRconnect sampling with observed IQ status. | New coordinates replace old ones; sample-rate/throughput warnings are understandable. Configured rate and delivered throughput need not be equal. | ☐ |
| C09 | Watch quiet spectrum, an isolated FFT spike and a suspected DC/receiver spur. | Save false detections and ambiguous cases, not only successes. Do not call every narrow carrier a spur. | ☐ |
| C10 | Run Scan frequencies and inspect a hit. | Scan controls work; result leads to its associated evidence/recording when one exists. | ☐ |

## F. Recordings, replay and investigation

Use a disposable test recording for editing/clipping tests. Keep original corpus recordings unchanged.

| ID | Operation | Expected result | Result |
|---|---|---|---|
| F01 | ARM, allow pre-roll, REC, then STOP. | State transitions are clear; recording appears under `<output folder>/recordings`. Check duration and metadata. | ☐ |
| F02 | Open the saved recording and replay it. | Spectrum/timeline agree with recorded content; playback controls work. | ☐ |
| F03 | Pause, resume, seek and loop a region. | Position is consistent; old analyzer results are invalidated when appropriate. | ☐ |
| F04 | Listen/tune within replay where supported. | Replay operations do not retune the live receiver. Unsupported actions are explicit. | ☐ |
| F05 | Start a replay-supported decoder on a known recording. | Messages correspond to the recorded interval. Raw-IQ-only live decoders are not expected to work in replay. | ☐ |
| F06 | Add a note/bookmark and return to it after restart. | Timestamp, recording reference and note survive. | ☐ |
| F07 | Create and replay a clip from a test recording. | Boundaries match the requested interval; original remains intact. Default clips go under the output folder unless a separate clips directory is configured. | ☐ |
| F08 | Stop SDRconnect during a test recording. | Recording ends or reports source loss/gaps clearly; no invented uninterrupted capture. | ☐ |
| F09 | Review a signal with several observations. | Earlier measurements/claims remain historical evidence when today’s identification changes. | ☐ |
| F10 | Analyze recording / inspect report. | Input, time/frequency context and output are linked; failure is reported rather than an empty success. | ☐ |

## G. Decoder validation

For **each installed decoder you can receive**, test: selection → start → output → stop → restart → retune → missing/quiet input. Record dependency readiness, reception conditions and independently checked output separately.

Use decoder help/readiness for its exact input requirements. Single-channel raw IQ and band/audio decoding are different paths. A decoder being intentionally disabled for an incompatible path is not a defect; unexplained inability to start the supported path is.

| ID | Decoder / operation | Evidence needed for a pass | Result |
|---|---|---|---|
| G01 | ADS-B / Mode S: live single-channel IQ, supported rate ≥2.4 MS/s, target inside span. | Reports agree with an independent decoder on the same input; coordinate-bearing reports map correctly. BaseStation output does not expose per-message CRC: do not call it CRC-verified. | ◐ Live output/map seen; same-IQ reference comparison pending |
| G02 | RS41: known radiosonde signal. | Accepted CRC-checked frames; coherent identity/position/altitude compared with a reference decode. Missing error correction remains a limitation. | ☐ |
| G03 | APRS / packet1200. | Parsed AX.25/APRS messages with exposed integrity distinguished from merely hearing Bell-202 tones. Compare exact payloads. | ◐ Live decoding reconfirmed 2026-10-03; earlier FCS-passing frame; independent payload comparison pending |
| G04 | AIS on the two known channels. | Valid payloads and consistent vessel fields/positions; no identity claim from frequency alone. | ◐ 107 CRC-valid live messages; same-IQ reference comparison pending |
| G05 | FT8: USB, `jt9` available, IQ rate an integer multiple of 12 kHz and >12 kHz. | Compare accepted messages over several 15-second UTC periods with WSJT-X; note timing and missed messages. | ◐ 13 recorded-reference messages; live timing untested |
| G06 | DSC: suitable HF/VHF profile and a known signal/fixture. | Compare framing, MMSI, category and any position/time fields against independent output. No coordinate marker unless position was actually decoded. | ◐ Two-minute watch; no DSC call received |
| G07 | End-of-Train. | Valid CRC-18 packets, repeatable fields and independent reference. RF validation remains pending until captured. | ☐ |
| G08 | DMR / P25 / NXDN / D-Star / YSF / dPMR / M17, individually. | Standalone dependency works; protocol/identity fields match a reference. Audio only where supported. Mark each unavailable protocol untested. | ◐ DMR live Capacity Plus control, calls and operator-confirmed voice audio on 2026-10-03; independent comparison and complete-call audio quality pending. Earlier NXDN sync was recorded with dsd-fme auto mode, which cannot detect NXDN, so NXDN48/96 remain unverified. P25 candidates gave no sync; other modes untested |
| G09 | POCSAG / FLEX. | Repeatable messages/fields versus reference; corruption is not presented as verified text. | ◐ POCSAG live decoding reconfirmed after 2026-10-03 repair; no exposed integrity flag; FLEX untested |
| G10 | Packet9600 / packet300. | Known packets decoded at the correct mode/rate; compare payload and exposed checks. | ☐ |
| G11 | CW / RTTY / DTMF / EAS. | Known text, tone sequence or recorded alert matches output. Use recordings for alert fixtures; no transmission is required. | ◐ CW audio and decoder text confirmed live 2026-10-03; independent transcription pending. SAME watched ten minutes without alert; DTMF/EAS untested (RTTY has separate recorded-reference evidence). |
| G12 | WEFAX / NAVTEX. | Known image/text matches a reference; timing, inversion and framing problems are documented. | ☐ |
| G13 | WWV / DCF77 / MSF. | Time/code fields agree with the independently known recording time; absence of local reception is not a failure. | ☐ |
| G14 | RDS analyzer. | PI/PS/PTY agree with a reference; report received PS text separately from assumed station identity. | ◐ Live RDS groups/text; independent PI/PS/PTY comparison pending |
| G15 | VOR / ILS estimators. | Compare tentative measurements with a known fixture/reference. These are not certified navigation outputs and must not be used for navigation. | ☐ |
| G16 | Band decoding with supported narrowband decoders. | Multiple eligible channels produce correctly attributed output; raw-IQ single-channel-only decoders remain clearly excluded. | ☐ |
| G17 | Discover decoded-output view and Listen decoder strip/modal. | New output appears in the right session with frequency/time, survives selection changes as expected, and is readable. | ☐ |

**Not shipped:** FreeDV and DAB/DAB+. Meshtastic/MeshCore are not part of this release’s promised validation scope. For current implementation limits see [decoder scope](../decoder-expansion.md), [FT8](../ft8.md) and [DSC](../dsc.md).

## H. Discover Map / Table

| ID | Operation | Expected result | Result |
|---|---|---|---|
| H01 | Switch Map ↔ Table, filter layers and select an event. | Selection/filter context agrees; original decoded evidence is accessible. | ☐ |
| H02 | Inspect ADS-B, RS41, APRS and maritime position events when available. | Only usable decoded coordinates make markers; non-position messages remain in Table. | ☐ |
| H03 | Inspect aging events/tracks. | Age/unknown position age is visible; gaps do not fabricate continuous travel. | ☐ |
| H04 | Save a memory with both latitude and longitude; then remove both. | Marker appears/disappears appropriately; reference location is not reception evidence. | ☐ |
| H05 | Inspect HF catalog/repeater reference positions. | Only explicit coordinates are mapped; a country or station name alone does not invent a location. | ☐ |
| H06 | Disable online basemap. | Markers and Table remain usable without background map tiles. | ☐ |
| H07 | Tune from a located memory; try the corresponding action during replay. | Live tuning works; replay does not silently retune hardware. | ☐ |

## I. Library, search and undo

| ID | Operation | Expected result | Result |
|---|---|---|---|
| I01 | Save current signal, edit name/notes/tags, add to a list and favorite it. | Values persist and refer to the correct frequency/profile. | ☐ |
| I02 | Use Cmd/Ctrl-K, Nearby and tune history Back/Forward. | Correct result is selected; receiver/profile restoration is consistent. | ☐ |
| I03 | Use catalog suggestions and schedule/timezone filters. | Scheduled candidates remain tentative, even when no signal is present. | ☐ |
| I04 | Save and restore a receiver profile. | Supported settings restore; unsupported/failed settings are reported. | ☐ |
| I05 | Undo a tuning change, library edit and ordinary Setup change separately. | Most recent supported operation is reversed. Received evidence/recordings are not erased. Text editing retains appropriate undo behavior. | ☐ |
| I06 | Export library; import into a disposable test database. | Counts and key fields agree; working library is not overwritten during the test. | ☐ |

## J. Satellites, optional workflows and endurance

| ID | Operation | Expected result | Result |
|---|---|---|---|
| J01 | Set receiving location, refresh TLEs, inspect a pass. | Timezone, orbit-data age and azimuth/elevation are understandable; compare against nextpass for the same location, epoch and TLEs. | ☐ |
| J02 | Start/stop tracking during an appropriate pass or test scenario. | Doppler changes apply as intended; stop ends automatic retuning. | ☐ |
| J03 | Start/stop beacon monitoring when applicable. | Events identify the correct session/frequency; missing reception is not labeled a successful beacon decode. | ☐ |
| J04 | Run your normal Survey + audio + decoder combination for 30–60 minutes. | Record memory trend, CPU, dropped IQ/jobs, audio gaps and UI responsiveness. Memory/queues should not grow without bound. | ☐ |
| J05 | Restart msdr after the endurance session. | Library/settings/recordings survive; no duplicate runaway decoder processes or abandoned active state. | ☐ |

## K. Collect the RF evidence

Start with **20–30 short examples across several sessions**. This is a pilot dataset, not enough to claim general reliability.

| Capture group | What to include | Labeling rule |
|---|---|---|
| NOAA voice | Strong and weak speech; use the actual received channel | Confirm voice by listening; do not copy an old frequency label blindly. |
| APRS 144.390 | Several packets plus quiet gaps | Protocol confirmed only with a successful checked decode/reference. |
| AIS 161.975 / 162.025 | Bursts, neighboring energy and quiet gaps | Compare decoded payloads; allocation alone is not truth. |
| Reported 152.475 / 152.879 digital activity | Clear and weak segments | “Digital, protocol unknown” until independently established. |
| Keyed carrier / unknown bursts | On/off intervals and neighboring spectrum | Preserve uncertainty; keying alone is not CW identification. |
| Noise / suspected spurs | Quiet intervals, DC, adjacent-channel leakage, brief impulses | Independently review; a weak real signal must not be mislabeled noise. |
| HF later | Broadcast/utility examples at varied SNR | Separate HF results from the VHF/UHF claim. |

Keep development and held-out test recordings from genuinely separate sessions. Do not split adjacent windows from one transmission across those sets. Use a reference decoder on the **same recording**, not a different live reception. Note capture drops, gain, antenna, clipping and sample format. Store file hashes when assembling the corpus.

## Failure / result template

Copy one block for each failure or interesting success:

```text
Test ID and result:
Session ID / version / commit:
Expected:
Observed:
Exact steps to reproduce:
Frequency / center / sample rate / mode / bandwidth:
Antenna / gain / source (live or replay):
Decoder / backend / thresholds:
Recording path and time interval:
Screenshot / log / crash-report path:
Independent reference / confidence in the operator label:
Reproducible after restart?:
Impact: blocks listening / wrong evidence / lost data / cosmetic / other
```

Prioritize crashes, lost recordings, wrong tuning and confidently wrong evidence. Keep a separate list of unavailable signals/dependencies so “not tested” never disappears into the pass count.
