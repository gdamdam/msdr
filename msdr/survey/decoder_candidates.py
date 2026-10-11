"""Conservative protocol candidates for survey labels.

These are suggestions for a follow-up decoder, not protocol identifications.
Frequency-only matches are explicitly allocation context and never represent
an IQ detection.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from msdr.decoder.registry import DECODERS

_FT8_DIAL_HZ = (
    1_840_000, 3_573_000, 5_357_000, 7_074_000, 10_136_000,
    14_074_000, 18_100_000, 21_074_000, 24_915_000, 28_074_000,
    50_313_000,
)

_MEASURED_HINTS = {
    # Shared symbol clocks make these compatibility hints, never a unique ID.
    "dstar": (
        "fm_fsk", 4800,
        "Measured FM/FSK near 4800 symbols/s; compatible, not protocol-specific.",
    ),
    "ysf": (
        "fm_fsk", 4800,
        "Measured FM/FSK near 4800 symbols/s; compatible, not protocol-specific.",
    ),
    "m17": (
        "fm_fsk", 4800,
        "Measured FM/FSK near 4800 symbols/s; compatible, not protocol-specific.",
    ),
    "rs41": (
        "fm_fsk", 4800,
        "Measured FM/FSK near the RS41 4800-symbol/s rate; many signals share it.",
    ),
    "dpmr": (
        "fm_fsk", 2400,
        "Measured FM/FSK near 2400 symbols/s; compatible, not protocol-specific.",
    ),
    "endoftrain": (
        "fm_fsk", 1200,
        "Measured FM/FSK near 1200 symbols/s; compatible, not protocol-specific.",
    ),
    "dsc": (
        "fm_fsk", 100,
        "Measured FM/FSK near 100 symbols/s; compatible with DSC, not unique.",
    ),
}


@lru_cache(maxsize=1)
def _phone_bands() -> tuple[tuple[int, int, str], ...]:
    """Use the packaged US band plan's explicitly named amateur phone bands."""
    text = resources.files("msdr.providers.data").joinpath("bandplan_us.json").read_text()
    entries = json.loads(text)
    return tuple(
        (entry["lo_hz"], entry["hi_hz"], entry["modes"][0])
        for entry in entries
        if entry.get("category") == "ham"
        and entry.get("kind") == "band"
        and "phone" in entry.get("name", "").lower()
        and entry.get("modes")
        and entry["modes"][0] in {"USB", "LSB"}
    )


def _candidate(decoder_id: str, evidence_kind: str, reason: str, next_action: str,
               limitations: str) -> dict:
    spec = DECODERS[decoder_id]
    return {
        "id": decoder_id,
        "label": spec.label,
        "evidence_kind": evidence_kind,
        "reason": reason,
        "next_action": next_action,
        "limitations": limitations,
    }


def _rate_matches(rate: float | int | None, target: int) -> bool:
    return rate is not None and abs(float(rate) - target) <= target * 0.05


def decoder_candidates(
    frequency_hz: float,
    modulation: dict | None = None,
    symbol_rate_hz: float | None = None,
    demodulator: str | None = None,
) -> list[dict]:
    """Suggest decoders from measured analyzer hints and allocation context.

    No confidence score is implied. `iq_estimate` means a measured modulation
    and symbol-rate combination is compatible; `allocation` means frequency
    context only and must never be displayed as a detected protocol.
    """
    modulation_kind = (modulation or {}).get("kind")
    result: list[dict] = []
    for decoder_id, (expected_kind, rate, reason) in _MEASURED_HINTS.items():
        if modulation_kind == expected_kind and _rate_matches(symbol_rate_hz, rate):
            result.append(
                _candidate(
                    decoder_id,
                    "iq_estimate",
                    reason,
                    "Try this decoder and require protocol output or integrity checks "
                    "before identifying the signal.",
                    "A modulation estimate and symbol clock cannot distinguish this "
                    "protocol from unrelated emitters.",
                )
            )

    # AM is an observed modulation hint; the allocation is still needed to
    # make a navigation receiver a reasonable next test.
    if modulation_kind == "am":
        if 108_000_000 <= frequency_hz <= 117_975_000:
            result.append(
                _candidate(
                    "vor",
                    "iq_estimate",
                    "Measured AM within the VOR allocation.",
                    "Try the VOR decoder; require a valid radial/ident before naming a station.",
                    "AM and frequency overlap other services; this does not identify a VOR.",
                )
            )
        if 108_100_000 <= frequency_hz <= 111_950_000 or 329_000_000 <= frequency_hz <= 335_000_000:
            result.append(
                _candidate(
                    "ils",
                    "iq_estimate",
                    "Measured AM within an ILS localizer or glide-slope allocation.",
                    "Try the ILS decoder; require stable course/deviation evidence "
                    "before identifying a runway.",
                    "Frequency and AM are not unique to ILS; glide-slope and localizer "
                    "ranges have different roles.",
                )
            )

    allocation_contexts = []
    if abs(frequency_hz - 1_090_000_000) <= 1_000_000:
        allocation_contexts.append(
            (
                "adsb",
                "Frequency is near 1090 MHz, an ADS-B / Mode S allocation.",
                "Try the ADS-B decoder; identify aircraft only from a valid decoded message.",
                "Frequency context alone does not detect an aircraft or prove a Mode S "
                "transmission.",
            )
        )
    if 108_000_000 <= frequency_hz <= 117_975_000:
        allocation_contexts.append(
            (
                "vor",
                "Frequency lies in the VOR navigation allocation.",
                "Try VOR decoding and require a stable valid radial before assigning an identity.",
                "Frequency allocation context only; no VOR modulation or station has been decoded.",
            )
        )
    if 108_100_000 <= frequency_hz <= 111_950_000 or 329_000_000 <= frequency_hz <= 335_000_000:
        allocation_contexts.append(
            (
                "ils",
                "Frequency lies in an ILS localizer or glide-slope allocation.",
                "Try ILS decoding and require stable course/deviation evidence "
                "before identifying a runway.",
                "Frequency allocation context only; the two ILS bands carry different components.",
            )
        )
    if any(abs(frequency_hz - center) <= 3_500 for center in _FT8_DIAL_HZ):
        allocation_contexts.append(
            (
                "ft8",
                "Frequency is near a commonly used amateur FT8 dial frequency.",
                "Try FT8 decoding on the appropriate USB passband and require decoded, "
                "checked messages.",
                "This is band-plan context only; an FT8 signal has not been detected or decoded.",
            )
        )

    existing = {item["id"] for item in result}
    for decoder_id, reason, next_action, limitations in allocation_contexts:
        if decoder_id not in existing:
            result.append(_candidate(decoder_id, "allocation", reason, next_action, limitations))
            existing.add(decoder_id)
    return result
