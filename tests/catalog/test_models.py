import datetime as dt

from msdr.catalog.models import CatalogEntry, next_start, on_air, tune_recipe
from msdr.catalog.parsers.eibi import season_code


def _entry(**overrides) -> CatalogEntry:
    base = dict(source="eibi", frequency_hz=6070_000, station="Test")
    base.update(overrides)
    return CatalogEntry(**base)


def test_on_air_always_when_no_window():
    e = _entry()  # start_utc/stop_utc both None
    assert on_air(e, dt.datetime(2026, 9, 22, 3, 0))


def test_on_air_plain_window_inside_and_outside():
    e = _entry(start_utc="1000", stop_utc="1400")
    assert on_air(e, dt.datetime(2026, 9, 22, 12, 0))
    assert not on_air(e, dt.datetime(2026, 9, 22, 15, 0))


def test_on_air_midnight_wrap():
    e = _entry(start_utc="2200", stop_utc="0600")
    assert on_air(e, dt.datetime(2026, 9, 22, 23, 0))  # after start, before midnight
    assert on_air(e, dt.datetime(2026, 9, 22, 5, 0))  # after midnight, before stop
    assert not on_air(e, dt.datetime(2026, 9, 22, 12, 0))  # squarely outside


def test_on_air_excluded_weekday():
    # 'MTWTFSS' mask with Sat/Sun off (Mon-Fri only)
    e = _entry(start_utc="0000", stop_utc="2359", days="MTWTF..")
    monday = dt.datetime(2026, 9, 21, 12, 0)  # Mon
    saturday = dt.datetime(2026, 9, 26, 12, 0)  # Sat
    assert on_air(e, monday)
    assert not on_air(e, saturday)


def test_season_code_boundaries():
    assert season_code(dt.date(2026, 3, 29)) == "A26"  # last Sunday of March 2026
    assert season_code(dt.date(2026, 10, 25)) == "B26"  # last Sunday of October 2026
    assert season_code(dt.date(2026, 3, 28)) == "B25"  # day before -> still last season


def test_tune_recipe_defaults_to_am_9000():
    e = _entry(mode=None, bandwidth_hz=None)
    assert tune_recipe(e) == (6070_000, "AM", 9000)


def test_tune_recipe_respects_explicit_mode():
    e = _entry(mode="usb", bandwidth_hz=2700, frequency_hz=14_230_000)
    assert tune_recipe(e) == (14_230_000, "USB", 2700)


def test_on_air_one_off_date_not_active_on_other_days():
    # 2026-09-21 is a Monday; a one-off event on that date shouldn't recur.
    e = _entry(start_utc="1000", stop_utc="1400", date="2026-09-21")
    assert on_air(e, dt.datetime(2026, 9, 21, 12, 0))
    assert not on_air(e, dt.datetime(2026, 9, 23, 12, 0))  # Wednesday, same time-of-day


def test_next_start_weekly_monday_only_not_upcoming_on_wednesday():
    e = _entry(start_utc="0900", days="M......")
    at = dt.datetime(2026, 9, 23, 10, 0)  # Wednesday, after Monday's slot already passed
    start = next_start(e, at)
    assert start == dt.datetime(2026, 9, 28, 9, 0, tzinfo=dt.UTC)  # next Monday, not "soon"


def test_next_start_rolls_over_midnight_into_the_correct_weekday():
    # Tuesday-only, 00:30 start -- checked from just before midnight Monday.
    e = _entry(start_utc="0030", days=".T.....")
    at = dt.datetime(2026, 9, 21, 23, 50, tzinfo=dt.UTC)  # Monday
    start = next_start(e, at)
    assert start == dt.datetime(2026, 9, 22, 0, 30, tzinfo=dt.UTC)  # Tuesday


def test_next_start_one_off_date_returns_none_once_passed():
    e = _entry(start_utc="0900", date="2026-09-21")
    assert next_start(e, dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.UTC)) is not None
    assert next_start(e, dt.datetime(2026, 9, 23, 0, 0, tzinfo=dt.UTC)) is None


def test_on_air_overnight_window_uses_previous_day_mask_after_midnight():
    # Mon-only 2200-0600: Tue 01:00 is still Monday's broadcast; Mon 01:00 is Sunday's.
    e = _entry(start_utc="2200", stop_utc="0600", days="M......")
    assert on_air(e, dt.datetime(2026, 9, 21, 23, 0))  # Mon 23:00
    assert on_air(e, dt.datetime(2026, 9, 22, 1, 0))  # Tue 01:00
    assert not on_air(e, dt.datetime(2026, 9, 21, 1, 0))  # Mon 01:00
    assert not on_air(e, dt.datetime(2026, 9, 22, 23, 0))  # Tue 23:00
