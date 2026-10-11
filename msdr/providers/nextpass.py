"""nextpass CLI integration: satellite pass predictions for the PASSES section.

Runs the user's own `nextpass` binary as a subprocess and parses its --json
output. No network imports here (F1, mirrors bandplan.py): nextpass itself
only touches the network when refresh=True asks for --refresh; every other
call passes --offline.
"""

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from msdr.library.models import now_iso
from msdr.settings import Location

SATELLITES = ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"]

_DEFAULT_TIMEOUT_S = 60
_REFRESH_TIMEOUT_S = 120
_STDERR_TAIL_CHARS = 500


class Downlink(BaseModel):
    frequency_hz: int
    mode: str
    label: str


class Pass(BaseModel):
    satellite: str
    norad: int
    rise: str
    peak: str
    set: str
    max_elevation_deg: float
    rise_azimuth_deg: float
    peak_azimuth_deg: float
    set_azimuth_deg: float
    range_at_peak_km: float
    window_minutes: float
    geometry: str | None = None
    rank: int | None = None
    # Primary downlink for ▸-tune with no bound memory. Filled here from
    # nextpass's own --radio output (fallback source); AppService overrides
    # it with msdr's own SatNOGS catalog when that has a match (source of
    # truth -- see AppService._catalog_downlink).
    downlink: Downlink | None = None
    # Sunlit + dark observer during any sampled portion of the pass.
    # None when visibility data is unavailable (including nextpass results).
    visible: bool | None = None


class PassesResult(BaseModel):
    passes: list[Pass] = []
    generated_at: str
    source: str = "fresh"  # "cache" | "fresh" | "internal"
    timezone: str | None = None
    error: str | None = None
    # Which predictor produced `passes` -- AppService.passes() picks the
    # internal skyfield predictor when TLEs are cached and a location is
    # configured, and falls back to this provider otherwise (see api/service.py).
    engine: str = "nextpass"  # "internal" | "nextpass"


def _radio_downlink(rows: list) -> Downlink | None:
    """First usable transmitter row (nextpass radio_metadata.normalize_transmitter
    shape) -> a Downlink, or None. "Usable" just means it carries a frequency;
    picking the first one is an approximation of "primary" -- nextpass doesn't
    rank transmitters, and SatNOGS DB tends to list the main transmitter first."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        freq = row.get("frequency_hz") or row.get("downlink_low_hz")
        if not freq:
            continue
        mode = row.get("mode") or row.get("expected_mode") or "unknown"
        label = row.get("description") or f"{mode} downlink"
        try:
            return Downlink(frequency_hz=int(freq), mode=str(mode), label=str(label))
        except (TypeError, ValueError, ValidationError):
            continue
    return None


def _attach_radio_downlinks(passes: list[Pass], radio: dict | None) -> None:
    """Fills each Pass.downlink from --radio's per-NORAD transmitter list, if
    present. `radio` may be the load_radio_metadata() envelope (a "satellites"
    dict keyed by NORAD string) or the {"status": "unavailable", ...} shape
    meteor_passes.py substitutes when radio metadata couldn't be loaded --
    either way, a missing/malformed "satellites" dict just means no downlinks."""
    satellites = (radio or {}).get("satellites")
    if not isinstance(satellites, dict):
        return
    for p in passes:
        rows = satellites.get(str(p.norad))
        if isinstance(rows, list):
            p.downlink = _radio_downlink(rows)


class NextPassProvider:
    name = "nextpass"

    def __init__(self, bin: str = "nextpass", cache_ttl_s: float = 600.0) -> None:
        self.bin = bin
        self.cache_ttl_s = cache_ttl_s
        # Keyed on the exact query so a different satellite set/window/location
        # never serves another query's cached passes.
        self._cache: dict[tuple, tuple[float, PassesResult]] = {}

    async def passes(
        self,
        satellites: list[str],
        *,
        days: int,
        min_elevation: float,
        horizon: float,
        top: int,
        location: Location | None,
        refresh: bool = False,
        extra_satellites: list[dict] | None = None,
    ) -> PassesResult:
        """`extra_satellites` (settings.passes_extra_satellites, as
        [{"label", "norad"}, ...]) is written out as a nextpass `--catalog`
        JSON file so the fallback engine can predict for them too -- see
        AppService._extra_catalog, the same shape the internal predictor's
        SatTracker(extra=...) takes."""
        loc_key = (
            (location.lat, location.lon, location.altitude_m, location.timezone)
            if location is not None
            else None
        )
        extra_key = tuple(sorted((e["label"], e["norad"]) for e in (extra_satellites or [])))
        key = (tuple(satellites), days, min_elevation, horizon, top, loc_key, extra_key)
        now = time.monotonic()
        if not refresh:
            cached = self._cache.get(key)
            if cached is not None and (now - cached[0]) < self.cache_ttl_s:
                return cached[1].model_copy(update={"source": "cache"})

        result = await self._run(
            satellites,
            days=days,
            min_elevation=min_elevation,
            horizon=horizon,
            top=top,
            location=location,
            refresh=refresh,
            extra_satellites=extra_satellites,
        )
        # Only a successful run repopulates the cache -- caching an error
        # would otherwise pin a transient failure for the full TTL.
        if result.error is None:
            self._cache[key] = (now, result)
        return result

    async def _run(
        self,
        satellites: list[str],
        *,
        days: int,
        min_elevation: float,
        horizon: float,
        top: int,
        location: Location | None,
        refresh: bool,
        extra_satellites: list[dict] | None = None,
    ) -> PassesResult:
        tmpdir = tempfile.mkdtemp()
        try:
            json_path = Path(tmpdir) / "nextpass.json"
            args = [
                self.bin,
                "--json",
                str(json_path),
                "--days",
                str(days),
                "--min-elevation",
                str(min_elevation),
                "--horizon",
                str(horizon),
                "--top",
                str(top),
                "--satellites",
                ",".join(satellites),
                # Optional SatNOGS transmitter metadata (fallback downlink
                # source -- see Pass.downlink). Safe with --offline: nextpass
                # serves its own cached radio.json, or reports the metadata
                # "unavailable" without touching the network; only --refresh
                # (an explicit user action) lets it actually fetch.
                "--radio",
            ]
            if extra_satellites:
                # nextpass's own --catalog schema: (label, group, name) per
                # NORAD id, all required non-empty strings -- "custom"/the
                # label itself are reasonable stand-ins for a user-added
                # satellite that only has a label and a NORAD id.
                catalog_path = Path(tmpdir) / "catalog.json"
                catalog_path.write_text(
                    json.dumps(
                        [
                            {
                                "norad": e["norad"],
                                "label": e["label"],
                                "group": "custom",
                                "name": e["label"],
                            }
                            for e in extra_satellites
                        ]
                    )
                )
                args += ["--catalog", str(catalog_path)]
            if location is not None:
                args += [
                    "--lat",
                    str(location.lat),
                    "--lon",
                    str(location.lon),
                    "--altitude",
                    str(location.altitude_m),
                    "--timezone",
                    location.timezone,
                ]
            args.append("--refresh" if refresh else "--offline")
            timeout = _REFRESH_TIMEOUT_S if refresh else _DEFAULT_TIMEOUT_S

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                return PassesResult(generated_at=now_iso(), error="nextpass not installed")
            except OSError as exc:
                # e.g. nextpass_bin points at a non-executable file
                return PassesResult(generated_at=now_iso(), error=f"cannot run {self.bin}: {exc}")

            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return PassesResult(
                    generated_at=now_iso(), error=f"{self.bin} timed out after {timeout}s"
                )

            if proc.returncode != 0:
                tail = stderr.decode(errors="replace").strip()[-_STDERR_TAIL_CHARS:]
                return PassesResult(
                    generated_at=now_iso(),
                    error=tail or f"{self.bin} exited {proc.returncode}",
                )

            # Valid JSON with an unexpected shape must degrade the same way as bad JSON:
            # the route contract is "never raises".
            try:
                doc = json.loads(json_path.read_text())
                passes = [Pass.model_validate(p) for p in doc.get("passes", [])]
                timezone = doc.get("timezone")
                _attach_radio_downlinks(passes, doc.get("radio"))
            except (OSError, json.JSONDecodeError, ValidationError, AttributeError, TypeError) as e:
                return PassesResult(generated_at=now_iso(), error=f"bad nextpass output: {e}")

            return PassesResult(
                passes=passes, generated_at=now_iso(), source="fresh", timezone=timezone
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
