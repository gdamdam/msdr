"""Command palette search: frequency parsing shortcut, fuzzy text ranking, and
the "what am I hearing" nearest-signal lookup used to auto-fill heard counts."""

import re
from dataclasses import dataclass, field
from typing import Literal

from msdr.engine.state import ReceiverState
from msdr.library.frequency import format_mhz, parse_frequency
from msdr.library.models import ReceiverProfile, Signal, Tag

# Score bands are spaced far enough apart that the length tie-breaker (which
# subtracts len(text)) can never make a lower band outrank a higher one for
# any realistic title/station/tag string.
_WORD_PREFIX_BASE = 10_000.0
_SUBSEQUENCE_BASE = 1_000.0

ACTIONS = [
    ("back", "Back"),
    ("forward", "Forward"),
    ("capture", "Save current frequency"),
    ("export", "Export library"),
    # Layout presets (msdr/ui/format.js LAYOUT_PRESETS) -- the "layout-"
    # prefix on the id is what msdr/ui/components/palette.js keys off of to
    # dispatch apply-layout instead of a bare same-named event.
    ("layout-listening", "layout: listening"),
    ("layout-library", "layout: library"),
    ("layout-satellites", "layout: satellites"),
    ("layout-default", "layout: default"),
    # Scanner controls (v0.12.0) -- ids double as the palette's event names,
    # handled by app.js's _doScan* like the SCANNER section's own buttons.
    ("scan-stop", "scan: stop"),
    ("scan-pause", "scan: pause"),
    ("scan-resume", "scan: resume"),
    ("show-getting-started", "getting started"),
]


@dataclass
class PaletteResult:
    kind: Literal["tune_frequency", "signal", "profile", "action"]
    id: str | None
    title: str
    subtitle: str
    score: float
    frequency_hz: int | None = None
    color: str | None = None
    tag_ids: list[str] = field(default_factory=list)


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def fuzzy_score(query: str, text: str) -> float:
    """0 = no match. Exact word-prefix match scores highest, then subsequence
    match; shorter text breaks ties within a band."""
    if not query or not text:
        return 0.0
    q = query.lower()
    t = text.lower()
    words = re.split(r"\W+", t)
    if any(word.startswith(q) for word in words if word):
        return _WORD_PREFIX_BASE - len(t)
    if _is_subsequence(q, t):
        return _SUBSEQUENCE_BASE - len(t)
    return 0.0


def nearby(
    signals: list[Signal], hz: int, *, bandwidth_hz: int | None, nearby_hz: int
) -> list[Signal]:
    """Signals within max(bandwidth/2, nearby_hz) of hz, nearest first."""
    radius = max((bandwidth_hz or 0) / 2, nearby_hz)
    candidates = [s for s in signals if abs(s.frequency_hz - hz) <= radius]
    candidates.sort(key=lambda s: abs(s.frequency_hz - hz))
    return candidates


def match_heard(signals: list[Signal], state: ReceiverState, *, nearby_hz: int) -> Signal | None:
    """The signal the receiver is most likely tuned to right now, if any."""
    candidates = nearby(
        signals, state.vfo_hz, bandwidth_hz=state.filter_bandwidth_hz, nearby_hz=nearby_hz
    )
    compatible = [
        s for s in candidates if s.demodulator is None or s.demodulator == state.demodulator
    ]
    return compatible[0] if compatible else None


def rank(
    query: str,
    signals: list[Signal],
    profiles: list[ReceiverProfile],
    tags: dict[str, Tag],
    *,
    nearby_hz: int,
    limit: int = 20,
) -> list[PaletteResult]:
    if not query:
        return []

    freq = parse_frequency(query)
    if freq is not None:
        results = [
            PaletteResult(
                kind="tune_frequency",
                id=None,
                title=f"Tune to {format_mhz(freq)}",
                subtitle="",
                score=_WORD_PREFIX_BASE,
                frequency_hz=freq,
            )
        ]
        for signal in nearby(signals, freq, bandwidth_hz=None, nearby_hz=nearby_hz):
            distance_khz = (signal.frequency_hz - freq) / 1000
            subtitle = f"{distance_khz:+.1f} kHz"
            results.append(
                PaletteResult(
                    kind="signal",
                    id=signal.id,
                    title=signal.name,
                    subtitle=subtitle,
                    score=_WORD_PREFIX_BASE - abs(distance_khz),
                    frequency_hz=signal.frequency_hz,
                    color=signal.color,
                    tag_ids=signal.tag_ids,
                )
            )
        return results[:limit]

    results = []
    for signal in signals:
        tag_names = [tags[tid].name for tid in signal.tag_ids if tid in tags]
        candidates = (signal.name, signal.station, *tag_names)
        best = max((fuzzy_score(query, candidate) for candidate in candidates), default=0.0)
        if best > 0:
            results.append(
                PaletteResult(
                    kind="signal",
                    id=signal.id,
                    title=signal.name,
                    subtitle=signal.station,
                    score=best,
                    frequency_hz=signal.frequency_hz,
                    color=signal.color,
                    tag_ids=signal.tag_ids,
                )
            )
    for profile in profiles:
        score = fuzzy_score(query, profile.name)
        if score > 0:
            results.append(
                PaletteResult(
                    kind="profile",
                    id=profile.id,
                    title=f"Profile: {profile.name}",
                    subtitle=profile.description,
                    score=score,
                )
            )
    for action_id, action_title in ACTIONS:
        score = fuzzy_score(query, action_title)
        if score > 0:
            results.append(
                PaletteResult(
                    kind="action", id=action_id, title=action_title, subtitle="", score=score
                )
            )

    results.sort(key=lambda r: (-r.score, r.title))
    return results[:limit]
