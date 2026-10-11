"""LISTEN presets.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import HTTPException

from msdr.api.schemas import ListenItem, ListenTune
from msdr.api.services._common import _ALL_SIGNALS_LIMIT
from msdr.catalog.models import CatalogEntry, next_start, on_air, tune_recipe
from msdr.catalog.store import ON_AIR_PREFILTER_LIMIT
from msdr.library.frequency import format_mhz
from msdr.library.models import Signal, now_iso

# Catalog rows a LISTEN preset pulls before its Python-side filtering. The
# catalog can hold hundreds of thousands of rows; a preset shows ~30.
_LISTEN_CANDIDATE_LIMIT = ON_AIR_PREFILTER_LIMIT


# --- LISTEN presets -------------------------------------------------------

_LISTEN_STARTING_SOON_WINDOW_MIN = 15


# Mode/notes substrings (case-insensitive) that mark a catalog entry as
# decodable with an off-the-shelf tool (radiofax, beacon, time signal, etc).
_LISTEN_DECODABLE_TOKENS = ("fax", "beacon", "time", "navtex", "rtty", "dsc")


# NCDXF/IARU beacon network: 5 fixed frequencies, 18 callsigns rotating
# through a 3-minute (180s) cycle, 10s per callsign, one slot offset per
# successive frequency (see beacon_schedule).
_BEACON_FREQUENCIES_HZ = [14_100_000, 18_110_000, 21_150_000, 24_930_000, 28_200_000]


_BEACON_CALLSIGNS = [
    "4U1UN",
    "VE8AT",
    "W6WX",
    "KH6RS",
    "ZL6B",
    "VK6RBP",
    "JA2IGY",
    "RR9O",
    "VR2B",
    "4S7B",
    "ZS6DN",
    "5Z4B",
    "4X6TU",
    "OH2B",
    "CS3B",
    "LU4AA",
    "OA4B",
    "YV5B",
]


_BEACON_SLOT_S = 10


_BEACON_CYCLE_S = _BEACON_SLOT_S * len(_BEACON_CALLSIGNS)  # 180


# Narrow enough for CW; there's no catalog/signal row to derive a recipe from.
_BEACON_FILTER_BANDWIDTH_HZ = 500


def beacon_schedule(at: datetime) -> dict[int, str]:
    """Which NCDXF/IARU beacon callsign is transmitting on each of the 5
    beacon frequencies at `at`. A cycle is 3 minutes (18 callsigns x 10s),
    starting on 14.100 MHz at :00 seconds of every 3rd UTC minute; each
    successive frequency plays the same callsign 10s later than the one
    before it. Pure and UTC-only so it's directly testable against fixed
    instants -- the `beacons` LISTEN preset is the only caller."""
    at = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    at = at.astimezone(UTC)
    seconds_of_day = at.hour * 3600 + at.minute * 60 + at.second
    slot = (seconds_of_day % _BEACON_CYCLE_S) // _BEACON_SLOT_S
    n = len(_BEACON_CALLSIGNS)
    return {
        freq_hz: _BEACON_CALLSIGNS[(slot - f) % n]
        for f, freq_hz in enumerate(_BEACON_FREQUENCIES_HZ)
    }


def _catalog_listen_item(entry: CatalogEntry, *, score: float = 0.0) -> ListenItem:
    # Callers filter with _listen_tunable first; a null frequency_hz would
    # make tune_recipe raise and ListenItem.frequency_hz fail validation.
    window = f"{entry.start_utc or '0000'}-{entry.stop_utc or '2359'} UTC"
    title = f"{entry.station} ({entry.language})" if entry.language else entry.station
    frequency_hz, demodulator, bandwidth_hz = tune_recipe(entry)
    return ListenItem(
        kind="catalog",
        id=entry.id,
        title=title,
        subtitle=f"{format_mhz(entry.frequency_hz)} · {window}",
        frequency_hz=entry.frequency_hz,
        demodulator=entry.mode,
        when=None,
        score=score,
        tune=ListenTune(
            kind="catalog",
            id=entry.id,
            frequency_hz=frequency_hz,
            demodulator=demodulator,
            filter_bandwidth_hz=bandwidth_hz,
        ),
    )


def _listen_tunable(entry: CatalogEntry) -> bool:
    """LISTEN only offers rows the radio can be tuned to: reference records
    without a verified exact frequency (DME/TACAN, radar sites) are skipped."""
    return entry.frequency_hz is not None


def _signal_listen_item(signal: Signal, *, score: float = 0.0) -> ListenItem:
    subtitle = signal.station or signal.location or format_mhz(signal.frequency_hz)
    return ListenItem(
        kind="signal",
        id=signal.id,
        title=signal.name,
        subtitle=subtitle,
        frequency_hz=signal.frequency_hz,
        demodulator=signal.demodulator,
        when=signal.last_heard_at,
        score=score,
        tune=ListenTune(
            kind="signal",
            id=signal.id,
            frequency_hz=signal.frequency_hz,
            demodulator=signal.demodulator,
            filter_bandwidth_hz=signal.filter_bandwidth_hz,
        ),
    )


class ListenServiceMixin:
    # --- LISTEN presets ------------------------------------------------

    async def listen(self, preset: str, limit: int = 30) -> dict:
        handlers: dict[str, Callable] = {
            "on_air_my_languages": self._listen_on_air_my_languages,
            "starting_soon": self._listen_starting_soon,
            "unheard": self._listen_unheard,
            "heard_this_hour": self._listen_heard_this_hour,
            "decodable": self._listen_decodable,
            "beacons": self._listen_beacons,
            "passes_compatible": self._listen_passes_compatible,
        }
        handler = handlers.get(preset)
        if handler is None:
            raise HTTPException(422, f"unknown preset: {preset!r}")
        result = handler(limit)
        items = await result if asyncio.iscoroutine(result) else result
        return {
            "preset": preset,
            "items": [i.model_dump(mode="json") for i in items],
            "generated_at": now_iso(),
        }

    def _listen_on_air_my_languages(self, limit: int) -> list[ListenItem]:
        if not self.settings.features.catalog:
            return []
        now = datetime.now(UTC)
        languages = self.settings.catalog_languages or None
        # Language filter runs in SQL; only the bounded candidate slab is
        # turned into CatalogEntry objects.
        entries = self.catalog.search("", limit=_LISTEN_CANDIDATE_LIMIT, languages=languages)
        airing = [e for e in entries if _listen_tunable(e) and on_air(e, now)]
        vfo = self.confirmed.props.get("device_vfo_frequency")
        if vfo is not None:
            airing.sort(key=lambda e: abs(e.frequency_hz - vfo))
        return [_catalog_listen_item(e) for e in airing[:limit]]

    def _listen_starting_soon(self, limit: int) -> list[ListenItem]:
        if not self.settings.features.catalog:
            return []
        now = datetime.now(UTC)
        entries = self.catalog.search("", limit=_LISTEN_CANDIDATE_LIMIT)
        upcoming = []
        for e in entries:
            if not _listen_tunable(e) or on_air(e, now):
                continue
            # next_start honors days/date (weekday mask, one-off dates, and
            # midnight rollover) instead of a naive HH:MM-modulo-1440 delta.
            start = next_start(e, now)
            if start is None:
                continue
            delta_min = (start - now).total_seconds() / 60
            if 0 < delta_min <= _LISTEN_STARTING_SOON_WINDOW_MIN:
                upcoming.append((delta_min, start, e))
        upcoming.sort(key=lambda triple: triple[0])
        items = []
        for _delta, start, e in upcoming[:limit]:
            when = start.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            items.append(_catalog_listen_item(e).model_copy(update={"when": when}))
        return items

    def _listen_unheard(self, limit: int) -> list[ListenItem]:
        if not self.settings.features.catalog:
            return []
        now = datetime.now(UTC)
        heard = self.journal_repo.heard_station_names()
        entries = self.catalog.search("", limit=_LISTEN_CANDIDATE_LIMIT)
        airing = [
            e
            for e in entries
            if _listen_tunable(e) and on_air(e, now) and e.station.lower() not in heard
        ]
        return [_catalog_listen_item(e) for e in airing[:limit]]

    def _listen_heard_this_hour(self, limit: int) -> list[ListenItem]:
        now = datetime.now(UTC)
        counts = self.journal_repo.hour_of_day_counts(now.hour, exclude_day=now.date().isoformat())
        items: list[ListenItem] = []
        for signal_id, count in counts.items():
            signal = self.signals.get(signal_id)
            if signal is None:
                continue
            items.append(_signal_listen_item(signal, score=float(count)))
            if len(items) >= limit:
                break
        return items

    def _listen_decodable(self, limit: int) -> list[ListenItem]:
        if not self.settings.features.catalog:
            return []
        now = datetime.now(UTC)
        entries = self.catalog.search("", limit=_LISTEN_CANDIDATE_LIMIT)
        matches = []
        for e in entries:
            if not _listen_tunable(e) or not on_air(e, now):
                continue
            haystack = f"{e.mode or ''} {e.notes or ''}".lower()
            if any(token in haystack for token in _LISTEN_DECODABLE_TOKENS):
                matches.append(e)
        return [_catalog_listen_item(e) for e in matches[:limit]]

    def _listen_beacons(self, limit: int) -> list[ListenItem]:
        schedule = beacon_schedule(datetime.now(UTC))
        items = [
            ListenItem(
                kind="signal",
                id=f"beacon:{freq_hz}",
                title=f"{format_mhz(freq_hz)} beacon",
                subtitle=callsign,
                frequency_hz=freq_hz,
                demodulator="CW",
                when=None,
                score=0.0,
                tune=ListenTune(
                    kind="recipe",
                    id=None,
                    frequency_hz=freq_hz,
                    demodulator="CW",
                    filter_bandwidth_hz=_BEACON_FILTER_BANDWIDTH_HZ,
                ),
            )
            for freq_hz, callsign in schedule.items()
        ]
        return items[:limit]

    async def _listen_passes_compatible(self, limit: int) -> list[ListenItem]:
        if not self.settings.features.passes:
            return []
        all_signals = self.signals.list(limit=_ALL_SIGNALS_LIMIT)
        # One signal per satellite label ("a memory") -- last one wins if
        # more than one memory is bound to the same satellite.
        bound = {s.satellite: s for s in all_signals if s.satellite}
        if not bound:
            return []
        settings = self.settings
        result = await self._predict_passes(
            sorted(bound),
            days=settings.passes_days,
            min_elevation=settings.passes_min_elevation,
            horizon=settings.passes_horizon,
            top=limit,
        )
        items: list[ListenItem] = []
        for p in result.passes[:limit]:
            signal = bound.get(p.satellite)
            if signal is None:
                continue
            items.append(
                ListenItem(
                    kind="pass",
                    id=f"{p.satellite}:{p.rise}",
                    title=p.satellite,
                    subtitle=f"{signal.name} · {format_mhz(signal.frequency_hz)}",
                    frequency_hz=signal.frequency_hz,
                    demodulator=signal.demodulator,
                    when=p.rise,
                    score=p.max_elevation_deg,
                    tune=ListenTune(
                        kind="signal",
                        id=signal.id,
                        frequency_hz=signal.frequency_hz,
                        demodulator=signal.demodulator,
                        filter_bandwidth_hz=signal.filter_bandwidth_hz,
                    ),
                )
            )
        return items
