"""Plain dataclasses for the allocation dataset (see docs/allocations.md).

`to_dict()` output is the JSON contract shared by the importer, the store
and the API; nothing here does I/O.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
IMPORTER_VERSION = "1"

COLUMNS = ("itu_r1", "itu_r2", "itu_r3", "us_federal", "us_non_federal")
US_COLUMNS = ("us_federal", "us_non_federal")
JURISDICTIONS = ("US", "ITU1", "ITU2", "ITU3")
# ITU region column consulted as international context per jurisdiction.
INTERNATIONAL_COLUMN = {"US": "itu_r2", "ITU1": "itu_r1", "ITU2": "itu_r2", "ITU3": "itu_r3"}
PRIORITIES = ("primary", "secondary")

# 5.132A, US340, NG155, G129; the optional letter suffix preserves ordering
# in the source (see §2.106(a)).
FOOTNOTE_ID_RE = re.compile(r"^(?:5\.\d+[A-Z]*|US\d+[A-Z]*|NG\d+[A-Z]*|G\d+[A-Z]*)$")

_KIND_BY_PREFIX = (("5.", "international"), ("US", "us"), ("NG", "non_federal"), ("G", "federal"))


def is_footnote_id(token: str) -> bool:
    return bool(FOOTNOTE_ID_RE.match(token))


def footnote_kind(footnote_id: str) -> str:
    for prefix, kind in _KIND_BY_PREFIX:
        if footnote_id.startswith(prefix):
            return kind
    raise ValueError(f"not a footnote id: {footnote_id!r}")


@dataclass
class Service:
    name: str
    priority: str
    qualifier: str | None = None
    footnotes: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Service:
        return cls(
            name=str(d["name"]),
            priority=str(d["priority"]),
            qualifier=d.get("qualifier"),
            footnotes=list(d.get("footnotes") or []),
            raw=str(d.get("raw") or ""),
        )


@dataclass
class Band:
    lo_hz: int
    hi_hz: int
    column: str
    services: list[Service] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    rule_parts: list[str] = field(default_factory=list)
    unallocated: bool = False
    source_id: str = "ntia_table"
    # data-allocation-id of the NTIA grid cell this row came from (provenance
    # for the limitations list); None for user-supplied data.
    cell_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lo_hz": self.lo_hz,
            "hi_hz": self.hi_hz,
            "column": self.column,
            "services": [s.to_dict() for s in self.services],
            "footnotes": list(self.footnotes),
            "rule_parts": list(self.rule_parts),
            "unallocated": self.unallocated,
            "source_id": self.source_id,
            "cell_id": self.cell_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Band:
        return cls(
            lo_hz=int(d["lo_hz"]),
            hi_hz=int(d["hi_hz"]),
            column=str(d["column"]),
            services=[Service.from_dict(s) for s in d.get("services") or []],
            footnotes=list(d.get("footnotes") or []),
            rule_parts=list(d.get("rule_parts") or []),
            unallocated=bool(d.get("unallocated", False)),
            source_id=str(d.get("source_id") or "ntia_table"),
            cell_id=d.get("cell_id"),
        )

    def footnote_ids(self) -> list[str]:
        """Every footnote id the row references, cell-level first, deduplicated."""
        seen: list[str] = []
        for fid in list(self.footnotes) + [f for s in self.services for f in s.footnotes]:
            if fid not in seen:
                seen.append(fid)
        return seen


@dataclass
class Footnote:
    id: str
    text: str | None
    source_id: str | None = "ecfr_2_106"
    kind: str = "international"
    # True when the id is referenced by the table but no source carries its
    # text; the reference is kept so the UI can still show the id.
    missing: bool = False
    # True when the text comes from the NTIA footnote page because eCFR
    # lacks the id (source_id "ntia_footnotes"); eCFR stays primary otherwise.
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Footnote:
        return cls(
            id=str(d["id"]),
            text=d.get("text"),
            source_id=d.get("source_id"),
            kind=str(d.get("kind") or footnote_kind(str(d["id"]))),
            missing=bool(d.get("missing", False)),
            fallback=bool(d.get("fallback", False)),
        )


@dataclass
class SourceStatus:
    id: str
    title: str
    publisher: str
    url: str
    edition: str | None = None
    effective_date: str | None = None
    imported_at: str | None = None
    origin: str = "bundled"
    lo_hz: int | None = None
    hi_hz: int | None = None
    band_count: int = 0
    footnote_count: int = 0
    status: str = "ok"
    error: str | None = None
    stale: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourceStatus:
        return cls(
            id=str(d["id"]),
            title=str(d.get("title") or d["id"]),
            publisher=str(d.get("publisher") or ""),
            url=str(d.get("url") or ""),
            edition=d.get("edition"),
            effective_date=d.get("effective_date"),
            imported_at=d.get("imported_at"),
            origin=str(d.get("origin") or "bundled"),
            lo_hz=d.get("lo_hz"),
            hi_hz=d.get("hi_hz"),
            band_count=int(d.get("band_count") or 0),
            footnote_count=int(d.get("footnote_count") or 0),
            status=str(d.get("status") or "ok"),
            error=d.get("error"),
            stale=bool(d.get("stale", False)),
            provenance=dict(d.get("provenance") or {}),
        )
