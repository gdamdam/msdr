"""Satellite tracking: live az/el/range, sky tracks and Doppler tuning.

skyfield does the orbital mechanics; this module owns the TLE (OMM) cache, the
catalog of trackable satellites, and translating skyfield results into the
plain models the API/UI consume. No network imports outside `fetch_omm`
(mirrors nextpass.py/bandplan.py) -- `fetch_omm` is used only by the refresh
route, and tests monkeypatch it.
"""

import asyncio
import contextlib
import json
import math
import urllib.request
import warnings
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from pydantic import BaseModel
from skyfield.api import EarthSatellite, load, load_file, wgs84

from msdr.providers.nextpass import Pass
from msdr.settings import Location, Settings

_CATALOG_PATH = Path(__file__).parent / "data" / "satellites.json"
CATALOG: list[dict] = json.loads(_CATALOG_PATH.read_text())
CATALOG_BY_LABEL: dict[str, dict] = {entry["label"]: entry for entry in CATALOG}

_SPEED_OF_LIGHT_KMS = 299792.458
_RANGE_RATE_HALF_STEP_S = 0.5  # central-difference half-step for range_rate_kms
_TRACK_STEP_S = 15.0

_FETCH_TIMEOUT_S = 20

# Fields sgp4.omm.initialize() reads unconditionally (via EarthSatellite.from_omm)
# -- a cached entry missing any of these raises a bare KeyError instead of the
# uniform "resolve failed" error every other _resolve path already returns.
_REQUIRED_OMM_FIELDS = frozenset(
    {
        "CLASSIFICATION_TYPE",
        "OBJECT_ID",
        "EPHEMERIS_TYPE",
        "ELEMENT_SET_NO",
        "REV_AT_EPOCH",
        "EPOCH",
        "ARG_OF_PERICENTER",
        "BSTAR",
        "ECCENTRICITY",
        "INCLINATION",
        "MEAN_ANOMALY",
        "MEAN_MOTION_DDOT",
        "MEAN_MOTION_DOT",
        "MEAN_MOTION",
        "RA_OF_ASC_NODE",
        "NORAD_CAT_ID",
    }
)

# de421.bsp, cached alongside TLEs (see AppService._load_visible_ephemeris) --
# used only for passes_visible_only's sunlit/dark-observer computation.
EPHEMERIS_NAME = "de421.bsp"
# Sun altitude at/below which the observer counts as "dark" for visibility,
# matching nextpass's own --visible-only default (planning_features.py).
_MAX_SUN_ALTITUDE_DEG = -6.0

# Named so leaks are identifiable in asyncio.all_tasks() (and in tests).
_RUN_TASK_NAME = "msdr-doppler-loop"
_WATCH_TASK_NAME = "msdr-doppler-watch"
_STOP_TASK_NAME = "msdr-doppler-stop"


def _pass_geometry(max_elevation_deg: float) -> str:
    """Plain-language read on how good a pass is, by peak elevation."""
    if max_elevation_deg >= 60:
        return "Excellent"
    if max_elevation_deg >= 40:
        return "Good"
    if max_elevation_deg >= 20:
        return "Fair"
    return "Low"


def _ensure_utc(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _parse_epoch(epoch: str | None) -> datetime | None:
    if not epoch:
        return None
    try:
        parsed = datetime.fromisoformat(epoch)
    except ValueError:
        return None
    return _ensure_utc(parsed)


def resolve_location(settings: Settings) -> Location | None:
    """settings.location -> settings.location_file -> None (per the contract)."""
    if settings.location is not None:
        return settings.location
    path = Path(settings.location_file).expanduser()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return Location(
            lat=data["lat"],
            lon=data["lon"],
            altitude_m=data.get("altitude", 0),
            timezone=data.get("timezone", "UTC"),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Covers a missing/unreadable file (including a sandboxed/denied
        # ~/.config) the same as a plain "doesn't exist" -- tracking degrades
        # to "no location configured" rather than the app failing to start.
        return None


class TleStore:
    """OMM (CelesTrak GP JSON) cache across one or more directories.

    Each directory may hold `<norad>.json`, either a single OMM object or a
    list with one (CelesTrak's own gp.php shape, and what nextpass writes).
    """

    def __init__(self, dirs: list[Path]) -> None:
        self.dirs = dirs

    def _read(self, path: Path) -> dict | None:
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(doc, list):
            return doc[0] if doc else None
        return doc

    def load(self, norad: int) -> dict | None:
        best: dict | None = None
        best_epoch: datetime | None = None
        for directory in self.dirs:
            omm = self._read(directory / f"{norad}.json")
            if omm is None:
                continue
            epoch = _parse_epoch(omm.get("EPOCH"))
            if epoch is None:
                continue
            if best_epoch is None or epoch > best_epoch:
                best, best_epoch = omm, epoch
        return best

    def save(self, norad: int, omm: dict | list, dir: Path) -> None:
        dir.mkdir(parents=True, exist_ok=True)
        (dir / f"{norad}.json").write_text(json.dumps(omm))

    def age_days(self, norad: int) -> float | None:
        omm = self.load(norad)
        if omm is None:
            return None
        epoch = _parse_epoch(omm.get("EPOCH"))
        if epoch is None:
            return None
        return (datetime.now(UTC) - epoch).total_seconds() / 86400.0


def load_cached_ephemeris(cache_dir: Path):
    """de421.bsp from `cache_dir` if it's already there, else None -- never
    touches the network (mirrors TleStore.load's "cache or nothing"). Callers
    that want to actually fetch it use fetch_ephemeris, from the explicit
    refresh path only."""
    path = cache_dir / EPHEMERIS_NAME
    if not path.is_file():
        return None
    with contextlib.suppress(Exception):
        # skyfield's own load_file; any failure (truncated/corrupt download)
        # degrades to "no ephemeris" rather than raising.
        return load_file(str(path))
    return None


async def fetch_ephemeris(cache_dir: Path) -> None:
    """Download de421.bsp into cache_dir if not already cached. Refresh route
    only (mirrors fetch_omm) -- passes()/position() never fetch on their own."""

    def _fetch() -> None:
        from skyfield.api import Loader

        cache_dir.mkdir(parents=True, exist_ok=True)
        Loader(str(cache_dir))(EPHEMERIS_NAME)

    await asyncio.to_thread(_fetch)


async def fetch_omm(norad: int) -> dict:
    """Fetch a single satellite's OMM from CelesTrak. Refresh route only."""

    def _fetch() -> dict:
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=json"
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            doc = json.loads(resp.read().decode())
        if isinstance(doc, list):
            if not doc:
                raise ValueError(f"no OMM data returned for NORAD {norad}")
            return doc[0]
        return doc

    return await asyncio.to_thread(_fetch)


class SatPosition(BaseModel):
    satellite: str
    norad: int
    at: str
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    range_rate_kms: float
    visible: bool


class TrackPoint(BaseModel):
    at: str
    azimuth_deg: float
    elevation_deg: float


class SkyTrack(BaseModel):
    satellite: str
    rise: str
    peak: str
    set: str
    max_elevation_deg: float
    points: list[TrackPoint] = []


class SkyResult(BaseModel):
    position: SatPosition | None = None
    track: SkyTrack | None = None
    error: str | None = None
    tle_age_days: float | None = None


class SatTracker:
    """Computes live positions, tracks and Doppler shift for catalog satellites.

    Never raises to routes: every lookup failure (unknown label, missing TLE,
    no location configured) comes back as a `SkyResult`/`None` carrying an
    `error`, not an exception -- except `doppler_hz`, which is only ever
    called from `DopplerLoop` (never directly from a route) and raises
    `LookupError` so the loop can treat it the same as any other stop
    condition.
    """

    def __init__(
        self,
        store: TleStore,
        location: Location | None,
        extra: dict[str, dict] | None = None,
    ) -> None:
        self.store = store
        self.location = location
        # passes_extra_satellites, keyed by label, in the same {"label",
        # "norad"} shape as CATALOG entries -- see AppService._extra_catalog.
        self.extra_by_label = extra or {}
        self.ts = load.timescale(builtin=True)

    def _resolve(
        self, label: str
    ) -> tuple[EarthSatellite, int, float | None, None] | tuple[None, None, None, str]:
        entry = CATALOG_BY_LABEL.get(label) or self.extra_by_label.get(label)
        if entry is None:
            return None, None, None, f"unknown satellite: {label!r}"
        norad = entry["norad"]
        omm = self.store.load(norad)
        if omm is None:
            return None, None, None, f"no TLE cached for {label} (NORAD {norad})"
        missing = _REQUIRED_OMM_FIELDS - omm.keys()
        if missing:
            return (
                None,
                None,
                None,
                f"orbital cache entry for {label} is incomplete — refresh the cache",
            )
        satellite = EarthSatellite.from_omm(self.ts, omm)
        age_days = self.store.age_days(norad)
        return satellite, norad, age_days, None

    def _observer(self):
        assert self.location is not None
        return wgs84.latlon(
            self.location.lat, self.location.lon, elevation_m=self.location.altitude_m
        )

    def _range_rate_kms(self, diff, t) -> float:
        half_step_days = _RANGE_RATE_HALF_STEP_S / 86400.0
        before = self.ts.tt_jd(t.tt - half_step_days)
        after = self.ts.tt_jd(t.tt + half_step_days)
        d_before = diff.at(before).distance().km
        d_after = diff.at(after).distance().km
        return (d_after - d_before) / (2 * _RANGE_RATE_HALF_STEP_S)

    def position(self, label: str, at: datetime | None = None) -> SkyResult:
        satellite, norad, age_days, error = self._resolve(label)
        if error is not None:
            return SkyResult(error=error)
        if self.location is None:
            return SkyResult(error="no location configured", tle_age_days=age_days)

        t = self.ts.from_datetime(_ensure_utc(at)) if at is not None else self.ts.now()
        diff = satellite - self._observer()
        topo = diff.at(t)
        alt, az, dist = topo.altaz()
        pos = SatPosition(
            satellite=label,
            norad=norad,
            at=t.utc_iso(),
            azimuth_deg=az.degrees,
            elevation_deg=alt.degrees,
            range_km=dist.km,
            range_rate_kms=self._range_rate_kms(diff, t),
            visible=alt.degrees > 0,
        )
        return SkyResult(position=pos, tle_age_days=age_days)

    def track(
        self, label: str, start: datetime, end: datetime, step_s: float = _TRACK_STEP_S
    ) -> SkyTrack | None:
        satellite, norad, _age_days, error = self._resolve(label)
        if error is not None or self.location is None:
            return None

        t_start = self.ts.from_datetime(_ensure_utc(start))
        t_end = self.ts.from_datetime(_ensure_utc(end))
        diff = satellite - self._observer()

        if t_end.tt <= t_start.tt or step_s <= 0:
            return None
        samples = list(np.arange(t_start.tt, t_end.tt, step_s / 86400.0))
        samples.append(t_end.tt)
        # A 15-second grid can miss the narrow peak of an overhead pass by
        # several degrees. Include exact culminations as well as endpoints.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            event_times, events = satellite.find_events(self._observer(), t_start, t_end)
        samples.extend(ti.tt for ti, event in zip(event_times, events, strict=True) if event == 1)
        times = self.ts.tt_jd(np.array(sorted(set(samples))))
        alt, az, _dist = diff.at(times).altaz()
        points = [
            TrackPoint(at=ti.utc_iso(), azimuth_deg=float(azimuth), elevation_deg=float(elevation))
            for ti, azimuth, elevation in zip(times, az.degrees, alt.degrees, strict=True)
        ]

        peak = max(points, key=lambda p: p.elevation_deg)
        return SkyTrack(
            satellite=label,
            rise=points[0].at,
            peak=peak.at,
            set=points[-1].at,
            max_elevation_deg=peak.elevation_deg,
            points=points,
        )

    def next_or_current_pass(
        self,
        label: str,
        horizon_deg: float = 0.0,
        hours: float = 24.0,
        start: datetime | None = None,
    ) -> SkyTrack | None:
        """`start` defaults to now; passing it pins the search window, which
        is what makes this testable against a fixed TLE epoch (same escape
        hatch as passes())."""
        satellite, _norad, _age_days, error = self._resolve(label)
        if error is not None or self.location is None:
            return None

        t0 = self.ts.from_datetime(start if start is not None else datetime.now(UTC))
        t1 = self.ts.tt_jd(t0.tt + hours / 24.0)
        with warnings.catch_warnings():
            # skyfield 1.55's _find_discrete sets a bool ndarray's .dtype in
            # place, which recent numpy deprecates (numpy#..., not our bug) --
            # scoped to this exact call, same pattern as test_app.py's own
            # third-party warning suppression.
            warnings.simplefilter("ignore", DeprecationWarning)
            times, events = satellite.find_events(
                self._observer(), t0, t1, altitude_degrees=horizon_deg
            )

        rise_t = None
        for ti, ev in zip(times, events, strict=True):
            if ev == 0:  # rise
                rise_t = ti
            elif ev == 2:  # set
                start = rise_t if rise_t is not None else t0
                return self.track(label, start.utc_datetime(), ti.utc_datetime())
        return None

    def _annotate_visible(
        self, p: Pass, satellite: EarthSatellite, peak_t, observer, ephemeris
    ) -> None:
        """A pass is optically visible if any sampled portion is sunlit in dark sky.

        Sample the entire configured rise/set interval, including both endpoints
        and culmination. Visibility near rise/set must not be lost just because
        the satellite is in shadow at culmination.
        """
        start = datetime.fromisoformat(p.rise)
        end = datetime.fromisoformat(p.set)
        t0, t1 = self.ts.from_datetime(start), self.ts.from_datetime(end)
        count = max(1, math.ceil((end - start).total_seconds() / 10.0))
        times = self.ts.tt_jd(np.concatenate((np.linspace(t0.tt, t1.tt, count + 1), [peak_t.tt])))
        sunlit = satellite.at(times).is_sunlit(ephemeris)
        sun_alt, _, _ = (
            (ephemeris["earth"] + observer).at(times).observe(ephemeris["sun"]).apparent().altaz()
        )
        p.visible = bool(np.any(sunlit & (sun_alt.degrees <= _MAX_SUN_ALTITUDE_DEG)))

    def passes(
        self,
        labels: list[str],
        *,
        days: int,
        min_elevation: float,
        horizon: float,
        top: int,
        start: datetime | None = None,
        ephemeris=None,
        visible_only: bool = False,
    ) -> list[Pass]:
        """Predict passes for `labels` over the next `days`, in the same
        `Pass` shape nextpass.py emits -- AppService.passes() serves either
        engine behind one contract (see api/service.py). A label with no
        cached TLE (or unknown to the catalog) is silently skipped; that's
        the caller's job to turn into a reported `error`. Never raises.

        `ephemeris` (a loaded de421.bsp, or None) annotates visibility
        whether or not filtering is enabled. `visible_only` additionally drops
        non-visible passes, but only once ephemeris is actually available;
        with no ephemeris cached yet, every pass is kept with visible=None
        rather than being silently hidden."""
        if self.location is None:
            return []
        tz = ZoneInfo(self.location.timezone)
        t0 = self.ts.from_datetime(_ensure_utc(start)) if start is not None else self.ts.now()
        t1 = self.ts.tt_jd(t0.tt + days)
        observer = self._observer()

        results: list[Pass] = []
        for label in labels:
            satellite, norad, _age_days, error = self._resolve(label)
            if error is not None:
                continue
            diff = satellite - observer
            with warnings.catch_warnings():
                # see next_or_current_pass's own comment on skyfield 1.55's
                # _find_discrete/numpy DeprecationWarning.
                warnings.simplefilter("ignore", DeprecationWarning)
                times, events = satellite.find_events(observer, t0, t1, altitude_degrees=horizon)

            rise_t = peak_t = None
            for ti, ev in zip(times, events, strict=True):
                if ev == 0:  # rise
                    rise_t, peak_t = ti, None
                elif ev == 1:  # culminate
                    peak_t = ti
                elif ev == 2:  # set
                    if rise_t is None or peak_t is None:
                        # Already above the horizon (or already culminated)
                        # at t0 -- an incomplete pass, not predictable from
                        # rise onward, so skip it rather than guess.
                        rise_t = peak_t = None
                        continue
                    set_t = ti
                    peak_alt, peak_az, peak_dist = diff.at(peak_t).altaz()
                    if peak_alt.degrees >= min_elevation:
                        rise_alt, rise_az, _rise_dist = diff.at(rise_t).altaz()
                        set_alt, set_az, _set_dist = diff.at(set_t).altaz()
                        window_minutes = (set_t.tt - rise_t.tt) * 1440.0
                        p = Pass(
                            satellite=label,
                            norad=norad,
                            rise=rise_t.utc_datetime().astimezone(tz).isoformat(),
                            peak=peak_t.utc_datetime().astimezone(tz).isoformat(),
                            set=set_t.utc_datetime().astimezone(tz).isoformat(),
                            max_elevation_deg=peak_alt.degrees,
                            rise_azimuth_deg=rise_az.degrees,
                            peak_azimuth_deg=peak_az.degrees,
                            set_azimuth_deg=set_az.degrees,
                            range_at_peak_km=peak_dist.km,
                            window_minutes=window_minutes,
                            geometry=_pass_geometry(peak_alt.degrees),
                        )
                        if ephemeris is not None:
                            self._annotate_visible(p, satellite, peak_t, observer, ephemeris)
                        results.append(p)
                    rise_t = peak_t = None

        if visible_only and ephemeris is not None:
            # Fail open when there's no ephemeris yet (see docstring) --
            # only filter once visibility was actually computed.
            results = [p for p in results if p.visible]
        results.sort(key=lambda p: p.max_elevation_deg, reverse=True)
        top_results = results[:top]
        for i, p in enumerate(top_results, start=1):
            p.rank = i
        return top_results

    def doppler_hz(self, label: str, f0_hz: float, at: datetime | None = None) -> float:
        result = self.position(label, at)
        if result.position is None:
            raise LookupError(result.error or f"no position for {label!r}")
        return -f0_hz * result.position.range_rate_kms / _SPEED_OF_LIGHT_KMS


class DopplerLoop:
    """Ticks every `settings.doppler_interval_s`, re-tuning the VFO to track
    a bound satellite's Doppler shift. Never touches history/journal -- this
    is a continuous background adjustment, not a user action worth recording.
    """

    def __init__(self, service) -> None:
        self.service = service
        self.signal_id: str | None = None
        self.satellite: str | None = None
        self.f0_hz: float | None = None
        self._last_commanded_hz: float | None = None
        self._doppler_hz: float = 0.0
        self._elevation_deg: float = 0.0
        self._task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        # Strong reference to the stop() spawned by _watch_external: a bare
        # create_task() can be collected mid-flight, leaving the loop running.
        self._stop_task: asyncio.Task | None = None
        self._confirmed_q: asyncio.Queue | None = None

    @property
    def status(self) -> dict | None:
        if self.signal_id is None:
            return None
        return {
            "signal_id": self.signal_id,
            "satellite": self.satellite,
            "f0_hz": self.f0_hz,
            "doppler_hz": self._doppler_hz,
            "elevation_deg": self._elevation_deg,
            "active": True,
        }

    async def start(self, signal_id: str, satellite: str, f0_hz: float) -> dict:
        """Start tracking. Caller (AppService) has already validated the
        signal exists, has a bound satellite, and the receiver is connected
        and started -- this class stays free of the web-framework HTTPException
        mapping that validation implies (only api/app.py and service.py use
        those directly)."""
        await self.stop()

        self.signal_id = signal_id
        self.satellite = satellite
        self.f0_hz = f0_hz
        # The VFO is assumed already at f0 (no Doppler applied yet) when
        # tracking starts, so the first real correction is judged against f0,
        # not against an arbitrary sentinel.
        self._last_commanded_hz = self.f0_hz
        self._doppler_hz = 0.0
        self._elevation_deg = 0.0

        self._confirmed_q = self.service.confirmed.subscribe()
        self._watch_task = asyncio.create_task(self._watch_external(), name=_WATCH_TASK_NAME)
        self._task = asyncio.create_task(self._run(), name=_RUN_TASK_NAME)
        return self.status

    async def stop(self) -> None:
        """Idempotent, and safe to call from inside the loop's own task: a
        task that cancels itself here would suppress its own CancelledError
        and keep looping with `_task = None`, so the next start() would leave
        two loops driving one VFO."""
        pending = self._stop_task
        if pending is not None and pending is not asyncio.current_task():
            self._stop_task = None
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        task = self._task
        if task is not None and task is not asyncio.current_task():
            self._task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._teardown()

    async def _teardown(self) -> None:
        """Release the subscription and clear the binding. Idempotent, and
        never cancels the calling task (see stop())."""
        was_active = self.signal_id is not None
        watch = self._watch_task
        self._watch_task = None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        if self._confirmed_q is not None:
            self.service.confirmed.unsubscribe(self._confirmed_q)
            self._confirmed_q = None
        self.signal_id = None
        self.satellite = None
        self.f0_hz = None
        self._last_commanded_hz = None
        if was_active:
            await self.service.broadcaster.publish({"type": "tracking"})

    async def _watch_external(self) -> None:
        assert self._confirmed_q is not None
        while True:
            sc = await self._confirmed_q.get()
            if sc.property == "device_vfo_frequency" and sc.cause == "external":
                self._stop_task = asyncio.create_task(self.stop(), name=_STOP_TASK_NAME)
                return

    async def _tick(self) -> str | None:
        """Returns a stop reason, or None to keep ticking. _run() (not _tick)
        owns teardown, so a tick can never cancel the task it runs in."""
        service = self.service
        if not service.adapter.connected or not service.confirmed.props.get("started", False):
            return "receiver unavailable"
        try:
            result = service.tracker.position(self.satellite)
        except Exception:
            return "tracker error"
        if result.position is None or not result.position.visible:
            return "below horizon"

        self._elevation_deg = result.position.elevation_deg
        self._doppler_hz = -self.f0_hz * result.position.range_rate_kms / _SPEED_OF_LIGHT_KMS
        target_hz = round(self.f0_hz + self._doppler_hz)
        if (
            self._last_commanded_hz is None
            or abs(target_hz - self._last_commanded_hz) >= service.settings.doppler_min_step_hz
        ):
            service.confirmed.expect("device_vfo_frequency", target_hz)
            await service.adapter.set("device_vfo_frequency", target_hz)
            service.dwell.note_app_action()
            self._last_commanded_hz = target_hz

        await service.broadcaster.publish({"type": "tracking", **self.status})
        return None

    async def _run(self) -> None:
        service = self.service
        while True:
            await asyncio.sleep(service.settings.doppler_interval_s)
            if await self._tick() is not None:
                break
        # Clear _task first so a stop() racing this teardown doesn't cancel
        # a task that is already on its way out.
        self._task = None
        await self._teardown()
