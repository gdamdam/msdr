import datetime as dt

from msdr.catalog.models import on_air
from msdr.catalog.parsers.priyom import default_url, parse_priyom

PRIYOM_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:e11-001@priyom.org
DTSTART:20240101T090000Z
DTEND:20240101T093000Z
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
SUMMARY:E11 9130 kHz USB
DESCRIPTION:Numbers station transmission\\nRepeat of previous message
END:VEVENT
BEGIN:VEVENT
UID:s06-002@priyom.org
DTSTART;TZID=Europe/Moscow:20240103T140000
DTEND;TZID=Europe/Moscow:20240103T143000
SUMMARY:S06 5810 kHz AM
DESCRIPTION:Slavic numbers station\\nrepeat broadcast
END:VEVENT
BEGIN:VEVENT
UID:holiday-003@priyom.org
DTSTART:20240107T000000Z
DTEND:20240108T000000Z
SUMMARY:Holiday - no scheduled transmissions
END:VEVENT
END:VCALENDAR
"""


def test_default_url_is_official_json_api():
    assert default_url == "https://calendar.priyom.org/events"


def test_parse_official_json_api_occurrences_and_multiple_frequencies():
    payload = '''{"items":[
      {"summary":"E11 9130, 10200 kHz USB/AM Repeated message",
       "start":{"dateTime":"2026-09-29T09:30:00Z"}},
      {"summary":"S06 Search AM",
       "start":{"dateTime":"2026-09-29T10:00:00Z"}},
      {"summary":"holiday", "start":{"dateTime":"2026-09-29T00:00:00Z"}},
      {"summary":"M12 5473 kHz CW",
       "start":{"dateTime":"2026-09-29T15:00:00+03:00"}}
    ]}'''
    entries = parse_priyom(payload)
    assert [(e.station, e.frequency_hz) for e in entries] == [
        ("E11", 9_130_000), ("E11", 10_200_000), ("M12", 5_473_000)
    ]
    e11 = entries[0]
    assert e11.mode == "USB"
    assert e11.start_utc == "0930"
    assert e11.date == "2026-09-29"
    assert "Repeated message" in e11.notes
    assert "listed mode USB/AM" in e11.notes
    assert entries[2].start_utc == "1200"  # offset normalized to UTC


def test_parse_priyom_skips_events_without_frequency():
    entries = parse_priyom(PRIYOM_ICS)
    assert len(entries) == 2  # the holiday event has no "<digits> kHz" and is skipped


def test_parse_priyom_weekly_rrule_event():
    entries = parse_priyom(PRIYOM_ICS)
    e11 = next(e for e in entries if e.station == "E11")
    assert e11.source == "priyom"
    assert e11.frequency_hz == 9_130_000
    assert e11.mode == "USB"
    assert e11.bandwidth_hz == 3000
    assert e11.start_utc == "0900"
    assert e11.stop_utc == "0930"
    assert e11.days == "M.W.F.."  # Mon/Wed/Fri
    assert "numbers station" in e11.notes
    assert "Numbers station transmission" in e11.notes


def test_parse_priyom_tzid_converted_to_utc_no_rrule():
    entries = parse_priyom(PRIYOM_ICS)
    s06 = next(e for e in entries if e.station == "S06")
    assert s06.frequency_hz == 5_810_000
    assert s06.mode == "AM"
    # Europe/Moscow (UTC+3) 14:00-14:30 -> 11:00-11:30 UTC
    assert s06.start_utc == "1100"
    assert s06.stop_utc == "1130"
    assert s06.days is None  # no RRULE -> not treated as recurring


def test_on_air_priyom_weekly_event_respects_mask_and_window():
    entries = parse_priyom(PRIYOM_ICS)
    e11 = next(e for e in entries if e.station == "E11")
    monday_in_window = dt.datetime(2024, 1, 1, 9, 15)  # Monday, inside window
    monday_before_window = dt.datetime(2024, 1, 1, 8, 0)  # Monday, before window
    tuesday_in_window_time = dt.datetime(2024, 1, 2, 9, 15)  # Tuesday, off day

    assert on_air(e11, monday_in_window)
    assert not on_air(e11, monday_before_window)
    assert not on_air(e11, tuesday_in_window_time)


def test_parse_priyom_skips_event_with_impossible_dtstart():
    bad = PRIYOM_ICS.replace("DTSTART:20240101T090000Z", "DTSTART:20241301T250000Z")
    entries = parse_priyom(bad)
    assert [e.station for e in entries] == ["S06"]  # E11 skipped, import not aborted
