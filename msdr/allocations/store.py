"""In-memory allocation lookup over the bundled / imported dataset.

`AllocationStore` loads once, answers `lookup`/`bands` by bisecting sorted
band edges per column, and never touches the network. A valid dataset in the
user data dir is merged per column over the bundled one: columns it contains
replace the bundled ones, columns it lacks stay bundled, footnotes and
sources are unioned. An invalid or older file is reported through
`sources()` and otherwise ignored.
"""

from __future__ import annotations

import datetime as dt
import json
from bisect import bisect_right
from pathlib import Path
from typing import Any

from .importer import (
    AllocationImportError,
    bundled_dataset_path,
    dataset_records,
    validate_dataset,
)
from .model import (
    COLUMNS,
    INTERNATIONAL_COLUMN,
    JURISDICTIONS,
    US_COLUMNS,
    Band,
    Footnote,
    SourceStatus,
)

DATASET_NAME = "dataset.json"
DEFAULT_DATA_DIR = Path.home() / "Library/Application Support/msdr/allocations"
# A dataset older than this is flagged `stale` so the UI can suggest Refresh.
STALE_AFTER_DAYS = 365


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Column:
    """Sorted bands of one column plus their lo edges for bisect."""

    def __init__(self, bands: list[Band]) -> None:
        self.bands = sorted(bands, key=lambda b: b.lo_hz)
        self.edges = [b.lo_hz for b in self.bands]
        self.lo_hz = self.bands[0].lo_hz if self.bands else None
        self.hi_hz = self.bands[-1].hi_hz if self.bands else None

    def find(self, hz: int) -> Band | None:
        """Band with lo <= hz < hi; the top band also owns its hi edge."""
        if not self.bands or hz < self.lo_hz or hz > self.hi_hz:
            return None
        i = bisect_right(self.edges, hz) - 1
        band = self.bands[i]
        if hz < band.hi_hz or (band.hi_hz == self.hi_hz and hz == self.hi_hz):
            return band
        return None

    def overlapping(self, lo_hz: int, hi_hz: int) -> list[Band]:
        i = max(bisect_right(self.edges, lo_hz) - 1, 0)
        out = []
        for band in self.bands[i:]:
            if band.lo_hz >= hi_hz and not (band.lo_hz == hi_hz == lo_hz):
                break
            if band.hi_hz > lo_hz or (band.hi_hz == lo_hz == hi_hz):
                out.append(band)
        return out


class _Loaded:
    def __init__(self, dataset: dict[str, Any], origin: str, path: str) -> None:
        bands, footnotes, sources = dataset_records(dataset)
        # A hand-edited user dataset can carry the wrong JSON types here; raise
        # AllocationImportError so _try_load falls back instead of crashing.
        generated_at = dataset.get("generated_at")
        disagreements = dataset.get("disagreements") or []
        if generated_at is not None and not isinstance(generated_at, str):
            raise AllocationImportError("generated_at is not a string")
        if not isinstance(disagreements, list) or not all(
            isinstance(d, dict) for d in disagreements
        ):
            raise AllocationImportError("disagreements is not a list of objects")
        self.origin = origin
        self.path = path
        self.generated_at = _parse_iso(generated_at)
        self.columns: dict[str, _Column] = {}
        for col in COLUMNS:
            rows = [b for b in bands if b.column == col]
            if rows:
                self.columns[col] = _Column(rows)
        self.footnotes: dict[str, Footnote] = {f.id: f for f in footnotes}
        self.sources: list[SourceStatus] = sources
        self.disagreements: dict[str, list[dict[str, Any]]] = {}
        for d in disagreements:
            self.disagreements.setdefault(str(d.get("id")), []).append(d)
        for src in self.sources:
            src.origin = origin
            when = _parse_iso(src.imported_at) or self.generated_at
            src.stale = bool(when and (dt.datetime.now(dt.UTC) - when).days > STALE_AFTER_DAYS)

    def merge(self, overlay: _Loaded) -> None:
        """Per-column overlay: the overlay's columns win, everything else is unioned."""
        self.columns.update(overlay.columns)
        self.footnotes.update(overlay.footnotes)
        self.sources = self.sources + overlay.sources
        for fid, items in overlay.disagreements.items():
            self.disagreements.setdefault(fid, []).extend(items)


class AllocationStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self._loaded: _Loaded | None = None
        self._load_errors: list[dict[str, Any]] = []
        self.reload()

    # ------------------------------------------------------------------ loading

    @staticmethod
    def bundled_path() -> Path:
        return bundled_dataset_path()

    def _try_load(
        self, path: Path, origin: str, known_footnotes: list[Footnote] | None = None
    ) -> _Loaded | None:
        try:
            with open(path, encoding="utf-8") as fh:
                dataset = json.load(fh)
        except FileNotFoundError:
            self._load_errors.append(
                {"path": str(path), "origin": origin, "error": "file not found"}
            )
            return None
        except (OSError, ValueError) as exc:
            self._load_errors.append(
                {"path": str(path), "origin": origin, "error": f"unreadable: {exc}"}
            )
            return None
        errors = validate_dataset(dataset, known_footnotes=known_footnotes)
        if errors:
            shown = "; ".join(errors[:3]) + (
                f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
            )
            self._load_errors.append(
                {"path": str(path), "origin": origin, "error": f"invalid: {shown}"}
            )
            return None
        try:
            return _Loaded(dataset, origin, str(path))
        except AllocationImportError as exc:
            self._load_errors.append({"path": str(path), "origin": origin, "error": str(exc)})
            return None

    def reload(self) -> None:
        self._load_errors = []
        bundled = self._try_load(self.bundled_path(), "bundled")
        user_path = self.data_dir / DATASET_NAME
        user = None
        if user_path.exists():
            known = list(bundled.footnotes.values()) if bundled is not None else None
            user = self._try_load(user_path, "imported", known)
        chosen = bundled
        if user is not None:
            newer = (
                bundled is None
                or bundled.generated_at is None
                or (user.generated_at is not None and user.generated_at >= bundled.generated_at)
            )
            if bundled is None:
                chosen = user
            elif newer:
                bundled.merge(user)
            else:
                self._load_errors.append(
                    {
                        "path": str(user_path),
                        "origin": "imported",
                        "error": "older than bundled dataset "
                        f"({user.generated_at} < {bundled.generated_at})",
                    }
                )
        self._loaded = chosen

    # ------------------------------------------------------------------ queries

    def sources(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self._loaded is not None:
            out.extend(s.to_dict() for s in self._loaded.sources)
        for err in self._load_errors:
            out.append(
                SourceStatus(
                    id="dataset_file",
                    title="Allocation dataset file",
                    publisher="",
                    url=err["path"],
                    origin=err["origin"],
                    status="error",
                    error=err["error"],
                ).to_dict()
            )
        return out

    def _source_block(self) -> dict[str, Any]:
        if self._loaded is None:
            return {"stale": False, "statuses": {}}
        return {
            "stale": any(s.stale for s in self._loaded.sources),
            "statuses": {s.id: s.status for s in self._loaded.sources},
        }

    def _citations(self, source_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Citations for the sources actually used (all `ok` sources when None)."""
        if self._loaded is None:
            return []
        return [
            {
                "source_id": s.id,
                "title": s.title,
                "url": s.url,
                "edition": s.edition,
                "effective_date": s.effective_date,
            }
            for s in self._loaded.sources
            if s.status == "ok" and (source_ids is None or s.id in source_ids)
        ]

    def _unavailable_message(self) -> str:
        parts = [f"{e['origin']} file {e['path']}: {e['error']}" for e in self._load_errors]
        return "no allocation dataset could be loaded (" + "; ".join(parts) + ")"

    def lookup(self, hz: int, jurisdiction: str) -> dict[str, Any]:
        hz = int(hz)
        result: dict[str, Any] = {
            "frequency_hz": hz,
            "jurisdiction": jurisdiction,
            "status": "ok",
            "message": None,
            "lo_hz": None,
            "hi_hz": None,
            "national": None,
            "international": None,
            "footnotes": [],
            "citations": self._citations(),
            "disagreements": [],
            "sources": self._source_block(),
        }
        if jurisdiction not in JURISDICTIONS:
            result["status"] = "unsupported_jurisdiction"
            result["message"] = (
                f"jurisdiction {jurisdiction!r} is not supported; "
                f"use one of {', '.join(JURISDICTIONS)}"
            )
            return result
        loaded = self._loaded
        if loaded is None:
            result["status"] = "source_unavailable"
            result["message"] = self._unavailable_message()
            return result

        intl_col = INTERNATIONAL_COLUMN[jurisdiction]
        national_cols = US_COLUMNS if jurisdiction == "US" else ()
        needed = list(national_cols) + [intl_col]
        covering = [loaded.columns[c] for c in needed if c in loaded.columns]
        if not covering:
            result["status"] = "not_covered"
            result["message"] = (
                f"the loaded dataset has no {' / '.join(needed)} column "
                f"for jurisdiction {jurisdiction}"
            )
            return result
        lo_hz = min(c.lo_hz for c in covering)
        hi_hz = max(c.hi_hz for c in covering)
        if hz < lo_hz or hz > hi_hz:
            result["status"] = "not_covered"
            result["message"] = (
                f"{hz} Hz is outside the loaded {jurisdiction} allocation range {lo_hz}-{hi_hz} Hz"
            )
            return result

        ids: list[str] = []
        used_sources: set[str] = set()
        services_found = 0
        lo_edges: list[int] = []
        hi_edges: list[int] = []

        def _take(band: Band) -> dict[str, Any]:
            nonlocal services_found
            services_found += len(band.services)
            used_sources.add(band.source_id)
            for fid in band.footnote_ids():
                if fid not in ids:
                    ids.append(fid)
            return {
                "lo_hz": band.lo_hz,
                "hi_hz": band.hi_hz,
                "services": [s.to_dict() for s in band.services],
                "footnotes": list(band.footnotes),
                "unallocated": band.unallocated,
            }

        if jurisdiction == "US":
            national: dict[str, Any] = {"federal": None, "non_federal": None, "shared": False}
            names: set[str] = set()
            for col, key in (("us_federal", "federal"), ("us_non_federal", "non_federal")):
                column = loaded.columns.get(col)
                band = column.find(hz) if column else None
                if band is None:
                    continue
                block = _take(band)
                if key == "non_federal":
                    block["rule_parts"] = list(band.rule_parts)
                national[key] = block
                names.update(s.name for s in band.services)
                lo_edges.append(band.lo_hz)
                hi_edges.append(band.hi_hz)
            national["shared"] = len(names) > 1
            result["national"] = national
            national_services = services_found

        intl_column = loaded.columns.get(intl_col)
        intl_band = intl_column.find(hz) if intl_column else None
        if intl_band is not None:
            before = services_found
            block = _take(intl_band)
            block["region"] = "ITU" + intl_col[-1]
            result["international"] = block
            if jurisdiction != "US":
                lo_edges.append(intl_band.lo_hz)
                hi_edges.append(intl_band.hi_hz)
                national_services = services_found - before
        if jurisdiction == "US" and not lo_edges and intl_band is not None:
            lo_edges.append(intl_band.lo_hz)
            hi_edges.append(intl_band.hi_hz)

        if not lo_edges:
            result["status"] = "not_covered"
            result["message"] = (
                f"{hz} Hz falls in no {' / '.join(needed)} band of the loaded dataset "
                f"({lo_hz}-{hi_hz} Hz)"
            )
            return result
        result["lo_hz"] = max(lo_edges)
        result["hi_hz"] = min(hi_edges)
        if national_services == 0:
            result["status"] = "unallocated"
            scope = (
                "US Federal and Non-Federal tables"
                if jurisdiction == "US"
                else f"ITU Region {intl_col[-1]} table"
            )
            result["message"] = (
                f"{result['lo_hz']}-{result['hi_hz']} Hz is not allocated to any service "
                f"in the {scope}"
            )
        result["footnotes"] = [
            loaded.footnotes[fid].to_dict()
            if fid in loaded.footnotes
            else Footnote(id=fid, text=None, source_id=None, missing=True).to_dict()
            for fid in ids
        ]
        used_sources.update(
            f["source_id"] for f in result["footnotes"] if f.get("source_id") is not None
        )
        result["citations"] = self._citations(used_sources)
        result["disagreements"] = [d for fid in ids for d in loaded.disagreements.get(fid, [])]
        return result

    def bands(
        self,
        lo_hz: int,
        hi_hz: int,
        jurisdiction: str,
        column: str | None = None,
    ) -> list[dict[str, Any]]:
        """Bands overlapping [lo_hz, hi_hz) in the jurisdiction's columns, in table order."""
        if self._loaded is None or jurisdiction not in JURISDICTIONS:
            return []
        cols = (
            list(US_COLUMNS) + [INTERNATIONAL_COLUMN["US"]]
            if jurisdiction == "US"
            else [INTERNATIONAL_COLUMN[jurisdiction]]
        )
        if column is not None:
            cols = [c for c in cols if c == column]
        out: list[dict[str, Any]] = []
        for col in cols:
            col_obj = self._loaded.columns.get(col)
            if col_obj is None:
                continue
            out.extend(b.to_dict() for b in col_obj.overlapping(int(lo_hz), int(hi_hz)))
        return out
