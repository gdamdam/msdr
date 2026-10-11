"""Identify (bandplan + catalog) and the searchable frequency catalog.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import asyncio
import inspect
import io
import math
import os
import re
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException

from msdr.catalog import CATALOG_KINDS, CATALOG_LABELS, catalog_sources_help
from msdr.catalog import DEFAULT_URLS as CATALOG_DEFAULT_URLS
from msdr.catalog import PARSERS as ALL_CATALOG_PARSERS
from msdr.catalog.models import CatalogEntry, on_air, on_air_state, tune_recipe
from msdr.catalog.models import on_air as entry_on_air
from msdr.catalog.parsers.aoki import parse_aoki
from msdr.catalog.parsers.eibi import default_url as eibi_default_url
from msdr.catalog.parsers.eibi import parse_eibi, season_code
from msdr.catalog.parsers.faa_nasr import fetch_faa_nasr
from msdr.catalog.parsers.hfcc import parse_hfcc
from msdr.catalog.parsers.ioos_hfradar import default_url as ioos_hfradar_default_url
from msdr.catalog.parsers.ioos_hfradar import fetch_ioos_hfradar
from msdr.catalog.parsers.userlists import parse_gqrx_csv, parse_sdrpp_json
from msdr.engine.restore import RestoreReport, plan_with_desired
from msdr.engine.state import TuneRecipe
from msdr.library.frequency import format_mhz
from msdr.library.models import Signal
from msdr.providers.base import Candidate, IdentifyQuery
from msdr.settings import Location

_CATALOG_FETCH_TIMEOUT_S = 20


# Result orderings for /api/catalog/search. "relevance" is the store's own
# order (on-air first when asked); the others sort the whole filtered set
# in Python, then page it (see CatalogServiceMixin.catalog_search). "time" is
# the UTC start of the schedule window; "name" is the station name. With
# on_air set, rows transmitting now come first under every sort.
CATALOG_SORTS = ("relevance", "frequency", "distance", "time", "name")

_NO_LOCATION_DETAIL = "No receiver location configured; set it in Settings → Location"

# A refresh that parses to fewer than half of a source's current rows is
# refused (see CatalogServiceMixin._store_refresh); below this many existing
# rows the ratio is noise and the refresh always goes through.
_SHRINK_GUARD_MIN_ROWS = 20


# One line per source for the SETUP → Catalog group, next to CATALOG_KINDS'
# static / refreshable / live word (docs/reference-catalogs.md, "Coverage").
CATALOG_COVERAGE: dict[str, str] = {
    "eibi": "Worldwide shortwave broadcast schedule, seasonal (A/B) editions",
    "aoki": "International shortwave broadcast schedule, seasonal ZIP download",
    "hfcc": "HFCC/ASBU coordinated shortwave schedule, seasonal ZIP download",
    "ndxc": "NDXC shortwave broadcast schedule, fixed public URL",
    "noaa_radiofax": "NOAA/USCG HF radiofax stations (19 records, bundled)",
    "sigidwiki": "Signal Identification Wiki reference examples, bundled",
    "priyom": "Numbers-station calendar, next 7 UTC days from the Priyom API",
    "satnogs": "SatNOGS DB satellite transmitters, optional NORAD filter",
    "nist_time": "WWV, WWVH, WWVB (12 records, snapshot 2026-10-01)",
    "superdarn": "49 radars, hardware tables snapshot 2026-10-01, no frequencies",
    "faa_nasr": "US navaids and localizers, 28-day cycle",
    "ioos_hfradar": "US coastal HF radar sites, live station list",
}


def _is_static_parser(parser: Callable) -> bool:
    """A bundled-table parser takes no text: every parameter has a default
    (radiofax/sigidwiki take none; nist_time/superdarn take an optional
    snapshot path)."""
    return all(
        p.default is not inspect.Parameter.empty
        for p in inspect.signature(parser).parameters.values()
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (mean Earth radius 6371 km)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def catalog_entry_dict(entry: CatalogEntry, at: datetime, location: Location | None = None) -> dict:
    """API shape of a catalog entry: the stored row plus the derived
    `on_air_state`, `tunable` and `distance_km` (null unless `location` is
    given and the row has coordinates). `frequency_hz` stays null for
    untunable reference rows; nothing here formats it."""
    d = entry.model_dump(mode="json")
    d["on_air_state"] = on_air_state(entry, at)
    d["tunable"] = entry.frequency_hz is not None
    d["distance_km"] = None
    if location is not None and entry.latitude is not None and entry.longitude is not None:
        d["distance_km"] = round(
            haversine_km(location.lat, location.lon, entry.latitude, entry.longitude), 1
        )
    return d


# Candidates near a catalog hit: at least this wide, or half the query's own
# filter bandwidth if that's wider (mirrors identify's own channel tolerance).
_CATALOG_IDENTIFY_MIN_TOLERANCE_HZ = 5_000


# Catalog import reads the whole file into memory before parsing.
_MAX_IMPORT_BYTES = 20 * 1024 * 1024


def fetch_catalog_text(
    url: str, encoding: str = "utf-8", member: re.Pattern[str] | None = None
) -> str:
    """Blocking fetch of a catalog source's raw schedule text. A module-level
    function (not a method) so tests can monkeypatch it directly instead of
    hitting the network -- mirrors providers/tracking.py's fetch_omm."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=_CATALOG_FETCH_TIMEOUT_S) as resp:
        data = resp.read()
    return catalog_payload_text(data, encoding, member)


def fetch_priyom_catalog(days: int = 7) -> tuple[list[CatalogEntry], str]:
    """Fetch bounded UTC day windows from the official Priyom calendar API.

    The official viewer itself requests one UTC day at a time. Keep that
    contract here to avoid endpoint range limits and deduplicate repeated API
    items defensively.
    """
    base_url = CATALOG_DEFAULT_URLS["priyom"]
    first_day = datetime.now(UTC).date()
    entries: list[CatalogEntry] = []
    seen: set[tuple] = set()
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        url = (
            base_url
            + "?"
            + urlencode(
                {
                    "timeMin": start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "timeMax": end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                }
            )
        )
        parsed = ALL_CATALOG_PARSERS["priyom"](fetch_catalog_text(url))
        for entry in parsed:
            key = (entry.station, entry.frequency_hz, entry.mode, entry.date, entry.start_utc)
            if key not in seen:
                seen.add(key)
                entries.append(entry)
    return entries, base_url


def fetch_faa_nasr_catalog() -> tuple[list[CatalogEntry], str]:
    """FAA NASR downloads two ZIPs for the current 28-day cycle; the parser
    module owns the URLs and the cycle arithmetic."""
    return fetch_faa_nasr(), "https://nfdc.faa.gov/webContent/28DaySub/"


def fetch_ioos_hfradar_catalog() -> tuple[list[CatalogEntry], str]:
    """IOOS HFRNet downloads the station list CSV plus the best-effort ERDDAP
    names table; the parser module owns both URLs."""
    return fetch_ioos_hfradar(), ioos_hfradar_default_url


# Sources whose refresh is a self-contained download (not one text URL):
# `() -> (entries, url)`. Checked before the static branch, because the
# faa_nasr / ioos_hfradar PARSERS entries are the offline bundled snapshots
# (parse_faa_nasr / parse_ioos_hfradar), which are only the fallback for a
# first load without network.
_CATALOG_FETCHERS: dict[str, Callable[[], tuple[list[CatalogEntry], str]]] = {
    "priyom": fetch_priyom_catalog,
    "faa_nasr": fetch_faa_nasr_catalog,
    "ioos_hfradar": fetch_ioos_hfradar_catalog,
}


def catalog_payload_text(
    data: bytes, encoding: str = "utf-8", member: re.Pattern[str] | None = None
) -> str:
    """Decode a fetched schedule. HFCC and Aoki publish ZIPs that bundle the
    schedule with reference tables (sites, languages, an .xlsx...), so a ZIP
    must be opened and the one schedule member picked by `member`; a plain
    text payload (a URL pointing straight at the .TXT) is decoded as is."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        if member is not None and data[:2] == b"PK":
            raise ValueError("invalid ZIP archive")
        return data.decode(encoding, errors="replace")
    if member is None:
        raise ValueError("ZIP archive not supported for this source")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if member.search(n.rsplit("/", 1)[-1])]
        if not names:
            listing = ", ".join(zf.namelist())
            raise ValueError(f"schedule file not found in ZIP (members: {listing})")
        return zf.read(names[0]).decode(encoding, errors="replace")


def _parse_eibi_fetched(text: str, url: str) -> list[CatalogEntry]:
    filename = Path(urlsplit(url).path).name
    return parse_eibi(text, filename=filename)


_CATALOG_PARSERS: dict[str, Callable[[str, str], list[CatalogEntry]]] = {
    "eibi": _parse_eibi_fetched,
    "aoki": lambda text, url: parse_aoki(text),
    "hfcc": lambda text, url: parse_hfcc(text),
}


# eibispace.de serves sked-*.csv as latin-1 (accented station/target names);
# everything else defaults to utf-8.
_CATALOG_ENCODINGS: dict[str, str] = {"eibi": "latin-1", "hfcc": "latin-1", "aoki": "latin-1"}


# The schedule member inside a source's ZIP: HFCC's A26all00.TXT (not
# admin/antenna/site/... reference tables), Aoki's xta26.txt (not the .xlsx
# twin or userlist1.txt).
_CATALOG_ZIP_MEMBERS: dict[str, re.Pattern[str]] = {
    "hfcc": re.compile(r"^[AB]\d\dall00\.txt$", re.IGNORECASE),
    "aoki": re.compile(r"^xt[ab]\d\d\.txt$", re.IGNORECASE),
}


def _catalog_note_line(entry: CatalogEntry) -> str:
    """Notes line for a signal saved from a catalog entry. Mirrors
    msdr/ui/format.js's catalogNoteLine so CATALOG/LISTEN "★" produce the same
    text whether the save happens client-side (old path) or here."""
    if not entry.start_utc or not entry.stop_utc:
        window = "24h"
    else:
        window = f"{entry.start_utc}-{entry.stop_utc}"
    return f"{entry.source} {window} {entry.language or ''} → {entry.target or ''}".strip()


class CatalogServiceMixin:
    # --- identify (bandplan + catalog) --------------------------------------

    def identify(self, q: IdentifyQuery) -> list[Candidate]:
        if not self.settings.features.ident:
            raise HTTPException(404, "feature disabled: ident")
        candidates = list(self.identify_provider.identify(q))
        if self.settings.features.catalog:
            candidates.extend(self._catalog_candidates(q))
        return candidates

    def _catalog_candidates(self, q: IdentifyQuery) -> list[Candidate]:
        tolerance = max((q.filter_bandwidth_hz or 0) // 2, _CATALOG_IDENTIFY_MIN_TOLERANCE_HZ)
        now = datetime.now(UTC)
        candidates = []
        for entry in self.catalog.near(q.frequency_hz, tolerance_hz=tolerance, at=now):
            is_on_air = on_air(entry, now)
            window = f"{entry.start_utc or '0000'}-{entry.stop_utc or '2359'} UTC"
            name = f"{entry.station} ({entry.language})" if entry.language else entry.station
            candidates.append(
                Candidate(
                    name=name,
                    category="bc",
                    confidence=0.85 if is_on_air else 0.45,
                    reason=f"{format_mhz(entry.frequency_hz)}: {entry.station}, {window}",
                    provider="catalog",
                    frequency_hz=entry.frequency_hz,
                    demodulator=entry.mode,
                )
            )
        return candidates

    # --- catalog (searchable frequency catalog) -----------------------------

    def catalog_search(
        self,
        q: str,
        *,
        on_air: bool = False,
        band: tuple[int | None, int | None] | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        languages: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        has_coordinates: bool | None = None,
        network: str | None = None,
        station_id: str | None = None,
        ref_type: str | None = None,
        sort: str = "relevance",
    ) -> tuple[list[CatalogEntry], int]:
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        if sort not in CATALOG_SORTS:
            raise HTTPException(400, f"unknown sort: {sort!r}")
        location = None
        if sort == "distance":
            location = self.receiver_location()
            if location is None:
                raise HTTPException(400, _NO_LOCATION_DETAIL)
        on_air_at = datetime.now(UTC) if on_air else None
        filters = dict(
            band=band,
            source=source,
            sources=sources,
            languages=languages,
            has_coordinates=has_coordinates,
            network=network,
            station_id=station_id,
            ref_type=ref_type,
        )
        count = self.catalog.count_matches(q, **filters)
        if sort == "relevance":
            # LIMIT lives in SQL; the total comes from a COUNT, so neither
            # path builds a CatalogEntry per row in the catalog.
            matches = self.catalog.search(
                q, on_air_at=on_air_at, limit=limit, offset=offset, **filters
            )
            return matches, count
        # frequency / distance / time / name: the store has no such ORDER BY,
        # so sort the whole filtered set here and page afterwards. Rows
        # without a key (null frequency, no coordinates) sort last, in store
        # order. On-air ranking is applied here too, so skip the store's.
        rows = self.catalog.search(q, limit=max(count, 1), **filters)
        if sort == "frequency":
            rows.sort(key=lambda e: (e.frequency_hz is None, e.frequency_hz or 0))
        elif sort == "time":
            # Rows with no window (always on, or reference records) sort last.
            rows.sort(key=lambda e: (
                e.start_utc is None, e.start_utc or "", e.frequency_hz is None, e.frequency_hz or 0
            ))
        elif sort == "name":
            rows.sort(key=lambda e: (e.station.casefold(), e.frequency_hz or 0))
        else:
            rows.sort(key=lambda e: self._distance_key(e, location))
        if on_air_at is not None:
            # Stable sort: on-air rows first, each group keeping the order above.
            # entry_on_air: this method's `on_air` flag parameter shadows the function.
            rows.sort(key=lambda e: not entry_on_air(e, on_air_at))
        return rows[offset : offset + limit], count

    @staticmethod
    def _distance_key(entry: CatalogEntry, location: Location) -> tuple[bool, float]:
        if entry.latitude is None or entry.longitude is None:
            return (True, 0.0)
        return (False, haversine_km(location.lat, location.lon, entry.latitude, entry.longitude))

    def receiver_location(self) -> Location | None:
        """The configured receiver location (settings.location, then the
        location file), or None. Never a default city."""
        return self.tracker.location

    def catalog_entry_dicts(self, entries: list[CatalogEntry], *, with_distance: bool = False):
        now = datetime.now(UTC)
        location = self.receiver_location() if with_distance else None
        return [catalog_entry_dict(e, now, location) for e in entries]

    def catalog_near(self, hz: int, tolerance_hz: int = 5000) -> list[CatalogEntry]:
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        return self.catalog.near(hz, tolerance_hz=tolerance_hz, at=datetime.now(UTC))

    def catalog_sources_summary(self) -> dict:
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        current_season = season_code(datetime.now(UTC).date())
        sources = []
        for s in self.catalog.sources():
            d = s.model_dump(mode="json")
            # A season mismatch is just a reminder to refresh, not an error --
            # entries from last season are still shown (better stale than
            # empty), the UI decides how to surface it.
            d["stale"] = s.season is not None and s.season != current_season
            sources.append(d)
        # `available`: every known source (whether or not it's ever been
        # fetched), for the SETUP CATALOG group -- built from PARSERS rather
        # than the fetched-state list above so a never-fetched source (e.g. a
        # fresh install) still gets a row.
        descriptions = catalog_sources_help()
        available = []
        for key, parser in ALL_CATALOG_PARSERS.items():
            static = _is_static_parser(parser) and key not in _CATALOG_FETCHERS
            has_default_url = (
                key == "eibi" or key in CATALOG_DEFAULT_URLS or key in _CATALOG_FETCHERS
            )
            available.append(
                {
                    "source": key,
                    "label": CATALOG_LABELS.get(key, key),
                    "description": descriptions.get(key, ""),
                    "has_default_url": has_default_url,
                    "static": static,
                    "configured_url": self.settings.catalog_sources.get(key, ""),
                    # `group` tells station catalogs from allocation datasets;
                    # `kind` is the refresh model (docs/reference-catalogs.md).
                    "group": "catalog",
                    "kind": CATALOG_KINDS.get(key, "static" if static else "refreshable"),
                    "coverage": CATALOG_COVERAGE.get(key, ""),
                }
            )
        # Allocation datasets (docs/allocations.md) share the SETUP sources
        # group: a catalog refresh of one of these ids rebuilds the
        # allocation dataset instead (see catalog_refresh).
        for src in self.allocation_sources():
            available.append(
                {
                    "source": src.get("id"),
                    "label": src.get("title") or src.get("id"),
                    "description": src.get("publisher") or "",
                    "has_default_url": bool(src.get("url")),
                    "static": False,
                    "configured_url": src.get("url") or "",
                    "group": "allocation",
                    "kind": "refreshable",
                    "coverage": "",
                    "status": src.get("status"),
                    "edition": src.get("edition"),
                    "effective_date": src.get("effective_date"),
                    "imported_at": src.get("imported_at"),
                    "stale": bool(src.get("stale")),
                }
            )
        return {
            "sources": sources,
            "season": current_season,
            "available": available,
            "languages": self.catalog.languages(),
        }

    async def catalog_refresh(self, source: str) -> dict:
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        if source not in ALL_CATALOG_PARSERS and self.is_allocation_source(source):
            return await self.allocation_refresh(source)
        generic = ALL_CATALOG_PARSERS.get(source)
        url = self.settings.catalog_sources.get(source) or ""
        fetcher = _CATALOG_FETCHERS.get(source)
        if fetcher is not None and not url:
            try:
                entries, url = await asyncio.to_thread(fetcher)
            except Exception as exc:  # refresh reports errors without raising
                bundled = generic is not None and _is_static_parser(generic)
                if bundled and not self._has_rows(source):
                    # First load without network: the bundled dated snapshot
                    # (faa_nasr) beats an empty catalog. `note` keeps the
                    # download failure visible without flagging the load as
                    # an error.
                    try:
                        entries = generic()
                    except Exception as snap_exc:
                        return {"source": source, "entries": 0, "error": str(snap_exc)}
                    return self._store_refresh(
                        source,
                        entries,
                        url="",
                        note=f"download failed ({exc}); loaded bundled snapshot",
                    )
                return {"source": source, "entries": 0, "error": str(exc)}
            if not entries:
                return {
                    "source": source,
                    "entries": 0,
                    "error": "no entries parsed; catalog unchanged",
                }
            return self._store_refresh(source, entries, url=url)
        if generic is not None and _is_static_parser(generic) and source not in _CATALOG_FETCHERS:
            # Static sources (radiofax, sigidwiki, nist_time, superdarn) are
            # bundled tables: nothing to fetch. Stable ids make this idempotent.
            try:
                entries = generic()
            except Exception as exc:  # never raise: refresh reports failure, doesn't 500
                return {"source": source, "entries": 0, "error": str(exc)}
            return self._store_refresh(source, entries, url="")
        if not url and source == "eibi":
            url = eibi_default_url(season_code(datetime.now(UTC).date()))
        if not url:
            # Sources with a fixed public URL (ndxc, satnogs, ...) need no configuration.
            url = CATALOG_DEFAULT_URLS.get(source) or ""
        if not url:
            return {
                "source": source,
                "entries": 0,
                "error": f"no url configured for source: {source!r}",
            }
        # Local wrappers take (text, url) because EiBi derives the season from the
        # filename; the generic catalog parsers take the text only.
        wrapper = _CATALOG_PARSERS.get(source)
        generic = ALL_CATALOG_PARSERS.get(source)
        if wrapper is None and generic is None:
            return {"source": source, "entries": 0, "error": f"unknown catalog source: {source!r}"}
        encoding = _CATALOG_ENCODINGS.get(source, "utf-8")
        member = _CATALOG_ZIP_MEMBERS.get(source)
        extra = {"member": member} if member is not None else {}
        try:
            text = await asyncio.to_thread(fetch_catalog_text, url, encoding, **extra)
            entries = wrapper(text, url) if wrapper is not None else generic(text)
        except Exception as exc:  # never raise: refresh reports failure, doesn't 500
            return {"source": source, "entries": 0, "error": str(exc)}
        if not entries:
            # A wrong URL, an error page or a format change parses to nothing;
            # keep the source's current entries rather than wiping them.
            return {"source": source, "entries": 0, "error": "no entries parsed; catalog unchanged"}
        return self._store_refresh(source, entries, url=url)

    def _store_refresh(
        self, source: str, entries: list[CatalogEntry], *, url: str, note: str | None = None
    ) -> dict:
        """Commit a successfully parsed refresh, unless it would shrink the
        source by more than half: a truncated download or a format change
        can parse "successfully" to a fraction of the real dataset, and the
        previous rows are worth more than a plausible-looking stub. Sources
        below `_SHRINK_GUARD_MIN_ROWS` rows are too small for the ratio to
        mean anything and always replace; zero-row results are handled by
        the callers (same "catalog unchanged" outcome)."""
        previous = self.catalog.count_matches("", source=source)
        if previous >= _SHRINK_GUARD_MIN_ROWS and len(entries) * 2 < previous:
            return {
                "source": source,
                "entries": 0,
                "error": (
                    f"refresh produced {len(entries)} rows, previous dataset has {previous}; "
                    "refusing to shrink by more than half (download may be truncated)"
                ),
                "kept_previous": True,
            }
        self.catalog.replace_source(source, entries, url=url)
        result = {"source": source, "entries": len(entries), "error": None}
        if note is not None:
            result["note"] = note
        return result

    def catalog_import_file(self, path: str, fmt: str) -> dict:
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        p = Path(path).expanduser()
        if not p.exists():
            raise HTTPException(400, f"file not found: {path!r}")
        if p.is_dir():
            raise HTTPException(400, f"path is a directory: {path!r}")
        if p.stat().st_uid != os.getuid():
            raise HTTPException(403, "file not owned by current user")
        size = p.stat().st_size
        if size > _MAX_IMPORT_BYTES:
            # Read into memory and parsed in one go, so an accidental 2 GB
            # recording would take the sidecar down with it.
            raise HTTPException(
                400,
                f"file too large: {size} bytes (max {_MAX_IMPORT_BYTES // (1024 * 1024)} MB)",
            )
        text = p.read_text()
        if fmt == "sdrpp":
            entries = parse_sdrpp_json(text, filename=p.name)
        elif fmt == "gqrx":
            entries = parse_gqrx_csv(text, filename=p.name)
        else:
            raise HTTPException(400, f"unknown import format: {fmt!r}")
        source = f"user:{p.name}"
        self.catalog.replace_source(source, entries)
        return {"source": source, "entries": len(entries), "error": None}

    def _has_rows(self, source: str) -> bool:
        return any(s.source == source and s.entry_count > 0 for s in self.catalog.sources())

    def _tunable_entry(self, entry_id: str) -> CatalogEntry:
        """The entry, or 404 when unknown / 409 when it has no verified exact
        frequency (DME/TACAN, radar sites): tune_recipe would raise."""
        if not self.settings.features.catalog:
            raise HTTPException(404, "feature disabled: catalog")
        entry = self.catalog.get(entry_id)
        if entry is None:
            raise HTTPException(404, f"catalog entry not found: {entry_id!r}")
        if entry.frequency_hz is None:
            raise HTTPException(409, f"{entry.station} has no verified exact frequency")
        return entry

    async def tune_catalog(self, entry_id: str) -> RestoreReport:
        entry = self._tunable_entry(entry_id)
        frequency_hz, demodulator, bandwidth_hz = tune_recipe(entry)
        recipe = TuneRecipe(
            frequency_hz=frequency_hz, demodulator=demodulator, filter_bandwidth_hz=bandwidth_hz
        )
        steps, desired = plan_with_desired(confirmed=self.confirmed.props, recipe=recipe)
        return await self._restore(steps, desired=desired, kind="tune", note=entry.station)

    def save_catalog_entry(self, entry_id: str) -> Signal:
        """Save a catalog entry as an inbox signal using its real tune recipe
        (mode/bandwidth from the entry, not a hardcoded AM/9kHz). Backs both
        CATALOG's and LISTEN's "★" actions so they agree with what tuning the
        entry would actually set."""
        entry = self._tunable_entry(entry_id)
        frequency_hz, demodulator, bandwidth_hz = tune_recipe(entry)
        signal = Signal(
            name=entry.station,
            frequency_hz=frequency_hz,
            demodulator=demodulator,
            filter_bandwidth_hz=bandwidth_hz,
            status="identified",
            station=entry.station,
            latitude=entry.latitude,
            longitude=entry.longitude,
            notes=_catalog_note_line(entry),
        )
        self.signals.add(signal)
        self.library_undo.record(f"create {signal.name}", [(signal.id, None)])
        return signal
