"""Priyom numbers-station schedule adapter.

The official calendar viewer uses the JSON endpoint at
``https://calendar.priyom.org/events`` (``items`` with ``summary`` and
``start.dateTime``). The historical iCalendar parser remains supported for
user supplied feeds.

For iCalendar VEVENTs:

- DTSTART/DTEND give the UTC transmission window (HHMM). A DTSTART/DTEND
  with a `TZID` param is converted to UTC via `zoneinfo` (stdlib); a bare
  UTC value (trailing 'Z') is used as-is.
- RRULE `FREQ=WEEKLY;BYDAY=...` gives a Monday-first days mask; `FREQ=DAILY`
  leaves `days` as None ("every day"); no RRULE at all means a one-off event,
  recorded via `date` (DTSTART's own UTC calendar day) instead. An RRULE with
  UNTIL already in the past, a COUNT (which this parser can't safely expand),
  or any other FREQ (MONTHLY/YEARLY/...) is dropped rather than misreported.
- SUMMARY like "E11 9130 kHz USB" gives the station callsign (first token),
  the frequency (first "<digits> kHz" match in SUMMARY, else DESCRIPTION)
  and the mode (first USB/LSB/AM/CW/FM token, default USB).
- DESCRIPTION's first line is folded into notes.

Events with no recognisable frequency are skipped -- Priyom's feed also
carries non-frequency housekeeping/holiday entries. The official JSON API is
the default source; a configured custom URL continues to support legacy ICS
feeds."""

from __future__ import annotations

import re
from datetime import UTC, datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib since Python 3.9
    ZoneInfo = None  # type: ignore[assignment,misc]

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

default_url = "https://calendar.priyom.org/events"

_FREQ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k[Hh][Zz]")
_MODE_RE = re.compile(r"\b(USB|LSB|AM|CW|FM)\b", re.IGNORECASE)
_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$")
_MASK_LETTERS = "MTWTFSS"
_BYDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text: str) -> list[str]:
    """RFC 5545 line folding: a line starting with a space/tab continues
    the previous logical line."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def _to_utc(value: str, params: dict[str, str]) -> datetime | None:
    m = _DT_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, z = m.groups()
    naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    tzid = params.get("TZID")
    if z == "Z" or not tzid:
        return naive.replace(tzinfo=UTC)
    if ZoneInfo is not None:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid)).astimezone(UTC)
        except Exception:
            pass
    return naive.replace(tzinfo=UTC)  # best effort: unknown tz, assume UTC


def _parse_rrule(rrule: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)


def _weekly_mask(parts: dict[str, str], dtstart_weekday: int | None) -> str | None:
    """BYDAY (or, absent that, DTSTART's own weekday) as a Mon..Sun mask.
    Caller has already established FREQ=WEEKLY."""
    byday = parts.get("BYDAY")
    if byday:
        indices = {_BYDAY[d] for d in byday.split(",") if d in _BYDAY}
    elif dtstart_weekday is not None:
        indices = {dtstart_weekday}
    else:
        return None
    if not indices:
        return None
    mask = ["."] * 7
    for idx in indices:
        mask[idx] = _MASK_LETTERS[idx]
    return "".join(mask)


def _build_entry(block: dict[str, object], imported_at: str, now: datetime) -> CatalogEntry | None:
    summary = str(block.get("summary", "")).strip()
    description = str(block.get("description", "")).strip()

    freq_match = _FREQ_RE.search(summary) or _FREQ_RE.search(description)
    if not freq_match:
        return None  # not a transmission event (e.g. holiday/housekeeping)
    frequency_hz = round(float(freq_match.group(1)) * 1000)

    station_match = re.match(r"^(\S+)", summary)
    station = station_match.group(1) if station_match else "Unknown"

    mode_match = _MODE_RE.search(summary) or _MODE_RE.search(description)
    mode = mode_match.group(1).upper() if mode_match else "USB"

    start_utc = stop_utc = None
    days = None
    event_date = None
    dtstart_dt = None
    dtstart = block.get("dtstart")
    if dtstart:
        value, params = dtstart  # type: ignore[misc]
        dtstart_dt = _to_utc(value, params)
        if dtstart_dt is not None:
            start_utc = f"{dtstart_dt.hour:02d}{dtstart_dt.minute:02d}"

    rrule = block.get("rrule")
    if rrule:
        parts = _parse_rrule(str(rrule))
        until = parts.get("UNTIL")
        if until:
            until_dt = _to_utc(until, {})
            if until_dt is not None and until_dt < now:
                return None  # recurrence already ended before this import
        if "COUNT" in parts:
            # COUNT bounds the recurrence to N occurrences; telling whether
            # that count is already exhausted needs real RRULE expansion,
            # which this hand-rolled parser doesn't do. Drop rather than
            # risk showing a long-finished one-off/limited series as live.
            return None
        freq = parts.get("FREQ", "").upper()
        if freq == "WEEKLY":
            days = _weekly_mask(parts, dtstart_dt.weekday() if dtstart_dt is not None else None)
        elif freq == "DAILY":
            days = None  # recurs every day
        else:
            return None  # unsupported FREQ (MONTHLY/YEARLY/...): don't misreport
    elif dtstart_dt is not None:
        event_date = dtstart_dt.date().isoformat()  # one-off: a single calendar day

    dtend = block.get("dtend")
    if dtend:
        value, params = dtend  # type: ignore[misc]
        dt = _to_utc(value, params)
        if dt is not None:
            stop_utc = f"{dt.hour:02d}{dt.minute:02d}"

    notes_parts = ["numbers station"]
    if description:
        notes_parts.append(description.splitlines()[0])
    notes = "; ".join(notes_parts)

    return CatalogEntry(
        source="priyom",
        frequency_hz=frequency_hz,
        station=station,
        mode=mode,
        bandwidth_hz=3000,
        start_utc=start_utc,
        stop_utc=stop_utc,
        days=days,
        date=event_date,
        notes=notes,
        imported_at=imported_at,
    )


def parse_priyom(text: str) -> list[CatalogEntry]:
    """Parse either official calendar JSON or a legacy iCalendar feed."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return _parse_priyom_json(text)
    return _parse_priyom_ics(text)


def _parse_priyom_json(text: str) -> list[CatalogEntry]:
    import json

    payload = json.loads(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("Priyom JSON must contain an items array")
    imported_at = now_iso()
    entries: list[CatalogEntry] = []
    # Match the official viewer's event summary convention.
    summary_re = re.compile(r"^(\w+)\s+(?:(.*?)\s*kHz\s+([A-Z/]+)|Search)(?:\s+(.*))?$", re.I)
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        match = summary_re.match(summary)
        if not match or not match.group(2):
            continue  # search and housekeeping entries have no tuneable frequency
        station, frequency_list, mode_text, remarks = match.groups()
        mode_match = _MODE_RE.search(mode_text or "")
        mode = mode_match.group(1).upper() if mode_match else "USB"
        start_value = (
            (item.get("start") or {}).get("dateTime")
            if isinstance(item.get("start"), dict) else None
        )
        if not start_value:
            continue
        try:
            start_dt = datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            start_dt = start_dt.astimezone(UTC)
        except (TypeError, ValueError):
            continue
        frequencies = re.findall(r"\d+(?:\.\d+)?", frequency_list)
        for frequency in frequencies:
            hz = round(float(frequency) * 1000)
            entries.append(CatalogEntry(
                source="priyom", frequency_hz=hz, station=station,
                mode=mode.upper(), bandwidth_hz=3000,
                start_utc=f"{start_dt.hour:02d}{start_dt.minute:02d}",
                date=start_dt.date().isoformat(),
                notes="; ".join(part for part in (
                    "numbers station",
                    (
                        f"listed mode {mode_text.upper()}"
                        if mode_text and mode_text.upper() != mode else ""
                    ),
                    remarks.strip() if remarks else "",
                ) if part),
                imported_at=imported_at,
            ))
    return entries


def _parse_priyom_ics(text: str) -> list[CatalogEntry]:
    imported_at = now_iso()
    now = datetime.now(UTC)  # for dropping RRULEs whose UNTIL has already passed
    entries: list[CatalogEntry] = []
    block: dict[str, object] | None = None

    for raw in _unfold(text):
        line = raw.strip("\r")
        if line == "BEGIN:VEVENT":
            block = {}
            continue
        if line == "END:VEVENT":
            if block is not None:
                try:
                    entry = _build_entry(block, imported_at, now)
                except ValueError:
                    entry = None  # impossible DTSTART (month 13, hour 25): skip the event
                if entry is not None:
                    entries.append(entry)
            block = None
            continue
        if block is None:
            continue
        prop = _parse_prop(line)
        if not prop:
            continue
        name, params, value = prop
        if name == "DTSTART":
            block["dtstart"] = (value, params)
        elif name == "DTEND":
            block["dtend"] = (value, params)
        elif name == "RRULE":
            block["rrule"] = value
        elif name == "SUMMARY":
            block["summary"] = value
        elif name == "DESCRIPTION":
            block["description"] = value.replace("\\n", "\n")

    return entries
