# Survey band scanning

Survey band scanning steps the live receiver across a configured frequency
range. The scan span follows the current SDRconnect sample rate. Set range,
dwell, overlap, and DSP peak sensitivity in the receiver's
Band scan quick setup. Peak sensitivity is the same setting used by Discover.
Scanner, live decoder, beacon monitoring, recording, replay, and cycling watch
ownership prevent conflicting receiver operations.

## Retuning and coverage

Each tune invalidates outstanding DSP work immediately. Queued spectrum and IQ
from before the boundary are rejected. After the receiver confirms its final
span, a short settling window precedes new capture. SDRconnect supplies no
hardware tune-generation tag: timestamp/queue boundaries and settling protect
against local stale work, but cannot prove the radio capture time of every
network frame.

Coverage distinguishes three facts: the receiver visited a range, usable
spectrum was captured, and DSP analysis completed. Visiting a range does not
prove that every signal was analyzed. The peak detector excludes the center
bin and two bins on either side (five FFT bins total). Reports expose usable
intervals and center exclusions; another overlapping capture can fill those
holes. Failed tunes, missing frames, disconnects, early stops, and unvisited
spans remain explicit. Scan comparisons use usable intervals when available.
Legacy reports retain their original, less detailed coverage information.

Pause freezes the remaining dwell; resume continues capture. Stop preserves
completed and partial findings and flushes queued persistence work. A restart
marks unfinished scans interrupted and incomplete rather than complete.
Only committed work can survive an abrupt process or machine failure.

## Findings and storage

The 64-row live display prioritizes active signals. Scan findings use the
complete collection, independently of that presentation limit.

New sessions append observations to normalized SQLite tables through a bounded
writer queue with its own database connection. Unchanged measurements are
aggregated into compact runs with first/last times, counts, and strength
statistics; meaningful evidence changes create new runs. Identical snapshots
are not repeatedly added. Compact history cannot reconstruct every original
frame; retained evidence and counts describe what was captured.

Writer backlog or failure is exposed as degraded capture. If the queue fills,
the scan waits with its pending findings retained while the live receiver keeps
running. Spectrum/history sampling can be incomplete during that wait. A normal
stop waits for accepted writes to finish; storage failures end the scan and
remain visible.

Existing JSON reports are not converted or truncated on read. Their observations
remain available through paginated history and full JSON export. Heavy report
reads and encoding run in a separate process, with a bounded legacy cache, so
large reports do not parse on the live receiver event loop.

## Reviewing the band

The report first loads compact frequency summaries. Timeline history loads one
page at a time. Periodic session refresh does not cancel or invalidate a pending
request for the selected report. Minimum peak SNR filters frequencies, finding
markers, timeline pages, and CSV export; a blank threshold shows all signals.
The threshold refers to each frequency's strongest recorded SNR, so its timeline
can still include weaker measurements. Delete one report or all saved reports
with confirmation; active scans must be stopped first. Full JSON export preserves
the whole report regardless of the display filter.

New scans can retain a stitched measured spectrum with typical and maximum-held
levels, finding markers, and compact time-frequency slices. Zoom and pan inspect
parts of the band; markers open the signal inspector. Frequency coordinates and
capture times belong to the measurements, and uncaptured regions and excluded
bins remain gaps. Spectrum levels are relative, not calibrated absolute power.
Older scans without retained spectrum show findings only; a spectrum cannot be
reconstructed from their peak labels.

## API

- `GET /api/survey/band-scan`: current progress and recent sessions.
- `GET /api/survey/band-scan/{id}/summary`: compact frequencies and coverage.
- `GET /api/survey/band-scan/{id}/observations?offset=0&limit=100`: history page;
  optional `frequency_hz` selects one frequency and `min_peak_snr_db` filters by
  session peak before pagination. Limit is at most 500.
- `GET /api/survey/band-scan/{id}/spectrum`: retained measured spectrum/slices,
  or an explicit unavailable result.
- `GET /api/survey/band-scan/{id}/export`: full saved JSON; the original
  `GET /api/survey/band-scan/{id}` remains compatible.
- `DELETE /api/survey/band-scan/{id}` or `DELETE /api/survey/band-scan`: delete
  one or all reports; requests including active scans return 409.
- `POST /api/survey/band-scan/start`: `start_hz`, `end_hz`, `span_hz`, `dwell_s`,
  and `overlap` (a fraction from 0 through less than 1).
- `POST /api/survey/band-scan/{pause,resume,stop}`: session controls.

Status broadcasts use the `survey_band_scan` websocket message.

### Live peak markers and speech activity

The live spectrum receives compact markers for every tracked peak, independently
of the 64-row rich-label limit. Marker anchors use the strongest measured FFT bin;
the centroid remains the signal frequency estimate. Center exclusion is off by default. Settings → Survey → Exclude receiver center
optionally excludes the five center bins for receivers with a DC spike; when enabled,
the excluded region is amber. Offset the receiver center to inspect a carrier there.

Signal measurements distinguish RF presence from sampled, tentative speech
classification. A fresh silence result shows Carrier only; unavailable or stale
voice analysis shows Carrier present · speech unknown. Speech only filters the
table using a three-second hold from the analysis timestamp, including brief
silence updates, while preserving all RF markers. Short speech may occur between
IQ analysis dwells and be missed. Voice analysis can be enabled from the filter.

DSP marker hold defaults to 15 seconds and can be set to Off, 5, 15, 30, or 60
seconds beside the spectrum. Live markers are bright with solid connectors; retained
markers fade with dashed connectors and show their last-seen age on hover. Retention
uses the last RF observation timestamp, does not extend carrier or speech activity,
and is cleared by retuning or clearing the survey.
