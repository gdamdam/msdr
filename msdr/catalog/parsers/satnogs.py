"""SatNOGS DB transmitters API (https://db.satnogs.org/api/transmitters/):
one JSON object per known satellite transmitter/transceiver/transponder,
e.g. {"description": ..., "alive": true, "status": "active",
"type": "Transmitter", "mode": "FM", "baud": 9600, "downlink_low": 145900000,
"downlink_high": null, "norad_cat_id": 25544, "sat_id": ..., "uuid": ...}.

One CatalogEntry per transmitter that is `status == "active"`, `alive`, and
has a `downlink_low` (some entries carry no frequency at all). Frequency is
the downlink_low/downlink_high midpoint when both are given, else just
downlink_low. SatNOGS' own `mode` field (FM, AFSK, BPSK, GMSK, LoRa, ...)
doesn't map cleanly onto the handful of demodulators our tune_recipe knows
about, so every mode -- FM included -- becomes "NFM" here, with the original
SatNOGS mode kept in notes so nothing is lost. Pass windows for these come
from orbit tracking, not the catalog, so start/stop stay the full day."""

from __future__ import annotations

import json

from msdr.catalog.models import CatalogEntry
from msdr.library.models import now_iso

default_url = "https://db.satnogs.org/api/transmitters/?format=json&alive=true"

_MIN_BANDWIDTH_HZ = 12500


def url_for_norad(norad: int | str) -> str:
    return f"https://db.satnogs.org/api/transmitters/?format=json&satellite__norad_cat_id={norad}"


def parse_satnogs(text: str) -> list[CatalogEntry]:
    imported_at = now_iso()
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []

    entries: list[CatalogEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "active" or not item.get("alive"):
            continue
        downlink_low = item.get("downlink_low")
        if not downlink_low:
            continue
        downlink_high = item.get("downlink_high")
        if downlink_high:
            frequency_hz = round((downlink_low + downlink_high) / 2)
            bandwidth_hz = max(_MIN_BANDWIDTH_HZ, downlink_high - downlink_low)
        else:
            frequency_hz = downlink_low
            bandwidth_hz = _MIN_BANDWIDTH_HZ

        norad = item.get("norad_cat_id")
        description = (item.get("description") or "").strip() or "Unknown"
        station = f"NORAD {norad} {description}" if norad is not None else description

        sat_mode = (item.get("mode") or "").strip() or "unknown"
        xmtr_type = (item.get("type") or "Transmitter").strip()
        notes = f"satnogs {xmtr_type} {sat_mode}"
        baud = item.get("baud")
        if baud:
            notes += f" {baud} baud"

        entries.append(
            CatalogEntry(
                source="satnogs",
                frequency_hz=frequency_hz,
                station=station,
                mode="NFM",
                bandwidth_hz=bandwidth_hz,
                start_utc="0000",
                stop_utc="2400",
                days=None,
                country=None,
                location=None,
                notes=notes,
                imported_at=imported_at,
            )
        )
    return entries
