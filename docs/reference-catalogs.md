# Reference catalogs: NIST time stations, FAA navaids, IOOS HF radar, SuperDARN

Design contract and measured coverage for the NIST, FAA, IOOS and SuperDARN reference catalogs; for contributors working on `msdr/catalog/`.

Status: design contract for 0.53.1.

- Station listings stay in the catalog layer (`msdr/catalog/`), separate from regulatory allocations (`msdr/allocations/`, see `docs/allocations.md`) and from the supplemental band-plan hints.
- The "Coverage" section at the end is filled in from real import output.

## Sources (verified 2026-10-01)

| id | Network | Mode | Format and URL | Reuse |
|---|---|---|---|---|
| `nist_time` | WWV, WWVH, WWVB | dated bundled snapshot, hand-reviewed | HTML prose and tables at `https://www.nist.gov/pml/time-and-frequency-division/time-distribution/radio-station-{wwv,wwvh,wwvb}`; no structured feed; outage pages per year | NIST: public information, may be copied (byline requested) |
| `faa_nasr` | VOR, VOR/DME, VORTAC, DME, TACAN, NDB, ILS localizers | refreshable download, 28-day cycle, plus a dated bundled snapshot of the full extract | `https://nfdc.faa.gov/webContent/28DaySub/<DD>_<Mon>_<YYYY>_NAV_CSV.zip` and `..._ILS_CSV.zip` (about 650 KB and 730 KB); `NAV_BASE.csv`, `ILS_BASE.csv`; layout in `NAV_CSV_DATA_STRUCTURE.csv`; cycle 2026-10-01, next 2026-10-29 | US government work; no explicit statement on the page, recorded as such |
| `ioos_hfradar` | IOOS / HFRNet coastal HF radar sites | refreshable download | `https://hfradar.ioos.us/hfrnet/sitediag/stationList.php?output=CSV` (Station, Station Name, Network, Frequency MHz, Bandwidth, SweepRate, Date Added, Last Radial File, Latitude, Longitude, Manufacturer); ERDDAP `https://hfradar.ioos.us/erddap/tabledap/allDatasets.csv` for platform names and data coverage | free use and redistribution, no warranty |
| `superdarn` | SuperDARN radars | dated bundled snapshot of hardware tables, refreshable from GitHub raw | `https://github.com/SuperDARN/hdw` `hdw.dat.<code>` (station id, geographic lat/lon, boresight, beams, gates, valid-from date); names and institutions from `https://superdarn.ca/radar-info` (server-rendered) | repo has no licence; site is "all rights reserved"; radar data needs a PI agreement. Only facts (identifiers, coordinates) are stored, with that caveat |

Facts that shape the design:

- WWV: 2.5, 5, 10, 15, 20 MHz; 25 MHz is on air "on an experimental basis"
  and may be suspended without notice. WWVH: 2.5, 5, 10, 15 MHz only. WWVB:
  60 kHz, pulse-width AM plus BPSK since 2012, no msdr decoder. Each WWV
  frequency has its own antenna coordinates. The existing band-plan entries
  "WWV/WWVH 20 MHz" and "WWV/WWVH 25 MHz" are wrong for WWVH and are removed
  from `bandplan_us.json` in favour of this catalog; the shared 2.5–15 MHz
  entries are relabelled "WWV / WWVH (time signal)".
- FAA: NAV_BASE gives decimal NAD83 coordinates, `FREQ` in MHz for VOR
  family and kHz for NDB, `NAV_STATUS` such as "OPERATIONAL RESTRICTED". DME
  and TACAN carry a channel, not a VHF frequency: those records get no
  `frequency_hz`. ILS is a separate file; localizer frequency is `LOC_FREQ`,
  glideslope lives in `ILS_GS.csv` and is out of scope. Real rows: SFO
  VOR/DME 115.8, OAK VOR/DME 116.8, PYE 113.7, SAU 116.2, SFO localizers
  108.9 (19L), 109.55 (28L), 111.7 (28R).
- IOOS: exact nominal frequency per site is published (e.g. ACI1 13.475 MHz,
  AGL1 4.4505 MHz). "Last Radial File" is a data-availability date, never a
  transmission indicator. Radar type comes from `Manufacturer` (CODAR
  SeaSonde, WERA).
- SuperDARN: no frequency field in any primary source. The 8–22 MHz band is
  only stated by secondary sources and is recorded as unverified in notes,
  not as a range field. No current-frequency feed exists for any radar.

## Data model

`CatalogEntry` (pydantic, `msdr/catalog/models.py`) and the
`catalog_entries` table gain nullable columns via migration
`019_catalog_reference.sql` (table rebuild because `frequency_hz` becomes
nullable; indexes recreated; existing rows unchanged):

| field | meaning |
|---|---|
| `ref_type` | `schedule` (existing broadcast rows, default), `station`, `navaid`, `radar_site` |
| `network` | "NIST", "FAA NASR", "IOOS HFRNet", "SuperDARN" |
| `station_id` | stable source identifier: "WWV", "SFO", "ACI1", "bks" |
| `component` | sub-identity: "25 MHz", "VOR", "DME", "LOC 28R", "north array" |
| `operator` | institution or operator string from the source |
| `frequency_hz` | now nullable: the verified fixed or nominal frequency, or null |
| `freq_lo_hz`, `freq_hi_hz` | documented operating range, when the source states one |
| `current_freq_hz`, `current_freq_at` | timestamped current frequency; none of the four sources provide it, so null, but the model and UI support it |
| `status` | `operational`, `operational_restricted`, `experimental`, `data_recent`, `unknown`, `shutdown`, source wording kept in `notes` |
| `status_at` | timestamp the status refers to (FAA `EFF_DATE` or `COMPONENT_STATUS_DATE`, IOOS Last Radial File) |
| `source_url`, `edition` | record-level citation and dataset edition or cycle date |
| `decoder` | msdr decoder id only when verified for the component: `wwv` for WWV/WWVH, `vor` for VOR family with a VHF frequency, `ils` for localizers; null for NDB (no decoder), WWVB, DME/TACAN and all radars |

Derived in the API, never stored: `tunable = frequency_hz is not None`,
`distance_km` when a receiver location is configured.

### Stable ids

Entries keep `id` across refreshes by deriving it (uuid5 in a fixed namespace) from `(source, station_id, component, place)`.

- `place` is the `location` text when present ("CITY, ST", "Angel Island, CA"), else latitude/longitude rounded to 4 decimals (about 10 m), else empty.
- The place is part of the key because short source identifiers repeat nationally: the 2026-10-01 FAA cycle has 33 `(ident, type)` pairs used twice (NDB "AA" is both Fargo ND and Thomson GA), and without it one row replaced the other.
- Location text is preferred over coordinates so a survey correction does not churn ids.
- A refresh upserts and marks rows missing from the new dataset as removed (deleted from the source table). User annotations and saved signals referencing them are untouched because those live in `signals`, not in `catalog_entries`.

## On-air states

`on_air()` currently returns True for any entry without a schedule window. That is right for broadcast schedules and wrong for reference records. New `on_air_state(entry, at)` returns one of:

- `continuous`: `ref_type == "station"` with `status in (operational, experimental)` (WWV, WWVH, WWVB are continuous services),
- `scheduled_on` / `scheduled_off`: `ref_type == "schedule"` by the existing window logic,
- `published_active`: `ref_type == "navaid"` with `status` operational or operational_restricted,
- `unknown`: radar sites, missing or stale status, shutdown.

`on_air()` becomes `on_air_state(...) in ("continuous", "scheduled_on",
"published_active")`, so LISTEN presets and the on-air filter exclude radar
sites and unknowns. The UI shows the state word, not a generic "on air".

## Importers (`msdr/catalog/parsers/`)

- `nist_time.py`: parses `msdr/catalog/data/nist_time_stations.json`, a
  reviewed snapshot with `snapshot_date` and per-record `source_url`; one
  entry per station per frequency; mode `AM` for WWV/WWVH, null for WWVB;
  WWV 25 MHz `status: experimental`.
- `faa_nasr.py`: `current_cycle_date(today)` from the 2026-10-01 epoch and
  28-day period; downloads the two CSV zips, parses NAV_BASE and ILS_BASE,
  maps units by `NAV_TYPE`, drops nothing silently (DME/TACAN keep null
  frequency with the channel in `component`/`notes`); a dated bundled
  snapshot (`msdr/catalog/data/faa_nasr_<cycle>.json.gz`) is produced by the
  same parser so browsing works offline.
- `ioos_hfradar.py`: parses the station list CSV (and ERDDAP names when
  reachable); `frequency_hz` from Frequency MHz, `status: data_recent` with
  `status_at` = Last Radial File when within 30 days else `unknown`; mode and
  decoder null.
- `superdarn.py`: parses bundled `hdw.dat.*` snapshot plus a names table;
  no frequency fields; notes carry the licence caveat and the unverified band.

## Persistence and refresh

- All refreshes go through the existing `replace_source` path, which is atomic per source and keeps the previous dataset on failure.
- Network fetches run in a thread with a bounded timeout and size cap.
- A refresh that raises, or parses to zero rows, reports `error` and leaves the source unchanged.
- A refresh that parses successfully but yields fewer than half of the rows the source currently holds is also refused when the source has at least 20 rows (a truncated download or a format change can parse to a plausible-looking stub). The response carries `kept_previous: true` and `error: "refresh produced N rows, previous dataset has M; refusing to shrink by more than half (download may be truncated)"`.
- The rule applies to every source (URL/text parsers, bundled static tables and the FAA/IOOS fetchers) through one helper, `CatalogServiceMixin._store_refresh`.

## API and UI

- `/api/catalog/search` accepts `network`, `station_id`, `ref_type`,
  `near=lat,lon` and `sort=distance`; results include `on_air_state`,
  `tunable`, `distance_km`.
- `/api/catalog/sources` rows gain `kind: "static" | "refreshable" | "live"`
  and `coverage` text; the Setup → Catalog UI shows that word, the edition,
  the last successful refresh, counts and errors.
- Catalog view: filters by network and station id, a "Nearby" sort using the
  configured receiver location (disabled with an explanation when no
  location is set), fixed frequency, range and timestamped current frequency
  rendered distinctly, Tune only when `tunable`, decoder chips only from the
  `decoder` field, state word from `on_air_state`. Requests carry a sequence
  token so a slow response cannot replace a newer selection.

## Coverage

Measured from the bundled snapshots on 2026-10-01/02 (the counts below are
asserted by `tests/catalog/test_parsers_*.py`). Reception of any listed
station is not implied: a catalog row says what is published, not what a
given receiver can hear.

### `nist_time` (static, 12 records)

- Hand-reviewed snapshot `nist_time_stations.json`, `snapshot_date`
  2026-10-02. WWV 2.5/5/10/15/20 MHz plus 25 MHz (`experimental`), WWVH
  2.5/5/10/15 MHz, WWVB 60 kHz as two rows (north and south antenna
  arrays). WWV/WWVH: mode AM, decoder `wwv`; WWVB: mode and decoder null.
- Verified: frequencies, powers, and per-frequency antenna coordinates for
  every WWV and WWVH frequency and both WWVB arrays, read from the complete
  station pages fetched on 2026-10-02 (the earlier truncated fetch left WWVH
  without coordinates; that gap is closed). Status is the snapshot date,
  there is no live outage feed.
- Update: edit the JSON by hand from the NIST pages, bump `snapshot_date`,
  then Setup → Catalog → Refresh `nist_time` (no network).

### `faa_nasr` (refreshable, 3,173 records in cycle 2026-10-01)

- NAV_BASE: 1,617 navaids: VOR/DME 412, VORTAC 382, NDB 367, DME 163,
  TACAN 155, VOT 78, VOR 27, NDB/DME 21, FAN MARKER 11, MARINE NDB 1.
  ILS_BASE: 1,556 localizers (`LOC RWY <end>`, decoder `ils`).
- Units: VOR family and VOT `FREQ` in MHz, NDB family in kHz, both stored
  as Hz; DME and TACAN keep their channel in `component` and no
  `frequency_hz` (untunable); FAN MARKER has no frequency convention and is
  stored without one. Localizers are separate rows from the VOR of the same
  ident (SFO has VOR/DME 115.8, VOT 111.0 and LOC RWY 28L 109.55).
- Status vocabulary in this cycle: "OPERATIONAL IFR" (1,994) and
  "OPERATIONAL VFR ONLY" (16) map to `operational`, "OPERATIONAL
  RESTRICTED" (1,157) to `operational_restricted`, "SHUTDOWN" (6) to
  `shutdown`; there is no bare "OPERATIONAL" value. The raw wording is kept
  in `notes`. `status_at` is `EFF_DATE` (NAV) or `COMPONENT_STATUS_DATE`
  (ILS).
- Owner strings are normalised to "FAA" for the FAA spellings only; the
  military variants ("U.S. AIR FORCE", "US AIR FORCE", "U.S AIR FORCE") are
  kept as published.
- Download URL: the real files live under `28DaySub/extra/`, not the
  `28DaySub/` path the FAA index suggests (`nav_csv_url()` /
  `ils_csv_url()` encode this). Cycle: `current_cycle_date(today)` from the
  2026-10-01 epoch in 28-day steps (2026-10-15 → 2026-10-01, 2026-10-29 →
  2026-10-29); the bundled `faa_nasr_2026-10-01.json.gz` is the offline
  fallback for a first load without network, after which a failed download
  reports an error and keeps the previous cycle's rows.
- Bay Area rows: SFO VOR/DME 115.8 (San Francisco), OAK VOR/DME 116.8
  (Oakland), PYE VOR/DME 113.7 (Point Reyes), SAU VOR/DME 116.2
  (Sausalito), all `operational_restricted`; SFO localizers 108.9 (19L,
  ident SIA), 109.55 (28L, ident SFO), 111.7 (28R, ident GWQ).
- Update: Setup → Catalog → Refresh `faa_nasr` downloads the two ZIPs for
  the current cycle (20 MB cap each, 30 s timeout).

### `ioos_hfradar` (live, 184 sites in snapshot 2026-10-02)

- Station list CSV: 184 sites, CODAR SeaSonde 157, WERA 14, "U. of Hawaii
  GHFDR" 13 (unrecognised manufacturer string kept as published). Status
  from "Last Radial File": 155 `data_recent` (within 30 days of retrieval),
  29 `unknown`; it is a data-availability date, never a transmission
  indicator, and the row notes say so.
- Verified: `Frequency` is MHz (magnitudes 4.45–43.7 MHz match the
  worked examples ACI1 13.475 MHz, AGL1 4.4505 MHz). Rejected: the
  `Bandwidth` column as a range source (negative values and a `-9999`
  sentinel on about 90% of rows), so `freq_lo_hz`/`freq_hi_hz` stay null.
  ERDDAP institution names are a best-effort enrichment in `notes`.
- Bay Area row: ANGL "Angel Island, CA", CODAR, 41.34 MHz, 37.8533 N
  122.4192 W.
- Update: Setup → Catalog → Refresh `ioos_hfradar` downloads the station
  list (plus ERDDAP when reachable); a first load without network uses the
  bundled snapshot, a later failed or empty download keeps the previous rows.

### `superdarn` (static, 49 radars)

- Snapshot `superdarn_2026-10-01.json` from the `hdw.dat.*` hardware
  tables and the radar-info page: 49 radars, 47 with a name and institution
  joined from radar-info (`ekb`, `tst` keep their code as the name). Latest
  hardware configuration per radar: coordinates, boresight, beams, gates.
- No frequency fields anywhere in the primary sources; the 8–22 MHz band is
  recorded in `notes` as unverified, not as a range. Status is always
  `unknown`, mode and decoder null. Licence is unclear (repository without a
  licence, site all rights reserved): only identifiers and coordinates are
  stored, and the caveat is in every row's notes.
- Update: regenerate the snapshot with `build_snapshot()` from fresh
  `hdw.dat` files and the radar-info page, then Refresh `superdarn`.

See also: [catalog sources](catalogs.md), [allocations](allocations.md).
