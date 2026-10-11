"""Catalog entry/source models and the small bits of domain logic
(on-air check, default tune recipe) shared by the store and the parsers."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field, model_validator

from msdr.ids import uuid7
from msdr.library.models import now_iso

# Namespace for ids derived from (source, station_id, component, place) --
# see stable_entry_id(). Fixed so the same reference record keeps its id
# across refreshes and across installs.
_STABLE_ID_NAMESPACE = uuid.UUID("8f2a4c6e-1d3b-4a5f-9c7e-2b6d8e0f1a3c")

# on_air_state() vocabulary (see docs/reference-catalogs.md, "On-air states").
ON_AIR_STATES = ("continuous", "scheduled_on", "scheduled_off", "published_active", "unknown")


def _place_discriminator(
    location: str | None, latitude: float | None, longitude: float | None
) -> str:
    """The location part of a stable id. Short source identifiers repeat
    nationally (FAA NDB "AA" is both Fargo ND and Thomson GA), so the place
    has to take part in the id or one row silently replaces the other. The
    location text ("CITY, ST") is preferred because it is the stabler
    discriminator across refreshes; coordinates rounded to 4 decimals (about
    10 m) are the fallback, and records with neither get no discriminator."""
    if location and location.strip():
        return location.strip()
    if latitude is not None and longitude is not None:
        return f"{latitude:.4f},{longitude:.4f}"
    return ""


def stable_entry_id(
    source: str,
    station_id: str,
    component: str | None,
    location: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """Deterministic id for a reference record, so a refresh upserts the same
    row instead of churning ids (user annotations stay attached). Derived
    from (source, station_id, component) plus the place discriminator
    (docs/reference-catalogs.md, "Data model")."""
    place = _place_discriminator(location, latitude, longitude)
    name = f"{source}\x1f{station_id}\x1f{component or ''}\x1f{place}"
    return str(uuid.uuid5(_STABLE_ID_NAMESPACE, name))


class CatalogEntry(BaseModel):
    """Mirrors the `catalog_entries` table (see migrations/004_catalog.sql and
    019_catalog_reference.sql)."""

    id: str = Field(default_factory=uuid7)
    source: str
    # None for reference records without a tunable fixed frequency (DME/TACAN,
    # radar sites); every schedule row has one.
    frequency_hz: int | None = None
    station: str
    mode: str | None = None
    bandwidth_hz: int | None = None
    # HHMM strings (UTC), or both None for "always". A window with
    # start > stop wraps midnight (e.g. 2200-0600).
    start_utc: str | None = None
    stop_utc: str | None = None
    # 7-char mask 'MTWTFSS' (Mon..Sun), '.' for off; None means daily.
    # Ignored when `date` is set (a one-off event on a specific calendar day).
    days: str | None = None
    # YYYY-MM-DD (UTC): a specific one-off calendar date rather than a
    # `days`-recurring schedule (e.g. a Priyom event with no RRULE, or one
    # with an unsupported/expired RRULE). None means `days` (or daily)
    # applies as usual.
    date: str | None = None
    language: str | None = None
    target: str | None = None
    country: str | None = None
    location: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    power_kw: float | None = None
    notes: str | None = None
    season: str | None = None
    imported_at: str = Field(default_factory=now_iso)
    # Reference-catalog fields (docs/reference-catalogs.md, "Data model").
    # Schedule rows leave all of them at their defaults.
    ref_type: str = "schedule"  # schedule | station | navaid | radar_site
    network: str | None = None
    station_id: str | None = None
    component: str | None = None
    operator: str | None = None
    freq_lo_hz: int | None = None
    freq_hi_hz: int | None = None
    current_freq_hz: int | None = None
    current_freq_at: str | None = None
    # operational | operational_restricted | experimental | data_recent |
    # unknown | shutdown; the source's own wording goes in `notes`.
    status: str | None = None
    status_at: str | None = None
    source_url: str | None = None
    edition: str | None = None
    decoder: str | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_stable_id(cls, data):
        # Reference records (anything with a station_id) get a deterministic
        # id unless the caller supplied one; schedule rows keep uuid7.
        if isinstance(data, dict) and not data.get("id") and data.get("station_id"):
            data = dict(data)
            data["id"] = stable_entry_id(
                data["source"],
                data["station_id"],
                data.get("component"),
                data.get("location"),
                data.get("latitude"),
                data.get("longitude"),
            )
        return data

    @model_validator(mode="after")
    def coordinates_are_paired(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class Source(BaseModel):
    """Mirrors the `catalog_sources` table."""

    source: str
    label: str | None = None
    url: str | None = None
    season: str | None = None
    fetched_at: str | None = None
    entry_count: int = 0


def _minutes(hhmm: str) -> int | None:
    if len(hhmm) != 4 or not hhmm.isdigit():
        return None
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


def _parse_date(date_str: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(date_str)
    except ValueError:
        return None


def _schedule_on(entry: CatalogEntry, at: dt.datetime) -> bool:
    """The schedule-window test: is a broadcast schedule row active at `at`
    (UTC)? A NULL window means "always" per the schema."""
    if at.tzinfo is not None:
        at = at.astimezone(dt.UTC)
    start = _minutes(entry.start_utc) if entry.start_utc else None
    stop = _minutes(entry.stop_utc) if entry.stop_utc else None
    wraps = start is not None and stop is not None and start > stop
    if entry.date:
        event_date = _parse_date(entry.date)
        if event_date is None:
            return True  # malformed date: don't let it hide an entry
        if wraps:
            # The window straddles midnight, so the event actually runs
            # from `date` (from `start`) into `date` + 1 day (until `stop`).
            if at.date() == event_date:
                return at.hour * 60 + at.minute >= start
            if at.date() == event_date + dt.timedelta(days=1):
                return at.hour * 60 + at.minute < stop
            return False
        if at.date() != event_date:
            return False
        # Same-day window (or no window at all) -- fall through below.
    elif entry.days and len(entry.days) == 7:
        weekday = at.weekday()
        if wraps and at.hour * 60 + at.minute < stop:
            # After midnight we're in the tail of the window that started
            # yesterday, so yesterday's mask decides.
            weekday = (weekday - 1) % 7
        if entry.days[weekday] == ".":
            return False
    if start is None or stop is None:
        return True  # NULL window = "always" per the schema
    now = at.hour * 60 + at.minute
    if start == stop:
        return True  # degenerate 24h window (e.g. "0000-0000")
    if start < stop:
        return start <= now < stop
    return now >= start or now < stop  # wraps midnight


def on_air_state(entry: CatalogEntry, at: dt.datetime) -> str:
    """One of ON_AIR_STATES. Schedule rows use the window logic; reference
    records answer from their published status instead, and anything whose
    activity is not published (radar sites, missing or stale status,
    shutdown) is `unknown` rather than a guessed "on air"."""
    if entry.ref_type == "station":
        return "continuous" if entry.status in ("operational", "experimental") else "unknown"
    if entry.ref_type == "navaid":
        if entry.status in ("operational", "operational_restricted"):
            return "published_active"
        return "unknown"
    if entry.ref_type == "radar_site":
        return "unknown"
    return "scheduled_on" if _schedule_on(entry, at) else "scheduled_off"


def on_air(entry: CatalogEntry, at: dt.datetime) -> bool:
    """Is `entry` transmitting at `at`? Schedules are given in UTC, so a
    naive `at` is assumed to already be UTC; an aware one is converted.
    Reference records are on air only when their published state says so."""
    return on_air_state(entry, at) in ("continuous", "scheduled_on", "published_active")


def next_start(entry: CatalogEntry, at: dt.datetime) -> dt.datetime | None:
    """The next UTC instant `entry` starts transmitting, strictly after
    `at` -- used by LISTEN's "starting soon" preset. Honors `days` (or,
    for a one-off `date` entry, that specific calendar day) and rolls a
    recurring schedule over midnight/week boundaries correctly. None if
    the entry has no start time, or its one-off `date` has already passed."""
    if at.tzinfo is not None:
        at = at.astimezone(dt.UTC)
    else:
        at = at.replace(tzinfo=dt.UTC)
    start = _minutes(entry.start_utc) if entry.start_utc else None
    if start is None:
        return None
    hour, minute = divmod(start, 60)
    if entry.date:
        event_date = _parse_date(entry.date)
        if event_date is None:
            return None
        candidate = dt.datetime(
            event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=dt.UTC
        )
        return candidate if candidate > at else None
    # Recurring: walk forward at most a full week looking for a day the mask
    # allows, so e.g. a Monday-only entry checked from Wednesday resolves to
    # next Monday rather than "not upcoming" from a same-day-only check.
    for offset in range(8):
        day = at.date() + dt.timedelta(days=offset)
        if entry.days and len(entry.days) == 7 and entry.days[day.weekday()] == ".":
            continue
        candidate = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.UTC)
        if candidate > at:
            return candidate
    return None  # every day masked off


def tune_recipe(entry: CatalogEntry) -> tuple[int, str, int]:
    """(frequency_hz, demodulator, bandwidth_hz) to hand the radio. Broadcast
    schedules (EiBi/Aoki/HFCC) rarely encode mode/bandwidth explicitly, so
    default to AM / 9000 Hz -- the near-universal shortwave broadcast mode."""
    if entry.frequency_hz is None:
        raise ValueError(f"catalog entry {entry.id} has no tunable frequency")
    demodulator = (entry.mode or "AM").upper()
    bandwidth_hz = entry.bandwidth_hz or 9000
    return entry.frequency_hz, demodulator, bandwidth_hz
