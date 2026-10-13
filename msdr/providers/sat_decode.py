"""Bundled downlinks of amateur satellites msdr can decode during a pass.

Short on purpose: only entries whose downlink and modulation are well known.
Satellites retune, change modes and deorbit, so treat this as a starting
point (verify against SatNOGS DB before relying on a pass). Entries are
validated synthetically only (see tests); no over-the-air validation.
"""

from dataclasses import dataclass

# Date the table was last reviewed. Frequencies change; bump when edited.
TABLE_DATE = "2026-10-13"


@dataclass(frozen=True)
class SatDecode:
    norad: int
    name: str
    frequency_hz: int
    mode: str  # human-readable modulation
    decoder: str  # registry decoder id


SAT_DECODE_TABLE: tuple[SatDecode, ...] = (
    SatDecode(25544, "ISS", 145_825_000, "1200 baud AFSK (APRS)", "aprs"),
    # No 9600 baud entries yet: that decoder's IQ channel is fixed and does not
    # follow Doppler (about +-10 kHz at 437 MHz), so a pass would mostly miss.
)

_BY_NORAD = {e.norad: e for e in SAT_DECODE_TABLE}


def decode_plan(norad: int) -> SatDecode | None:
    return _BY_NORAD.get(int(norad))
