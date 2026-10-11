"""Frequency allocation reference (docs/allocations.md "Backend integration").

AppService mixin over msdr.allocations.AllocationStore: lookup / bands /
sources, the Settings refresh action (importer in a thread, atomic replace,
reload, never raises) and `allocation_context` -- a lookup plus the
supplemental channel / application entries of the band-plan file matched by
frequency containment only. Nothing here looks at demodulator or bandwidth.

`self.allocations` (the store) is built in AppService.__init__ and rebuilt by
apply_settings when `allocation_data_dir` changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from importlib import resources
from typing import Any

from msdr.allocations import importer as allocation_importer
from msdr.allocations.store import DATASET_NAME

logger = logging.getLogger(__name__)

# Ids a refresh accepts even before the store has listed them (a dataset that
# failed to load still has these as its sources). Both come from the one
# network import, so refreshing either rebuilds the whole dataset.
ALLOCATION_SOURCE_IDS: tuple[str, ...] = ("ntia_table", "ecfr_2_106")

# A named channel (kind "channel", lo == hi) counts as context this close.
_CHANNEL_TOLERANCE_HZ = 5_000

# `allocation_context()["supplemental"]["reason"]` for non-US jurisdictions.
SUPPLEMENTAL_US_ONLY_REASON = "Channel and application hints are US-specific"


def build_allocation_dataset(source_id: str) -> dict[str, Any]:
    """Blocking build of a fresh dataset for the refresh action. A module-level
    function (not a method) so tests can monkeypatch it instead of hitting the
    network -- mirrors services/catalog.py's fetch_catalog_text."""
    return allocation_importer.import_from_network(log=lambda *a, **k: logger.info(*a, **k))


@lru_cache(maxsize=1)
def _bandplan_entries() -> tuple[dict[str, Any], ...]:
    text = resources.files("msdr.providers.data").joinpath("bandplan_us.json").read_text()
    return tuple(json.loads(text))


def _entry_hint(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry.get(k) for k in ("name", "lo_hz", "hi_hz", "category", "kind", "note")}


def bandplan_context(hz: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(applications, channels) from the band-plan file by frequency only:
    kind "band" entries containing hz, kind "channel" entries within 5 kHz."""
    applications: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    for entry in _bandplan_entries():
        lo, hi = entry.get("lo_hz"), entry.get("hi_hz")
        if lo is None or hi is None:
            continue
        if entry.get("kind") == "channel":
            if abs(hz - (lo + hi) / 2) <= _CHANNEL_TOLERANCE_HZ:
                channels.append(_entry_hint(entry))
        elif entry.get("kind") == "band" and lo <= hz <= hi:
            applications.append(_entry_hint(entry))
    return applications, channels


def allocation_summary(lookup: dict[str, Any]) -> dict[str, Any]:
    """Compact form of a lookup for survey labels, finds and claims: band
    edges, status and every service (deduplicated across Federal /
    Non-Federal; the international table only when there is no national
    block, i.e. ITU jurisdictions)."""
    services: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def _add(block: dict[str, Any] | None, table: str) -> None:
        for s in (block or {}).get("services") or []:
            key = (s.get("name"), s.get("qualifier"), s.get("priority"), table)
            if key in seen:
                continue
            seen.add(key)
            services.append(
                {
                    "name": s.get("name"),
                    "qualifier": s.get("qualifier"),
                    "priority": s.get("priority"),
                    "table": table,
                }
            )

    national = lookup.get("national")
    if national:
        _add(national.get("federal"), "federal")
        _add(national.get("non_federal"), "non_federal")
    else:
        _add(lookup.get("international"), "international")
    return {
        "lo_hz": lookup.get("lo_hz"),
        "hi_hz": lookup.get("hi_hz"),
        "jurisdiction": lookup.get("jurisdiction"),
        "status": lookup.get("status"),
        "services": services,
    }


def format_services(services: list[dict[str, Any]]) -> str:
    """Render services as "FIXED, MOBILE except aeronautical mobile; Radiolocation
    (secondary)": primaries first, secondaries marked, each distinct name once."""
    primary: list[str] = []
    secondary: list[str] = []
    for s in services:
        text = " ".join(part for part in (s.get("name"), s.get("qualifier")) if part)
        if not text:
            continue
        if s.get("priority") == "secondary":
            text += " (secondary)"
            bucket = secondary
        else:
            bucket = primary
        if text not in bucket:
            bucket.append(text)
    return "; ".join(part for part in (", ".join(primary), ", ".join(secondary)) if part)


class AllocationServiceMixin:
    def _allocation_jurisdiction(self, jurisdiction: str | None) -> str:
        return jurisdiction or self.settings.allocation_jurisdiction

    def allocation_lookup(self, hz: int, jurisdiction: str | None = None) -> dict:
        return self.allocations.lookup(int(hz), self._allocation_jurisdiction(jurisdiction))

    def allocation_bands(
        self,
        lo_hz: int,
        hi_hz: int,
        jurisdiction: str | None = None,
        column: str | None = None,
    ) -> list[dict]:
        return self.allocations.bands(
            int(lo_hz), int(hi_hz), self._allocation_jurisdiction(jurisdiction), column=column
        )

    def allocation_sources(self) -> list[dict]:
        return self.allocations.sources()

    def is_allocation_source(self, source_id: str) -> bool:
        return source_id in ALLOCATION_SOURCE_IDS or any(
            s.get("id") == source_id for s in self.allocations.sources()
        )

    def allocation_context(self, hz: int, jurisdiction: str | None = None) -> dict:
        """Lookup plus the supplemental layer, matched by frequency only. The
        band-plan file is US-specific (47 CFR citations, NOAA channels), so
        ITU jurisdictions get empty lists and `supplemental` says why."""
        result = dict(self.allocation_lookup(hz, jurisdiction))
        if result.get("jurisdiction") == "US":
            applications, channels = bandplan_context(int(hz))
            supplemental: dict[str, Any] = {"available": True, "reason": None}
        else:
            applications, channels = [], []
            supplemental = {"available": False, "reason": SUPPLEMENTAL_US_ONLY_REASON}
        result["applications"] = applications
        result["channels"] = channels
        result["supplemental"] = supplemental
        return result

    def allocation_summary_at(self, hz: int, jurisdiction: str | None = None) -> dict | None:
        """Compact summary for survey labels / finds / claims; never raises
        (these run from the survey's status loop and the scanner, not a
        request)."""
        try:
            return allocation_summary(self.allocation_lookup(hz, jurisdiction))
        except Exception:
            logger.exception("allocation summary failed at %s Hz", hz)
            return None

    async def allocation_refresh(self, source_id: str) -> dict:
        """Settings -> Allocation sources -> Refresh: build in a thread, validate,
        write atomically into the user data dir, reload. Never raises; on any
        failure the previously loaded dataset stays in place."""
        path = self.allocations.data_dir / DATASET_NAME

        def _run() -> None:
            dataset = build_allocation_dataset(source_id)
            errors = allocation_importer.validate_dataset(dataset)
            if errors:
                raise allocation_importer.AllocationImportError("; ".join(errors[:5]))
            allocation_importer.write_atomic(path, dataset)

        error: str | None = None
        try:
            await asyncio.to_thread(_run)
            self.allocations.reload()
        except Exception as exc:  # refresh reports failure, doesn't 500
            logger.warning("allocation refresh %s failed: %s", source_id, exc)
            error = str(exc) or exc.__class__.__name__
        return {
            "source": source_id,
            "ok": error is None,
            "error": error,
            "sources": self.allocation_sources(),
        }
