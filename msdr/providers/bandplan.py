"""Offline US (ITU Region 2) band-plan identify provider.

Loads a static JSON table once via importlib.resources and scores it against
a query per the rules agreed in .superpowers/sdd/identify/contract.md. No
network/web imports -- this has to work with SDRconnect unreachable.
"""

import json
from importlib import resources
from typing import Any

from msdr.library.frequency import format_mhz
from msdr.providers.base import Candidate, IdentifyQuery

_CHANNEL_MIN_TOLERANCE_HZ = 5_000
_DIGITAL_HINT_BW_HZ = (12_500, 25_000)
_DIGITAL_HINT_CONFIDENCE = 0.2
_MAX_CANDIDATES = 5


def _load_entries(region: str) -> list[dict[str, Any]]:
    # Only "US" has data; any other region is a valid, empty provider rather
    # than an error (F1: unknown region must not crash the identify route).
    if region != "US":
        return []
    text = resources.files("msdr.providers.data").joinpath("bandplan_us.json").read_text()
    return json.loads(text)


def _reason(q: IdentifyQuery, name: str) -> str:
    parts = [format_mhz(q.frequency_hz)]
    if q.demodulator:
        parts.append(q.demodulator)
    if q.filter_bandwidth_hz:
        parts.append(f"{q.filter_bandwidth_hz / 1000:g} kHz")
    return f"{' '.join(parts)}: {name}"


class BandPlanProvider:
    name = "bandplan"

    def __init__(self, region: str = "US") -> None:
        self.region = region
        self._entries = _load_entries(region)

    def identify(self, q: IdentifyQuery) -> list[Candidate]:
        scored: list[tuple[float, int, dict[str, Any]]] = []
        digital_hint_entry: dict[str, Any] | None = None

        for entry in self._entries:
            lo, hi = entry["lo_hz"], entry["hi_hz"]
            is_channel = entry["kind"] == "channel"
            if is_channel:
                tolerance = max((q.filter_bandwidth_hz or 0) / 2, _CHANNEL_MIN_TOLERANCE_HZ)
                matched = abs(q.frequency_hz - lo) <= tolerance
                base = 0.9
            else:
                matched = lo <= q.frequency_hz <= hi
                base = 0.5
            if not matched:
                continue

            score = base
            modes = entry["modes"]
            if modes is not None and q.demodulator is not None:
                score += 0.1 if q.demodulator in modes else -0.2
            bw_hz = entry["bw_hz"]
            if bw_hz is not None and q.filter_bandwidth_hz is not None:
                score += 0.05 if bw_hz[0] <= q.filter_bandwidth_hz <= bw_hz[1] else -0.1

            scored.append((max(0.0, min(1.0, score)), hi - lo, entry))

            if (
                not is_channel
                and "land mobile" in entry["name"].lower()
                and q.demodulator == "NFM"
                and q.filter_bandwidth_hz in _DIGITAL_HINT_BW_HZ
            ):
                digital_hint_entry = entry

        # An exact channel hit (>=0.9) is already a confident, specific
        # identification; piling the generic digital-voice hint on top of it
        # (e.g. a NOAA WX or marine channel that happens to sit inside the
        # broad land-mobile band) would just be noise.
        has_strong_channel_hit = any(
            score >= 0.9 and entry["kind"] == "channel" for score, _range, entry in scored
        )

        scored.sort(key=lambda t: (-t[0], t[1]))

        candidates = []
        for confidence, _range, entry in scored[:_MAX_CANDIDATES]:
            is_channel = entry["kind"] == "channel"
            modes = entry["modes"]
            candidates.append(
                Candidate(
                    name=entry["name"],
                    category=entry["category"],
                    confidence=confidence,
                    reason=_reason(q, entry["name"]),
                    provider=self.name,
                    frequency_hz=entry["lo_hz"] if is_channel else None,
                    demodulator=modes[0] if modes else None,
                )
            )

        if digital_hint_entry is not None and not has_strong_channel_hit:
            candidates.append(
                Candidate(
                    name="Digital voice (DMR/P25/NXDN?)",
                    category="digital",
                    confidence=_DIGITAL_HINT_CONFIDENCE,
                    reason=_reason(q, "possible digital voice (narrowband FM, land mobile)"),
                    provider=self.name,
                    frequency_hz=None,
                    demodulator="NFM",
                )
            )
            candidates.sort(key=lambda c: -c.confidence)
            candidates = candidates[:_MAX_CANDIDATES]

        return candidates
