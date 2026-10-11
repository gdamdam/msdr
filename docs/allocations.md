# Frequency allocation reference

Contract and as-built reference for the regulatory frequency-allocation layer (`msdr/allocations/`); for contributors and anyone consuming the allocation API.

Status: contract and as-built reference for 0.53.0. The shapes below are the API; the "Coverage and limitations" section is filled in from the real import output, never from assumptions.

## Why a separate layer

Allocation data answers "what is this band legally allocated for, in which jurisdiction, with what priority". It is regulatory context, not evidence that a particular transmitter is on the air. It therefore lives in its own package (`msdr/allocations/`), separate from:

- station catalogs (`msdr/catalog/`, who is scheduled where),
- signal identification scoring (`msdr/providers/`, ranked candidates), and
- the supplemental channel / application layer (`msdr/providers/data/bandplan_us.json`,
  named channels such as NOAA WX2 and application notes such as DME or
  oceanographic radar).

Allocation membership depends only on frequency and jurisdiction. Demodulator and filter bandwidth never influence it.

## Sources (verified 2026-10-01)

| id | What it provides | Publisher / licence | Format | Edition seen |
|---|---|---|---|---|
| `ntia_table` | Full US Table of Frequency Allocations: ITU Region 1/2/3 columns, US Federal, US Non-Federal, FCC rule parts, with primary / secondary and footnote references | NTIA Manual (Redbook) ch. 4, US government work, public domain | HTML grid at `https://redbook.ntia.gov/json/preRenderedTableOfAllocations-from-xml.html` (undocumented endpoint behind the `redbook.ntia.gov/view/4-17` SPA) | 2025 edition |
| `ecfr_2_106` | Footnote text for international (5.xxx), US (USxxx), non-federal (NGxxx) and federal (Gxxx) footnotes, plus the authoritative amendment date | 47 CFR §2.106 via eCFR versioner API, public domain | XML, gzip required: `https://www.ecfr.gov/api/versioner/v1/full/{date}/title-47.xml?part=2&section=2.106`; versions list at `.../versions/title-47.json?part=2` | latest amendment 2026-09-29 |
| `ntia_chart` | Official wall chart, linked and dated, never parsed | NTIA | PDF, 268 KB: `https://www.ntia.gov/sites/default/files/2025-09/ntia-us-frequency-allocations.pdf` | September 2025 |
| `itu_rr` | ITU Radio Regulations Article 5 | ITU, free PDF "for personal use", redistribution terms not verified | PDF / Word only | 2024 |

Facts that shape the design:

- The eCFR XML of §2.106 contains the allocation table only as 68 scanned GIF
  images. Band edges and services are therefore imported from the NTIA HTML,
  footnote prose from eCFR, and both are cited.
- §2.100 states the International Table in §2.106 reflects the Radio
  Regulations edition of 2016. ITU Region 1/2/3 data shipped here is that
  reproduction, labelled as such, not the 2024 Regulations.
- ITU RR 2024 is not bundled. A user who has the right to use it can import a
  normalized JSON file through the same importer (`--from-file`), validated by
  the same rules.
- §90.103 (radiolocation, oceanographic radar at 5250–5275 kHz) is an
  application rule, not an allocation. It is carried in the supplemental layer
  with its citation.

## Data model (`msdr/allocations/model.py`)

All records are plain dataclasses with `to_dict()`; the JSON forms below are
the API contract.

```jsonc
// Service
{"name": "MOBILE", "qualifier": "except aeronautical mobile",
 "priority": "primary" | "secondary", "footnotes": ["5.132A"], "raw": "MOBILE except aeronautical mobile 5.132A"}

// Band (one row of one column)
{"lo_hz": 5250000, "hi_hz": 5275000,
 "column": "itu_r1" | "itu_r2" | "itu_r3" | "us_federal" | "us_non_federal",
 "services": [Service...],            // every service in the cell, in table order
 "footnotes": ["US340"],              // cell-level footnotes not bound to one service
 "rule_parts": ["Maritime (80)"],     // us_non_federal only
 "unallocated": false}                 // true when the cell says "(Not allocated)" or is empty by rule

// Footnote
{"id": "5.132A", "text": "...", "source_id": "ecfr_2_106", "kind": "international" | "us" | "non_federal" | "federal"}

// Source status
{"id": "ntia_table", "title": "...", "publisher": "NTIA", "url": "...",
 "edition": "2025", "effective_date": "2025-09-10" | null,
 "imported_at": "2026-10-01T18:00:00Z", "origin": "bundled" | "imported",
 "lo_hz": 8300, "hi_hz": 3000000000000, "band_count": 0, "footnote_count": 0,
 "status": "ok" | "error", "error": null | "...", "stale": false,
 "provenance": {"fetched_url": "...", "sha256": "...", "importer_version": "1"}}
```

Jurisdictions: `US`, `ITU1`, `ITU2`, `ITU3`. `US` resolves against the two US
columns and attaches `itu_r2` as international context. `ITUn` resolves
against `itu_rn` only and has no national block.

### Lookup result

```jsonc
{"frequency_hz": 5264000, "jurisdiction": "US",
 "status": "ok" | "unallocated" | "not_covered" | "unsupported_jurisdiction" | "source_unavailable",
 "message": "...",                       // precise wording for every non-ok status
 "lo_hz": 5250000, "hi_hz": 5275000,     // band containing the frequency (national if present, else international)
 "national": {                           // null for ITU jurisdictions
   "federal":     {"services": [Service...], "footnotes": [...]},
   "non_federal": {"services": [Service...], "footnotes": [...], "rule_parts": [...]},
   "shared": true},                       // more than one service across the two columns
 "international": {"region": "ITU2", "lo_hz": ..., "hi_hz": ..., "services": [...], "footnotes": [...]},
 "footnotes": [Footnote...],             // resolved text for every id referenced above, deduplicated
 "citations": [{"source_id": "ntia_table", "title": "...", "url": "...", "edition": "2025", "effective_date": "..."}],
 "disagreements": [{"kind": "footnote_text", "id": "US340", "sources": ["ntia_table", "ecfr_2_106"]}],
 "sources": {"stale": false, "statuses": {"ntia_table": "ok", "ecfr_2_106": "ok"}}}
```

Boundary rule:

- A band covers `[lo_hz, hi_hz)`; the top band of a column also includes its `hi_hz`.
- A lookup exactly on a shared edge returns the upper band.
- Tests cover both sides of an edge and the top edge.

Status meanings:

| `status` | Meaning |
|---|---|
| `ok` | At least one service in the resolved column(s). |
| `unallocated` | The band exists and is explicitly not allocated. |
| `not_covered` | Frequency is outside the loaded data's `[lo_hz, hi_hz]`. |
| `unsupported_jurisdiction` | Jurisdiction string not in the four above. |
| `source_unavailable` | No dataset could be loaded at all (bundled file missing or corrupt and no valid import). Last-known-good data prevents this in normal operation. |

## Store (`msdr/allocations/store.py`)

`AllocationStore(data_dir: Path | None = None)`

- Loads the bundled dataset from `msdr/allocations/data/` via `importlib.resources`.
- If `data_dir` (default `~/Library/Application Support/msdr/allocations/`) holds a valid `dataset.json` whose `generated_at` is not older than the bundled one, it is merged **per column**:
  - every column the overlay contains replaces the bundled column; columns it lacks stay bundled;
  - footnotes are unioned (overlay ids win);
  - `sources()` lists the sources of both files, each with its own `origin`.
- A partial ITU RR import therefore switches only `itu_r1/2/3` and leaves US lookups on the bundled table.
- Bands keep their `source_id`, and a lookup cites only the sources of the bands and footnotes it actually returned.
- The overlay is validated per column over its own extent, with footnotes resolved against bundled + overlay.
- An invalid, unreadable or older file is reported in `sources()` as `{"id": "dataset_file", "status": "error", "url": <path>}` and ignored (last-known-good).
- `lookup(hz: int, jurisdiction: str) -> dict` (shape above), bisect on sorted band edges per column, no I/O, no network.
- `bands(lo_hz, hi_hz, jurisdiction, column=None) -> list[dict]` for the chart
  and the textual alternative.
- `sources() -> list[dict]`, `reload() -> None`.

## Importer (`msdr/allocations/importer.py`, `python -m msdr.allocations.importer`)

- `fetch_ntia_table()`, `parse_ntia_table(html) -> list[Band]`
- `fetch_ecfr_section(date=None, section="2.106")`, `parse_ecfr_footnotes(xml) -> list[Footnote]`
- `validate(bands, footnotes) -> list[str]`: every column contiguous from table
  start to end with no gaps or overlaps, every referenced footnote id
  resolves, every service parsed to a known priority, record counts above a
  floor. Any error aborts the import.
- Footnote ids the table references but eCFR lacks are filled from the NTIA
  chapter 4 footnote page (`resolve_missing_footnotes`): `source_id:
  "ntia_footnotes"`, `fallback: true`, `missing: false`, listed under a third
  source `ntia_footnotes`. eCFR text is never replaced. An id with no text
  anywhere is kept as `text: null, missing: true`.
- `build_dataset(...) -> dict` with `sources`, `bands`, `footnotes`,
  `generated_at`, `schema_version`.
- `write_atomic(path, dataset)`: temp file + fsync + rename; previous file kept
  as `dataset.prev.json`.
- CLI flags: `--out`, `--ecfr-date`, `--ntia-url`, `--from-file <normalized.json>`
  (user-supplied data, e.g. ITU RR, in the dataset shape), `--check` (validate
  only). `--from-file` validates per column over the file's own extent:
  contiguity, no overlaps, known priorities, and footnote resolution against
  the union of the file's and the bundled footnotes; the 2,000-band floor does
  not apply, so a file carrying only `itu_r1/2/3` is accepted and the store
  then replaces just those columns. The bundled dataset is produced by this
  CLI and committed with its provenance; regenerating it is the update
  procedure.
- Never called per lookup. Called from the Settings refresh action and the CLI.

## Backend integration

Settings:

| Setting | Type | Default |
|---|---|---|
| `allocation_jurisdiction` | `"US" \| "ITU1" \| "ITU2" \| "ITU3"` | `"US"` |
| `allocation_data_dir` | `Path \| None` | `None` |

`region` keeps its current meaning for identification.

`msdr/api/services/allocations.py` (`AllocationServiceMixin`):

- `allocation_lookup(hz, jurisdiction=None)`, `allocation_bands(...)`, `allocation_sources()`.
- `allocation_refresh(source_id)`: importer in a thread, atomic replace, reload, never raises, returns status.
- `allocation_context(hz)`: lookup plus supplemental `applications` and `channels` from the band-plan file matched by frequency containment only (`kind: "band"` entries containing `hz`, `kind: "channel"` entries within 5 kHz).
- The band-plan file is US-specific, so these are filled only when the effective jurisdiction is `US`. ITU jurisdictions get empty lists and `"supplemental": {"available": false, "reason": "Channel and application hints are US-specific"}` (US: `{"available": true, "reason": null}`), which the signal panel and tune readout show in place of the hints block.

Routes:

| Route | Purpose |
|---|---|
| `GET /api/allocations/lookup?hz=&jurisdiction=` | Full lookup result |
| `GET /api/allocations/bands?lo_hz=&hi_hz=&jurisdiction=&column=` | Bands in a range |
| `GET /api/allocations/sources` | Source statuses |
| `POST /api/allocations/refresh {source}` | Refresh a source |
| `GET /api/catalog/sources` | Also lists allocation sources with `kind: "allocation"` |
| `POST /api/catalog/refresh` | Routes allocation ids to `allocation_refresh`, so a catalog refresh updates allocation data |

Consumers:

- Consumers switch from the first band-plan candidate to the full lookup:
  - signal detail evidence (`kind: "allocation"`, all services);
  - survey labels (`label["allocation"]` compact summary with every service; `label["bandplan"]` stays for saved-report compatibility and now means channel / application hint);
  - band-scan results copy `allocation`;
  - scanner finds classification and notes list every service;
  - observation claims record all services.
- Existing `/api/identify` keeps returning band-plan candidates; the band-plan provider no longer adjusts allocation ranking by demodulator or bandwidth.

## UI

- `<allocation-summary>`: compact line with band edges, jurisdiction and all
  services with priority; expandable Federal / Non-Federal detail, footnote
  texts, citations with edition and effective date, international context,
  stale badge; status-specific empty states using `message`; "Open chart"
  action. Keyboard operable, responsive.
- Used by the signal panel, the live tune readout (requests carry a sequence
  token so a late response never overwrites a newer one), the survey table
  and band-scan report.
- Settings: jurisdiction select and an "Allocation sources" group showing
  edition, effective date, last successful update, range, status and Refresh.
- `<allocation-chart>` view (`allocations` in `app.js`): rows per decade like
  the NTIA chart, bands coloured by service family, zoom and pan, frequency
  jump, tuned-frequency marker, click-to-inspect using the same lookup
  records, a text list alternative, and a dated link to the official NTIA PDF
  with a notice that the generated chart differs from the publication.

## Update procedure

Bundled dataset (release time):

```
python -m msdr.allocations.importer --out msdr/allocations/data/dataset.json
python -m msdr.allocations.importer --check msdr/allocations/data/dataset.json
```

Commit the regenerated dataset together with the provenance block it carries
and update the "Coverage and limitations" numbers below from the importer's
log.

At runtime, Settings → Allocation sources → Refresh (`POST /api/allocations/refresh {source}`; `POST /api/catalog/refresh` with an allocation source id does the same):

1. Runs the same network importer in a thread (`build_allocation_dataset`) and validates the result.
2. Writes it atomically to `<allocation_data_dir>/dataset.json` (default `~/Library/Application Support/msdr/allocations/`, previous file kept as `dataset.prev.json`) and reloads the store. The bundled file under `msdr/allocations/data/` is never written.
3. On failure the refresh never raises: an importer error (offline, parser failure, validation failure) is returned as `{"ok": false, "error": ...}` with the current `sources`, nothing is written, and the previously loaded dataset keeps answering (last-known-good).
4. On the next start the store finds the user file and merges it per column over the bundled one, provided it validates and its `generated_at` is not older than the bundled file's; otherwise it is listed under `sources()` as `dataset_file` / `error` and ignored.

Changing `allocation_data_dir` in Settings rebuilds the store from the new directory.

A user-supplied file (for example ITU RR 2024 normalized to the dataset
shape) goes through `python -m msdr.allocations.importer --from-file
<normalized.json> --out <allocation_data_dir>/dataset.json`; its columns then
replace only the columns it carries.

## Jurisdiction precedence

1. National table for the selected national jurisdiction (US: Federal and
   Non-Federal shown side by side, never merged). `lo_hz`/`hi_hz` of the
   result are the intersection of the Federal and Non-Federal bands.
2. International table for the matching ITU region shown as context, never as
   a substitute for national rules (`US` attaches `itu_r2`; `ITUn` resolves
   against `itu_rn` only, `national` is `null`).
3. Supplemental channel / application entries, labelled separately, US only
   (see `allocation_context` above).

Data precedence between files is per column, not per jurisdiction:

- For each of the five columns the user-dir dataset wins when it carries that column; the bundled dataset answers otherwise.
- Footnotes are unioned with the user file's ids winning.
- A lookup therefore cites exactly the sources of the bands and footnotes it returned, which can mix an imported ITU column with the bundled US columns.

## Coverage and limitations

Facts from the bundled import of 2026-10-02 (`msdr/allocations/data/dataset.json`,
1,836,379 bytes, `schema_version` 1), produced by
`python -m msdr.allocations.importer --out msdr/allocations/data/dataset.json`.

### Summary

- Range: every column covers 0 Hz – 3000 GHz contiguously (the table's own
  first row is 0–8.3 kHz NOT ALLOCATED, its last 275–3000 GHz NOT ALLOCATED);
  anything outside returns `not_covered`.
- Counts: 2,670 bands (`itu_r1` 544, `itu_r2` 544, `itu_r3` 538, `us_federal`
  501, `us_non_federal` 543), 6,396 services, 976 footnotes (960 from eCFR
  §2.106, 16 fallback texts from the NTIA footnote page), 143 bands marked
  `unallocated`.
- The 16 fallback footnotes (`5.79A 5.87A 5.329A 5.418A 5.418B 5.418C 5.448A
  5.448B 5.448C 5.448D 5.547A 5.547B 5.547C 5.547D 5.547E US115`) are absent
  from the eCFR XML text and carry `source_id: "ntia_footnotes"`,
  `fallback: true`. No referenced id is left without text.
- 271 footnote-text wording disagreements between eCFR and the NTIA footnote
  page are recorded and surfaced per lookup; none is resolved automatically.
- ITU Region 1/2/3 columns are the Radio Regulations **2016** edition as
  reproduced in the US table (§2.100), not the current ITU RR 2024.
- ITU RR 2024 is not bundled: it is published as PDF/Word only, the ITU
  offers it free "for personal use" and its redistribution terms have not
  been verified, so no copy or parse of it ships. A user with the right to
  use it can import a normalized JSON file (`--from-file`), which replaces
  only the ITU columns.
- The NTIA wall chart (September 2025 PDF) is linked and dated in the chart
  view, never parsed; the generated chart is derived from the table data and
  differs from the publication.
- Licences: the NTIA Manual table and footnote pages and the eCFR text of 47
  CFR §2.106 are US government works (public domain). The supplemental
  band-plan file cites 47 CFR rules (e.g. §90.103) per entry. ITU material is
  not redistributed for the reason above.
- The ITU columns are only as current as the US table's reproduction, and
  the US table itself (`ntia_table`) carries no effective date of its own in
  the grid HTML; `ecfr_2_106`'s amendment date (2026-09-29) is the only
  authoritative date, and `stale` is set once an import is older than 365
  days.

### Sources as fetched

| id | fetched | size | sha256 (prefix) | edition / date |
|---|---|---|---|---|
| `ntia_table` | `preRenderedTableOfAllocations-from-xml.html`, HTTP `Last-Modified` 2026-09-16 | 1,688,237 B | `23e14282ed17cc66` | edition `2025` (from the Redbook SPA's CSS); `effective_date` is `null` because the grid HTML carries no date of its own |
| `ntia_footnotes` | `preRenderedChapter4Footnotes.html` (disagreement check, and fallback text for ids eCFR lacks) | 3,361,480 B | `0a7163be6f9137a5` | 978 footnotes, edition `2025` |
| `ecfr_2_106` | versioner `full/2026-09-29/title-47.xml?part=2&section=2.106` | 561,679 B | `dd041c99153e7b6c` | amendment date 2026-09-29 = latest in the `versions` list (91 versions) |

Both publishers cut long transfers early through some proxies; the fetcher resumes with `Range` requests (on the gzip representation for eCFR, which returns 406 without `Accept-Encoding: gzip`).

### Grid parsing

- The grid is a CSS grid of 1,968 `.allocation-cell` divs, 709 rows × 6
  columns. `grid-column` 1-3 = ITU Region 1/2/3, 4 = US Federal, 5 = US
  Non-Federal, 6 = FCC rule parts; `grid-row` is the band row. A cell spanning
  several columns (e.g. `1 / 4`, `4 / 6`) yields one band per column with
  identical content, so a lookup can legitimately show Federal and Non-Federal
  as identical. 1,637 cells carry band labels; the 331 rule-part cells have no
  label and are joined to Non-Federal bands by grid-row overlap.
- Table start is **0 Hz** (first row "0-8.3 kHz", explicitly NOT ALLOCATED),
  end 3000 GHz ("275-3000 GHz", NOT ALLOCATED). Every column is contiguous
  over `[0, 3 THz]` with no gaps and no overlaps; the importer fails rather
  than bridging a gap.
- Band counts: `itu_r1` 544, `itu_r2` 544, `itu_r3` 538, `us_federal` 501,
  `us_non_federal` 543; 2,670 bands, 6,396 services (5,501 primary, 895
  secondary), 53 distinct qualifiers, 38 distinct rule-part strings on 416
  Non-Federal bands. Floor enforced at import: 2,000 bands.
- `unallocated: true` on 143 bands: 10 from explicit "NOT ALLOCATED" (the
  two end rows), 133 empty cells (95 Federal, 42 Non-Federal, e.g. Federal at
  535-1605 kHz) which the table leaves blank; these keep their footnotes.
- Service grammar found in the 2025 grid: ALL CAPS = primary, Title Case =
  secondary; footnotes always parenthesised after the service
  (`RADIOLOCATION (G129, G2)`); qualifier = everything from the first `(` or
  from `except` on. The grid title-cases the clause ("MOBILE Except
  Aeronautical Mobile"); the stored qualifier is lower-cased up to the first
  parenthesis (`except aeronautical mobile (R)`), parenthesised parts keep
  case (`(Earth-to-space)`, `(R)`, `(20 kHz)`).
- Cells the parser could not handle: none (`parser_problems` is empty).

### Footnotes

- eCFR §2.106(b)-(e) yields 960 footnotes: 668 international (5.xxx), 199 US,
  62 NG, 31 G. Continuation paragraphs, "Note n to §2.106…" paragraphs and
  tables are appended to the footnote they follow (tables flattened as
  `cell | cell` rows).
- The table references 975 distinct ids; 959 resolve in eCFR and **16 are
  absent from eCFR**: `5.79A 5.87A 5.329A 5.418A 5.418B 5.418C 5.448A 5.448B
  5.448C 5.448D 5.547A 5.547B 5.547C 5.547D 5.547E US115`. The fifteen 5.xxx
  ids do not appear in the eCFR text; `US115` is present but printed as
  "US1157" in the eCFR XML (paragraph (c)(115)), so it is not matched (the
  orphan `US1157` is stored as parsed and referenced by nothing). All 16 take
  their text from the NTIA footnote page (`source_id: "ntia_footnotes"`,
  `fallback: true`). **Unresolved ids: 0**; `missing: true` is never set in
  the bundled dataset. 976 footnotes in total: 960 eCFR + 16 fallback.
- 271 footnote-text disagreements between eCFR and the NTIA footnote page are
  recorded in `disagreements` (182 international, 62 US, 21 NG, 6 G) and
  surfaced by `lookup()` for the ids it returns. Comparison ignores case,
  whitespace, punctuation, WRC markers, short paragraph markers and citation
  style ("§ x of this chapter" vs "47 CFR x"); what remains is wording: RR
  2024 vs the 2016 reproduction (e.g. 5.118 "Mexico and Peru" vs "Mexico,
  Peru and Uruguay", 5.162A "North Macedonia" vs "The Former Yugoslav
  Republic of Macedonia", "frequency band" vs "band") and eCFR-only editorial
  notes. Nothing is resolved automatically.

### Store behaviour fixed by this import

- A `dataset.json` in `data_dir` is merged per column (see Store) only when
  it passes validation and its `generated_at` is not older than the bundled
  file's; an unreadable, invalid or older file is listed in `sources()` as
  `{"id": "dataset_file", "status": "error", "url": <path>, "error": ...}`
  and ignored.
- `stale` is set per source when its `imported_at` is older than 365 days.
- `--check` additionally verifies the declared `lo_hz`/`hi_hz`/`band_count`
  of each source against the bands in the file.

See also: [reference catalogs](reference-catalogs.md), [catalog sources](catalogs.md).
