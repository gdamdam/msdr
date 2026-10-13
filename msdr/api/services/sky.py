"""Satellite passes, skyfield tracking and Doppler tuning.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from msdr.api.services._common import _ALL_SIGNALS_LIMIT, RestoreRunning
from msdr.library.models import now_iso
from msdr.providers.nextpass import SATELLITES, Downlink, Pass, PassesResult
from msdr.providers.sat_decode import decode_plan
from msdr.providers.tracking import (
    CATALOG,
    CATALOG_BY_LABEL,
    SkyResult,
    TleStore,
    fetch_ephemeris,
    fetch_omm,
    load_cached_ephemeris,
)
from msdr.settings import Settings


def _ics_escape(text: str) -> str:
    """RFC 5545 TEXT escaping: backslash, comma, semicolon and newline are
    the only characters that need it for the plain SUMMARY/DESCRIPTION
    values passes_ics() builds."""
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _ics_utc(iso: str) -> str:
    """An ISO-8601 timestamp (naive-UTC or tz-aware, per Pass.rise/peak/set)
    as an RFC 5545 UTC DATE-TIME (YYYYMMDDTHHMMSSZ)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _passes_ics(passes: list[Pass]) -> str:
    """Hand-rolled iCalendar (no new dependency, per the PASSES calendar
    export): one VEVENT per predicted pass, rise->set, with the downlink (if
    any) folded into DESCRIPTION rather than a separate property -- no
    standard VEVENT field fits "downlink frequency/mode" any better."""
    stamp = _ics_utc(now_iso())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//msdr//passes//EN", "CALSCALE:GREGORIAN"]
    for p in passes:
        summary = f"{p.satellite} max {round(p.max_elevation_deg)}\u00b0"
        rise_az, peak_az, set_az = (
            round(p.rise_azimuth_deg),
            round(p.peak_azimuth_deg),
            round(p.set_azimuth_deg),
        )
        az_line = f"az {rise_az}\u00b0\u2192{peak_az}\u00b0\u2192{set_az}\u00b0"
        description = [f"peak {round(p.max_elevation_deg)}\u00b0, {az_line}"]
        if p.downlink is not None:
            description.append(
                f"downlink {p.downlink.frequency_hz / 1e6:.4f} MHz {p.downlink.mode} "
                f"({p.downlink.label})"
            )
        lines += [
            "BEGIN:VEVENT",
            f"UID:{p.satellite}-{_ics_utc(p.rise)}@msdr",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_utc(p.rise)}",
            f"DTEND:{_ics_utc(p.set)}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape('; '.join(description))}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class SkyServiceMixin:
    def _build_tle_store(self, settings: Settings) -> TleStore:
        return TleStore(
            [
                Path(settings.tle_cache_dir).expanduser(),
                Path(settings.nextpass_cache_dir).expanduser(),
            ]
        )

    def _extra_catalog(self) -> dict[str, dict]:
        """settings.passes_extra_satellites, in the {"label", "norad"} shape
        SatTracker/CATALOG entries use -- see settings.ExtraSatellite."""
        return {
            e.label: {"label": e.label, "norad": e.norad_id}
            for e in self.settings.passes_extra_satellites
        }

    def _catalog_entry(self, label: str) -> dict | None:
        return CATALOG_BY_LABEL.get(label) or self._extra_catalog().get(label)

    def _passes_satellites(self) -> list[str]:
        if self.settings.passes_satellites:
            base = list(self.settings.passes_satellites)
        else:
            # Satellites named on any saved Signal, or every catalog label
            # when none are pinned yet (per the contract).
            all_signals = self.signals.list(limit=_ALL_SIGNALS_LIMIT)
            pinned = sorted({s.satellite for s in all_signals if s.satellite})
            base = pinned or list(SATELLITES)
        # Extra satellites are additive to whichever of the above applied --
        # they're NORAD-id-only and can't join the Satellite Literal that
        # passes_satellites/base is drawn from (see settings.ExtraSatellite).
        extra = [e.label for e in self.settings.passes_extra_satellites if e.label not in base]
        return base + extra

    def _tle_cached(self, label: str) -> bool:
        entry = self._catalog_entry(label)
        return entry is not None and self.tle_store.load(entry["norad"]) is not None

    def _catalog_downlink(self, norad: int) -> Downlink | None:
        """msdr's own SatNOGS catalog entries are the source of truth for a
        pass's downlink (see AppService._predict_passes) -- nextpass's own
        --radio output only fills in where this has nothing. Catalog station
        strings are "NORAD <norad> <description>" (see
        catalog/parsers/satnogs.py); searching for the NORAD id as a
        (phrase-matched) FTS token is precise enough that a coincidental
        substring match elsewhere isn't a real risk."""
        try:
            entries = self.catalog.search(str(norad), source="satnogs", limit=5)
        except Exception:
            return None
        for e in entries:
            if f"NORAD {norad} " in (e.station or "") or e.station == f"NORAD {norad}":
                return Downlink(frequency_hz=e.frequency_hz, mode=e.mode or "NFM", label=e.station)
        return None

    def _load_visible_ephemeris(self):
        """Read cached visibility data even with filtering off; never download here."""
        return load_cached_ephemeris(Path(self.settings.tle_cache_dir).expanduser())

    async def _predict_passes(
        self,
        satellites: list[str],
        *,
        days: int,
        min_elevation: float,
        horizon: float,
        top: int,
        refresh: bool = False,
    ) -> PassesResult:
        """Shared by `passes()` and LISTEN's `passes_compatible` preset: the
        internal skyfield predictor when TLEs are cached for `satellites` and
        a location is configured, else the nextpass provider. `refresh=True`
        refreshes both engines' caches regardless of which one ends up
        serving this call (POST /api/passes/refresh)."""
        if refresh:
            await self._refresh_tles()

        location = self.tracker.location
        cached = [s for s in satellites if self._tle_cached(s)] if location is not None else []

        nextpass_result: PassesResult | None = None
        if refresh or not cached:
            nextpass_result = await self.nextpass_provider.passes(
                satellites,
                days=days,
                min_elevation=min_elevation,
                horizon=horizon,
                top=top,
                location=location,
                refresh=refresh,
                extra_satellites=list(self._extra_catalog().values()),
            )

        if cached:
            missing = [s for s in satellites if s not in cached]
            ephemeris = self._load_visible_ephemeris()
            predicted = self.tracker.passes(
                cached,
                days=days,
                min_elevation=min_elevation,
                horizon=horizon,
                top=top,
                ephemeris=ephemeris,
                visible_only=self.settings.passes_visible_only,
            )
            self._fill_downlinks(predicted)
            errors = [f"no TLE for {', '.join(missing)} (refresh TLEs)"] if missing else []
            if self.settings.passes_visible_only and ephemeris is None:
                errors.append("visible-only: no cached ephemeris (refresh TLEs once online)")
            return PassesResult(
                passes=predicted,
                generated_at=now_iso(),
                source="internal",
                timezone=location.timezone,
                error="; ".join(errors) or None,
                engine="internal",
            )

        assert nextpass_result is not None
        self._fill_downlinks(nextpass_result.passes)
        return nextpass_result.model_copy(update={"engine": "nextpass"})

    def _fill_downlinks(self, passes: list) -> None:
        """Overrides each Pass.downlink (nextpass's own --radio fallback, or
        None from the internal predictor) with msdr's own SatNOGS catalog
        match, when there is one -- see _catalog_downlink."""
        for p in passes:
            downlink = self._catalog_downlink(p.norad)
            if downlink is not None:
                p.downlink = downlink

    async def passes(
        self,
        days: int | None = None,
        min_elevation: float | None = None,
        horizon: float | None = None,
        top: int | None = None,
        refresh: bool = False,
    ) -> PassesResult:
        if not self.settings.features.passes:
            raise HTTPException(404, "feature disabled: passes")
        settings = self.settings
        return await self._predict_passes(
            self._passes_satellites(),
            days=settings.passes_days if days is None else days,
            min_elevation=settings.passes_min_elevation if min_elevation is None else min_elevation,
            horizon=settings.passes_horizon if horizon is None else horizon,
            top=settings.passes_top if top is None else top,
            refresh=refresh,
        )

    async def passes_ics(self) -> str:
        """GET /api/passes.ics: the current predicted passes (same defaults
        as passes()) as a hand-rolled iCalendar, one VEVENT per pass."""
        if not self.settings.features.passes:
            raise HTTPException(404, "feature disabled: passes")
        result = await self.passes()
        return _passes_ics(result.passes)

    def _sky_satellite_labels(self) -> list[str]:
        # Satellites bound to any saved Signal, or the whole catalog when
        # none are pinned yet -- mirrors passes()'s own fallback, but ignores
        # passes_satellites since /api/sky is about what's on the air now,
        # not the operator's chosen PASSES subset.
        all_signals = self.signals.list(limit=_ALL_SIGNALS_LIMIT)
        pinned = sorted({s.satellite for s in all_signals if s.satellite})
        return pinned or [entry["label"] for entry in CATALOG]

    def sky_summary(self) -> dict:
        if not self.settings.features.sky:
            raise HTTPException(404, "feature disabled: sky")
        positions = []
        error = None
        for label in self._sky_satellite_labels():
            result = self.tracker.position(label)
            if result.position is not None:
                positions.append(result.position)
            elif error is None:
                error = result.error
        return {
            "positions": [p.model_dump(mode="json") for p in positions],
            "location": self.tracker.location.model_dump(mode="json")
            if self.tracker.location is not None
            else None,
            "error": error,
        }

    def sky_detail(
        self,
        label: str,
        hours: float = 24.0,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> SkyResult:
        if not self.settings.features.sky:
            raise HTTPException(404, "feature disabled: sky")
        if (start is None) != (end is None):
            raise HTTPException(422, "start and end must be supplied together")
        if start is not None and end is not None:
            if start.tzinfo is None or end.tzinfo is None:
                raise HTTPException(422, "pass times must include a timezone")
            if not 0 < (end - start).total_seconds() <= 86400:
                raise HTTPException(422, "pass interval must be positive and at most 24 hours")
        result = self.tracker.position(label)
        if result.error is not None:
            return result
        track = (
            self.tracker.track(label, start, end)
            if start is not None and end is not None
            else self.tracker.next_or_current_pass(label, hours=hours)
        )
        return SkyResult(position=result.position, track=track, tle_age_days=result.tle_age_days)

    async def _refresh_tles(self) -> dict:
        updated: list[str] = []
        errors: dict[str, str] = {}
        tle_cache_dir = Path(self.settings.tle_cache_dir).expanduser()
        for entry in [*CATALOG, *self._extra_catalog().values()]:
            label, norad = entry["label"], entry["norad"]
            try:
                omm = await fetch_omm(norad)
            except Exception as exc:  # network/HTTP/parse errors -- report, keep going
                errors[label] = str(exc)
                continue
            self.tle_store.save(norad, omm, tle_cache_dir)
            updated.append(label)
        if self.settings.passes_visible_only:
            # The only place de421.bsp is ever fetched -- passes()/_tick()
            # only read a cache that's already there (see
            # _load_visible_ephemeris), same network boundary as TLEs above.
            try:
                await fetch_ephemeris(tle_cache_dir)
            except Exception as exc:
                errors["ephemeris"] = str(exc)
        return {"updated": updated, "errors": errors}

    async def refresh_sky(self) -> dict:
        if not self.settings.features.sky:
            raise HTTPException(404, "feature disabled: sky")
        return await self._refresh_tles()

    async def start_tracking(self, signal_id: str, decode: bool = False) -> dict:
        self._require_band_scan_idle()
        if not self.settings.features.tracking:
            raise HTTPException(404, "feature disabled: tracking")
        signal = self.signals.get(signal_id)
        if signal is None:
            raise HTTPException(404, f"signal not found: {signal_id!r}")
        if signal.satellite is None:
            raise HTTPException(400, "signal has no satellite bound")
        if not self.adapter.connected or not self.confirmed.props.get("started", False):
            raise HTTPException(503, "receiver not connected or not started")
        if self._executor.running:
            # A restore in flight is already driving the VFO; letting the
            # Doppler loop start would have the two fight over it.
            raise RestoreRunning("restore running")
        f0_hz = signal.frequency_hz
        on_end = None
        plan = None
        if decode:
            entry = self._catalog_entry(signal.satellite)
            plan = decode_plan(entry["norad"]) if entry else None
            if plan is None:
                raise HTTPException(400, f"no known decodable downlink for {signal.satellite}")
            self._require_decoder()
            # Tags first so the very first frame is already grouped by pass;
            # decoder_start tunes to the table downlink (Doppler f0) and fails
            # loudly if the tune isn't confirmed.
            self.decoder.event_tags = {
                "satellite": plan.name,
                "norad": plan.norad,
                "pass_id": f"{plan.norad}-{int(time.time())}",
            }
            try:
                await self.decoder_start([plan.decoder], plan.frequency_hz)
            except Exception:
                self.decoder.event_tags = None
                raise
            f0_hz = int(self.confirmed.props.get("device_vfo_frequency") or plan.frequency_hz)
            on_end = self._end_pass_decode
        result = await self.doppler_loop.start(
            signal_id, signal.satellite, f0_hz, on_end=on_end
        )
        if plan is not None:
            result = {**result, "decode": {"decoder": plan.decoder, **self.decoder.event_tags}}
        await self._publish_owner()
        return result

    async def _end_pass_decode(self) -> None:
        """LOS / stop / retune: stop the pass decoder and drop the tags."""
        self.decoder.event_tags = None
        try:
            await self.decoder.stop_decoding("pass ended")
        except Exception:
            logging.getLogger(__name__).exception("stopping pass decoder failed")

    async def stop_tracking(self) -> None:
        await self.doppler_loop.stop()
        await self._publish_owner()
