# Frequency allocation reference

msdr includes a regulatory frequency-allocation layer (`msdr/allocations/`). Given a frequency and a jurisdiction, it reports what the band is allocated for, with priority, footnotes and citations. This page is for users who want to know where the data comes from and how to refresh it, and for developers using the allocation API.

Allocation data is regulatory context. It does not show that a particular transmitter is on the air. It lives in its own package, separate from:

- station catalogs (`msdr/catalog/`, who is scheduled where),
- signal identification scoring (`msdr/providers/`, ranked candidates),
- the supplemental channel and application layer (`msdr/providers/data/bandplan_us.json`), which holds named channels such as NOAA WX2 and application notes such as DME or oceanographic radar.

Allocation membership depends only on frequency and jurisdiction. Demodulator and filter bandwidth never influence it.

## Using the data

- The signal panel, the live tune readout, the survey table and the band-scan report show an allocation summary line for the tuned or detected frequency. Expand it for Federal and Non-Federal detail, footnote texts, citations and international context.
- The **Allocations** chart view shows the whole table (see [UI](#ui)).
- Settings has a jurisdiction select and an "Allocation sources" group. For each source it shows edition, effective date, last successful update, range and status, with a **Refresh** button. See [Updating the data](#updating-the-data).

## Sources (verified 2026-10-01)

| id | What it provides | Publisher / licence | Format | Edition seen |
|---|---|---|---|---|
| `ntia_table` | Full US Table of Frequency Allocations: ITU Region 1/2/3 columns, US Federal, US Non-Federal, FCC rule parts, with primary/secondary and footnote references | NTIA Manual (Redbook) ch. 4, US government work, public domain | HTML grid at `https://redbook.ntia.gov/json/preRenderedTableOfAllocations-from-xml.html` (an undocumented endpoint behind the `redbook.ntia.gov/view/4-17` SPA) | 2025 edition |
| `ecfr_2_106` | Footnote text for international (5.xxx), US (USxxx), non-federal (NGxxx) and federal (Gxxx) footnotes, plus the authoritative amendment date | 47 CFR §2.106 via the eCFR versioner API, public domain | XML, gzip required: `https://www.ecfr.gov/api/versioner/v1/full/{date}/title-47.xml?part=2&section=2.106`; versions list at `.../versions/title-47.json?part=2` | latest amendment 2026-09-29 |
| `ntia_chart` | Official wall chart, linked and dated, never parsed | NTIA | PDF, 268 KB: `https://www.ntia.gov/sites/default/files/2025-09/ntia-us-frequency-allocations.pdf` | September 2025 |
| `itu_rr` | ITU Radio Regulations Article 5 | ITU, free PDF "for personal use", redistribution terms not verified | PDF / Word only | 2024 |

What this means in practice:

- The eCFR XML of §2.106 contains the allocation table only as 68 scanned GIF images. Band edges and services are therefore imported from the NTIA HTML and footnote prose from eCFR, and both are cited.
- §2.100 states that the International Table in §2.106 reflects the Radio Regulations edition of 2016. The ITU Region 1/2/3 data shipped here is that reproduction, labelled as such, not the 2024 Regulations.
- ITU RR 2024 is not bundled. A user who has the right to use it can import a normalized JSON file through the same importer (`--from-file`), validated by the same rules.
- §90.103 (radiolocation, oceanographic radar at 5250-5275 kHz) is an application rule, not an allocation. It is carried in the supplemental layer with its citation.

## Data model (`msdr/allocations/model.py`)

All records are plain dataclasses with `to_dict()`. The JSON forms below are the API shapes.

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

### Jurisdictions

The jurisdictions are `US`, `ITU1`, `ITU2` and `ITU3`.

- `US` resolves against the two US columns and attaches `itu_r2` as international context.
- `ITUn` resolves against `itu_rn` only and has no national block.

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

Band boundaries:

- A band covers `[lo_hz, hi_hz)`. The top band of a column also includes its `hi_hz`.
- A lookup exactly on a shared edge returns the upper band.
- Tests cover both sides of an edge and the top edge.

| `status` | Meaning |
|---|---|
| `ok` | At least one service in the resolved column(s). |
| `unallocated` | The band exists and is explicitly not allocated. |
| `not_covered` | The frequency is outside the loaded data's `[lo_hz, hi_hz]`. |
| `unsupported_jurisdiction` | The jurisdiction string is not one of the four above. |
| `source_unavailable` | No dataset could be loaded at all (bundled file missing or corrupt, and no valid import). Last-known-good data prevents this in normal operation. |

### Jurisdiction precedence

1. **National table** for the selected national jurisdiction. For the US, Federal and Non-Federal are shown side by side, never merged. The result's `lo_hz` and `hi_hz` are the intersection of the Federal and Non-Federal bands.
2. **International table** for the matching ITU region, shown as context and never as a substitute for national rules.
3. **Supplemental channel and application entries**, labelled separately, US only (see `allocation_context` under [Backend integration](#backend-integration)).

Precedence between data files is per column, not per jurisdiction:

- For each of the five columns, the user-directory dataset wins when it carries that column. Otherwise the bundled dataset answers.
- Footnotes are unioned, with the user file's ids winning.
- A lookup cites exactly the sources of the bands and footnotes it returned, which can mix an imported ITU column with the bundled US columns.

## Store (`msdr/allocations/store.py`)

`AllocationStore(data_dir: Path | None = None)` loads the bundled dataset from `msdr/allocations/data/` via `importlib.resources`.

If `data_dir` (default `~/Library/Application Support/msdr/allocations/`) holds a valid `dataset.json` whose `generated_at` is not older than the bundled one, it is merged **per column**:

- Every column the overlay contains replaces the bundled column. Columns it lacks stay bundled.
- Footnotes are unioned (overlay ids win).
- `sources()` lists the sources of both files, each with its own `origin`.
- A partial ITU RR import therefore switches only `itu_r1/2/3` and leaves US lookups on the bundled table.
- Bands keep their `source_id`, and a lookup cites only the sources of the bands and footnotes it returned.
- The overlay is validated per column over its own extent, with footnotes resolved against bundled plus overlay.
- An invalid, unreadable or older file is reported in `sources()` as `{"id": "dataset_file", "status": "error", "url": <path>, "error": ...}` and ignored (last-known-good).
- `stale` is set per source when its `imported_at` is older than 365 days.

Methods:

- `lookup(hz: int, jurisdiction: str) -> dict` (shape above): bisect on sorted band edges per column, with no I/O and no network.
- `bands(lo_hz, hi_hz, jurisdiction, column=None) -> list[dict]`, for the chart and its textual alternative.
- `sources() -> list[dict]`.
- `reload() -> None`.

## Importer (`msdr/allocations/importer.py`)

Run with `python -m msdr.allocations.importer`. It is never called per lookup, only from the Settings refresh action and the CLI.

- `fetch_ntia_table()` and `parse_ntia_table(html) -> list[Band]`.
- `fetch_ecfr_section(date=None, section="2.106")` and `parse_ecfr_footnotes(xml) -> list[Footnote]`.
- `validate(bands, footnotes) -> list[str]` checks that:
  - every column is contiguous from table start to end, with no gaps or overlaps;
  - every referenced footnote id resolves;
  - every service parses to a known priority;
  - record counts are above a floor.

  Any error aborts the import.
- `resolve_missing_footnotes`: footnote ids the table references but eCFR lacks are filled from the NTIA chapter 4 footnote page, with `source_id: "ntia_footnotes"`, `fallback: true`, `missing: false`, listed under a third source `ntia_footnotes`. eCFR text is never replaced. An id with no text anywhere is kept as `text: null, missing: true`.
- `build_dataset(...) -> dict` returns `sources`, `bands`, `footnotes`, `generated_at` and `schema_version`.
- `write_atomic(path, dataset)` writes a temp file, fsyncs and renames. The previous file is kept as `dataset.prev.json`.

CLI flags:

| Flag | Meaning |
|---|---|
| `--out` | output path |
| `--ecfr-date` | eCFR version date |
| `--ntia-url` | NTIA table URL |
| `--from-file <normalized.json>` | import user-supplied data (for example ITU RR) in the dataset shape |
| `--check` | validate only; also verifies each source's declared `lo_hz`, `hi_hz` and `band_count` against the bands in the file |

`--from-file` validates per column over the file's own extent: contiguity, no overlaps, known priorities, and footnote resolution against the union of the file's and the bundled footnotes. The 2,000-band floor does not apply, so a file carrying only `itu_r1/2/3` is accepted, and the store then replaces just those columns.

The bundled dataset is produced by this CLI and committed with its provenance. Regenerating it is the update procedure.

## Backend integration

Settings:

| Setting | Type | Default |
|---|---|---|
| `allocation_jurisdiction` | `"US" \| "ITU1" \| "ITU2" \| "ITU3"` | `"US"` |
| `allocation_data_dir` | `Path \| None` | `None` |

`region` keeps its current meaning for identification. Changing `allocation_data_dir` in Settings rebuilds the store from the new directory.

`msdr/api/services/allocations.py` (`AllocationServiceMixin`) provides:

- `allocation_lookup(hz, jurisdiction=None)`, `allocation_bands(...)` and `allocation_sources()`.
- `allocation_refresh(source_id)`: runs the importer in a thread, replaces the file atomically, reloads, never raises, and returns a status.
- `allocation_context(hz)`: the lookup plus supplemental `applications` and `channels` from the band-plan file, matched by frequency containment only (`kind: "band"` entries containing `hz`, `kind: "channel"` entries within 5 kHz).

The band-plan file is US-specific, so the supplemental data is filled only when the effective jurisdiction is `US`. For ITU jurisdictions, the lists are empty and the result carries `"supplemental": {"available": false, "reason": "Channel and application hints are US-specific"}` (for US: `{"available": true, "reason": null}`). The signal panel and tune readout show that in place of the hints block.

Routes:

| Route | Purpose |
|---|---|
| `GET /api/allocations/lookup?hz=&jurisdiction=` | Full lookup result |
| `GET /api/allocations/bands?lo_hz=&hi_hz=&jurisdiction=&column=` | Bands in a range |
| `GET /api/allocations/sources` | Source statuses |
| `POST /api/allocations/refresh {source}` | Refresh a source |
| `GET /api/catalog/sources` | Also lists allocation sources, with `kind: "allocation"` |
| `POST /api/catalog/refresh` | Routes allocation ids to `allocation_refresh`, so a catalog refresh updates allocation data |

Consumers use the full lookup rather than the first band-plan candidate:

- signal detail evidence (`kind: "allocation"`, all services);
- survey labels (`label["allocation"]` is a compact summary with every service; `label["bandplan"]` stays for saved-report compatibility and now means a channel or application hint);
- band-scan results, which copy `allocation`;
- scanner finds, whose classification and notes list every service;
- observation claims, which record all services.

`/api/identify` still returns band-plan candidates. The band-plan provider no longer adjusts allocation ranking by demodulator or bandwidth.

## UI

- `<allocation-summary>`: a compact line with band edges, jurisdiction and all services with priority. It expands to Federal / Non-Federal detail, footnote texts, citations with edition and effective date, international context and a stale badge. Each status has its own empty state, using `message`. It has an "Open chart" action, is keyboard operable and is responsive.
- It is used by the signal panel, the live tune readout (requests carry a sequence token so a late response never overwrites a newer one), the survey table and the band-scan report.
- Settings: the jurisdiction select and the "Allocation sources" group, showing edition, effective date, last successful update, range, status and Refresh.
- `<allocation-chart>` view (`allocations` in `app.js`): rows per decade like the NTIA chart, with bands coloured by service family. It has zoom and pan, a frequency jump, a tuned-frequency marker, click-to-inspect using the same lookup records, a text list alternative, and a dated link to the official NTIA PDF. A notice says the generated chart differs from the publication.

## Updating the data

### Bundled dataset (at release time)

```
python -m msdr.allocations.importer --out msdr/allocations/data/dataset.json
python -m msdr.allocations.importer --check msdr/allocations/data/dataset.json
```

Commit the regenerated dataset together with the provenance block it carries, and update the numbers in [Coverage and limitations](#coverage-and-limitations) from the importer's log.

### At runtime

Use Settings → Allocation sources → **Refresh** (`POST /api/allocations/refresh {source}`; `POST /api/catalog/refresh` with an allocation source id does the same).

1. The same network importer runs in a thread (`build_allocation_dataset`) and validates the result.
2. The result is written atomically to `<allocation_data_dir>/dataset.json` (default `~/Library/Application Support/msdr/allocations/`, previous file kept as `dataset.prev.json`) and the store reloads. The bundled file under `msdr/allocations/data/` is never written.
3. A failed refresh never raises. An importer error (offline, parser failure, validation failure) is returned as `{"ok": false, "error": ...}` with the current `sources`. Nothing is written, and the previously loaded dataset keeps answering.
4. On the next start, the store finds the user file and merges it per column over the bundled one, provided it validates and its `generated_at` is not older than the bundled file's. Otherwise it is listed under `sources()` as `dataset_file` / `error` and ignored.

### Your own data (for example ITU RR 2024)

Normalize the data to the dataset shape, then run:

```
python -m msdr.allocations.importer --from-file <normalized.json> --out <allocation_data_dir>/dataset.json
```

Its columns replace only the columns it carries.

## Coverage and limitations

Facts from the bundled import of 2026-10-02 (`msdr/allocations/data/dataset.json`, 1,836,379 bytes, `schema_version` 1), produced by `python -m msdr.allocations.importer --out msdr/allocations/data/dataset.json`.

### Summary

- **Range:** every column covers 0 Hz to 3000 GHz contiguously. The table's first row is 0-8.3 kHz NOT ALLOCATED and its last is 275-3000 GHz NOT ALLOCATED. Anything outside returns `not_covered`.
- **Counts:** 2,670 bands (`itu_r1` 544, `itu_r2` 544, `itu_r3` 538, `us_federal` 501, `us_non_federal` 543), 6,396 services, 976 footnotes (960 from eCFR §2.106, 16 fallback texts from the NTIA footnote page), and 143 bands marked `unallocated`.
- **Fallback footnotes:** the 16 ids are `5.79A 5.87A 5.329A 5.418A 5.418B 5.418C 5.448A 5.448B 5.448C 5.448D 5.547A 5.547B 5.547C 5.547D 5.547E US115`. They are absent from the eCFR XML text and carry `source_id: "ntia_footnotes"`, `fallback: true`. No referenced id is left without text.
- **Wording disagreements:** 271 footnote-text disagreements between eCFR and the NTIA footnote page are recorded and surfaced per lookup. None is resolved automatically.
- **ITU columns:** Region 1/2/3 are the Radio Regulations **2016** edition as reproduced in the US table (§2.100), not the current ITU RR 2024.
- **ITU RR 2024 is not bundled.** It is published as PDF/Word only, the ITU offers it free "for personal use", and its redistribution terms have not been verified, so no copy or parse of it ships. A user with the right to use it can import a normalized JSON file (`--from-file`), which replaces only the ITU columns.
- **NTIA wall chart:** the September 2025 PDF is linked and dated in the chart view and never parsed. The generated chart is derived from the table data and differs from the publication.
- **Licences:** the NTIA Manual table and footnote pages and the eCFR text of 47 CFR §2.106 are US government works (public domain). The supplemental band-plan file cites 47 CFR rules (for example §90.103) per entry. ITU material is not redistributed, for the reason above.
- **Dates:** the ITU columns are only as current as the US table's reproduction. The US table (`ntia_table`) carries no effective date of its own in the grid HTML, so the `ecfr_2_106` amendment date (2026-09-29) is the only authoritative date. `stale` is set once an import is older than 365 days.

### Sources as fetched

| id | fetched | size | sha256 (prefix) | edition / date |
|---|---|---|---|---|
| `ntia_table` | `preRenderedTableOfAllocations-from-xml.html`, HTTP `Last-Modified` 2026-09-16 | 1,688,237 B | `23e14282ed17cc66` | edition `2025` (from the Redbook SPA's CSS); `effective_date` is `null` because the grid HTML carries no date of its own |
| `ntia_footnotes` | `preRenderedChapter4Footnotes.html` (disagreement check, and fallback text for ids eCFR lacks) | 3,361,480 B | `0a7163be6f9137a5` | 978 footnotes, edition `2025` |
| `ecfr_2_106` | versioner `full/2026-09-29/title-47.xml?part=2&section=2.106` | 561,679 B | `dd041c99153e7b6c` | amendment date 2026-09-29 = latest in the `versions` list (91 versions) |

Both publishers cut long transfers early through some proxies. The fetcher resumes with `Range` requests (on the gzip representation for eCFR, which returns 406 without `Accept-Encoding: gzip`).

### Grid parsing

- The grid is a CSS grid of 1,968 `.allocation-cell` divs, 709 rows by 6 columns. `grid-column` 1-3 are ITU Region 1/2/3, 4 is US Federal, 5 is US Non-Federal and 6 is FCC rule parts. `grid-row` is the band row.
- A cell spanning several columns (for example `1 / 4`, `4 / 6`) yields one band per column with identical content, so a lookup can legitimately show Federal and Non-Federal as identical.
- 1,637 cells carry band labels. The 331 rule-part cells have no label and are joined to Non-Federal bands by grid-row overlap.
- The table starts at **0 Hz** (first row "0-8.3 kHz", explicitly NOT ALLOCATED) and ends at 3000 GHz ("275-3000 GHz", NOT ALLOCATED). Every column is contiguous over `[0, 3 THz]` with no gaps or overlaps. The importer fails rather than bridging a gap.
- Band counts: `itu_r1` 544, `itu_r2` 544, `itu_r3` 538, `us_federal` 501, `us_non_federal` 543. That is 2,670 bands and 6,396 services (5,501 primary, 895 secondary), with 53 distinct qualifiers and 38 distinct rule-part strings on 416 Non-Federal bands. The floor enforced at import is 2,000 bands.
- `unallocated: true` on 143 bands: 10 from explicit "NOT ALLOCATED" (the two end rows) and 133 from empty cells (95 Federal, 42 Non-Federal, for example Federal at 535-1605 kHz) which the table leaves blank. These keep their footnotes.
- Service grammar in the 2025 grid:
  - ALL CAPS is primary and Title Case is secondary.
  - Footnotes are always parenthesised after the service (`RADIOLOCATION (G129, G2)`).
  - The qualifier is everything from the first `(` or from `except` on.
  - The grid title-cases the clause ("MOBILE Except Aeronautical Mobile"). The stored qualifier is lower-cased up to the first parenthesis (`except aeronautical mobile (R)`), and parenthesised parts keep their case (`(Earth-to-space)`, `(R)`, `(20 kHz)`).
- Cells the parser could not handle: none (`parser_problems` is empty).

### Footnotes

- eCFR §2.106(b)-(e) yields 960 footnotes: 668 international (5.xxx), 199 US, 62 NG and 31 G. Continuation paragraphs, "Note n to §2.106..." paragraphs and tables are appended to the footnote they follow (tables flattened as `cell | cell` rows).
- The table references 975 distinct ids. 959 resolve in eCFR and **16 are absent from eCFR** (the ids listed above).
  - The fifteen 5.xxx ids do not appear in the eCFR text.
  - `US115` is present but printed as "US1157" in the eCFR XML (paragraph (c)(115)), so it is not matched. The orphan `US1157` is stored as parsed and referenced by nothing.
  - All 16 take their text from the NTIA footnote page (`source_id: "ntia_footnotes"`, `fallback: true`).
  - Unresolved ids: 0. `missing: true` is never set in the bundled dataset. The total is 976 footnotes: 960 eCFR plus 16 fallback.
- The 271 footnote-text disagreements are recorded in `disagreements` (182 international, 62 US, 21 NG, 6 G) and surfaced by `lookup()` for the ids it returns. The comparison ignores case, whitespace, punctuation, WRC markers, short paragraph markers and citation style ("§ x of this chapter" versus "47 CFR x"). What remains is wording: RR 2024 versus the 2016 reproduction (for example 5.118 "Mexico and Peru" versus "Mexico, Peru and Uruguay", 5.162A "North Macedonia" versus "The Former Yugoslav Republic of Macedonia", "frequency band" versus "band"), and eCFR-only editorial notes.

See also: [reference catalogs](reference-catalogs.md), [catalog sources](catalogs.md).
